from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ...normalizer import slugify
from ..v2.schemas import ENTITY_TYPES
from ..v3.schemas import V3_PREDICATES
from ..v4.query_planner import StructuredPlanner
from ..v4.schemas import RankingCriterion, V4Constraint
from ..v5.schemas import (
    GeoScope,
    QueryTarget,
    QueryTask,
    TargetKind,
    V5Intent,
    V5QueryPlan,
)


SYSTEM_INSTRUCTION = """You are the semantic query planner for NexTripAI GraphRAG V8.
Return only JSON matching the schema. Never emit Cypher or prose.
Choose one canonical intent and preserve raw user meaning for grounding.
The user_query may contain a JSON object with current_message and
conversation_context. Treat current_message as the new request and use the
explicit context fields only to resolve omitted city, duration, or entity type.
Use only canonical values listed in graph_contract; do not invent IDs.
Use aggregate for counts, plan_candidates for itinerary candidates, and
tool_required for live weather, price, traffic, booking, or availability.
For recommendations and lists, use a place target and provide canonical
entity_types when the user specifies a venue type. Put cities and areas in
geo_scope, never as a place name. Keep uncertain semantic phrases as concepts;
the application resolves them against graph concepts and evidence chunks.
For plan_candidates, create one place target per venue type (attraction,
restaurant, cafe, hotel, or nightlife). Do not encode a venue type as an
activity/dish/concept target; those kinds are reserved for concept discovery.
Use constraints for numeric requirements: distance_to_beach_max and
distance_to_center_max are kilometres, party_size is the number of travellers.
If the request cannot be grounded safely, set clarification_needed.
"""


class V8TargetDraft(BaseModel):
    kind: str = Field(
        description=(
            "place for venue recommendations; city or geo_area for scope; "
            "activity/dish/concept only for concept discovery"
        )
    )
    value: str | None = None
    entity_types: list[str] = Field(default_factory=list)


class V8GeoScopeDraft(BaseModel):
    cities: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    near_entities: list[str] = Field(default_factory=list)


class V8ConstraintDraft(BaseModel):
    field: str = Field(
        description=(
            "Canonical constraint field. Use distance_to_beach_max, "
            "distance_to_center_max, or party_size for numeric travel requirements."
        )
    )
    value: str | int | float | bool | None = None
    mode: str = "hard"


class V8TaskDraft(BaseModel):
    name: str = "task"
    intent: str = "plan_candidates"
    targets: list[V8TargetDraft] = Field(default_factory=list)
    geo_scope: V8GeoScopeDraft = Field(default_factory=V8GeoScopeDraft)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[str] = Field(default_factory=list)
    constraints: list[V8ConstraintDraft] = Field(default_factory=list)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)


class V8PlannerDraft(BaseModel):
    """LLM-facing schema intentionally uses strings for recoverable enums.

    Gemini structured output is still constrained to objects, arrays, strings,
    numbers and booleans, but a single spelling variation must not discard the
    whole plan before graph grounding can repair it.
    """

    intent: str
    targets: list[V8TargetDraft] = Field(default_factory=list)
    geo_scope: V8GeoScopeDraft = Field(default_factory=V8GeoScopeDraft)
    requested_fields: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[str] = Field(default_factory=list)
    constraints: list[V8ConstraintDraft] = Field(default_factory=list)
    tasks: list[V8TaskDraft] = Field(default_factory=list)
    duration_days: int | None = Field(default=None, ge=1, le=30)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    required_tools: list[str] = Field(default_factory=list)
    clarification_needed: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)


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

    prompt = _user_prompt(query, catalog)
    try:
        draft = gemini.generate_structured(
            SYSTEM_INSTRUCTION,
            prompt,
            V8PlannerDraft,
        )
        plan = _compile_plan(draft, catalog)
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning(
            "V8 planner normalization failed; retrying with a repair prompt "
            "error_type={} error={} query_length={}",
            exc.__class__.__name__,
            str(exc),
            len(query),
        )
        try:
            repaired = gemini.generate_structured(
                _repair_instruction(exc),
                prompt,
                V8PlannerDraft,
            )
            plan = _compile_plan(repaired, catalog)
        except (ValidationError, ValueError, TypeError) as repair_exc:
            logger.warning(
                "V8 planner repair failed error_type={} error={} query_length={}",
                repair_exc.__class__.__name__,
                str(repair_exc),
                len(query),
            )
            return _unavailable_plan(), "planner_invalid", PlannerFailure(
                code="invalid_plan",
                reason=repair_exc.__class__.__name__,
                retryable=False,
            )
        except Exception as repair_exc:
            return _unavailable_plan(), "planner_unavailable", PlannerFailure(
                code="planner_unavailable",
                reason=_error_reason(repair_exc),
                retryable=True,
            )
        planner_name = "gemini_semantic_v8_repair"
    except Exception as exc:
        logger.warning(
            "V8 planner unavailable error_type={} query_length={}",
            exc.__class__.__name__,
            len(query),
        )
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="planner_unavailable",
            reason=_error_reason(exc),
            retryable=True,
        )
    else:
        planner_name = "gemini_semantic_v8"

    logger.info("V8 planner output plan={}", plan.model_dump_json())
    return plan, planner_name, None


