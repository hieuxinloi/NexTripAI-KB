from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .v2_runner import evaluate_v2_response


L1_ROW = re.compile(r"^\|\s*(L1-\d{3})\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$")


def read_l1_cases(markdown_path: str | Path) -> list[dict[str, str]]:
    cases = []
    for line in Path(markdown_path).read_text(encoding="utf-8").splitlines():
        match = L1_ROW.match(line)
        if match:
            cases.append(
                {
                    "id": match.group(1),
                    "query": match.group(2),
                    "expected_output_type": match.group(3),
                }
            )
    return cases


def run_l1_audit(
    service: Any,
    markdown_path: str | Path,
    canonical_path: str | Path,
) -> dict[str, Any]:
    cases = read_l1_cases(markdown_path)
    canonical_payload = json.loads(Path(canonical_path).read_text(encoding="utf-8"))
    canonical = {case["id"]: case["expected"] for case in canonical_payload["cases"]}
    results = []
    for case in cases:
        try:
            response = service.query(case["query"], top_k=10)
            result = _classify(case, response, canonical.get(case["id"]))
        except Exception as exc:
            result = {
                **case,
                "status": "error",
                "error_type": exc.__class__.__name__,
                "message": str(exc),
            }
        results.append(result)

    counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
    strict_results = [result for result in results if result.get("has_canonical_oracle")]
    return {
        "dataset": "nextrip_level_1_audit",
        "kb_version": getattr(service, "kb_version", "unknown"),
        "total": len(results),
        "status_counts": counts,
        "strict_accuracy": {
            "passed": sum(result["status"] == "strict_pass" for result in strict_results),
            "total": len(strict_results),
        },
        "results": results,
    }


def _classify(
    case: dict[str, str],
    response: Any,
    canonical: dict[str, Any] | None,
) -> dict[str, Any]:
    base = {
        **case,
        "answer_type": response.answer_type.value,
        "planner": response.trace[0].get("planner") if response.trace else None,
        "entity_ids": [entity.place_id for entity in response.entities],
        "recommendation_ids": [entity.place_id for entity in response.recommendations],
        "fact_predicates": [fact.predicate for fact in response.facts],
        "missing_fields": response.missing_fields,
        "has_canonical_oracle": canonical is not None,
    }
    if canonical is not None:
        failures = evaluate_v2_response(canonical, response)
        return {
            **base,
            "status": "strict_pass" if not failures else "strict_fail",
            "failures": failures,
        }
    if response.missing_fields:
        return {**base, "status": "needs_clarification"}
    if response.answer_type.value == "unsupported":
        return {**base, "status": "unsupported"}
    if response.entities or response.recommendations or response.facts:
        return {**base, "status": "retrieved_unverified"}
    return {**base, "status": "no_result"}
