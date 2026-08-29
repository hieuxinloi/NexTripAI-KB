from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, HttpUrl

from .common import NexTripModel, VerificationStatus


class PlaceMediaRole(StrEnum):
    COVER = "cover"
    GALLERY = "gallery"
    MENU = "menu"


class PlaceMediaAsset(NexTripModel):
    url: HttpUrl
    role: PlaceMediaRole = PlaceMediaRole.GALLERY
    alt_text: str | None = None


class PlaceMediaObservation(NexTripModel):
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_url: HttpUrl
    assets: list[PlaceMediaAsset] = Field(default_factory=list)
    observed_at: AwareDatetime
    verification_status: VerificationStatus = VerificationStatus.PENDING_REVIEW
