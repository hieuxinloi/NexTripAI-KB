from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from .common import NexTripModel
from .validation import ValidationStatus


class TransportMode(StrEnum):
    WALK = "walk"
    BICYCLE = "bicycle"
    TWO_WHEELER = "two_wheeler"
    DRIVE = "drive"
    TRANSIT = "transit"


class RoutingProvider(StrEnum):
    HERE = "here"
    GOOGLE = "google"


class ProviderRole(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


class RouteObservation(NexTripModel):
    """Expiring route estimate returned by HERE or Google."""

    observation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    origin_access_point_id: str = Field(min_length=1)
    destination_access_point_id: str = Field(min_length=1)
    mode: TransportMode
    provider: RoutingProvider
    provider_role: ProviderRole
    fallback_reason: str | None = None
    distance_meters: int = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    base_duration_seconds: int | None = Field(default=None, gt=0)
    traffic_delay_seconds: int | None = Field(default=None, ge=0)
    encoded_polyline: str | None = None
    route_geojson: dict[str, object] | None = None
    departure_time: AwareDatetime
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    validation_status: ValidationStatus = ValidationStatus.PASS
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_route(self) -> RouteObservation:
        if self.origin_access_point_id == self.destination_access_point_id:
            raise ValueError("origin and destination must be different")
        if not self.encoded_polyline and not self.route_geojson:
            raise ValueError("route geometry is required")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        if self.provider_role == ProviderRole.FALLBACK and not self.fallback_reason:
            raise ValueError("fallback routes must include fallback_reason")
        return self

