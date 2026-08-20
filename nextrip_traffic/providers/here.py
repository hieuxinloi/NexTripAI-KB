from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from nextrip_pipeline.schemas.place import GeoPoint
from nextrip_pipeline.schemas.route import (
    ProviderRole,
    RouteMatrixCell,
    RouteMatrixResult,
    RouteObservation,
    RoutingProvider,
    TrafficBasis,
    TransportMode,
)

from .base import (
    MatrixLimitExceededError,
    MatrixQuery,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderHealth,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    RouteNotFoundError,
    RouteQuery,
    RoutingProviderError,
    UnsupportedTransportModeError,
)


HERE_ROUTING_V8_URL = "https://router.hereapi.com/v8/routes"

_HERE_TRANSPORT_MODES: dict[TransportMode, str] = {
    TransportMode.WALK: "pedestrian",
    TransportMode.BICYCLE: "bicycle",
    TransportMode.TWO_WHEELER: "scooter",
    TransportMode.DRIVE: "car",
}


class HereRoutingProvider:
    """Synchronous HERE Routing v8 adapter.

    Matrix requests deliberately use a bounded route fan-out in this first
    implementation. It keeps the contract synchronous and avoids exposing the
    asynchronous HERE Matrix job lifecycle to the rest of the traffic service.
    """

    provider = RoutingProvider.HERE

    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.Client | None = None,
        routes_url: str = HERE_ROUTING_V8_URL,
        timeout_seconds: float = 20.0,
        route_ttl_seconds: int = 600,
        matrix_ttl_seconds: int = 600,
        max_matrix_cells: int = 25,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_matrix_cells < 1 or max_matrix_cells > 100:
            raise ValueError("max_matrix_cells must be between 1 and 100")
        if route_ttl_seconds < 1 or route_ttl_seconds > 86_400:
            raise ValueError("route_ttl_seconds must be between 1 and 86400")
        if matrix_ttl_seconds < 1 or matrix_ttl_seconds > 86_400:
            raise ValueError("matrix_ttl_seconds must be between 1 and 86400")
        self.api_key = api_key.strip() if api_key else None
        self.routes_url = routes_url
        self.route_ttl_seconds = route_ttl_seconds
        self.matrix_ttl_seconds = matrix_ttl_seconds
        self.max_matrix_cells = max_matrix_cells
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def route(self, query: RouteQuery) -> RouteObservation:
        api_key = self._require_api_key()
        transport_mode = self._transport_mode(query.mode)
        observed_at = self._aware_now()
        params = {
            "apiKey": api_key,
            "origin": self._format_point(query.origin.location),
            "destination": self._format_point(query.destination.location),
            "transportMode": transport_mode,
            "routingMode": "fast",
            "return": "polyline,summary",
            "departureTime": self._format_time(query.departure_time),
        }

        try:
            response = self.client.get(self.routes_url, params=params)
        except httpx.HTTPError as error:
            raise ProviderUnavailableError("HERE Routing API request failed") from error

        self._raise_for_status(response)
        payload = self._json_object(response)
        route, sections = self._select_route(payload)
        metrics = self._aggregate_sections(sections)
        traffic_aware, traffic_basis = self._traffic_provenance(
            query.mode,
            query.departure_time,
            observed_at,
        )
        base_duration = metrics["base_duration"]
        duration = metrics["duration"]

        return RouteObservation(
            observation_id=f"route-{uuid4().hex}",
            request_id=query.request_id,
            origin_access_point_id=query.origin.access_point_id,
            destination_access_point_id=query.destination.access_point_id,
            mode=query.mode,
            provider=self.provider,
            provider_role=query.provider_role,
            fallback_reason=query.fallback_reason,
            fallback_used=query.provider_role == ProviderRole.FALLBACK,
            traffic_aware=traffic_aware,
            traffic_basis=traffic_basis,
            distance_meters=metrics["distance"],
            duration_seconds=duration,
            base_duration_seconds=base_duration,
            traffic_delay_seconds=(
                max(0, duration - base_duration) if base_duration is not None else None
            ),
            encoded_polyline=metrics["polyline"],
            origin_snap_distance_meters=self._snap_distance(
                sections[0],
                key="departure",
                requested=query.origin.location,
            ),
            destination_snap_distance_meters=self._snap_distance(
                sections[-1],
                key="arrival",
                requested=query.destination.location,
            ),
            provider_request_id=self._provider_request_id(response, route),
            map_data_version=self._header(
                response.headers,
                "x-map-version",
                "x-here-map-version",
            ),
            departure_time=query.departure_time,
            observed_at=observed_at,
            expires_at=observed_at
            + timedelta(seconds=query.ttl_seconds or self.route_ttl_seconds),
            confidence=1.0,
        )

    def matrix(self, query: MatrixQuery) -> RouteMatrixResult:
        cell_count = len(query.origins) * len(query.destinations)
        allowed_cells = min(query.max_cells, self.max_matrix_cells)
        if cell_count > allowed_cells:
            raise MatrixLimitExceededError(
                f"matrix has {cell_count} cells, provider limit is {allowed_cells}"
            )

        observed_at = self._aware_now()
        cells: list[RouteMatrixCell] = []
        for origin_index, origin in enumerate(query.origins):
            for destination_index, destination in enumerate(query.destinations):
                if origin.access_point_id == destination.access_point_id:
                    cells.append(
                        RouteMatrixCell(
                            origin_index=origin_index,
                            destination_index=destination_index,
                            distance_meters=0,
                            duration_seconds=0,
                        )
                    )
                    continue

                route_query = RouteQuery(
                    request_id=(
                        f"{query.request_id}:{origin_index}:{destination_index}"
                    ),
                    origin=origin,
                    destination=destination,
                    mode=query.mode,
                    departure_time=query.departure_time,
                    provider_role=query.provider_role,
                    fallback_reason=query.fallback_reason,
                    ttl_seconds=query.ttl_seconds or self.matrix_ttl_seconds,
                )
                try:
                    route = self.route(route_query)
                except RouteNotFoundError:
                    cells.append(
                        RouteMatrixCell(
                            origin_index=origin_index,
                            destination_index=destination_index,
                            reachable=False,
                            error_code="route_not_found",
                        )
                    )
                except (
                    ProviderAuthenticationError,
                    ProviderRateLimitError,
                    ProviderUnavailableError,
                ):
                    # These failures apply to the provider as a whole. Stop the
                    # fan-out instead of spending quota on guaranteed failures.
                    raise
                except ProviderResponseError:
                    cells.append(
                        RouteMatrixCell(
                            origin_index=origin_index,
                            destination_index=destination_index,
                            reachable=False,
                            error_code="invalid_provider_response",
                        )
                    )
                except RoutingProviderError:
                    cells.append(
                        RouteMatrixCell(
                            origin_index=origin_index,
                            destination_index=destination_index,
                            reachable=False,
                            error_code="provider_request_rejected",
                        )
                    )
                else:
                    cells.append(
                        RouteMatrixCell(
                            origin_index=origin_index,
                            destination_index=destination_index,
                            distance_meters=route.distance_meters,
                            duration_seconds=route.duration_seconds,
                        )
                    )

        traffic_aware, traffic_basis = self._traffic_provenance(
            query.mode,
            query.departure_time,
            observed_at,
        )
        return RouteMatrixResult(
            matrix_id=f"matrix-{uuid4().hex}",
            request_id=query.request_id,
            provider=self.provider,
            provider_role=query.provider_role,
            fallback_reason=query.fallback_reason,
            fallback_used=query.provider_role == ProviderRole.FALLBACK,
            mode=query.mode,
            origin_access_point_ids=[item.access_point_id for item in query.origins],
            destination_access_point_ids=[
                item.access_point_id for item in query.destinations
            ],
            cells=cells,
            traffic_aware=traffic_aware,
            traffic_basis=traffic_basis,
            departure_time=query.departure_time,
            observed_at=observed_at,
            expires_at=observed_at
            + timedelta(seconds=query.ttl_seconds or self.matrix_ttl_seconds),
        )

    def health(self) -> ProviderHealth:
        configured = bool(self.api_key)
        return ProviderHealth(
            provider=self.provider,
            configured=configured,
            # HERE exposes no free, non-routing health endpoint. Avoid spending
            # quota merely to render service health; the first route request
            # records actual reachability through its typed result/error.
            available=None if configured else False,
            checked_at=self._aware_now(),
            detail=(
                "HERE API key configured; reachability not probed"
                if configured
                else "HERE_API_KEY is not configured"
            ),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> HereRoutingProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise ProviderConfigurationError("HERE_API_KEY is not configured")
        return self.api_key

    @staticmethod
    def _transport_mode(mode: TransportMode) -> str:
        try:
            return _HERE_TRANSPORT_MODES[mode]
        except KeyError as error:
            raise UnsupportedTransportModeError(
                f"HERE provider does not support transport mode '{mode.value}'"
            ) from error

    @staticmethod
    def _format_point(point: GeoPoint) -> str:
        return f"{point.latitude:.7f},{point.longitude:.7f}"

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "HERE provider clock must return a timezone-aware datetime"
            )
        return value

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status in {401, 403}:
            raise ProviderAuthenticationError(
                "HERE Routing API rejected the configured credentials"
            )
        if status == 429:
            raise ProviderRateLimitError("HERE Routing API rate limit exceeded")
        if status >= 500:
            raise ProviderUnavailableError(
                "HERE Routing API is temporarily unavailable"
            )
        if status >= 400:
            raise RoutingProviderError(
                f"HERE Routing API rejected the request with HTTP {status}"
            )

    @staticmethod
    def _json_object(response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderResponseError(
                "HERE Routing API returned malformed JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise ProviderResponseError(
                "HERE Routing API response must be a JSON object"
            )
        return payload

    @staticmethod
    def _select_route(
        payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        routes = payload.get("routes")
        if not isinstance(routes, list) or not routes:
            raise RouteNotFoundError("HERE Routing API returned no route")
        route = routes[0]
        if not isinstance(route, Mapping):
            raise ProviderResponseError("HERE route entry must be an object")
        raw_sections = route.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ProviderResponseError("HERE route has no sections")
        sections: list[Mapping[str, Any]] = []
        for raw_section in raw_sections:
            if not isinstance(raw_section, Mapping):
                raise ProviderResponseError("HERE route section must be an object")
            sections.append(raw_section)
        return route, sections

    @staticmethod
    def _aggregate_sections(
        sections: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        total_distance = 0
        total_duration = 0
        total_base_duration = 0
        every_base_duration = True
        polyline: str | None = None

        for section in sections:
            summary = section.get("summary")
            if not isinstance(summary, Mapping):
                raise ProviderResponseError("HERE route section has no summary")
            distance = summary.get("length")
            duration = summary.get("duration")
            if not HereRoutingProvider._is_positive_int(distance):
                raise ProviderResponseError(
                    "HERE route section has invalid summary.length"
                )
            if not HereRoutingProvider._is_positive_int(duration):
                raise ProviderResponseError(
                    "HERE route section has invalid summary.duration"
                )
            total_distance += int(distance)
            total_duration += int(duration)

            base_duration = summary.get("baseDuration")
            if HereRoutingProvider._is_positive_int(base_duration):
                total_base_duration += int(base_duration)
            else:
                every_base_duration = False

            candidate = section.get("polyline")
            if polyline is None and isinstance(candidate, str) and candidate.strip():
                polyline = candidate.strip()

        if polyline is None:
            raise ProviderResponseError("HERE route contains no polyline")
        return {
            "distance": total_distance,
            "duration": total_duration,
            "base_duration": (total_base_duration if every_base_duration else None),
            "polyline": polyline,
        }

    @staticmethod
    def _is_positive_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _provider_request_id(
        response: httpx.Response,
        route: Mapping[str, Any],
    ) -> str | None:
        header_id = HereRoutingProvider._header(
            response.headers,
            "x-correlation-id",
            "x-request-id",
        )
        if header_id:
            return header_id
        route_id = route.get("id")
        return route_id if isinstance(route_id, str) and route_id.strip() else None

    @staticmethod
    def _header(headers: httpx.Headers, *names: str) -> str | None:
        for name in names:
            value = headers.get(name)
            if value and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _traffic_provenance(
        mode: TransportMode,
        departure_time: datetime,
        observed_at: datetime,
    ) -> tuple[bool, TrafficBasis]:
        if mode not in {TransportMode.DRIVE, TransportMode.TWO_WHEELER}:
            return False, TrafficBasis.FREE_FLOW
        delta_seconds = (departure_time - observed_at).total_seconds()
        if delta_seconds < -900:
            return True, TrafficBasis.HISTORICAL
        if delta_seconds > 900:
            return True, TrafficBasis.PREDICTED
        return True, TrafficBasis.CURRENT

    @staticmethod
    def _snap_distance(
        section: Mapping[str, Any],
        *,
        key: str,
        requested: GeoPoint,
    ) -> int | None:
        endpoint = section.get(key)
        if not isinstance(endpoint, Mapping):
            return None
        place = endpoint.get("place")
        if not isinstance(place, Mapping):
            return None
        location = place.get("location")
        if not isinstance(location, Mapping):
            return None
        latitude = location.get("lat")
        longitude = location.get("lng")
        if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
            return None
        if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
            return None
        return round(
            HereRoutingProvider._haversine_meters(
                requested.latitude,
                requested.longitude,
                float(latitude),
                float(longitude),
            )
        )

    @staticmethod
    def _haversine_meters(
        latitude_a: float,
        longitude_a: float,
        latitude_b: float,
        longitude_b: float,
    ) -> float:
        radius = 6_371_000.0
        phi_a = math.radians(latitude_a)
        phi_b = math.radians(latitude_b)
        delta_phi = math.radians(latitude_b - latitude_a)
        delta_lambda = math.radians(longitude_b - longitude_a)
        haversine = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
        )
        return radius * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))
