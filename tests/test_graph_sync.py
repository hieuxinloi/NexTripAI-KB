from __future__ import annotations

from typing import Any

import pytest

from nextrip_graphrag.neo4j_store import Neo4jGraphStore


class RecordingStore(Neo4jGraphStore):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


def test_sync_place_ids_deletes_nodes_outside_verified_snapshot() -> None:
    store = RecordingStore()

    store.sync_place_ids(["place-1", "place-2"])

    query, params = store.calls[0]
    assert "DETACH DELETE p" in query
    assert params["place_ids"] == ["place-1", "place-2"]


def test_sync_place_ids_rejects_empty_snapshot() -> None:
    store = RecordingStore()

    with pytest.raises(ValueError, match="empty place snapshot"):
        store.sync_place_ids([])


def test_relationship_refresh_preserves_evidence_links() -> None:
    store = RecordingStore()

    store._clear_managed_place_relationships("place-1")

    outgoing_types = store.calls[0][1]["relationship_types"]
    incoming_types = store.calls[1][1]["relationship_types"]
    assert "MENTIONS" not in outgoing_types
    assert "SOURCE_FOR" not in incoming_types
    assert "HAS_CATEGORY" in outgoing_types
    assert "HAS_PLACE" in incoming_types
