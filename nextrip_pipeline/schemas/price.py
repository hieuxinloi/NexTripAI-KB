from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from .common import NexTripModel, VerificationStatus


class OfferAvailability(StrEnum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Occupancy(NexTripModel):
    adults: int = Field(default=2, ge=1)
    children: int = Field(default=0, ge=0)
    rooms: int = Field(default=1, ge=1)


class PriceObservation(NexTripModel):
    """Point-in-time hotel offer price; previous observations are preserved."""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    hotel_id: str = Field(min_length=1)
    offer_key: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    room_type: str = Field(min_length=1)
    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    amount: Decimal | None = Field(default=None, gt=0)
    min_amount: Decimal | None = Field(default=None, gt=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    tax_included: bool | None = None
    meal_plan: str | None = None
    cancellation_policy: str | None = None
    availability: OfferAvailability = OfferAvailability.UNKNOWN
    raw_text: str | None = None
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @model_validator(mode="after")
    def validate_stay_and_price(self) -> PriceObservation:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")

        prices = [self.amount, self.min_amount, self.max_amount]
        if self.availability == OfferAvailability.AVAILABLE and not any(prices):
            raise ValueError("an available offer must include a price")

        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.max_amount < self.min_amount
        ):
            raise ValueError("max_amount must be greater than or equal to min_amount")

        return self

