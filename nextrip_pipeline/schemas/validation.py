from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field

from .common import NexTripModel


class ValidationStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"


class SuggestedAction(StrEnum):
    AUTO_ACCEPT = "auto_accept"
    AGENT_REVIEW = "agent_review"
    HUMAN_REVIEW = "human_review"
    QUARANTINE = "quarantine"
    ARCHIVE = "archive"


class ValidationEvidence(NexTripModel):
    code: str = Field(min_length=1)
    source_record_id: str | None = None
    field: str | None = None
    observed_value: Any = None
    expected_value: Any = None


class ValidationResult(NexTripModel):
    """One validator's auditable result for a record or observation."""

    validation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    validator: str = Field(min_length=1)
    validator_version: str = Field(min_length=1)
    status: ValidationStatus
    score: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)
    evidence: list[ValidationEvidence] = Field(default_factory=list)
    suggested_action: SuggestedAction
    requires_semantic_review: bool = False
    requires_human_review: bool = False
    duration_ms: int = Field(default=0, ge=0)
    validated_at: AwareDatetime
