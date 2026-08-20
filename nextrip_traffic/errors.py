from __future__ import annotations


class TrafficError(RuntimeError):
    """Base error exposed by the traffic domain."""


class TrafficConfigurationError(TrafficError):
    """The traffic service is missing or has invalid configuration."""


class AccessPointNotFoundError(TrafficError, LookupError):
    """A requested city/place/access-point cannot be resolved."""

    def __init__(self, identifier: str) -> None:
        super().__init__(f"unknown access point or alias: {identifier}")
        self.identifier = identifier


class InvalidTrafficRequestError(TrafficError, ValueError):
    """A semantically invalid request passed schema-level validation."""


class UnsupportedTransportModeError(TrafficError):
    """The selected provider cannot route the requested transport mode."""


class RoutingProviderError(TrafficError):
    """A routing provider failed or returned an invalid response."""


class RoutingProviderAuthenticationError(RoutingProviderError):
    """The provider rejected the configured credential."""


class RoutingProviderRateLimitError(RoutingProviderError):
    """The provider rejected the request because of a quota/rate limit."""


class RouteNotFoundError(RoutingProviderError):
    """No road-network route was available for the requested endpoints."""


class TrafficValidationError(TrafficError):
    """A provider result failed deterministic traffic validation."""
