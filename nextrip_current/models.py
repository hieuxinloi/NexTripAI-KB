from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from nextrip_pipeline.schemas import (
    CurrentPlaceSnapshot,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    NexTripModel,
    Occupancy,
    OfferAvailability,
    VerificationStatus,
)
from nextrip_traffic.models import TransportRecommendationResponse


class CurrentLookupStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"


class HotelNameSource(StrEnum):
    TRIVAGO_NAME = "trivago_name"
    HOTEL_NAME = "hotel_name"
    CURRENT_PLACE = "current_place"
    UNKNOWN = "unknown"


class CurrentPlaceEnvelope(NexTripModel):
    place: CurrentPlaceSnapshot
    master_name: str
    aliases: list[str] = Field(default_factory=list)
    name_source: HotelNameSource = HotelNameSource.CURRENT_PLACE
    stale: bool
    evaluated_at: AwareDatetime


class PlaceBatchRequest(NexTripModel):
    place_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_place_ids(self) -> PlaceBatchRequest:
        if len(set(self.place_ids)) != len(self.place_ids):
            raise ValueError("place_ids cannot contain duplicates")
        return self


class PlaceBatchItem(NexTripModel):
    place_id: str
    status: CurrentLookupStatus
    current: CurrentPlaceEnvelope | None = None

    @model_validator(mode="after")
    def validate_result(self) -> PlaceBatchItem:
        if self.status is CurrentLookupStatus.MISSING and self.current is not None:
            raise ValueError("missing place cannot include current data")
        if self.status is not CurrentLookupStatus.MISSING and self.current is None:
            raise ValueError("available or stale place requires current data")
        return self


class PlaceBatchResponse(NexTripModel):
    evaluated_at: AwareDatetime
    items: list[PlaceBatchItem]


class TripRouteLegRequest(NexTripModel):
    origin_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    departure_time: AwareDatetime

    @model_validator(mode="after")
    def distinct_endpoints(self) -> TripRouteLegRequest:
        if self.origin_id == self.destination_id:
            raise ValueError("trip route endpoints must be different")
        return self


class HotelOfferSearchRequest(NexTripModel):
    hotel_ids: list[str] = Field(min_length=1, max_length=100)
    check_in: date
    check_out: date | None = Field(
        default=None,
        description="Exclusive checkout date; derived from duration when omitted.",
    )
    stay_nights: int | None = Field(
        default=None,
        ge=1,
        le=30,
        description="Billable nights; equals check_out minus check_in.",
    )
    stay_days: int | None = Field(
        default=None,
        ge=2,
        le=31,
        description=(
            "Inclusive calendar days; for example, 3 days means 2 hotel nights."
        ),
    )
    lookahead_days: int = Field(
        default=1,
        ge=0,
        le=14,
        description=(
            "Consecutive later check-in dates to inspect after trusted unavailable "
            "inventory; the complete stay interval shifts each time."
        ),
    )
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list, max_length=20)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    seller: str | None = Field(default=None, min_length=1)
    include_stale: bool = False
    refresh_if_missing: bool = False

    @field_validator("children_ages")
    @classmethod
    def normalize_children_ages(cls, value: list[int]) -> list[int]:
        return sorted(value)

    @model_validator(mode="after")
    def validate_context(self) -> HotelOfferSearchRequest:
        explicit_check_out = self.check_out
        nights_from_check_out = (
            (explicit_check_out - self.check_in).days
            if explicit_check_out is not None
            else None
        )
        if nights_from_check_out is not None and nights_from_check_out < 1:
            raise ValueError("check_out must be after check_in")

        nights_from_days = self.stay_days - 1 if self.stay_days is not None else None
        supplied_durations = [
            value
            for value in (
                nights_from_check_out,
                self.stay_nights,
                nights_from_days,
            )
            if value is not None
        ]
        if len(set(supplied_durations)) > 1:
            raise ValueError(
                "check_out, stay_nights, and stay_days must describe the same stay"
            )

        resolved_nights = supplied_durations[0] if supplied_durations else 1
        if resolved_nights > 30:
            raise ValueError("hotel stays cannot exceed 30 nights")
        resolved_check_out = self.check_in + timedelta(days=resolved_nights)
        # ``NexTripModel`` validates assignment. Bypass assignment hooks here so
        # the after-validator can canonicalize the three equivalent inputs once.
        object.__setattr__(self, "check_out", resolved_check_out)
        object.__setattr__(self, "stay_nights", resolved_nights)
        object.__setattr__(self, "stay_days", resolved_nights + 1)

        if len(set(self.hotel_ids)) != len(self.hotel_ids):
            raise ValueError("hotel_ids cannot contain duplicates")
        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        if self.refresh_if_missing and len(self.hotel_ids) != 1:
            raise ValueError("refresh_if_missing requires exactly one hotel_id")
        return self


class HotelAvailabilitySearchRequest(HotelOfferSearchRequest):
    """On-demand stay lookup; missing/stale data refreshes by default."""

    refresh_if_missing: bool = True


