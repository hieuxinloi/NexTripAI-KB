from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from nextrip_pipeline.schemas.place import GeoPoint
from nextrip_pipeline.schemas.route import TrafficBasis, TransportMode
from nextrip_traffic.errors import (
    RouteNotFoundError,
    RoutingProviderAuthenticationError,
    RoutingProviderRateLimitError,
    UnsupportedTransportModeError,
)
from nextrip_traffic.providers import (
    HereRoutingProvider,
    MatrixLimitExceededError,
    MatrixQuery,
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderUnavailableError,
    RouteEndpoint,
    RouteQuery,
)


NOW = datetime(2026, 8, 19, 8, 30, tzinfo=UTC)


def endpoint(identifier: str, latitude: float, longitude: float) -> RouteEndpoint:
    return RouteEndpoint(
        access_point_id=identifier,
        location=GeoPoint(latitude=latitude, longitude=longitude),
    )


ORIGIN = endpoint("city:quy-nhon", 13.7820, 109.2190)
DESTINATION = endpoint("city:da-nang", 16.0544, 108.2022)


def query(*, mode: TransportMode = TransportMode.DRIVE) -> RouteQuery:
    return RouteQuery(
        request_id="req-001",
        origin=ORIGIN,
        destination=DESTINATION,
        mode=mode,
        departure_time=NOW,
        ttl_seconds=900,
    )


def route_payload(*, include_polyline: bool = True) -> dict[str, object]:
    first: dict[str, object] = {
        "id": "section-1",
        "summary": {"length": 100_000, "duration": 7_200, "baseDuration": 6_600},
        "departure": {"place": {"location": {"lat": 13.7821, "lng": 109.2190}}},
    }
    if include_polyline:
        first["polyline"] = "BFoz5xJ67i1B1B7PzIhaxL7Y"
    return {
        "routes": [
            {
                "id": "here-route-123",
                "sections": [
                    first,
                    {
                        "id": "section-2",
                        "summary": {
                            "length": 220_000,
                            "duration": 14_400,
                            "baseDuration": 13_800,
                        },
                        **(
                            {"polyline": "BGwyn5xJ47i1B1B7PzIhaxL7Y"}
                            if include_polyline
                            else {}
                        ),
                        "arrival": {
                            "place": {
                                "location": {"lat": 16.0545, "lng": 108.2022}
                            }
                        },
                    },
                ],
            }
        ]
    }


def provider_for(
    handler: httpx.MockTransport,
    *,
    api_key: str | None = "test-key",
    max_matrix_cells: int = 25,
) -> HereRoutingProvider:
    return HereRoutingProvider(
        api_key=api_key,
        client=httpx.Client(transport=handler),
        clock=lambda: NOW,
        max_matrix_cells=max_matrix_cells,
    )


def test_route_calls_here_v8_and_aggregates_all_sections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url.copy_with(query=None)) == (
            "https://router.hereapi.com/v8/routes"
        )
        assert request.url.params["apiKey"] == "test-key"
        assert request.url.params["origin"] == "13.7820000,109.2190000"
        assert request.url.params["destination"] == "16.0544000,108.2022000"
        assert request.url.params["transportMode"] == "car"
        assert request.url.params["routingMode"] == "fast"
        assert request.url.params["return"] == "polyline,summary"
        assert request.url.params["departureTime"] == "2026-08-19T08:30:00Z"
        return httpx.Response(
            200,
            json=route_payload(),
            headers={"x-correlation-id": "here-request-456"},
        )

    observation = provider_for(httpx.MockTransport(handler)).route(query())

    assert observation.provider == "here"
    assert observation.request_id == "req-001"
    assert observation.distance_meters == 320_000
    assert observation.duration_seconds == 21_600
    assert observation.base_duration_seconds == 20_400
    assert observation.traffic_delay_seconds == 1_200
    assert observation.encoded_polyline == "BFoz5xJ67i1B1B7PzIhaxL7Y"
    assert observation.provider_request_id == "here-request-456"
    assert observation.origin_snap_distance_meters == 11
    assert observation.destination_snap_distance_meters == 11
    assert observation.traffic_aware is True
    assert observation.traffic_basis == TrafficBasis.CURRENT
    assert (observation.expires_at - observation.observed_at).total_seconds() == 900


@pytest.mark.parametrize(
    ("mode", "here_mode", "traffic_aware", "traffic_basis"),
    [
        (TransportMode.WALK, "pedestrian", False, TrafficBasis.FREE_FLOW),
        (TransportMode.BICYCLE, "bicycle", False, TrafficBasis.FREE_FLOW),
        (TransportMode.TWO_WHEELER, "scooter", True, TrafficBasis.CURRENT),
        (TransportMode.DRIVE, "car", True, TrafficBasis.CURRENT),
    ],
)
def test_transport_mode_mapping_and_traffic_provenance(
    mode: TransportMode,
    here_mode: str,
    traffic_aware: bool,
    traffic_basis: TrafficBasis,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["transportMode"] == here_mode
        return httpx.Response(200, json=route_payload())

    observation = provider_for(httpx.MockTransport(handler)).route(query(mode=mode))

    assert observation.traffic_aware is traffic_aware
    assert observation.traffic_basis == traffic_basis


def test_transit_is_explicitly_unsupported_without_calling_here() -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("HERE must not be called for unsupported transit mode")

    provider = provider_for(httpx.MockTransport(unexpected))

    with pytest.raises(UnsupportedTransportModeError, match="transit"):
        provider.route(query(mode=TransportMode.TRANSIT))


def test_missing_key_is_reported_only_when_provider_is_used() -> None:
    provider = provider_for(
        httpx.MockTransport(lambda _request: httpx.Response(200)),
        api_key=None,
    )

    assert provider.health().configured is False
    with pytest.raises(ProviderConfigurationError, match="HERE_API_KEY"):
        provider.route(query())


def test_configured_health_does_not_spend_route_quota() -> None:
    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("HERE health must not make a billable route request")

    status = provider_for(httpx.MockTransport(unexpected)).health()

    assert status.configured is True
    assert status.available is None
    assert status.detail == "HERE API key configured; reachability not probed"


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, RoutingProviderAuthenticationError),
        (403, RoutingProviderAuthenticationError),
        (429, RoutingProviderRateLimitError),
        (500, ProviderUnavailableError),
        (503, ProviderUnavailableError),
    ],
)
def test_http_failures_have_typed_sanitized_errors(
    status_code: int,
    error_type: type[Exception],
) -> None:
    provider = provider_for(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                status_code,
                json={"title": "secret provider error"},
            )
        )
    )

    with pytest.raises(error_type) as caught:
        provider.route(query())

    assert "test-key" not in str(caught.value)
    assert "secret provider error" not in str(caught.value)


