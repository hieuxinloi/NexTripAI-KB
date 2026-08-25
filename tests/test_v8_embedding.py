from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from nextrip_graphrag.v8_embedding_cli import build_parser
from nextrip_graphrag.versions.v8.embedding import (
    V8EmbeddingReleaseError,
    V8EmbeddingRunWriter,
    V8EmbeddingTargetType,
    V8EmbeddingWriteError,
    apply_v8_embedding_plan,
    build_v8_embedding_plan,
)


NOW = datetime(2026, 8, 25, 8, tzinfo=timezone.utc)
DATASET_HASH = "d" * 64


class FakeEmbeddingStore:
    def __init__(self) -> None:
        self.release_id = "v8-canonical-release-test"
        self.dataset_id = "canonical-active-test"
        self.dataset_hash = DATASET_HASH
        self.dimension = 2
        self.marked_ready = False
        self.targets = {
            V8EmbeddingTargetType.PLACE: [
                self._target("place-1", "Cafe One", "a" * 64, current=True)
            ],
            V8EmbeddingTargetType.CONCEPT: [
                self._target("concept-1", "pet friendly", "b" * 64)
            ],
            V8EmbeddingTargetType.TEXT_UNIT: [
                self._target("text-1", "Tên: Cafe One", "c" * 64)
            ],
        }

    def _target(self, node_id, text, content_hash, *, current=False):
        return {
            "node_id": node_id,
            "text": text,
            "content_hash": content_hash,
            "embedding_present": current,
            "embedding_model": "gemini-embedding-001" if current else None,
            "embedding_dimension": self.dimension if current else None,
            "embedding_content_hash": content_hash if current else None,
        }

    def run(self, query: str, **params):
        if "v8-embedding:active-release" in query:
            return [
                {
                    "release_id": self.release_id,
                    "dataset_id": self.dataset_id,
                    "dataset_hash": self.dataset_hash,
                    "place_count": 1,
                    "text_unit_count": 1,
                    "embedding_dimension": self.dimension,
                    "semantic_index_status": (
                        "ready" if self.marked_ready else "pending"
                    ),
                }
            ]
        for target_type in V8EmbeddingTargetType:
            if f"v8-embedding:targets-{target_type.value.replace('_', '-')}" in query:
                return [dict(item) for item in self.targets[target_type]]
            if f"v8-embedding:write-{target_type.value.replace('_', '-')}" in query:
                updated = 0
                by_id = {
                    item["node_id"]: item for item in self.targets[target_type]
                }
                for row in params["rows"]:
                    target = by_id.get(row["node_id"])
                    if target is None or target["content_hash"] != row["content_hash"]:
                        continue
                    target.update(
                        {
                            "embedding_present": True,
                            "embedding_model": params["model"],
                            "embedding_dimension": params["dimension"],
                            "embedding_content_hash": row["content_hash"],
                        }
                    )
                    updated += 1
                return [{"updated_count": updated}]
        if "v8-embedding:mark-ready" in query:
            if (
                params["release_id"] == self.release_id
                and params["dataset_id"] == self.dataset_id
                and params["dataset_hash"] == self.dataset_hash
            ):
                self.marked_ready = True
                return [
                    {
                        "release_id": self.release_id,
                        "semantic_index_status": "ready",
                    }
                ]
            return []
        raise AssertionError(f"unexpected query: {query}")


class FakeEmbedder:
    def __init__(self, dimension: int = 2) -> None:
        self.dimension = dimension
        self.calls = []

    def embed_documents(self, texts):
        values = list(texts)
        self.calls.append(values)
        return [[float(index + 1)] * self.dimension for index, _ in enumerate(values)]


def _dataset(store: FakeEmbeddingStore):
    return SimpleNamespace(
        dataset_id=store.dataset_id,
        dataset_hash=store.dataset_hash,
        records=[SimpleNamespace(place_id="place-1")],
    )


def test_embedding_plan_is_release_pinned_and_reuses_current_vectors() -> None:
    store = FakeEmbeddingStore()

    plan = build_v8_embedding_plan(
        store,
        _dataset(store),
        model="gemini-embedding-001",
        dimension=2,
    )

    assert plan.release_id == store.release_id
    assert len(plan.targets) == 3
    assert [item.target_type for item in plan.targets] == [
        V8EmbeddingTargetType.CONCEPT,
        V8EmbeddingTargetType.PLACE,
        V8EmbeddingTargetType.TEXT_UNIT,
    ]
    assert sum(item.embedding_current for item in plan.targets) == 1


def test_embedding_run_only_calls_provider_for_missing_and_marks_ready(tmp_path) -> None:
    store = FakeEmbeddingStore()
    plan = build_v8_embedding_plan(
        store,
        _dataset(store),
        model="gemini-embedding-001",
        dimension=2,
    )
    embedder = FakeEmbedder()

    result = apply_v8_embedding_plan(
        store,
        plan,
        embedder,
        run_id="embedding-attempt-1",
        batch_size=8,
        clock=lambda: NOW,
    )

    assert embedder.calls == [["pet friendly"], ["Tên: Cafe One"]]
    assert store.marked_ready is True
    assert result.ready is True
    by_type = {item.target_type: item for item in result.families}
    assert by_type[V8EmbeddingTargetType.PLACE].reused_count == 1
    assert by_type[V8EmbeddingTargetType.CONCEPT].embedded_count == 1
    assert by_type[V8EmbeddingTargetType.TEXT_UNIT].ready_count == 1
    writer = V8EmbeddingRunWriter(tmp_path)
    assert writer.write(result) == writer.write(result)


def test_embedding_rejects_wrong_dataset_and_wrong_vector_dimension() -> None:
    store = FakeEmbeddingStore()
    wrong_dataset = SimpleNamespace(
        dataset_id="canonical-active-other",
        dataset_hash="e" * 64,
        records=[SimpleNamespace(place_id="place-1")],
    )
    with pytest.raises(V8EmbeddingReleaseError, match="does not match"):
        build_v8_embedding_plan(
            store,
            wrong_dataset,
            model="gemini-embedding-001",
            dimension=2,
        )

    plan = build_v8_embedding_plan(
        store,
        _dataset(store),
        model="gemini-embedding-001",
        dimension=2,
    )
    with pytest.raises(V8EmbeddingWriteError, match="dimension mismatch"):
        apply_v8_embedding_plan(
            store,
            plan,
            FakeEmbedder(dimension=3),
            run_id="embedding-bad-dimension",
            clock=lambda: NOW,
        )
    assert store.marked_ready is False


def test_embedding_cli_defaults_to_plan_only() -> None:
    args = build_parser().parse_args(
        ["--canonical-dataset", "canonical-active-dataset.json"]
    )

    assert args.apply is False
    assert args.model == "gemini-embedding-001"
    assert args.dimension == 1536
    assert args.batch_size == 16
