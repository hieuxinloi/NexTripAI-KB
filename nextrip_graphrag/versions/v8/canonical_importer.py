from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

from pydantic import Field, model_validator

from nextrip_pipeline.canonical.completeness import (
    CanonicalCompletenessAudit,
    CompletenessSeverity,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessReport,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import NexTripModel


KB_VERSION = "v8"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class CanonicalV8ImportError(RuntimeError):
    """Base error for a canonical V8 import that must fail closed."""


class CanonicalV8ArtifactError(CanonicalV8ImportError):
    """Raised when an immutable source artifact is invalid or mismatched."""


class CanonicalV8GateError(CanonicalV8ImportError):
    """Raised when identity or static-ingest readiness blocks publication."""


class CanonicalV8GraphWriteError(CanonicalV8ImportError):
    """Raised when staged graph data does not match the release manifest."""


class CanonicalV8ReleaseAlreadyExistsError(FileExistsError):
    """Raised rather than overwrite an immutable release manifest."""


class _GraphStore(Protocol):
    driver: Any
    settings: Any

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


class CanonicalV8ArtifactDigest(NexTripModel):
    kind: Literal["completeness", "dataset", "readiness"]
    artifact_id: str = Field(min_length=1)
    semantic_hash: str = Field(pattern=_SHA256_PATTERN)
    file_sha256: str = Field(pattern=_SHA256_PATTERN)


class CanonicalV8EntityCount(NexTripModel):
    entity_type: str = Field(min_length=1)
    count: int = Field(ge=0)


def _release_payload(
    *,
    schema_version: Literal["1.1.0"],
    kb_version: Literal["v8"],
    dataset_id: str,
    dataset_hash: str,
    canonical_manifest_id: str,
    canonical_manifest_hash: str,
    readiness_id: str,
    readiness_hash: str,
    completeness_audit_id: str,
    completeness_audit_hash: str,
    source_as_of: str,
    place_count: int,
    city_count: int,
    document_count: int,
    text_unit_count: int,
    fact_count: int,
    claim_count: int,
    entity_counts: list[CanonicalV8EntityCount],
    record_set_hash: str,
    artifact_digests: list[CanonicalV8ArtifactDigest],
    embedding_dimension: int,
    semantic_index_status: Literal["pending"],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "kb_version": kb_version,
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "canonical_manifest_id": canonical_manifest_id,
        "canonical_manifest_hash": canonical_manifest_hash,
        "readiness_id": readiness_id,
        "readiness_hash": readiness_hash,
        "completeness_audit_id": completeness_audit_id,
        "completeness_audit_hash": completeness_audit_hash,
        "source_as_of": source_as_of,
        "place_count": place_count,
        "city_count": city_count,
        "document_count": document_count,
        "text_unit_count": text_unit_count,
        "fact_count": fact_count,
        "claim_count": claim_count,
        "entity_counts": [item.model_dump(mode="json") for item in entity_counts],
        "record_set_hash": record_set_hash,
        "artifact_digests": [item.model_dump(mode="json") for item in artifact_digests],
        "embedding_dimension": embedding_dimension,
        "semantic_index_status": semantic_index_status,
    }


class CanonicalV8ReleaseManifest(NexTripModel):
    """Content-addressed, deterministic contract for one V8 graph release."""

    schema_version: Literal["1.1.0"] = "1.1.0"
    release_id: str = Field(min_length=1)
    release_hash: str = Field(pattern=_SHA256_PATTERN)
    kb_version: Literal["v8"] = "v8"
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=_SHA256_PATTERN)
    canonical_manifest_id: str = Field(min_length=1)
    canonical_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    readiness_id: str = Field(min_length=1)
    readiness_hash: str = Field(pattern=_SHA256_PATTERN)
    completeness_audit_id: str = Field(min_length=1)
    completeness_audit_hash: str = Field(pattern=_SHA256_PATTERN)
    source_as_of: str = Field(min_length=1)
    place_count: int = Field(ge=1)
    city_count: int = Field(ge=1)
    document_count: int = Field(ge=1)
    text_unit_count: int = Field(ge=1)
    fact_count: int = Field(ge=1)
    claim_count: int = Field(ge=0)
    entity_counts: list[CanonicalV8EntityCount] = Field(min_length=1)
    record_set_hash: str = Field(pattern=_SHA256_PATTERN)
    artifact_digests: list[CanonicalV8ArtifactDigest] = Field(min_length=3)
    embedding_dimension: int = Field(default=1536, gt=0)
    semantic_index_status: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def validate_release(self) -> CanonicalV8ReleaseManifest:
        expected_counts = sorted(
            self.entity_counts,
            key=lambda item: item.entity_type,
        )
        if self.entity_counts != expected_counts:
            raise ValueError("entity_counts must be sorted")
        if len({item.entity_type for item in self.entity_counts}) != len(
            self.entity_counts
        ):
            raise ValueError("entity_counts must be unique")
        if sum(item.count for item in self.entity_counts) != self.place_count:
            raise ValueError("entity_counts must sum to place_count")
        if self.document_count != self.place_count:
            raise ValueError("each place version requires exactly one document")
        if self.text_unit_count != self.place_count:
            raise ValueError("each place version requires exactly one text unit")
        expected_digests = sorted(
            self.artifact_digests,
            key=lambda item: item.kind,
        )
        if self.artifact_digests != expected_digests:
            raise ValueError("artifact_digests must be sorted")
        if {item.kind for item in self.artifact_digests} != {
            "completeness",
            "dataset",
            "readiness",
        }:
            raise ValueError("release requires exactly three source artifacts")
        payload = _release_payload(
            schema_version=self.schema_version,
            kb_version=self.kb_version,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            canonical_manifest_id=self.canonical_manifest_id,
            canonical_manifest_hash=self.canonical_manifest_hash,
            readiness_id=self.readiness_id,
            readiness_hash=self.readiness_hash,
            completeness_audit_id=self.completeness_audit_id,
            completeness_audit_hash=self.completeness_audit_hash,
            source_as_of=self.source_as_of,
            place_count=self.place_count,
            city_count=self.city_count,
            document_count=self.document_count,
            text_unit_count=self.text_unit_count,
            fact_count=self.fact_count,
            claim_count=self.claim_count,
            entity_counts=self.entity_counts,
            record_set_hash=self.record_set_hash,
            artifact_digests=self.artifact_digests,
            embedding_dimension=self.embedding_dimension,
            semantic_index_status=self.semantic_index_status,
        )
        expected_hash = stable_sha256(payload)
        if self.release_hash != expected_hash:
            raise ValueError("release_hash does not match release content")
        if self.release_id != f"v8-canonical-release-{expected_hash[:20]}":
            raise ValueError("release_id does not match release_hash")
        return self


class CanonicalV8ImportResult(NexTripModel):
    release_id: str = Field(min_length=1)
    release_hash: str = Field(pattern=_SHA256_PATTERN)
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=_SHA256_PATTERN)
    dry_run: bool
    status: Literal["validated", "active"]
    place_count: int = Field(ge=1)
    city_count: int = Field(ge=1)
    place_version_count: int = Field(ge=1)
    provenance_count: int = Field(ge=1)
    type_count: int = Field(ge=1)
    category_count: int = Field(ge=1)
    concept_count: int = Field(ge=0)
    document_count: int = Field(ge=1)
    text_unit_count: int = Field(ge=1)
    fact_count: int = Field(ge=1)
    claim_count: int = Field(ge=0)
    static_graph_ready: bool
    semantic_index_ready: bool
    semantic_index_status: Literal["pending", "ready"]


@dataclass(frozen=True)
class CanonicalV8ImportPlan:
    """Validated rows ready for dry-run inspection or one atomic graph write."""

    release: CanonicalV8ReleaseManifest
    cities: tuple[dict[str, Any], ...]
    places: tuple[dict[str, Any], ...]
    versions: tuple[dict[str, Any], ...]
    provenances: tuple[dict[str, Any], ...]
    place_types: tuple[dict[str, Any], ...]
    categories: tuple[dict[str, Any], ...]
    concepts: tuple[dict[str, Any], ...]
    documents: tuple[dict[str, Any], ...]
    text_units: tuple[dict[str, Any], ...]
    facts: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]

    def dry_run_result(self) -> CanonicalV8ImportResult:
        return CanonicalV8ImportResult(
            release_id=self.release.release_id,
            release_hash=self.release.release_hash,
            dataset_id=self.release.dataset_id,
            dataset_hash=self.release.dataset_hash,
            dry_run=True,
            status="validated",
            place_count=len(self.places),
            city_count=len(self.cities),
            place_version_count=len(self.versions),
            provenance_count=len(self.provenances),
            type_count=len(self.place_types),
            category_count=len(self.categories),
            concept_count=len(self.concepts),
            document_count=len(self.documents),
            text_unit_count=len(self.text_units),
            fact_count=len(self.facts),
            claim_count=len(self.claims),
            static_graph_ready=True,
            semantic_index_ready=False,
            semantic_index_status="pending",
        )


@dataclass(frozen=True)
class _LoadedArtifacts:
    dataset: CanonicalActiveDataset
    readiness: CanonicalDatasetReadinessReport
    completeness: CanonicalCompletenessAudit
    digests: tuple[CanonicalV8ArtifactDigest, ...]


def prepare_canonical_v8_import(
    dataset_path: str | Path,
    readiness_path: str | Path,
    completeness_path: str | Path,
    *,
    embedding_dimension: int = 1536,
) -> CanonicalV8ImportPlan:
    """Read immutable artifacts once, validate gates, and build deterministic rows."""

    if embedding_dimension <= 0:
        raise ValueError("embedding_dimension must be positive")
    artifacts = _load_artifacts(
        dataset_path=dataset_path,
        readiness_path=readiness_path,
        completeness_path=completeness_path,
    )
    _validate_artifact_contract(artifacts)
    return _build_import_plan(
        artifacts,
        embedding_dimension=embedding_dimension,
    )


def dry_run_canonical_v8_import(
    dataset_path: str | Path,
    readiness_path: str | Path,
    completeness_path: str | Path,
    *,
    embedding_dimension: int = 1536,
) -> CanonicalV8ImportResult:
    """Validate and project a release without importing Neo4j or reading env vars."""

    return prepare_canonical_v8_import(
        dataset_path,
        readiness_path,
        completeness_path,
        embedding_dimension=embedding_dimension,
    ).dry_run_result()


def _load_artifacts(
    *,
    dataset_path: str | Path,
    readiness_path: str | Path,
    completeness_path: str | Path,
) -> _LoadedArtifacts:
    dataset, dataset_digest = _read_artifact(
        dataset_path,
        kind="dataset",
        model=CanonicalActiveDataset,
        id_field="dataset_id",
        hash_field="dataset_hash",
    )
    readiness, readiness_digest = _read_artifact(
        readiness_path,
        kind="readiness",
        model=CanonicalDatasetReadinessReport,
        id_field="readiness_id",
        hash_field="readiness_hash",
    )
    completeness, completeness_digest = _read_artifact(
        completeness_path,
        kind="completeness",
        model=CanonicalCompletenessAudit,
        id_field="audit_id",
        hash_field="audit_hash",
    )
    return _LoadedArtifacts(
        dataset=dataset,
        readiness=readiness,
        completeness=completeness,
        digests=tuple(
            sorted(
                (dataset_digest, readiness_digest, completeness_digest),
                key=lambda item: item.kind,
            )
        ),
    )


