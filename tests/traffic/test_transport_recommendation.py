from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    AccessPointType,
    GeoPoint,
    ProviderRole,
    RouteObservation,
    RoutingProvider,
    TrafficBasis,
    TransportMode,
    VerificationStatus,
)
from nextrip_traffic.access_points import AccessPointNotFoundError
from nextrip_traffic.api import create_app
from nextrip_traffic.cache import SQLiteTrafficCache
from nextrip_traffic import cli
from nextrip_traffic.config import TrafficSettings
from nextrip_traffic.models import (
    RecommendationObjective,
    RecommendationStatus,
    TrafficPreference,
    TransportOptionStatus,
    TransportRecommendationRequest,
)
from nextrip_traffic.providers import ProviderHealth, ProviderUnavailableError
from nextrip_traffic.runtime import TrafficRuntime, TrafficRuntimeFactory
from nextrip_traffic.service import HybridTrafficService


NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
ROAD_MODES = (
    TransportMode.WALK,
    TransportMode.BICYCLE,
    TransportMode.TWO_WHEELER,
    TransportMode.DRIVE,
)
MOTORIZED_MODES = {TransportMode.TWO_WHEELER, TransportMode.DRIVE}

DEFAULT_VALHALLA_METRICS = {
    TransportMode.WALK: (6_000, 1_000),
    TransportMode.BICYCLE: (6_100, 800),
    TransportMode.TWO_WHEELER: (6_200, 540),
    TransportMode.DRIVE: (6_300, 450),
}
DEFAULT_HERE_METRICS = {
    TransportMode.WALK: (6_000, 1_000),
    TransportMode.BICYCLE: (6_100, 800),
    TransportMode.TWO_WHEELER: (6_200, 600),
    TransportMode.DRIVE: (6_300, 500),
}


def point(
    identifier: str,
    latitude: float,
    longitude: float,
    *,
    supported_modes: tuple[TransportMode, ...] = ROAD_MODES,
) -> AccessPointRecord:
    return AccessPointRecord(
        access_point_id=f"place:{identifier}:main",
        owner_entity_id=identifier,
        access_type=AccessPointType.MAIN_ENTRANCE,
        name=identifier.title(),
        location=GeoPoint(latitude=latitude, longitude=longitude),
        supported_modes=list(supported_modes),
        source_record_ids=[identifier],
        verification_status=VerificationStatus.HUMAN_VERIFIED,
        updated_at=NOW,
    )


class FakeRegistry:
    def __init__(
        self,
        *,
        origin_modes: tuple[TransportMode, ...] = ROAD_MODES,
        destination_modes: tuple[TransportMode, ...] = ROAD_MODES,
    ) -> None:
        self.values = {
            "origin": point(
                "origin",
                16.05,
                108.20,
                supported_modes=origin_modes,
            ),
            "destination": point(
                "destination",
                16.08,
                108.24,
                supported_modes=destination_modes,
            ),
        }

    def resolve(self, identifier: str) -> AccessPointRecord:
        try:
            return self.values[identifier]
        except KeyError as error:
            raise AccessPointNotFoundError(identifier) from error

    def __len__(self) -> int:
        return len(self.values)


class ModeProvider:
    def __init__(
        self,
        provider: RoutingProvider,
        metrics: dict[TransportMode, tuple[int, int]],
        *,
        fail_modes: set[TransportMode] | None = None,
    ) -> None:
        self.provider = provider
        self.metrics = dict(metrics)
        self.fail_modes = set(fail_modes or ())
        self.route_queries = []
        self.closed = False
        self._lock = Lock()

    def route(self, query) -> RouteObservation:
        with self._lock:
            self.route_queries.append(query)
            call_number = len(self.route_queries)
        if query.mode in self.fail_modes:
            raise ProviderUnavailableError(
                f"{self.provider.value} unavailable for {query.mode.value}"
            )

        distance, duration = self.metrics[query.mode]
        traffic_aware = (
            self.provider == RoutingProvider.HERE and query.mode in MOTORIZED_MODES
        )
        base_duration = max(1, duration - 60) if traffic_aware else duration
        return RouteObservation(
            observation_id=(f"{self.provider.value}-{query.mode.value}-{call_number}"),
            request_id=query.request_id,
            origin_access_point_id=query.origin.access_point_id,
            destination_access_point_id=query.destination.access_point_id,
            mode=query.mode,
            provider=self.provider,
            provider_role=ProviderRole.PRIMARY,
            traffic_aware=traffic_aware,
            traffic_basis=(
                TrafficBasis.CURRENT if traffic_aware else TrafficBasis.FREE_FLOW
            ),
            distance_meters=distance,
            duration_seconds=duration,
            base_duration_seconds=base_duration,
            traffic_delay_seconds=(duration - base_duration),
            encoded_polyline="encoded-shape",
            departure_time=query.departure_time,
            observed_at=NOW,
            expires_at=NOW + timedelta(seconds=query.ttl_seconds or 600),
        )

    def matrix(self, _query):
        raise AssertionError("recommendations must use route, not matrix")

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.provider,
            configured=True,
            available=True,
            checked_at=NOW,
        )

    def close(self) -> None:
        self.closed = True


