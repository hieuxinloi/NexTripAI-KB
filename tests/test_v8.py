from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from nextrip_graphrag.versions.v5.schemas import (
    GeoScope,
    QueryTarget,
    TargetKind,
    V5Intent,
    V5QueryPlan,
)
from nextrip_graphrag.versions.v8.graph_store import V8GraphStore
from nextrip_graphrag.versions.v8.query_planner import V8PlannerDraft, plan_query
from nextrip_graphrag.versions.v8.retrieval import (
    V8RetrievalService,
    _effective_tasks,
    _public_missing_fields,
    _requires_city_scope,
)
from nextrip_graphrag.versions.v6.schemas import ConversationContext


CATALOG = {
    "cities": ["Đà Nẵng", "Quy Nhơn"],
    "areas": ["Hải Châu", "Nhơn Lý"],
    "concepts": ["families", "quiet", "sea view"],
    "places": ["Cầu Rồng", "Eo Gió"],
}


class FakePlanner:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def generate_structured(self, _instruction, _prompt, response_schema):
        return response_schema.model_validate(self.payload)


def test_v8_structured_schema_is_compatible_with_gemini_developer_api() -> None:
    schema = V8PlannerDraft.model_json_schema()

    def assert_no_open_objects(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert "additionalProperties" not in node or node["additionalProperties"] is False
            for value in node.values():
                assert_no_open_objects(value)
        elif isinstance(node, list):
            for value in node:
                assert_no_open_objects(value)

    assert_no_open_objects(schema)


def test_v8_normalizes_optional_invalid_constraints_without_dropping_plan() -> None:
    planner = FakePlanner(
        {
            "intent": "AGGREGATE",
            "targets": [{"kind": "place", "entity_types": ["restaurant"]}],
            "geo_scope": {"cities": ["Đà Nẵng"]},
            "constraints": [
                {"field": "not_a_graph_field", "value": "ignored"},
            ],
            "confidence": 0.9,
        }
    )

    plan, planner_name, failure = plan_query("đếm nhà hàng ở Đà Nẵng", planner, CATALOG)

    assert failure is None
    assert planner_name == "gemini_semantic_v8"
    assert plan.intent == V5Intent.AGGREGATE
    assert plan.geo_scope.cities == ["Đà Nẵng"]
    assert plan.constraints == []


def test_v8_uses_v6_stateful_executor_and_v8_response_contract() -> None:
    assert V8RetrievalService.api_kb_version == "v8"
    assert V8RetrievalService.graph_kb_version == "v8"
    assert V8GraphStore.kb_version == "v8"
    assert V8RetrievalService.response_model.model_fields["kb_version"].default == "v8"


def test_v8_projection_namespaces_graph_reference_properties() -> None:
    projected = V8GraphStore._namespace_reference_properties(
        {
            "id": "fact:hotel:price",
            "subject_id": "hotel-1",
            "anchor_place_ids": ["hotel-1", "beach-1"],
            "name": "verified fact",
        }
    )

    assert projected["id"] == "fact:hotel:price"
    assert projected["subject_id"] == "v8:hotel-1"
    assert projected["anchor_place_ids"] == ["v8:hotel-1", "v8:beach-1"]
    assert projected["name"] == "verified fact"


def test_v8_fallback_uses_current_search_clause_and_keeps_filters_safe() -> None:
    source = (
        Path(__file__).parents[1] / "nextrip_graphrag" / "versions" / "v8" / "retrieval.py"
    ).read_text(encoding="utf-8")

    assert "SEARCH place IN" in source
    assert "db.index.vector.queryNodes" not in source
    assert "WHERE place.kb_version = $kb_version" in source
    assert "v5_place_fulltext" not in source
    assert 'fulltext_index = "v8_place_fulltext"' in source
    assert 'vector_index = "v8_place_embedding"' in source


def test_v8_does_not_expose_grounding_diagnostics_as_missing_fields() -> None:
    assert _public_missing_fields(
        [
            "unresolved:place:trung tâm thành phố",
            "unresolved:geo_area:Phù Mỹ",
            "unresolved:city:thành phố biển",
        ]
    ) == ["near_reference", "geo_area", "city"]


def test_v8_compiles_independent_itinerary_tasks_and_numeric_constraints() -> None:
    planner = FakePlanner(
        {
            "intent": "plan_candidates",
            "targets": [{"kind": "place", "entity_types": ["hotel"]}],
            "geo_scope": {"cities": ["Quy NhÆ¡n"]},
            "tasks": [
                {
                    "name": "stay",
                    "targets": [{"kind": "place", "entity_types": ["hotel"]}],
                    "geo_scope": {"cities": ["Quy NhÆ¡n"]},
                    "constraints": [
                        {"field": "distance_to_beach_max", "value": 2},
                        {"field": "party_size", "value": 4},
                    ],
                }
            ],
            "duration_days": 2,
            "confidence": 0.95,
        }
    )

    plan, _, failure = plan_query("lá»‹ch trÃ¬nh", planner, CATALOG)

    assert failure is None
    assert plan.tasks[0].name == "stay"
    assert [item.field for item in plan.tasks[0].constraints] == [
        "distance_to_beach_max",
        "party_size",
    ]


def test_v8_expands_multi_type_itinerary_without_phrase_aliases() -> None:
    plan = V5QueryPlan(
        intent=V5Intent.PLAN_CANDIDATES,
        targets=[
            QueryTarget(
                kind=TargetKind.PLACE,
                entity_types=["attraction", "restaurant", "cafe"],
            )
        ],
        geo_scope=GeoScope(cities=["ÄÃ  Náºµng"]),
        confidence=1.0,
    )

    tasks = _effective_tasks(plan)

    assert [task.name for task in tasks] == [
        "attraction",
        "restaurant",
        "cafe",
    ]
    assert [task.targets[0].entity_types for task in tasks] == [
        ["attraction"],
        ["restaurant"],
        ["cafe"],
    ]
    assert all(len(task.targets) == 1 for task in tasks)


def test_v8_task_expansion_does_not_duplicate_separate_targets() -> None:
    plan = V5QueryPlan(
        intent=V5Intent.PLAN_CANDIDATES,
        targets=[
            QueryTarget(kind=TargetKind.PLACE, entity_types=[entity_type])
            for entity_type in ("hotel", "restaurant", "attraction")
        ],
        geo_scope=GeoScope(cities=["Quy NhÆ¡n"]),
        confidence=1.0,
    )

    tasks = _effective_tasks(plan)

    assert [len(task.targets) for task in tasks] == [1, 1, 1]


def test_v8_requires_city_only_for_unscoped_generic_place_requests() -> None:
    generic = V5QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[QueryTarget(kind=TargetKind.PLACE, entity_types=["cafe"])],
        confidence=1.0,
    )
    named = generic.model_copy(
        update={
            "intent": V5Intent.LOOKUP,
            "targets": [QueryTarget(kind=TargetKind.PLACE, value="Highlight Coffee")],
        }
    )

    assert _requires_city_scope(generic) is True
    assert _requires_city_scope(named) is False

    near_scoped = generic.model_copy(
        update={
            "geo_scope": GeoScope(near_entities=["Cầu Rồng"]),
        }
    )
    assert _requires_city_scope(near_scoped) is False


