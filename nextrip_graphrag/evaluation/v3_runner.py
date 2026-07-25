from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable


CASE_ROW = re.compile(
    r"^\|\s*([A-F]-\d{3})\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$"
)
COMMON_CASE_LIMITS = {
    "A": 120,
    "B": 80,
    "C": 50,
    "D": 60,
    "E": 50,
    "F": 40,
}


class OfflinePlanner:
    """Fail-fast stand-in used to exercise the production fallback path."""

    def generate_structured(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("PlannerOffline")


def read_v3_cases(markdown_path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in Path(markdown_path).read_text(encoding="utf-8").splitlines():
        match = CASE_ROW.match(line)
        if not match:
            continue
        case_id, query, expected = match.groups()
        group, ordinal = case_id.split("-", maxsplit=1)
        cases.append(
            {
                "id": case_id,
                "group": group,
                "query": query,
                "expected_output": expected,
                "comparison_case": int(ordinal) <= COMMON_CASE_LIMITS[group],
            }
        )
    return cases


def run_v3_benchmark(
    service: Any,
    markdown_path: str | Path,
    *,
    output_path: str | Path | None = None,
    conversation_runner: Callable[[list[str], int], list[Any]] | None = None,
    benchmark_version: str | None = None,
) -> dict[str, Any]:
    results = []
    for case in read_v3_cases(markdown_path):
        responses = []
        try:
            turns = _turns(case["query"])
            if conversation_runner is not None and len(turns) > 1:
                responses = conversation_runner(turns, 10)
            else:
                for turn in turns:
                    responses.append(service.query(turn, top_k=10))
            passed, reason = evaluate_v3_case(case, responses)
            result = {
                **case,
                "actual_output": format_actual_output(responses),
                "status": "PASS" if passed else "FAIL",
                "reason": reason,
                "responses": [_response_payload(item) for item in responses],
            }
        except Exception as exc:
            result = {
                **case,
                "actual_output": f"{exc.__class__.__name__}: {exc}",
                "status": "FAIL",
                "reason": f"runtime_error:{exc.__class__.__name__}",
                "responses": [],
            }
        results.append(result)

    status_counts = Counter(item["status"] for item in results)
    group_counts = {
        group: dict(Counter(item["status"] for item in results if item["group"] == group))
        for group in COMMON_CASE_LIMITS
    }
    common = [item for item in results if item["comparison_case"]]
    common_counts = Counter(item["status"] for item in common)
    report = {
        "benchmark": "nextrip_end_to_end_graphrag_v3",
        "kb_version": benchmark_version or service.kb_version,
        "total": len(results),
        "passed": status_counts["PASS"],
        "failed": status_counts["FAIL"],
        "pass_rate": status_counts["PASS"] / len(results) if results else 0.0,
        "comparison_400": {
            "total": len(common),
            "passed": common_counts["PASS"],
            "failed": common_counts["FAIL"],
            "pass_rate": common_counts["PASS"] / len(common) if common else 0.0,
        },
        "group_counts": group_counts,
        "failure_reasons": dict(
            Counter(item["reason"] for item in results if item["status"] == "FAIL")
        ),
        "results": results,
    }
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return report


def rejudge_v3_report(
    input_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    report = json.loads(Path(input_path).read_text(encoding="utf-8"))
    for result in report["results"]:
        passed, reason = evaluate_v3_case(result, result.get("responses") or [])
        result["status"] = "PASS" if passed else "FAIL"
        result["reason"] = reason
    status_counts = Counter(item["status"] for item in report["results"])
    common = [item for item in report["results"] if item["comparison_case"]]
    common_counts = Counter(item["status"] for item in common)
    report.update(
        {
            "passed": status_counts["PASS"],
            "failed": status_counts["FAIL"],
            "pass_rate": status_counts["PASS"] / len(report["results"]),
            "comparison_400": {
                "total": len(common),
                "passed": common_counts["PASS"],
                "failed": common_counts["FAIL"],
                "pass_rate": common_counts["PASS"] / len(common),
            },
            "group_counts": {
                group: dict(
                    Counter(
                        item["status"]
                        for item in report["results"]
                        if item["group"] == group
                    )
                )
                for group in COMMON_CASE_LIMITS
            },
            "failure_reasons": dict(
                Counter(
                    item["reason"]
                    for item in report["results"]
                    if item["status"] == "FAIL"
                )
            ),
        }
    )
    path = Path(output_path) if output_path else Path(input_path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def evaluate_v3_case(
    case: dict[str, Any],
    responses: list[Any],
) -> tuple[bool, str]:
    if not responses:
        return False, "no_response"
    payloads = [_response_payload(response) for response in responses]
    payload = payloads[-1]
    if payload.get("error"):
        code = payload["error"].get("code", "unknown")
        return False, f"planner_error:{code}"

    group = case["group"]
    query = case["query"]
    if group == "A":
        return _evaluate_information(query, payload)
    if group == "B":
        return _evaluate_recommendation(query, payload)
    if group == "C":
        return _evaluate_relationship(query, payload)
    if group == "D":
        return _evaluate_itinerary(query, payload)
    if group == "E":
        return _evaluate_multiturn(query, payloads)
    return _evaluate_hard_case(query, payload)


def format_actual_output(responses: list[Any]) -> str:
    rendered = [_format_one(_response_payload(response)) for response in responses]
    if len(rendered) == 1:
        return rendered[0]
    return " → ".join(f"Lượt {index}: {text}" for index, text in enumerate(rendered, 1))


def _format_one(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        error = payload["error"]
        return f"Lỗi {error.get('code')}: {error.get('message')}"
    itinerary = payload.get("itinerary") or []
    if itinerary:
        return " | ".join(
            f"Ngày {day.get('day')}: "
            + ", ".join(
                f"{slot.get('start_time')} {slot.get('name')}"
                for slot in day.get("slots") or []
            )
            for day in itinerary
        )
    facts = payload.get("facts") or []
    if facts:
        return "; ".join(
            f"{fact.get('predicate')}={_short(fact.get('value'))}" for fact in facts[:8]
        )
    items = [
        *(payload.get("recommendations") or []),
        *(payload.get("entities") or []),
        *(payload.get("targets") or []),
    ]
    if items:
        descriptions = []
        for item in items[:10]:
            name = item.get("name", item.get("target_id", "?"))
            city = item.get("city")
            entity_type = item.get("entity_type", item.get("kind"))
            category = item.get("category")
            distance = item.get("distance_km")
            details = [str(value) for value in (city, entity_type, category) if value]
            if distance is not None:
                details.append(f"{distance:g} km")
            descriptions.append(
                f"{name} ({', '.join(details)})" if details else str(name)
            )
        return "; ".join(descriptions)
    required_tools = payload.get("required_tools") or []
    if required_tools:
        return "Cần công cụ dữ liệu động: " + ", ".join(required_tools)
    missing = payload.get("missing_fields") or []
    if missing:
        return "Cần bổ sung: " + ", ".join(missing)
    return f"Không có kết quả (intent={payload.get('intent', 'unknown')})"


def _evaluate_information(query: str, payload: dict[str, Any]) -> tuple[bool, str]:
    plain = _plain(query)
    facts = payload.get("facts") or []
    predicates = {str(item.get("predicate")) for item in facts}
    if (
        "co bao nhieu" in plain
        and _mentioned_city(query)
        and _mentioned_entity_types(query)
    ):
        if payload.get("answer_type") != "aggregate_count" or not facts:
            return False, "count_not_aggregated"
        return True, "count_fact_returned"

    required = _requested_predicates(plain)
    if required:
        if not predicates.intersection(required):
            return False, "requested_fact_missing:" + ",".join(sorted(required))
        return True, "requested_fact_returned"

    if "thuoc loai dia diem" in plain:
        if not payload.get("entities"):
            return False, "entity_type_missing"
        return True, "entity_type_returned"
    if "thuoc loai hinh nightlife" in plain:
        if "venue_type" not in predicates:
            return False, "requested_fact_missing:venue_type"
        return True, "venue_type_returned"
    if payload.get("targets") or payload.get("entities") or facts:
        return True, "grounded_profile_returned"
    return False, "named_entity_not_resolved"


def _evaluate_recommendation(query: str, payload: dict[str, Any]) -> tuple[bool, str]:
    recommendations = payload.get("recommendations") or []
    if payload.get("answer_type") != "recommendation" or not recommendations:
        return False, "recommendations_missing"
    expected_city = _mentioned_city(query)
    if expected_city and any(item.get("city") != expected_city for item in recommendations):
        return False, "wrong_city_recommendation"
    expected_types = set(_mentioned_entity_types(query))
    if expected_types and any(
        item.get("entity_type") not in expected_types for item in recommendations
    ):
        return False, "wrong_entity_type_recommendation"

    plan = payload.get("query_plan") or {}
    requested_filters = _requested_filters(query)
    recognized = _recognized_filters(plan)
    missing_filters = sorted(requested_filters - recognized)
    if missing_filters:
        return False, "filters_not_applied:" + ",".join(missing_filters)
    hard_checks = payload.get("constraint_results") or []
    if any(not item.get("passed") for item in hard_checks):
        return False, "hard_constraint_failed"
    return True, "grounded_recommendations_returned"


def _evaluate_relationship(query: str, payload: dict[str, Any]) -> tuple[bool, str]:
    plain = _plain(query)
    paths = payload.get("matched_paths") or []
    items = [*(payload.get("recommendations") or []), *(payload.get("entities") or [])]
    if "bao xa" in plain:
        distance_facts = [
            fact
            for fact in payload.get("facts") or []
            if "distance" in str(fact.get("predicate"))
        ]
        has_near_path = any(
            any("NEAR" in str(rel) for rel in path.get("relationships") or [])
            for path in paths
        )
        if not distance_facts and not has_near_path:
            return False, "distance_relationship_missing"
        return True, "distance_relationship_returned"
    if any(term in plain for term in ("gan do", "quanh ", "cach do")):
        if not items:
            return False, "nearby_recommendations_missing"
        if not any(item.get("distance_km") is not None for item in items):
            return False, "nearby_distance_missing"
        return True, "nearby_recommendations_returned"
    return False, "relationship_not_supported"


def _evaluate_itinerary(query: str, payload: dict[str, Any]) -> tuple[bool, str]:
    plan = payload.get("query_plan") or {}
    duration = _duration_days(query)
    if payload.get("intent") != "plan_candidates":
        return False, "itinerary_intent_missing"
    if duration and plan.get("duration_days") != duration:
        return False, "itinerary_duration_missing"
    if len(payload.get("recommendations") or []) < 3:
        return False, "itinerary_candidates_insufficient"
    itinerary = payload.get("itinerary") or []
    if not itinerary:
        return False, "itinerary_schedule_not_generated"
    if duration and len(itinerary) != duration:
        return False, "itinerary_day_count_mismatch"
    recommendation_ids = {
        item.get("place_id")
        for item in payload.get("recommendations") or []
        if item.get("place_id")
    }
    for expected_day, day in enumerate(itinerary, start=1):
        if day.get("day") != expected_day:
            return False, "itinerary_day_order_invalid"
        slots = day.get("slots") or []
        if not slots:
            return False, "itinerary_empty_day"
        if len(slots) > 3:
            return False, "itinerary_day_overloaded"
        if any(slot.get("place_id") not in recommendation_ids for slot in slots):
            return False, "itinerary_ungrounded_place"
        if any(
            not slot.get("start_time") or not slot.get("end_time")
            for slot in slots
        ):
            return False, "itinerary_time_window_missing"
    return True, "grounded_itinerary_returned"


def _evaluate_multiturn(
    query: str,
    payloads: list[dict[str, Any]],
) -> tuple[bool, str]:
    if len(_turns(query)) < 2 or len(payloads) < 2:
        return False, "multiturn_not_executed"
    final = payloads[-1]
    if final.get("error"):
        return False, "multiturn_context_lost"
    first_city = _mentioned_city(_turns(query)[0])
    final_plan = final.get("query_plan") or {}
    final_cities = (final_plan.get("geo_scope") or {}).get("cities") or []
    if first_city and first_city not in final_cities:
        return False, "multiturn_city_context_lost"
    context = final.get("conversation_context") or {}
    if context.get("turn_count") != len(_turns(query)):
        return False, "multiturn_state_not_recorded"
    if first_city and first_city not in (context.get("cities") or []):
        return False, "multiturn_context_city_missing"
    if not context.get("applied_updates"):
        return False, "multiturn_update_not_applied"
    if not context.get("resolved_query"):
        return False, "multiturn_resolved_query_missing"
    grounded = bool(
        final.get("facts")
        or final.get("entities")
        or final.get("recommendations")
        or final.get("itinerary")
        or final.get("required_tools")
    )
    if not grounded:
        return False, "multiturn_grounded_result_missing"
    return True, "multiturn_context_applied"


def _evaluate_hard_case(query: str, payload: dict[str, Any]) -> tuple[bool, str]:
    plain = _plain(query)
    dynamic_terms = (
        "toi nay",
        "ngay mai",
        "thoi tiet",
        "con hoat dong",
        "gia phong",
        "gia ve",
        "dat tour",
    )
    if any(term in plain for term in dynamic_terms):
        if payload.get("required_tools"):
            return True, "dynamic_tool_requested"
        if payload.get("missing_fields"):
            return True, "dynamic_claim_withheld"
        return False, "dynamic_claim_not_routed"

    unsafe_terms = ("so dien thoai rieng", "hay doan gia", "nguon a noi")
    if any(term in plain for term in unsafe_terms):
        has_claim = bool(
            payload.get("facts")
            or payload.get("entities")
            or payload.get("recommendations")
        )
        return (not has_claim, "unsafe_claim_withheld" if not has_claim else "unsafe_claim_returned")

    context_only = (
        "quan do",
        "cho thu hai",
        "gan trung tam",
        "khach san re",
        "dep nhat",
        "cuoi tuan",
        "gan day",
    )
    if any(term in plain for term in context_only) and not _mentioned_city(query):
        if payload.get("missing_fields") or (payload.get("query_plan") or {}).get(
            "clarification_needed"
        ):
            return True, "clarification_requested"
        return False, "clarification_missing"
    if (
        not _mentioned_city(query)
        and (
            payload.get("missing_fields")
            or (payload.get("query_plan") or {}).get("clarification_needed")
        )
    ):
        return True, "clarification_requested"

    if (
        payload.get("facts")
        or payload.get("entities")
        or payload.get("recommendations")
        or payload.get("targets")
    ):
        return True, "grounded_hard_case_response"
    return False, "hard_case_not_resolved"


def _response_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"Unsupported response type: {type(response).__name__}")


def _turns(query: str) -> list[str]:
    return [part.strip() for part in query.split("→") if part.strip()]


def _plain(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    return " ".join(
        "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
        .replace("?", " ")
        .replace(".", " ")
        .replace(",", " ")
        .split()
    )


def _mentioned_city(query: str) -> str | None:
    plain = _plain(query)
    if any(term in plain for term in ("da nang", "danang", " dn ")):
        return "Đà Nẵng"
    if any(term in f" {plain} " for term in (" quy nhon ", " qui nhon ", " qn ")):
        return "Quy Nhơn"
    return None


def _mentioned_entity_types(query: str) -> list[str]:
    plain = _plain(query)
    patterns = {
        "hotel": ("khach san", "hotel", "resort", "luu tru", "nha nghi", "homestay"),
        "restaurant": ("nha hang", "quan an", "am thuc"),
        "cafe": ("cafe", "ca phe", "coffee", "quan tra"),
        "nightlife": ("nightlife", "bar", "pub", "club"),
        "attraction": (
            "diem tham quan",
            "diem du lich",
            "dia diem lich su",
            "dia diem thien nhien",
            "dia diem van hoa",
            "bai bien",
            "khu vui choi",
        ),
    }
    return [
        entity_type
        for entity_type, terms in patterns.items()
        if any(term in plain for term in terms)
    ]


def _requested_predicates(plain: str) -> set[str]:
    if any(term in plain for term in ("o dau", "dia chi", "nam o")):
        return {"address", "location"}
    if any(term in plain for term in ("gio hoat dong", "mo cua", "mo cua may")):
        return {"opening_hours"}
    if "danh gia bao nhieu" in plain or "bao nhieu diem" in plain:
        return {"rating"}
    if "may sao" in plain or "bao nhieu sao" in plain:
        return {"star_rating"}
    if "thoi diem nao phu hop" in plain:
        return {"weather"}
    if "danh bao lau" in plain or "tham quan" in plain and "mat bao lau" in plain:
        return {"duration"}
    if any(term in plain for term in ("gia bao nhieu", "ton khoang bao nhieu")):
        return {"price", "price_min", "price_max"}
    return set()


def _requested_filters(query: str) -> set[str]:
    plain = _plain(query)
    filters: set[str] = set()
    star = re.search(r"\b([1-5])\s*sao\b", plain)
    if star:
        filters.add(f"star_rating:{star.group(1)}")
    aliases = {
        "pool": ("ho boi",),
        "wifi": ("wifi",),
        "family": ("gia dinh", "phong gia dinh"),
        "beach": ("bai bien", "view bien", "ra bien"),
        "onsite_restaurant": ("nha hang trong khuon vien",),
        "high_rating": ("danh gia cao", "pho bien"),
        "budget": ("tiet kiem", "gia re", "duoi "),
        "indoor": ("trong nha", "troi mua"),
        "history": ("lich su",),
        "nature": ("thien nhien",),
        "culture": ("van hoa",),
        "entertainment": ("khu vui choi", "vui choi", "giai tri"),
        "seafood": ("hai san",),
        "vietnamese": ("mon viet",),
        "bbq": ("bbq",),
        "local_food": ("dac san", "dia phuong"),
        "rooftop": ("rooftop",),
        "work": ("lam viec",),
        "relax": ("thu gian",),
        "photo": ("chup anh",),
        "couple": ("cap doi", "lang man"),
        "group": ("nhom ban",),
        "live_music": ("nhac song",),
        "tea": ("quan tra",),
    }
    for name, terms in aliases.items():
        if any(term in plain for term in terms):
            filters.add(name)
    return filters


def _recognized_filters(plan: dict[str, Any]) -> set[str]:
    recognized: set[str] = set()
    for constraint in plan.get("constraints") or []:
        field = constraint.get("field")
        value = constraint.get("value")
        if field == "star_rating":
            recognized.add(f"star_rating:{value}")
        elif field == "budget_max":
            recognized.add("budget")
        elif field == "indoor":
            recognized.add("indoor")
        elif field == "weather":
            recognized.add("indoor")
        elif field == "near_subject":
            recognized.add("near")
        elif field == "category":
            category_filters = {
                "beach": "beach",
                "entertainment": "entertainment",
                "historical": "history",
                "nature": "nature",
                "culture": "culture",
                "seafood": "seafood",
                "vietnamese": "vietnamese",
                "bbq": "bbq",
                "rooftop_cafe": "rooftop",
                "work_cafe": "work",
                "tea_shop": "tea",
                "specialty_coffee": "local_food",
                "rooftop_bar": "rooftop",
            }
            mapped = category_filters.get(str(value))
            if mapped:
                recognized.add(mapped)
    if "rating" in (plan.get("ranking_criteria") or []):
        recognized.add("high_rating")
    if "popularity" in (plan.get("ranking_criteria") or []):
        recognized.add("high_rating")
    if "price_low" in (plan.get("ranking_criteria") or []):
        recognized.add("budget")
    concept_text = _plain(
        " ".join(
            [
                *(plan.get("required_concepts") or []),
                *(plan.get("preferred_concepts") or []),
            ]
        )
    )
    concept_aliases = {
        "pool": ("pool", "ho boi"),
        "wifi": ("wifi",),
        "family": ("family", "families", "gia dinh"),
        "beach": ("beach", "bien", "sea view"),
        "onsite_restaurant": ("onsite restaurant", "nha hang", "restaurant"),
        "history": ("history", "lich su"),
        "nature": ("nature", "thien nhien"),
        "culture": ("culture", "van hoa"),
        "entertainment": ("entertainment", "vui choi", "giai tri"),
        "seafood": ("seafood", "hai san"),
        "vietnamese": ("vietnamese", "mon viet"),
        "bbq": ("bbq",),
        "local_food": ("local", "dac san", "dia phuong", "vietnamese"),
        "rooftop": ("rooftop",),
        "work": ("work", "lam viec"),
        "relax": ("relax", "thu gian", "chill"),
        "photo": ("photo", "chup anh", "ca phe dep"),
        "couple": ("couple", "cap doi", "romantic"),
        "group": ("group", "nhom"),
        "live_music": ("live music", "nhac song"),
        "tea": ("tea", "tra"),
    }
    for name, terms in concept_aliases.items():
        if any(term in concept_text for term in terms):
            recognized.add(name)
    return recognized


def _duration_days(query: str) -> int | None:
    plain = _plain(query)
    match = re.search(r"\b(\d{1,2})\s*(?:ngay|n)\b", plain)
    return int(match.group(1)) if match else None


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