@dataclass
class ServiceBundle:
    service: HybridTrafficService
    registry: FakeRegistry
    valhalla: ModeProvider
    here: ModeProvider


@contextmanager
def recommendation_service(
    tmp_path,
    *,
    origin_modes: tuple[TransportMode, ...] = ROAD_MODES,
    destination_modes: tuple[TransportMode, ...] = ROAD_MODES,
    valhalla_metrics: dict[TransportMode, tuple[int, int]] | None = None,
    here_metrics: dict[TransportMode, tuple[int, int]] | None = None,
    valhalla_fail_modes: set[TransportMode] | None = None,
    here_fail_modes: set[TransportMode] | None = None,
) -> Iterator[ServiceBundle]:
    registry = FakeRegistry(
        origin_modes=origin_modes,
        destination_modes=destination_modes,
    )
    valhalla = ModeProvider(
        RoutingProvider.VALHALLA,
        valhalla_metrics or DEFAULT_VALHALLA_METRICS,
        fail_modes=valhalla_fail_modes,
    )
    here = ModeProvider(
        RoutingProvider.HERE,
        here_metrics or DEFAULT_HERE_METRICS,
        fail_modes=here_fail_modes,
    )
    service = HybridTrafficService(
        registry=registry,  # type: ignore[arg-type]
        cache=SQLiteTrafficCache(":memory:"),
        providers={
            RoutingProvider.VALHALLA: valhalla,
            RoutingProvider.HERE: here,
        },
        settings=TrafficSettings(kb_root=tmp_path),
        clock=lambda: NOW,
    )
    bundle = ServiceBundle(
        service=service,
        registry=registry,
        valhalla=valhalla,
        here=here,
    )
    try:
        yield bundle
    finally:
        service.close()


def request(**updates) -> TransportRecommendationRequest:
    payload = {
        "request_id": "recommendation-1",
        "origin_id": "origin",
        "destination_id": "destination",
        "departure_time": NOW,
        # Most policy tests need every successfully routed mode to remain viable.
        "max_walk_duration_seconds": None,
        "max_bicycle_duration_seconds": None,
        "max_two_wheeler_distance_meters": None,
    }
    payload.update(updates)
    return TransportRecommendationRequest.model_validate(payload)


def options_by_mode(response):
    return {option.mode: option for option in response.options}


def query_counts(provider: ModeProvider) -> Counter[TransportMode]:
    return Counter(query.mode for query in provider.route_queries)


def test_request_defaults_and_validation() -> None:
    value = TransportRecommendationRequest(
        origin_id="origin",
        destination_id="destination",
        departure_time=NOW,
    )

    assert value.candidate_modes == list(ROAD_MODES)
    assert value.objective == RecommendationObjective.BALANCED
    assert (
        value.motorized_traffic_preference == TrafficPreference.TRAFFIC_AWARE_PREFERRED
    )
    assert value.max_walk_duration_seconds == 1_800
    assert value.max_bicycle_duration_seconds == 3_600
    assert value.max_two_wheeler_distance_meters == 80_000
    assert value.include_baseline is False
    assert value.force_refresh is False
    assert value.allow_stale_on_error is True

    with pytest.raises(ValidationError, match="must be different"):
        TransportRecommendationRequest(
            origin_id="same",
            destination_id="same",
            departure_time=NOW,
        )
    with pytest.raises(ValidationError, match="cannot contain duplicates"):
        TransportRecommendationRequest(
            origin_id="origin",
            destination_id="destination",
            candidate_modes=[TransportMode.WALK, TransportMode.WALK],
            departure_time=NOW,
        )
    with pytest.raises(ValidationError, match="greater than 0"):
        TransportRecommendationRequest(
            origin_id="origin",
            destination_id="destination",
            max_walk_duration_seconds=0,
            departure_time=NOW,
        )


