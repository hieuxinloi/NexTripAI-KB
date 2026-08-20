from __future__ import annotations

from pydantic import AwareDatetime, Field, HttpUrl

from .common import EntityType, NexTripModel, VerificationStatus
from .opening_status import (
    OpeningStatusObservation,
    WeeklyOpeningScheduleObservation,
)
from .place import BusinessStatus, GeoPoint


class CurrentPlaceProvenance(NexTripModel):
    """Traceable evidence used to build one current place projection."""

    # Google Maps projections populate the quality-artifact identifiers below.
    # A verified-master baseline has no crawl observation, mapping decision, or
    # validation artifact, so those identifiers intentionally remain empty.
    mapping_id: str | None = Field(default=None, min_length=1)
    observation_id: str | None = Field(default=None, min_length=1)
    decision_id: str | None = Field(default=None, min_length=1)
    validation_ids: list[str] = Field(default_factory=list)
    run_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    source_file: str | None = None
    verification_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED
    observed_at: AwareDatetime | None = None
    decided_at: AwareDatetime | None = None


class CurrentPlaceOpeningHours(NexTripModel):
    """Opening-hours text retained exactly from the verified master record.

    Master data contains general hours, not a date-specific ``open_now`` fact.
    Keeping this separate from ``OpeningStatusObservation`` prevents the
    bootstrap process from inventing a daily status.
    """

    opens_at: str | None = None
    closes_at: str | None = None
    closed_days: list[str] | str = Field(default_factory=list)
    note: str | None = None


class CurrentPlaceSnapshot(NexTripModel):
    """Latest verified Google Maps state for one canonical master place.

    Identity fields are copied from master-data mapping metadata. All remaining
    place fields are a replaceable projection of one accepted Maps observation.
    """

    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city: str = Field(min_length=1)
    city_id: str | None = None

    name: str = Field(min_length=1)
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    business_status: BusinessStatus | None = None
    opening: OpeningStatusObservation | None = None
    weekly_opening: WeeklyOpeningScheduleObservation | None = None
    opening_hours: CurrentPlaceOpeningHours | None = None
    cover_image_url: HttpUrl | None = None
    price_level: int | None = Field(default=None, ge=1, le=4)

    # Per-field source ownership makes master fallbacks distinguishable after
    # a later Google Maps observation replaces only the fields it actually has.
    field_sources: dict[str, str] = Field(default_factory=dict)

    provenance: CurrentPlaceProvenance
    updated_at: AwareDatetime
    stale_after: AwareDatetime | None = None

    def is_stale(self, at: AwareDatetime) -> bool:
        return self.stale_after is not None and at > self.stale_after
