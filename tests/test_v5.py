from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nextrip_graphrag.config import Settings
from nextrip_graphrag.versions.v2.retrieval import _entity
from nextrip_graphrag.versions.v2.schemas import EntityResult
from nextrip_graphrag.versions.v5.concept_linker import ConceptLinker
from nextrip_graphrag.versions.v5.geo import (
    extract_geo_area_candidates,
    verified_address_area_vocabulary,
)
from nextrip_graphrag.versions.v5.graph_store import V5GraphStore, _concept_semantic_text
from nextrip_graphrag.versions.v5.query_planner import plan_query
from nextrip_graphrag.versions.v5.resolver import V5EntityResolver
from nextrip_graphrag.versions.v5.retrieval import V5RetrievalService
from nextrip_graphrag.versions.v5.schemas import (
    QueryTarget,
    TargetKind,
    TargetResult,
    V5Intent,
    V5QueryPlan,
)


CATALOG = {
    "cities": ["Đà Nẵng", "Quy Nhơn"],
    "areas": ["Nhơn Lý", "Tuy Phước"],
    "concepts": ["hải sản", "gia đình", "quiet"],
    "places": ["Eo Gió", "Kỳ Co", "Cafe"],
}


class FakeGemini:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def generate_structured(self, system_instruction, prompt, response_schema):
        return response_schema.model_validate(self.payload)

    def embed_query(self, query):
        return [0.1, 0.2]


class FailingGemini:
    def generate_structured(self, system_instruction, prompt, response_schema):
        raise RuntimeError("planner unavailable")


class SequencedGemini:
    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = payloads
        self.calls = 0

    def generate_structured(self, system_instruction, prompt, response_schema):
        payload = self.payloads[self.calls]
        self.calls += 1
        return response_schema.model_validate(payload)


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


class DishPlaceStore:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if "MATCH (node:Dish" in query:
            return [{
                "id": "concept:dish:banh-trang",
                "name": "bánh tráng",
                "description": None,
                "evidence_ids": ["unit:dish:banh-trang"],
                "score": 1.0,
            }]
        if "UNWIND $targets AS target" in query:
            return [{
                "place": {
                    "id": "rest_qn_001",
                    "name": "Quán Anh Nhật Gia Viên",
                    "city": "Quy Nhơn",
                    "entity_type": "restaurant",
                    "category": "Ẩm thực",
                    "address": "1087 Trần Hưng Đạo, Quy Nhơn",
                    "matched_targets": ["bánh tráng"],
                    "support_count": 2,
                    "score": 1.0,
                }
            }]
        return []


class FakeConceptStore:
    def __init__(self, candidates: list[dict[str, Any]], settings: Settings | None = None):
        self.candidates = candidates
        self.settings = settings if settings is not None else Settings()

    def semantic_concept_candidates(self, embedding, limit):
        return self.candidates[:limit]


class FakeConceptClient:
    def __init__(self, selected_id: str | None = None, confidence: float = 0.0):
        self.selected_id = selected_id
        self.confidence = confidence

    def embed_query(self, term):
        return [0.1, 0.2]

    def generate_structured(self, system_instruction, prompt, response_schema):
        return response_schema(
            selected_concept_id=self.selected_id,
            confidence=self.confidence,
        )


def concept_candidate(
    concept_id: str,
    canonical_name: str,
    score: float,
    concept_type: str = "Ambience",
) -> dict[str, Any]:
    return {
        "concept_id": concept_id,
        "canonical_name": canonical_name,
        "name": canonical_name.title(),
        "concept_type": concept_type,
        "domain": "experience",
        "score": score,
    }


def test_place_projection_preserves_travel_details_for_answer_generation() -> None:
    entity = _entity({
        "id": "rest_qn_001",
        "name": "Quán Anh Nhật Gia Viên",
        "city": "Quy Nhơn",
        "entity_type": "restaurant",
        "address": "1087 Trần Hưng Đạo, Quy Nhơn",
        "rating": 4.5,
        "opening_hours": "06:00-22:00",
    })

    assert entity.attributes == {
        "address": "1087 Trần Hưng Đạo, Quy Nhơn",
        "opening_hours": "06:00-22:00",
        "rating": 4.5,
    }


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


