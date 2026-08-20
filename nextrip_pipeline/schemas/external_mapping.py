from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, JsonValue, model_validator

from .common import EntityType, NexTripModel, RecordSubjectType


class MappingStatus(StrEnum):
    CONFIRMED = "confirmed"
    AUTO_MATCHED = "auto_matched"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"


class ExternalEntityMapping(NexTripModel):
    """Links one canonical NexTrip entity to an external provider identifier."""

    mapping_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    entity_type: EntityType | None = None
    subject_type: RecordSubjectType = RecordSubjectType.PLACE
    source_id: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    external_url: HttpUrl | None = None
    status: MappingStatus = MappingStatus.PENDING_REVIEW
    confidence: float | None = Field(default=None, ge=0, le=1)
    matched_at: AwareDatetime
    verified_at: AwareDatetime | None = None
    last_checked_at: AwareDatetime | None = None
    source_record_ids: list[str] = Field(default_factory=list)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def confirmed_mapping_requires_verification(self) -> ExternalEntityMapping:
        if self.subject_type is RecordSubjectType.PLACE and self.entity_type is None:
            raise ValueError("place mapping requires entity_type")
        if self.status is MappingStatus.CONFIRMED and self.verified_at is None:
            raise ValueError("confirmed mapping requires verified_at")
        return self
