from __future__ import annotations

from pathlib import Path
from typing import Any

from nextrip_graphrag.versions.v5.schemas import V5Intent
from nextrip_graphrag.versions.v8.query_planner import V8PlannerDraft, plan_query
from nextrip_graphrag.versions.v8.retrieval import V8RetrievalService


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
    assert V8RetrievalService.graph_kb_version == "v5"
    assert V8RetrievalService.response_model.model_fields["kb_version"].default == "v8"


def test_v8_fallback_uses_current_search_clause_and_keeps_filters_safe() -> None:
    source = (
        Path(__file__).parents[1] / "nextrip_graphrag" / "versions" / "v8" / "retrieval.py"
    ).read_text(encoding="utf-8")

    assert "SEARCH place IN" in source
    assert "db.index.vector.queryNodes" not in source
    assert "WHERE place.kb_version = $kb_version" in source