def test_v5_geo_extraction_learns_verified_address_components() -> None:
    places = [
        {
            "id": "attr_qn_031",
            "city_id": "city_quy_nhon",
            "props": {
                "city": "Quy Nhơn",
                "address": "An Hoà, Tuy Phước Bắc, Gia Lai, Việt Nam",
            },
        },
        {
            "id": "attr_qn_012",
            "city_id": "city_quy_nhon",
            "props": {
                "city": "Quy Nhơn",
                "address": "Tuy Phước Bắc, Gia Lai, Việt Nam",
            },
        },
        {
            "id": "cafe_qn_008",
            "city_id": "city_quy_nhon",
            "props": {
                "city": "Quy Nhơn",
                "address": "Xuân Diệu, Quy Nhơn, Gia Lai, Việt Nam",
            },
        },
    ]

    vocabulary = verified_address_area_vocabulary(places)
    chua_candidates = extract_geo_area_candidates(places[0], vocabulary)
    tower_candidates = extract_geo_area_candidates(places[1], vocabulary)
    street_candidates = extract_geo_area_candidates(places[2], vocabulary)

    assert "tuy-phuoc-bac" in vocabulary
    assert any(
        item.name == "Tuy Phước Bắc" and item.relation == "located_in"
        for item in chua_candidates
    )
    assert any(
        item.name == "Tuy Phước Bắc" and item.relation == "located_in"
        for item in tower_candidates
    )
    assert not any(item.name == "Xuân Diệu" for item in street_candidates)


