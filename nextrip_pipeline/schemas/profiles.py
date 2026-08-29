from __future__ import annotations

from datetime import time
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .common import NexTripModel, VerificationStatus


class VisitDuration(NexTripModel):
    minimum_minutes: int | None = Field(default=None, ge=0)
    recommended_minutes: int = Field(gt=0)
    maximum_minutes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> VisitDuration:
        if (
            self.minimum_minutes is not None
            and self.minimum_minutes > self.recommended_minutes
        ):
            raise ValueError("minimum_minutes cannot exceed recommended_minutes")
        if (
            self.maximum_minutes is not None
            and self.maximum_minutes < self.recommended_minutes
        ):
            raise ValueError("maximum_minutes cannot be below recommended_minutes")
        return self


class TimeWindow(NexTripModel):
    start: time
    end: time
    ends_next_day: bool = False


class AccessibilityProfile(NexTripModel):
    wheelchair: str | None = None
    parking: bool | None = None
    public_transport: bool | None = None
    note: str | None = None


class ReservationPolicy(NexTripModel):
    required: bool = False
    recommended: bool = False


class ProfileBase(NexTripModel):
    profile_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
    updated_at: AwareDatetime


class AttractionProfile(ProfileBase):
    profile_type: Literal["attraction"] = "attraction"
    is_indoor: bool | None = None
    location_scope: str | None = None
    visit_duration: VisitDuration | None = None
    best_months: list[int] = Field(default_factory=list)
    best_time_windows: list[TimeWindow] = Field(default_factory=list)
    weather_suitable: list[str] = Field(default_factory=list)
    weather_not_suitable: list[str] = Field(default_factory=list)
    accessibility: AccessibilityProfile = Field(default_factory=AccessibilityProfile)
    access_requirements: list[str] = Field(default_factory=list)
    not_recommended_for: list[str] = Field(default_factory=list)


class CafeProfile(ProfileBase):
    profile_type: Literal["cafe"] = "cafe"
    ambience: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    serves: list[str] = Field(default_factory=list)
    cuisines: list[str] = Field(default_factory=list)
    signature_items: list[str] = Field(default_factory=list)
    dietary_options: list[str] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    reservation: ReservationPolicy = Field(default_factory=ReservationPolicy)


class HotelProfile(ProfileBase):
    profile_type: Literal["hotel"] = "hotel"
    star_rating: int | None = Field(default=None, ge=1, le=5)
    hotel_styles: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    room_types: list[str] = Field(default_factory=list)
    check_in_time: time | None = None
    check_out_time: time | None = None
    beach_distance_meters: int | None = Field(default=None, ge=0)
    center_distance_meters: int | None = Field(default=None, ge=0)


class RestaurantProfile(ProfileBase):
    profile_type: Literal["restaurant"] = "restaurant"
    cuisines: list[str] = Field(default_factory=list)
    signature_dishes: list[str] = Field(default_factory=list)
    ambience: list[str] = Field(default_factory=list)
    serves: list[str] = Field(default_factory=list)
    dietary_options: list[str] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    reservation: ReservationPolicy = Field(default_factory=ReservationPolicy)


class HappyHour(NexTripModel):
    start: time | None = None
    end: time | None = None
    description: str | None = None


class NightlifeProfile(ProfileBase):
    profile_type: Literal["nightlife"] = "nightlife"
    venue_type: str | None = None
    music_genres: list[str] = Field(default_factory=list)
    vibe: list[str] = Field(default_factory=list)
    ambience: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    minimum_age: int | None = Field(default=None, ge=0)
    dress_code: str | None = None
    has_food: bool | None = None
    best_days: list[str] = Field(default_factory=list)
    happy_hour: HappyHour = Field(default_factory=HappyHour)
    reservation: ReservationPolicy = Field(default_factory=ReservationPolicy)


PlaceProfile = Annotated[
    AttractionProfile
    | CafeProfile
    | HotelProfile
    | RestaurantProfile
    | NightlifeProfile,
    Field(discriminator="profile_type"),
]