def test_balanced_and_fastest_rank_routes_and_reuse_route_cache(tmp_path) -> None:
    with recommendation_service(tmp_path) as bundle:
        balanced = bundle.service.recommend_transport(request())

        assert balanced.status == RecommendationStatus.RECOMMENDED
        assert balanced.recommended_mode == TransportMode.TWO_WHEELER
        assert balanced.selection_reason == "balanced_generalized_duration"
        assert [option.mode for option in balanced.options] == list(ROAD_MODES)
        balanced_options = options_by_mode(balanced)
        assert {
            mode: option.generalized_duration_seconds
            for mode, option in balanced_options.items()
        } == {
            TransportMode.WALK: 1_000,
            TransportMode.BICYCLE: 1_040,
            TransportMode.TWO_WHEELER: 960,
            TransportMode.DRIVE: 1_100,
        }
        assert {mode: option.rank for mode, option in balanced_options.items()} == {
            TransportMode.WALK: 2,
            TransportMode.BICYCLE: 3,
            TransportMode.TWO_WHEELER: 1,
            TransportMode.DRIVE: 4,
        }
        assert balanced_options[TransportMode.TWO_WHEELER].recommended is True
        assert (
            "lowest_generalized_duration"
            in balanced_options[TransportMode.TWO_WHEELER].reason_codes
        )

        initial_valhalla_calls = query_counts(bundle.valhalla)
        initial_here_calls = query_counts(bundle.here)
        fastest = bundle.service.recommend_transport(
            request(
                request_id="recommendation-2",
                objective=RecommendationObjective.FASTEST,
            )
        )

        assert fastest.status == RecommendationStatus.RECOMMENDED
        assert fastest.recommended_mode == TransportMode.DRIVE
        assert fastest.selection_reason == "fastest_route_duration"
        fastest_options = options_by_mode(fastest)
        assert {mode: option.rank for mode, option in fastest_options.items()} == {
            TransportMode.WALK: 4,
            TransportMode.BICYCLE: 3,
            TransportMode.TWO_WHEELER: 2,
            TransportMode.DRIVE: 1,
        }
        assert all(
            option.generalized_duration_seconds == option.duration_seconds
            for option in fastest.options
        )
        assert all(
            option.route is not None and option.route.cache_hit
            for option in fastest.options
        )
        # The objective is recommendation policy, not part of a route cache key.
        assert query_counts(bundle.valhalla) == initial_valhalla_calls
        assert query_counts(bundle.here) == initial_here_calls


def test_balanced_ties_use_raw_duration_distance_then_canonical_mode(tmp_path) -> None:
    # All generalized scores are 1,000. Raw durations therefore decide first;
    # bicycle and walk then exercise distance and canonical ordering separately.
    valhalla_metrics = {
        TransportMode.WALK: (6_000, 1_000),
        TransportMode.BICYCLE: (6_000, 760),
        TransportMode.TWO_WHEELER: (6_000, 640),
        TransportMode.DRIVE: (6_000, 400),
    }
    here_metrics = dict(valhalla_metrics)
    with recommendation_service(
        tmp_path,
        valhalla_metrics=valhalla_metrics,
        here_metrics=here_metrics,
    ) as bundle:
        result = bundle.service.recommend_transport(request())

    assert [
        option.mode
        for option in sorted(
            result.options,
            key=lambda option: option.rank or 99,
        )
    ] == [
        TransportMode.DRIVE,
        TransportMode.TWO_WHEELER,
        TransportMode.BICYCLE,
        TransportMode.WALK,
    ]


