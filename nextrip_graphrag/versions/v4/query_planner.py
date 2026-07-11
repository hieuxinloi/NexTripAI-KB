from __future__ import annotations

import re
from typing import Any

from ...normalizer import slugify
from ..v2.schemas import QueryIntent
from ..v3.query_planner import deterministic_plan as v3_deterministic_plan
from .policy import POLICY
from .schemas import ConstraintMode, RetrievalMode, V4Constraint, V4QueryPlan


SYSTEM_INSTRUCTION = """
Create a typed travel retrieval plan. Never emit Cypher.
Use entity_lookup for exact place facts, aggregate for counts, path_search for
typed graph constraints, recommendation for ranked suggestions, community_search
for broad themes, dynamic_search for live weather/traffic/route data, and
planning_candidates for itinerary candidates. Mark mandatory requirements as
hard, preferences as soft, and exclusions as exclude.
""".strip()


CONCEPT_TERMS = {
    "hai san": "seafood",
    "chay": "vegetarian",
    "gia dinh": "families",
    "tre em": "children",
    "nguoi cao tuoi": "seniors",
    "cap doi": "couples",
    "nhom ban": "friends",
    "ho boi": "pool",
    "rooftop": "rooftop",
    "view bien": "bien",
    "yen tinh": "quiet",
    "lan ngam san ho": "lan ngam san ho",
    "tam bien": "tam bien",
    "check in": "check-in",
}


def plan_query(query: str, gemini: Any | None = None) -> tuple[V4QueryPlan, str, str | None]:
    deterministic = deterministic_plan(query)
    if deterministic.confidence >= POLICY.planner_fastpath_confidence:
        return deterministic, "deterministic_fastpath", None
    if gemini is not None:
        try:
            plan = gemini.generate_structured(
                SYSTEM_INSTRUCTION,
                f"Plan retrieval for this Vietnamese query:\n{query}",
                V4QueryPlan,
            )
            return _normalize_plan(plan), "gemini", None
        except Exception as exc:
            return deterministic, "deterministic_fallback", exc.__class__.__name__
    return deterministic, "deterministic", None


def deterministic_plan(query: str) -> V4QueryPlan:
    plain = slugify(query).replace("-", " ")
    base = v3_deterministic_plan(query)
    city = base.city or _city_plain(plain)
    entity_types = _unique(
        [entity_type for task in base.tasks for entity_type in task.entity_types]
        + _entity_types_plain(plain)
    )
    concepts = [canonical for term, canonical in CONCEPT_TERMS.items() if term in plain]
    constraints = _constraints(plain)

    if _requires_live_tool(plain):
        return V4QueryPlan(
            intent=QueryIntent.UNSUPPORTED,
            city=city,
            retrieval_mode=RetrievalMode.DYNAMIC_SEARCH,
            constraints=constraints,
            confidence=0.98,
        )

    if _is_planning_query(plain):
        return V4QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            city=city,
            entity_types=entity_types,
            required_concepts=concepts,
            constraints=constraints,
            retrieval_mode=RetrievalMode.PLANNING_CANDIDATES,
            limit=_requested_limit(plain),
            clarification_needed=city is None,
            confidence=0.94,
        )

    if _is_contextual_recommendation(plain, constraints):
        return V4QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            city=city,
            entity_types=entity_types,
            required_concepts=concepts,
            constraints=constraints,
            retrieval_mode=RetrievalMode.RECOMMENDATION,
            limit=_requested_limit(plain),
            clarification_needed=city is None,
            confidence=0.96,
        )

    if "so sanh" in plain:
        return V4QueryPlan(
            intent=QueryIntent.ENTITY_LIST,
            city=city,
            subjects=_comparison_subjects(query),
            entity_types=entity_types,
            required_concepts=concepts,
            constraints=constraints,
            retrieval_mode=RetrievalMode.COMPARISON,
            limit=_requested_limit(plain),
            confidence=0.92,
        )

    if any(marker in plain for marker in ("tong quan", "noi bat cua", "xu huong", "khu vuc nao")):
        return V4QueryPlan(
            intent=QueryIntent.ENTITY_LIST,
            city=city,
            entity_types=entity_types,
            required_concepts=concepts,
            retrieval_mode=RetrievalMode.COMMUNITY_SEARCH,
            limit=_requested_limit(plain),
            confidence=0.91,
        )

    if base.intent != QueryIntent.UNSUPPORTED:
        task = base.tasks[0]
        mode = {
            QueryIntent.AGGREGATE_COUNT: RetrievalMode.AGGREGATE,
            QueryIntent.ENTITY_DETAIL: RetrievalMode.ENTITY_LOOKUP,
            QueryIntent.ENTITY_LIST: RetrievalMode.PATH_SEARCH if concepts or constraints else RetrievalMode.PATH_SEARCH,
            QueryIntent.RECOMMENDATION: RetrievalMode.RECOMMENDATION,
        }[base.intent]
        base_filters = task.filters.model_dump(exclude_none=True)
        concepts.extend(_concepts_from_v3_filters(base_filters))
        constraints.extend(_constraints_from_v3_filters(base_filters))
        return V4QueryPlan(
            intent=base.intent,
            city=city,
            subjects=base.subjects,
            entity_types=entity_types,
            predicates=task.predicates,
            required_concepts=_unique(concepts),
            constraints=_unique_constraints(constraints),
            retrieval_mode=mode,
            limit=task.limit,
            clarification_needed=base.clarification_needed,
            confidence=base.confidence,
        )

    if "bao nhieu" in plain and entity_types:
        return V4QueryPlan(
            intent=QueryIntent.AGGREGATE_COUNT,
            city=city,
            entity_types=entity_types,
            retrieval_mode=RetrievalMode.AGGREGATE,
            clarification_needed=city is None,
            confidence=0.97,
        )

    if concepts or entity_types and any(marker in plain for marker in ("goi y", "nen", "nao", "danh sach", "liet ke")):
        return V4QueryPlan(
            intent=QueryIntent.RECOMMENDATION,
            city=city,
            entity_types=entity_types,
            required_concepts=_unique(concepts),
            constraints=constraints,
            retrieval_mode=RetrievalMode.RECOMMENDATION,
            limit=_requested_limit(plain),
            clarification_needed=city is None,
            confidence=0.92,
        )

    return V4QueryPlan(
        intent=QueryIntent.UNSUPPORTED,
        city=city,
        retrieval_mode=RetrievalMode.UNSUPPORTED,
        confidence=0.3,
    )


