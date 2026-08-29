from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from neo4j.exceptions import SessionExpired

from nextrip_graphrag.api import router
from nextrip_graphrag.api.schemas import TypedQueryRequest
from nextrip_graphrag.versions.v2.schemas import EntityResult
from nextrip_graphrag.versions.v5.schemas import (
    GeoScope,
    QueryTarget,
    TargetKind,
    V5Intent,
    V5QueryPlan,
)
from nextrip_graphrag.versions.v6.context import (
    is_itinerary_request,
    resolve_turn,
    update_context,
)
from nextrip_graphrag.versions.v6.itinerary import ItineraryBuilder
from nextrip_graphrag.versions.v6.retrieval import _planner_query
from nextrip_graphrag.versions.v6.schemas import ConversationContext


CATALOG = {
    "cities": ["Đà Nẵng", "Quy Nhơn"],
    "areas": [],
    "concepts": [],
    "places": [],
}


class ItineraryStore:
    def run_versioned(self, query, **params):
        assert params["place_ids"] == ["attr-1", "rest-1", "cafe-1", "attr-2"]
        nearby = {
            "attr-1": [{"target_id": "cafe-1", "distance_km": 0.5}],
            "cafe-1": [{"target_id": "attr-1", "distance_km": 0.5}],
            "rest-1": [{"target_id": "attr-2", "distance_km": 0.4}],
            "attr-2": [{"target_id": "rest-1", "distance_km": 0.4}],
        }
        return [
            {
                "place_id": place_id,
                "opening_hours_open": "07:00",
                "opening_hours_close": "22:00",
                "duration_recommendation": "1-2 giờ",
                "distances": nearby[place_id],
            }
            for place_id in params["place_ids"]
        ]


def _place(place_id: str, entity_type: str) -> EntityResult:
    return EntityResult(
        place_id=place_id,
        name=place_id,
        city="Đà Nẵng",
        entity_type=entity_type,
    )


def test_v6_resolves_followup_from_explicit_context() -> None:
    context = ConversationContext(
        turn_count=1,
        cities=["Đà Nẵng"],
        duration_days=2,
        entity_types=["hotel"],
        previous_intent="recommend",
    )

    resolved = resolve_turn("Ưu tiên nơi có hồ bơi nhé.", context, CATALOG)

    assert "Đà Nẵng" in resolved.query
    assert "2 ngày" in resolved.query
    assert "khách sạn" in resolved.query
    assert resolved.updates == [
        "inherited_city",
        "inherited_duration",
        "inherited_entity_types",
    ]


def test_v6_does_not_treat_trip_duration_as_hotel_itinerary() -> None:
    assert is_itinerary_request(
        "Tìm khách sạn ở Đà Nẵng cho chuyến đi 2 ngày."
    ) is False
    assert is_itinerary_request(
        "Lên lịch trình 2 ngày ở Đà Nẵng."
    ) is True
    assert is_itinerary_request(
        "Khám phá ẩm thực 2 ngày, xen kẽ điểm tham quan."
    ) is True


def test_v6_removes_only_explicitly_negated_nightlife_types() -> None:
    query = "Lịch trình 2 ngày, không muốn đi bar hay club."

    planner_query = _planner_query(query)

    assert "bar" not in planner_query
    assert "club" not in planner_query
    assert _planner_query("Gợi ý bar ở Đà Nẵng") == "Gợi ý bar ở Đà Nẵng"
    assert _planner_query(
        "Surf bar thuộc loại hình nightlife nào?"
    ) == "Surf bar thuộc loại hình nightlife nào?"


def test_v6_increments_duration_without_losing_city() -> None:
    context = ConversationContext(
        turn_count=1,
        cities=["Quy Nhơn"],
        duration_days=2,
        previous_intent="plan_candidates",
    )

    resolved = resolve_turn(
        "Tôi vừa sắp xếp được thêm một ngày, cập nhật lại giúp tôi.",
        context,
        CATALOG,
    )

    assert "Quy Nhơn" in resolved.query
    assert "3 ngày" in resolved.query
    assert "incremented_duration" in resolved.updates


