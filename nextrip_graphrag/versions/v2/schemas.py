from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K


ENTITY_TYPES = {"attraction", "cafe", "hotel", "nightlife", "restaurant"}
PREDICATES = {
    "address",
    "altitude",
    "amenities",
    "city",
    "description",
    "duration",
    "indoor",
    "latitude",
    "location",
    "longitude",
    "opening_hours",
    "phone",
    "price",
    "rating",
    "review_count",
    "star_rating",
    "weather",
}


class QueryIntent(StrEnum):
    AGGREGATE_COUNT = "aggregate_count"
    ENTITY_DETAIL = "entity_detail"
    ENTITY_LIST = "entity_list"
    RECOMMENDATION = "recommendation"
    UNSUPPORTED = "unsupported"


class QueryOperation(StrEnum):
    COUNT = "count"
    LOOKUP = "lookup"
    FILTER = "filter"
    RECOMMEND = "recommend"


class HardConstraints(BaseModel):
    indoor: bool | None = None
    weather: Literal["rain", "sunny", "cloudy", "all_weather"] | None = None
    budget_max: float | None = Field(default=None, ge=0)


class RetrievalTask(BaseModel):
    operation: QueryOperation
    entity_types: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    hard_constraints: HardConstraints = Field(default_factory=HardConstraints)
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)

    @model_validator(mode="after")
    def validate_whitelist(self) -> "RetrievalTask":
        unknown_types = set(self.entity_types) - ENTITY_TYPES
        unknown_predicates = set(self.predicates) - PREDICATES
        if unknown_types:
            raise ValueError(f"Unknown entity types: {sorted(unknown_types)}")
        if unknown_predicates:
            raise ValueError(f"Unknown predicates: {sorted(unknown_predicates)}")
        return self


class QueryPlan(BaseModel):
    intent: QueryIntent
    city: Literal["Đà Nẵng", "Quy Nhơn"] | None = None
    duration_days: int | None = Field(default=None, ge=1, le=30)
    subjects: list[str] = Field(default_factory=list)
    tasks: list[RetrievalTask] = Field(default_factory=list, max_length=5)
    clarification_needed: bool = False
    confidence: float = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "QueryPlan":
        if self.intent != QueryIntent.UNSUPPORTED and not self.tasks:
            raise ValueError("A supported query plan must contain at least one task")
        if self.intent == QueryIntent.ENTITY_DETAIL and not self.subjects:
            raise ValueError("Entity detail requires at least one subject")
        if self.clarification_needed or not self.tasks:
            return self
        task = self.tasks[0]
        if self.intent == QueryIntent.AGGREGATE_COUNT:
            valid_tasks = all(
                item.operation == QueryOperation.COUNT and len(item.entity_types) == 1
                for item in self.tasks
            )
            if self.city is None or not valid_tasks:
                raise ValueError("Aggregate count requires city, entity types and count operation")
        elif self.intent == QueryIntent.ENTITY_DETAIL:
            if not task.predicates or task.operation != QueryOperation.LOOKUP:
                raise ValueError("Entity detail requires predicates and lookup operation")
        elif self.intent == QueryIntent.ENTITY_LIST:
            if self.city is None or not task.entity_types or task.operation != QueryOperation.FILTER:
                raise ValueError("Entity list requires city, entity types and filter operation")
        elif self.intent == QueryIntent.RECOMMENDATION:
            if self.city is None or task.operation != QueryOperation.RECOMMEND:
                raise ValueError("Recommendation requires city and recommend operation")
        return self


class EntityResult(BaseModel):
    place_id: str
    name: str
    city: str
    entity_type: str
    category: str | None = None
    score: float | None = None
    distance_km: float | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class FactResult(BaseModel):
    fact_id: str
    subject_id: str
    predicate: str
    entity_type: str | None = None
    value: str | float | int | bool | list[str]
    value_type: str
    unit: str | None = None
    confidence: float
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceResult(BaseModel):
    text_unit_id: str
    document_id: str
    title: str
    text: str
    url: str | None = None
    source_name: str | None = None


class V2QueryResponse(BaseModel):
    kb_version: Literal["v2"] = "v2"
    answer_type: QueryIntent
    query_plan: QueryPlan
    entities: list[EntityResult] = Field(default_factory=list)
    facts: list[FactResult] = Field(default_factory=list)
    recommendations: list[EntityResult] = Field(default_factory=list)
    evidence: list[EvidenceResult] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)
