from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from nextrip_pipeline.schemas.place import GeoPoint
from nextrip_pipeline.schemas.route import (
    ProviderRole,
    RoutingProvider,
    TrafficBasis,
    TransportMode,
)
from nextrip_traffic.providers.base import (
    MatrixLimitExceededError,
    MatrixQuery,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    RouteEndpoint,
    RouteNotFoundError,
    RouteQuery,
    RoutingProviderError,
    UnsupportedTransportModeError,
)
from nextrip_traffic.providers.valhalla import ValhallaRoutingProvider


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def endpoint(identifier: str, latitude: float, longitude: float) -> RouteEndpoint:
    return RouteEndpoint(
        access_point_id=identifier,
        location=GeoPoint(latitude=latitude, longitude=longitude),
    )


def route_query(mode: TransportMode = TransportMode.DRIVE) -> RouteQuery:
    return RouteQuery(
        request_id="route-request-1",
        origin=endpoint("quy-nhon", 13.7820, 109.2190),
        destination=endpoint("da-nang", 16.0544, 108.2022),
        mode=mode,
        departure_time=NOW,
        ttl_seconds=900,
    )


def provider_for(handler: httpx.MockTransport) -> ValhallaRoutingProvider:
    return ValhallaRoutingProvider(
        base_url="http://valhalla.test:8002/",
        client=httpx.Client(transport=handler),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    ("mode", "expected_costing"),
    [
        (TransportMode.DRIVE, "auto"),
        (TransportMode.WALK, "pedestrian"),
        (TransportMode.BICYCLE, "bicycle"),
        (TransportMode.TWO_WHEELER, "motor_scooter"),
    ],
)
def test_route_maps_modes_and_normalizes_response(
    mode: TransportMode,
    expected_costing: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/route"
        payload = json.loads(request.content)
        assert payload["costing"] == expected_costing
        assert payload["units"] == "kilometers"
        assert payload["locations"] == [
            {"lat": 13.782, "lon": 109.219},
            {"lat": 16.0544, "lon": 108.2022},
        ]
        return httpx.Response(
            200,
            headers={"x-request-id": "valhalla-http-1"},
            json={
                "id": payload["id"],
                "trip": {
                    "status": 0,
                    "units": "kilometers",
                    "summary": {"length": 322.456, "time": 18_001.4},
                    "legs": [{"shape": "encoded-polyline6"}],
                },
            },
            request=request,
        )

    result = provider_for(httpx.MockTransport(handler)).route(route_query(mode))

    assert result.provider == RoutingProvider.VALHALLA
    assert result.mode == mode
    assert result.distance_meters == 322_456
    assert result.duration_seconds == 18_001
    assert result.base_duration_seconds == 18_001
    assert result.traffic_delay_seconds == 0
    assert result.traffic_aware is False
    assert result.traffic_basis == TrafficBasis.FREE_FLOW
    assert result.encoded_polyline == "encoded-polyline6"
    assert result.provider_request_id == "valhalla-http-1"
    assert result.expires_at.timestamp() - result.observed_at.timestamp() == 900


def test_route_preserves_fallback_provenance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "trip": {
                    "summary": {"length": 1.0, "time": 60},
                    "legs": [{"shape": "shape"}],
                    "units": "kilometers",
                }
            },
            request=request,
        )

    query = route_query().model_copy(
        update={
            "provider_role": ProviderRole.FALLBACK,
            "fallback_reason": "HERE unavailable",
        }
    )
    result = provider_for(httpx.MockTransport(handler)).route(query)

    assert result.provider_role == ProviderRole.FALLBACK
    assert result.fallback_used is True
    assert result.fallback_reason == "HERE unavailable"


def test_route_rejects_transit_without_sending_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url}")

    provider = provider_for(httpx.MockTransport(handler))

    with pytest.raises(UnsupportedTransportModeError, match="transit"):
        provider.route(route_query(TransportMode.TRANSIT))


@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (401, {"error": "denied"}, ProviderAuthenticationError),
        (429, {"error": "slow down"}, ProviderRateLimitError),
        (503, {"error": "unavailable"}, ProviderUnavailableError),
        (400, {"error": "invalid costing"}, RoutingProviderError),
        (
            400,
            {"error_code": 442, "error": "No path could be found for input"},
            RouteNotFoundError,
        ),
    ],
)
def test_route_maps_http_errors(
    status: int,
    body: dict[str, object],
    error_type: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, request=request)

    with pytest.raises(error_type):
        provider_for(httpx.MockTransport(handler)).route(route_query())


def test_route_maps_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderUnavailableError, match="route request failed"):
        provider_for(httpx.MockTransport(handler)).route(route_query())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"trip": {"legs": [{"shape": "shape"}]}}),
        httpx.Response(
            200,
            json={
                "trip": {
                    "summary": {"length": 1.0, "time": 60},
                    "legs": [{}],
                }
            },
        ),
    ],
)
def test_route_rejects_malformed_responses(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    with pytest.raises(ProviderResponseError):
        provider_for(httpx.MockTransport(handler)).route(route_query())


def test_route_maps_success_payload_without_path_to_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error_code": 442,
                "error": "No path could be found for input",
            },
            request=request,
        )

    with pytest.raises(RouteNotFoundError):
        provider_for(httpx.MockTransport(handler)).route(route_query())


