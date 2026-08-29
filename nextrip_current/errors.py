from __future__ import annotations


class CurrentDataError(RuntimeError):
    """Base error for Current Data reads and integrations."""


class CurrentDataUnavailableError(CurrentDataError):
    """Raised when a configured current-data store is unavailable."""


class CurrentDataCorruptError(CurrentDataError):
    """Raised when a current projection cannot satisfy its persisted schema."""


class CurrentPlaceNotFoundError(CurrentDataError, LookupError):
    def __init__(self, place_id: str) -> None:
        self.place_id = place_id
        super().__init__(f"current place not found: {place_id}")


class HotelRefreshUnavailableError(CurrentDataError):
    """Raised when an on-demand hotel refresh was requested but is disabled."""


class HotelRefreshError(CurrentDataError):
    """Raised when the configured on-demand hotel refresher fails."""


class TrafficIntegrationUnavailableError(CurrentDataError):
    """Raised when typed traffic integration has not been configured."""


class TrafficIntegrationError(CurrentDataError):
    """Raised when the traffic service rejects or cannot complete a request."""