def test_v6_context_uses_typed_plan_contract() -> None:
    plan = V5QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[
            QueryTarget(kind=TargetKind.PLACE, entity_types=["cafe"])
        ],
        geo_scope=GeoScope(cities=["Đà Nẵng"]),
        confidence=0.9,
    )
    resolved = resolve_turn("Gợi ý cafe ở Đà Nẵng", ConversationContext(), CATALOG)

    context = update_context(
        ConversationContext(),
        resolved,
        plan,
        [_place("cafe-1", "cafe")],
    )

    assert context.turn_count == 1
    assert context.cities == ["Đà Nẵng"]
    assert context.entity_types == ["cafe"]
    assert context.previous_recommendations == ["cafe-1"]


def test_v6_api_request_validates_conversation_context() -> None:
    request = TypedQueryRequest.model_validate(
        {
            "query": "Ưu tiên hồ bơi",
            "kb_version": "v6",
            "conversation_context": {
                "turn_count": 1,
                "cities": ["Đà Nẵng"],
            },
        }
    )

    assert request.conversation_context == ConversationContext(
        turn_count=1,
        cities=["Đà Nẵng"],
    )


def test_v8_api_passes_conversation_context_to_stateful_service(monkeypatch) -> None:
    received = {}

    class StatefulService:
        def __init__(self, store, gemini):
            pass

        def query(self, query, top_k, *, context=None):
            received["context"] = context
            return SimpleNamespace(
                answer_type="recommendation",
                entities=[],
                recommendations=[],
                facts=[],
                trace=[{}],
            )

    monkeypatch.setattr(
        router,
        "version_retrieval_service_class",
        lambda version: StatefulService,
    )
    services = SimpleNamespace(
        settings=SimpleNamespace(configured_kb_versions=("v8",)),
        gemini=None,
        store_for=lambda version: object(),
    )
    context = ConversationContext(
        turn_count=1,
        cities=["Quy Nhơn"],
    )

    router.query_typed(
        TypedQueryRequest(
            query="Ưu tiên nơi yên tĩnh",
            kb_version="v8",
            conversation_context=context,
        ),
        services,
    )

    assert received["context"] == context


def test_typed_query_maps_exhausted_neo4j_retry_to_503(monkeypatch) -> None:
    class UnavailableService:
        def __init__(self, store, gemini):
            pass

        def query(self, query, top_k, *, context=None):
            raise SessionExpired("defunct pooled connection")

    monkeypatch.setattr(
        router,
        "version_retrieval_service_class",
        lambda version: UnavailableService,
    )
    services = SimpleNamespace(
        settings=SimpleNamespace(configured_kb_versions=("v8",)),
        gemini=None,
        store_for=lambda version: object(),
    )

    with pytest.raises(HTTPException) as exc_info:
        router.query_typed(
            TypedQueryRequest(
                query="Len lich trinh mot ngay o Quy Nhon",
                kb_version="v8",
            ),
            services,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "neo4j_temporarily_unavailable",
        "message": "Knowledge Base connection is temporarily unavailable.",
        "retryable": True,
    }


def test_v6_itinerary_is_grounded_and_bounded_per_day() -> None:
    recommendations = [
        _place("attr-1", "attraction"),
        _place("rest-1", "restaurant"),
        _place("cafe-1", "cafe"),
        _place("attr-2", "attraction"),
    ]

    itinerary = ItineraryBuilder(ItineraryStore()).build(
        recommendations,
        duration_days=2,
    )

    assert [day.day for day in itinerary] == [1, 2]
    assert all(day.slots for day in itinerary)
    assert all(len(day.slots) <= 3 for day in itinerary)
    scheduled = {
        slot.place_id
        for day in itinerary
        for slot in day.slots
    }
    assert scheduled == {item.place_id for item in recommendations}
    assert {
        slot.place_id for slot in itinerary[0].slots
    } == {"attr-1", "cafe-1"}
    assert {
        slot.place_id for slot in itinerary[1].slots
    } == {"rest-1", "attr-2"}
