from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

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


class HotelPriceObservation(NexTripModel):
    """Point-in-time hotel offer price; previous observations are preserved."""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    hotel_id: str = Field(min_length=1)
    offer_key: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_id: str | None = None
    mapping_id: str | None = Field(default=None, min_length=1)
    external_id: str | None = Field(default=None, min_length=1)
    seller: str | None = None
    room_type: str = Field(min_length=1)
    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    amount: Decimal | None = Field(default=None, gt=0)
    nightly_amount: Decimal | None = Field(default=None, gt=0)
    total_amount: Decimal | None = Field(default=None, gt=0)
    tax_amount: Decimal | None = Field(default=None, ge=0)
    fee_amount: Decimal | None = Field(default=None, ge=0)
    min_amount: Decimal | None = Field(default=None, gt=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    tax_included: bool | None = None
    meal_plan: str | None = None
    cancellation_policy: str | None = None
    refundable: bool | None = None
    booking_url: HttpUrl | None = None
    availability: OfferAvailability = OfferAvailability.UNKNOWN
    raw_text: str | None = None
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @field_validator("children_ages")
    @classmethod
    def normalize_children_ages(cls, value: list[int]) -> list[int]:
        return sorted(value)

    @model_validator(mode="after")
    def validate_stay_and_price(self) -> HotelPriceObservation:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")

        if (self.mapping_id is None) != (self.external_id is None):
            raise ValueError(
                "mapping_id and external_id must either both be set or both be absent"
            )

        prices = [
            self.amount,
            self.nightly_amount,
            self.total_amount,
            self.min_amount,
            self.max_amount,
        ]
        if self.availability == OfferAvailability.AVAILABLE and not any(prices):
            raise ValueError("an available offer must include a price")

        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.max_amount < self.min_amount
        ):
            raise ValueError("max_amount must be greater than or equal to min_amount")

        # An empty list is retained as the backward-compatible sentinel for an
        # older observation whose child ages were not captured. Once ages are
        # present, the context must be complete and valid.
        if self.children_ages and (len(self.children_ages) != self.occupancy.children):
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")

        return self


# Backward-compatible name used by the first pipeline version.
PriceObservation = HotelPriceObservation


class PriceUnit(StrEnum):
    PERSON = "person"
    GROUP = "group"
    ITEM = "item"
    VISIT = "visit"


class SpendType(StrEnum):
    AVERAGE_PER_PERSON = "average_per_person"
    PRICE_RANGE = "price_range"
    MINIMUM_SPEND = "minimum_spend"


class AdmissionPriceObservation(NexTripModel):
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    ticket_name: str = Field(min_length=1)
    visitor_type: str | None = None
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    amount: Decimal = Field(ge=0)
    unit: PriceUnit = PriceUnit.PERSON
    valid_from: date | None = None
    valid_to: date | None = None
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @model_validator(mode="after")
    def validate_validity(self) -> AdmissionPriceObservation:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot be before valid_from")
        return self


class SpendObservation(NexTripModel):
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    spend_type: SpendType
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @model_validator(mode="after")
    def validate_amounts(self) -> SpendObservation:
        if self.amount is None and self.min_amount is None and self.max_amount is None:
            raise ValueError("at least one spend amount is required")
        if self.min_amount is not None and self.max_amount is not None:
            if self.max_amount < self.min_amount:
                raise ValueError("max_amount cannot be below min_amount")
        return self
