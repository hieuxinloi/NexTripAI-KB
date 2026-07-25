from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ...normalizer import canonical_city, slugify
from ..v3.schemas import V3_PREDICATES
from ..v4.query_planner import StructuredPlanner
from ..v4.schemas import RankingCriterion, V4Constraint
from .schemas import GeoScope, QueryTarget, TargetKind, V5Intent, V5QueryPlan


SYSTEM_INSTRUCTION = """You are the typed query planner for NexTripAI GraphRAG V5.
Return only the requested structured schema; never emit Cypher or prose.
Choose the target kind before retrieval. A named geographic locality is geo_area,
not place. Food specialties are dish targets. Activities are activity targets.
Use profile for broad requests about one named target and lookup for requested fields.
For location questions about a place, request both address and location.
Use summarize for broad questions about a city or area. Use recommend/list for
candidate retrieval, aggregate only for counts, and plan_candidates for itinerary
inputs. Dynamic weather, live routes, booking, current prices and transport status
must use tool_required with explicit required_tools. Never invent entities, fields,
constraints, or tools.
For place recommendations and lists, always set entity_types. Use attraction for
sightseeing, scenery, activities, and places to visit; use hotel, restaurant, cafe,
or nightlife only when the user asks for that venue type or an equivalent meaning.
For required_concepts and preferred_concepts, preserve the user's normalized
meaning even when no graph concept has the exact same wording. The application
will link those semantic terms to graph concepts after planning.
Generic subjective words such as "ngon" are preferences, not hard requirements.
"""


class TargetDraft(BaseModel):
    kind: TargetKind
    value: str | None = None
    entity_types: list[str] = Field(default_factory=list)


class GeoScopeDraft(BaseModel):
    cities: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    near_entities: list[str] = Field(default_factory=list)


class V5PlannerDraft(BaseModel):
    intent: V5Intent
    targets: list[TargetDraft] = Field(default_factory=list)
    geo_scope: GeoScopeDraft = Field(default_factory=GeoScopeDraft)
    requested_fields: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V4Constraint] = Field(default_factory=list)
    duration_days: int | None = Field(default=None, ge=1, le=30)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    required_tools: list[str] = Field(default_factory=list)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class PlannerFailure:
    code: Literal["invalid_plan", "planner_unavailable"]
    reason: str
    retryable: bool


def plan_query(
    query: str,
    gemini: StructuredPlanner | None,
    catalog: dict[str, list[str]],
) -> tuple[V5QueryPlan, str, PlannerFailure | None]:
    if gemini is None:
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="planner_unavailable",
            reason="GeminiUnavailable",
            retryable=True,
        )
    try:
        draft = gemini.generate_structured(
            SYSTEM_INSTRUCTION,
            _user_prompt(query, catalog),
            V5PlannerDraft,
        )
        plan = _compile_plan(draft, catalog, query)
        logger.info("V5 planner output plan={}", plan.model_dump_json())
        return plan, "gemini", None
    except (ValueError, ValidationError) as exc:
        if _catalog_mentions(query, catalog["cities"]) or _catalog_mentions(
            query, catalog["areas"]
        ):
            try:
                repaired_draft = gemini.generate_structured(
                    _repair_instruction(exc),
                    _user_prompt(query, catalog),
                    V5PlannerDraft,
                )
                repaired_plan = _compile_plan(repaired_draft, catalog, query)
                logger.info(
                    "V5 planner repaired output plan={}",
                    repaired_plan.model_dump_json(),
                )
                return repaired_plan, "gemini_repair", None
            except Exception as repair_exc:
                logger.warning(
                    "V5 planner repair failed error_type={} query_length={}",
                    repair_exc.__class__.__name__,
                    len(query),
                )
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="invalid_plan",
            reason=exc.__class__.__name__,
            retryable=False,
        )
    except Exception as exc:
        logger.warning(
            "V5 planner unavailable error_type={} query_length={}",
            exc.__class__.__name__,
            len(query),
        )
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="planner_unavailable",
            reason=exc.__class__.__name__,
            retryable=True,
        )


def _repair_instruction(exc: Exception) -> str:
    return (
        SYSTEM_INSTRUCTION
        + "\nThe previous plan failed validation: "
        + f"{exc.__class__.__name__}: {exc}. "
        + "Create a new plan using only graph_vocabulary. For recommendations or "
        + "itineraries, target kind place and always set entity_types. Use a city "
        + "only in geo_scope, never as the candidate place target."
    )


