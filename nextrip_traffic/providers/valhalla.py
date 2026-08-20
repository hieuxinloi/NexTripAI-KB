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
    ProviderHealth,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    RouteNotFoundError,
    RouteQuery,
    RoutingProviderError,
    UnsupportedTransportModeError,
)


_VALHALLA_COSTING: dict[TransportMode, str] = {
    TransportMode.DRIVE: "auto",
    TransportMode.WALK: "pedestrian",
    TransportMode.BICYCLE: "bicycle",
    TransportMode.TWO_WHEELER: "motor_scooter",
}

# Valhalla reports these as request errors even though they mean that a
# road-network connection cannot be produced for otherwise valid locations.
_NO_ROUTE_ERROR_CODES = {154, 170, 171, 172, 441, 442, 443, 444}
_NO_ROUTE_MESSAGES = (
    "no path",
    "unreachable",
    "cannot reach",
    "unconnected regions",
    "no suitable edges",
    "no data found for location",
    "path distance exceeds",
)


class ValhallaRoutingProvider:
    """Synchronous adapter for a self-hosted Valhalla HTTP service.

    The adapter deliberately treats route and matrix durations as free-flow
    estimates. A healthy Valhalla instance may have live-traffic tiles loaded,
    but ``/status`` only proves availability of those tiles, not that a
    particular result used them. Traffic-aware provenance can be added later
    when the deployment exposes route-level evidence.
    """

    provider = RoutingProvider.VALHALLA

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
        route_ttl_seconds: int = 86_400,
        matrix_ttl_seconds: int = 86_400,
        max_matrix_cells: int = 25,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_matrix_cells < 1 or max_matrix_cells > 100:
            raise ValueError("max_matrix_cells must be between 1 and 100")
        if route_ttl_seconds < 1 or route_ttl_seconds > 86_400:
            raise ValueError("route_ttl_seconds must be between 1 and 86400")
        if matrix_ttl_seconds < 1 or matrix_ttl_seconds > 86_400:
            raise ValueError("matrix_ttl_seconds must be between 1 and 86400")

        normalized_url = base_url.strip().rstrip("/")
        try:
            parsed_url = httpx.URL(normalized_url)
        except (TypeError, ValueError) as error:
            raise ValueError("base_url must be a valid absolute HTTP URL") from error
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.host:
            raise ValueError("base_url must be a valid absolute HTTP URL")

        self.base_url = normalized_url
        self.route_ttl_seconds = route_ttl_seconds
        self.matrix_ttl_seconds = matrix_ttl_seconds
        self.max_matrix_cells = max_matrix_cells
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)
        self._map_data_version: str | None = None

    def route(self, query: RouteQuery) -> RouteObservation:
        costing = self._costing(query.mode)
        observed_at = self._aware_now()
        payload = {
            "id": query.request_id,
            "locations": [
                self._location(query.origin.location),
                self._location(query.destination.location),
            ],
            "costing": costing,
            "units": "kilometers",
            "shape_format": "polyline6",
        }
        response = self._post("/route", payload, operation="route")
        response_payload = self._json_object(response, operation="route")
        trip = self._trip(response_payload)
        summary = trip.get("summary")
        if not isinstance(summary, Mapping):
            raise ProviderResponseError("Valhalla route has no trip.summary object")

        distance_meters = self._distance_meters(
            summary.get("length"),
            units=trip.get("units"),
            field_name="trip.summary.length",
            allow_zero=False,
        )
        duration_seconds = self._seconds(
            summary.get("time"),
            field_name="trip.summary.time",
            allow_zero=False,
        )
        shape = self._route_shape(trip)

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
            traffic_aware=False,
            traffic_basis=TrafficBasis.FREE_FLOW,
            distance_meters=distance_meters,
            duration_seconds=duration_seconds,
            base_duration_seconds=duration_seconds,
            traffic_delay_seconds=0,
            encoded_polyline=shape,
            provider_request_id=self._provider_request_id(
                response,
                response_payload,
            ),
            map_data_version=(
                self._header(response.headers, "x-map-version")
                or self._map_data_version
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

        costing = self._costing(query.mode)
        observed_at = self._aware_now()
        payload = {
            "id": query.request_id,
            "sources": [self._location(item.location) for item in query.origins],
            "targets": [self._location(item.location) for item in query.destinations],
            "costing": costing,
            "units": "kilometers",
            "verbose": True,
            "shape_format": "no_shape",
        }
        response = self._post(
            "/sources_to_targets",
            payload,
            operation="matrix",
        )
        response_payload = self._json_object(response, operation="matrix")
        cells = self._matrix_cells(
            response_payload,
            origin_count=len(query.origins),
            destination_count=len(query.destinations),
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
            traffic_aware=False,
            traffic_basis=TrafficBasis.FREE_FLOW,
            departure_time=query.departure_time,
            observed_at=observed_at,
            expires_at=observed_at
            + timedelta(seconds=query.ttl_seconds or self.matrix_ttl_seconds),
        )

    def health(self) -> ProviderHealth:
        checked_at = self._aware_now()
        try:
            response = self.client.get(f"{self.base_url}/status")
        except httpx.HTTPError as error:
            return ProviderHealth(
                provider=self.provider,
                configured=True,
                available=False,
                checked_at=checked_at,
                detail=f"Valhalla status request failed: {type(error).__name__}",
            )

        if response.status_code != 200:
            return ProviderHealth(
                provider=self.provider,
                configured=True,
                available=False,
                checked_at=checked_at,
                detail=f"Valhalla status returned HTTP {response.status_code}",
            )

        try:
            payload = self._json_object(response, operation="status")
        except ProviderResponseError as error:
            return ProviderHealth(
                provider=self.provider,
                configured=True,
                available=False,
                checked_at=checked_at,
                detail=str(error),
            )

        version = payload.get("version")
        if not isinstance(version, str) or not version.strip():
            return ProviderHealth(
                provider=self.provider,
                configured=True,
                available=False,
                checked_at=checked_at,
                detail="Valhalla status response has no version",
            )

        has_tiles = payload.get("has_tiles")
        available = has_tiles is not False
        tileset_modified = payload.get("tileset_last_modified")
        if self._is_number(tileset_modified) and float(tileset_modified) >= 0:
            self._map_data_version = f"tileset-{int(float(tileset_modified))}"

        live_traffic = payload.get("has_live_traffic")
        traffic_detail = (
            "yes"
            if live_traffic is True
            else "no"
            if live_traffic is False
            else "unknown"
        )
        details = [f"Valhalla {version.strip()}"]
        if self._map_data_version:
            details.append(self._map_data_version)
        details.append(f"live_traffic={traffic_detail}")
        if has_tiles is False:
            details.append("no routing tiles loaded")

        return ProviderHealth(
            provider=self.provider,
            configured=True,
            available=available,
            checked_at=checked_at,
            detail="; ".join(details),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ValhallaRoutingProvider:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _costing(mode: TransportMode) -> str:
        try:
            return _VALHALLA_COSTING[mode]
        except KeyError as error:
            raise UnsupportedTransportModeError(
                f"Valhalla provider does not support transport mode '{mode.value}'"
            ) from error

    @staticmethod
    def _location(point: GeoPoint) -> dict[str, float]:
        return {
            "lat": round(point.latitude, 7),
            "lon": round(point.longitude, 7),
        }

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "Valhalla provider clock must return a timezone-aware datetime"
            )
        return value

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        operation: str,
    ) -> httpx.Response:
        try:
            response = self.client.post(
                f"{self.base_url}{path}",
                json=payload,
            )
        except httpx.HTTPError as error:
            raise ProviderUnavailableError(
                f"Valhalla {operation} request failed"
            ) from error
        self._raise_for_status(response, operation=operation)
        return response

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        *,
        operation: str,
    ) -> None:
        status = response.status_code
        if status in {401, 403}:
            raise ProviderAuthenticationError(
                "Valhalla service rejected the configured credentials"
            )
        if status == 429:
            raise ProviderRateLimitError("Valhalla service rate limit exceeded")
        if status >= 500:
            raise ProviderUnavailableError(
                "Valhalla service is temporarily unavailable"
            )
        if status >= 400:
            error_code, detail = ValhallaRoutingProvider._error_details(response)
            if ValhallaRoutingProvider._is_no_route(error_code, detail):
                raise RouteNotFoundError(f"Valhalla returned no {operation}: {detail}")
            suffix = f": {detail}" if detail else ""
            raise RoutingProviderError(
                f"Valhalla rejected the {operation} request with HTTP {status}{suffix}"
            )

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[int | None, str]:
        try:
            payload = response.json()
        except ValueError:
            return None, response.text.strip()[:300]
        if not isinstance(payload, Mapping):
            return None, str(payload)[:300]
        raw_code = payload.get("error_code")
        error_code = (
            raw_code
            if isinstance(raw_code, int) and not isinstance(raw_code, bool)
            else None
        )
        for key in ("error", "status_message", "status"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return error_code, value.strip()[:300]
        return error_code, ""

    @staticmethod
    def _is_no_route(error_code: int | None, detail: str) -> bool:
        if error_code in _NO_ROUTE_ERROR_CODES:
            return True
        normalized_detail = detail.casefold()
        return any(message in normalized_detail for message in _NO_ROUTE_MESSAGES)

    @staticmethod
    def _json_object(
        response: httpx.Response,
        *,
        operation: str,
    ) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderResponseError(
                f"Valhalla {operation} returned malformed JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise ProviderResponseError(
                f"Valhalla {operation} response must be a JSON object"
            )
        return payload

    @staticmethod
    def _trip(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        error_code = payload.get("error_code")
        detail = payload.get("error")
        normalized_code = (
            error_code
            if isinstance(error_code, int) and not isinstance(error_code, bool)
            else None
        )
        normalized_detail = detail if isinstance(detail, str) else ""
        if ValhallaRoutingProvider._is_no_route(
            normalized_code,
            normalized_detail,
        ):
            raise RouteNotFoundError(
                f"Valhalla returned no route: {normalized_detail or normalized_code}"
            )

        trip = payload.get("trip")
        if not isinstance(trip, Mapping):
            raise ProviderResponseError("Valhalla route has no trip object")
        status = trip.get("status")
        if (
            isinstance(status, (int, float))
            and not isinstance(status, bool)
            and status != 0
        ):
            message = trip.get("status_message")
            detail = message if isinstance(message, str) else f"status={status}"
            if ValhallaRoutingProvider._is_no_route(None, detail):
                raise RouteNotFoundError(f"Valhalla returned no route: {detail}")
            raise ProviderResponseError(f"Valhalla route failed: {detail}")
        return trip

    @staticmethod
    def _route_shape(trip: Mapping[str, Any]) -> str:
        top_level_shape = trip.get("shape")
        if isinstance(top_level_shape, str) and top_level_shape.strip():
            return top_level_shape.strip()

        legs = trip.get("legs")
        if not isinstance(legs, list) or not legs:
            raise ProviderResponseError("Valhalla route has no trip.legs")
        first_leg = legs[0]
        if not isinstance(first_leg, Mapping):
            raise ProviderResponseError("Valhalla route leg must be an object")
        shape = first_leg.get("shape")
        if not isinstance(shape, str) or not shape.strip():
            raise ProviderResponseError("Valhalla route leg has no shape")
        return shape.strip()

    @staticmethod
    def _provider_request_id(
        response: httpx.Response,
        payload: Mapping[str, Any],
    ) -> str | None:
        header_id = ValhallaRoutingProvider._header(
            response.headers,
            "x-request-id",
            "x-correlation-id",
        )
        if header_id:
            return header_id
        request_id = payload.get("id")
        return (
            request_id.strip()
            if isinstance(request_id, str) and request_id.strip()
            else None
        )

    @staticmethod
    def _header(headers: httpx.Headers, *names: str) -> str | None:
        for name in names:
            value = headers.get(name)
            if value and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _matrix_cells(
        payload: Mapping[str, Any],
        *,
        origin_count: int,
        destination_count: int,
    ) -> list[RouteMatrixCell]:
        raw_matrix = payload.get("sources_to_targets")
        entries = ValhallaRoutingProvider._matrix_entries(
            raw_matrix,
            origin_count=origin_count,
            destination_count=destination_count,
        )
        units = payload.get("units")
        by_pair: dict[tuple[int, int], RouteMatrixCell] = {}

        for origin_index, destination_index, entry in entries:
            pair = (origin_index, destination_index)
            if pair in by_pair:
                raise ProviderResponseError(
                    "Valhalla matrix contains duplicate source/target cells"
                )
            if origin_index < 0 or origin_index >= origin_count:
                raise ProviderResponseError(
                    "Valhalla matrix from_index is out of range"
                )
            if destination_index < 0 or destination_index >= destination_count:
                raise ProviderResponseError("Valhalla matrix to_index is out of range")

            distance = entry.get("distance")
            duration = entry.get("time")
            if distance is None or duration is None:
                by_pair[pair] = RouteMatrixCell(
                    origin_index=origin_index,
                    destination_index=destination_index,
                    reachable=False,
                    error_code="route_not_found",
                )
                continue

            by_pair[pair] = RouteMatrixCell(
                origin_index=origin_index,
                destination_index=destination_index,
                distance_meters=ValhallaRoutingProvider._distance_meters(
                    distance,
                    units=units,
                    field_name="sources_to_targets.distance",
                    allow_zero=True,
                ),
                duration_seconds=ValhallaRoutingProvider._seconds(
                    duration,
                    field_name="sources_to_targets.time",
                    allow_zero=True,
                ),
            )

        expected_pairs = [
            (origin_index, destination_index)
            for origin_index in range(origin_count)
            for destination_index in range(destination_count)
        ]
        missing = [pair for pair in expected_pairs if pair not in by_pair]
        if missing:
            raise ProviderResponseError(
                f"Valhalla matrix is missing {len(missing)} source/target cells"
            )
        return [by_pair[pair] for pair in expected_pairs]

    @staticmethod
    def _matrix_entries(
        raw_matrix: Any,
        *,
        origin_count: int,
        destination_count: int,
    ) -> list[tuple[int, int, Mapping[str, Any]]]:
        if isinstance(raw_matrix, Mapping):
            return ValhallaRoutingProvider._concise_matrix_entries(
                raw_matrix,
                origin_count=origin_count,
                destination_count=destination_count,
            )
        if not isinstance(raw_matrix, list):
            raise ProviderResponseError(
                "Valhalla matrix has no sources_to_targets result"
            )

        entries: list[tuple[int, int, Mapping[str, Any]]] = []
        is_nested = all(isinstance(row, list) for row in raw_matrix)
        if is_nested:
            if len(raw_matrix) != origin_count:
                raise ProviderResponseError(
                    "Valhalla matrix has an unexpected number of source rows"
                )
            for row_index, raw_row in enumerate(raw_matrix):
                if len(raw_row) != destination_count:
                    raise ProviderResponseError(
                        "Valhalla matrix has an unexpected number of target columns"
                    )
                for column_index, raw_entry in enumerate(raw_row):
                    entry = ValhallaRoutingProvider._matrix_entry(raw_entry)
                    entries.append(
                        ValhallaRoutingProvider._indexed_entry(
                            entry,
                            inferred_origin=row_index,
                            inferred_destination=column_index,
                        )
                    )
            return entries

        expected_count = origin_count * destination_count
        if len(raw_matrix) != expected_count:
            raise ProviderResponseError(
                "Valhalla matrix has an unexpected number of cells"
            )
        for position, raw_entry in enumerate(raw_matrix):
            entry = ValhallaRoutingProvider._matrix_entry(raw_entry)
            entries.append(
                ValhallaRoutingProvider._indexed_entry(
                    entry,
                    inferred_origin=position // destination_count,
                    inferred_destination=position % destination_count,
                )
            )
        return entries

    @staticmethod
    def _concise_matrix_entries(
        raw_matrix: Mapping[str, Any],
        *,
        origin_count: int,
        destination_count: int,
    ) -> list[tuple[int, int, Mapping[str, Any]]]:
        distances = raw_matrix.get("distances")
        durations = raw_matrix.get("durations")
        if not isinstance(distances, list) or not isinstance(durations, list):
            raise ProviderResponseError(
                "Valhalla concise matrix requires distances and durations"
            )
        if len(distances) != origin_count or len(durations) != origin_count:
            raise ProviderResponseError(
                "Valhalla concise matrix has an unexpected number of rows"
            )

        entries: list[tuple[int, int, Mapping[str, Any]]] = []
        for origin_index, (distance_row, duration_row) in enumerate(
            zip(distances, durations, strict=True)
        ):
            if not isinstance(distance_row, list) or not isinstance(
                duration_row,
                list,
            ):
                raise ProviderResponseError(
                    "Valhalla concise matrix rows must be arrays"
                )
            if (
                len(distance_row) != destination_count
                or len(duration_row) != destination_count
            ):
                raise ProviderResponseError(
                    "Valhalla concise matrix has an unexpected number of columns"
                )
            for destination_index, (distance, duration) in enumerate(
                zip(distance_row, duration_row, strict=True)
            ):
                entries.append(
                    (
                        origin_index,
                        destination_index,
                        {"distance": distance, "time": duration},
                    )
                )
        return entries

    @staticmethod
    def _matrix_entry(raw_entry: Any) -> Mapping[str, Any]:
        if not isinstance(raw_entry, Mapping):
            raise ProviderResponseError("Valhalla matrix cell must be a JSON object")
        return raw_entry

    @staticmethod
    def _indexed_entry(
        entry: Mapping[str, Any],
        *,
        inferred_origin: int,
        inferred_destination: int,
    ) -> tuple[int, int, Mapping[str, Any]]:
        origin_index = ValhallaRoutingProvider._matrix_index(
            entry.get("from_index", inferred_origin),
            field_name="from_index",
        )
        destination_index = ValhallaRoutingProvider._matrix_index(
            entry.get("to_index", inferred_destination),
            field_name="to_index",
        )
        return origin_index, destination_index, entry

    @staticmethod
    def _matrix_index(value: Any, *, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProviderResponseError(
                f"Valhalla matrix {field_name} must be an integer"
            )
        return value

    @staticmethod
    def _distance_meters(
        value: Any,
        *,
        units: Any,
        field_name: str,
        allow_zero: bool,
    ) -> int:
        number = ValhallaRoutingProvider._number(
            value,
            field_name=field_name,
            allow_zero=allow_zero,
        )
        normalized_units = units.casefold() if isinstance(units, str) else "kilometers"
        if normalized_units in {"kilometers", "kilometres", "km"}:
            factor = 1_000
        elif normalized_units in {"miles", "mi"}:
            factor = 1_609.344
        else:
            raise ProviderResponseError(
                f"Valhalla response uses unsupported distance units '{units}'"
            )
        converted = round(number * factor)
        return max(1, converted) if not allow_zero else converted

    @staticmethod
    def _seconds(value: Any, *, field_name: str, allow_zero: bool) -> int:
        number = ValhallaRoutingProvider._number(
            value,
            field_name=field_name,
            allow_zero=allow_zero,
        )
        converted = round(number)
        return max(1, converted) if not allow_zero else converted

    @staticmethod
    def _number(value: Any, *, field_name: str, allow_zero: bool) -> float:
        if not ValhallaRoutingProvider._is_number(value):
            raise ProviderResponseError(f"Valhalla response has invalid {field_name}")
        number = float(value)
        if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
            raise ProviderResponseError(f"Valhalla response has invalid {field_name}")
        return number

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
