from __future__ import annotations

from decimal import Decimal

from pydantic import AwareDatetime, Field, model_validator

from .common import NexTripModel, VerificationStatus


class RatingObservation(NexTripModel):
    """Point-in-time rating; zero is retained only when supplied by a source."""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @model_validator(mode="after")
    def require_measurement(self) -> RatingObservation:
        if self.rating is None and self.review_count is None:
            raise ValueError("rating or review_count is required")
        return self
