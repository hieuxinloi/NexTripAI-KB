from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from ..v2.schemas import ENTITY_TYPES, EntityResult, EvidenceResult, FactResult, QueryIntent
from ..v3.schemas import V3_PREDICATES


class ClaimPolarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNCERTAIN = "uncertain"


class ConceptType(StrEnum):
    GEO_AREA = "GeoArea"
    LANDMARK = "Landmark"
    FOOD_OFFERING = "FoodOffering"
    DRINK_OFFERING = "DrinkOffering"
    ACCOMMODATION_OFFERING = "AccommodationOffering"
    CUISINE = "Cuisine"
    DISH = "Dish"
    DIETARY_OPTION = "DietaryOption"
    ACTIVITY = "Activity"
    EXPERIENCE = "Experience"
    SCENERY = "Scenery"
    AMBIENCE = "Ambience"
    AUDIENCE = "Audience"
    ACCESSIBILITY_FEATURE = "AccessibilityFeature"
    WEATHER_CONDITION = "WeatherCondition"
    RISK = "Risk"
    TRAVEL_CONSTRAINT = "TravelConstraint"
    TIME_WINDOW = "TimeWindow"
    SEASON = "Season"
    AMENITY = "Amenity"
    QUALITY_CRITERION = "QualityCriterion"


class ExtractedConcept(BaseModel):
    concept_type: ConceptType
    name: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)


class ExtractedClaim(BaseModel):
    predicate: str = Field(min_length=1)
    object_type: ConceptType
    object_name: str = Field(min_length=1)
    polarity: ClaimPolarity = ClaimPolarity.POSITIVE
    confidence: float = Field(ge=0, le=1)
    evidence_text: str = Field(min_length=1)
    evidence_start: int | None = Field(default=None, ge=0)
    evidence_end: int | None = Field(default=None, ge=0)
    extraction_method: str = "deterministic"
    subject_scope: Literal["place", "offering"] = "place"


class DescriptionExtraction(BaseModel):
    concepts: list[ExtractedConcept] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)


class RetrievalMode(StrEnum):
    ENTITY_LOOKUP = "entity_lookup"
    AGGREGATE = "aggregate"
    PATH_SEARCH = "path_search"
    RECOMMENDATION = "recommendation"
    COMPARISON = "comparison"
    COMMUNITY_SEARCH = "community_search"
    DYNAMIC_SEARCH = "dynamic_search"
    PLANNING_CANDIDATES = "planning_candidates"
    UNSUPPORTED = "unsupported"


class RankingCriterion(StrEnum):
    RATING = "rating"
    POPULARITY = "popularity"


class ConstraintMode(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    EXCLUDE = "exclude"


class V4Constraint(BaseModel):
    field: Literal[
        "budget_max",
        "indoor",
        "near_subject",
        "open_24h",
        "star_rating",
        "weather",
    ]
    value: str | float | int | bool
    mode: ConstraintMode = ConstraintMode.HARD

    @model_validator(mode="after")
    def validate_constraint(self) -> "V4Constraint":
        if self.mode != ConstraintMode.HARD:
            raise ValueError(
                "V4 currently supports hard constraints only; use preferred_concepts for preferences"
            )
        if self.field in {"indoor", "open_24h"} and not isinstance(self.value, bool):
            raise ValueError(f"{self.field} requires a boolean value")
        if self.field == "star_rating":
            if isinstance(self.value, bool) or not isinstance(self.value, int) or not 1 <= self.value <= 5:
                raise ValueError("star_rating requires an integer from 1 to 5")
        if self.field == "budget_max":
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or self.value < 0:
                raise ValueError("budget_max requires a non-negative number")
        if self.field == "near_subject" and not str(self.value).strip():
            raise ValueError("near_subject requires a named place")
        if self.field == "weather" and self.value not in {"rain", "sunny", "cloudy", "all_weather"}:
            raise ValueError("weather uses the supported weather enum")
        return self


class V4QueryPlan(BaseModel):
    intent: QueryIntent
    city: Literal["Đà Nẵng", "Quy Nhơn"] | None = None
    subjects: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[V4Constraint] = Field(default_factory=list)
    retrieval_mode: RetrievalMode
    limit: int = Field(default=5, ge=1, le=30)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "V4QueryPlan":
        unknown_types = set(self.entity_types) - ENTITY_TYPES
        unknown_predicates = set(self.predicates) - V3_PREDICATES
        if unknown_types:
            raise ValueError(f"Unknown V4 entity types: {sorted(unknown_types)}")
        if unknown_predicates:
            raise ValueError(f"Unknown V4 predicates: {sorted(unknown_predicates)}")
        if self.clarification_needed:
            return self
        if self.retrieval_mode == RetrievalMode.ENTITY_LOOKUP:
            if self.intent != QueryIntent.ENTITY_DETAIL or not self.subjects or not self.predicates:
                raise ValueError("V4 entity lookup requires detail intent, subject and predicates")
        elif self.retrieval_mode == RetrievalMode.AGGREGATE:
            if self.intent != QueryIntent.AGGREGATE_COUNT or self.city is None or not self.entity_types:
                raise ValueError("V4 aggregate retrieval requires count intent, city and entity types")
        elif self.retrieval_mode in {RetrievalMode.PATH_SEARCH, RetrievalMode.COMMUNITY_SEARCH}:
            if self.intent != QueryIntent.ENTITY_LIST or not self.entity_types or self.city is None:
                raise ValueError("V4 list retrieval requires list intent, city and entity types")
        elif self.retrieval_mode in {RetrievalMode.RECOMMENDATION, RetrievalMode.PLANNING_CANDIDATES}:
            if self.intent != QueryIntent.RECOMMENDATION or not self.entity_types or self.city is None:
                raise ValueError("V4 recommendation requires recommendation intent, city and entity types")
        elif self.retrieval_mode == RetrievalMode.COMPARISON:
            if self.intent != QueryIntent.ENTITY_LIST or len(self.subjects) < 2:
                raise ValueError("V4 comparison requires list intent and at least two named subjects")
        elif self.retrieval_mode in {RetrievalMode.DYNAMIC_SEARCH, RetrievalMode.UNSUPPORTED}:
            if self.intent != QueryIntent.UNSUPPORTED:
                raise ValueError("Non-KB retrieval modes require unsupported intent")
        return self


class MatchedPath(BaseModel):
    place_id: str
    nodes: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    score: float = 0


class ConstraintResult(BaseModel):
    place_id: str
    field: str
    expected: str | float | int | bool
    mode: ConstraintMode
    passed: bool
    evidence_ids: list[str] = Field(default_factory=list)


class V4EvidenceResult(EvidenceResult):
    subject_id: str


class V4QueryResponse(BaseModel):
    kb_version: Literal["v4"] = "v4"
    answer_type: QueryIntent
    query_plan: V4QueryPlan
    entities: list[EntityResult] = Field(default_factory=list)
    facts: list[FactResult] = Field(default_factory=list)
    recommendations: list[EntityResult] = Field(default_factory=list)
    evidence: list[V4EvidenceResult] = Field(default_factory=list)
    matched_paths: list[MatchedPath] = Field(default_factory=list)
    constraint_results: list[ConstraintResult] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    manifest: dict[str, Any] = Field(default_factory=dict)


class DynamicObservationInput(BaseModel):
    subject_id: str = Field(min_length=1)
    observation_type: Literal["weather", "traffic", "availability", "price"]
    value: dict[str, Any]
    source: str = Field(min_length=1)
    observed_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> "DynamicObservationInput":
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be later than observed_at")
        return self