def _constraints(plain: str) -> list[V4Constraint]:
    constraints: list[V4Constraint] = []
    if "troi mua" in plain or "khi mua" in plain:
        constraints.append(V4Constraint(field="weather", value="rain", mode=ConstraintMode.HARD))
    if any(term in plain for term in ("khong ngoai troi", "trong nha", "indoor")):
        constraints.append(V4Constraint(field="indoor", value=True, mode=ConstraintMode.HARD))
    budget = re.search(r"(?:duoi|toi da|ngan sach)\s*([0-9]+)(?:\s*(k|nghin|trieu))?", plain)
    if budget:
        value = float(budget.group(1))
        if budget.group(2) in {"k", "nghin"}:
            value *= 1_000
        elif budget.group(2) == "trieu":
            value *= 1_000_000
        constraints.append(V4Constraint(field="budget_max", value=value, mode=ConstraintMode.HARD))
    return constraints


def _constraints_from_v3_filters(filters: dict[str, Any]) -> list[V4Constraint]:
    constraints = []
    for field in ("star_rating", "near_subject", "open_24h", "indoor", "budget_max"):
        if field in filters:
            constraints.append(V4Constraint(field=field, value=filters[field], mode=ConstraintMode.HARD))
    return constraints


def _concepts_from_v3_filters(filters: dict[str, Any]) -> list[str]:
    return [
        str(filters[field])
        for field in ("amenity", "cuisine", "dish", "feature", "venue_type", "ambience", "tag", "category")
        if filters.get(field)
    ]


def _requires_live_tool(plain: str) -> bool:
    live_markers = ("hom nay", "bay gio", "hien tai", "ngay mai", "dang mua", "tinh trang giao thong")
    live_domains = ("thoi tiet", "mua", "nhiet do", "giao thong", "duong di", "mat bao lau")
    return any(marker in plain for marker in live_markers) and any(domain in plain for domain in live_domains)


def _is_planning_query(plain: str) -> bool:
    return any(marker in plain for marker in ("lich trinh", "ke hoach", "1 ngay", "mot ngay", "2 ngay", "hai ngay"))


def _is_contextual_recommendation(plain: str, constraints: list[V4Constraint]) -> bool:
    return bool(constraints) and any(
        marker in plain for marker in ("nen", "goi y", "di dau", "di choi", "phu hop")
    )


def _city_plain(plain: str) -> str | None:
    if "quy nhon" in plain or "qui nhon" in plain:
        return "Quy Nh\u01a1n"
    if "da nang" in plain or "danang" in plain:
        return "\u0110\u00e0 N\u1eb5ng"
    return None


def _entity_types_plain(plain: str) -> list[str]:
    patterns = (
        ("hotel", ("khach san", "hotel")),
        ("restaurant", ("nha hang", "quan an", "restaurant")),
        ("cafe", ("ca phe", "cafe", "coffee")),
        ("nightlife", ("nightlife", "bar", "pub", "quan dem")),
        ("attraction", ("diem tham quan", "dia diem du lich", "di choi")),
    )
    matches = []
    for entity_type, terms in patterns:
        positions = [plain.find(term) for term in terms if term in plain]
        if positions:
            matches.append((min(positions), entity_type))
    return [entity_type for _, entity_type in sorted(matches)]


def _requested_limit(plain: str) -> int:
    match = re.search(r"\b([1-9]|[12][0-9]|30)\b", plain)
    return int(match.group(1)) if match else 5


def _comparison_subjects(query: str) -> list[str]:
    parts = re.split(r"\s+(?:và|va|với|voi)\s+", query, maxsplit=1, flags=re.IGNORECASE)
    return [part.strip(" ?.!") for part in parts if part.strip(" ?.!")]


def _normalize_plan(plan: V4QueryPlan) -> V4QueryPlan:
    return plan.model_copy(
        update={
            "required_concepts": _unique(plan.required_concepts),
            "preferred_concepts": _unique(plan.preferred_concepts),
            "constraints": _unique_constraints(plan.constraints),
        }
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.casefold().strip() for value in values if value.strip()))


def _unique_constraints(values: list[V4Constraint]) -> list[V4Constraint]:
    return list({(item.field, str(item.value), item.mode): item for item in values}.values())
