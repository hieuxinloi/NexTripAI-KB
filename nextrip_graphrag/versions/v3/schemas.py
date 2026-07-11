from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..v2.schemas import (
    EntityResult,
    EvidenceResult,
    FactResult,
    QueryIntent,
    QueryOperation,
)


V3_PREDICATES = {
    "address",
    "age_restriction",
    "availability_status",
    "altitude",
    "ambience",
    "amenities",
    "check_in_time",
    "check_out_time",
    "city",
    "cuisine",
    "construction_period",
    "cable_car",
    "branch_info",
    "booking_advice",
    "data_quality_warning",
    "opening_24h_claim",
    "description",
    "distance_from_center",
    "distance_to_city_center_geo",
    "distance_to_beach",
    "distance_to_center",
    "duration",
    "features",
    "highlights",
    "hotel_style",
    "location",
    "music_genres",
    "opening_hours",
    "phone",
    "price",
    "price_max",
    "price_min",
    "rating",
    "review_count",
    "serves",
    "signature_dishes",
    "star_rating",
    "suitable_for",
    "tags",
    "travel_time_from_center",
    "unesco_status",
    "venue_type",
    "weather",
}


class V3Filters(BaseModel):
    star_rating: int | None = Field(default=None, ge=1, le=5)
    amenity: str | None = None
    cuisine: str | None = None
    dish: str | None = None
    feature: str | None = None
    venue_type: str | None = None
    ambience: str | None = None
    tag: str | None = None
    category: str | None = None
    near_subject: str | None = None
    open_24h: bool | None = None
    indoor: bool | None = None
    weather: Literal["rain", "sunny", "cloudy", "all_weather"] | None = None
    budget_max: float | None = Field(default=None, ge=0)


class V3RetrievalTask(BaseModel):
    operation: QueryOperation
    entity_types: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    filters: V3Filters = Field(default_factory=V3Filters)
    terms: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def validate_predicates(self) -> "V3RetrievalTask":
        unknown = set(self.predicates) - V3_PREDICATES
        if unknown:
            raise ValueError(f"Unknown V3 predicates: {sorted(unknown)}")
        return self


class V3QueryPlan(BaseModel):
    intent: QueryIntent
    city: Literal["Đà Nẵng", "Quy Nhơn"] | None = None
    subjects: list[str] = Field(default_factory=list)
    tasks: list[V3RetrievalTask] = Field(default_factory=list, max_length=8)
    clarification_needed: bool = False
    confidence: float = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_shape(self) -> "V3QueryPlan":
        if self.intent != QueryIntent.UNSUPPORTED and not self.tasks:
            raise ValueError("Supported V3 plans require tasks")
        if self.intent == QueryIntent.ENTITY_DETAIL and not self.subjects:
            raise ValueError("V3 entity detail requires a subject")
        return self


class V3QueryResponse(BaseModel):
    kb_version: Literal["v3"] = "v3"
    answer_type: QueryIntent
    query_plan: V3QueryPlan
    entities: list[EntityResult] = Field(default_factory=list)
    facts: list[FactResult] = Field(default_factory=list)
    recommendations: list[EntityResult] = Field(default_factory=list)
    evidence: list[EvidenceResult] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)