class HotelIdentity(NexTripModel):
    hotel_id: str
    display_name: str | None = None
    master_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    name_source: HotelNameSource = HotelNameSource.UNKNOWN
    mapping_id: str | None = None
    external_id: str | None = None
    external_url: HttpUrl | None = None


class HotelOfferProvenance(NexTripModel):
    run_id: str
    source_id: str | None = None
    source_record_id: str
    observation_id: str
    decision_id: str
    verification_status: VerificationStatus
    mapping_id: str | None = None
    external_id: str | None = None
    external_url: HttpUrl | None = None


class CurrentHotelOffer(NexTripModel):
    hotel_id: str
    offer_key: str
    seller: str | None = None
    room_type: str
    check_in: date
    check_out: date
    occupancy: Occupancy
    children_ages: list[int] = Field(default_factory=list)
    currency: str
    amount: Decimal | None = None
    nightly_amount: Decimal | None = None
    total_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    tax_included: bool | None = None
    meal_plan: str | None = None
    cancellation_policy: str | None = None
    refundable: bool | None = None
    booking_url: HttpUrl | None = None
    availability: OfferAvailability
    observed_at: AwareDatetime
    stale_after: AwareDatetime
    stale: bool
    provenance: HotelOfferProvenance


class HotelOfferResult(NexTripModel):
    hotel_id: str
    status: CurrentLookupStatus
    identity: HotelIdentity
    offers: list[CurrentHotelOffer] = Field(default_factory=list)
    stale_offer_count: int = Field(default=0, ge=0)
    latest_observed_at: AwareDatetime | None = None
    latest_stale_after: AwareDatetime | None = None
    refresh_attempted: bool = False


class HotelOfferSearchResponse(NexTripModel):
    check_in: date
    check_out: date
    occupancy: Occupancy
    children_ages: list[int]
    currency: str | None = None
    seller: str | None = None
    include_stale: bool
    evaluated_at: AwareDatetime
    results: list[HotelOfferResult]


class HotelStayWindowResult(NexTripModel):
    """Current result for one exact stay and one fallback date offset.

    ``lookup_status`` describes whether the current projection exists and is
    fresh. ``availability`` separately describes room inventory, preventing a
    crawl or mapping failure from being presented as a sold-out hotel.
    """

    requested_check_in: date
    fallback_offset_days: int = Field(ge=0)
    check_in: date
    check_out: date
    stay_nights: int = Field(ge=1)
    lookup_status: CurrentLookupStatus
    availability: HotelAvailabilityStatus
    reason: HotelAvailabilityReason | None = None
    offers: list[CurrentHotelOffer] = Field(default_factory=list)
    stale_offer_count: int = Field(default=0, ge=0)
    latest_observed_at: AwareDatetime | None = None
    latest_stale_after: AwareDatetime | None = None
    availability_observation_id: str | None = None
    refresh_attempted: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> HotelStayWindowResult:
        if self.check_in != self.requested_check_in + timedelta(
            days=self.fallback_offset_days
        ):
            raise ValueError(
                "check_in must equal requested_check_in plus fallback_offset_days"
            )
        if self.check_out != self.check_in + timedelta(days=self.stay_nights):
            raise ValueError("check_out must match stay_nights")
        if any(
            offer.check_in != self.check_in or offer.check_out != self.check_out
            for offer in self.offers
        ):
            raise ValueError("offers must match the stay window")

        available_reasons = {HotelAvailabilityReason.OFFER_FOUND}
        unavailable_reasons = {
            HotelAvailabilityReason.SOLD_OUT,
            HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
        }
        unknown_reasons = {
            HotelAvailabilityReason.NO_PRICE,
            HotelAvailabilityReason.PROVIDER_NOT_LISTED,
            HotelAvailabilityReason.CONFIRMED_LISTING_NOT_RETURNED,
            HotelAvailabilityReason.IDENTITY_REVERIFY,
            HotelAvailabilityReason.CRAWL_ERROR,
            HotelAvailabilityReason.MAPPING_UNRESOLVED,
        }
        reasons_by_status = {
            HotelAvailabilityStatus.AVAILABLE: available_reasons,
            HotelAvailabilityStatus.UNAVAILABLE: unavailable_reasons,
            HotelAvailabilityStatus.UNKNOWN: unknown_reasons,
        }
        if (
            self.reason is not None
            and self.reason not in reasons_by_status[self.availability]
        ):
            raise ValueError("reason is incompatible with availability status")
        if (
            self.availability is not HotelAvailabilityStatus.UNKNOWN
            and self.reason is None
        ):
            raise ValueError("available or unavailable result requires a reason")

        priced_available_offers = [
            offer
            for offer in self.offers
            if offer.availability is OfferAvailability.AVAILABLE
            and any(
                price is not None
                for price in (
                    offer.amount,
                    offer.nightly_amount,
                    offer.total_amount,
                    offer.min_amount,
                    offer.max_amount,
                )
            )
        ]
        if self.availability is HotelAvailabilityStatus.AVAILABLE:
            if not priced_available_offers:
                raise ValueError("available result requires a priced available offer")
            if self.lookup_status is CurrentLookupStatus.MISSING:
                raise ValueError("missing lookup cannot report available inventory")
        elif priced_available_offers:
            raise ValueError(
                "unavailable or unknown result cannot include a priced available offer"
            )

        if self.lookup_status is CurrentLookupStatus.MISSING:
            if self.offers:
                raise ValueError("missing lookup cannot include offers")
            if self.availability is not HotelAvailabilityStatus.UNKNOWN:
                raise ValueError("missing lookup must keep availability unknown")
        return self


