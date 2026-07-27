from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace
from typing import Any

from neo4j_graphrag.types import RetrieverResultItem

from nextrip_graphrag.versions.v5.schemas import (
    GeoScope,
    QueryTarget,
    TargetKind,
    V5Intent,
    V5QueryPlan,
)
from nextrip_graphrag.versions.v8.graph_store import V8GraphStore
from nextrip_graphrag.versions.v8.query_planner import V8PlannerDraft, plan_query
from nextrip_graphrag.versions.v8.schemas import V8QueryPlan
from nextrip_graphrag.versions.v8.retrieval import (
    V8RetrievalService,
    _effective_tasks,
    _itinerary_candidate_plan,
    _public_missing_fields,
    _recover_itinerary_plan,
    _requires_city_scope,
    _restore_unresolved_named_targets,
    _scope_recovery_plan,
)
from nextrip_graphrag.versions.v5.retrieval import _RetrievalOutcome
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


def test_v8_rejects_invalid_constraints_in_the_structured_schema() -> None:
    planner = FakePlanner(
        {
            "intent": "aggregate",
            "targets": [{"kind": "place", "entity_types": ["restaurant"]}],
            "geo_scope": {"cities": ["Đà Nẵng"]},
            "constraints": [
                {"field": "not_a_graph_field", "value": "ignored"},
            ],
            "confidence": 0.9,
        }
    )

    plan, planner_name, failure = plan_query("đếm nhà hàng ở Đà Nẵng", planner, CATALOG)

    assert plan.intent == V5Intent.UNSUPPORTED
    assert planner_name == "planner_invalid"
    assert failure is not None
    assert failure.code == "invalid_plan"
    assert failure.retryable is False


def test_v8_drops_soft_planner_constraints_instead_of_rejecting_query() -> None:
    planner = FakePlanner(
        {
            "intent": "plan_candidates",
            "targets": [{"kind": "place", "entity_types": ["attraction"]}],
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "constraints": [
                {"field": "party_size", "value": 2, "mode": "soft"},
            ],
            "duration_days": 2,
            "confidence": 0.9,
        }
    )

    plan, planner_name, failure = plan_query(
        "Quy Nhơn 2 ngày 1 đêm",
        planner,
        CATALOG,
    )

    assert failure is None
    assert planner_name == "gemini_semantic_v8"
    assert plan.duration_days == 2
    assert plan.constraints == []


def test_v8_recovers_grounded_itinerary_shape_when_planner_is_invalid() -> None:
    plan = _recover_itinerary_plan(
        "Tôi ở Quy Nhơn 2 ngày 1 đêm, lên lộ trình giúp tôi",
        {"cities": ["Đà Nẵng", "Quy Nhơn"]},
    )

    assert plan.intent == V5Intent.PLAN_CANDIDATES
    assert plan.geo_scope.cities == ["Quy Nhơn"]
    assert plan.duration_days == 2
    assert plan.targets[0].entity_types == [
        "attraction",
        "restaurant",
        "cafe",
        "hotel",
    ]


def test_v8_uses_v6_stateful_executor_and_v8_response_contract() -> None:
    assert V8RetrievalService.api_kb_version == "v8"
    assert V8RetrievalService.graph_kb_version == "v8"
    assert V8GraphStore.kb_version == "v8"
    assert V8RetrievalService.response_model.model_fields["kb_version"].default == "v8"


def test_v8_entity_mentions_do_not_leak_into_the_v5_contract() -> None:
    assert "entity_mentions" not in V5QueryPlan.model_fields
    assert "entity_mentions" in V8QueryPlan.model_fields


def test_v8_projection_uses_server_side_dynamic_graph_copy() -> None:
    source = (
        Path(__file__).parents[1]
        / "nextrip_graphrag"
        / "versions"
        / "v8"
        / "graph_store.py"
    ).read_text(encoding="utf-8")

    assert "CREATE (copy:$(source_labels + [$entity_label])" in source
    assert "SET copy[key] = source[key]" in source
    assert "SET copy = properties(source)" not in source
    assert "CREATE (source_copy)-[copy:$(type(original))]->(target_copy)" in source
    assert "create_vector_index(" in source
    assert "create_fulltext_index(" in source
    assert "defaultdict" not in source
    assert "_primary_label" not in source