def _compile_plan(
    draft: V5PlannerDraft,
    catalog: dict[str, list[str]],
    query: str,
) -> V5QueryPlan:
    dish_discovery = _is_dish_discovery_query(query)
    referenced_dishes = _referenced_dish_mentions(query, catalog["concepts"])
    intent = (
        V5Intent.RECOMMEND
        if referenced_dishes
        else V5Intent.LIST if dish_discovery else draft.intent
    )
    cities = [
        *_catalog_mentions(query, catalog["cities"]),
        *(_canonical_city(value, catalog["cities"]) for value in draft.geo_scope.cities),
    ]
    mentioned_areas = _catalog_mentions(query, catalog["areas"])
    areas = [
        *mentioned_areas,
        *(
            _canonical_value(value, catalog["areas"], "area")
            for value in draft.geo_scope.areas
        ),
    ]
    concepts = set(catalog["concepts"])
    concepts_by_slug = {slugify(item): item for item in concepts}
    required = [_canonical_or_raw(value, concepts_by_slug) for value in draft.required_concepts]
    soft_preferences = [
        value for value in required if slugify(value) in {"ngon"}
    ]
    required = [value for value in required if value not in soft_preferences]
    preferred = [
        value
        for value in (
            _canonical_or_raw(item, concepts_by_slug)
            for item in [*draft.preferred_concepts, *soft_preferences]
        )
        if value not in required
    ]
    if dish_discovery or referenced_dishes:
        required = []
        preferred = (
            ["ngon"]
            if dish_discovery and "ngon" in slugify(query).replace("-", " ")
            else []
        )
    unknown_fields = set(draft.requested_fields) - V3_PREDICATES
    if unknown_fields:
        raise ValueError(f"Unknown requested fields: {sorted(unknown_fields)}")
    if dish_discovery:
        targets = [QueryTarget(kind=TargetKind.DISH)]
    elif referenced_dishes:
        targets = [
            QueryTarget(kind=TargetKind.DISH, value=dish)
            for dish in referenced_dishes
        ]
    else:
        targets = [
            QueryTarget(
                kind=target.kind,
                value=_target_value(target, catalog),
                entity_types=_unique_lower(target.entity_types),
            )
            for target in draft.targets
        ]
    if not targets and mentioned_areas:
        targets = [
            QueryTarget(kind=TargetKind.GEO_AREA, value=area)
            for area in mentioned_areas
        ]
    targets = _repair_candidate_targets(
        query=query,
        intent=intent,
        targets=targets,
        cities=cities,
    )
    return V5QueryPlan(
        intent=intent,
        targets=targets,
        geo_scope=GeoScope(
            cities=_unique(cities),
            areas=_unique(areas),
            near_entities=_unique(draft.geo_scope.near_entities),
        ),
        requested_fields=_unique_lower(draft.requested_fields),
        required_concepts=_unique(required),
        preferred_concepts=_unique(preferred),
        ranking_criteria=list(dict.fromkeys(draft.ranking_criteria)),
        constraints=draft.constraints,
        duration_days=draft.duration_days,
        limit=draft.limit,
        required_tools=_unique_lower(draft.required_tools),
        clarification_needed=(
            draft.clarification_needed
            and not _has_actionable_scope(targets, cities, areas)
        ),
        confidence=draft.confidence,
    )


def _target_value(target: TargetDraft, catalog: dict[str, list[str]]) -> str | None:
    if target.value is None:
        return None
    if target.kind == TargetKind.CITY:
        return _canonical_city(target.value, catalog["cities"])
    if target.kind == TargetKind.GEO_AREA:
        return _canonical_value(target.value, catalog["areas"], "area")
    if target.kind in {TargetKind.DISH, TargetKind.ACTIVITY, TargetKind.CONCEPT}:
        return _canonical_value(target.value, catalog["concepts"], "concept")
    return target.value.strip()