def test_network_failure_is_provider_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    provider = provider_for(httpx.MockTransport(handler))

    with pytest.raises(ProviderUnavailableError, match="request failed"):
        provider.route(query())


@pytest.mark.parametrize(
    ("response", "error_type", "message"),
    [
        (httpx.Response(200, text="not-json"), ProviderResponseError, "malformed JSON"),
        (httpx.Response(200, json={"routes": []}), RouteNotFoundError, "no route"),
        (
            httpx.Response(200, json=route_payload(include_polyline=False)),
            ProviderResponseError,
            "no polyline",
        ),
        (
            httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "sections": [
                                {
                                    "summary": {"length": "100", "duration": 60},
                                    "polyline": "valid",
                                }
                            ]
                        }
                    ]
                },
            ),
            ProviderResponseError,
            "summary.length",
        ),
    ],
)
def test_malformed_and_missing_route_responses_are_rejected(
    response: httpx.Response,
    error_type: type[Exception],
    message: str,
) -> None:
    provider = provider_for(
        httpx.MockTransport(lambda _request: response)
    )

    with pytest.raises(error_type, match=message):
        provider.route(query())


def test_matrix_fanout_is_bounded_by_provider_limit() -> None:
    locations = [
        endpoint("one", 13.7, 109.2),
        endpoint("two", 14.0, 109.1),
    ]
    matrix_query = MatrixQuery(
        request_id="matrix-too-large",
        origins=locations,
        destinations=locations,
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=4,
    )
    provider = provider_for(
        httpx.MockTransport(lambda _request: httpx.Response(200)),
        max_matrix_cells=3,
    )

    with pytest.raises(MatrixLimitExceededError, match="4 cells"):
        provider.matrix(matrix_query)


def test_matrix_query_rejects_unbounded_product_before_provider_call() -> None:
    locations = [endpoint(f"point-{index}", 13.7 + index / 100, 109.2) for index in range(6)]

    with pytest.raises(ValidationError, match="exceeding max_cells"):
        MatrixQuery(
            request_id="matrix-contract-limit",
            origins=locations,
            destinations=locations,
            mode=TransportMode.DRIVE,
            departure_time=NOW,
            max_cells=25,
        )


def test_matrix_marks_no_route_cell_unreachable_and_keeps_identity_cell() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"routes": []})

    matrix_query = MatrixQuery(
        request_id="matrix-001",
        origins=[ORIGIN],
        destinations=[ORIGIN, DESTINATION],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=2,
    )
    result = provider_for(httpx.MockTransport(handler)).matrix(matrix_query)

    assert calls == 1
    assert result.origin_access_point_ids == ["city:quy-nhon"]
    assert result.destination_access_point_ids == [
        "city:quy-nhon",
        "city:da-nang",
    ]
    assert result.cells[0].reachable is True
    assert result.cells[0].distance_meters == 0
    assert result.cells[0].duration_seconds == 0
    assert result.cells[1].reachable is False
    assert result.cells[1].error_code == "route_not_found"


def test_matrix_records_a_pair_level_malformed_response() -> None:
    matrix_query = MatrixQuery(
        request_id="matrix-malformed",
        origins=[ORIGIN],
        destinations=[DESTINATION],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=1,
    )
    provider = provider_for(
        httpx.MockTransport(lambda _request: httpx.Response(200, text="not-json"))
    )

    result = provider.matrix(matrix_query)

    assert result.cells[0].reachable is False
    assert result.cells[0].error_code == "invalid_provider_response"


def test_matrix_aggregates_successful_route_cells() -> None:
    matrix_query = MatrixQuery(
        request_id="matrix-002",
        origins=[ORIGIN],
        destinations=[DESTINATION],
        mode=TransportMode.DRIVE,
        departure_time=NOW,
        max_cells=1,
        ttl_seconds=1_200,
    )
    provider = provider_for(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=route_payload()))
    )

    result = provider.matrix(matrix_query)

    assert len(result.cells) == 1
    assert result.cells[0].reachable is True
    assert result.cells[0].distance_meters == 320_000
    assert result.cells[0].duration_seconds == 21_600
    assert result.traffic_aware is True
    assert result.traffic_basis == TrafficBasis.CURRENT
    assert (result.expires_at - result.observed_at).total_seconds() == 1_200
