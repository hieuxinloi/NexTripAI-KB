from __future__ import annotations

from typing import Any

import neo4j
import pytest
from neo4j import RoutingControl

from nextrip_graphrag.config import Settings
from nextrip_graphrag.neo4j_store import Neo4jGraphStore


class RecordingStore(Neo4jGraphStore):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


def test_driver_configures_pool_liveness_and_retry_policy(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeResult:
        records = []

    class FakeDriver:
        def execute_query(self, query: str, **options: Any) -> FakeResult:
            captured["query"] = query
            captured["query_options"] = options
            return FakeResult()

        def close(self) -> None:
            pass

    def fake_driver(uri: str, **options: Any) -> FakeDriver:
        captured["uri"] = uri
        captured["options"] = options
        return FakeDriver()

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", fake_driver)
    settings = Settings(
        neo4j_connection_timeout=4,
        neo4j_connection_acquisition_timeout=6,
        neo4j_liveness_check_timeout=20,
        neo4j_max_connection_lifetime=240,
        neo4j_max_transaction_retry_time=12,
    )

    store = Neo4jGraphStore(settings)

    assert captured["uri"] == settings.neo4j_uri
    assert captured["options"] == {
        "auth": (settings.neo4j_user, settings.neo4j_password),
        "connection_timeout": 4,
        "connection_acquisition_timeout": 6,
        "liveness_check_timeout": 20,
        "max_connection_lifetime": 240,
        "max_transaction_retry_time": 12,
        "keep_alive": True,
    }
    assert store.run_read("RETURN 1 AS ok") == []
    assert captured["query"] == "RETURN 1 AS ok"
    assert captured["query_options"]["routing_"] is RoutingControl.READ
    store.close()


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