def test_v8_fallback_uses_official_hybrid_retriever_and_keeps_filters_safe() -> None:
    source = (
        Path(__file__).parents[1] / "nextrip_graphrag" / "versions" / "v8" / "retrieval.py"
    ).read_text(encoding="utf-8")

    assert "HybridCypherRetriever" in source
    assert "fuse_ranked_ids" not in source
    assert "WHERE node.kb_version = $kb_version" in source
    assert "v5_place_fulltext" not in source
    assert 'fulltext_index = "v8_place_fulltext"' in source
    assert 'vector_index = "v8_place_embedding"' in source


def test_v8_hybrid_retriever_adapts_sdk_results_to_entities() -> None:
    class FakeHybridRetriever:
        def __init__(self) -> None:
            self.call: dict[str, Any] | None = None

        def search(self, **kwargs: Any) -> SimpleNamespace:
            self.call = kwargs
            return SimpleNamespace(
                items=[
                    RetrieverResultItem(
                        content={
                            "id": "v8:cafe-1",
                            "name": "Cafe One",
                            "city": "Quy Nhơn",
                            "entity_type": "cafe",
                        },
                        metadata={"score": 0.8},
                    )
                ]
            )

    service = object.__new__(V8RetrievalService)
    retriever = FakeHybridRetriever()
    service.__dict__["_place_hybrid_retriever"] = retriever

    results = service._place_fallback_candidates(
        "cafe yên tĩnh",
        [0.1, 0.2],
        "Quy Nhơn",
        ["cafe"],
        5,
    )

    assert [item.place_id for item in results] == ["v8:cafe-1"]
    assert results[0].score == 0.8
    assert retriever.call is not None
    assert retriever.call["query_params"]["kb_version"] == "v8"


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
    assert tasks[0].limit == 20
    assert all(task.limit >= 6 for task in tasks[1:])


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


def test_v8_named_entity_span_overrides_a_split_generic_intent() -> None:
    planner = FakePlanner(
        {
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["cafe"]}],
            "requested_fields": ["highlights"],
            "entity_mentions": [
                {
                    "surface": "Highlight Coffee",
                    "role": "target",
                    "kind": "venue_or_brand",
                    "confidence": 0.98,
                }
            ],
            "clarification_needed": True,
            "confidence": 0.9,
        }
    )

    plan, _, failure = plan_query("Highlight Coffee o dau", planner, CATALOG)

    assert failure is None
    assert plan.intent == V5Intent.LOOKUP
    assert [target.value for target in plan.targets] == ["Highlight Coffee"]
    assert plan.entity_mentions[0].surface == "Highlight Coffee"
    assert plan.clarification_needed is False


def test_v8_restores_exact_catalog_place_when_planner_treats_it_as_scope() -> None:
    place = "Chợ đêm Sơn Trà"
    planner = FakePlanner(
        {
            "intent": "recommend",
            "geo_scope": {"cities": ["Quy Nhơn"]},
            "entity_mentions": [
                {
                    "surface": "Sơn Trà",
                    "role": "scope",
                    "kind": "geo_area",
                    "confidence": 0.95,
                }
            ],
            "confidence": 0.9,
        }
    )
    planner_input = json.dumps(
        {
            "current_message": f"{place} ở đâu?",
            "conversation_context": {"cities": ["Quy Nhơn"]},
        },
        ensure_ascii=False,
    )

    plan, _, failure = plan_query(
        planner_input,
        planner,
        {
            "cities": ["Đà Nẵng", "Quy Nhơn"],
            "areas": ["Sơn Trà"],
            "concepts": [],
            "places": [place],
        },
    )

    assert failure is None
    assert plan.intent == V5Intent.LOOKUP
    assert [target.value for target in plan.targets] == [place]
    assert plan.geo_scope.cities == []
    assert plan.clarification_needed is False


