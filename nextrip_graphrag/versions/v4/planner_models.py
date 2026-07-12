from __future__ import annotations

from pydantic import BaseModel, Field

from ...config import DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from .schemas import ConstraintMode, RankingCriterion, RetrievalMode


class PlannerConstraintDraft(BaseModel):
    field: str = Field(min_length=1)
    value: str | float | int | bool
    mode: ConstraintMode = ConstraintMode.HARD


class V4PlannerDraft(BaseModel):
    city: str | None = None
    subjects: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    preferred_concepts: list[str] = Field(default_factory=list)
    ranking_criteria: list[RankingCriterion] = Field(default_factory=list)
    constraints: list[PlannerConstraintDraft] = Field(default_factory=list)
    retrieval_mode: RetrievalMode
    limit: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)
