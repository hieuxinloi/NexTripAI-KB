from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.schemas.city import AccessPointRecord
from nextrip_pipeline.schemas.common import NexTripModel
from nextrip_pipeline.schemas.place import GeoPoint
from nextrip_pipeline.schemas.route import (
    ProviderRole,
    RouteMatrixResult,
    RouteObservation,
    RoutingProvider,
    TransportMode,
)
from nextrip_traffic.errors import (
    RouteNotFoundError as RouteNotFoundError,
    RoutingProviderAuthenticationError,
    RoutingProviderError,
    RoutingProviderRateLimitError,
    TrafficConfigurationError,
    UnsupportedTransportModeError as UnsupportedTransportModeError,
)


# Short provider-facing aliases retain the canonical traffic-domain exception
# classes, so API/service layers can catch either spelling without translation.
ProviderConfigurationError = TrafficConfigurationError
ProviderAuthenticationError = RoutingProviderAuthenticationError
ProviderRateLimitError = RoutingProviderRateLimitError


class RouteEndpoint(NexTripModel):
    """Resolved routing endpoint used by provider adapters."""

    access_point_id: str = Field(min_length=1)
    location: GeoPoint

    @classmethod
    def from_access_point(cls, value: AccessPointRecord) -> RouteEndpoint:
        return cls(
            access_point_id=value.access_point_id,
            location=value.location,
        )


class RouteQuery(NexTripModel):
    request_id: str = Field(min_length=1)
    origin: RouteEndpoint
    destination: RouteEndpoint
    mode: TransportMode
    departure_time: AwareDatetime
    provider_role: ProviderRole = ProviderRole.PRIMARY
    fallback_reason: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_endpoints_and_role(self) -> RouteQuery:
        if self.origin.access_point_id == self.destination.access_point_id:
            raise ValueError("origin and destination must be different")
        if self.provider_role == ProviderRole.FALLBACK and not self.fallback_reason:
            raise ValueError("fallback requests must include fallback_reason")
        return self


class MatrixQuery(NexTripModel):
    request_id: str = Field(min_length=1)
    origins: list[RouteEndpoint] = Field(min_length=1)
    destinations: list[RouteEndpoint] = Field(min_length=1)
    mode: TransportMode
    departure_time: AwareDatetime
    provider_role: ProviderRole = ProviderRole.PRIMARY
    fallback_reason: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=1, le=86_400)
    max_cells: int = Field(default=25, ge=1, le=100)

    @model_validator(mode="after")
    def validate_matrix_bounds_and_role(self) -> MatrixQuery:
        cell_count = len(self.origins) * len(self.destinations)
        if cell_count > self.max_cells:
            raise ValueError(
                f"matrix has {cell_count} cells, exceeding max_cells={self.max_cells}"
            )
        if self.provider_role == ProviderRole.FALLBACK and not self.fallback_reason:
            raise ValueError("fallback requests must include fallback_reason")
        return self


class ProviderHealth(NexTripModel):
    provider: RoutingProvider
    configured: bool
    available: bool | None
    checked_at: AwareDatetime
    detail: str | None = None


class ProviderUnavailableError(RoutingProviderError):
    """The provider could not be reached or returned a transient 5xx."""


class ProviderResponseError(RoutingProviderError):
    """The provider returned a response that violates its documented schema."""


class MatrixLimitExceededError(RoutingProviderError):
    """A fan-out matrix would exceed the configured safety bound."""


@runtime_checkable
class RoutingProviderAdapter(Protocol):
    provider: RoutingProvider

    def route(self, query: RouteQuery) -> RouteObservation: ...

    def matrix(self, query: MatrixQuery) -> RouteMatrixResult: ...

    def health(self) -> ProviderHealth: ...

    def close(self) -> None: ...


def ensure_aware(value: datetime, *, field_name: str) -> datetime:
    """Defensive helper for callers outside the Pydantic request contracts."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value
