from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..v2.schemas import ENTITY_TYPES, EntityResult, FactResult, QueryIntent
from ..v4.schemas import (
    ConstraintResult,
    MatchedPath,
    RankingCriterion,
    V4Constraint,
    V4EvidenceResult,
)


class TargetKind(StrEnum):
    PLACE = "place"
    CITY = "city"
    GEO_AREA = "geo_area"
    DISH = "dish"
    ACTIVITY = "activity"
    CONCEPT = "concept"


class V5Intent(StrEnum):
    LOOKUP = "lookup"
    PROFILE = "profile"
    LIST = "list"
    RECOMMEND = "recommend"
    COMPARE = "compare"
    SUMMARIZE = "summarize"
    AGGREGATE = "aggregate"
    PLAN_CANDIDATES = "plan_candidates"
    TOOL_REQUIRED = "tool_required"
    UNSUPPORTED = "unsupported"


class QueryTarget(BaseModel):
    kind: TargetKind
    value: str | None = None
    entity_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entity_types(self) -> "QueryTarget":
        unknown = set(self.entity_types) - ENTITY_TYPES
        if unknown:
            raise ValueError(f"Unknown place entity types: {sorted(unknown)}")
        if self.kind != TargetKind.PLACE and self.entity_types:
            raise ValueError("entity_types are only valid for place targets")
        return self


class GeoScope(BaseModel):
    cities: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    near_entities: list[str] = Field(default_factory=list)


class V5QueryPlan(BaseModel):
    intent: V5Intent
    targets: list[QueryTarget] = Field(default_factory=list)
    geo_scope: GeoScope = Field(default_factory=GeoScope)
    requested_fields: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V4Constraint] = Field(default_factory=list)
    duration_days: int | None = Field(default=None, ge=1, le=30)
    limit: int = Field(default=5, ge=1, le=30)
    required_tools: list[str] = Field(default_factory=list)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "V5QueryPlan":
        if self.clarification_needed:
            return self
        if self.intent == V5Intent.TOOL_REQUIRED and not self.required_tools:
            raise ValueError("tool_required plans must name at least one tool")
        if self.intent in {V5Intent.LOOKUP, V5Intent.PROFILE, V5Intent.COMPARE}:
            named = [target for target in self.targets if target.value]
            minimum = 2 if self.intent == V5Intent.COMPARE else 1
            if len(named) < minimum:
                raise ValueError(f"{self.intent.value} requires {minimum} named target(s)")
        if self.intent == V5Intent.AGGREGATE and not self.targets:
            raise ValueError("aggregate requires a typed target")
        return self


class TargetResult(BaseModel):
    target_id: str
    kind: TargetKind
    name: str
    description: str | None = None
    score: float = 0
    evidence_ids: list[str] = Field(default_factory=list)


class V5QueryResponse(BaseModel):
    kb_version: Literal["v5"] = "v5"
    answer_type: QueryIntent
    intent: V5Intent
    query_plan: V5QueryPlan
    targets: list[TargetResult] = Field(default_factory=list)
    entities: list[EntityResult] = Field(default_factory=list)
    facts: list[FactResult] = Field(default_factory=list)
    recommendations: list[EntityResult] = Field(default_factory=list)
    evidence: list[V4EvidenceResult] = Field(default_factory=list)
    matched_paths: list[MatchedPath] = Field(default_factory=list)
    constraint_results: list[ConstraintResult] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)
