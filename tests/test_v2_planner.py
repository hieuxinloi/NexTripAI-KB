from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nextrip_graphrag.normalizer import read_processed
from nextrip_graphrag.evaluation.l1_audit import read_l1_cases
from nextrip_graphrag.versions.v2.ontology import extract_facts
from nextrip_graphrag.versions.v2.query_planner import deterministic_plan, plan_query
from nextrip_graphrag.versions.v2.retrieval import _is_compatible_anchor
from nextrip_graphrag.versions.v2.schemas import QueryPlan


DATASET = Path("nextrip_graphrag/evaluation/datasets/l1_1_v2.json")


@pytest.mark.parametrize(
    ("query", "intent", "operation"),
    [
        ("Đà Nẵng có bao nhiêu khách sạn?", "aggregate_count", "count"),
        ("Quy Nhơn có bao nhiêu nhà hàng?", "aggregate_count", "count"),
        ("Có bao nhiêu điểm tham quan ở Đà Nẵng?", "aggregate_count", "count"),
        ("Liệt kê các quán cafe ở Quy Nhơn", "entity_list", "filter"),
        ("Đà Nẵng có những địa điểm nightlife nào?", "entity_list", "filter"),
        ("Bãi biển Mỹ Khê ở đâu?", "entity_detail", "lookup"),
        ("Cầu Rồng có gì đặc biệt?", "entity_detail", "lookup"),
        ("Ghềnh Ráng Tiên Sa ở thành phố nào?", "entity_detail", "lookup"),
        ("Bà Nà Hills có độ cao bao nhiêu?", "entity_detail", "lookup"),
        ("Chùa Linh Ứng nằm ở đâu?", "entity_detail", "lookup"),
    ],
)
def test_l1_1_queries_compile_to_typed_plan(query: str, intent: str, operation: str) -> None:
    plan = deterministic_plan(query)

    assert plan.intent == intent
    assert plan.tasks[0].operation == operation
    assert plan.clarification_needed is False


def test_rain_recommendation_becomes_hard_constraint() -> None:
    plan = deterministic_plan("Trời mưa thì nên đi chơi ở đâu ở Quy Nhơn?")

    assert plan.intent == "recommendation"
    assert plan.tasks[0].hard_constraints.indoor is True
    assert plan.tasks[0].hard_constraints.weather == "rain"


def test_one_day_query_requests_diverse_candidates_without_timeline() -> None:
    plan = deterministic_plan("Quy Nhơn 1 ngày cần làm gì?")

    assert plan.intent == "recommendation"
    assert plan.duration_days == 1
    assert plan.tasks[0].entity_types == ["attraction", "restaurant", "cafe"]


def test_unknown_predicate_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown predicates"):
        QueryPlan.model_validate(
            {
                "intent": "entity_detail",
                "subjects": ["Cầu Rồng"],
                "tasks": [{"operation": "lookup", "predicates": ["raw_cypher"]}],
            }
        )


def test_entity_detail_without_predicates_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires predicates"):
        QueryPlan.model_validate(
            {
                "intent": "entity_detail",
                "subjects": ["Bãi biển Mỹ Khê"],
                "tasks": [{"operation": "lookup", "predicates": []}],
            }
        )


def test_clear_factual_query_skips_gemini() -> None:
    class UnexpectedGemini:
        def generate_structured(self, *args, **kwargs):
            raise AssertionError("Gemini should not run for high-confidence factual queries")

    plan, planner, fallback_reason = plan_query(
        "Bãi biển Mỹ Khê ở đâu?",
        UnexpectedGemini(),
    )

    assert plan.intent == "entity_detail"
    assert planner == "deterministic_fastpath"
    assert fallback_reason is None


def test_multi_entity_count_creates_one_task_per_type() -> None:
    plan = deterministic_plan(
        "Quy Nhơn có bao nhiêu quán cafe và bao nhiêu nhà hàng và bao nhiêu khách sạn"
    )

    assert [task.entity_types for task in plan.tasks] == [
        ["cafe"],
        ["restaurant"],
        ["hotel"],
    ]


def test_location_query_extracts_entity_name_without_city_suffix() -> None:
    plan = deterministic_plan("cafe highlands ở Quy Nhơn có địa chỉ như nào")

    assert plan.subjects == ["cafe highlands"]
    assert plan.tasks[0].predicates == ["address", "location"]


def test_fulltext_anchor_rejects_unrelated_entity() -> None:
    assert not _is_compatible_anchor(
        "cafe highlands",
        {"name": "East West Brewing Quy Nhon", "aliases": []},
    )
    assert _is_compatible_anchor(
        "cafe highlands",
        {"name": "Highland Coffee", "aliases": []},
    )


def test_benchmark_entity_ids_exist_and_ba_na_has_altitude_fact() -> None:
    benchmark = json.loads(DATASET.read_text(encoding="utf-8"))
    bundle = read_processed("processed_verified")
    places = {place["id"]: place for place in bundle["places"]}
    accepted_ids = {
        entity_id
        for case in benchmark["cases"]
        for entity_id in case["expected"].get("accepted_entity_ids", [])
    }

    assert accepted_ids <= places.keys()
    facts = {fact["predicate"]: fact for fact in extract_facts(places["attr_dn_067"])}
    assert facts["altitude"]["value"] == 1400
    assert facts["altitude"]["unit"] == "m"


def test_level_1_markdown_contains_100_parseable_cases() -> None:
    cases = read_l1_cases(Path("../docs/test_cases_benchmark.md"))

    assert len(cases) == 100
    assert cases[0]["id"] == "L1-001"
    assert cases[-1]["id"] == "L1-100"
