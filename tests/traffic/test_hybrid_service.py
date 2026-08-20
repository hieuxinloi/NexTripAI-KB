from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    AccessPointType,
    GeoPoint,
    ProviderRole,
    RouteMatrixCell,
    RouteMatrixResult,
    RouteObservation,
    RoutingProvider,
    TrafficBasis,
    TransportMode,
    VerificationStatus,
)
from nextrip_traffic.cache import SQLiteTrafficCache
from nextrip_traffic.config import TrafficSettings
from nextrip_traffic.models import (
    TrafficMatrixRequest,
    TrafficPreference,
    TrafficRouteRequest,
)
from nextrip_traffic.providers import ProviderHealth, ProviderUnavailableError
from nextrip_traffic.service import HybridTrafficService


NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


def point(identifier: str, latitude: float, longitude: float) -> AccessPointRecord:
    return AccessPointRecord(
        access_point_id=f"place:{identifier}:main",
        owner_entity_id=identifier,
        access_type=AccessPointType.MAIN_ENTRANCE,
        location=GeoPoint(latitude=latitude, longitude=longitude),
        supported_modes=list(TransportMode),
        source_record_ids=[identifier],
        verification_status=VerificationStatus.HUMAN_VERIFIED,
        updated_at=NOW,
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.values = {
            "origin": point("origin", 16.05, 108.20),
            "destination": point("destination", 16.08, 108.24),
        }

    def resolve(self, identifier: str) -> AccessPointRecord:
        return self.values[identifier]


class FakeProvider:
    def __init__(self, provider: RoutingProvider, *, fails: bool = False) -> None:
        self.provider = provider
        self.fails = fails
        self.route_calls = 0
        self.matrix_calls = 0

    def route(self, query) -> RouteObservation:
        self.route_calls += 1
        if self.fails:
            raise ProviderUnavailableError(f"{self.provider.value} unavailable")
        is_here = self.provider == RoutingProvider.HERE
        return RouteObservation(
            observation_id=f"{self.provider.value}-route-{self.route_calls}",
            request_id=query.request_id,
            origin_access_point_id=query.origin.access_point_id,
            destination_access_point_id=query.destination.access_point_id,
            mode=query.mode,
            provider=self.provider,
            provider_role=ProviderRole.PRIMARY,
            traffic_aware=is_here,
            traffic_basis=TrafficBasis.CURRENT if is_here else TrafficBasis.FREE_FLOW,
            distance_meters=6000 if is_here else 6200,
            duration_seconds=900 if is_here else 720,
            base_duration_seconds=720 if is_here else None,
            traffic_delay_seconds=180 if is_here else None,
            encoded_polyline="encoded-shape",
            departure_time=query.departure_time,
            observed_at=NOW,
            expires_at=NOW + timedelta(seconds=query.ttl_seconds or 600),
        )

    def matrix(self, query) -> RouteMatrixResult:
        self.matrix_calls += 1
        if self.fails:
            raise ProviderUnavailableError(f"{self.provider.value} unavailable")
        is_here = self.provider == RoutingProvider.HERE
        return RouteMatrixResult(
            matrix_id=f"{self.provider.value}-matrix-{self.matrix_calls}",
            request_id=query.request_id,
            provider=self.provider,
            provider_role=ProviderRole.PRIMARY,
            mode=query.mode,
            origin_access_point_ids=[item.access_point_id for item in query.origins],
            destination_access_point_ids=[
                item.access_point_id for item in query.destinations
            ],
            cells=[
                RouteMatrixCell(
                    origin_index=i,
                    destination_index=j,
                    distance_meters=6000,
                    duration_seconds=900 if is_here else 720,
                )
                for i, _ in enumerate(query.origins)
                for j, _ in enumerate(query.destinations)
            ],
            traffic_aware=is_here,
            traffic_basis=TrafficBasis.CURRENT if is_here else TrafficBasis.FREE_FLOW,
            departure_time=query.departure_time,
            observed_at=NOW,
            expires_at=NOW + timedelta(seconds=query.ttl_seconds or 600),
        )

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.provider,
            configured=True,
            available=not self.fails,
            checked_at=NOW,
        )

    def close(self) -> None:
        return None


def service(tmp_path, *, here_fails: bool = False):
    valhalla = FakeProvider(RoutingProvider.VALHALLA)
    here = FakeProvider(RoutingProvider.HERE, fails=here_fails)
    settings = TrafficSettings(
        kb_root=tmp_path,
        route_ttl_seconds=600,
        matrix_ttl_seconds=600,
        free_flow_ttl_seconds=86400,
    )
    instance = HybridTrafficService(
        registry=FakeRegistry(),
        cache=SQLiteTrafficCache(":memory:"),
        providers={
            RoutingProvider.VALHALLA: valhalla,
            RoutingProvider.HERE: here,
        },
        settings=settings,
        clock=lambda: NOW,
    )
    return instance, valhalla, here


