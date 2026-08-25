from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.schemas import NexTripModel


KB_VERSION = "v8"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class V8EmbeddingError(RuntimeError):
    """Base error for a release-aware semantic embedding run."""


class V8EmbeddingReleaseError(V8EmbeddingError):
    """Raised when Neo4j and the pinned canonical dataset do not match."""


class V8EmbeddingWriteError(V8EmbeddingError):
    """Raised when a target changes during an embedding write."""


class V8EmbeddingCoverageError(V8EmbeddingError):
    """Raised when all required release nodes are not embedding-ready."""


class _GraphStore(Protocol):
    def run(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


class _Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class V8EmbeddingTargetType(StrEnum):
    PLACE = "place"
    CONCEPT = "concept"
    TEXT_UNIT = "text_unit"


class V8EmbeddingTarget(NexTripModel):
    target_type: V8EmbeddingTargetType
    node_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=_SHA256_PATTERN)
    text: str = Field(min_length=1)
    embedding_current: bool = False


class V8EmbeddingPlan(NexTripModel):
    release_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    targets: list[V8EmbeddingTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self) -> V8EmbeddingPlan:
        expected = sorted(
            self.targets,
            key=lambda item: (item.target_type.value, item.node_id),
        )
        if self.targets != expected:
            raise ValueError("embedding targets must be sorted")
        identities = [(item.target_type, item.node_id) for item in self.targets]
        if len(identities) != len(set(identities)):
            raise ValueError("embedding targets must be unique per family")
        return self


class V8EmbeddingFamilyResult(NexTripModel):
    target_type: V8EmbeddingTargetType
    target_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    embedded_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)


class V8EmbeddingRunResult(NexTripModel):
    run_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=_SHA256_PATTERN)
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    families: list[V8EmbeddingFamilyResult]
    semantic_index_status: str = Field(pattern=r"^(pending|ready)$")

    @property
    def ready(self) -> bool:
        return self.semantic_index_status == "ready"


_ACTIVE_RELEASE_QUERY = """
// v8-embedding:active-release
MATCH (release:DatasetRelease {kb_version: $kb_version, status: 'active'})
MATCH (catalog:TravelCatalog {kb_version: $kb_version})
      -[:CURRENT_RELEASE]->(release)
RETURN release.id AS release_id,
       release.dataset_id AS dataset_id,
       release.dataset_hash AS dataset_hash,
       release.place_count AS place_count,
       release.text_unit_count AS text_unit_count,
       catalog.embedding_dimension AS embedding_dimension,
       catalog.semantic_index_status AS semantic_index_status
"""

_TARGET_QUERIES = {
    V8EmbeddingTargetType.PLACE: """
// v8-embedding:targets-place
MATCH (place:Place {kb_version: $kb_version, current_release_id: $release_id})
RETURN place.id AS node_id,
       coalesce(
         properties(place)[$semantic_text_key],
         place.entity_profile,
         place.search_text
       ) AS text,
       properties(place)[$semantic_hash_key] AS content_hash,
       place.embedding IS NOT NULL AS embedding_present,
       properties(place)[$embedding_model_key] AS embedding_model,
       properties(place)[$embedding_dimension_key] AS embedding_dimension,
       properties(place)[$embedding_hash_key] AS embedding_content_hash
ORDER BY node_id
""",
    V8EmbeddingTargetType.CONCEPT: """
// v8-embedding:targets-concept
MATCH (:Place {kb_version: $kb_version, current_release_id: $release_id})
      -[:HAS_CONCEPT]->(concept:Concept {kb_version: $kb_version})
WITH DISTINCT concept
RETURN concept.id AS node_id,
       coalesce(
         properties(concept)[$semantic_text_key],
         concept.canonical_name,
         concept.name
       ) AS text,
       properties(concept)[$semantic_hash_key] AS content_hash,
       concept.embedding IS NOT NULL AS embedding_present,
       properties(concept)[$embedding_model_key] AS embedding_model,
       properties(concept)[$embedding_dimension_key] AS embedding_dimension,
       properties(concept)[$embedding_hash_key] AS embedding_content_hash
ORDER BY node_id
""",
    V8EmbeddingTargetType.TEXT_UNIT: """
// v8-embedding:targets-text-unit
MATCH (release:DatasetRelease {
  id: $release_id,
  kb_version: $kb_version,
  status: 'active'
})-[:CONTAINS_TEXT_UNIT]->(unit:CanonicalTextUnit:TextUnit {
  kb_version: $kb_version
})
RETURN unit.id AS node_id,
       coalesce(properties(unit)[$semantic_text_key], unit.text) AS text,
       properties(unit)[$semantic_hash_key] AS content_hash,
       unit.embedding IS NOT NULL AS embedding_present,
       properties(unit)[$embedding_model_key] AS embedding_model,
       properties(unit)[$embedding_dimension_key] AS embedding_dimension,
       properties(unit)[$embedding_hash_key] AS embedding_content_hash
ORDER BY node_id
""",
}

