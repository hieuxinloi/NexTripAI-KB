from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

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
from nextrip_traffic.access_points import AccessPointNotFoundError
from nextrip_traffic.api import create_app
from nextrip_traffic.errors import UnsupportedTransportModeError
from nextrip_traffic.models import TrafficMatrixResponse, TrafficRouteResponse
from nextrip_traffic.providers import ProviderHealth
from nextrip_traffic.runtime import TrafficRuntime, TrafficRuntimeFactory


NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


def _point(identifier: str, latitude: float, longitude: float) -> AccessPointRecord:
    return AccessPointRecord(
        access_point_id=f"place:{identifier}:main",
        owner_entity_id=identifier,
        access_type=AccessPointType.MAIN_ENTRANCE,
        name=identifier.title(),
        location=GeoPoint(latitude=latitude, longitude=longitude),
        supported_modes=list(TransportMode),
        source_record_ids=[identifier],
        verification_status=VerificationStatus.HUMAN_VERIFIED,
        updated_at=NOW,
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.values = {
            "origin": _point("origin", 16.05, 108.20),
            "destination": _point("destination", 16.08, 108.24),
        }

    def resolve(self, identifier: str) -> AccessPointRecord:
        try:
            return self.values[identifier]
        except KeyError as error:
            raise AccessPointNotFoundError(identifier) from error

    def list(
        self,
        *,
        access_type: AccessPointType | None = None,
        owner_entity_id: str | None = None,
    ) -> list[AccessPointRecord]:
        return [
            value
            for value in self.values.values()
            if (access_type is None or value.access_type == access_type)
            and (
                owner_entity_id is None
                or value.owner_entity_id == owner_entity_id
            )
        ]

    def stats(self) -> dict[str, object]:
        return {
            "total_access_points": len(self.values),
            "aliases": len(self.values),
            "by_origin": {"place": len(self.values)},
        }

    def __len__(self) -> int:
        return len(self.values)


class FakeService:
    def __init__(
        self,
        *,
        valhalla_available: bool = True,
        route_error: Exception | None = None,
    ) -> None:
        self.registry = FakeRegistry()
        self.valhalla_available = valhalla_available
        self.route_error = route_error
        self.closed = False
        self._route: TrafficRouteResponse | None = None
        self._matrix: TrafficMatrixResponse | None = None

    def route(self, request) -> TrafficRouteResponse:
        if self.route_error is not None:
            raise self.route_error
        origin = self.registry.resolve(request.origin_id)
        destination = self.registry.resolve(request.destination_id)
        route = RouteObservation(
            observation_id="here-route-1",
            request_id=request.request_id,
            origin_access_point_id=origin.access_point_id,
            destination_access_point_id=destination.access_point_id,
            mode=request.mode,
            provider=RoutingProvider.HERE,
            provider_role=ProviderRole.PRIMARY,
            traffic_aware=True,
            traffic_basis=TrafficBasis.CURRENT,
            distance_meters=6000,
            duration_seconds=900,
            base_duration_seconds=720,
            traffic_delay_seconds=180,
            encoded_polyline="encoded-shape",
            departure_time=request.departure_time,
            observed_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        self._route = TrafficRouteResponse(
            request_id=request.request_id,
            selection_reason="here_traffic_aware_selected",
            origin=origin,
            destination=destination,
            route=route,
        )
        return self._route

    def matrix(self, request) -> TrafficMatrixResponse:
        origins = [self.registry.resolve(value) for value in request.origin_ids]
        destinations = [
            self.registry.resolve(value) for value in request.destination_ids
        ]
        matrix = RouteMatrixResult(
            matrix_id="here-matrix-1",
            request_id=request.request_id,
            provider=RoutingProvider.HERE,
            provider_role=ProviderRole.PRIMARY,
            mode=request.mode,
            origin_access_point_ids=[item.access_point_id for item in origins],
            destination_access_point_ids=[
                item.access_point_id for item in destinations
            ],
            cells=[
                RouteMatrixCell(
                    origin_index=origin_index,
                    destination_index=destination_index,
                    distance_meters=6000,
                    duration_seconds=900,
                )
                for origin_index, _origin in enumerate(origins)
                for destination_index, _destination in enumerate(destinations)
            ],
            traffic_aware=True,
            traffic_basis=TrafficBasis.CURRENT,
            departure_time=request.departure_time,
            observed_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        self._matrix = TrafficMatrixResponse(
            request_id=request.request_id,
            selection_reason="here_traffic_aware_selected",
            origins=origins,
            destinations=destinations,
            matrix=matrix,
        )
        return self._matrix

    def get_route(
        self,
        observation_id: str,
        *,
        allow_stale: bool = False,
    ) -> TrafficRouteResponse | None:
        del allow_stale
        if self._route and self._route.route.observation_id == observation_id:
            return self._route
        return None

    def get_matrix(
        self,
        matrix_id: str,
        *,
        allow_stale: bool = False,
    ) -> TrafficMatrixResponse | None:
        del allow_stale
        if self._matrix and self._matrix.matrix.matrix_id == matrix_id:
            return self._matrix
        return None

    def provider_health(self) -> list[ProviderHealth]:
        return [
            ProviderHealth(
                provider=RoutingProvider.VALHALLA,
                configured=True,
                available=self.valhalla_available,
                checked_at=NOW,
            ),
            ProviderHealth(
                provider=RoutingProvider.HERE,
                configured=True,
                available=None,
                checked_at=NOW,
            ),
        ]

    def close(self) -> None:
        self.closed = True


class CountingBuilder:
    def __init__(self, service: FakeService) -> None:
        self.service = service
        self.calls = 0

    def __call__(self) -> TrafficRuntime:
        self.calls += 1
        return TrafficRuntime(service=self.service)  # type: ignore[arg-type]


def _app_for(service: FakeService, *, api_key: str | None = None):
    builder = CountingBuilder(service)
    factory = TrafficRuntimeFactory(builder)
    application = create_app(runtime_factory=factory, internal_api_key=api_key)
    return application, builder, factory


def _route_payload() -> dict[str, object]:
    return {
        "request_id": "request-1",
        "origin_id": "origin",
        "destination_id": "destination",
        "mode": "drive",
        "departure_time": NOW.isoformat(),
    }


def test_health_is_exact_and_does_not_initialize_runtime(monkeypatch) -> None:
    monkeypatch.delenv("TRAFFIC_API_KEY", raising=False)
    application, builder, _factory = _app_for(FakeService())

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "nextrip-traffic",
    }
    assert builder.calls == 0


def test_readiness_requires_registry_and_healthy_valhalla() -> None:
    application, builder, factory = _app_for(FakeService())

    with TestClient(application) as client:
        response = client.get("/ready")
        assert factory.peek() is not None

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["access_points"] == 2
    assert builder.calls == 1
    assert factory.peek() is None


def test_readiness_returns_503_when_valhalla_is_unavailable() -> None:
    application, _builder, _factory = _app_for(
        FakeService(valhalla_available=False)
    )

    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_optional_internal_api_key_uses_constant_contract() -> None:
    application, builder, _factory = _app_for(
        FakeService(),
        api_key="secret-traffic-key",
    )

    with TestClient(application) as client:
        missing = client.get("/access-points")
        wrong = client.get(
            "/access-points",
            headers={"X-NexTrip-Traffic-Key": "wrong"},
        )
        accepted = client.get(
            "/access-points",
            headers={"X-NexTrip-Traffic-Key": "secret-traffic-key"},
        )
        public_health = client.get("/health")
        public_ready = client.get("/ready")

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["error"]["code"] == "traffic_api_key_invalid"
    assert missing.headers["www-authenticate"] == "ApiKey"
    assert accepted.status_code == 200
    assert public_health.status_code == public_ready.status_code == 200
    assert builder.calls == 1


def test_internal_api_key_is_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRAFFIC_API_KEY", "environment-secret")
    application, builder, _factory = _app_for(FakeService())

    with TestClient(application) as client:
        denied = client.get("/providers/health")
        accepted = client.get(
            "/providers/health",
            headers={"X-NexTrip-Traffic-Key": "environment-secret"},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert builder.calls == 1


def test_route_and_matrix_create_and_cache_lookup_endpoints() -> None:
    application, _builder, _factory = _app_for(FakeService())

    with TestClient(application) as client:
        route = client.post("/routes", json=_route_payload())
        cached_route = client.get("/routes/here-route-1")
        missing_route = client.get("/routes/unknown")
        matrix = client.post(
            "/matrix",
            json={
                "request_id": "matrix-request",
                "origin_ids": ["origin"],
                "destination_ids": ["destination"],
                "departure_time": NOW.isoformat(),
            },
        )
        cached_matrix = client.get("/matrix/here-matrix-1")

    assert route.status_code == cached_route.status_code == 200
    assert route.json()["route"]["provider"] == "here"
    assert missing_route.status_code == 404
    assert missing_route.json()["error"]["code"] == "route_not_found"
    assert matrix.status_code == cached_matrix.status_code == 200
    assert matrix.json()["matrix"]["matrix_id"] == "here-matrix-1"


def test_access_point_and_provider_health_endpoints() -> None:
    application, _builder, _factory = _app_for(FakeService())

    with TestClient(application) as client:
        filtered = client.get(
            "/access-points",
            params={"owner_entity_id": "origin"},
        )
        stats = client.get("/access-points/stats")
        detail = client.get("/access-points/origin")
        missing = client.get("/access-points/missing")
        providers = client.get("/providers/health")
        provider_alias = client.get("/health/providers")

    assert filtered.status_code == 200
    assert [item["owner_entity_id"] for item in filtered.json()] == ["origin"]
    assert stats.json()["total_access_points"] == 2
    assert detail.json()["access_point_id"] == "place:origin:main"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "access_point_not_found"
    assert providers.status_code == provider_alias.status_code == 200
    assert [item["provider"] for item in providers.json()] == ["valhalla", "here"]


def test_domain_error_is_translated_to_typed_http_error() -> None:
    application, _builder, _factory = _app_for(
        FakeService(
            route_error=UnsupportedTransportModeError("transit unavailable")
        )
    )

    with TestClient(application) as client:
        response = client.post("/routes", json=_route_payload())

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "unsupported_transport_mode",
            "message": "transit unavailable",
        }
    }
