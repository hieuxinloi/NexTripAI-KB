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
    except Exception as exc:
        logger.warning(
            "V5 planner unavailable error_type={} query_length={}",
            exc.__class__.__name__,
            len(query),
        )
        fallback = _catalog_fallback_plan(query, catalog)
        if fallback is not None:
            return fallback, "catalog_fallback", None
        invalid_plan = isinstance(exc, (ValueError, ValidationError))
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="invalid_plan" if invalid_plan else "planner_unavailable",
            reason=exc.__class__.__name__,
            retryable=not invalid_plan,
        )


def _compile_plan(
    draft: V5PlannerDraft,
    catalog: dict[str, list[str]],
    query: str,
) -> V5QueryPlan:
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
    preferred = [
        value
        for value in (
            _canonical_or_raw(item, concepts_by_slug)
            for item in draft.preferred_concepts
        )
        if value not in required
    ]
    unknown_fields = set(draft.requested_fields) - V3_PREDICATES
    if unknown_fields:
        raise ValueError(f"Unknown requested fields: {sorted(unknown_fields)}")
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
    return V5QueryPlan(
        intent=draft.intent,
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
        clarification_needed=draft.clarification_needed,
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


def _catalog_fallback_plan(
    query: str,
    catalog: dict[str, list[str]],
) -> V5QueryPlan | None:
    areas = _catalog_mentions(query, catalog["areas"])
    if not areas:
        return None
    return V5QueryPlan(
        intent=V5Intent.SUMMARIZE,
        targets=[
            QueryTarget(kind=TargetKind.GEO_AREA, value=area)
            for area in areas
        ],
        geo_scope=GeoScope(areas=areas),
        confidence=1.0,
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_lower(values: list[str]) -> list[str]:
    return _unique([value.casefold() for value in values])


def _user_prompt(query: str, catalog: dict[str, list[str]]) -> str:
    return json.dumps(
        {"query": query, "graph_vocabulary": catalog},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unavailable_plan() -> V5QueryPlan:
    return V5QueryPlan(intent=V5Intent.UNSUPPORTED, confidence=0)
