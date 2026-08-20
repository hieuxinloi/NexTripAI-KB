from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable

from nextrip_pipeline.schemas import RoutingProvider

from .access_points import AccessPointRegistry
from .cache import SQLiteTrafficCache
from .config import TrafficSettings
from .errors import TrafficConfigurationError
from .providers.base import RoutingProviderAdapter
from .providers.here import HereRoutingProvider
from .providers.valhalla import ValhallaRoutingProvider
from .service import HybridTrafficService


def build_traffic_service(
    settings: TrafficSettings | None = None,
) -> HybridTrafficService:
    """Construct the traffic service without making provider network calls.

    The returned service owns its providers and SQLite cache. Call
    ``service.close()`` when using this factory outside the FastAPI lifespan.
    """

    cache: SQLiteTrafficCache | None = None
    providers: dict[RoutingProvider, RoutingProviderAdapter] = {}
    try:
        resolved = settings or TrafficSettings.from_env()
        current_place_root = resolved.current_place_root
        cache_path = resolved.cache_path
        if current_place_root is None or cache_path is None:
            raise TrafficConfigurationError(
                "current-place and cache paths must be configured"
            )

        override_path = _existing_override_path(resolved.access_point_overrides_path)
        registry = AccessPointRegistry(
            current_place_root,
            overrides_path=override_path,
        )
        cache = SQLiteTrafficCache(cache_path)
        if resolved.valhalla_enabled:
            providers[RoutingProvider.VALHALLA] = ValhallaRoutingProvider(
                base_url=resolved.valhalla_base_url,
                timeout_seconds=resolved.provider_timeout_seconds,
                route_ttl_seconds=resolved.free_flow_ttl_seconds,
                matrix_ttl_seconds=resolved.free_flow_ttl_seconds,
                max_matrix_cells=resolved.max_matrix_cells,
            )
        if resolved.here_enabled:
            providers[RoutingProvider.HERE] = HereRoutingProvider(
                api_key=resolved.here_api_key,
                routes_url=resolved.here_routes_url,
                timeout_seconds=resolved.provider_timeout_seconds,
                route_ttl_seconds=resolved.route_ttl_seconds,
                matrix_ttl_seconds=resolved.matrix_ttl_seconds,
                max_matrix_cells=resolved.max_matrix_cells,
            )
        return HybridTrafficService(
            registry=registry,
            cache=cache,
            providers=providers,
            settings=resolved,
        )
    except Exception as error:
        for provider in providers.values():
            try:
                provider.close()
            except Exception:
                pass
        if cache is not None:
            try:
                cache.close()
            except Exception:
                pass
        if isinstance(error, TrafficConfigurationError):
            raise
        raise TrafficConfigurationError(
            f"traffic runtime initialization failed: {error}"
        ) from error


def _existing_override_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidate = path.expanduser()
    return candidate if candidate.is_file() else None


@dataclass(slots=True)
class TrafficRuntime:
    """Resources exposed to HTTP handlers and closed as one unit."""

    service: HybridTrafficService

    @property
    def settings(self) -> TrafficSettings:
        return self.service.settings

    @property
    def registry(self) -> AccessPointRegistry:
        return self.service.registry

    def close(self) -> None:
        self.service.close()


def build_traffic_runtime(
    settings: TrafficSettings | None = None,
) -> TrafficRuntime:
    return TrafficRuntime(service=build_traffic_service(settings))


class TrafficRuntimeFactory:
    """Thread-safe, lazy owner for one process-local traffic runtime."""

    def __init__(
        self,
        builder: Callable[[], TrafficRuntime] | None = None,
    ) -> None:
        self._builder = builder or build_traffic_runtime
        self._runtime: TrafficRuntime | None = None
        self._lock = RLock()

    def get(self) -> TrafficRuntime:
        with self._lock:
            if self._runtime is None:
                self._runtime = self._builder()
            return self._runtime

    def peek(self) -> TrafficRuntime | None:
        with self._lock:
            return self._runtime

    def close(self) -> None:
        with self._lock:
            runtime, self._runtime = self._runtime, None
        if runtime is not None:
            runtime.close()

    def reset(self) -> None:
        """Close the current runtime so the next ``get`` rebuilds it."""

        self.close()