def _read_artifact(
    path: str | Path,
    *,
    kind: Literal["completeness", "dataset", "readiness"],
    model: Any,
    id_field: str,
    hash_field: str,
) -> tuple[Any, CanonicalV8ArtifactDigest]:
    artifact_path = Path(path)
    try:
        payload = artifact_path.read_bytes()
        parsed = model.model_validate_json(payload)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalV8ArtifactError(
            f"invalid immutable {kind} artifact: {artifact_path}"
        ) from error
    digest = CanonicalV8ArtifactDigest(
        kind=kind,
        artifact_id=str(getattr(parsed, id_field)),
        semantic_hash=str(getattr(parsed, hash_field)),
        file_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return parsed, digest


def _validate_artifact_contract(artifacts: _LoadedArtifacts) -> None:
    dataset = artifacts.dataset
    readiness = artifacts.readiness
    completeness = artifacts.completeness
    if not dataset.records:
        raise CanonicalV8GateError("refusing to import an empty canonical dataset")
    if (readiness.dataset_id, readiness.dataset_hash) != (
        dataset.dataset_id,
        dataset.dataset_hash,
    ):
        raise CanonicalV8ArtifactError(
            "readiness report belongs to a different canonical dataset"
        )
    if (readiness.manifest_id, readiness.manifest_hash) != (
        dataset.manifest_id,
        dataset.manifest_hash,
    ):
        raise CanonicalV8ArtifactError(
            "readiness report belongs to a different canonical manifest"
        )
    if (completeness.dataset_id, completeness.dataset_hash) != (
        dataset.dataset_id,
        dataset.dataset_hash,
    ):
        raise CanonicalV8ArtifactError(
            "completeness audit belongs to a different canonical dataset"
        )
    if (completeness.readiness_id, completeness.readiness_hash) != (
        readiness.readiness_id,
        readiness.readiness_hash,
    ):
        raise CanonicalV8ArtifactError(
            "completeness audit belongs to a different readiness decision"
        )
    blockers: list[str] = []
    if not readiness.publish_ready:
        blockers.append("readiness.publish_ready=false")
    if not completeness.identity_publish_ready:
        blockers.append("completeness.identity_publish_ready=false")
    if not completeness.static_ingest_ready:
        blockers.append("completeness.static_ingest_ready=false")
    if dataset.report.open_vacancy_count:
        blockers.append(
            f"dataset.open_vacancy_count={dataset.report.open_vacancy_count}"
        )
    if readiness.open_vacancy_count not in (None, 0):
        blockers.append(f"readiness.open_vacancy_count={readiness.open_vacancy_count}")
    if completeness.open_vacancy_count not in (None, 0):
        blockers.append(
            f"completeness.open_vacancy_count={completeness.open_vacancy_count}"
        )
    blocking_gaps = [
        item
        for item in completeness.gaps
        if item.severity is CompletenessSeverity.BLOCKING
    ]
    if blocking_gaps:
        blockers.append(f"blocking_gaps={len(blocking_gaps)}")
    if blockers:
        raise CanonicalV8GateError(
            "canonical V8 publish gate rejected the artifacts: " + ", ".join(blockers)
        )

    city_names: dict[str, str] = {}
    for record in dataset.records:
        if (record.provenance.manifest_id, record.provenance.manifest_hash) != (
            dataset.manifest_id,
            dataset.manifest_hash,
        ):
            raise CanonicalV8ArtifactError(
                f"record {record.place_id} has mismatched manifest provenance"
            )
        if record.provenance.canonical_place_id != record.place_id:
            raise CanonicalV8ArtifactError(
                f"record {record.place_id} has mismatched canonical provenance"
            )
        existing_city = city_names.setdefault(record.city_id, record.city)
        if existing_city != record.city:
            raise CanonicalV8ArtifactError(
                f"city_id {record.city_id} maps to multiple city names"
            )


def _build_import_plan(
    artifacts: _LoadedArtifacts,
    *,
    embedding_dimension: int,
) -> CanonicalV8ImportPlan:
    dataset = artifacts.dataset
    completeness = artifacts.completeness
    records = list(dataset.records)
    record_set_hash = stable_sha256(
        [
            {"place_id": item.place_id, "record_hash": item.record_hash}
            for item in records
        ]
    )
    counts = Counter(item.primary_type.value for item in records)
    entity_counts = [
        CanonicalV8EntityCount(entity_type=entity_type, count=count)
        for entity_type, count in sorted(counts.items())
    ]
    cities = _city_rows(records)
    document_count = len(records)
    text_unit_count = len(records)
    fact_count = sum(len(_record_fact_specs(record)) for record in records)
    claim_count = sum(len(_record_concepts(record)) for record in records)
    release_values = {
        "schema_version": "1.1.0",
        "kb_version": KB_VERSION,
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "canonical_manifest_id": dataset.manifest_id,
        "canonical_manifest_hash": dataset.manifest_hash,
        "readiness_id": artifacts.readiness.readiness_id,
        "readiness_hash": artifacts.readiness.readiness_hash,
        "completeness_audit_id": completeness.audit_id,
        "completeness_audit_hash": completeness.audit_hash,
        "source_as_of": _iso_z(completeness.as_of),
        "place_count": len(records),
        "city_count": len(cities),
        "document_count": document_count,
        "text_unit_count": text_unit_count,
        "fact_count": fact_count,
        "claim_count": claim_count,
        "entity_counts": entity_counts,
        "record_set_hash": record_set_hash,
        "artifact_digests": list(artifacts.digests),
        "embedding_dimension": embedding_dimension,
        "semantic_index_status": "pending",
    }
    release_hash = stable_sha256(_release_payload(**release_values))
    release = CanonicalV8ReleaseManifest(
        release_id=f"v8-canonical-release-{release_hash[:20]}",
        release_hash=release_hash,
        **release_values,
    )

    type_rows = _place_type_rows(records)
    category_rows = _category_rows(records)
    concept_rows = _concept_rows(records)
    category_id_by_value = {
        item["normalized_value"]: item["id"] for item in category_rows
    }
    concept_id_by_key = {
        (item["concept_type"], item["normalized_value"]): item["id"]
        for item in concept_rows
    }
    place_rows: list[dict[str, Any]] = []
    version_rows: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    document_rows: list[dict[str, Any]] = []
    text_unit_rows: list[dict[str, Any]] = []
    fact_rows: list[dict[str, Any]] = []
    claim_rows: list[dict[str, Any]] = []
    for record in records:
        category = _record_category(record)
        category_ids = (
            [category_id_by_value[_normalized_dimension_value(category)]]
            if category
            else []
        )
        concepts = _record_concepts(record)
        concept_ids = [
            concept_id_by_key[(kind, _normalized_dimension_value(value))]
            for kind, value in concepts
        ]
        type_ids = [f"v8:type:{item.value}" for item in record.place_types]
        version_id = f"v8:place-version:{record.place_id}:{record.record_hash}"
        provenance_id = f"v8:source-provenance:{record.place_id}:{record.record_hash}"
        document_id = f"v8:document:{record.place_id}:{record.record_hash}"
        text_unit_id = f"v8:text-unit:{record.place_id}:{record.record_hash}"
        place_rows.append(
            {
                "id": record.place_id,
                "city_id": record.city_id,
                "version_id": version_id,
                "provenance_id": provenance_id,
                "type_ids": type_ids,
                "category_ids": category_ids,
                "concept_ids": concept_ids,
                "document_id": document_id,
                "text_unit_id": text_unit_id,
                "properties": _place_properties(record, release),
            }
        )
        version_rows.append(
            {
                "id": version_id,
                "place_id": record.place_id,
                "city_id": record.city_id,
                "type_ids": type_ids,
                "category_ids": category_ids,
                "concept_ids": concept_ids,
                "document_id": document_id,
                "text_unit_id": text_unit_id,
                "properties": _version_properties(record, release),
            }
        )
        provenance_rows.append(
            {
                "id": provenance_id,
                "version_id": version_id,
                "properties": _provenance_properties(record, release),
            }
        )
        document_rows.append(
            _document_row(
                record,
                release,
                version_id=version_id,
                document_id=document_id,
            )
        )
        text_unit_rows.append(
            _text_unit_row(
                record,
                release,
                version_id=version_id,
                document_id=document_id,
                text_unit_id=text_unit_id,
            )
        )
        fact_rows.extend(
            _fact_rows(
                record,
                release,
                version_id=version_id,
                text_unit_id=text_unit_id,
            )
        )
        claim_rows.extend(
            _claim_rows(
                record,
                release,
                version_id=version_id,
                text_unit_id=text_unit_id,
                concept_ids=concept_ids,
                concepts=concepts,
            )
        )
    if (
        len(document_rows) != document_count
        or len(text_unit_rows) != text_unit_count
        or len(fact_rows) != fact_count
        or len(claim_rows) != claim_count
    ):
        raise CanonicalV8ArtifactError(
            "deterministic compatibility row counts changed while building release"
        )
    return CanonicalV8ImportPlan(
        release=release,
        cities=tuple(cities),
        places=tuple(place_rows),
        versions=tuple(version_rows),
        provenances=tuple(provenance_rows),
        place_types=tuple(type_rows),
        categories=tuple(category_rows),
        concepts=tuple(concept_rows),
        documents=tuple(document_rows),
        text_units=tuple(text_unit_rows),
        facts=tuple(fact_rows),
        claims=tuple(claim_rows),
    )


def _city_rows(records: list[CanonicalActivePlaceRecord]) -> list[dict[str, Any]]:
    values = {record.city_id: record.city for record in records}
    return [
        {
            "id": city_id,
            "properties": {
                "id": city_id,
                "canonical_city_id": city_id,
                "name": name,
                "kb_version": KB_VERSION,
            },
        }
        for city_id, name in sorted(values.items())
    ]


def _place_type_rows(
    records: list[CanonicalActivePlaceRecord],
) -> list[dict[str, Any]]:
    values = sorted({item.value for record in records for item in record.place_types})
    return [
        {
            "id": f"v8:type:{value}",
            "properties": {
                "id": f"v8:type:{value}",
                "name": value,
                "value": value,
                "kb_version": KB_VERSION,
            },
        }
        for value in values
    ]


def _category_rows(
    records: list[CanonicalActivePlaceRecord],
) -> list[dict[str, Any]]:
    values = {_record_category(record) for record in records}
    values.discard(None)
    return _dimension_rows("category", values)


def _concept_rows(
    records: list[CanonicalActivePlaceRecord],
) -> list[dict[str, Any]]:
    concepts = {
        (kind, value) for record in records for kind, value in _record_concepts(record)
    }
    rows = []
    for concept_type, value in sorted(
        concepts,
        key=lambda item: (item[0], _normalized_dimension_value(item[1])),
    ):
        normalized = _normalized_dimension_value(value)
        identifier = _dimension_id(f"concept:{concept_type}", normalized)
        domain = _concept_domain(concept_type)
        semantic_text = (
            f"Khái niệm: {value} | Loại: {concept_type} | Miền: {domain}"
        )
        rows.append(
            {
                "id": identifier,
                "normalized_value": normalized,
                "concept_type": concept_type,
                "properties": {
                    "id": identifier,
                    "name": value,
                    "canonical_name": value,
                    "normalized_name": normalized,
                    "concept_type": concept_type,
                    "domain": domain,
                    "semantic_text": semantic_text,
                    "semantic_content_hash": stable_sha256(
                        {"target": "concept", "text": semantic_text}
                    ),
                    "kb_version": KB_VERSION,
                },
            }
        )
    return rows


def _dimension_rows(kind: str, values: set[str]) -> list[dict[str, Any]]:
    by_normalized: dict[str, str] = {}
    for value in sorted(values, key=lambda item: (item.casefold(), item)):
        by_normalized.setdefault(_normalized_dimension_value(value), value)
    return [
        {
            "id": _dimension_id(kind, normalized),
            "normalized_value": normalized,
            "properties": {
                "id": _dimension_id(kind, normalized),
                "name": value,
                "value": value,
                "normalized_name": normalized,
                "kb_version": KB_VERSION,
            },
        }
        for normalized, value in sorted(by_normalized.items())
    ]


def _dimension_id(kind: str, normalized: str) -> str:
    digest = stable_sha256({"kind": kind, "value": normalized})[:24]
    return f"v8:{kind}:{digest}"


def _normalized_dimension_value(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


_CONCEPT_FIELDS = (
    "activities",
    "ambience",
    "amenities",
    "cuisine",
    "dietary_options",
    "drink_specialties",
    "features",
    "highlights",
    "music_genres",
    "music_style",
    "payment_methods",
    "room_types",
    "serves",
    "signature_dishes",
    "suitable_for",
    "tags",
    "vibe",
    "weather_suitable",
)


def _record_concepts(record: CanonicalActivePlaceRecord) -> list[tuple[str, str]]:
    values: set[tuple[str, str]] = {
        ("tags", value) for value in record.tags if value.strip()
    }
    for field_name in _CONCEPT_FIELDS:
        raw = record.data.get(field_name)
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                values.add((field_name, candidate.strip()))
    return sorted(
        values,
        key=lambda item: (item[0], _normalized_dimension_value(item[1])),
    )


def _concept_domain(concept_type: str) -> str:
    if concept_type in {
        "cuisine",
        "drink_specialties",
        "room_types",
        "serves",
        "signature_dishes",
    }:
        return "offering"
    if concept_type in {
        "amenities",
        "dietary_options",
        "features",
        "payment_methods",
        "suitable_for",
        "weather_suitable",
    }:
        return "compatibility"
    return "experience"


def _record_category(record: CanonicalActivePlaceRecord) -> str | None:
    for field_name in ("google_maps_category", "category"):
        value = record.data.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_FACT_FIELDS = (
    "accessibility",
    "age_restriction",
    "ambience",
    "amenities",
    "best_nights",
    "best_time",
    "booking_advice",
    "branch_info",
    "cable_car",
    "check_in_time",
    "check_out_time",
    "construction_period",
    "cuisine",
    "dietary_options",
    "distance_to_beach",
    "distance_to_center",
    "dress_code",
    "duration_recommendation",
    "features",
    "happy_hour",
    "highlights",
    "hotel_style",
    "is_indoor",
    "music_genres",
    "music_style",
    "payment_methods",
    "rating",
    "reservation_recommended",
    "reservation_required",
    "review_count",
    "room_types",
    "serves",
    "signature_dishes",
    "star_rating",
    "suitable_for",
    "tags",
    "unesco_status",
    "venue_type",
    "vibe",
    "weather_suitable",
)

_FACT_UNITS = {
    "altitude": "m",
    "distance_to_beach": "km",
    "distance_to_center": "km",
    "latitude": "degrees",
    "longitude": "degrees",
    "price": "VND",
    "price_max": "VND",
    "price_min": "VND",
    "rating": "stars",
    "star_rating": "stars",
}

_CONCEPT_PREDICATES = {
    "activities": "PROVIDES_ACTIVITY",
    "ambience": "HAS_AMBIENCE",
    "amenities": "HAS_AMENITY",
    "cuisine": "SERVES_CUISINE",
    "dietary_options": "SUPPORTS_DIET",
    "drink_specialties": "HAS_DRINK_OFFERING",
    "features": "HAS_FEATURE",
    "highlights": "HAS_HIGHLIGHT",
    "music_genres": "HAS_MUSIC_GENRE",
    "music_style": "HAS_MUSIC_STYLE",
    "payment_methods": "ACCEPTS_PAYMENT_METHOD",
    "room_types": "HAS_ROOM_TYPE",
    "serves": "SERVES",
    "signature_dishes": "SERVES_DISH",
    "suitable_for": "SUITABLE_FOR",
    "tags": "HAS_TAG",
    "vibe": "HAS_VIBE",
    "weather_suitable": "SUITABLE_DURING",
}


def _record_fact_specs(record: CanonicalActivePlaceRecord) -> list[dict[str, Any]]:
    """Build deterministic facts without an LLM or release-specific state."""

    flattened = _safe_flat_properties(record.data)
    flattened.update(_flatten_known_nested_fields(record.data))
    values: dict[str, Any] = {
        "name": record.name,
        "aliases": record.aliases,
        "entity_type": record.primary_type.value,
        "place_types": [item.value for item in record.place_types],
        "city": record.city,
        "address": record.address,
        "location": _json_dumps(
            {"lat": record.coordinates.lat, "lng": record.coordinates.lng}
        ),
        "latitude": record.coordinates.lat,
        "longitude": record.coordinates.lng,
        "category": _record_category(record),
        "description": _string_or_none(record.data.get("description")),
        "phone": record.phone,
        "website_url": (
            str(record.website_url) if record.website_url is not None else None
        ),
    }
    for field_name in _FACT_FIELDS:
        values[field_name] = flattened.get(field_name)

    opening = _render_opening_hours(flattened)
    values["opening_hours"] = opening
    if _claims_open_24h(flattened):
        values["opening_24h_claim"] = True

    # Hotel price is a dated observation published by observation_publisher.py.
    # A canonical snapshot must not masquerade as the current bookable rate.
    if record.primary_type.value != "hotel":
        price_min = _first_price_bound(flattened, "min")
        price_max = _first_price_bound(flattened, "max")
        values["price_min"] = price_min
        values["price_max"] = price_max
        values["price"] = (
            price_min if price_min is not None else flattened.get("price_range")
        )
    altitude = flattened.get("altitude_m")
    values["altitude"] = altitude
    values["indoor"] = flattened.get("is_indoor")
    values["weather"] = flattened.get("weather_suitable")
    values["duration"] = flattened.get("duration_recommendation")

    rows: list[dict[str, Any]] = []
    for predicate, raw_value in sorted(values.items()):
        value = _neo4j_property_value(raw_value)
        if value in (None, "", []):
            continue
        value_type = (
            "boolean"
            if isinstance(value, bool)
            else (
                "number"
                if isinstance(value, (int, float))
                else "string_list"
                if isinstance(value, list)
                else "string"
            )
        )
        rows.append(
            {
                "predicate": predicate,
                "value": value,
                "value_type": value_type,
                "unit": _FACT_UNITS.get(predicate),
                "confidence": 1.0,
            }
        )
    return rows


def _render_opening_hours(properties: dict[str, Any]) -> str | None:
    opening = _string_or_none(properties.get("opening_hours_open"))
    closing = _string_or_none(properties.get("opening_hours_close"))
    if opening and closing:
        return f"{opening}-{closing}"
    return opening or closing or _string_or_none(properties.get("opening_hours_note"))


def _claims_open_24h(properties: dict[str, Any]) -> bool:
    return properties.get("opening_hours_open") in {"00:00", "0:00"} and (
        properties.get("opening_hours_close") in {"23:59", "24:00", "00:00"}
    )


def _first_price_bound(
    properties: dict[str, Any],
    bound: Literal["min", "max"],
) -> int | float | None:
    for prefix in (
        "price_per_night",
        "price_per_person",
        "drink_price",
        "entry_fee",
        "ticket_price",
    ):
        value = properties.get(f"{prefix}_{bound}")
        if value is None and prefix == "ticket_price" and bound == "min":
            value = properties.get("ticket_price_adult")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _record_source(record: CanonicalActivePlaceRecord) -> tuple[str | None, str]:
    verified_sources = record.data.get("verified_sources")
    if isinstance(verified_sources, list):
        for source in verified_sources:
            if not isinstance(source, dict):
                continue
            url = _string_or_none(source.get("url"))
            name = _string_or_none(source.get("source_name") or source.get("name"))
            if url:
                return url, name or record.provenance.source_kind.value
    for identity in record.external_identities:
        url = _string_or_none(identity.get("external_url"))
        if url:
            return url, str(identity.get("source_id") or "external_identity")
    if record.website_url is not None:
        return str(record.website_url), "official_website"
    return None, record.provenance.source_kind.value


def _place_evidence_text(record: CanonicalActivePlaceRecord) -> str:
    category = _record_category(record)
    lines = [
        f"Tên: {record.name}",
        f"Loại: {record.primary_type.value}",
        f"Thành phố: {record.city}",
        f"Địa chỉ: {record.address}" if record.address else None,
        f"Danh mục: {category}" if category else None,
        (f"Tọa độ: {record.coordinates.lat}, {record.coordinates.lng}"),
        (
            f"Mô tả: {record.data.get('description')}"
            if _string_or_none(record.data.get("description"))
            else None
        ),
    ]
    for field_name in _CONCEPT_FIELDS:
        raw_value = record.data.get(field_name)
        if raw_value in (None, "", []):
            continue
        rendered = (
            ", ".join(str(item) for item in raw_value)
            if isinstance(raw_value, list)
            else str(raw_value)
        )
        lines.append(f"{field_name}: {rendered}")
    for fact in _record_fact_specs(record):
        value = fact["value"]
        rendered = (
            ", ".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        unit = f" {fact['unit']}" if fact.get("unit") else ""
        lines.append(f"fact:{fact['predicate']}={rendered}{unit}")
    return "\n".join(line for line in lines if line)


def _document_row(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
    *,
    version_id: str,
    document_id: str,
) -> dict[str, Any]:
    url, source_name = _record_source(record)
    content = {
        "canonical_place_id": record.place_id,
        "record_hash": record.record_hash,
        "title": f"Canonical record: {record.name}",
        "url": url,
        "source_name": source_name,
    }
    return {
        "id": document_id,
        "version_id": version_id,
        "place_id": record.place_id,
        "properties": {
            "id": document_id,
            "kb_version": KB_VERSION,
            "canonical_place_id": record.place_id,
            "record_hash": record.record_hash,
            "content_hash": stable_sha256(content),
            "title": content["title"],
            "url": url,
            "source_name": source_name,
            "evidence_origin": "verified_place_record",
            "first_release_id": release.release_id,
            "payload_json": _json_dumps(content),
        },
    }


def _text_unit_row(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
    *,
    version_id: str,
    document_id: str,
    text_unit_id: str,
) -> dict[str, Any]:
    text = _place_evidence_text(record)
    semantic_content_hash = stable_sha256(
        {"target": "text_unit", "text": text}
    )
    content = {
        "canonical_place_id": record.place_id,
        "record_hash": record.record_hash,
        "document_id": document_id,
        "sequence": 0,
        "text": text,
    }
    return {
        "id": text_unit_id,
        "version_id": version_id,
        "document_id": document_id,
        "place_id": record.place_id,
        "properties": {
            "id": text_unit_id,
            "kb_version": KB_VERSION,
            "canonical_place_id": record.place_id,
            "record_hash": record.record_hash,
            "content_hash": stable_sha256(content),
            "semantic_content_hash": semantic_content_hash,
            "semantic_text": text,
            "title": record.name,
            "text": text,
            "sequence": 0,
            "evidence_origin": "verified_place_record",
            "confidence": 1.0,
            "first_release_id": release.release_id,
            "payload_json": _json_dumps(content),
        },
    }


def _fact_rows(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
    *,
    version_id: str,
    text_unit_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in _record_fact_specs(record):
        identity = {
            "canonical_place_id": record.place_id,
            "record_hash": record.record_hash,
            "predicate": spec["predicate"],
        }
        fact_id = f"v8:fact:{stable_sha256(identity)[:32]}"
        content = {**identity, **spec}
        rows.append(
            {
                "id": fact_id,
                "version_id": version_id,
                "place_id": record.place_id,
                "text_unit_id": text_unit_id,
                "properties": {
                    "id": fact_id,
                    "kb_version": KB_VERSION,
                    **content,
                    "content_hash": stable_sha256(content),
                    "first_release_id": release.release_id,
                    "payload_json": _json_dumps(content),
                },
            }
        )
    return rows


def _claim_rows(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
    *,
    version_id: str,
    text_unit_id: str,
    concept_ids: list[str],
    concepts: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    evidence_text = _place_evidence_text(record)
    rows: list[dict[str, Any]] = []
    for (concept_type, value), concept_id in zip(
        concepts,
        concept_ids,
        strict=True,
    ):
        predicate = _CONCEPT_PREDICATES[concept_type]
        rendered_evidence = f"{concept_type}: {value}"
        evidence_start = evidence_text.find(rendered_evidence)
        identity = {
            "canonical_place_id": record.place_id,
            "record_hash": record.record_hash,
            "concept_id": concept_id,
            "predicate": predicate,
        }
        claim_id = f"v8:claim:{stable_sha256(identity)[:32]}"
        content = {
            **identity,
            "polarity": "positive",
            "confidence": 1.0,
            "evidence_text": rendered_evidence,
            "evidence_start": evidence_start if evidence_start >= 0 else None,
            "evidence_end": (
                evidence_start + len(rendered_evidence) if evidence_start >= 0 else None
            ),
            "extraction_method": "canonical_structured_field",
            "subject_scope": "place",
        }
        rows.append(
            {
                "id": claim_id,
                "version_id": version_id,
                "place_id": record.place_id,
                "concept_id": concept_id,
                "text_unit_id": text_unit_id,
                "properties": {
                    "id": claim_id,
                    "kb_version": KB_VERSION,
                    **content,
                    "content_hash": stable_sha256(content),
                    "first_release_id": release.release_id,
                    "payload_json": _json_dumps(content),
                },
            }
        )
    return rows


def _place_properties(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
) -> dict[str, Any]:
    category = _record_category(record)
    description = _string_or_none(record.data.get("description"))
    search_parts = [
        record.name,
        *record.aliases,
        record.city,
        record.address or "",
        category or "",
        description or "",
        *record.tags,
    ]
    semantic_text = " | ".join(
        part.strip() for part in search_parts if part.strip()
    )
    properties = _safe_flat_properties(record.data)
    properties.update(_flatten_known_nested_fields(record.data))
    if record.primary_type.value == "hotel":
        # Bookable hotel rates are observation-owned and date/context scoped.
        # They remain traceable in the immutable version payload, but are not
        # exposed as an undated current Place property.
        for key in tuple(properties):
            if key == "price_range" or key.startswith("price_per_night_"):
                properties.pop(key)
    properties.update(
        {
            "id": record.place_id,
            "canonical_place_id": record.place_id,
            "kb_version": KB_VERSION,
            "name": record.name,
            "aliases": record.aliases,
            "tags": record.tags,
            "entity_type": record.primary_type.value,
            "entity_label": record.primary_type.value,
            "primary_type": record.primary_type.value,
            "secondary_types": [item.value for item in record.secondary_types],
            "place_types": [item.value for item in record.place_types],
            "city_id": record.city_id,
            "city": record.city,
            "address": record.address,
            "lat": record.coordinates.lat,
            "lng": record.coordinates.lng,
            "phone": record.phone,
            "website_url": (
                str(record.website_url) if record.website_url is not None else None
            ),
            "category": category,
            "category_name": category,
            "description": description,
            "search_text": semantic_text,
            "entity_profile": semantic_text,
            "semantic_text": semantic_text,
            "semantic_content_hash": stable_sha256(
                {"target": "place", "text": semantic_text}
            ),
            "record_hash": record.record_hash,
            "current_record_hash": record.record_hash,
            "current_release_id": release.release_id,
            "current_dataset_id": release.dataset_id,
            "active": True,
            "external_identities_json": _json_dumps(record.external_identities),
            "data_json": _json_dumps(record.data),
        }
    )
    return properties


def _flatten_known_nested_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Expose fields consumed by inherited V8 queries without losing JSON."""

    result: dict[str, Any] = {}
    opening = data.get("opening_hours")
    if isinstance(opening, dict):
        result.update(
            {
                "opening_hours_open": _string_or_none(opening.get("open")),
                "opening_hours_close": _string_or_none(opening.get("close")),
                "opening_hours_note": _string_or_none(opening.get("note")),
            }
        )
        closed_days = opening.get("closed_days")
        if isinstance(closed_days, list) and all(
            isinstance(item, str) for item in closed_days
        ):
            result["opening_hours_closed_days"] = closed_days

    for field_name in (
        "price_per_night",
        "price_per_person",
        "drink_price",
        "entry_fee",
        "price_range",
    ):
        value = data.get(field_name)
        if not isinstance(value, dict):
            continue
        for nested_name in ("min", "max", "currency", "note"):
            nested_value = _neo4j_property_value(value.get(nested_name))
            if nested_value is not None:
                result[f"{field_name}_{nested_name}"] = nested_value

    ticket = data.get("ticket_price")
    if isinstance(ticket, dict):
        for nested_name in (
            "adult",
            "child",
            "student",
            "elderly",
            "currency",
            "note",
        ):
            nested_value = _neo4j_property_value(ticket.get(nested_name))
            if nested_value is not None:
                result[f"ticket_price_{nested_name}"] = nested_value
    return result


def _version_properties(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
) -> dict[str, Any]:
    version_id = f"v8:place-version:{record.place_id}:{record.record_hash}"
    return {
        "id": version_id,
        "kb_version": KB_VERSION,
        "canonical_place_id": record.place_id,
        "record_hash": record.record_hash,
        "identity_hash": record.provenance.identity_hash,
        "dataset_id": release.dataset_id,
        "dataset_hash": release.dataset_hash,
        "first_release_id": release.release_id,
        "source_as_of": release.source_as_of,
        "entity_type": record.primary_type.value,
        "city_id": record.city_id,
        "name": record.name,
        "lat": record.coordinates.lat,
        "lng": record.coordinates.lng,
        "payload_json": _json_dumps(record.model_dump(mode="json")),
        "data_json": _json_dumps(record.data),
    }


def _provenance_properties(
    record: CanonicalActivePlaceRecord,
    release: CanonicalV8ReleaseManifest,
) -> dict[str, Any]:
    provenance = record.provenance
    provenance_id = f"v8:source-provenance:{record.place_id}:{record.record_hash}"
    payload = provenance.model_dump(mode="json")
    return {
        "id": provenance_id,
        "kb_version": KB_VERSION,
        "canonical_place_id": record.place_id,
        "record_hash": record.record_hash,
        "provenance_hash": stable_sha256(payload),
        "source_kind": provenance.source_kind.value,
        "manifest_id": provenance.manifest_id,
        "manifest_hash": provenance.manifest_hash,
        "identity_hash": provenance.identity_hash,
        "active_legacy_place_id": provenance.active_legacy_place_id,
        "legacy_place_ids": provenance.legacy_place_ids,
        "retired_alias_ids": provenance.retired_alias_ids,
        "approval_id": provenance.approval_id,
        "approval_hash": provenance.approval_hash,
        "candidate_detail_id": provenance.candidate_detail_id,
        "source_record_ids": provenance.source_record_ids,
        "observation_ids": provenance.observation_ids,
        "replacement_of": provenance.replacement_of,
        "vacancy_id": provenance.vacancy_id,
        "first_release_id": release.release_id,
        "payload_json": _json_dumps(payload),
    }


def _safe_flat_properties(data: dict[str, Any]) -> dict[str, Any]:
    reserved = {
        "active",
        "canonical_place_id",
        "current_dataset_id",
        "current_record_hash",
        "current_release_id",
        "data_json",
        "embedding",
        "external_identities_json",
        "id",
        "kb_version",
        "lat",
        "lng",
        "location",
        "record_hash",
    }
    result: dict[str, Any] = {}
    for key, value in sorted(data.items()):
        if key in reserved:
            continue
        safe_value = _neo4j_property_value(value)
        if safe_value is not None:
            result[key] = safe_value
    return result


def _neo4j_property_value(value: Any) -> Any | None:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(item, str) for item in value):
            return value
        if all(isinstance(item, bool) for item in value):
            return value
        if all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in value
        ):
            return value
    return None


def _string_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _iso_z(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


class CanonicalV8ReleaseManifestWriter:
    """Persist a content-addressed release manifest without replacement."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, manifest: CanonicalV8ReleaseManifest) -> Path:
        return (
            self.output_root
            / f"release={quote(manifest.release_id, safe='-_.')}"
            / "canonical-v8-release-manifest.json"
        )

    def write(self, manifest: CanonicalV8ReleaseManifest) -> Path:
        validated = CanonicalV8ReleaseManifest.model_validate_json(
            manifest.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = CanonicalV8ReleaseManifest.model_validate_json(
                        destination.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalV8ReleaseAlreadyExistsError(
                        f"immutable V8 release manifest is invalid: {destination}"
                    ) from error
                if existing.release_hash == validated.release_hash:
                    return destination
                raise CanonicalV8ReleaseAlreadyExistsError(
                    f"immutable V8 release manifest already exists: {destination}"
                )
            serialized = (
                json.dumps(
                    validated.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination


_SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT canonical_place_id IF NOT EXISTS "
    "FOR (node:CanonicalPlace) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_city_id IF NOT EXISTS "
    "FOR (node:CanonicalCity) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT place_version_id IF NOT EXISTS "
    "FOR (node:PlaceVersion) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_source_provenance_id IF NOT EXISTS "
    "FOR (node:CanonicalSourceProvenance) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT dataset_release_id IF NOT EXISTS "
    "FOR (node:DatasetRelease) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_travel_catalog_id IF NOT EXISTS "
    "FOR (node:CanonicalTravelCatalog) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_place_type_id IF NOT EXISTS "
    "FOR (node:CanonicalPlaceType) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_category_id IF NOT EXISTS "
    "FOR (node:CanonicalCategory) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_concept_id IF NOT EXISTS "
    "FOR (node:CanonicalConcept) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_document_id IF NOT EXISTS "
    "FOR (node:CanonicalDocument) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_text_unit_id IF NOT EXISTS "
    "FOR (node:CanonicalTextUnit) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_fact_id IF NOT EXISTS "
    "FOR (node:CanonicalFact) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT canonical_claim_id IF NOT EXISTS "
    "FOR (node:CanonicalClaim) REQUIRE node.id IS UNIQUE",
    "CREATE RANGE INDEX place_city IF NOT EXISTS FOR (node:Place) ON (node.city)",
    "CREATE RANGE INDEX place_entity_type IF NOT EXISTS "
    "FOR (node:Place) ON (node.entity_type)",
    "CREATE RANGE INDEX place_version_hash IF NOT EXISTS "
    "FOR (node:PlaceVersion) ON (node.record_hash)",
    "CREATE RANGE INDEX canonical_fact_predicate IF NOT EXISTS "
    "FOR (node:CanonicalFact) ON (node.predicate)",
    "CREATE RANGE INDEX entity_id IF NOT EXISTS FOR (node:Entity) ON (node.id)",
    "CREATE FULLTEXT INDEX place_fulltext IF NOT EXISTS "
    "FOR (node:Place) ON EACH "
    "[node.name, node.aliases, node.entity_profile]",
    "CREATE FULLTEXT INDEX concept_fulltext IF NOT EXISTS "
    "FOR (node:Concept) ON EACH [node.name, node.canonical_name]",
)


_STAGE_RELEASE = """
// v8:stage-release
MERGE (release:DatasetRelease {id: $release_id})
ON CREATE SET release = $properties
SET release:Entity
RETURN release.id AS id,
       release.release_hash AS release_hash,
       release.manifest_json AS manifest_json,
       release.kb_version AS kb_version,
       release.status AS status
"""

_STAGE_CITIES = """
// v8:stage-cities
UNWIND $rows AS row
MERGE (city:CanonicalCity {id: row.id})
ON CREATE SET city = row.properties
SET city:Entity
"""

_STAGE_TYPES = """
// v8:stage-types
UNWIND $rows AS row
MERGE (placeType:CanonicalPlaceType {id: row.id})
ON CREATE SET placeType = row.properties
SET placeType:Entity
"""

_STAGE_CATEGORIES = """
// v8:stage-categories
UNWIND $rows AS row
MERGE (category:CanonicalCategory {id: row.id})
ON CREATE SET category = row.properties
SET category:Entity
"""

_STAGE_CONCEPTS = """
// v8:stage-concepts
UNWIND $rows AS row
MERGE (concept:CanonicalConcept {id: row.id})
ON CREATE SET concept = row.properties
SET concept:Entity
"""

_STAGE_VERSIONS = """
// v8:stage-versions
UNWIND $rows AS row
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MERGE (place:CanonicalPlace {id: row.place_id})
ON CREATE SET place.kb_version = $kb_version
SET place:Entity
MERGE (version:PlaceVersion {id: row.id})
ON CREATE SET version = row.properties
SET version:Entity
MERGE (place)-[:HAS_VERSION]->(version)
MERGE (release)-[:CONTAINS_VERSION]->(version)
FOREACH (
  ignored IN CASE
    WHEN version.location IS NULL THEN [1]
    ELSE []
  END |
  SET version.location = point({
    latitude: row.properties.lat,
    longitude: row.properties.lng
  })
)
"""

_STAGE_PROVENANCE = """
// v8:stage-provenance
UNWIND $rows AS row
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MERGE (provenance:CanonicalSourceProvenance {id: row.id})
ON CREATE SET provenance = row.properties
SET provenance:SourceProvenance:Entity
MERGE (version)-[:SUPPORTED_BY]->(provenance)
"""

_STAGE_DOCUMENTS = """
// v8:stage-documents
UNWIND $rows AS row
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MERGE (document:CanonicalDocument {id: row.id})
ON CREATE SET document = row.properties
SET document:Entity
MERGE (version)-[:HAS_EVIDENCE_DOCUMENT]->(document)
MERGE (release)-[:CONTAINS_DOCUMENT]->(document)
"""

_STAGE_TEXT_UNITS = """
// v8:stage-text-units
UNWIND $rows AS row
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MATCH (document:CanonicalDocument {id: row.document_id, kb_version: $kb_version})
MERGE (unit:CanonicalTextUnit {id: row.id})
ON CREATE SET unit = row.properties
SET unit:Entity
MERGE (version)-[:HAS_EVIDENCE_TEXT_UNIT]->(unit)
MERGE (unit)-[:PART_OF_VERSION_DOCUMENT]->(document)
MERGE (unit)-[:PART_OF]->(document)
MERGE (release)-[:CONTAINS_TEXT_UNIT]->(unit)
"""

_STAGE_FACTS = """
// v8:stage-facts
UNWIND $rows AS row
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MATCH (unit:CanonicalTextUnit {id: row.text_unit_id, kb_version: $kb_version})
MERGE (fact:CanonicalFact {id: row.id})
ON CREATE SET fact = row.properties
SET fact:Entity
MERGE (version)-[:HAS_RECORDED_FACT]->(fact)
MERGE (fact)-[:EVIDENCED_BY]->(unit)
MERGE (fact)-[:SUPPORTED_BY]->(unit)
MERGE (release)-[:CONTAINS_FACT]->(fact)
"""

_STAGE_CLAIMS = """
// v8:stage-claims
UNWIND $rows AS row
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MATCH (concept:CanonicalConcept {id: row.concept_id, kb_version: $kb_version})
MATCH (unit:CanonicalTextUnit {id: row.text_unit_id, kb_version: $kb_version})
MERGE (claim:CanonicalClaim {id: row.id})
ON CREATE SET claim = row.properties
SET claim:Entity
MERGE (version)-[:HAS_RECORDED_CLAIM]->(claim)
MERGE (claim)-[:ABOUT_VERSION]->(version)
MERGE (claim)-[:ASSERTS_CONCEPT]->(concept)
MERGE (claim)-[:EVIDENCED_BY]->(unit)
MERGE (claim)-[:OBJECT]->(concept)
MERGE (claim)-[:SUPPORTED_BY]->(unit)
MERGE (release)-[:CONTAINS_CLAIM]->(claim)
"""

_STAGE_VERSION_CITY = """
// v8:stage-version-city
UNWIND $rows AS row
MATCH (version:PlaceVersion {id: row.id, kb_version: $kb_version})
MATCH (city:CanonicalCity {id: row.city_id, kb_version: $kb_version})
MERGE (version)-[:RECORDED_IN_CITY]->(city)
"""

_STAGE_VERSION_TYPES = """
// v8:stage-version-types
UNWIND $rows AS row
UNWIND row.type_ids AS type_id
MATCH (version:PlaceVersion {id: row.id, kb_version: $kb_version})
MATCH (placeType:CanonicalPlaceType {id: type_id, kb_version: $kb_version})
MERGE (version)-[:RECORDED_TYPE]->(placeType)
"""

_STAGE_VERSION_CATEGORIES = """
// v8:stage-version-categories
UNWIND $rows AS row
UNWIND row.category_ids AS category_id
MATCH (version:PlaceVersion {id: row.id, kb_version: $kb_version})
MATCH (category:CanonicalCategory {id: category_id, kb_version: $kb_version})
MERGE (version)-[:RECORDED_CATEGORY]->(category)
"""

_STAGE_VERSION_CONCEPTS = """
// v8:stage-version-concepts
UNWIND $rows AS row
UNWIND row.concept_ids AS concept_id
MATCH (version:PlaceVersion {id: row.id, kb_version: $kb_version})
MATCH (concept:CanonicalConcept {id: concept_id, kb_version: $kb_version})
MERGE (version)-[:RECORDED_CONCEPT]->(concept)
"""

_CHECK_IMMUTABLE_VERSIONS = """
// v8:check-immutable-versions
UNWIND $rows AS row
MATCH (version:PlaceVersion {id: row.id, kb_version: $kb_version})
WITH row, version
WHERE version.canonical_place_id <> row.properties.canonical_place_id
   OR version.record_hash <> row.properties.record_hash
   OR version.identity_hash <> row.properties.identity_hash
   OR version.payload_json <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_IMMUTABLE_PROVENANCE = """
// v8:check-immutable-provenance
UNWIND $rows AS row
MATCH (provenance:CanonicalSourceProvenance {id: row.id, kb_version: $kb_version})
WITH row, provenance
WHERE provenance.canonical_place_id <> row.properties.canonical_place_id
   OR provenance.record_hash <> row.properties.record_hash
   OR provenance.provenance_hash <> row.properties.provenance_hash
   OR provenance.payload_json <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_IMMUTABLE_DOCUMENTS = """
// v8:check-immutable-documents
UNWIND $rows AS row
MATCH (document:CanonicalDocument {id: row.id})
WITH row, document
WHERE coalesce(document.kb_version, '') <> $kb_version
   OR coalesce(document.canonical_place_id, '') <>
      row.properties.canonical_place_id
   OR coalesce(document.record_hash, '') <> row.properties.record_hash
   OR coalesce(document.content_hash, '') <> row.properties.content_hash
   OR coalesce(document.payload_json, '') <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_IMMUTABLE_TEXT_UNITS = """
// v8:check-immutable-text-units
UNWIND $rows AS row
MATCH (unit:CanonicalTextUnit {id: row.id})
WITH row, unit
WHERE coalesce(unit.kb_version, '') <> $kb_version
   OR coalesce(unit.canonical_place_id, '') <>
      row.properties.canonical_place_id
   OR coalesce(unit.record_hash, '') <> row.properties.record_hash
   OR coalesce(unit.content_hash, '') <> row.properties.content_hash
   OR coalesce(unit.payload_json, '') <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_IMMUTABLE_FACTS = """
// v8:check-immutable-facts
UNWIND $rows AS row
MATCH (fact:CanonicalFact {id: row.id})
WITH row, fact
WHERE coalesce(fact.kb_version, '') <> $kb_version
   OR coalesce(fact.canonical_place_id, '') <>
      row.properties.canonical_place_id
   OR coalesce(fact.record_hash, '') <> row.properties.record_hash
   OR coalesce(fact.content_hash, '') <> row.properties.content_hash
   OR coalesce(fact.payload_json, '') <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_IMMUTABLE_CLAIMS = """
// v8:check-immutable-claims
UNWIND $rows AS row
MATCH (claim:CanonicalClaim {id: row.id})
WITH row, claim
WHERE coalesce(claim.kb_version, '') <> $kb_version
   OR coalesce(claim.canonical_place_id, '') <>
      row.properties.canonical_place_id
   OR coalesce(claim.record_hash, '') <> row.properties.record_hash
   OR coalesce(claim.content_hash, '') <> row.properties.content_hash
   OR coalesce(claim.payload_json, '') <> row.properties.payload_json
RETURN collect(row.id) AS mismatched_ids
"""

_CHECK_STAGED_RELEASE = """
// v8:check-staged-release
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
OPTIONAL MATCH (release)-[:CONTAINS_VERSION]->(version:PlaceVersion)
WITH release, version
ORDER BY version.canonical_place_id
RETURN release.release_hash AS release_hash,
       release.manifest_json AS manifest_json,
       collect(version.canonical_place_id) AS place_ids,
       count(version) AS version_count,
       count { (release)-[:CONTAINS_DOCUMENT]->(:CanonicalDocument) }
         AS document_count,
       count { (release)-[:CONTAINS_TEXT_UNIT]->(:CanonicalTextUnit) }
         AS text_unit_count,
       count { (release)-[:CONTAINS_FACT]->(:CanonicalFact) } AS fact_count,
       count { (release)-[:CONTAINS_CLAIM]->(:CanonicalClaim) } AS claim_count
"""

_CHECK_STAGED_PROVENANCE = """
// v8:check-staged-provenance
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MATCH (release)-[:CONTAINS_VERSION]->(version:PlaceVersion)
OPTIONAL MATCH (version)-[:SUPPORTED_BY]->(provenance:CanonicalSourceProvenance)
WITH version, count(provenance) AS provenance_count
RETURN collect(
  CASE WHEN provenance_count = 1 THEN NULL ELSE version.canonical_place_id END
) AS invalid_place_ids,
count(version) AS version_count,
sum(provenance_count) AS provenance_count
"""

_CLEAN_CURRENT_OUTGOING = """
// v8:clean-current-outgoing
MATCH (place:Place {kb_version: $kb_version})-[relationship]->()
WHERE type(relationship) IN [
  'CURRENT_VERSION', 'IN_CITY', 'HAS_TYPE', 'HAS_CATEGORY', 'HAS_CONCEPT',
  'HAS_FACT'
]
DELETE relationship
"""

_CLEAN_CURRENT_INCOMING = """
// v8:clean-current-incoming
MATCH ()-[relationship]->(place:Place {kb_version: $kb_version})
WHERE type(relationship) IN [
  'HAS_PLACE', 'CONTAINS_PLACE', 'MENTIONS', 'ABOUT', 'SOURCE_FOR'
]
DELETE relationship
"""

_CLEAN_CURRENT_COMPATIBILITY = """
// v8:clean-current-compatibility
MATCH (node {kb_version: $kb_version})-[relationship]->
      (place:Place {kb_version: $kb_version})
WHERE (node:CanonicalClaim AND type(relationship) = 'ABOUT')
   OR (node:CanonicalTextUnit AND type(relationship) = 'MENTIONS')
   OR (node:CanonicalDocument AND type(relationship) = 'SOURCE_FOR')
DELETE relationship
"""

_CLEAN_CURRENT_CITY_TYPES = """
// v8:clean-current-city-types
MATCH (:CanonicalCity {kb_version: $kb_version})
      -[relationship:HAS_PLACE_TYPE]->()
DELETE relationship
"""

_CLEAN_CATALOG_RELATIONSHIPS = """
// v8:clean-catalog-relationships
MATCH (catalog:TravelCatalog {kb_version: $kb_version})-[relationship]->()
WHERE type(relationship) IN ['HAS_CITY', 'CURRENT_RELEASE']
DELETE relationship
"""

_RESET_CATALOG_LABELS = """
// v8:reset-catalog-labels
MATCH (catalog:TravelCatalog {kb_version: $kb_version})
REMOVE catalog:TravelCatalog
SET catalog:ArchivedCatalog
"""

_RESET_CITY_LABELS = """
// v8:reset-city-labels
MATCH (city:City {kb_version: $kb_version})
REMOVE city:City
SET city:ArchivedCity:Entity
"""

_RESET_TYPE_LABELS = """
// v8:reset-type-labels
MATCH (placeType:PlaceType {kb_version: $kb_version})
REMOVE placeType:PlaceType
SET placeType:ArchivedPlaceType:Entity
"""

_RESET_CATEGORY_LABELS = """
// v8:reset-category-labels
MATCH (category:Category {kb_version: $kb_version})
REMOVE category:Category
SET category:ArchivedCategory:Entity
"""

_RESET_CONCEPT_LABELS = """
// v8:reset-concept-labels
MATCH (concept:Concept {kb_version: $kb_version})
REMOVE concept:Concept:Term:Dish:Activity
SET concept:ArchivedConcept:Entity
"""

_RESET_DOCUMENT_LABELS = """
// v8:reset-document-labels
MATCH (document:CanonicalDocument:Document {kb_version: $kb_version})
REMOVE document:Document
SET document:ArchivedDocument:Entity
"""

_RESET_TEXT_UNIT_LABELS = """
// v8:reset-text-unit-labels
MATCH (unit:CanonicalTextUnit:TextUnit {kb_version: $kb_version})
REMOVE unit:TextUnit
SET unit:ArchivedTextUnit:Entity
"""

_RESET_FACT_LABELS = """
// v8:reset-fact-labels
MATCH (fact:CanonicalFact:Fact {kb_version: $kb_version})
REMOVE fact:Fact
SET fact:ArchivedFact:Entity
"""

_RESET_CLAIM_LABELS = """
// v8:reset-claim-labels
MATCH (claim:CanonicalClaim:Claim {kb_version: $kb_version})
REMOVE claim:Claim
SET claim:ArchivedClaim:Entity
"""

_ARCHIVE_OMITTED_PLACES = """
// v8:archive-omitted-places
MATCH (place:Place {kb_version: $kb_version})
WHERE NOT place.id IN $place_ids
REMOVE place:Place
SET place:ArchivedPlace,
    place.active = false,
    place.deactivated_in_release_id = $release_id
"""

_ACTIVATE_CITIES = """
// v8:activate-cities
UNWIND $rows AS row
MATCH (city:CanonicalCity {id: row.id, kb_version: $kb_version})
SET city += row.properties
SET city:City:Entity
REMOVE city:ArchivedCity
"""

_ACTIVATE_TYPES = """
// v8:activate-types
UNWIND $rows AS row
MATCH (placeType:CanonicalPlaceType {id: row.id, kb_version: $kb_version})
SET placeType += row.properties
SET placeType:PlaceType:Entity
REMOVE placeType:ArchivedPlaceType
"""

_ACTIVATE_CATEGORIES = """
// v8:activate-categories
UNWIND $rows AS row
MATCH (category:CanonicalCategory {id: row.id, kb_version: $kb_version})
SET category += row.properties
SET category:Category:Entity
REMOVE category:ArchivedCategory
"""

_ACTIVATE_CONCEPTS = """
// v8:activate-concepts
UNWIND $rows AS row
MATCH (concept:CanonicalConcept {id: row.id, kb_version: $kb_version})
SET concept += row.properties
SET concept:Concept:Term:Entity
REMOVE concept:ArchivedConcept
"""

_ACTIVATE_PLACES = """
// v8:activate-places
UNWIND $rows AS row
MATCH (place:CanonicalPlace {id: row.id, kb_version: $kb_version})
WITH place, row,
     CASE
       WHEN place.semantic_content_hash = row.properties.semantic_content_hash
       THEN place.embedding
       ELSE null
     END AS preserved_embedding,
     CASE
       WHEN place.semantic_content_hash = row.properties.semantic_content_hash
       THEN place.embedding_model
       ELSE null
     END AS preserved_embedding_model,
     CASE
       WHEN place.semantic_content_hash = row.properties.semantic_content_hash
       THEN place.embedding_dimension
       ELSE null
     END AS preserved_embedding_dimension
SET place = row.properties
SET place:Place:Entity
REMOVE place:ArchivedPlace
FOREACH (
  ignored IN CASE WHEN preserved_embedding IS NULL THEN [] ELSE [1] END |
  SET place.embedding = preserved_embedding,
      place.embedding_model = preserved_embedding_model,
      place.embedding_dimension = preserved_embedding_dimension,
      place.embedding_content_hash = row.properties.semantic_content_hash
)
SET place.location = point({
  latitude: row.properties.lat,
  longitude: row.properties.lng
})
WITH place, row
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MATCH (city:CanonicalCity {id: row.city_id, kb_version: $kb_version})
SET city:City
REMOVE city:ArchivedCity
MERGE (place)-[:CURRENT_VERSION]->(version)
MERGE (place)-[:IN_CITY]->(city)
MERGE (city)-[:HAS_PLACE]->(place)
"""

_ACTIVATE_PLACE_TYPES = """
// v8:activate-place-types
UNWIND $rows AS row
UNWIND row.type_ids AS type_id
MATCH (place:Place {id: row.id, kb_version: $kb_version})
MATCH (placeType:CanonicalPlaceType {id: type_id, kb_version: $kb_version})
MATCH (city:CanonicalCity {id: row.city_id, kb_version: $kb_version})
SET placeType:PlaceType
REMOVE placeType:ArchivedPlaceType
MERGE (place)-[:HAS_TYPE]->(placeType)
MERGE (placeType)-[:CONTAINS_PLACE]->(place)
MERGE (city)-[:HAS_PLACE_TYPE]->(placeType)
"""

_ACTIVATE_PLACE_CATEGORIES = """
// v8:activate-place-categories
UNWIND $rows AS row
UNWIND row.category_ids AS category_id
MATCH (place:Place {id: row.id, kb_version: $kb_version})
MATCH (category:CanonicalCategory {id: category_id, kb_version: $kb_version})
SET category:Category
REMOVE category:ArchivedCategory
MERGE (place)-[:HAS_CATEGORY]->(category)
"""

_ACTIVATE_PLACE_CONCEPTS = """
// v8:activate-place-concepts
UNWIND $rows AS row
UNWIND row.concept_ids AS concept_id
MATCH (place:Place {id: row.id, kb_version: $kb_version})
MATCH (concept:CanonicalConcept {id: concept_id, kb_version: $kb_version})
SET concept:Concept:Term
REMOVE concept:ArchivedConcept
MERGE (place)-[:HAS_CONCEPT]->(concept)
"""

_ACTIVATE_TYPED_CONCEPTS = """
// v8:activate-typed-concepts
MATCH (concept:Concept {kb_version: $kb_version})
FOREACH (
  ignored IN CASE WHEN concept.concept_type = 'signature_dishes' THEN [1] ELSE [] END |
  SET concept:Dish
)
FOREACH (
  ignored IN CASE WHEN concept.concept_type = 'activities' THEN [1] ELSE [] END |
  SET concept:Activity
)
"""

_ACTIVATE_EVIDENCE = """
// v8:activate-evidence
UNWIND $rows AS row
MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
MATCH (version:PlaceVersion {id: row.version_id, kb_version: $kb_version})
MATCH (document:CanonicalDocument {id: row.document_id, kb_version: $kb_version})
MATCH (unit:CanonicalTextUnit {id: row.id, kb_version: $kb_version})
SET document:Document:Entity,
    unit:TextUnit:Entity
REMOVE document:ArchivedDocument,
       unit:ArchivedTextUnit
MERGE (unit)-[:PART_OF]->(document)
MERGE (unit)-[:MENTIONS]->(place)
MERGE (document)-[:SOURCE_FOR]->(place)
"""

_ACTIVATE_FACTS = """
// v8:activate-facts
UNWIND $rows AS row
MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
MATCH (fact:CanonicalFact {id: row.id, kb_version: $kb_version})
MATCH (unit:CanonicalTextUnit {id: row.text_unit_id, kb_version: $kb_version})
SET fact:Fact:Entity
REMOVE fact:ArchivedFact
MERGE (place)-[:HAS_FACT]->(fact)
MERGE (fact)-[:SUPPORTED_BY]->(unit)
"""

_ACTIVATE_CLAIMS = """
// v8:activate-claims
UNWIND $rows AS row
MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
MATCH (claim:CanonicalClaim {id: row.id, kb_version: $kb_version})
MATCH (concept:Concept {id: row.concept_id, kb_version: $kb_version})
MATCH (unit:CanonicalTextUnit {id: row.text_unit_id, kb_version: $kb_version})
SET claim:Claim:Entity
REMOVE claim:ArchivedClaim
MERGE (claim)-[:ABOUT]->(place)
MERGE (claim)-[:OBJECT]->(concept)
MERGE (claim)-[:SUPPORTED_BY]->(unit)
"""

_ACTIVATE_CATALOG = """
// v8:activate-catalog
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
MERGE (catalog:CanonicalTravelCatalog {id: 'v8:canonical-travel-catalog'})
SET catalog = $properties
SET catalog:TravelCatalog:Entity
REMOVE catalog:ArchivedCatalog
MERGE (catalog)-[:CURRENT_RELEASE]->(release)
"""

_ACTIVATE_CATALOG_CITIES = """
// v8:activate-catalog-cities
UNWIND $rows AS row
MATCH (catalog:CanonicalTravelCatalog {
  id: 'v8:canonical-travel-catalog',
  kb_version: $kb_version
})
MATCH (city:CanonicalCity:City {id: row.id, kb_version: $kb_version})
OPTIONAL MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(city)
WITH catalog, city, count(DISTINCT place) AS place_count
MERGE (catalog)-[relationship:HAS_CITY]->(city)
SET relationship.place_count_snapshot = place_count
"""

_REFRESH_TYPE_COUNT_SNAPSHOTS = """
// v8:refresh-type-count-snapshots
MATCH (city:CanonicalCity:City {kb_version: $kb_version})
      -[relationship:HAS_PLACE_TYPE]->
      (placeType:CanonicalPlaceType:PlaceType {kb_version: $kb_version})
OPTIONAL MATCH (placeType)-[:CONTAINS_PLACE]->
               (place:Place {kb_version: $kb_version})-[:IN_CITY]->(city)
WITH relationship, count(DISTINCT place) AS place_count
SET relationship.place_count_snapshot = place_count
"""

_ACTIVATE_RELEASE = """
// v8:activate-release
MATCH (release:DatasetRelease {id: $release_id, kb_version: $kb_version})
OPTIONAL MATCH (previous:DatasetRelease {
  kb_version: $kb_version,
  status: 'active'
})
WHERE previous.id <> release.id
FOREACH (
  ignored IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
  SET previous.status = 'superseded',
      previous.superseded_by = release.id
)
WITH DISTINCT release
MATCH (catalog:CanonicalTravelCatalog {
  id: 'v8:canonical-travel-catalog',
  kb_version: $kb_version
})
SET release.status = 'active',
    release.activated_at = datetime(),
    catalog.status = 'ready',
    catalog.built_at = datetime()
RETURN release.id AS release_id, release.status AS status
"""

_CHECK_ACTIVE_RELEASE = """
// v8:check-active-release
MATCH (release:DatasetRelease {
  id: $release_id,
  kb_version: $kb_version,
  status: 'active'
})
MATCH (catalog:CanonicalTravelCatalog:TravelCatalog {
  id: 'v8:canonical-travel-catalog',
  kb_version: $kb_version,
  status: 'ready'
})-[:CURRENT_RELEASE]->(release)
OPTIONAL MATCH (place:Place {kb_version: $kb_version})
WITH release, catalog, place
ORDER BY place.id
RETURN release.id AS release_id,
       collect(place.id) AS place_ids,
       count(place) AS place_count,
       sum(count {
         (place)-[:CURRENT_VERSION]->(:PlaceVersion)
       }) AS current_version_count,
       sum(count {
         (place)<-[:SOURCE_FOR]-(:CanonicalDocument:Document)
       }) AS document_count,
       sum(count {
         (place)<-[:MENTIONS]-(:CanonicalTextUnit:TextUnit)
       }) AS text_unit_count,
       sum(count {
         (place)<-[:MENTIONS]-(:CanonicalTextUnit:TextUnit)-[:PART_OF]->
         (:CanonicalDocument:Document)
       }) AS text_unit_document_count,
       sum(count {
         (place)-[:HAS_FACT]->(:CanonicalFact:Fact)
       }) AS fact_count,
       sum(count {
         (place)-[:HAS_FACT]->(:CanonicalFact:Fact)-[:SUPPORTED_BY]->
         (:CanonicalTextUnit:TextUnit)
       }) AS fact_evidence_count,
       sum(count {
         (place)<-[:ABOUT]-(:CanonicalClaim:Claim)
       }) AS claim_count,
       sum(count {
         (place)<-[:ABOUT]-(:CanonicalClaim:Claim)-[:OBJECT]->
         (:Concept)
       }) AS claim_object_count,
       sum(count {
         (place)<-[:ABOUT]-(:CanonicalClaim:Claim)-[:SUPPORTED_BY]->
         (:CanonicalTextUnit:TextUnit)
       }) AS claim_evidence_count,
       count { (catalog)-[:HAS_CITY]->(:City) } AS city_count,
       catalog.semantic_index_status AS semantic_index_status
"""


def ensure_canonical_v8_schema(
    store: _GraphStore,
    *,
    embedding_dimension: int = 1536,
) -> None:
    """Create only V8-owned constraints/indexes; no model or Gemini is needed."""

    if embedding_dimension <= 0:
        raise ValueError("embedding_dimension must be positive")
    for statement in _SCHEMA_STATEMENTS:
        store.run(statement)
    store.run(
        f"""
        CREATE VECTOR INDEX place_embedding IF NOT EXISTS
        FOR (node:Place) ON (node.embedding)
        OPTIONS {{indexConfig: {{
          `vector.dimensions`: {embedding_dimension},
          `vector.similarity_function`: 'cosine'
        }}}}
        """
    )
    store.run(
        f"""
        CREATE VECTOR INDEX concept_embedding IF NOT EXISTS
        FOR (node:Concept) ON (node.embedding)
        OPTIONS {{indexConfig: {{
          `vector.dimensions`: {embedding_dimension},
          `vector.similarity_function`: 'cosine'
        }}}}
        """
    )
    store.run(
        f"""
        CREATE VECTOR INDEX text_unit_embedding IF NOT EXISTS
        FOR (node:TextUnit) ON (node.embedding)
        OPTIONS {{indexConfig: {{
          `vector.dimensions`: {embedding_dimension},
          `vector.similarity_function`: 'cosine'
        }}}}
        """
    )
    store.run("CALL db.awaitIndexes(60)")


def apply_canonical_v8_import(
    store: _GraphStore,
    plan: CanonicalV8ImportPlan,
    *,
    ensure_schema: bool = True,
    batch_size: int = 500,
) -> CanonicalV8ImportResult:
    """Commit bounded staging batches, then atomically activate the release.

    Staging nodes are V8-owned but deliberately lack the generic labels used by
    retrieval. If any batch or validation fails, the release remains inactive
    and the previous catalog snapshot is untouched. Only the final, bounded
    current-projection switch runs in one transaction.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    validated_release = CanonicalV8ReleaseManifest.model_validate_json(
        plan.release.model_dump_json()
    )
    _validate_plan_counts(plan, validated_release)
    if ensure_schema:
        ensure_canonical_v8_schema(
            store,
            embedding_dimension=validated_release.embedding_dimension,
        )

    database = getattr(getattr(store, "settings", None), "neo4j_database", None)
    session_options = {"database": database} if database else {}
    with store.driver.session(**session_options) as session:
        initialized = session.execute_write(
            lambda transaction: _initialize_release_transaction(transaction, plan)
        )
        if bool(initialized["already_active"]):
            counts = initialized
        else:
            _stage_release_in_committed_batches(
                session,
                plan,
                batch_size=batch_size,
            )
            session.execute_write(
                lambda transaction: _verify_staged_release_summary(
                    transaction,
                    plan,
                )
            )
            counts = session.execute_write(
                lambda transaction: _write_release_transaction(
                    transaction,
                    plan,
                    batch_size=batch_size,
                    staging_complete=True,
                )
            )
    return CanonicalV8ImportResult(
        release_id=validated_release.release_id,
        release_hash=validated_release.release_hash,
        dataset_id=validated_release.dataset_id,
        dataset_hash=validated_release.dataset_hash,
        dry_run=False,
        status="active",
        place_count=counts["place_count"],
        city_count=len(plan.cities),
        place_version_count=counts["place_version_count"],
        provenance_count=counts["provenance_count"],
        type_count=len(plan.place_types),
        category_count=len(plan.categories),
        concept_count=len(plan.concepts),
        document_count=counts["document_count"],
        text_unit_count=counts["text_unit_count"],
        fact_count=counts["fact_count"],
        claim_count=counts["claim_count"],
        static_graph_ready=True,
        semantic_index_ready=counts["semantic_index_status"] == "ready",
        semantic_index_status=counts["semantic_index_status"],
    )


def _validate_plan_counts(
    plan: CanonicalV8ImportPlan,
    release: CanonicalV8ReleaseManifest,
) -> None:
    expected = {
        "place": (len(plan.places), release.place_count),
        "place version": (len(plan.versions), release.place_count),
        "provenance": (len(plan.provenances), release.place_count),
        "document": (len(plan.documents), release.document_count),
        "text unit": (len(plan.text_units), release.text_unit_count),
        "fact": (len(plan.facts), release.fact_count),
        "claim": (len(plan.claims), release.claim_count),
    }
    mismatches = [
        f"{label}={actual}, expected={declared}"
        for label, (actual, declared) in expected.items()
        if actual != declared
    ]
    if mismatches:
        raise CanonicalV8ArtifactError(
            "plan counts do not match immutable release: " + ", ".join(mismatches)
        )


def _release_manifest_json(release: CanonicalV8ReleaseManifest) -> str:
    return _json_dumps(release.model_dump(mode="json"))


def _release_properties(
    release: CanonicalV8ReleaseManifest,
    manifest_json: str,
) -> dict[str, Any]:
    return {
        "id": release.release_id,
        "release_hash": release.release_hash,
        "kb_version": KB_VERSION,
        "dataset_id": release.dataset_id,
        "dataset_hash": release.dataset_hash,
        "canonical_manifest_id": release.canonical_manifest_id,
        "canonical_manifest_hash": release.canonical_manifest_hash,
        "readiness_id": release.readiness_id,
        "readiness_hash": release.readiness_hash,
        "completeness_audit_id": release.completeness_audit_id,
        "completeness_audit_hash": release.completeness_audit_hash,
        "source_as_of": release.source_as_of,
        "place_count": release.place_count,
        "city_count": release.city_count,
        "document_count": release.document_count,
        "text_unit_count": release.text_unit_count,
        "fact_count": release.fact_count,
        "claim_count": release.claim_count,
        "record_set_hash": release.record_set_hash,
        "manifest_json": manifest_json,
        "status": "staging",
    }


def _stage_release_header(
    transaction: Any,
    plan: CanonicalV8ImportPlan,
) -> tuple[dict[str, Any], str]:
    release = plan.release
    manifest_json = _release_manifest_json(release)
    staged_release = _single_record(
        transaction.run(
            _STAGE_RELEASE,
            release_id=release.release_id,
            properties=_release_properties(release, manifest_json),
        ),
        "stage release",
    )
    if (
        staged_release.get("release_hash") != release.release_hash
        or staged_release.get("manifest_json") != manifest_json
        or staged_release.get("kb_version") != KB_VERSION
    ):
        raise CanonicalV8GraphWriteError(
            "existing release node conflicts with immutable release manifest"
        )
    return staged_release, manifest_json


def _plan_result_counts(
    plan: CanonicalV8ImportPlan,
    *,
    semantic_index_status: str,
    already_active: bool,
) -> dict[str, int | str | bool]:
    return {
        "already_active": already_active,
        "place_count": len(plan.places),
        "place_version_count": len(plan.versions),
        "provenance_count": len(plan.provenances),
        "document_count": len(plan.documents),
        "text_unit_count": len(plan.text_units),
        "fact_count": len(plan.facts),
        "claim_count": len(plan.claims),
        "semantic_index_status": semantic_index_status,
    }


def _validate_active_snapshot(
    active: dict[str, Any],
    plan: CanonicalV8ImportPlan,
) -> None:
    expected_place_ids = sorted(item["id"] for item in plan.places)
    if (
        active.get("release_id") != plan.release.release_id
        or active.get("place_ids") != expected_place_ids
        or int(active.get("place_count", -1)) != len(plan.places)
        or int(active.get("current_version_count", -1)) != len(plan.versions)
        or int(active.get("city_count", -1)) != len(plan.cities)
        or int(active.get("document_count", -1)) != len(plan.documents)
        or int(active.get("text_unit_count", -1)) != len(plan.text_units)
        or int(active.get("text_unit_document_count", -1)) != len(plan.text_units)
        or int(active.get("fact_count", -1)) != len(plan.facts)
        or int(active.get("fact_evidence_count", -1)) != len(plan.facts)
        or int(active.get("claim_count", -1)) != len(plan.claims)
        or int(active.get("claim_object_count", -1)) != len(plan.claims)
        or int(active.get("claim_evidence_count", -1)) != len(plan.claims)
    ):
        raise CanonicalV8GraphWriteError(
            "already-active V8 snapshot does not match the release"
        )


def _initialize_release_transaction(
    transaction: Any,
    plan: CanonicalV8ImportPlan,
) -> dict[str, int | str | bool]:
    staged_release, _ = _stage_release_header(transaction, plan)
    status = str(staged_release.get("status") or "")
    if status == "active":
        active = _single_record(
            transaction.run(
                _CHECK_ACTIVE_RELEASE,
                release_id=plan.release.release_id,
                kb_version=KB_VERSION,
            ),
            "validate already-active release",
        )
        _validate_active_snapshot(active, plan)
        return _plan_result_counts(
            plan,
            semantic_index_status=str(active.get("semantic_index_status") or "pending"),
            already_active=True,
        )
    if status != "staging":
        raise CanonicalV8GraphWriteError(
            f"release cannot be resumed from status {status!r}"
        )
    return {"already_active": False}


def _stage_release_in_committed_batches(
    session: Any,
    plan: CanonicalV8ImportPlan,
    *,
    batch_size: int,
) -> None:
    """Commit idempotent, retrieval-invisible staging batches."""

    release_id = plan.release.release_id
    for query, rows in (
        (_STAGE_CITIES, plan.cities),
        (_STAGE_TYPES, plan.place_types),
        (_STAGE_CATEGORIES, plan.categories),
        (_STAGE_CONCEPTS, plan.concepts),
    ):
        _run_committed_batches(session, query, rows, batch_size)

    for stage_query, check_query, rows, label, params in (
        (
            _STAGE_VERSIONS,
            _CHECK_IMMUTABLE_VERSIONS,
            plan.versions,
            "place versions",
            {"release_id": release_id},
        ),
        (
            _STAGE_PROVENANCE,
            _CHECK_IMMUTABLE_PROVENANCE,
            plan.provenances,
            "source provenance",
            {},
        ),
        (
            _STAGE_DOCUMENTS,
            _CHECK_IMMUTABLE_DOCUMENTS,
            plan.documents,
            "documents",
            {"release_id": release_id},
        ),
        (
            _STAGE_TEXT_UNITS,
            _CHECK_IMMUTABLE_TEXT_UNITS,
            plan.text_units,
            "text units",
            {"release_id": release_id},
        ),
        (
            _STAGE_FACTS,
            _CHECK_IMMUTABLE_FACTS,
            plan.facts,
            "facts",
            {"release_id": release_id},
        ),
        (
            _STAGE_CLAIMS,
            _CHECK_IMMUTABLE_CLAIMS,
            plan.claims,
            "claims",
            {"release_id": release_id},
        ),
    ):
        _run_checked_committed_batches(
            session,
            stage_query,
            check_query,
            rows,
            batch_size,
            label=label,
            **params,
        )

    for query in (
        _STAGE_VERSION_CITY,
        _STAGE_VERSION_TYPES,
        _STAGE_VERSION_CATEGORIES,
        _STAGE_VERSION_CONCEPTS,
    ):
        _run_committed_batches(session, query, plan.versions, batch_size)


def _verify_staged_release_summary(
    transaction: Any,
    plan: CanonicalV8ImportPlan,
) -> None:
    release = plan.release
    manifest_json = _release_manifest_json(release)
    expected_place_ids = sorted(item["id"] for item in plan.places)
    staged = _single_record(
        transaction.run(
            _CHECK_STAGED_RELEASE,
            release_id=release.release_id,
            kb_version=KB_VERSION,
        ),
        "validate staged release",
    )
    if (
        staged.get("release_hash") != release.release_hash
        or staged.get("manifest_json") != manifest_json
        or staged.get("place_ids") != expected_place_ids
        or int(staged.get("version_count", -1)) != len(plan.versions)
        or int(staged.get("document_count", -1)) != len(plan.documents)
        or int(staged.get("text_unit_count", -1)) != len(plan.text_units)
        or int(staged.get("fact_count", -1)) != len(plan.facts)
        or int(staged.get("claim_count", -1)) != len(plan.claims)
    ):
        raise CanonicalV8GraphWriteError(
            "staged release does not exactly cover canonical place IDs"
        )
    provenance_check = _single_record(
        transaction.run(
            _CHECK_STAGED_PROVENANCE,
            release_id=release.release_id,
            kb_version=KB_VERSION,
        ),
        "validate staged provenance",
    )
    invalid_provenance = [
        item for item in provenance_check.get("invalid_place_ids", []) if item
    ]
    if (
        invalid_provenance
        or int(provenance_check.get("version_count", -1)) != len(plan.versions)
        or int(provenance_check.get("provenance_count", -1)) != len(plan.provenances)
    ):
        raise CanonicalV8GraphWriteError(
            "every staged place version must have exactly one source provenance"
        )


def _write_release_transaction(
    transaction: Any,
    plan: CanonicalV8ImportPlan,
    *,
    batch_size: int,
    staging_complete: bool,
) -> dict[str, int | str | bool]:
    release = plan.release
    staged_release, _ = _stage_release_header(transaction, plan)
    if staged_release.get("status") == "active":
        active = _single_record(
            transaction.run(
                _CHECK_ACTIVE_RELEASE,
                release_id=release.release_id,
                kb_version=KB_VERSION,
            ),
            "validate already-active release",
        )
        _validate_active_snapshot(active, plan)
        return _plan_result_counts(
            plan,
            semantic_index_status=str(active.get("semantic_index_status") or "pending"),
            already_active=True,
        )
    if staged_release.get("status") != "staging":
        raise CanonicalV8GraphWriteError(
            "only an inactive staging release may be activated"
        )
    if not staging_complete:
        raise CanonicalV8GraphWriteError(
            "final activation requires completed committed staging"
        )

    expected_place_ids = sorted(item["id"] for item in plan.places)
    # Recheck committed staging inside the activation transaction. A failure
    # rolls back every current-label/current-edge mutation below.
    _verify_staged_release_summary(transaction, plan)

    transaction.run(_CLEAN_CURRENT_OUTGOING, kb_version=KB_VERSION).consume()
    transaction.run(_CLEAN_CURRENT_INCOMING, kb_version=KB_VERSION).consume()
    transaction.run(
        _CLEAN_CURRENT_COMPATIBILITY,
        kb_version=KB_VERSION,
    ).consume()
    transaction.run(_CLEAN_CURRENT_CITY_TYPES, kb_version=KB_VERSION).consume()
    transaction.run(_CLEAN_CATALOG_RELATIONSHIPS, kb_version=KB_VERSION).consume()
    for query in (
        _RESET_CATALOG_LABELS,
        _RESET_CITY_LABELS,
        _RESET_TYPE_LABELS,
        _RESET_CATEGORY_LABELS,
        _RESET_CONCEPT_LABELS,
        _RESET_DOCUMENT_LABELS,
        _RESET_TEXT_UNIT_LABELS,
        _RESET_FACT_LABELS,
        _RESET_CLAIM_LABELS,
    ):
        transaction.run(query, kb_version=KB_VERSION).consume()
    activation_batch_size = min(batch_size, 100)
    for query, rows in (
        (_ACTIVATE_CITIES, plan.cities),
        (_ACTIVATE_TYPES, plan.place_types),
        (_ACTIVATE_CATEGORIES, plan.categories),
        (_ACTIVATE_CONCEPTS, plan.concepts),
    ):
        _run_batches(
            transaction,
            query,
            rows,
            activation_batch_size,
        )
    transaction.run(
        _ARCHIVE_OMITTED_PLACES,
        kb_version=KB_VERSION,
        place_ids=expected_place_ids,
        release_id=release.release_id,
    ).consume()
    _run_batches(transaction, _ACTIVATE_PLACES, plan.places, activation_batch_size)
    _run_batches(
        transaction,
        _ACTIVATE_PLACE_TYPES,
        plan.places,
        activation_batch_size,
    )
    _run_batches(
        transaction,
        _ACTIVATE_PLACE_CATEGORIES,
        plan.places,
        activation_batch_size,
    )
    _run_batches(
        transaction,
        _ACTIVATE_PLACE_CONCEPTS,
        plan.places,
        activation_batch_size,
    )
    transaction.run(
        _ACTIVATE_TYPED_CONCEPTS,
        kb_version=KB_VERSION,
    ).consume()
    _run_batches(
        transaction,
        _ACTIVATE_EVIDENCE,
        plan.text_units,
        activation_batch_size,
    )
    _run_batches(transaction, _ACTIVATE_FACTS, plan.facts, activation_batch_size)
    _run_batches(transaction, _ACTIVATE_CLAIMS, plan.claims, activation_batch_size)
    catalog_properties = {
        "id": "v8:canonical-travel-catalog",
        "name": "NexTripAI Canonical Travel Catalog",
        "kb_version": KB_VERSION,
        "status": "building",
        "current_release_id": release.release_id,
        "dataset_id": release.dataset_id,
        "dataset_hash": release.dataset_hash,
        "source_place_count": release.place_count,
        "source_city_count": release.city_count,
        "source_document_count": release.document_count,
        "source_text_unit_count": release.text_unit_count,
        "source_fact_count": release.fact_count,
        "source_claim_count": release.claim_count,
        "static_graph_ready": True,
        "semantic_index_ready": False,
        "semantic_index_status": release.semantic_index_status,
        "embedding_dimension": release.embedding_dimension,
    }
    transaction.run(
        _ACTIVATE_CATALOG,
        release_id=release.release_id,
        kb_version=KB_VERSION,
        properties=catalog_properties,
    ).consume()
    _run_batches(
        transaction,
        _ACTIVATE_CATALOG_CITIES,
        plan.cities,
        activation_batch_size,
    )
    transaction.run(
        _REFRESH_TYPE_COUNT_SNAPSHOTS,
        kb_version=KB_VERSION,
    ).consume()
    activated = _single_record(
        transaction.run(
            _ACTIVATE_RELEASE,
            release_id=release.release_id,
            kb_version=KB_VERSION,
        ),
        "activate release",
    )
    if activated != {"release_id": release.release_id, "status": "active"}:
        raise CanonicalV8GraphWriteError("release activation did not complete")
    active = _single_record(
        transaction.run(
            _CHECK_ACTIVE_RELEASE,
            release_id=release.release_id,
            kb_version=KB_VERSION,
        ),
        "validate active release",
    )
    if (
        active.get("release_id") != release.release_id
        or active.get("place_ids") != expected_place_ids
        or int(active.get("place_count", -1)) != len(plan.places)
        or int(active.get("current_version_count", -1)) != len(plan.versions)
        or int(active.get("city_count", -1)) != len(plan.cities)
        or int(active.get("document_count", -1)) != len(plan.documents)
        or int(active.get("text_unit_count", -1)) != len(plan.text_units)
        or int(active.get("text_unit_document_count", -1)) != len(plan.text_units)
        or int(active.get("fact_count", -1)) != len(plan.facts)
        or int(active.get("fact_evidence_count", -1)) != len(plan.facts)
        or int(active.get("claim_count", -1)) != len(plan.claims)
        or int(active.get("claim_object_count", -1)) != len(plan.claims)
        or int(active.get("claim_evidence_count", -1)) != len(plan.claims)
        or active.get("semantic_index_status") != "pending"
    ):
        raise CanonicalV8GraphWriteError(
            "active V8 snapshot does not exactly match the release"
        )
    return {
        "place_count": len(plan.places),
        "place_version_count": len(plan.versions),
        "provenance_count": len(plan.provenances),
        "document_count": len(plan.documents),
        "text_unit_count": len(plan.text_units),
        "fact_count": len(plan.facts),
        "claim_count": len(plan.claims),
        "semantic_index_status": "pending",
    }


def _run_batches(
    transaction: Any,
    query: str,
    rows: tuple[dict[str, Any], ...],
    batch_size: int,
    **params: Any,
) -> None:
    for start in range(0, len(rows), batch_size):
        batch = list(rows[start : start + batch_size])
        transaction.run(
            query,
            rows=batch,
            kb_version=KB_VERSION,
            **params,
        ).consume()


def _run_committed_batches(
    session: Any,
    query: str,
    rows: tuple[dict[str, Any], ...],
    batch_size: int,
    **params: Any,
) -> None:
    for start in range(0, len(rows), batch_size):
        batch = list(rows[start : start + batch_size])

        def write_batch(transaction: Any) -> None:
            transaction.run(
                query,
                rows=batch,
                kb_version=KB_VERSION,
                **params,
            ).consume()

        session.execute_write(write_batch)


def _run_checked_committed_batches(
    session: Any,
    stage_query: str,
    check_query: str,
    rows: tuple[dict[str, Any], ...],
    batch_size: int,
    *,
    label: str,
    **params: Any,
) -> None:
    for start in range(0, len(rows), batch_size):
        batch = list(rows[start : start + batch_size])

        def stage_and_check(transaction: Any) -> None:
            transaction.run(
                stage_query,
                rows=batch,
                kb_version=KB_VERSION,
                **params,
            ).consume()
            checked = _single_record(
                transaction.run(
                    check_query,
                    rows=batch,
                    kb_version=KB_VERSION,
                ),
                f"validate immutable {label}",
            )
            mismatched = sorted(checked.get("mismatched_ids", []))
            if mismatched:
                raise CanonicalV8GraphWriteError(
                    f"immutable {label} conflict: " + ", ".join(mismatched[:5])
                )

        session.execute_write(stage_and_check)


def _single_record(result: Any, operation: str) -> dict[str, Any]:
    if hasattr(result, "data"):
        rows = result.data()
    else:
        rows = [
            record.data() if hasattr(record, "data") else dict(record)
            for record in result
        ]
    if len(rows) != 1:
        raise CanonicalV8GraphWriteError(
            f"{operation} expected one result row, received {len(rows)}"
        )
    return dict(rows[0])


__all__ = [
    "CanonicalV8ArtifactError",
    "CanonicalV8GateError",
    "CanonicalV8GraphWriteError",
    "CanonicalV8ImportPlan",
    "CanonicalV8ImportResult",
    "CanonicalV8ReleaseManifest",
    "CanonicalV8ReleaseManifestWriter",
    "apply_canonical_v8_import",
    "dry_run_canonical_v8_import",
    "ensure_canonical_v8_schema",
    "prepare_canonical_v8_import",
]