def test_v8_normalizes_structural_planner_mistakes() -> None:
    planner = FakePlanner(
        {
            "intent": "recommend",
            "targets": [
                {"kind": "place", "entity_types": ["restaurant"]},
                {"kind": "concept", "value": "đặc sản địa phương"},
            ],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "duration_days": 2,
            "confidence": 0.95,
        }
    )

    plan, _, failure = plan_query("lịch trình", planner, CATALOG)

    assert failure is None
    assert plan.intent == V5Intent.PLAN_CANDIDATES
    assert [target.kind for target in plan.targets] == [TargetKind.PLACE]
    assert plan.preferred_concepts == ["đặc sản địa phương"]


def test_v8_two_named_places_are_compiled_as_comparison() -> None:
    planner = FakePlanner(
        {
            "intent": "lookup",
            "targets": [
                {"kind": "place", "value": "Cầu Rồng"},
                {"kind": "place", "value": "Chợ Hàn"},
            ],
            "confidence": 1.0,
        }
    )

    plan, _, failure = plan_query("khoảng cách", planner, CATALOG)

    assert failure is None
    assert plan.intent == V5Intent.COMPARE


def test_v8_serializes_conversation_state_without_phrase_alias_matching() -> None:
    service = object.__new__(V8RetrievalService)
    resolved = service._resolve_turn(
        "ưu tiên yên tĩnh",
        ConversationContext(
            turn_count=1,
            cities=["Quy NhÆ¡n"],
            entity_types=["cafe"],
        ),
        CATALOG,
    )

    planner_input = json.loads(resolved.planner_query or "{}")
    assert resolved.query == "ưu tiên yên tĩnh"
    assert planner_input["current_message"] == resolved.query
    assert planner_input["conversation_context"]["cities"] == ["Quy NhÆ¡n"]
    assert planner_input["conversation_context"]["entity_types"] == ["cafe"]
