from __future__ import annotations

from datetime import date, time
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .common import NexTripModel, VerificationStatus


class DailyOpeningStatus(StrEnum):
    OPEN_TODAY = "open_today"
    CLOSED_TODAY = "closed_today"
    TEMPORARILY_CLOSED = "temporarily_closed"
    PERMANENTLY_CLOSED = "permanently_closed"
    UNKNOWN = "unknown"


class OpeningInterval(NexTripModel):
    opens_at: time
    closes_at: time
    closes_next_day: bool = False


class OpeningStatusObservation(NexTripModel):
    """Daily opening decision derived from schedules and special notices."""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_ids: list[str] = Field(min_length=1)
    local_date: date
    timezone: str = "Asia/Ho_Chi_Minh"
    status: DailyOpeningStatus
    opening_intervals: list[OpeningInterval] = Field(default_factory=list)
    is_24_hours: bool = False
    special_hours: bool = False
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_opening_details(self) -> OpeningStatusObservation:
        if (
            self.status == DailyOpeningStatus.OPEN_TODAY
            and not self.is_24_hours
            and not self.opening_intervals
        ):
            raise ValueError("open_today requires intervals or is_24_hours")
        return self