def test_default_hard_thresholds_make_routes_ineligible_without_hiding_them(
    tmp_path,
) -> None:
    valhalla_metrics = dict(DEFAULT_VALHALLA_METRICS)
    valhalla_metrics.update(
        {
            TransportMode.WALK: (6_000, 1_801),
            TransportMode.BICYCLE: (6_100, 3_601),
            TransportMode.TWO_WHEELER: (80_001, 1_300),
        }
    )
    here_metrics = dict(DEFAULT_HERE_METRICS)
    here_metrics[TransportMode.TWO_WHEELER] = (80_001, 1_200)
    with recommendation_service(
        tmp_path,
        valhalla_metrics=valhalla_metrics,
        here_metrics=here_metrics,
    ) as bundle:
        result = bundle.service.recommend_transport(
            TransportRecommendationRequest(
                request_id="thresholds",
                origin_id="origin",
                destination_id="destination",
                departure_time=NOW,
            )
        )

    options = options_by_mode(result)
    assert result.status == RecommendationStatus.RECOMMENDED
    assert result.recommended_mode == TransportMode.DRIVE
    assert options[TransportMode.WALK].status == TransportOptionStatus.INELIGIBLE
    assert options[TransportMode.BICYCLE].status == TransportOptionStatus.INELIGIBLE
    assert options[TransportMode.TWO_WHEELER].status == TransportOptionStatus.INELIGIBLE
    assert options[TransportMode.DRIVE].status == TransportOptionStatus.ELIGIBLE
    assert options[TransportMode.WALK].route is not None
    assert options[TransportMode.BICYCLE].route is not None
    assert options[TransportMode.TWO_WHEELER].route is not None
    assert options[TransportMode.WALK].rank is None
    assert "walk_duration_exceeds_limit" in options[TransportMode.WALK].reason_codes
    assert (
        "bicycle_duration_exceeds_limit" in options[TransportMode.BICYCLE].reason_codes
    )
    assert (
        "two_wheeler_distance_exceeds_limit"
        in options[TransportMode.TWO_WHEELER].reason_codes
    )


def test_endpoint_mode_intersection_and_transit_skip_provider_calls(tmp_path) -> None:
    with recommendation_service(
        tmp_path,
        origin_modes=(TransportMode.WALK, TransportMode.BICYCLE),
        destination_modes=(TransportMode.WALK, TransportMode.DRIVE),
    ) as bundle:
        result = bundle.service.recommend_transport(
            request(
                candidate_modes=[
                    TransportMode.DRIVE,
                    TransportMode.TRANSIT,
                    TransportMode.BICYCLE,
                    TransportMode.WALK,
                ]
            )
        )

        options = options_by_mode(result)
        assert result.status == RecommendationStatus.RECOMMENDED
        assert result.recommended_mode == TransportMode.WALK
        assert result.partial is True
        assert options[TransportMode.WALK].status == TransportOptionStatus.ELIGIBLE
        assert (
            options[TransportMode.BICYCLE].status == TransportOptionStatus.UNSUPPORTED
        )
        assert options[TransportMode.DRIVE].status == TransportOptionStatus.UNSUPPORTED
        assert (
            options[TransportMode.TRANSIT].status == TransportOptionStatus.UNSUPPORTED
        )
        assert options[TransportMode.BICYCLE].route is None
        assert options[TransportMode.DRIVE].route is None
        assert options[TransportMode.TRANSIT].route is None
        assert query_counts(bundle.valhalla) == Counter({TransportMode.WALK: 1})
        assert bundle.here.route_queries == []


def test_no_endpoint_supported_candidate_is_no_eligible_mode(tmp_path) -> None:
    with recommendation_service(
        tmp_path,
        origin_modes=(TransportMode.DRIVE,),
        destination_modes=(TransportMode.WALK,),
    ) as bundle:
        result = bundle.service.recommend_transport(
            request(candidate_modes=[TransportMode.WALK, TransportMode.DRIVE])
        )

        assert result.status == RecommendationStatus.NO_ELIGIBLE_MODE
        assert result.recommended_mode is None
        assert (
            result.selection_reason == "no_candidate_mode_supported_by_both_endpoints"
        )
        assert all(
            option.status == TransportOptionStatus.UNSUPPORTED
            for option in result.options
        )
        assert bundle.valhalla.route_queries == []
        assert bundle.here.route_queries == []


