from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeVar

from google.genai.errors import APIError
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ...normalizer import slugify
from ..v4.query_planner import StructuredPlanner
from ..v4.schemas import ConstraintMode, RankingCriterion, V4Constraint
from ..v5.schemas import (
    GeoScope,
    QueryTarget,
    QueryTask,
    TargetKind,
    V5Intent,
)
from ..v7.query_planner import EntityType, RequestedField, ToolKind
from .schemas import EntityMention, MentionKind, MentionRole, V8QueryPlan


EnumValue = TypeVar("EnumValue", bound=StrEnum)


SYSTEM_INSTRUCTION = """You are the semantic query planner for NexTripAI GraphRAG V8.
Return only JSON matching the schema. Never emit Cypher or prose.
Choose one canonical intent and preserve raw user meaning for grounding.
First extract contiguous named venue or brand spans into entity_mentions. A word
inside a named span must not be reinterpreted as a requested field, category, or
preference. For example, an unknown proper name remains one venue_or_brand
target even when part of its name resembles a field. Do not require a named
venue or brand lookup to include a city; the graph grounding stage resolves it.
The user_query may contain a JSON object with current_message and
conversation_context. Treat current_message as the new request and use the
explicit context fields only to resolve omitted city, duration, or entity type.
When conversation_context.personalization exists, treat its preferred_concepts,
budget_level, party_type, and travel_pace only as ranking preferences. Explicit
requirements in current_message always win. excluded_concepts may be ignored
only when current_message explicitly requests the same concept.
When conversation_context.resolved_query is present, use it only to resolve an
omitted reference in current_message. Never let inherited context split or
replace a complete named venue/brand span in current_message. city_source tells
whether a city is explicit or inherited; inherited scope is a preference, not
proof that a named entity must belong to that city.
Use only canonical values listed in graph_contract; do not invent IDs.
Use aggregate for counts, plan_candidates for itinerary candidates, and
tool_required for live weather, price, traffic, booking, or availability.
For recommendations and lists, use a place target and provide canonical
entity_types when the user specifies a venue type. Put cities and areas in
geo_scope, never as a place name. Keep uncertain semantic phrases as concepts;
the application resolves them against graph concepts and evidence chunks.
A generic venue type followed by qualities or preferences is not a venue or
brand name. For example, "a highly rated quiet cafe" is a recommendation with
a cafe target whose value is null, not a named-place lookup. Reserve
venue_name, brand_name, and venue_or_brand mentions for proper names.
For plan_candidates, create one place target per venue type (attraction,
restaurant, cafe, hotel, or nightlife). Do not encode a venue type as an
activity/dish/concept target; those kinds are reserved for concept discovery.
Use only hard constraints for explicit requirements. Numeric duration such as
"2 days" belongs in duration_days and must never become party_size. Use
distance_to_beach_max and distance_to_center_max in kilometres; party_size is
only the explicitly stated number of travellers. Put preferences in
preferred_concepts instead of soft constraints.
If the request cannot be grounded safely, set clarification_needed.
"""


class V8TargetDraft(BaseModel):
    kind: TargetKind = Field(
        description=(
            "place for venue recommendations; city or geo_area for scope; "
            "activity/dish/concept only for concept discovery"
        )
    )
    value: str | None = None
    entity_types: list[EntityType] = Field(default_factory=list)


class V8GeoScopeDraft(BaseModel):
    cities: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    near_entities: list[str] = Field(default_factory=list)


class V8ConstraintDraft(BaseModel):
    """Lenient LLM boundary; invalid preferences are dropped at compilation."""

    field: Literal[
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
    ]
    value: str | float | int | bool
    mode: ConstraintMode = ConstraintMode.HARD


class V8TaskDraft(BaseModel):
    name: str = Field(default="task", min_length=1, max_length=80)
    intent: V5Intent = V5Intent.PLAN_CANDIDATES
    targets: list[V8TargetDraft] = Field(default_factory=list)
    geo_scope: V8GeoScopeDraft = Field(default_factory=V8GeoScopeDraft)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V8ConstraintDraft] = Field(default_factory=list)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)