_WRITE_QUERIES = {
    V8EmbeddingTargetType.PLACE: """
// v8-embedding:write-place
UNWIND $rows AS row
MATCH (node:Place {
  id: row.node_id,
  kb_version: $kb_version,
  current_release_id: $release_id
})
WHERE coalesce(node.semantic_content_hash, row.content_hash) = row.content_hash
CALL db.create.setNodeVectorProperty(node, 'embedding', row.embedding)
SET node.semantic_text = row.text,
    node.semantic_content_hash = row.content_hash,
    node.embedding_content_hash = row.content_hash,
    node.embedding_model = $model,
    node.embedding_dimension = $dimension,
    node.embedding_updated_at = $updated_at
RETURN count(node) AS updated_count
""",
    V8EmbeddingTargetType.CONCEPT: """
// v8-embedding:write-concept
UNWIND $rows AS row
MATCH (:Place {kb_version: $kb_version, current_release_id: $release_id})
      -[:HAS_CONCEPT]->(node:Concept {
        id: row.node_id,
        kb_version: $kb_version
      })
WITH DISTINCT row, node
WHERE coalesce(node.semantic_content_hash, row.content_hash) = row.content_hash
CALL db.create.setNodeVectorProperty(node, 'embedding', row.embedding)
SET node.semantic_text = row.text,
    node.semantic_content_hash = row.content_hash,
    node.embedding_content_hash = row.content_hash,
    node.embedding_model = $model,
    node.embedding_dimension = $dimension,
    node.embedding_updated_at = $updated_at
RETURN count(node) AS updated_count
""",
    V8EmbeddingTargetType.TEXT_UNIT: """
// v8-embedding:write-text-unit
UNWIND $rows AS row
MATCH (release:DatasetRelease {
  id: $release_id,
  kb_version: $kb_version,
  status: 'active'
})-[:CONTAINS_TEXT_UNIT]->(node:CanonicalTextUnit:TextUnit {
  id: row.node_id,
  kb_version: $kb_version
})
WHERE coalesce(node.semantic_content_hash, row.content_hash) = row.content_hash
CALL db.create.setNodeVectorProperty(node, 'embedding', row.embedding)
SET node.semantic_text = row.text,
    node.semantic_content_hash = row.content_hash,
    node.embedding_content_hash = row.content_hash,
    node.embedding_model = $model,
    node.embedding_dimension = $dimension,
    node.embedding_updated_at = $updated_at
RETURN count(node) AS updated_count
""",
}

_MARK_READY_QUERY = """
// v8-embedding:mark-ready
MATCH (release:DatasetRelease {
  id: $release_id,
  kb_version: $kb_version,
  status: 'active',
  dataset_id: $dataset_id,
  dataset_hash: $dataset_hash
})
MATCH (catalog:TravelCatalog {kb_version: $kb_version})
      -[:CURRENT_RELEASE]->(release)
WHERE catalog.embedding_dimension = $dimension
SET release.semantic_index_status = 'ready',
    release.semantic_index_ready = true,
    release.embedding_model = $model,
    release.embedding_completed_at = $completed_at,
    catalog.semantic_index_status = 'ready',
    catalog.semantic_index_ready = true,
    catalog.embedding_model = $model,
    catalog.embedding_completed_at = $completed_at
RETURN release.id AS release_id,
       catalog.semantic_index_status AS semantic_index_status
"""


def build_v8_embedding_plan(
    store: _GraphStore,
    dataset: CanonicalActiveDataset,
    *,
    model: str,
    dimension: int,
) -> V8EmbeddingPlan:
    """Pin semantic targets to exactly one active canonical V8 release."""

    return _build_v8_embedding_plan_for_identity(
        store,
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        expected_place_count=len(dataset.records),
        model=model,
        dimension=dimension,
    )


