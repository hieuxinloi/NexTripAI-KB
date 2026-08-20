from __future__ import annotations

from threading import RLock
from typing import Callable

from .config import CurrentDataSettings
from .repository import CurrentDataRepository
from .service import CurrentDataService, HotelPriceRefresher
from .traffic import TrafficHttpClient


def build_current_data_service(
    settings: CurrentDataSettings | None = None,
    *,
    hotel_refresher: HotelPriceRefresher | None = None,
    traffic_client: TrafficHttpClient | None = None,
) -> CurrentDataService:
    """Build the shared service without performing network calls or writes."""

    resolved = settings or CurrentDataSettings.from_env()
    place_root = resolved.current_place_root
    price_root = resolved.current_hotel_price_root
    availability_root = resolved.current_hotel_availability_root
    mapping_root = resolved.current_trivago_mapping_root
    if (
        place_root is None
        or price_root is None
        or availability_root is None
        or mapping_root is None
    ):
        raise ValueError("Current Data repository paths must be configured")
    repository = CurrentDataRepository(
        place_root=place_root,
        hotel_price_root=price_root,
        hotel_availability_root=availability_root,
        trivago_mapping_root=mapping_root,
    )
    resolved_refresher = None
    if resolved.trivago_refresh_enabled:
        resolved_refresher = hotel_refresher
        if resolved_refresher is None:
            # Keep the crawler and its network client outside import-time app
            # construction. The feature remains off unless explicitly enabled.
            from .refresh import (
                TrivagoOnDemandPriceRefresher,
                TrivagoRefreshPaths,
            )

            resolved_refresher = TrivagoOnDemandPriceRefresher(
                TrivagoRefreshPaths.from_kb_root(resolved.kb_root)
            )
    resolved_traffic = traffic_client
    if resolved_traffic is None and resolved.traffic_api_base_url is not None:
        resolved_traffic = TrafficHttpClient(
            base_url=resolved.traffic_api_base_url,
            api_key=resolved.traffic_api_key,
            timeout_seconds=resolved.traffic_timeout_seconds,
        )
    return CurrentDataService(
        repository,
        hotel_refresher=resolved_refresher,
        traffic_client=resolved_traffic,
    )


class CurrentDataServiceFactory:
    """Thread-safe lazy owner for one process-local Current Data service."""

    def __init__(
        self,
        builder: Callable[[], CurrentDataService] | None = None,
    ) -> None:
        self._builder = builder or build_current_data_service
        self._service: CurrentDataService | None = None
        self._lock = RLock()

    def get(self) -> CurrentDataService:
        with self._lock:
            if self._service is None:
                self._service = self._builder()
            return self._service

    def peek(self) -> CurrentDataService | None:
        with self._lock:
            return self._service

    def close(self) -> None:
        with self._lock:
            service, self._service = self._service, None
        if service is not None:
            service.close()

    def reset(self) -> None:
        self.close()