def test_matrix_uses_official_endpoint_and_marks_unreachable_cells() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sources_to_targets"
        payload = json.loads(request.content)
        assert payload["costing"] == "motor_scooter"
        assert payload["verbose"] is True
        assert payload["shape_format"] == "no_shape"
        return httpx.Response(
            200,
            json={
                "id": payload["id"],
                "units": "kilometers",
                "sources_to_targets": [
                    [
                        {
                            "from_index": 0,
                            "to_index": 0,
                            "distance": 0.0,
                            "time": 0,
                        },
                        {
                            "from_index": 0,
                            "to_index": 1,
                            "distance": 1.25,
                            "time": 180.2,
                        },
                    ],
                    [
                        {
                            "from_index": 1,
                            "to_index": 0,
                            "distance": None,
                            "time": None,
                        },
                        {
                            "from_index": 1,
                            "to_index": 1,
                            "distance": 2.5,
                            "time": 300,
                        },
                    ],
                ],
            },
            request=request,
        )

    query = MatrixQuery(
        request_id="matrix-request-1",
        origins=[
            endpoint("origin-0", 16.05, 108.20),
            endpoint("origin-1", 16.06, 108.21),
        ],
        destinations=[
            endpoint("target-0", 16.07, 108.22),
            endpoint("target-1", 16.08, 108.23),
        ],
        mode=TransportMode.TWO_WHEELER,
        departure_time=NOW,
        max_cells=4,
    )

    result = provider_for(httpx.MockTransport(handler)).matrix(query)

    assert result.provider == RoutingProvider.VALHALLA
    assert result.traffic_aware is False
    assert result.traffic_basis == TrafficBasis.FREE_FLOW
    assert [(cell.origin_index, cell.destination_index) for cell in result.cells] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert result.cells[0].distance_meters == 0
    assert result.cells[1].distance_meters == 1_250
    assert result.cells[1].duration_seconds == 180
    assert result.cells[2].reachable is False
    assert result.cells[2].error_code == "route_not_found"


def test_matrix_parses_concise_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "units": "kilometers",
                "sources_to_targets": {
                    "distances": [[0.5, None]],
                    "durations": [[90, None]],
                },
            },
            request=request,
        )

    query = MatrixQuery(
        request_id="concise-matrix",
        origins=[endpoint("origin", 16.05, 108.20)],
        destinations=[
            endpoint("target-0", 16.06, 108.21),
            endpoint("target-1", 16.07, 108.22),
        ],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=2,
    )
    result = provider_for(httpx.MockTransport(handler)).matrix(query)

    assert result.cells[0].distance_meters == 500
    assert result.cells[1].reachable is False


def test_matrix_enforces_provider_hard_limit() -> None:
    provider = ValhallaRoutingProvider(
        base_url="http://valhalla.test:8002",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail(f"unexpected request to {request.url}")
            )
        ),
        max_matrix_cells=1,
        clock=lambda: NOW,
    )
    query = MatrixQuery(
        request_id="large-matrix",
        origins=[endpoint("origin", 16.05, 108.20)],
        destinations=[
            endpoint("target-0", 16.06, 108.21),
            endpoint("target-1", 16.07, 108.22),
        ],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=2,
    )

    with pytest.raises(MatrixLimitExceededError, match="provider limit is 1"):
        provider.matrix(query)


def test_matrix_rejects_missing_cells() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sources_to_targets": []},
            request=request,
        )

    query = MatrixQuery(
        request_id="bad-matrix",
        origins=[endpoint("origin", 16.05, 108.20)],
        destinations=[endpoint("target", 16.06, 108.21)],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
    )

    with pytest.raises(ProviderResponseError):
        provider_for(httpx.MockTransport(handler)).matrix(query)


def test_health_uses_status_and_captures_tileset_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/status"
        return httpx.Response(
            200,
            json={
                "version": "3.5.1",
                "tileset_last_modified": 1_723_456_789,
                "has_tiles": True,
                "has_live_traffic": False,
            },
            request=request,
        )

    result = provider_for(httpx.MockTransport(handler)).health()

    assert result.provider == RoutingProvider.VALHALLA
    assert result.configured is True
    assert result.available is True
    assert "Valhalla 3.5.1" in (result.detail or "")
    assert "tileset-1723456789" in (result.detail or "")
    assert "live_traffic=no" in (result.detail or "")


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, request=request),
        lambda request: httpx.Response(200, text="not-json", request=request),
        lambda request: httpx.Response(200, json={}, request=request),
        lambda request: httpx.Response(
            200,
            json={"version": "3.5.1", "has_tiles": False},
            request=request,
        ),
    ],
)
def test_health_reports_unavailable_for_invalid_status(
    handler: object,
) -> None:
    result = provider_for(httpx.MockTransport(handler)).health()  # type: ignore[arg-type]

    assert result.configured is True
    assert result.available is False


def test_health_reports_transport_failure_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = provider_for(httpx.MockTransport(handler)).health()

    assert result.available is False
    assert "ConnectError" in (result.detail or "")


@pytest.mark.parametrize("base_url", ["", "localhost:8002", "ftp://host/path"])
def test_base_url_must_be_explicit_absolute_http_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP URL"):
        ValhallaRoutingProvider(base_url=base_url)