class V8PlannerDraft(BaseModel):
    """Strict LLM-facing contract consumed by Gemini structured output."""

    intent: V5Intent
    targets: list[V8TargetDraft] = Field(default_factory=list)
    geo_scope: V8GeoScopeDraft = Field(default_factory=V8GeoScopeDraft)
    requested_fields: list[RequestedField] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V8ConstraintDraft] = Field(default_factory=list)
    tasks: list[V8TaskDraft] = Field(default_factory=list)
    entity_mentions: list[EntityMention] = Field(
        default_factory=list,
        description=(
            "Contiguous semantic spans extracted before choosing intent. "
            "Keep a venue or brand name intact even when it is not in the catalog."
        ),
    )
    duration_days: int | None = Field(default=None, ge=1, le=30)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    required_tools: list[ToolKind] = Field(default_factory=list)
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
) -> tuple[V8QueryPlan, str, PlannerFailure | None]:
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
        plan = _compile_plan(draft)
        plan = _restore_exact_catalog_place(plan, query, catalog)
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning(
            "V8 planner violated structured graph contract "
            "error_type={} error={} query_length={}",
            exc.__class__.__name__,
            str(exc),
            len(query),
        )
        return _unavailable_plan(), "planner_invalid", PlannerFailure(
            code="invalid_plan",
            reason=exc.__class__.__name__,
            retryable=False,
        )
    except Exception as exc:
        failure = _planner_failure(exc)
        logger.warning(
            "V8 planner unavailable reason={} query_length={}",
            failure.reason,
            len(query),
        )
        return _unavailable_plan(), "planner_unavailable", failure

    logger.info("V8 planner output plan={}", plan.model_dump_json())
    return plan, "gemini_semantic_v8", None


def _compile_plan(
    draft: V8PlannerDraft,
) -> V8QueryPlan:
    intent = draft.intent
    targets = [_compile_target(raw) for raw in draft.targets]
    mentions = _compile_mentions(draft.entity_mentions)
    named_mentions = [
        mention
        for mention in mentions
        if mention.role == MentionRole.TARGET
        and mention.kind
        in {
            MentionKind.VENUE_NAME,
            MentionKind.BRAND_NAME,
            MentionKind.VENUE_OR_BRAND,
        }
    ]
    if named_mentions:
        inferred_types = list(
            dict.fromkeys(
                entity_type
                for target in targets
                if target.kind == TargetKind.PLACE
                for entity_type in target.entity_types
            )
        )
        targets = [
            QueryTarget(
                kind=TargetKind.PLACE,
                value=mention.surface,
                entity_types=inferred_types,
            )
            for mention in named_mentions
        ]
        if intent in {
            V5Intent.LOOKUP,
            V5Intent.PROFILE,
            V5Intent.LIST,
            V5Intent.RECOMMEND,
        }:
            intent = (
                V5Intent.COMPARE
                if len(targets) >= 2
                else V5Intent.LOOKUP
            )
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
        if place_targets or (
            supplemental_concepts
            and intent in {V5Intent.RECOMMEND, V5Intent.PLAN_CANDIDATES}
        ):
            # Recommendation targets are places. Semantic phrases are ranking
            # hints, not graph entities that the user must clarify. An untyped
            # place target searches across the catalog when no venue type was
            # supplied by the planner.
            targets = place_targets or [QueryTarget(kind=TargetKind.PLACE)]
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
    mentioned_cities = [
        mention.surface
        for mention in mentions
        if mention.role == MentionRole.SCOPE and mention.kind == MentionKind.CITY
    ]
    cities = _clean_values([*draft.geo_scope.cities, *mentioned_cities])
    areas = _clean_values(draft.geo_scope.areas)
    near_entities = _clean_values(draft.geo_scope.near_entities)
    requested = _enum_values(draft.requested_fields)
    ranking = list(dict.fromkeys(draft.ranking_criteria))
    tools = _enum_values(draft.required_tools)
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

    return V8QueryPlan(
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
        ranking_criteria=ranking,
        constraints=_compile_constraints(draft.constraints),
        tasks=tasks,
        entity_mentions=mentions,
        duration_days=draft.duration_days,
        limit=draft.limit,
        required_tools=tools,
        clarification_needed=(
            False
            if named_mentions
            and intent in {V5Intent.LOOKUP, V5Intent.PROFILE, V5Intent.COMPARE}
            else draft.clarification_needed
        ),
        confidence=draft.confidence,
    )


def _restore_exact_catalog_place(
    plan: V8QueryPlan,
    planner_input: str,
    catalog: dict[str, list[str]],
) -> V8QueryPlan:
    """Recover a venue that the semantic planner mistook for generic scope.

    The graph catalog remains the authority. This only repairs plans with no
    place target and requires the complete canonical venue name to occur in
    the current user turn.
    """
    if plan.intent not in {
        V5Intent.LOOKUP,
        V5Intent.PROFILE,
        V5Intent.LIST,
        V5Intent.RECOMMEND,
        V5Intent.SUMMARIZE,
    }:
        return plan
    if (
        any(target.kind == TargetKind.PLACE for target in plan.targets)
        or plan.geo_scope.near_entities
        or any(
            constraint.field == "near_subject"
            for constraint in plan.constraints
        )
    ):
        return plan

    message = _current_message(planner_input)
    normalized_message = slugify(message)
    if not normalized_message:
        return plan

    padded_message = f"-{normalized_message}-"
    matches = []
    for place in catalog.get("places", []):
        normalized_place = slugify(place)
        if len(normalized_place) < 4:
            continue
        if (
            normalized_message == normalized_place
            or f"-{normalized_place}-" in padded_message
        ):
            matches.append((len(normalized_place), place))
    if not matches:
        return plan

    _, canonical_place = max(matches)
    explicit_city = any(
        f"-{slugify(city)}-" in padded_message
        for city in catalog.get("cities", [])
        if slugify(city)
    )
    scope = plan.geo_scope
    if not explicit_city:
        scope = scope.model_copy(update={"cities": []})

    intent = plan.intent
    if intent in {
        V5Intent.LIST,
        V5Intent.PROFILE,
        V5Intent.RECOMMEND,
        V5Intent.SUMMARIZE,
    }:
        intent = V5Intent.LOOKUP
    return plan.model_copy(
        update={
            "intent": intent,
            "targets": [
                QueryTarget(
                    kind=TargetKind.PLACE,
                    value=canonical_place,
                )
            ],
            "geo_scope": scope,
            "entity_mentions": [
                *plan.entity_mentions,
                EntityMention(
                    surface=canonical_place,
                    role=MentionRole.TARGET,
                    kind=MentionKind.VENUE_OR_BRAND,
                    confidence=1.0,
                ),
            ],
            "clarification_needed": False,
        }
    )