def _compile_plan(
    draft: V8PlannerDraft,
    catalog: dict[str, list[str]],
) -> V5QueryPlan:
    intent = _enum_value(draft.intent, V5Intent)
    targets = [_compile_target(raw) for raw in draft.targets]
    if intent == V5Intent.LOOKUP:
        named_places = [
            target
            for target in targets
            if target.kind == TargetKind.PLACE and target.value
        ]
        if len(named_places) >= 2:
            intent = V5Intent.COMPARE

    required_concepts = _clean_values(draft.required_concepts)
    preferred_concepts = _clean_values(draft.preferred_concepts)
    if intent in {
        V5Intent.LIST,
        V5Intent.RECOMMEND,
        V5Intent.PLAN_CANDIDATES,
    }:
        place_targets = [
            target
            for target in targets
            if target.kind == TargetKind.PLACE
        ]
        if place_targets:
            supplemental_concepts = [
                target.value
                for target in targets
                if target.kind in {
                    TargetKind.ACTIVITY,
                    TargetKind.DISH,
                    TargetKind.CONCEPT,
                }
                and target.value
            ]
            targets = place_targets
            preferred_concepts = _clean_values(
                [
                    *preferred_concepts,
                    *(
                        value
                        for value in supplemental_concepts
                        if value not in required_concepts
                    ),
                ]
            )

    if (
        draft.duration_days
        and intent in {V5Intent.LIST, V5Intent.RECOMMEND}
        and any(target.kind == TargetKind.PLACE for target in targets)
    ):
        intent = V5Intent.PLAN_CANDIDATES
    cities = _clean_values(draft.geo_scope.cities)
    areas = _clean_values(draft.geo_scope.areas)
    near_entities = _clean_values(draft.geo_scope.near_entities)
    requested = _canonical_values(draft.requested_fields, V3_PREDICATES)
    ranking = [
        _enum_value(value, RankingCriterion)
        for value in draft.ranking_criteria
        if _is_enum_value(value, RankingCriterion)
    ]
    tools = _clean_values(draft.required_tools)
    constraints = _compile_constraints(draft.constraints)
    tasks = [_compile_task(raw) for raw in draft.tasks]
    if (
        intent == V5Intent.PLAN_CANDIDATES
        and targets
        and any(target.kind != TargetKind.PLACE for target in targets)
        and not tasks
    ):
        raise ValueError(
            "plan_candidates accepts place targets only, unless explicit tasks are supplied"
        )

    return V5QueryPlan(
        intent=intent,
        targets=targets,
        geo_scope=GeoScope(
            cities=cities,
            areas=areas,
            near_entities=near_entities,
        ),
        requested_fields=requested,
        required_concepts=required_concepts,
        preferred_concepts=preferred_concepts,
        ranking_criteria=list(dict.fromkeys(ranking)),
        constraints=constraints,
        tasks=tasks,
        duration_days=draft.duration_days,
        limit=draft.limit,
        required_tools=tools,
        clarification_needed=draft.clarification_needed,
        confidence=draft.confidence,
    )


