from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from ..v2.schemas import EntityResult, EvidenceResult, FactResult, QueryIntent


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


class ConstraintMode(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    EXCLUDE = "exclude"


class V4Constraint(BaseModel):
    field: str = Field(min_length=1)
    value: str | float | int | bool
    mode: ConstraintMode = ConstraintMode.HARD


class V4QueryPlan(BaseModel):
    intent: QueryIntent
    city: str | None = None
    subjects: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    constraints: list[V4Constraint] = Field(default_factory=list)
    retrieval_mode: RetrievalMode
    limit: int = Field(default=5, ge=1, le=30)
    clarification_needed: bool = False
    confidence: float = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "V4QueryPlan":
        if self.intent == QueryIntent.ENTITY_DETAIL and not self.subjects:
            raise ValueError("V4 entity lookup requires a subject")
        if self.retrieval_mode == RetrievalMode.AGGREGATE and not self.entity_types:
            raise ValueError("V4 aggregate retrieval requires entity types")
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