def _repair_candidate_targets(
    *,
    query: str,
    intent: V5Intent,
    targets: list[QueryTarget],
    cities: list[str],
) -> list[QueryTarget]:
    """Repair incomplete Gemini candidate targets without bypassing Gemini planning."""
    if intent not in {V5Intent.RECOMMEND, V5Intent.PLAN_CANDIDATES}:
        return targets

    inferred_types = _candidate_entity_types(query)
    place_targets = [target for target in targets if target.kind == TargetKind.PLACE]
    if place_targets:
        return [
            target.model_copy(update={"entity_types": inferred_types})
            if target.kind == TargetKind.PLACE and not target.entity_types
            else target
            for target in targets
        ]

    if any(
        target.kind in {TargetKind.DISH, TargetKind.ACTIVITY, TargetKind.CONCEPT}
        for target in targets
    ):
        return targets

    has_geo_target = any(
        target.kind in {TargetKind.CITY, TargetKind.GEO_AREA}
        for target in targets
    )
    if not cities and not has_geo_target:
        return targets

    scoped_targets = [target for target in targets if target.kind != TargetKind.CITY]
    return [
        *scoped_targets,
        QueryTarget(kind=TargetKind.PLACE, entity_types=inferred_types),
    ]


def _candidate_entity_types(query: str) -> list[str]:
    normalized = slugify(query).replace("-", " ")
    terms_by_type = {
        "hotel": ("khach san", "nha nghi", "luu tru", "resort"),
        "restaurant": ("nha hang", "quan an", "an gi", "am thuc"),
        "cafe": ("cafe", "ca phe", "coffee"),
        "nightlife": ("nightlife", "quan bar", "bar", "ve dem"),
        "attraction": (
            "canh dep",
            "di dau",
            "di choi",
            "diem den",
            "kham pha",
            "tham quan",
            "trai nghiem",
        ),
    }
    matches = [
        entity_type
        for entity_type, terms in terms_by_type.items()
        if any(term in normalized for term in terms)
    ]
    return matches or ["attraction"]


def _is_dish_discovery_query(query: str) -> bool:
    normalized = slugify(query).replace("-", " ")
    dish_terms = ("dac san", "mon an", "mon gi", "mon ngon", "mon nao")
    venue_terms = ("dia chi", "diem ban", "nha hang", "noi ban", "o dau", "quan")
    return any(term in normalized for term in dish_terms) and not any(
        term in normalized for term in venue_terms
    )


def _referenced_dish_mentions(query: str, concepts: list[str]) -> list[str]:
    normalized = slugify(query)
    marker = "cac-mon-duoc-nhac-den-o-luot-truoc"
    if marker not in normalized:
        return []
    return _catalog_mentions(query, concepts)


def _has_actionable_scope(
    targets: list[QueryTarget],
    cities: list[str],
    areas: list[str],
) -> bool:
    has_scope = bool(cities or areas) or any(
        target.kind in {TargetKind.CITY, TargetKind.GEO_AREA}
        for target in targets
    )
    has_candidate = any(
        (target.kind == TargetKind.PLACE and bool(target.entity_types))
        or (target.kind != TargetKind.PLACE and bool(target.value))
        for target in targets
    )
    return has_scope and has_candidate


def _canonical_city(value: str, allowed: list[str]) -> str:
    canonical = canonical_city(value)
    return _canonical_value(canonical, allowed, "city")


def _canonical_value(value: str, allowed: list[str] | set[str], label: str) -> str:
    by_slug = {slugify(item): item for item in allowed}
    canonical = by_slug.get(slugify(value))
    if canonical is None:
        raise ValueError(f"Unknown {label}: {value}")
    return canonical


def _canonical_or_raw(value: str, allowed_by_slug: dict[str, str]) -> str:
    return allowed_by_slug.get(slugify(value), value.strip())


def _catalog_mentions(query: str, allowed: list[str]) -> list[str]:
    query_slug = f"-{slugify(query)}-"
    matches: list[str] = []
    for value in sorted(allowed, key=lambda item: len(slugify(item)), reverse=True):
        value_slug = slugify(value)
        if f"-{value_slug}-" not in query_slug:
            continue
        if any(f"-{value_slug}-" in f"-{slugify(match)}-" for match in matches):
            continue
        matches.append(value)
    return matches


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_lower(values: list[str]) -> list[str]:
    return _unique([value.casefold() for value in values])


def _user_prompt(query: str, catalog: dict[str, list[str]]) -> str:
    graph_vocabulary = {
        key: catalog[key]
        for key in ("cities", "areas", "concepts")
    }
    return json.dumps(
        {"query": query, "graph_vocabulary": graph_vocabulary},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unavailable_plan() -> V5QueryPlan:
    return V5QueryPlan(intent=V5Intent.UNSUPPORTED, confidence=0)