def test_v5_verified_dataset_has_reviewed_geo_coverage() -> None:
    places = [
        json.loads(line)
        for line in Path("processed_verified/places.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    vocabulary = verified_address_area_vocabulary(places)
    located = {
        place["id"]: {
            candidate.name
            for candidate in extract_geo_area_candidates(place, vocabulary)
            if candidate.relation == "located_in"
        }
        for place in places
    }

    assert sum(bool(areas) for areas in located.values()) >= 260
    assert "Tuy Phước Bắc" in located["attr_qn_031"]
    assert "Tuy Phước Bắc" in located["attr_qn_012"]
    assert "Xuân Diệu" not in located["cafe_qn_008"]


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


def test_v5_planner_repairs_omitted_geo_area_from_graph_catalog() -> None:
    plan, planner, failure = plan_query(
        "Nhơn Lý có nơi nào để đi không?",
        FakeGemini({
            "intent": "list",
            "confidence": 0.95,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.geo_scope.areas == ["Nhơn Lý"]
    assert plan.targets == [QueryTarget(kind=TargetKind.GEO_AREA, value="Nhơn Lý")]


def test_v5_planner_repairs_city_trip_target_to_attractions() -> None:
    plan, planner, failure = plan_query(
        "Lên lịch trình một ngày khám phá Quy Nhơn",
        FakeGemini({
            "intent": "plan_candidates",
            "targets": [{"kind": "city", "value": "Quy Nhơn"}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "duration_days": 1,
            "confidence": 1.0,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.geo_scope.cities == ["Quy Nhơn"]
    assert plan.targets == [
        QueryTarget(kind=TargetKind.PLACE, entity_types=["attraction"])
    ]


def test_v5_planner_repairs_missing_restaurant_entity_type() -> None:
    plan, planner, failure = plan_query(
        "Gợi ý nhà hàng ở Đà Nẵng",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "place"}],
            "geo_scope": {"cities": ["Đà Nẵng"]},
            "confidence": 0.9,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.targets == [
        QueryTarget(kind=TargetKind.PLACE, entity_types=["restaurant"])
    ]


def test_v5_planner_treats_ngon_as_a_soft_preference() -> None:
    plan, planner, failure = plan_query(
        "Ở Quy Nhơn có món gì ngon?",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "dish"}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "required_concepts": ["ngon"],
            "confidence": 0.9,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.required_concepts == []
    assert plan.preferred_concepts == ["ngon"]


def test_v5_planner_repairs_food_discovery_misclassified_as_attractions() -> None:
    plan, planner, failure = plan_query(
        "Ở Quy Nhơn có món gì ngon?",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["attraction"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "required_concepts": ["hải sản"],
            "confidence": 0.9,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.intent == V5Intent.LIST
    assert plan.targets == [QueryTarget(kind=TargetKind.DISH)]
    assert plan.required_concepts == []
    assert plan.preferred_concepts == ["ngon"]


def test_v5_planner_does_not_clarify_an_actionable_named_dish_query() -> None:
    plan, planner, failure = plan_query(
        "Cho tôi địa chỉ quán bán hải sản ở Quy Nhơn",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "dish", "value": "hải sản"}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "requested_fields": ["address"],
            "clarification_needed": True,
            "confidence": 0.8,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.clarification_needed is False


def test_v5_planner_expands_contextual_dish_list_into_named_targets() -> None:
    catalog = CATALOG | {
        "concepts": [*CATALOG["concepts"], "bánh tráng", "bún cá"],
    }
    plan, planner, failure = plan_query(
        "Cho tôi địa chỉ quán bán các món này ở Quy Nhơn.\n"
        "Các món được nhắc đến ở lượt trước: bánh tráng, bún cá.",
        FakeGemini({
            "intent": "list",
            "targets": [{"kind": "place", "entity_types": ["restaurant"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "required_concepts": ["bánh tráng", "bún cá"],
            "confidence": 0.9,
        }),
        catalog,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.intent == V5Intent.RECOMMEND
    assert plan.targets == [
        QueryTarget(kind=TargetKind.DISH, value="bánh tráng"),
        QueryTarget(kind=TargetKind.DISH, value="bún cá"),
    ]
    assert plan.required_concepts == []


def test_v5_planner_retries_invalid_known_city_plan_with_gemini() -> None:
    gemini = SequencedGemini([
        {
            "intent": "plan_candidates",
            "targets": [{"kind": "activity", "value": "khám phá"}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "duration_days": 1,
            "confidence": 0.9,
        },
        {
            "intent": "plan_candidates",
            "targets": [{"kind": "place", "entity_types": ["attraction"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "duration_days": 1,
            "confidence": 0.95,
        },
    ])

    plan, planner, failure = plan_query(
        "Lên lịch trình một ngày khám phá Quy Nhơn",
        gemini,
        CATALOG,
    )

    assert gemini.calls == 2
    assert planner == "gemini_repair"
    assert failure is None
    assert plan.targets == [
        QueryTarget(kind=TargetKind.PLACE, entity_types=["attraction"])
    ]


def test_v5_planner_returns_unavailable_for_geo_query_when_gemini_fails() -> None:
    plan, planner, failure = plan_query(
        "Nhơn Lý có nơi nào để đi không?",
        FailingGemini(),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert failure.code == "planner_unavailable"
    assert failure.retryable is True
    assert plan.intent == V5Intent.UNSUPPORTED
    assert plan.targets == []


def test_v5_planner_returns_unavailable_for_named_place_when_gemini_fails() -> None:
    plan, planner, failure = plan_query(
        "Mô tả thêm về Kỳ Co Quy Nhơn",
        FailingGemini(),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert failure.code == "planner_unavailable"
    assert failure.retryable is True
    assert plan.intent == V5Intent.UNSUPPORTED
    assert plan.targets == []


def test_v5_planner_returns_unavailable_for_city_trip_when_gemini_fails() -> None:
    city = CATALOG["cities"][1]
    plan, planner, failure = plan_query(
        f"Goi y mot chuyen di tai {city}, uu tien canh dep va trai nghiem",
        FailingGemini(),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert failure.code == "planner_unavailable"
    assert failure.retryable is True
    assert plan.intent == V5Intent.UNSUPPORTED
    assert plan.geo_scope.cities == []
    assert plan.targets == []


def test_v5_planner_does_not_use_city_fallback_for_dynamic_queries() -> None:
    city = CATALOG["cities"][1]
    plan, planner, failure = plan_query(
        f"Thoi tiet {city} hom nay?",
        FailingGemini(),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert plan.intent == V5Intent.UNSUPPORTED


def test_v5_planner_does_not_fallback_to_generic_one_word_place() -> None:
    plan, planner, failure = plan_query(
        "Gợi ý cafe",
        FailingGemini(),
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert plan.intent == V5Intent.UNSUPPORTED


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


def test_v5_planner_preserves_semantic_concept_for_linking() -> None:
    plan, planner, failure = plan_query(
        "Gợi ý nơi nghỉ ngơi ở Quy Nhơn",
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["hotel"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "required_concepts": ["nghỉ ngơi"],
            "confidence": 0.9,
        }),
        CATALOG,
    )

    assert planner == "gemini"
    assert failure is None
    assert plan.required_concepts == ["nghỉ ngơi"]


def test_v5_concept_linker_maps_semantic_synonym_without_hardcoded_rule() -> None:
    store = FakeConceptStore([
        concept_candidate("concept:activity:ngu", "ngủ", 0.88, "Activity"),
        concept_candidate("concept:ambience:yen-tinh", "yên tĩnh", 0.70),
    ])

    result = ConceptLinker(store, FakeConceptClient()).link(
        ["nghỉ ngơi"],
        ["ngủ", "yên tĩnh"],
    )

    assert result.resolved == ["ngủ"]
    assert result.links[0].method == "semantic_embedding"


def test_v5_concept_linker_rejects_ambiguous_semantic_candidates() -> None:
    store = FakeConceptStore([
        concept_candidate("concept:activity:ngu", "ngủ", 0.82, "Activity"),
        concept_candidate("concept:ambience:yen-tinh", "yên tĩnh", 0.81),
    ])

    result = ConceptLinker(store, FakeConceptClient()).link(
        ["nghỉ ngơi"],
        ["ngủ", "yên tĩnh"],
    )

    assert result.resolved == []
    assert result.unresolved == ["nghỉ ngơi"]


def test_v5_concept_linker_lets_llm_choose_only_from_semantic_candidates() -> None:
    store = FakeConceptStore([
        concept_candidate("concept:ambience:yen-tinh", "yên tĩnh", 0.83),
        concept_candidate("concept:ambience:chill", "chill", 0.82),
    ])
    client = FakeConceptClient("concept:ambience:yen-tinh", confidence=0.9)

    result = ConceptLinker(store, client).link(
        ["nghỉ ngơi"],
        ["yên tĩnh", "chill"],
        user_query="Tìm khách sạn để nghỉ ngơi",
    )

    assert result.resolved == ["yên tĩnh"]
    assert result.links[0].method == "llm_candidate_selection"
    assert result.links[0].selector_confidence == 0.9


def test_v5_concept_embedding_text_includes_grounded_evidence_samples() -> None:
    text = _concept_semantic_text({
        "name": "Yên tĩnh",
        "canonical_name": "quiet",
        "concept_type": "Ambience",
        "domain": "experience",
        "evidence_samples": ["Không gian yên tĩnh phù hợp để nghỉ ngơi."],
    })

    assert "Yên tĩnh" in text
    assert "nghỉ ngơi" in text


def test_v5_near_area_edges_keep_proximity_distinct_from_location() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    store = V5GraphStore.__new__(V5GraphStore)
    store.run_versioned = lambda query, **params: calls.append((query, params))

    store._load_near_area_edges()

    build_query, params = calls[1]
    assert "[:LOCATED_IN]" in build_query
    assert "point.distance(candidate.location, anchor.location)" in build_query
    assert "MERGE (candidate)-[nearArea:NEAR_AREA]->(area)" in build_query
    assert "LOCATED_IN" not in build_query.split("MERGE", 1)[1]
    assert params["radius_km"] == 5.0


def test_v5_geo_scope_paths_expose_scope_semantics() -> None:
    class Store:
        def run_versioned(self, query, **params):
            assert "NEAR_AREA" in query
            assert params["area_ids"] == ["geo-area:nhon-ly"]
            return [{
                "place_id": "attr_qn_033",
                "area_name": "Nhơn Lý",
                "relationship": "NEAR_AREA",
                "score": 0.7,
            }]

    service = V5RetrievalService.__new__(V5RetrievalService)
    service.store = Store()
    paths = service._geo_scope_paths(
        [EntityResult(
            place_id="attr_qn_033",
            name="Kỳ Co",
            city="Quy Nhơn",
            entity_type="attraction",
        )],
        [TargetResult(
            target_id="geo-area:nhon-ly",
            kind=TargetKind.GEO_AREA,
            name="Nhơn Lý",
        )],
    )

    assert paths[0].relationships == ["NEAR_AREA"]
    assert paths[0].score == 0.7


def test_v5_resolver_guards_target_label() -> None:
    result = V5EntityResolver(FakeResolverStore()).resolve(
        QueryTarget(kind=TargetKind.GEO_AREA, value="Nhơn Lý")
    )

    assert result[0].kind == TargetKind.GEO_AREA
    assert result[0].name == "Nhơn Lý"


def test_v5_place_resolver_removes_entity_type_prefix_before_contains_lookup() -> None:
    class PrefixResolverStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if " CONTAINS " not in query:
                return []
            return [{
                "id": "hotel_dn_040",
                "name": "Hilton Da Nang:",
                "description": None,
                "evidence_ids": [],
                "score": 0.7,
            }]

    store = PrefixResolverStore()
    result = V5EntityResolver(store).resolve(
        QueryTarget(
            kind=TargetKind.PLACE,
            value="Khách sạn Hilton Da Nang",
            entity_types=["hotel"],
        ),
        cities=["Đà Nẵng"],
    )

    assert result[0].target_id == "hotel_dn_040"
    assert store.calls[0][1]["values"] == [
        "Hilton Da Nang",
        "Khách sạn Hilton Da Nang",
    ]
    assert not any("db.index.fulltext.queryNodes" in query for query, _ in store.calls)


def test_v5_place_resolver_falls_back_to_safe_fuzzy_fulltext_lookup() -> None:
    class FulltextResolverStore:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
            self.calls.append((query, params))
            if "db.index.fulltext.queryNodes" not in query:
                return []
            return [
                {
                    "id": "hotel_dn_040",
                    "name": "Hilton Da Nang",
                    "aliases": [],
                    "description": None,
                    "evidence_ids": [],
                    "score": 4.2,
                },
                {
                    "id": "hotel_dn_042",
                    "name": "Belle Maison Parosand Da Nang",
                    "aliases": [],
                    "description": None,
                    "evidence_ids": [],
                    "score": 4.1,
                },
            ]

    store = FulltextResolverStore()
    result = V5EntityResolver(store).resolve(
        QueryTarget(
            kind=TargetKind.PLACE,
            value="Khách sạn Hiltn Da Nang:",
            entity_types=["hotel"],
        ),
        cities=["Đà Nẵng"],
    )

    assert [item.target_id for item in result] == ["hotel_dn_040"]
    fulltext_calls = [
        params
        for query, params in store.calls
        if "db.index.fulltext.queryNodes" in query
    ]
    assert fulltext_calls[0]["query_text"] == "Hiltn Da Nang"
    assert fulltext_calls[0]["entity_types"] == ["hotel"]
    assert fulltext_calls[0]["cities"] == ["Đà Nẵng"]


def test_v5_dish_retrieval_is_scoped_to_city_and_restaurants() -> None:
    store = CapturingConceptStore()

    V5EntityResolver(store).resolve(
        QueryTarget(kind=TargetKind.DISH),
        limit=5,
        cities=["Đà Nẵng"],
    )

    assert store.params["cities"] == ["Đà Nẵng"]
    assert store.params["target_kind"] == "dish"


def test_v5_named_dish_recommendation_returns_restaurants_with_addresses() -> None:
    store = DishPlaceStore()
    service = V5RetrievalService(store)
    plan = V5QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[QueryTarget(kind=TargetKind.DISH, value="bánh tráng")],
        geo_scope={"cities": ["Quy Nhơn"]},
        requested_fields=["address"],
        confidence=1.0,
    )

    outcome = service._execute_plan(
        plan,
        "Cho tôi các điểm bán bánh tráng ở Quy Nhơn",
        5,
    )

    assert outcome.retrieval_strategy == "concept_place_path"
    assert outcome.recommendations[0].place_id == "rest_qn_001"
    assert outcome.recommendations[0].attributes["address"].startswith("1087")
    assert outcome.recommendations[0].attributes["matched_targets"] == ["bánh tráng"]


def test_v5_unnamed_dish_request_keeps_dish_results_instead_of_venues() -> None:
    store = DishPlaceStore()
    service = V5RetrievalService(store)
    plan = V5QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[QueryTarget(kind=TargetKind.DISH)],
        geo_scope={"cities": ["Quy Nhơn"]},
        preferred_concepts=["ngon"],
        confidence=1.0,
    )

    outcome = service._execute_plan(plan, "Ở Quy Nhơn có món gì ngon?", 5)

    assert outcome.targets[0].kind == TargetKind.DISH
    assert outcome.recommendations == []
    assert not any("UNWIND $targets AS target" in query for query, _ in store.calls)


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


def test_v5_unresolved_preference_does_not_block_semantic_place_fallback() -> None:
    class Store:
        settings = Settings()

        def planner_catalog(self):
            return CATALOG

        def semantic_concept_candidates(self, embedding, limit):
            return []

    class Service(V5RetrievalService):
        def _ensure_ready(self):
            return None

        def _path_candidates(self, *args, **kwargs):
            return [], []

        def _query_embedding(self, query):
            return [0.1, 0.2]

        def _semantic_place_candidates(
            self,
            embedding,
            city,
            entity_types,
            limit,
            place_ids=None,
        ):
            assert city == "Quy Nhơn"
            assert entity_types == ["hotel"]
            return [EntityResult(
                place_id="hotel_qn_001",
                name="Khách sạn Hải Âu",
                city="Quy Nhơn",
                entity_type="hotel",
                score=0.8,
            )]

        def _matched_paths(self, candidates, concepts):
            return []

        def _place_evidence(self, places):
            return []

        def _claim_evidence(self, candidates, concepts):
            return []

    service = Service(
        Store(),
        FakeGemini({
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["hotel"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "preferred_concepts": ["nghỉ ngơi"],
            "confidence": 0.95,
        }),
    )

    response = service.query("Khách sạn yên tĩnh để nghỉ ngơi", 5)

    assert response.recommendations[0].place_id == "hotel_qn_001"
    assert response.missing_fields == []
    assert response.trace[1]["status"] == "partial"
    assert response.trace[-2]["retrieval_strategy"] == "semantic_place_fallback"