def _compile_task(raw: V8TaskDraft) -> QueryTask:
    intent = _enum_value(raw.intent, V5Intent)
    ranking = [
        _enum_value(value, RankingCriterion)
        for value in raw.ranking_criteria
        if _is_enum_value(value, RankingCriterion)
    ]
    return QueryTask(
        name=str(raw.name or "task").strip()[:80] or "task",
        intent=intent,
        targets=[_compile_target(target) for target in raw.targets],
        geo_scope=GeoScope(
            cities=_clean_values(raw.geo_scope.cities),
            areas=_clean_values(raw.geo_scope.areas),
            near_entities=_clean_values(raw.geo_scope.near_entities),
        ),
        required_concepts=_clean_values(raw.required_concepts),
        preferred_concepts=_clean_values(raw.preferred_concepts),
        ranking_criteria=list(dict.fromkeys(ranking)),
        constraints=_compile_constraints(raw.constraints),
        limit=raw.limit,
    )


def _compile_target(raw: V8TargetDraft) -> QueryTarget:
    kind = _enum_value(raw.kind, TargetKind)
    value = raw.value
    if value is not None and not isinstance(value, str):
        value = str(value)
    entity_types = _canonical_values(raw.entity_types, ENTITY_TYPES)
    if kind != TargetKind.PLACE:
        entity_types = []
    return QueryTarget(
        kind=kind,
        value=value.strip() if isinstance(value, str) and value.strip() else None,
        entity_types=entity_types,
    )


def _compile_constraints(raw_constraints: list[V8ConstraintDraft]) -> list[V4Constraint]:
    constraints: list[V4Constraint] = []
    for raw in raw_constraints:
        try:
            constraints.append(V4Constraint.model_validate(raw.model_dump()))
        except ValidationError:
            # Optional malformed constraints must not erase a valid plan. The
            # graph executor remains conservative and simply does not apply it.
            continue
    return constraints


def _canonical_values(values: Any, allowed: set[str] | list[str] | None = None) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    if allowed is None:
        return list(dict.fromkeys(cleaned))
    by_slug = {slugify(value): value for value in allowed}
    return list(dict.fromkeys(by_slug[slugify(value)] for value in cleaned if slugify(value) in by_slug))


def _enum_value(value: Any, enum_type: Any) -> Any:
    normalized = slugify(str(value or "")).replace("-", "_")
    for item in enum_type:
        if normalized in {
            slugify(item.value).replace("-", "_"),
            slugify(item.name).replace("-", "_"),
        }:
            return item
    raise ValueError(f"Unsupported {enum_type.__name__}: {value}")


def _is_enum_value(value: Any, enum_type: Any) -> bool:
    try:
        _enum_value(value, enum_type)
        return True
    except ValueError:
        return False


def _clean_values(values: Any) -> list[str]:
    return _canonical_values(values)


def _user_prompt(query: str, catalog: dict[str, list[str]]) -> str:
    return json.dumps(
        {
            "user_query": query,
            "graph_contract": {
                "known_cities": catalog.get("cities", []),
                "known_geo_areas": catalog.get("areas", []),
                "known_concepts": catalog.get("concepts", []),
                "entity_types": sorted(ENTITY_TYPES),
                "requested_fields": sorted(V3_PREDICATES),
                "ranking_criteria": [item.value for item in RankingCriterion],
                "intents": [item.value for item in V5Intent],
                "target_kinds": [item.value for item in TargetKind],
                "constraint_fields": [
                    "budget_max",
                    "category",
                    "distance_to_beach_max",
                    "distance_to_center_max",
                    "indoor",
                    "near_subject",
                    "open_24h",
                    "party_size",
                    "star_rating",
                    "weather",
                ],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _repair_instruction(exc: Exception) -> str:
    return (
        SYSTEM_INSTRUCTION
        + "\nA previous JSON plan could not be normalized: "
        + f"{exc.__class__.__name__}: {exc}. "
        + "Return canonical enum values exactly as listed in graph_contract; "
        + "omit uncertain optional constraints instead of inventing values."
    )


def _unavailable_plan() -> V5QueryPlan:
    return V5QueryPlan(
        intent=V5Intent.UNSUPPORTED,
        clarification_needed=True,
        confidence=0.0,
    )


def _error_reason(exc: Exception) -> str:
    message = str(exc).upper()
    if "BILLING_DISABLED" in message:
        return "GeminiBillingDisabled"
    if "PERMISSION_DENIED" in message:
        return "GeminiPermissionDenied"
    if "RESOURCE_EXHAUSTED" in message:
        return "GeminiQuotaExceeded"
    return exc.__class__.__name__


__all__ = ["V8PlannerDraft", "PlannerFailure", "plan_query"]
