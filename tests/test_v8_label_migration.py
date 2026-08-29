from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nextrip_graphrag.versions.v8.label_migration import (
    LABEL_MAPPINGS,
    apply_generic_label_migration,
    inspect_generic_label_migration,
)


class _InspectionStore:
    def run(self, query: str, **_params: Any) -> list[dict[str, Any]]:
        query = query.strip()
        if "owned_nodes" in query:
            return [{"total_nodes": 10, "owned_nodes": 10}]
        if "count(node) AS count" in query:
            return [{"count": 10}]
        if "count(relationship) AS count" in query:
            return [{"count": 20}]
        if "UNWIND labels(node)" in query:
            return [
                {"label": "Place", "count": 2},
                {"label": "V8Place", "count": 2},
                {"label": "V8Observation", "count": 3},
                {"label": "V8Unexpected", "count": 1},
            ]
        if query.startswith("SHOW CONSTRAINTS"):
            return [
                {"name": "v8_canonical_place_id"},
                {"name": "v8_unexpected_constraint"},
                {"name": "unrelated_constraint"},
            ]
        if query.startswith("SHOW INDEXES"):
            return [
                {"name": "v8_place_fulltext"},
                {"name": "v8_unexpected_index"},
                {"name": "unrelated_index"},
            ]
        raise AssertionError(query)


def test_label_mapping_removes_version_prefix_without_collapsing_lifecycle() -> None:
    mappings = {item.legacy: item.replacements for item in LABEL_MAPPINGS}

    assert mappings["V8CanonicalPlace"] == ("CanonicalPlace",)
    assert mappings["V8Place"] == ("Place",)
    assert mappings["V8Fact"] == ("CanonicalFact",)
    assert mappings["V8Observation"] == ("Observation",)
    assert all(
        not replacement.startswith("V8")
        for item in LABEL_MAPPINGS
        for replacement in item.replacements
    )


def test_inspection_reports_only_owned_legacy_schema() -> None:
    report = inspect_generic_label_migration(_InspectionStore())

    assert report["nodes"] == 10
    assert report["relationships"] == 20
    assert report["owned_v8_nodes"] == 10
    assert report["legacy_label_counts"] == {
        "V8Place": 2,
        "V8Observation": 3,
    }
    assert report["unknown_legacy_label_counts"] == {"V8Unexpected": 1}
    assert report["replacement_label_counts"] == {"Place": 2}
    assert report["legacy_constraints"] == ["v8_canonical_place_id"]
    assert report["legacy_indexes"] == ["v8_place_fulltext"]
    assert report["unknown_legacy_constraints"] == ["v8_unexpected_constraint"]
    assert report["unknown_legacy_indexes"] == ["v8_unexpected_index"]


def test_apply_rejects_invalid_batch_size_before_touching_store() -> None:
    with pytest.raises(ValueError, match="batch_size must be positive"):
        apply_generic_label_migration(_InspectionStore(), batch_size=0)


def test_v8_runtime_cypher_no_longer_emits_prefixed_graph_labels() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_files = (
        root / "nextrip_graphrag/versions/v8/canonical_importer.py",
        root / "nextrip_graphrag/versions/v8/observation_publisher.py",
        root / "nextrip_graphrag/versions/v8/graph_store.py",
    )

    for path in runtime_files:
        assert ":V8" not in path.read_text(encoding="utf-8"), path
