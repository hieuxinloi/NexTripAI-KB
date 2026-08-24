from __future__ import annotations

from datetime import date, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from .common import NexTripModel, VerificationStatus
from .price import Occupancy


class HotelAvailabilityStatus(StrEnum):
    """Outcome of one exact hotel stay search."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class HotelAvailabilityReason(StrEnum):
    """Machine-readable reason for an availability outcome.

    ``UNAVAILABLE`` is reserved for a successful provider response proving that
    the requested stay has no usable priced offer. Transport, parser, and
    unresolved-identity failures remain ``UNKNOWN`` so callers never present a
    technical failure as a sold-out hotel.
    """

    OFFER_FOUND = "offer_found"
    SOLD_OUT = "sold_out"
    NO_PRICE = "no_price"
    NO_BOOKABLE_OFFER_RETURNED = "no_bookable_offer_returned"
    PROVIDER_NOT_LISTED = "provider_not_listed"
    CONFIRMED_LISTING_NOT_RETURNED = "confirmed_listing_not_returned"
    IDENTITY_REVERIFY = "identity_reverify"
    CRAWL_ERROR = "crawl_error"
    MAPPING_UNRESOLVED = "mapping_unresolved"


class HotelAvailabilityObservation(NexTripModel):
    """Availability for one hotel, stay interval, and guest context.

    The requested date and fallback offset preserve why a later check-in was
    crawled. They are response annotations rather than storage identity: a
    direct search and a fallback search for the same actual stay share one
    current snapshot.
    """

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    hotel_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    mapping_id: str | None = Field(default=None, min_length=1)
    external_id: str | None = Field(default=None, min_length=1)

    requested_check_in: date
    fallback_offset_days: int = Field(default=0, ge=0)
    check_in: date
    check_out: date
    nights: int = Field(ge=1)
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")

    status: HotelAvailabilityStatus
    reason: HotelAvailabilityReason
    offer_count: int = Field(default=0, ge=0)
    price_observation_ids: list[str] = Field(default_factory=list)
    raw_status_text: str | None = None
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW

    @field_validator("children_ages")
    @classmethod
    def normalize_children_ages(cls, value: list[int]) -> list[int]:
        return sorted(value)

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("price_observation_ids")
    @classmethod
    def require_unique_price_observations(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("price_observation_ids cannot contain blank values")
        if len(value) != len(set(value)):
            raise ValueError("price_observation_ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_context_and_outcome(self) -> HotelAvailabilityObservation:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if (self.check_out - self.check_in).days != self.nights:
            raise ValueError("nights must equal the check_in/check_out interval")
        if self.check_in != self.requested_check_in + timedelta(
            days=self.fallback_offset_days
        ):
            raise ValueError(
                "check_in must equal requested_check_in plus fallback_offset_days"
            )

        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")

        available_reasons = {HotelAvailabilityReason.OFFER_FOUND}
        unavailable_reasons = {
            HotelAvailabilityReason.SOLD_OUT,
            HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
        }
        unknown_reasons = {
            HotelAvailabilityReason.CRAWL_ERROR,
            HotelAvailabilityReason.MAPPING_UNRESOLVED,
            HotelAvailabilityReason.NO_PRICE,
            HotelAvailabilityReason.PROVIDER_NOT_LISTED,
            HotelAvailabilityReason.CONFIRMED_LISTING_NOT_RETURNED,
            HotelAvailabilityReason.IDENTITY_REVERIFY,
        }
        reasons_by_status = {
            HotelAvailabilityStatus.AVAILABLE: available_reasons,
            HotelAvailabilityStatus.UNAVAILABLE: unavailable_reasons,
            HotelAvailabilityStatus.UNKNOWN: unknown_reasons,
        }
        if self.reason not in reasons_by_status[self.status]:
            raise ValueError("reason is incompatible with availability status")

        if self.status is not HotelAvailabilityStatus.UNKNOWN and (
            self.mapping_id is None or self.external_id is None
        ):
            raise ValueError(
                "available and unavailable observations require confirmed mapping"
            )

        if self.status is HotelAvailabilityStatus.AVAILABLE:
            if self.offer_count < 1 or not self.price_observation_ids:
                raise ValueError(
                    "available availability requires a priced offer observation"
                )
            if self.offer_count < len(self.price_observation_ids):
                raise ValueError(
                    "offer_count cannot be lower than price_observation_ids count"
                )
        elif self.offer_count or self.price_observation_ids:
            raise ValueError(
                "unavailable or unknown availability cannot reference priced offers"
            )

        return self