def _build_v8_embedding_plan_for_identity(
    store: _GraphStore,
    *,
    dataset_id: str,
    dataset_hash: str,
    expected_place_count: int,
    model: str,
    dimension: int,
) -> V8EmbeddingPlan:

    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("embedding model is required")
    if dimension <= 0:
        raise ValueError("embedding dimension must be positive")
    active_rows = store.run(_ACTIVE_RELEASE_QUERY, kb_version=KB_VERSION)
    if len(active_rows) != 1:
        raise V8EmbeddingReleaseError(
            "expected exactly one active V8 release with a current catalog"
        )
    active = active_rows[0]
    if (active.get("dataset_id"), active.get("dataset_hash")) != (
        dataset_id,
        dataset_hash,
    ):
        raise V8EmbeddingReleaseError(
            "active V8 release does not match the pinned canonical dataset"
        )
    if int(active.get("embedding_dimension") or -1) != dimension:
        raise V8EmbeddingReleaseError(
            "active V8 release embedding dimension does not match the job"
        )
    release_id = str(active.get("release_id") or "")
    if not release_id:
        raise V8EmbeddingReleaseError("active V8 release has no release_id")

    targets: list[V8EmbeddingTarget] = []
    for target_type in V8EmbeddingTargetType:
        rows = store.run(
            _TARGET_QUERIES[target_type],
            kb_version=KB_VERSION,
            release_id=release_id,
            semantic_text_key="semantic_text",
            semantic_hash_key="semantic_content_hash",
            embedding_model_key="embedding_model",
            embedding_dimension_key="embedding_dimension",
            embedding_hash_key="embedding_content_hash",
        )
        for row in rows:
            node_id = str(row.get("node_id") or "").strip()
            text = str(row.get("text") or "").strip()
            if not node_id or not text:
                raise V8EmbeddingReleaseError(
                    f"{target_type.value} target is missing id or semantic text"
                )
            content_hash = str(row.get("content_hash") or "").strip()
            if not content_hash:
                content_hash = stable_sha256(
                    {"target": target_type.value, "text": text}
                )
            current = (
                bool(row.get("embedding_present"))
                and row.get("embedding_model") == normalized_model
                and int(row.get("embedding_dimension") or -1) == dimension
                and row.get("embedding_content_hash") == content_hash
            )
            targets.append(
                V8EmbeddingTarget(
                    target_type=target_type,
                    node_id=node_id,
                    content_hash=content_hash,
                    text=text,
                    embedding_current=current,
                )
            )
    targets.sort(key=lambda item: (item.target_type.value, item.node_id))
    place_count = sum(
        item.target_type is V8EmbeddingTargetType.PLACE for item in targets
    )
    text_unit_count = sum(
        item.target_type is V8EmbeddingTargetType.TEXT_UNIT for item in targets
    )
    expected = expected_place_count
    if (
        place_count != expected
        or text_unit_count != expected
        or int(active.get("place_count") or -1) != expected
        or int(active.get("text_unit_count") or -1) != expected
    ):
        raise V8EmbeddingReleaseError(
            "active release embedding targets do not cover the canonical dataset"
        )
    return V8EmbeddingPlan(
        release_id=release_id,
        dataset_id=dataset_id,
        dataset_hash=dataset_hash,
        model=normalized_model,
        dimension=dimension,
        targets=targets,
    )


