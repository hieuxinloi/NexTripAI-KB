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
    VALHALLA = "valhalla"


class ProviderRole(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


class TrafficBasis(StrEnum):
    CURRENT = "current"
    PREDICTED = "predicted"
    FREE_FLOW = "free_flow"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


class RouteObservation(NexTripModel):
    """Expiring road-network route estimate with explicit traffic provenance."""

    observation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    origin_access_point_id: str = Field(min_length=1)
    destination_access_point_id: str = Field(min_length=1)
    mode: TransportMode
    provider: RoutingProvider
    provider_role: ProviderRole
    fallback_reason: str | None = None
    fallback_used: bool = False
    traffic_aware: bool = False
    traffic_basis: TrafficBasis = TrafficBasis.UNKNOWN
    distance_meters: int = Field(gt=0)
    duration_seconds: int = Field(gt=0)
    base_duration_seconds: int | None = Field(default=None, gt=0)
    traffic_delay_seconds: int | None = Field(default=None, ge=0)
    encoded_polyline: str | None = None
    route_geojson: dict[str, object] | None = None
    origin_snap_distance_meters: int | None = Field(default=None, ge=0)
    destination_snap_distance_meters: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = None
    map_data_version: str | None = None
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
        if self.provider_role == ProviderRole.FALLBACK:
            object.__setattr__(self, "fallback_used", True)
        if self.traffic_basis == TrafficBasis.FREE_FLOW and self.traffic_aware:
            raise ValueError("free_flow route cannot be marked traffic_aware")
        return self


class RouteMatrixCell(NexTripModel):
    origin_index: int = Field(ge=0)
    destination_index: int = Field(ge=0)
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    reachable: bool = True
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_reachability(self) -> RouteMatrixCell:
        if self.reachable and (
            self.distance_meters is None or self.duration_seconds is None
        ):
            raise ValueError("reachable matrix cell requires distance and duration")
        return self


class RouteMatrixResult(NexTripModel):
    matrix_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    provider: RoutingProvider
    provider_role: ProviderRole
    fallback_reason: str | None = None
    fallback_used: bool = False
    mode: TransportMode
    origin_access_point_ids: list[str] = Field(min_length=1)
    destination_access_point_ids: list[str] = Field(min_length=1)
    cells: list[RouteMatrixCell] = Field(min_length=1)
    traffic_aware: bool = False
    traffic_basis: TrafficBasis = TrafficBasis.UNKNOWN
    departure_time: AwareDatetime
    observed_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_matrix(self) -> RouteMatrixResult:
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        pairs = {(cell.origin_index, cell.destination_index) for cell in self.cells}
        if len(pairs) != len(self.cells):
            raise ValueError("route matrix cannot contain duplicate cells")
        if any(i >= len(self.origin_access_point_ids) for i, _ in pairs):
            raise ValueError("matrix origin_index is out of range")
        if any(j >= len(self.destination_access_point_ids) for _, j in pairs):
            raise ValueError("matrix destination_index is out of range")
        if self.provider_role == ProviderRole.FALLBACK and not self.fallback_reason:
            raise ValueError("fallback matrices must include fallback_reason")
        if self.provider_role == ProviderRole.FALLBACK:
            object.__setattr__(self, "fallback_used", True)
        if self.traffic_basis == TrafficBasis.FREE_FLOW and self.traffic_aware:
            raise ValueError("free_flow matrix cannot be marked traffic_aware")
        return self