def test_one_route_failure_is_isolated_and_successes_are_still_ranked(tmp_path) -> None:
    with recommendation_service(
        tmp_path,
        valhalla_fail_modes={TransportMode.WALK},
    ) as bundle:
        result = bundle.service.recommend_transport(
            request(candidate_modes=[TransportMode.WALK, TransportMode.BICYCLE])
        )

    options = options_by_mode(result)
    assert result.status == RecommendationStatus.RECOMMENDED
    assert result.recommended_mode == TransportMode.BICYCLE
    assert result.partial is True
    assert options[TransportMode.WALK].status == TransportOptionStatus.FAILED
    assert options[TransportMode.WALK].route is None
    assert options[TransportMode.WALK].error_code == "ProviderUnavailableError"
    assert "unavailable for walk" in (options[TransportMode.WALK].error_detail or "")
    assert options[TransportMode.BICYCLE].status == TransportOptionStatus.ELIGIBLE
    assert options[TransportMode.BICYCLE].rank == 1


def test_all_attempted_routes_failing_is_no_route_available(tmp_path) -> None:
    with recommendation_service(
        tmp_path,
        valhalla_fail_modes={TransportMode.WALK, TransportMode.BICYCLE},
    ) as bundle:
        result = bundle.service.recommend_transport(
            request(candidate_modes=[TransportMode.WALK, TransportMode.BICYCLE])
        )

    assert result.status == RecommendationStatus.NO_ROUTE_AVAILABLE
    assert result.recommended_mode is None
    assert result.selection_reason == "no_candidate_mode_produced_a_route"
    assert all(
        option.status == TransportOptionStatus.FAILED for option in result.options
    )
    assert all(option.error_code for option in result.options)


def test_motorized_fallback_is_degraded_but_required_traffic_fails(tmp_path) -> None:
    with recommendation_service(
        tmp_path,
        here_fail_modes={TransportMode.TWO_WHEELER},
    ) as bundle:
        preferred = bundle.service.recommend_transport(
            request(candidate_modes=[TransportMode.TWO_WHEELER])
        )
        required = bundle.service.recommend_transport(
            request(
                request_id="required-traffic",
                candidate_modes=[TransportMode.TWO_WHEELER],
                motorized_traffic_preference=(TrafficPreference.TRAFFIC_AWARE_REQUIRED),
            )
        )

    preferred_option = preferred.options[0]
    assert preferred.status == RecommendationStatus.RECOMMENDED
    assert preferred.degraded is True
    assert preferred_option.status == TransportOptionStatus.ELIGIBLE
    assert preferred_option.route is not None
    assert preferred_option.route.degraded is True
    assert preferred_option.route.route.provider == RoutingProvider.VALHALLA
    assert preferred_option.route.route.provider_role == ProviderRole.FALLBACK
    assert preferred_option.route.route.traffic_basis == TrafficBasis.FREE_FLOW
    assert "degraded_provider_result" in preferred_option.reason_codes

    required_option = required.options[0]
    assert required.status == RecommendationStatus.NO_ROUTE_AVAILABLE
    assert required_option.status == TransportOptionStatus.FAILED
    assert required_option.route is None
    assert required_option.error_code == "ProviderUnavailableError"


def test_route_policy_and_cache_flags_are_forwarded_per_mode(tmp_path) -> None:
    with recommendation_service(tmp_path) as bundle:
        captured = []
        original_route = bundle.service.route

        def capture_route(child_request):
            captured.append(child_request)
            return original_route(child_request)

        bundle.service.route = capture_route  # type: ignore[method-assign]
        result = bundle.service.recommend_transport(
            request(
                candidate_modes=list(ROAD_MODES),
                motorized_traffic_preference=(TrafficPreference.TRAFFIC_AWARE_REQUIRED),
                include_baseline=True,
                force_refresh=True,
                allow_stale_on_error=False,
            )
        )

    captured_by_mode = {item.mode: item for item in captured}
    assert set(captured_by_mode) == set(ROAD_MODES)
    assert len({item.request_id for item in captured}) == len(ROAD_MODES)
    for mode in (TransportMode.WALK, TransportMode.BICYCLE):
        child = captured_by_mode[mode]
        assert child.traffic_preference == TrafficPreference.FREE_FLOW
        assert child.provider_hint == RoutingProvider.VALHALLA
        assert child.include_baseline is False
    for mode in (TransportMode.TWO_WHEELER, TransportMode.DRIVE):
        child = captured_by_mode[mode]
        assert child.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED
        assert child.provider_hint is None
        assert child.include_baseline is True
    assert all(item.force_refresh is True for item in captured)
    assert all(item.allow_stale_on_error is False for item in captured)
    assert all(item.departure_time == NOW for item in captured)

    result_options = options_by_mode(result)
    assert result_options[TransportMode.WALK].route is not None
    assert result_options[TransportMode.WALK].route.baseline_route is None
    assert result_options[TransportMode.BICYCLE].route is not None
    assert result_options[TransportMode.BICYCLE].route.baseline_route is None
    assert result_options[TransportMode.TWO_WHEELER].route is not None
    assert result_options[TransportMode.TWO_WHEELER].route.baseline_route is not None
    assert result_options[TransportMode.DRIVE].route is not None
    assert result_options[TransportMode.DRIVE].route.baseline_route is not None


