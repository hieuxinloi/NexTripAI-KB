from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..versions.v2.retrieval import V2RetrievalService


def run_v2_benchmark(
    service: V2RetrievalService,
    dataset_path: str | Path,
) -> dict[str, Any]:
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    results = []
    for case in dataset["cases"]:
        response = service.query(case["query"], top_k=30)
        failures = evaluate_v2_response(case["expected"], response)
        results.append(
            {
                "id": case["id"],
                "query": case["query"],
                "passed": not failures,
                "failures": failures,
                "answer_type": response.answer_type,
                "entity_ids": [entity.place_id for entity in response.entities],
                "fact_predicates": [fact.predicate for fact in response.facts],
            }
        )
    passed = sum(result["passed"] for result in results)
    return {
        "dataset": dataset["dataset"],
        "kb_version": "v2",
        "passed": passed,
        "total": len(results),
        "pass_rate": passed / len(results) if results else 0,
        "results": results,
    }


def evaluate_v2_response(expected: dict[str, Any], response: Any) -> list[str]:
    failures = []
    if response.answer_type != expected["intent"]:
        failures.append(f"intent:{response.answer_type}!={expected['intent']}")
    if not response.query_plan.tasks:
        return [*failures, "missing_task"]
    if response.query_plan.tasks[0].operation != expected["operation"]:
        failures.append("operation_mismatch")

    if "count" in expected:
        actual = response.facts[0].value if response.facts else None
        if actual != expected["count"]:
            failures.append(f"count:{actual}!={expected['count']}")

    accepted = set(expected.get("accepted_entity_ids", []))
    if accepted and (not response.entities or response.entities[0].place_id not in accepted):
        actual = response.entities[0].place_id if response.entities else None
        failures.append(f"entity:{actual} not in {sorted(accepted)}")

    required_predicates = set(expected.get("required_predicates", []))
    actual_predicates = {fact.predicate for fact in response.facts}
    if required_predicates and not required_predicates <= actual_predicates:
        failures.append(f"missing_predicates:{sorted(required_predicates - actual_predicates)}")

    minimum = expected.get("minimum_results")
    if minimum is not None and len(response.entities) < minimum:
        failures.append(f"results:{len(response.entities)}<{minimum}")
    return failures
