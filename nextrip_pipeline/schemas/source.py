from __future__ import annotations

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    model_validator,
)

from .common import EntityType, NexTripModel, RecordSubjectType


class SourceRecord(NexTripModel):
    """Immutable evidence captured by one crawl or API request."""

    # Raw evidence must retain leading/trailing whitespace exactly as captured;
    # the shared model default strips strings and would invalidate its hash.
    model_config = ConfigDict(str_strip_whitespace=False)

    source_record_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    entity_type: EntityType | None = None
    subject_type: RecordSubjectType | None = None
    subject_id: str | None = None
    crawled_at: AwareDatetime
    raw_payload: dict[str, JsonValue] = Field(default_factory=dict)
    content_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    parser_version: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None

    @model_validator(mode="after")
    def normalize_subject(self) -> SourceRecord:
        subject_type = self.subject_type
        if subject_type is None and self.entity_type is not None:
            subject_type = RecordSubjectType.PLACE
            object.__setattr__(self, "subject_type", subject_type)
        if subject_type is None:
            raise ValueError("subject_type or entity_type is required")
        if subject_type is RecordSubjectType.PLACE and self.entity_type is None:
            raise ValueError("place source records require entity_type")
        if subject_type is not RecordSubjectType.PLACE and not self.subject_id:
            raise ValueError("non-place source records require subject_id")
        return self