class HotelAvailabilityResult(NexTripModel):
    hotel_id: str
    identity: HotelIdentity
    windows: list[HotelStayWindowResult] = Field(min_length=1, max_length=15)
    selected_window_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_windows_and_selection(self) -> HotelAvailabilityResult:
        first = self.windows[0]
        expected_offsets = list(range(len(self.windows)))
        actual_offsets = [window.fallback_offset_days for window in self.windows]
        if actual_offsets != expected_offsets:
            raise ValueError("windows must be ordered at consecutive fallback offsets")
        if any(
            window.requested_check_in != first.requested_check_in
            or window.stay_nights != first.stay_nights
            for window in self.windows
        ):
            raise ValueError("all windows must preserve the requested stay context")
        if any(
            offer.hotel_id != self.hotel_id
            for window in self.windows
            for offer in window.offers
        ):
            raise ValueError("window offers must belong to hotel_id")

        selectable_indexes = [
            index
            for index, window in enumerate(self.windows)
            if window.lookup_status is CurrentLookupStatus.AVAILABLE
            and window.availability is HotelAvailabilityStatus.AVAILABLE
        ]
        expected_selection = selectable_indexes[0] if selectable_indexes else None
        if self.selected_window_index != expected_selection:
            raise ValueError(
                "selected_window_index must identify the earliest fresh available window"
            )
        return self


class HotelAvailabilitySearchResponse(NexTripModel):
    check_in: date
    check_out: date
    stay_nights: int = Field(ge=1, le=30)
    stay_days: int = Field(ge=2, le=31)
    lookahead_days: int = Field(ge=0, le=14)
    occupancy: Occupancy
    children_ages: list[int]
    currency: str | None = None
    seller: str | None = None
    include_stale: bool
    evaluated_at: AwareDatetime
    results: list[HotelAvailabilityResult]

    @model_validator(mode="after")
    def validate_search_context(self) -> HotelAvailabilitySearchResponse:
        if self.check_out != self.check_in + timedelta(days=self.stay_nights):
            raise ValueError("check_out must match stay_nights")
        if self.stay_days != self.stay_nights + 1:
            raise ValueError("stay_days must equal stay_nights plus one")
        if any(
            len(result.windows) > self.lookahead_days + 1 for result in self.results
        ):
            raise ValueError("results exceed the requested lookahead window")
        if any(
            window.requested_check_in != self.check_in
            or window.stay_nights != self.stay_nights
            for result in self.results
            for window in result.windows
        ):
            raise ValueError("result windows must match the search context")
        return self


class TripContextRequest(NexTripModel):
    place_ids: list[str] = Field(min_length=1, max_length=100)
    hotel_search: HotelAvailabilitySearchRequest | None = None
    route_legs: list[TripRouteLegRequest] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_scope(self) -> TripContextRequest:
        if len(set(self.place_ids)) != len(self.place_ids):
            raise ValueError("place_ids cannot contain duplicates")
        scope = set(self.place_ids)
        if self.hotel_search and not set(self.hotel_search.hotel_ids) <= scope:
            raise ValueError("hotel_search hotel_ids must be included in place_ids")
        route_ids = {
            place_id
            for leg in self.route_legs
            for place_id in (leg.origin_id, leg.destination_id)
        }
        if not route_ids <= scope:
            raise ValueError("route leg endpoints must be included in place_ids")
        return self


class TripContextRouteResult(NexTripModel):
    origin_id: str
    destination_id: str
    status: Literal["available", "unavailable"]
    recommendation: TransportRecommendationResponse | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> TripContextRouteResult:
        if self.status == "available" and self.recommendation is None:
            raise ValueError("available route requires a recommendation")
        if self.status == "unavailable" and self.recommendation is not None:
            raise ValueError("unavailable route cannot include a recommendation")
        return self


class TripContextResponse(NexTripModel):
    evaluated_at: AwareDatetime
    places: PlaceBatchResponse
    hotel_availability: HotelAvailabilitySearchResponse | None = None
    hotel_error_code: str | None = None
    routes: list[TripContextRouteResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hotel_result(self) -> TripContextResponse:
        if self.hotel_availability is not None and self.hotel_error_code is not None:
            raise ValueError("hotel result cannot include both data and an error")
        return self


class RepositoryReadiness(NexTripModel):
    ready: bool
    place_count: int = Field(ge=0)
    hotel_offer_count: int = Field(ge=0)
    trivago_mapping_count: int = Field(ge=0)
    issues: list[str] = Field(default_factory=list)