def test_force_refresh_bypasses_each_mode_route_cache(tmp_path) -> None:
    with recommendation_service(tmp_path) as bundle:
        first = bundle.service.recommend_transport(request())
        first_valhalla = query_counts(bundle.valhalla)
        first_here = query_counts(bundle.here)

        cached = bundle.service.recommend_transport(
            request(request_id="cached-recommendation")
        )
        assert query_counts(bundle.valhalla) == first_valhalla
        assert query_counts(bundle.here) == first_here
        assert all(
            option.route is not None and option.route.cache_hit
            for option in cached.options
        )

        refreshed = bundle.service.recommend_transport(
            request(request_id="refreshed-recommendation", force_refresh=True)
        )

        assert query_counts(bundle.valhalla) == Counter(
            {mode: count * 2 for mode, count in first_valhalla.items()}
        )
        assert query_counts(bundle.here) == Counter(
            {mode: count * 2 for mode, count in first_here.items()}
        )
        assert all(
            option.route is not None and not option.route.cache_hit
            for option in refreshed.options
        )
        assert all(
            option.route is not None and not option.route.cache_hit
            for option in first.options
        )


class CountingRuntimeBuilder:
    def __init__(self, service: HybridTrafficService) -> None:
        self.service = service
        self.calls = 0

    def __call__(self) -> TrafficRuntime:
        self.calls += 1
        return TrafficRuntime(service=self.service)


def api_payload(**updates) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": "api-recommendation",
        "origin_id": "origin",
        "destination_id": "destination",
        "candidate_modes": ["walk"],
        "departure_time": NOW.isoformat(),
        "max_walk_duration_seconds": None,
    }
    payload.update(updates)
    return payload


def test_recommendation_endpoint_is_protected_without_eager_runtime_init(
    tmp_path,
) -> None:
    with recommendation_service(tmp_path) as bundle:
        builder = CountingRuntimeBuilder(bundle.service)
        factory = TrafficRuntimeFactory(builder)
        application = create_app(
            runtime_factory=factory,
            internal_api_key="secret-traffic-key",
        )

        with TestClient(application) as client:
            missing = client.post(
                "/transport-recommendations",
                json=api_payload(),
            )
            wrong = client.post(
                "/transport-recommendations",
                json=api_payload(),
                headers={"X-NexTrip-Traffic-Key": "wrong"},
            )
            accepted = client.post(
                "/transport-recommendations",
                json=api_payload(),
                headers={
                    "X-NexTrip-Traffic-Key": "secret-traffic-key",
                },
            )

        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["error"]["code"] == "traffic_api_key_invalid"
        assert missing.headers["www-authenticate"] == "ApiKey"
        assert builder.calls == 1
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["status"] == "recommended"
        assert body["recommended_mode"] == "walk"
        assert body["options"][0]["route"]["route"]["provider"] == "valhalla"
        assert query_counts(bundle.valhalla) == Counter({TransportMode.WALK: 1})
        assert bundle.here.route_queries == []