def test_v8_keeps_exact_catalog_place_as_nearby_anchor() -> None:
    place = "Chợ đêm Sơn Trà"
    planner = FakePlanner(
        {
            "intent": "recommend",
            "geo_scope": {"near_entities": [place]},
            "confidence": 0.9,
        }
    )

    plan, _, failure = plan_query(
        f"Gợi ý quán cà phê gần {place}",
        planner,
        {
            "cities": ["Đà Nẵng"],
            "areas": [],
            "concepts": [],
            "places": [place],
        },
    )

    assert failure is None
    assert plan.targets == []
    assert plan.geo_scope.near_entities == [place]


def test_v8_distinguishes_named_targets_from_nearby_anchors() -> None:
    assert _public_missing_fields(
        [
            "unresolved:place:Unknown Venue",
            "unresolved:place:Nearby Anchor",
        ],
        target_terms={"unknown-venue"},
        near_terms={"nearby-anchor"},
    ) == ["not_found:entity:Unknown Venue", "near_reference"]


def test_v8_retries_only_named_lookup_outside_stale_city_scope() -> None:
    named = V5QueryPlan(
        intent=V5Intent.LOOKUP,
        targets=[QueryTarget(kind=TargetKind.PLACE, value="Eo Gio")],
        geo_scope=GeoScope(cities=["Da Nang"]),
        confidence=1.0,
    )
    missing = _RetrievalOutcome(missing_fields=["not_found:place:Eo Gio"])

    retry = _scope_recovery_plan(named, missing)

    assert retry is not None
    assert retry.geo_scope.cities == []

    generic = named.model_copy(
        update={
            "intent": V5Intent.RECOMMEND,
            "targets": [QueryTarget(kind=TargetKind.PLACE, entity_types=["cafe"])],
        }
    )
    assert _scope_recovery_plan(generic, missing) is None


def test_v8_keeps_raw_named_target_in_a_not_found_response_plan() -> None:
    original = V5QueryPlan(
        intent=V5Intent.LOOKUP,
        targets=[QueryTarget(kind=TargetKind.PLACE, value="Unknown Coffee")],
        confidence=1.0,
    )
    grounded = original.model_copy(
        update={
            "targets": [QueryTarget(kind=TargetKind.PLACE)],
            "clarification_needed": True,
        }
    )

    restored = _restore_unresolved_named_targets(original, grounded)

    valid_plan = grounded.model_copy(
        update={"targets": restored, "clarification_needed": False}
    )
    assert valid_plan.targets[0].value == "Unknown Coffee"


def test_v8_itinerary_policy_replaces_activity_phrase_with_balanced_place_types() -> None:
    raw = V8QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[QueryTarget(kind=TargetKind.ACTIVITY, value="lo trinh di choi")],
        geo_scope=GeoScope(cities=["Quy Nhon"]),
        duration_days=2,
        confidence=0.95,
    )

    compiled = _itinerary_candidate_plan(raw)
    tasks = _effective_tasks(compiled)

    assert compiled.intent == V5Intent.PLAN_CANDIDATES
    assert compiled.targets[0].kind == TargetKind.PLACE
    assert set(compiled.targets[0].entity_types) == {
        "attraction",
        "restaurant",
        "cafe",
        "hotel",
    }
    assert {task.name for task in tasks} == {
        "attraction",
        "restaurant",
        "cafe",
        "hotel",
    }
    assert all(task.geo_scope.cities == ["Quy Nhon"] for task in tasks)


def test_v8_city_entity_mention_is_promoted_to_query_scope() -> None:
    planner = FakePlanner(
        {
            "intent": "plan_candidates",
            "targets": [
                {
                    "kind": "place",
                    "entity_types": ["attraction", "restaurant"],
                }
            ],
            "duration_days": 2,
            "entity_mentions": [
                {
                    "surface": "Quy Nhon",
                    "role": "scope",
                    "kind": "city",
                    "confidence": 1.0,
                }
            ],
            "confidence": 1.0,
        }
    )

    plan, _, failure = plan_query("Lo trinh Quy Nhon 2 ngay", planner, CATALOG)

    assert failure is None
    assert plan.geo_scope.cities == ["Quy Nhon"]
