from __future__ import annotations

from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator

from .common import NexTripModel, VerificationStatus
from .place import GeoPoint
from .route import TransportMode


class AccessPointType(StrEnum):
    CITY_CENTER = "city_center"
    MAIN_ENTRANCE = "main_entrance"
    PARKING = "parking"
    HOTEL_DROP_OFF = "hotel_drop_off"
    STATION = "station"
    BUS_TERMINAL = "bus_terminal"
    AIRPORT = "airport"
    BOAT_TERMINAL = "boat_terminal"


class CityRecord(NexTripModel):
    """A canonical city used as an inter-city routing endpoint."""

    city_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    province: str | None = None
    country_code: str = Field(default="VN", pattern=r"^[A-Z]{2}$")
    timezone: str = "Asia/Ho_Chi_Minh"
    center: GeoPoint
    routing_access_point_id: str = Field(min_length=1)
    active: bool = True
    source_record_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
    updated_at: AwareDatetime

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value


class AccessPointRecord(NexTripModel):
    """A routable coordinate belonging to a place or city."""

    access_point_id: str = Field(min_length=1)
    owner_entity_id: str = Field(min_length=1)
    access_type: AccessPointType
    name: str | None = None
    location: GeoPoint
    supported_modes: list[TransportMode] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
    updated_at: AwareDatetime