def route_request(**updates) -> TrafficRouteRequest:
    payload = {
        "request_id": "request-1",
        "origin_id": "origin",
        "destination_id": "destination",
        "mode": TransportMode.DRIVE,
        "departure_time": NOW,
    }
    payload.update(updates)
    return TrafficRouteRequest.model_validate(payload)


def test_hybrid_selects_here_and_keeps_valhalla_baseline(tmp_path) -> None:
    instance, valhalla, here = service(tmp_path)

    response = instance.route(route_request())

    assert response.route.provider == RoutingProvider.HERE
    assert response.route.traffic_aware is True
    assert response.baseline_route is not None
    assert response.baseline_route.provider == RoutingProvider.VALHALLA
    assert response.selection_reason == "here_traffic_aware_selected"
    assert [item.provider for item in response.provider_attempts] == [
        RoutingProvider.VALHALLA,
        RoutingProvider.HERE,
    ]
    assert valhalla.route_calls == here.route_calls == 1


def test_route_cache_does_not_call_providers_twice(tmp_path) -> None:
    instance, valhalla, here = service(tmp_path)
    instance.route(route_request())

    cached = instance.route(route_request(request_id="request-2"))

    assert cached.cache_hit is True
    assert cached.request_id == "request-2"
    assert cached.route.request_id == "request-2"
    assert valhalla.route_calls == here.route_calls == 1


def test_route_cache_separates_baseline_shape(tmp_path) -> None:
    instance, valhalla, here = service(tmp_path)

    without_baseline = instance.route(
        route_request(include_baseline=False, request_id="without-baseline")
    )
    with_baseline = instance.route(
        route_request(include_baseline=True, request_id="with-baseline")
    )

    assert without_baseline.baseline_route is None
    assert with_baseline.baseline_route is not None
    assert valhalla.route_calls == here.route_calls == 2


def test_here_hint_cannot_claim_free_flow_only() -> None:
    with pytest.raises(ValueError, match="cannot guarantee"):
        route_request(
            traffic_preference=TrafficPreference.FREE_FLOW,
            provider_hint=RoutingProvider.HERE,
        )


def test_here_failure_returns_explicit_valhalla_free_flow_fallback(tmp_path) -> None:
    instance, _, _ = service(tmp_path, here_fails=True)

    response = instance.route(route_request())

    assert response.degraded is True
    assert response.route.provider == RoutingProvider.VALHALLA
    assert response.route.provider_role == ProviderRole.FALLBACK
    assert response.route.fallback_used is True
    assert response.route.traffic_basis == TrafficBasis.FREE_FLOW
    assert response.route.expires_at == NOW + timedelta(seconds=600)
    assert response.selection_reason.startswith("valhalla_free_flow_fallback:")


def test_required_traffic_does_not_silently_fallback(tmp_path) -> None:
    instance, _, _ = service(tmp_path, here_fails=True)

    with pytest.raises(ProviderUnavailableError):
        instance.route(
            route_request(
                traffic_preference=TrafficPreference.TRAFFIC_AWARE_REQUIRED
            )
        )


def test_free_flow_uses_only_valhalla(tmp_path) -> None:
    instance, valhalla, here = service(tmp_path)

    response = instance.route(
        route_request(traffic_preference=TrafficPreference.FREE_FLOW)
    )

    assert response.route.provider == RoutingProvider.VALHALLA
    assert response.route.traffic_basis == TrafficBasis.FREE_FLOW
    assert valhalla.route_calls == 1
    assert here.route_calls == 0


def test_hybrid_matrix_selects_here_with_valhalla_baseline(tmp_path) -> None:
    instance, valhalla, here = service(tmp_path)

    response = instance.matrix(
        TrafficMatrixRequest(
            request_id="matrix-request",
            origin_ids=["origin"],
            destination_ids=["destination"],
            departure_time=NOW,
        )
    )

    assert response.matrix.provider == RoutingProvider.HERE
    assert response.baseline_matrix is not None
    assert response.baseline_matrix.provider == RoutingProvider.VALHALLA
    assert valhalla.matrix_calls == here.matrix_calls == 1


def test_hybrid_matrix_fallback_keeps_reason_and_short_ttl(tmp_path) -> None:
    instance, _, _ = service(tmp_path, here_fails=True)

    response = instance.matrix(
        TrafficMatrixRequest(
            request_id="matrix-fallback",
            origin_ids=["origin"],
            destination_ids=["destination"],
            departure_time=NOW,
        )
    )

    assert response.degraded is True
    assert response.matrix.provider_role == ProviderRole.FALLBACK
    assert response.matrix.fallback_used is True
    assert response.matrix.fallback_reason == "ProviderUnavailableError"
    assert response.matrix.expires_at == NOW + timedelta(seconds=600)