def apply_v8_embedding_plan(
    store: _GraphStore,
    plan: V8EmbeddingPlan,
    embedder: _Embedder,
    *,
    run_id: str,
    batch_size: int = 16,
    clock: Callable[[], datetime] | None = None,
) -> V8EmbeddingRunResult:
    """Embed only missing targets, verify full coverage, then mark ready."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not run_id.strip():
        raise ValueError("run_id is required")
    active_clock = clock or (lambda: datetime.now(timezone.utc))
    started_at = active_clock()
    embedded_by_type = {item: 0 for item in V8EmbeddingTargetType}
    for target_type in V8EmbeddingTargetType:
        pending = [
            item
            for item in plan.targets
            if item.target_type is target_type and not item.embedding_current
        ]
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            vectors = embedder.embed_documents([item.text for item in batch])
            if len(vectors) != len(batch):
                raise V8EmbeddingWriteError(
                    "embedding provider returned a different vector count"
                )
            rows = []
            for target, vector in zip(batch, vectors, strict=True):
                _validate_vector(vector, plan.dimension)
                rows.append(
                    {
                        "node_id": target.node_id,
                        "content_hash": target.content_hash,
                        "text": target.text,
                        "embedding": vector,
                    }
                )
            updated_rows = store.run(
                _WRITE_QUERIES[target_type],
                rows=rows,
                kb_version=KB_VERSION,
                release_id=plan.release_id,
                model=plan.model,
                dimension=plan.dimension,
                updated_at=_iso_z(active_clock()),
            )
            updated_count = (
                int(updated_rows[0].get("updated_count") or 0)
                if len(updated_rows) == 1
                else -1
            )
            if updated_count != len(rows):
                raise V8EmbeddingWriteError(
                    f"{target_type.value} target changed during embedding write"
                )
            embedded_by_type[target_type] += updated_count

    refreshed = _refresh_plan(store, plan)
    families = []
    for target_type in V8EmbeddingTargetType:
        original = [
            item for item in plan.targets if item.target_type is target_type
        ]
        current = [
            item for item in refreshed.targets if item.target_type is target_type
        ]
        ready_count = sum(item.embedding_current for item in current)
        families.append(
            V8EmbeddingFamilyResult(
                target_type=target_type,
                target_count=len(original),
                reused_count=sum(item.embedding_current for item in original),
                embedded_count=embedded_by_type[target_type],
                ready_count=ready_count,
            )
        )
    if any(item.ready_count != item.target_count for item in families):
        raise V8EmbeddingCoverageError(
            "semantic index remains pending because embedding coverage is incomplete"
        )
    completed_at = active_clock()
    marked = store.run(
        _MARK_READY_QUERY,
        release_id=plan.release_id,
        dataset_id=plan.dataset_id,
        dataset_hash=plan.dataset_hash,
        kb_version=KB_VERSION,
        model=plan.model,
        dimension=plan.dimension,
        completed_at=_iso_z(completed_at),
    )
    if marked != [
        {"release_id": plan.release_id, "semantic_index_status": "ready"}
    ]:
        raise V8EmbeddingWriteError(
            "active release changed before semantic readiness promotion"
        )
    return V8EmbeddingRunResult(
        run_id=run_id,
        release_id=plan.release_id,
        dataset_id=plan.dataset_id,
        dataset_hash=plan.dataset_hash,
        model=plan.model,
        dimension=plan.dimension,
        started_at=started_at,
        finished_at=completed_at,
        families=families,
        semantic_index_status="ready",
    )


def _refresh_plan(store: _GraphStore, plan: V8EmbeddingPlan) -> V8EmbeddingPlan:
    refreshed = _build_v8_embedding_plan_for_identity(
        store,
        dataset_id=plan.dataset_id,
        dataset_hash=plan.dataset_hash,
        expected_place_count=sum(
            item.target_type is V8EmbeddingTargetType.PLACE
            for item in plan.targets
        ),
        model=plan.model,
        dimension=plan.dimension,
    )
    original_identity = [
        (item.target_type, item.node_id, item.content_hash, item.text)
        for item in plan.targets
    ]
    refreshed_identity = [
        (item.target_type, item.node_id, item.content_hash, item.text)
        for item in refreshed.targets
    ]
    if refreshed.release_id != plan.release_id or refreshed_identity != original_identity:
        raise V8EmbeddingReleaseError(
            "active release or semantic target content changed during embedding"
        )
    return refreshed


def _validate_vector(vector: Sequence[float], dimension: int) -> None:
    if len(vector) != dimension:
        raise V8EmbeddingWriteError(
            f"embedding dimension mismatch: expected {dimension}, got {len(vector)}"
        )
    if any(not math.isfinite(float(value)) for value in vector):
        raise V8EmbeddingWriteError("embedding contains a non-finite value")


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("embedding timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class V8EmbeddingRunWriter:
    """Write one immutable summary for an attempt-unique embedding run."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def write(self, result: V8EmbeddingRunResult) -> Path:
        path = self.root / f"run={quote(result.run_id, safe='-_.')}" / "summary.json"
        payload = (
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != payload:
                raise FileExistsError(f"immutable embedding summary differs: {path}")
            return path
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        return path


__all__ = [
    "V8EmbeddingCoverageError",
    "V8EmbeddingError",
    "V8EmbeddingFamilyResult",
    "V8EmbeddingPlan",
    "V8EmbeddingReleaseError",
    "V8EmbeddingRunResult",
    "V8EmbeddingRunWriter",
    "V8EmbeddingTarget",
    "V8EmbeddingTargetType",
    "V8EmbeddingWriteError",
    "apply_v8_embedding_plan",
    "build_v8_embedding_plan",
]
