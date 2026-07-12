from __future__ import annotations

from pydantic import BaseModel, Field

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
    limit: int = Field(default=5, ge=1, le=30)
    clarification_needed: bool = False
    confidence: float = Field(ge=0, le=1)
