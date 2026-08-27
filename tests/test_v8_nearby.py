from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from nextrip_graphrag.api.dependencies import get_kb_services
from nextrip_graphrag.api.router import router
from nextrip_graphrag.versions.v8.graph_store import (
    NearbyAnchorLocationMissingError,
    NearbyAnchorNotFoundError,
    V8GraphStore,
)


class _ScriptedV8Store(V8GraphStore):
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return self.responses.pop(0)


def _candidate_row() -> dict[str, Any]:
    return {
        "place_id": "cafe_qn_007",
        "name": "Cafe Gần Đây",
        "city": "Quy Nhơn",
        "entity_type": "cafe",
        "category": "coffee",
        "address": "1 Xuân Diệu",
        "distance_km": 1.23456,
        "latitude": 13.77,
        "longitude": 109.22,
        "rating": 4.6,
        "review_count": 120,
        "description": "Quán cà phê gần biển",
        "duration_recommendation": "60 phút",
        "opening_hours_open": "07:00",
        "opening_hours_close": "22:00",
        "opening_hours_note": None,
        "price_level": 2,
        "price_display": "25.000–55.000 VND",
        "price_range_min": 25_000,
        "price_range_max": 55_000,
        "ticket_price_child": "free",
        "price_currency": "VND",
        "price_note": "Giá tham khảo",
        "source_name": "google-maps-web",
        "source_url": "https://maps.google.com/example",
    }


def test_nearby_candidates_uses_point_distance_and_whitelists_output() -> None:
    store = _ScriptedV8Store([[{"has_location": True}], [_candidate_row()]])

    items = store.nearby_candidates(
        anchor_place_id="attr_qn_001",
        entity_types=["cafe", "cafe"],
        city="Quy Nhơn",
        radius_km=3.0,
        excluded_place_ids=["rest_qn_001", "rest_qn_001"],
        limit=5,
    )

    assert len(items) == 1
    assert items[0]["distance_km"] == 1.235
    assert items[0]["coordinates"] == {
        "latitude": 13.77,
        "longitude": 109.22,
    }
    assert items[0]["prices"]["price_range_min"] == 25_000
    assert items[0]["prices"]["display_text"] == "25.000–55.000 VND"
    assert items[0]["prices"]["ticket_price_child"] == "free"
    assert items[0]["source"]["url"].startswith("https://")
    assert "data_json" not in items[0]
    spatial_query, params = store.calls[1]
    assert "point.distance(anchor.location, candidate.location)" in spatial_query
    assert "[:NEAR" not in spatial_query
    assert params["entity_types"] == ["cafe"]
    assert params["excluded_place_ids"] == [
        "attr_qn_001",
        "rest_qn_001",
    ]
    assert params["radius_m"] == 3000.0


def test_nearby_candidates_rejects_missing_or_unlocated_anchor() -> None:
    missing = _ScriptedV8Store([[]])
    with pytest.raises(NearbyAnchorNotFoundError):
        missing.nearby_candidates(
            anchor_place_id="missing",
            entity_types=[],
            city=None,
            radius_km=5,
            excluded_place_ids=[],
            limit=5,
        )

    unlocated = _ScriptedV8Store([[{"has_location": False}]])
    with pytest.raises(NearbyAnchorLocationMissingError):
        unlocated.nearby_candidates(
            anchor_place_id="attr_qn_001",
            entity_types=[],
            city=None,
            radius_km=5,
            excluded_place_ids=[],
            limit=5,
        )


class _FakeServices:
    def __init__(self, store: Any) -> None:
        self.store = store

    def store_for(self, version: str) -> Any:
        assert version == "v8"
        return self.store


class _FakeNearbyStore:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.params: dict[str, Any] | None = None

    def nearby_candidates(self, **params: Any) -> list[dict[str, Any]]:
        self.params = params
        if self.error is not None:
            raise self.error
        return [
            {
                "place_id": "cafe_qn_007",
                "name": "Cafe Gần Đây",
                "city": "Quy Nhơn",
                "entity_type": "cafe",
                "category": "coffee",
                "address": "1 Xuân Diệu",
                "distance_km": 1.235,
                "coordinates": {"latitude": 13.77, "longitude": 109.22},
                "prices": {
                    "currency": "VND",
                    "price_range_min": 25_000,
                    "price_range_max": 55_000,
                    "ticket_price_child": "free",
                },
                "source": {
                    "name": "google-maps-web",
                    "url": "https://maps.google.com/example",
                },
                "attributes": {"rating": 4.6, "review_count": 120},
            }
        ]


def _client(store: Any) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_kb_services] = lambda: _FakeServices(store)
    return TestClient(app)


def test_nearby_endpoint_returns_typed_v8_candidates() -> None:
    store = _FakeNearbyStore()
    with _client(store) as client:
        response = client.post(
            "/api/kb/nearby",
            json={
                "anchor_place_id": "attr_qn_001",
                "entity_types": ["cafe", "cafe"],
                "city": "quy nhon",
                "radius_km": 3,
                "excluded_place_ids": ["rest_qn_001"],
                "limit": 5,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kb_version"] == "v8"
    assert payload["items"][0]["distance_km"] == 1.235
    assert payload["items"][0]["prices"]["currency"] == "VND"
    assert payload["items"][0]["prices"]["ticket_price_child"] == "free"
    assert store.params == {
        "anchor_place_id": "attr_qn_001",
        "entity_types": ["cafe"],
        "city": "Quy Nhơn",
        "radius_km": 3.0,
        "excluded_place_ids": ["rest_qn_001"],
        "limit": 5,
    }


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            NearbyAnchorNotFoundError("missing"),
            404,
            "nearby_anchor_not_found",
        ),
        (
            NearbyAnchorLocationMissingError("attr_qn_001"),
            422,
            "nearby_anchor_location_missing",
        ),
    ],
)
def test_nearby_endpoint_returns_typed_anchor_errors(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    with _client(_FakeNearbyStore(error=error)) as client:
        response = client.post(
            "/api/kb/nearby",
            json={"anchor_place_id": "attr_qn_001"},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize(
    "body",
    [
        {"anchor_place_id": "attr_qn_001", "kb_version": "v7"},
        {"anchor_place_id": "attr_qn_001", "radius_km": 50.1},
        {"anchor_place_id": "attr_qn_001", "limit": 21},
        {"anchor_place_id": "attr_qn_001", "entity_types": ["airport"]},
    ],
)
def test_nearby_endpoint_validates_bounded_v8_contract(body: dict[str, Any]) -> None:
    with _client(_FakeNearbyStore()) as client:
        response = client.post("/api/kb/nearby", json=body)

    assert response.status_code == 422
