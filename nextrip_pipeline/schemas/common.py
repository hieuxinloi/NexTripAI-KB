from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class NexTripModel(BaseModel):
    """Strict base contract shared by pipeline and traffic services."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EntityType(StrEnum):
    ATTRACTION = "attraction"
    CAFE = "cafe"
    HOTEL = "hotel"
    NIGHTLIFE = "nightlife"
    RESTAURANT = "restaurant"


# ``EntityType`` is retained for compatibility with the existing registry and
# crawl adapters. New canonical place contracts should use the more explicit
# ``PlaceType`` name.
PlaceType = EntityType


class RecordSubjectType(StrEnum):
    PLACE = "place"
    CITY = "city"
    ACCESS_POINT = "access_point"
    HOTEL_PRICE = "hotel_price"
    ADMISSION_PRICE = "admission_price"
    SPEND = "spend"
    RATING = "rating"
    OPENING_SCHEDULE = "opening_schedule"
    OPENING_STATUS = "opening_status"
    MENU = "menu"
    ROUTE = "route"
    ROUTE_MATRIX = "route_matrix"


class VerificationStatus(StrEnum):
    LEGACY_VERIFIED = "legacy_verified"
    AUTO_VERIFIED = "auto_verified"
    AGENT_VERIFIED = "agent_verified"
    HUMAN_VERIFIED = "human_verified"
    PENDING_REVIEW = "pending_review"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
