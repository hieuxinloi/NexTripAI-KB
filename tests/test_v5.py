from __future__ import annotations

from typing import Any

from nextrip_graphrag.versions.v5.geo import extract_geo_area_candidates
from nextrip_graphrag.versions.v5.query_planner import V5PlannerDraft, plan_query
from nextrip_graphrag.versions.v5.resolver import V5EntityResolver
from nextrip_graphrag.versions.v5.retrieval import V5RetrievalService
from nextrip_graphrag.versions.v5.schemas import (
    QueryTarget,
    TargetKind,
    V5Intent,
)


CATALOG = {
    "cities": ["Đà Nẵng", "Quy Nhơn"],
    "areas": ["Nhơn Lý", "Tuy Phước"],
    "concepts": ["hải sản", "gia đình"],
}


class FakeGemini:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def generate_structured(self, system_instruction, prompt, response_schema):
        return response_schema.model_validate(self.payload)


class FakeResolverStore:
    def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
        if "MATCH (node:GeoArea" not in query:
            return []
        return [{
            "id": "geo-area:city_quy_nhon:nhon-ly",
            "name": "Nhơn Lý",
            "description": None,
            "evidence_ids": ["text-unit:verified:attr_qn_007"],
            "score": 1.0,
        }]


class CapturingConceptStore:
    def __init__(self):
        self.params = None

    def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.params = params
        assert "place.entity_type = 'restaurant'" in query
        return []


def test_v5_geo_extraction_keeps_location_distinct_from_description_mention() -> None:
    place = {
        "id": "attr_qn_007",
        "city_id": "city_quy_nhon",
        "props": {
            "city": "Quy Nhơn",
            "address": "Xã Nhơn Lý, Thành phố Quy Nhơn",
            "description": "Địa điểm nằm gần huyện Tuy Phước.",
        },
    }

    candidates = extract_geo_area_candidates(place)

    assert any(item.name == "Nhơn Lý" and item.relation == "located_in" for item in candidates)
    assert any(item.name == "Tuy Phước" and item.relation == "mentions" for item in candidates)
    assert not any(item.name == "Tuy Phước" and item.relation == "located_in" for item in candidates)


def test_v5_geo_extraction_rejects_lowercase_prose_after_admin_prefix() -> None:
    place = {
        "id": "attr_qn_001",
        "city_id": "city_quy_nhon",
        "props": {
            "city": "Quy Nhơn",
            "description": "Địa điểm cách thành phố khoảng 10 km và gần biển.",
        },
    }

    assert extract_geo_area_candidates(place) == []


def test_v5_geo_extraction_trims_city_and_field_label_suffixes() -> None:
    place = {
        "id": "hotel_dn_001",
        "city_id": "city_da_nang",
        "props": {
            "city": "Đà Nẵng",
            "address": "Quận Hải Châu Đà Nẵng",
            "description": "Khách sạn ở quận Ngũ Hành Sơn Giờ mở cửa linh hoạt.",
        },
    }

    names = {item.name for item in extract_geo_area_candidates(place)}

    assert "Hải Châu" in names
    assert "Ngũ Hành Sơn" in names


def test_v5_geo_extraction_rejects_malformed_verified_district() -> None:
    place = {
        "id": "rest_qn_057",
        "city_id": "city_quy_nhon",
        "props": {
            "city": "Quy Nhơn",
            "district": "quy nhơn đông( cầu",
        },
    }

    assert extract_geo_area_candidates(place) == []


def test_v5_planner_compiles_typed_geo_area_summary() -> None:
    plan, planner, failure = plan_query(
        "Tuy Phước có gì đặc biệt?",
        FakeGemini({
            "intent": "summarize",
            "targets": [{"kind": "geo_area", "value": "Tuy Phước"}],
            "geo_scope": {"areas": ["Tuy Phước"]},
            "confidence": 0.95,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.targets[0].kind == TargetKind.GEO_AREA
    assert plan.targets[0].value == "Tuy Phước"


def test_v5_planner_rejects_vocabulary_outside_graph() -> None:
    plan, planner, failure = plan_query(
        "Gợi ý ở Huế",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["attraction"]}],
            "geo_scope": {"cities": ["Huế"]},
            "confidence": 0.8,
        }),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure.code == "invalid_plan"
    assert failure.retryable is False
    assert plan.intent == V5Intent.UNSUPPORTED


def test_v5_resolver_guards_target_label() -> None:
    result = V5EntityResolver(FakeResolverStore()).resolve(
        QueryTarget(kind=TargetKind.GEO_AREA, value="Nhơn Lý")
    )

    assert result[0].kind == TargetKind.GEO_AREA
    assert result[0].name == "Nhơn Lý"


def test_v5_dish_retrieval_is_scoped_to_city_and_restaurants() -> None:
    store = CapturingConceptStore()

    V5EntityResolver(store).resolve(
        QueryTarget(kind=TargetKind.DISH),
        limit=5,
        cities=["Đà Nẵng"],
    )

    assert store.params["cities"] == ["Đà Nẵng"]
    assert store.params["target_kind"] == "dish"


def test_v5_tool_required_does_not_query_graph() -> None:
    class ToolService(V5RetrievalService):
        def _ensure_ready(self):
            return None

    class Store:
        def planner_catalog(self):
            return CATALOG

    service = ToolService(
        Store(),
        FakeGemini({
            "intent": "tool_required",
            "required_tools": ["weather"],
            "confidence": 0.99,
        }),
    )

    response = service.query("Thời tiết Quy Nhơn hiện tại?", 5)

    assert response.intent == V5Intent.TOOL_REQUIRED
    assert response.required_tools == ["weather"]
    assert response.trace[0]["status"] == "ok"


def test_v5_planner_failure_is_retryable_not_plain_unsupported() -> None:
    class PlannerFailureService(V5RetrievalService):
        def _ensure_ready(self):
            return None

    class Store:
        def planner_catalog(self):
            return CATALOG

    response = PlannerFailureService(Store(), None).query("Tuy Phước có gì?", 5)

    assert response.error["code"] == "planner_unavailable"
    assert response.error["retryable"] is True
