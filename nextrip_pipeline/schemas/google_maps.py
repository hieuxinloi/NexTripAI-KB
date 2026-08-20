from __future__ import annotations

from pydantic import AwareDatetime, Field, HttpUrl

from .common import NexTripModel, VerificationStatus
from .media import PlaceMediaObservation
from .menu import MenuSourceObservation
from .opening_status import OpeningStatusObservation, WeeklyOpeningScheduleObservation
from .place import BusinessStatus, GeoPoint


class GoogleMapsPlaceObservation(NexTripModel):
    """Auditable place/status projection extracted from one Maps capture."""

    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: HttpUrl
    name: str | None = None
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    business_status: BusinessStatus = BusinessStatus.UNKNOWN
    opening: OpeningStatusObservation
    weekly_opening: WeeklyOpeningScheduleObservation | None = None
    price_level: int | None = Field(default=None, ge=1, le=4)
    raw_price_text: str | None = None
    menu_source: MenuSourceObservation | None = None
    media: PlaceMediaObservation | None = None
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