def _current_message(planner_input: str) -> str:
    try:
        payload = json.loads(planner_input)
    except json.JSONDecodeError:
        return planner_input
    if not isinstance(payload, dict):
        return planner_input
    current = payload.get("current_message")
    return current if isinstance(current, str) else planner_input


def _compile_task(raw: V8TaskDraft) -> QueryTask:
    return QueryTask(
        name=raw.name.strip(),
        intent=raw.intent,
        targets=[_compile_target(target) for target in raw.targets],
        geo_scope=GeoScope(
            cities=_clean_values(raw.geo_scope.cities),
            areas=_clean_values(raw.geo_scope.areas),
            near_entities=_clean_values(raw.geo_scope.near_entities),
        ),
        required_concepts=_clean_values(raw.required_concepts),
        preferred_concepts=_clean_values(raw.preferred_concepts),
        ranking_criteria=list(dict.fromkeys(raw.ranking_criteria)),
        constraints=_compile_constraints(raw.constraints),
        limit=raw.limit,
    )


def _compile_target(raw: V8TargetDraft) -> QueryTarget:
    entity_types = _enum_values(raw.entity_types)
    if raw.kind != TargetKind.PLACE:
        entity_types = []
    return QueryTarget(
        kind=raw.kind,
        value=_clean(raw.value),
        entity_types=entity_types,
    )


def _compile_constraints(values: list[V8ConstraintDraft]) -> list[V4Constraint]:
    """Keep only constraints the deterministic V4 executor can enforce."""

    constraints: list[V4Constraint] = []
    for value in values:
        if value.mode != ConstraintMode.HARD:
            continue
        try:
            constraints.append(
                V4Constraint.model_validate(value.model_dump(mode="python"))
            )
        except ValidationError as exc:
            logger.info(
                "Ignoring invalid planner constraint field={} reason={}",
                value.field,
                exc.__class__.__name__,
            )
    return constraints


def _compile_mentions(values: list[EntityMention]) -> list[EntityMention]:
    mentions: list[EntityMention] = []
    seen: set[tuple[str, MentionRole, MentionKind]] = set()
    for mention in values:
        surface = mention.surface.strip()
        if not surface:
            continue
        key = (surface.casefold(), mention.role, mention.kind)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(
            EntityMention(
                surface=surface,
                role=mention.role,
                kind=mention.kind,
                confidence=mention.confidence,
            )
        )
    return mentions


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned


def _clean_values(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _enum_values(values: list[EnumValue]) -> list[str]:
    return list(dict.fromkeys(value.value for value in values))


def _user_prompt(query: str, catalog: dict[str, list[str]]) -> str:
    return json.dumps(
        {
            "user_query": query,
            "graph_contract": {
                "known_cities": catalog.get("cities", []),
                "known_geo_areas": catalog.get("areas", []),
                "known_concepts": catalog.get("concepts", []),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unavailable_plan() -> V8QueryPlan:
    return V8QueryPlan(
        intent=V5Intent.UNSUPPORTED,
        clarification_needed=True,
        confidence=0.0,
    )


def _planner_failure(exc: Exception) -> PlannerFailure:
    if not isinstance(exc, APIError):
        return PlannerFailure(
            code="planner_unavailable",
            reason=exc.__class__.__name__,
            retryable=True,
        )
    reasons = {
        "PERMISSION_DENIED": "GeminiPermissionDenied",
        "RESOURCE_EXHAUSTED": "GeminiQuotaExceeded",
        "FAILED_PRECONDITION": "GeminiFailedPrecondition",
    }
    status = exc.status if exc.status is not None else ""
    return PlannerFailure(
        code="planner_unavailable",
        reason=reasons.get(status, f"GeminiHTTP{exc.code}"),
        retryable=exc.code == 429 or exc.code >= 500,
    )


__all__ = ["V8PlannerDraft", "PlannerFailure", "plan_query"]
