from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl

from .common import EntityType, NexTripModel, VerificationStatus


class BusinessStatus(StrEnum):
    ACTIVE = "active"
    TEMPORARILY_CLOSED = "temporarily_closed"
    PERMANENTLY_CLOSED = "permanently_closed"
    UNKNOWN = "unknown"


class Address(NexTripModel):
    formatted: str | None = None
    street: str | None = None
    ward: str | None = None
    district: str | None = None
    province: str | None = None
    country_code: str = Field(default="VN", pattern=r"^[A-Z]{2}$")


class GeoPoint(NexTripModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PlaceRecord(NexTripModel):
    """Canonical normalized place linked back to captured source records."""

    place_id: str = Field(min_length=1)
    entity_type: EntityType
    name: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    address: Address | None = None
    location: GeoPoint | None = None
    phone: str | None = None
    website: HttpUrl | None = None
    business_status: BusinessStatus = BusinessStatus.UNKNOWN
    source_record_ids: list[str] = Field(min_length=1)
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
    normalized_at: AwareDatetime