def test_recommendation_endpoint_returns_typed_statuses_and_place_errors(
    tmp_path,
) -> None:
    with recommendation_service(
        tmp_path,
        origin_modes=(TransportMode.DRIVE,),
        destination_modes=(TransportMode.WALK,),
    ) as bundle:
        builder = CountingRuntimeBuilder(bundle.service)
        application = create_app(
            runtime_factory=TrafficRuntimeFactory(builder),
            internal_api_key=None,
        )

        with TestClient(application) as client:
            no_eligible = client.post(
                "/transport-recommendations",
                json=api_payload(candidate_modes=["walk", "drive"]),
            )
            missing_place = client.post(
                "/transport-recommendations",
                json=api_payload(origin_id="missing"),
            )

        assert no_eligible.status_code == 200
        assert no_eligible.json()["status"] == "no_eligible_mode"
        assert no_eligible.json()["recommended_mode"] is None
        assert all(
            option["status"] == "unsupported"
            for option in no_eligible.json()["options"]
        )
        assert missing_place.status_code == 404
        assert missing_place.json()["error"]["code"] == "access_point_not_found"


@dataclass
class CliRecommendationResult:
    status: RecommendationStatus
    request_id: str
    recommended_mode: TransportMode | None


class CliRecommendationService:
    def __init__(
        self,
        *,
        status: RecommendationStatus = RecommendationStatus.RECOMMENDED,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.requests = []
        self.closed = False

    def recommend_transport(self, recommendation_request):
        self.requests.append(recommendation_request)
        if self.error is not None:
            raise self.error
        return CliRecommendationResult(
            status=self.status,
            request_id=recommendation_request.request_id,
            recommended_mode=(
                TransportMode.TWO_WHEELER
                if self.status == RecommendationStatus.RECOMMENDED
                else None
            ),
        )

    def close(self) -> None:
        self.closed = True


def test_recommend_transport_cli_builds_typed_request_and_converts_units(
    monkeypatch,
    capsys,
) -> None:
    service = CliRecommendationService()
    monkeypatch.setattr(cli, "_build_service", lambda: service)

    exit_code = cli.main(
        [
            "recommend-transport",
            "origin",
            "destination",
            "--modes",
            "walk",
            "two_wheeler",
            "drive",
            "--objective",
            "fastest",
            "--departure-time",
            "2026-08-20T17:00:00+07:00",
            "--motorized-traffic-preference",
            "traffic_aware_required",
            "--max-walk-minutes",
            "12.5",
            "--max-bicycle-minutes",
            "45",
            "--max-two-wheeler-km",
            "12.25",
            "--include-baseline",
            "--force-refresh",
            "--no-allow-stale-on-error",
            "--request-id",
            "cli-recommendation",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "recommended_mode": "two_wheeler",
        "request_id": "cli-recommendation",
        "status": "recommended",
    }
    built = service.requests[0]
    assert built.origin_id == "origin"
    assert built.destination_id == "destination"
    assert built.candidate_modes == [
        TransportMode.WALK,
        TransportMode.TWO_WHEELER,
        TransportMode.DRIVE,
    ]
    assert built.objective == RecommendationObjective.FASTEST
    assert (
        built.motorized_traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED
    )
    assert built.max_walk_duration_seconds == 750
    assert built.max_bicycle_duration_seconds == 2_700
    assert built.max_two_wheeler_distance_meters == 12_250
    assert built.include_baseline is True
    assert built.force_refresh is True
    assert built.allow_stale_on_error is False
    assert built.departure_time.utcoffset().total_seconds() == 7 * 3_600
    assert service.closed is True


def test_recommend_transport_cli_uses_status_exit_code_and_closes_service(
    monkeypatch,
    capsys,
) -> None:
    service = CliRecommendationService(status=RecommendationStatus.NO_ROUTE_AVAILABLE)
    monkeypatch.setattr(cli, "_build_service", lambda: service)

    exit_code = cli.main(
        [
            "recommend-transport",
            "origin",
            "destination",
            "--request-id",
            "no-route",
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "recommended_mode": None,
        "request_id": "no-route",
        "status": "no_route_available",
    }
    assert service.closed is True


def test_recommend_transport_cli_error_is_json_and_closes_service(
    monkeypatch,
    capsys,
) -> None:
    service = CliRecommendationService(error=RuntimeError("routing failed"))
    monkeypatch.setattr(cli, "_build_service", lambda: service)

    exit_code = cli.main(["recommend-transport", "origin", "destination"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "routing failed",
    }
    assert service.closed is True
