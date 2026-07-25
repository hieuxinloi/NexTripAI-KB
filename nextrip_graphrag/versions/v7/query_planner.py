from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ..v3.schemas import V3_PREDICATES
from ..v4.schemas import RankingCriterion, V4Constraint
from ..v5.schemas import GeoScope, QueryTarget, TargetKind, V5Intent, V5QueryPlan


SYSTEM_INSTRUCTION = """You are the semantic query planner for NexTripAI GraphRAG V7.
Interpret the user's meaning instead of matching phrases or templates.
Return only the requested structured schema. Never emit Cypher or prose.

Intent contract:
- lookup: requested fields for one named graph entity.
- profile: broad information about one named graph entity.
- list: an unranked set of graph entities.
- recommend: ranked candidates subject to preferences or constraints.
- compare: compare at least two named graph entities.
- summarize: broad information about a city or geographic area.
- aggregate: a count over typed graph entities.
- plan_candidates: candidates for a multi-stop itinerary.
- tool_required: current or external data that the graph cannot guarantee.
- unsupported: outside the travel knowledge-base scope.

Preserve named entities and semantic requirements in the user's own words.
Do not invent a canonical graph node ID. A separate grounding stage will resolve
raw mentions to candidate graph nodes. Use only entity types, requested fields,
ranking criteria, constraint fields, and tool names allowed by the schema.
If the meaning or referent is materially ambiguous, set clarification_needed.
"""


class EntityType(StrEnum):
    ATTRACTION = "attraction"
    CAFE = "cafe"
    HOTEL = "hotel"
    NIGHTLIFE = "nightlife"
    RESTAURANT = "restaurant"


class ToolKind(StrEnum):
    BOOKING = "booking"
    LIVE_PRICE = "live_price"
    LIVE_STATUS = "live_status"
    ROUTE = "route"
    TRANSPORT = "transport"
    WEATHER = "weather"


RequestedField = StrEnum(
    "RequestedField",
    {value.upper(): value for value in sorted(V3_PREDICATES)},
)


class TargetDraft(BaseModel):
    kind: TargetKind
    value: str | None = Field(
        default=None,
        description="Raw named entity or concept mention from the user.",
    )
    entity_types: list[EntityType] = Field(default_factory=list)


class GeoScopeDraft(BaseModel):
    cities: list[str] = Field(
        default_factory=list,
        description="Raw city mentions from the user.",
    )
    areas: list[str] = Field(
        default_factory=list,
        description="Raw district, ward, beach, or local-area mentions.",
    )
    near_entities: list[str] = Field(
        default_factory=list,
        description="Raw named-place anchors for proximity.",
    )


class V7PlannerDraft(BaseModel):
    intent: V5Intent
    targets: list[TargetDraft] = Field(default_factory=list)
    geo_scope: GeoScopeDraft = Field(default_factory=GeoScopeDraft)
    requested_fields: list[RequestedField] = Field(default_factory=list)
    required_concepts: list[str] = Field(
        default_factory=list,
        description="Hard semantic requirements in the user's own words.",
    )
    preferred_concepts: list[str] = Field(
        default_factory=list,
        description="Soft semantic preferences in the user's own words.",
    )
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V4Constraint] = Field(default_factory=list)
    duration_days: int | None = Field(default=None, ge=1, le=30)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    required_tools: list[ToolKind] = Field(default_factory=list)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)


class StructuredPlanner(Protocol):
    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[V7PlannerDraft],
    ) -> V7PlannerDraft: ...


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
            V7PlannerDraft,
        )
    except ValidationError as exc:
        logger.warning(
            "V7 planner returned an invalid structured response errors={}",
            exc.error_count(),
        )
        return _unavailable_plan(), "planner_invalid", PlannerFailure(
            code="invalid_plan",
            reason="ValidationError",
            retryable=False,
        )
    except Exception as exc:
        reason, retryable = _planner_error(exc)
        logger.warning(
            "V7 planner unavailable reason={} query_length={}",
            reason,
            len(query),
        )
        return _unavailable_plan(), "planner_unavailable", PlannerFailure(
            code="planner_unavailable",
            reason=reason,
            retryable=retryable,
        )

    try:
        plan = _compile_plan(draft)
    except (TypeError, ValueError, ValidationError) as exc:
        logger.warning(
            "V7 planner violated the graph contract error_type={}",
            exc.__class__.__name__,
        )
        return _unavailable_plan(), "planner_invalid", PlannerFailure(
            code="invalid_plan",
            reason=exc.__class__.__name__,
            retryable=False,
        )

    logger.info("V7 semantic planner output plan={}", plan.model_dump_json())
    return plan, "gemini_semantic", None


def _compile_plan(draft: V7PlannerDraft) -> V5QueryPlan:
    return V5QueryPlan(
        intent=draft.intent,
        targets=[
            QueryTarget(
                kind=target.kind,
                value=_clean(target.value),
                entity_types=[value.value for value in target.entity_types],
            )
            for target in draft.targets
        ],
        geo_scope=GeoScope(
            cities=_unique(draft.geo_scope.cities),
            areas=_unique(draft.geo_scope.areas),
            near_entities=_unique(draft.geo_scope.near_entities),
        ),
        requested_fields=_unique([value.value for value in draft.requested_fields]),
        required_concepts=_unique(draft.required_concepts),
        preferred_concepts=_unique(draft.preferred_concepts),
        ranking_criteria=list(dict.fromkeys(draft.ranking_criteria)),
        constraints=draft.constraints,
        duration_days=draft.duration_days,
        limit=draft.limit,
        required_tools=_unique([value.value for value in draft.required_tools]),
        clarification_needed=draft.clarification_needed,
        confidence=draft.confidence,
    )


def _user_prompt(query: str, catalog: dict[str, list[str]]) -> str:
    return json.dumps(
        {
            "user_query": query,
            "graph_contract": {
                "known_cities": catalog.get("cities", []),
                "known_geo_areas": catalog.get("areas", []),
                "known_categories": catalog.get("categories", []),
                "entity_types": [value.value for value in EntityType],
                "requested_fields": sorted(V3_PREDICATES),
                "tools": [value.value for value in ToolKind],
            },
            "instructions": {
                "locations": (
                    "Preserve an unknown or differently worded location as a raw mention; "
                    "the grounding stage will select a graph candidate."
                ),
                "concepts": (
                    "Write semantic requirements naturally. Do not translate them to a "
                    "guessed graph identifier."
                ),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unavailable_plan() -> V5QueryPlan:
    return V5QueryPlan(
        intent=V5Intent.UNSUPPORTED,
        clarification_needed=True,
        confidence=0.0,
    )


def _planner_error(exc: Exception) -> tuple[str, bool]:
    message = str(exc).upper()
    if "BILLING_DISABLED" in message:
        return "GeminiBillingDisabled", False
    if "PERMISSION_DENIED" in message:
        return "GeminiPermissionDenied", False
    if "RESOURCE_EXHAUSTED" in message:
        return "GeminiQuotaExceeded", True
    return exc.__class__.__name__, True


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))
