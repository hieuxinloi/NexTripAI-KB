from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from time import perf_counter

from nextrip_pipeline.schemas import (
    ProviderRole,
    RouteMatrixResult,
    RouteObservation,
    RoutingProvider,
    TransportMode,
    ValidationResult,
    ValidationStatus,
)

from .access_points import AccessPointRegistry
from .cache import CacheEntry, SQLiteTrafficCache, make_cache_key
from .config import TrafficSettings
from .errors import (
    AccessPointNotFoundError,
    InvalidTrafficRequestError,
    RoutingProviderError,
    TrafficConfigurationError,
    TrafficError,
    TrafficValidationError,
    UnsupportedTransportModeError,
)
from .models import (
    ProviderAttempt,
    RecommendationObjective,
    RecommendationStatus,
    TrafficMatrixRequest,
    TrafficMatrixResponse,
    TrafficPreference,
    TrafficRouteRequest,
    TrafficRouteResponse,
    TransportOptionStatus,
    TransportRecommendationOption,
    TransportRecommendationRequest,
    TransportRecommendationResponse,
)
from .providers import (
    MatrixQuery,
    ProviderUnavailableError,
    RouteEndpoint,
    RouteQuery,
    RoutingProviderAdapter,
)
from .validation import TrafficQualityValidator


_MOTORIZED_MODES = {TransportMode.DRIVE, TransportMode.TWO_WHEELER}
_RECOMMENDATION_MODE_ORDER = (
    TransportMode.WALK,
    TransportMode.BICYCLE,
    TransportMode.TWO_WHEELER,
    TransportMode.DRIVE,
)
_RECOMMENDATION_MODE_RANK = {
    mode: index for index, mode in enumerate(_RECOMMENDATION_MODE_ORDER)
}
_BALANCED_MODE_OVERHEAD_SECONDS = {
    TransportMode.WALK: 0,
    TransportMode.BICYCLE: 4 * 60,
    TransportMode.TWO_WHEELER: 6 * 60,
    TransportMode.DRIVE: 10 * 60,
}


@dataclass(slots=True)
class _CircuitState:
    consecutive_failures: int = 0
    opened_until: datetime | None = None


class _CircuitBreaker:
    def __init__(self, *, threshold: int, cooldown_seconds: int) -> None:
        self.threshold = threshold
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._states: dict[RoutingProvider, _CircuitState] = {}
        self._lock = RLock()

    def allow(self, provider: RoutingProvider, now: datetime) -> bool:
        with self._lock:
            state = self._states.setdefault(provider, _CircuitState())
            if state.opened_until is None:
                return True
            if state.opened_until <= now:
                state.opened_until = None
                state.consecutive_failures = 0
                return True
            return False

    def success(self, provider: RoutingProvider) -> None:
        with self._lock:
            self._states[provider] = _CircuitState()

    def failure(self, provider: RoutingProvider, now: datetime) -> None:
        with self._lock:
            state = self._states.setdefault(provider, _CircuitState())
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.threshold:
                state.opened_until = now + self.cooldown


@dataclass(slots=True)
class _RouteCall:
    route: RouteObservation | None
    validations: list[ValidationResult]
    attempt: ProviderAttempt
    error: TrafficError | None


@dataclass(slots=True)
class _MatrixCall:
    matrix: RouteMatrixResult | None
    validations: list[ValidationResult]
    attempt: ProviderAttempt
    error: TrafficError | None


class HybridTrafficService:
    """Routes with Valhalla baseline and HERE traffic-aware selection.

    The service never upgrades a free-flow result to current traffic. When
    HERE is unavailable, a Valhalla result is explicitly marked as fallback
    and the response is marked degraded.
    """

    def __init__(
        self,
        *,
        registry: AccessPointRegistry,
        cache: SQLiteTrafficCache,
        providers: dict[RoutingProvider, RoutingProviderAdapter],
        settings: TrafficSettings,
        validator: TrafficQualityValidator | None = None,
        clock=None,
    ) -> None:
        self.registry = registry
        self.cache = cache
        self.providers = dict(providers)
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self.validator = validator or TrafficQualityValidator(clock=self._clock)
        # A hybrid request should not pay the sum of HERE and Valhalla
        # latencies. Both independent provider calls run concurrently while
        # selection remains deterministic after they finish.
        self._provider_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="nextrip-routing",
        )
        self._circuit = _CircuitBreaker(
            threshold=settings.circuit_breaker_failure_threshold,
            cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        )

    def route(self, request: TrafficRouteRequest) -> TrafficRouteResponse:
        origin = self._resolve(request.origin_id)
        destination = self._resolve(request.destination_id)
        self._ensure_distinct_access_points(origin, destination)
        self._ensure_mode_supported(request.mode, [origin, destination])
        key = self._route_cache_key(
            request, origin.access_point_id, destination.access_point_id
        )
        now = self._now()
        cached = self.cache.get_entry(key, now=now, allow_stale=False)
        if cached is not None and not request.force_refresh:
            return self._cached_route_response(cached, request.request_id, stale=False)
        stale_candidate = self.cache.get_entry(key, now=now, allow_stale=True)

        try:
            response = self._compute_route(request, origin, destination)
        except TrafficError:
            if self._can_use_stale(request, stale_candidate, now):
                assert stale_candidate is not None
                return self._cached_route_response(
                    stale_candidate,
                    request.request_id,
                    stale=True,
                )
            raise

        self.cache.put(
            key,
            response.model_dump(mode="json"),
            expires_at=response.route.expires_at,
            observation_id=response.route.observation_id,
            now=now,
        )
        return response

    def recommend_transport(
        self,
        request: TransportRecommendationRequest,
    ) -> TransportRecommendationResponse:
        """Evaluate bounded road modes and return an explainable recommendation.

        Route computation and caching remain owned by :meth:`route`. A
        recommendation is intentionally recomputed from those route snapshots
        so user constraints can change without duplicating provider calls.
        """

        origin = self._resolve(request.origin_id)
        destination = self._resolve(request.destination_id)
        self._ensure_distinct_access_points(origin, destination)
        requested = set(request.candidate_modes)
        supported = set(origin.supported_modes) & set(destination.supported_modes)
        options: list[TransportRecommendationOption] = []

        for mode in (*_RECOMMENDATION_MODE_ORDER, TransportMode.TRANSIT):
            if mode not in requested:
                continue
            if mode == TransportMode.TRANSIT:
                options.append(
                    TransportRecommendationOption(
                        mode=mode,
                        status=TransportOptionStatus.UNSUPPORTED,
                        reason_codes=["transit_provider_not_configured"],
                    )
                )
                continue
            if mode not in supported:
                options.append(
                    TransportRecommendationOption(
                        mode=mode,
                        status=TransportOptionStatus.UNSUPPORTED,
                        reason_codes=["mode_not_supported_by_both_endpoints"],
                    )
                )
                continue

            child_request = self._recommendation_route_request(request, mode)
            try:
                route_response = self.route(child_request)
            except TrafficError as error:
                options.append(
                    TransportRecommendationOption(
                        mode=mode,
                        status=TransportOptionStatus.FAILED,
                        reason_codes=["route_computation_failed"],
                        error_code=type(error).__name__,
                        error_detail=str(error)[:300],
                    )
                )
                continue
            options.append(self._recommendation_option(request, mode, route_response))

        ranked = sorted(
            (
                option
                for option in options
                if option.status == TransportOptionStatus.ELIGIBLE
            ),
            key=lambda option: (
                option.generalized_duration_seconds,
                option.duration_seconds,
                option.distance_meters,
                _RECOMMENDATION_MODE_RANK[option.mode],
            ),
        )
        rank_by_mode = {option.mode: rank for rank, option in enumerate(ranked, 1)}
        recommended_mode = ranked[0].mode if ranked else None
        ranked_options: list[TransportRecommendationOption] = []
        for option in options:
            rank = rank_by_mode.get(option.mode)
            reason_codes = list(option.reason_codes)
            recommended = option.mode == recommended_mode
            if recommended:
                reason_codes.append(
                    "lowest_generalized_duration"
                    if request.objective == RecommendationObjective.BALANCED
                    else "shortest_route_duration"
                )
            ranked_options.append(
                option.model_copy(
                    update={
                        "rank": rank,
                        "recommended": recommended,
                        "reason_codes": reason_codes,
                    }
                )
            )

        successful = [option for option in options if option.route is not None]
        if recommended_mode is not None:
            status = RecommendationStatus.RECOMMENDED
            selection_reason = (
                "balanced_generalized_duration"
                if request.objective == RecommendationObjective.BALANCED
                else "fastest_route_duration"
            )
        elif successful:
            status = RecommendationStatus.NO_ELIGIBLE_MODE
            selection_reason = "all_available_modes_exceed_constraints"
        elif any(option.status == TransportOptionStatus.FAILED for option in options):
            status = RecommendationStatus.NO_ROUTE_AVAILABLE
            selection_reason = "no_candidate_mode_produced_a_route"
        else:
            status = RecommendationStatus.NO_ELIGIBLE_MODE
            selection_reason = "no_candidate_mode_supported_by_both_endpoints"

        recommended_option = next(
            (option for option in ranked_options if option.recommended),
            None,
        )
        return TransportRecommendationResponse(
            request_id=request.request_id,
            origin=origin,
            destination=destination,
            objective=request.objective,
            status=status,
            recommended_mode=recommended_mode,
            selection_reason=selection_reason,
            options=ranked_options,
            computed_at=self._now(),
            degraded=(
                recommended_option.route.degraded
                if recommended_option is not None and recommended_option.route
                else any(
                    option.route is not None and option.route.degraded
                    for option in ranked_options
                )
            ),
            partial=any(
                option.status
                in {TransportOptionStatus.FAILED, TransportOptionStatus.UNSUPPORTED}
                for option in ranked_options
            ),
        )

    def matrix(self, request: TrafficMatrixRequest) -> TrafficMatrixResponse:
        cell_count = len(request.origin_ids) * len(request.destination_ids)
        if cell_count > self.settings.max_matrix_cells:
            raise TrafficConfigurationError(
                f"matrix has {cell_count} cells; limit is {self.settings.max_matrix_cells}"
            )
        origins = [self._resolve(value) for value in request.origin_ids]
        destinations = [self._resolve(value) for value in request.destination_ids]
        self._ensure_mode_supported(request.mode, [*origins, *destinations])
        key = self._matrix_cache_key(
            request,
            [item.access_point_id for item in origins],
            [item.access_point_id for item in destinations],
        )
        now = self._now()
        cached = self.cache.get_entry(key, now=now, allow_stale=False)
        if cached is not None and not request.force_refresh:
            return self._cached_matrix_response(cached, request.request_id, stale=False)
        stale_candidate = self.cache.get_entry(key, now=now, allow_stale=True)

        try:
            response = self._compute_matrix(request, origins, destinations)
        except TrafficError:
            if self._can_use_stale(request, stale_candidate, now):
                assert stale_candidate is not None
                return self._cached_matrix_response(
                    stale_candidate,
                    request.request_id,
                    stale=True,
                )
            raise

        self.cache.put(
            key,
            response.model_dump(mode="json"),
            expires_at=response.matrix.expires_at,
            matrix_id=response.matrix.matrix_id,
            now=now,
        )
        return response

    def get_route(
        self, observation_id: str, *, allow_stale: bool = False
    ) -> TrafficRouteResponse | None:
        entry = self.cache.get_by_observation_id(
            observation_id,
            allow_stale=allow_stale,
            now=self._now(),
        )
        if entry is None:
            return None
        response = TrafficRouteResponse.model_validate(entry.payload)
        return response.model_copy(update={"cache_hit": True, "stale": entry.is_stale})

    def get_matrix(
        self, matrix_id: str, *, allow_stale: bool = False
    ) -> TrafficMatrixResponse | None:
        entry = self.cache.get_by_matrix_id(
            matrix_id,
            allow_stale=allow_stale,
            now=self._now(),
        )
        if entry is None:
            return None
        response = TrafficMatrixResponse.model_validate(entry.payload)
        return response.model_copy(update={"cache_hit": True, "stale": entry.is_stale})

    def provider_health(self) -> list[object]:
        return [
            self.providers[provider].health()
            for provider in sorted(self.providers, key=lambda value: value.value)
        ]

    def close(self) -> None:
        self._provider_executor.shutdown(wait=True, cancel_futures=True)
        for provider in self.providers.values():
            provider.close()
        self.cache.close()

    def _compute_route(self, request, origin, destination) -> TrafficRouteResponse:
        if request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED and (
            request.mode not in _MOTORIZED_MODES
        ):
            raise UnsupportedTransportModeError(
                "traffic-aware routing is only supported for motorized modes"
            )
        if request.provider_hint == RoutingProvider.VALHALLA and (
            request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED
        ):
            raise TrafficConfigurationError(
                "Valhalla cannot satisfy traffic_aware_required without a traffic feed"
            )

        valhalla_call: _RouteCall | None = None
        attempts: list[ProviderAttempt] = []
        use_hybrid = (
            request.mode in _MOTORIZED_MODES
            and request.traffic_preference != TrafficPreference.FREE_FLOW
            and request.provider_hint != RoutingProvider.VALHALLA
        )
        should_prepare_baseline = RoutingProvider.VALHALLA in self.providers and (
            request.include_baseline or use_hybrid
        )
        if should_prepare_baseline and not use_hybrid:
            valhalla_call = self._call_route(
                RoutingProvider.VALHALLA,
                request,
                origin,
                destination,
                purpose="baseline" if use_hybrid else "selected",
                ttl_seconds=self.settings.free_flow_ttl_seconds,
            )
            attempts.append(valhalla_call.attempt)

        if use_hybrid:
            valhalla_future = (
                self._provider_executor.submit(
                    self._call_route,
                    RoutingProvider.VALHALLA,
                    request,
                    origin,
                    destination,
                    purpose="baseline",
                    ttl_seconds=self.settings.free_flow_ttl_seconds,
                )
                if should_prepare_baseline
                else None
            )
            here_future = self._provider_executor.submit(
                self._call_route,
                RoutingProvider.HERE,
                request,
                origin,
                destination,
                purpose="selected",
                ttl_seconds=self.settings.route_ttl_seconds,
            )
            if valhalla_future is not None:
                valhalla_call = valhalla_future.result()
                attempts.append(valhalla_call.attempt)
            here_call = here_future.result()
            attempts.append(here_call.attempt)
            if here_call.route is not None:
                validations = list(here_call.validations)
                baseline = valhalla_call.route if valhalla_call else None
                if baseline is not None:
                    validations.extend(
                        self.validator.validate_cross_provider_route(
                            here_call.route,
                            baseline,
                            run_id=request.request_id,
                        )
                    )
                return TrafficRouteResponse(
                    request_id=request.request_id,
                    degraded=False,
                    selection_reason="here_traffic_aware_selected",
                    origin=origin,
                    destination=destination,
                    route=here_call.route,
                    baseline_route=baseline if request.include_baseline else None,
                    validations=validations,
                    provider_attempts=attempts,
                )
            if request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED:
                raise here_call.error or RoutingProviderError(
                    "HERE traffic-aware route failed"
                )
            if valhalla_call and valhalla_call.route is not None:
                fallback_reason = self._fallback_reason(here_call.error)
                fallback_expires_at = min(
                    valhalla_call.route.expires_at,
                    self._now() + timedelta(seconds=self.settings.route_ttl_seconds),
                )
                fallback = valhalla_call.route.model_copy(
                    update={
                        "provider_role": ProviderRole.FALLBACK,
                        "fallback_reason": fallback_reason,
                        "fallback_used": True,
                        # A free-flow fallback stored under a traffic-aware
                        # cache key must be retried at the short traffic TTL,
                        # not retained for Valhalla's 24-hour baseline TTL.
                        "expires_at": fallback_expires_at,
                    }
                )
                attempts[0] = valhalla_call.attempt.model_copy(
                    update={"purpose": "fallback"}
                )
                return TrafficRouteResponse(
                    request_id=request.request_id,
                    degraded=True,
                    selection_reason=f"valhalla_free_flow_fallback:{fallback_reason}",
                    origin=origin,
                    destination=destination,
                    route=fallback,
                    validations=valhalla_call.validations,
                    provider_attempts=attempts,
                )
            raise (
                here_call.error
                or (valhalla_call.error if valhalla_call else None)
                or RoutingProviderError("no routing provider produced a route")
            )

        provider = request.provider_hint or RoutingProvider.VALHALLA
        selected = valhalla_call if provider == RoutingProvider.VALHALLA else None
        if selected is None:
            selected = self._call_route(
                provider,
                request,
                origin,
                destination,
                purpose="selected",
                ttl_seconds=(
                    self.settings.free_flow_ttl_seconds
                    if provider == RoutingProvider.VALHALLA
                    else self.settings.route_ttl_seconds
                ),
            )
            attempts.append(selected.attempt)
        if selected.route is None:
            raise selected.error or RoutingProviderError("routing provider failed")
        return TrafficRouteResponse(
            request_id=request.request_id,
            selection_reason=f"{provider.value}_selected",
            origin=origin,
            destination=destination,
            route=selected.route,
            validations=selected.validations,
            provider_attempts=attempts,
        )

    @staticmethod
    def _recommendation_route_request(
        request: TransportRecommendationRequest,
        mode: TransportMode,
    ) -> TrafficRouteRequest:
        non_motorized = mode in {TransportMode.WALK, TransportMode.BICYCLE}
        return TrafficRouteRequest(
            request_id=f"{request.request_id}:{mode.value}",
            origin_id=request.origin_id,
            destination_id=request.destination_id,
            mode=mode,
            departure_time=request.departure_time,
            traffic_preference=(
                TrafficPreference.FREE_FLOW
                if non_motorized
                else request.motorized_traffic_preference
            ),
            provider_hint=(RoutingProvider.VALHALLA if non_motorized else None),
            include_baseline=(request.include_baseline if not non_motorized else False),
            force_refresh=request.force_refresh,
            allow_stale_on_error=request.allow_stale_on_error,
        )

    @staticmethod
    def _recommendation_option(
        request: TransportRecommendationRequest,
        mode: TransportMode,
        response: TrafficRouteResponse,
    ) -> TransportRecommendationOption:
        route = response.route
        reasons = ["route_available"]
        eligible = True
        if (
            mode == TransportMode.WALK
            and request.max_walk_duration_seconds is not None
            and route.duration_seconds > request.max_walk_duration_seconds
        ):
            eligible = False
            reasons.append("walk_duration_exceeds_limit")
        elif (
            mode == TransportMode.BICYCLE
            and request.max_bicycle_duration_seconds is not None
            and route.duration_seconds > request.max_bicycle_duration_seconds
        ):
            eligible = False
            reasons.append("bicycle_duration_exceeds_limit")
        elif (
            mode == TransportMode.TWO_WHEELER
            and request.max_two_wheeler_distance_meters is not None
            and route.distance_meters > request.max_two_wheeler_distance_meters
        ):
            eligible = False
            reasons.append("two_wheeler_distance_exceeds_limit")
        else:
            reasons.append("within_configured_limits")

        if response.degraded:
            reasons.append("degraded_provider_result")
        generalized_duration = route.duration_seconds
        if request.objective == RecommendationObjective.BALANCED:
            generalized_duration += _BALANCED_MODE_OVERHEAD_SECONDS[mode]
            reasons.append("balanced_mode_overhead_applied")
        return TransportRecommendationOption(
            mode=mode,
            status=(
                TransportOptionStatus.ELIGIBLE
                if eligible
                else TransportOptionStatus.INELIGIBLE
            ),
            route=response,
            distance_meters=route.distance_meters,
            duration_seconds=route.duration_seconds,
            generalized_duration_seconds=generalized_duration,
            reason_codes=reasons,
        )

    def _compute_matrix(self, request, origins, destinations) -> TrafficMatrixResponse:
        if request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED and (
            request.mode not in _MOTORIZED_MODES
        ):
            raise UnsupportedTransportModeError(
                "traffic-aware matrix is only supported for motorized modes"
            )
        use_hybrid = (
            request.mode in _MOTORIZED_MODES
            and request.traffic_preference != TrafficPreference.FREE_FLOW
            and request.provider_hint != RoutingProvider.VALHALLA
        )
        attempts: list[ProviderAttempt] = []
        valhalla_call: _MatrixCall | None = None
        should_prepare_baseline = RoutingProvider.VALHALLA in self.providers and (
            request.include_baseline or use_hybrid
        )
        if should_prepare_baseline and not use_hybrid:
            valhalla_call = self._call_matrix(
                RoutingProvider.VALHALLA,
                request,
                origins,
                destinations,
                purpose="baseline" if use_hybrid else "selected",
                ttl_seconds=self.settings.free_flow_ttl_seconds,
            )
            attempts.append(valhalla_call.attempt)

        if use_hybrid:
            valhalla_future = (
                self._provider_executor.submit(
                    self._call_matrix,
                    RoutingProvider.VALHALLA,
                    request,
                    origins,
                    destinations,
                    purpose="baseline",
                    ttl_seconds=self.settings.free_flow_ttl_seconds,
                )
                if should_prepare_baseline
                else None
            )
            here_future = self._provider_executor.submit(
                self._call_matrix,
                RoutingProvider.HERE,
                request,
                origins,
                destinations,
                purpose="selected",
                ttl_seconds=self.settings.matrix_ttl_seconds,
            )
            if valhalla_future is not None:
                valhalla_call = valhalla_future.result()
                attempts.append(valhalla_call.attempt)
            here_call = here_future.result()
            attempts.append(here_call.attempt)
            if here_call.matrix is not None:
                return TrafficMatrixResponse(
                    request_id=request.request_id,
                    selection_reason="here_traffic_aware_selected",
                    origins=origins,
                    destinations=destinations,
                    matrix=here_call.matrix,
                    baseline_matrix=(
                        valhalla_call.matrix
                        if request.include_baseline and valhalla_call
                        else None
                    ),
                    validations=here_call.validations,
                    provider_attempts=attempts,
                )
            if request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED:
                raise here_call.error or RoutingProviderError(
                    "HERE traffic-aware matrix failed"
                )
            if valhalla_call and valhalla_call.matrix is not None:
                fallback_reason = self._fallback_reason(here_call.error)
                fallback_expires_at = min(
                    valhalla_call.matrix.expires_at,
                    self._now() + timedelta(seconds=self.settings.matrix_ttl_seconds),
                )
                fallback = valhalla_call.matrix.model_copy(
                    update={
                        "provider_role": ProviderRole.FALLBACK,
                        "fallback_reason": fallback_reason,
                        "fallback_used": True,
                        "expires_at": fallback_expires_at,
                    }
                )
                attempts[0] = valhalla_call.attempt.model_copy(
                    update={"purpose": "fallback"}
                )
                return TrafficMatrixResponse(
                    request_id=request.request_id,
                    degraded=True,
                    selection_reason=f"valhalla_free_flow_fallback:{fallback_reason}",
                    origins=origins,
                    destinations=destinations,
                    matrix=fallback,
                    validations=valhalla_call.validations,
                    provider_attempts=attempts,
                )
            raise (
                here_call.error
                or (valhalla_call.error if valhalla_call else None)
                or RoutingProviderError("no routing provider produced a matrix")
            )

        provider = request.provider_hint or RoutingProvider.VALHALLA
        selected = valhalla_call if provider == RoutingProvider.VALHALLA else None
        if selected is None:
            selected = self._call_matrix(
                provider,
                request,
                origins,
                destinations,
                purpose="selected",
                ttl_seconds=(
                    self.settings.free_flow_ttl_seconds
                    if provider == RoutingProvider.VALHALLA
                    else self.settings.matrix_ttl_seconds
                ),
            )
            attempts.append(selected.attempt)
        if selected.matrix is None:
            raise selected.error or RoutingProviderError("matrix provider failed")
        return TrafficMatrixResponse(
            request_id=request.request_id,
            selection_reason=f"{provider.value}_selected",
            origins=origins,
            destinations=destinations,
            matrix=selected.matrix,
            validations=selected.validations,
            provider_attempts=attempts,
        )

    def _call_route(
        self,
        provider_name,
        request,
        origin,
        destination,
        *,
        purpose,
        ttl_seconds,
    ) -> _RouteCall:
        started = perf_counter()
        try:
            provider = self._ready_provider(provider_name)
            route = provider.route(
                RouteQuery(
                    request_id=request.request_id,
                    origin=RouteEndpoint.from_access_point(origin),
                    destination=RouteEndpoint.from_access_point(destination),
                    mode=request.mode,
                    departure_time=request.departure_time,
                    ttl_seconds=ttl_seconds,
                )
            )
            validations = self.validator.validate_route(
                route,
                origin,
                destination,
                run_id=request.request_id,
            )
            self._ensure_valid(validations)
        except TrafficError as error:
            self._circuit.failure(provider_name, self._now())
            return _RouteCall(
                route=None,
                validations=[],
                attempt=self._attempt(provider_name, purpose, started, error),
                error=error,
            )
        self._circuit.success(provider_name)
        return _RouteCall(
            route=route,
            validations=validations,
            attempt=self._attempt(provider_name, purpose, started, None),
            error=None,
        )

    def _call_matrix(
        self,
        provider_name,
        request,
        origins,
        destinations,
        *,
        purpose,
        ttl_seconds,
    ) -> _MatrixCall:
        started = perf_counter()
        try:
            provider = self._ready_provider(provider_name)
            matrix = provider.matrix(
                MatrixQuery(
                    request_id=request.request_id,
                    origins=[RouteEndpoint.from_access_point(item) for item in origins],
                    destinations=[
                        RouteEndpoint.from_access_point(item) for item in destinations
                    ],
                    mode=request.mode,
                    departure_time=request.departure_time,
                    ttl_seconds=ttl_seconds,
                    max_cells=self.settings.max_matrix_cells,
                )
            )
            validations = self.validator.validate_matrix(
                matrix,
                expected_origin_ids=[item.access_point_id for item in origins],
                expected_destination_ids=[
                    item.access_point_id for item in destinations
                ],
                run_id=request.request_id,
            )
            self._ensure_valid(validations)
        except TrafficError as error:
            self._circuit.failure(provider_name, self._now())
            return _MatrixCall(
                matrix=None,
                validations=[],
                attempt=self._attempt(provider_name, purpose, started, error),
                error=error,
            )
        self._circuit.success(provider_name)
        return _MatrixCall(
            matrix=matrix,
            validations=validations,
            attempt=self._attempt(provider_name, purpose, started, None),
            error=None,
        )

    def _ready_provider(self, name: RoutingProvider) -> RoutingProviderAdapter:
        provider = self.providers.get(name)
        if provider is None:
            raise TrafficConfigurationError(f"provider '{name.value}' is disabled")
        now = self._now()
        if not self._circuit.allow(name, now):
            raise ProviderUnavailableError(
                f"provider '{name.value}' circuit breaker is open"
            )
        return provider

    def _resolve(self, identifier: str):
        try:
            return self.registry.resolve(identifier)
        except AccessPointNotFoundError as error:
            raise error

    @staticmethod
    def _ensure_distinct_access_points(origin, destination) -> None:
        if origin.access_point_id == destination.access_point_id:
            raise InvalidTrafficRequestError(
                "origin and destination resolve to the same canonical access point"
            )

    @staticmethod
    def _ensure_mode_supported(mode: TransportMode, access_points) -> None:
        unsupported = [
            item.access_point_id
            for item in access_points
            if mode not in item.supported_modes
        ]
        if unsupported:
            raise UnsupportedTransportModeError(
                f"mode '{mode.value}' is not supported by access point(s): "
                + ",".join(unsupported)
            )

    @staticmethod
    def _ensure_valid(validations: list[ValidationResult]) -> None:
        failed = [item for item in validations if item.status == ValidationStatus.FAIL]
        if failed:
            reasons = sorted(
                {reason for item in failed for reason in item.reason_codes}
            )
            raise TrafficValidationError(
                "traffic result failed validation: " + ",".join(reasons)
            )

    def _attempt(self, provider, purpose, started, error) -> ProviderAttempt:
        return ProviderAttempt(
            provider=provider,
            purpose=purpose,
            succeeded=error is None,
            duration_ms=max(0, round((perf_counter() - started) * 1000)),
            error_code=type(error).__name__ if error else None,
            detail=str(error)[:300] if error else None,
        )

    def _route_cache_key(self, request, origin_id, destination_id) -> str:
        return make_cache_key(
            "route",
            {
                "origin": origin_id,
                "destination": destination_id,
                "mode": request.mode.value,
                "traffic_preference": request.traffic_preference.value,
                "provider_hint": (
                    request.provider_hint.value if request.provider_hint else None
                ),
                "include_baseline": request.include_baseline,
                "departure_bucket": self._departure_bucket(
                    request.departure_time,
                    free_flow=request.traffic_preference == TrafficPreference.FREE_FLOW,
                ),
            },
        )

    def _matrix_cache_key(self, request, origin_ids, destination_ids) -> str:
        return make_cache_key(
            "matrix",
            {
                "origins": origin_ids,
                "destinations": destination_ids,
                "mode": request.mode.value,
                "traffic_preference": request.traffic_preference.value,
                "provider_hint": (
                    request.provider_hint.value if request.provider_hint else None
                ),
                "include_baseline": request.include_baseline,
                "departure_bucket": self._departure_bucket(
                    request.departure_time,
                    free_flow=request.traffic_preference == TrafficPreference.FREE_FLOW,
                ),
            },
        )

    def _departure_bucket(self, value: datetime, *, free_flow: bool) -> str | None:
        if free_flow:
            return None
        utc = value.astimezone(UTC)
        minute = (
            utc.minute // self.settings.departure_bucket_minutes
        ) * self.settings.departure_bucket_minutes
        return utc.replace(minute=minute, second=0, microsecond=0).isoformat()

    def _can_use_stale(self, request, entry: CacheEntry | None, now: datetime) -> bool:
        if entry is None or not request.allow_stale_on_error:
            return False
        if request.traffic_preference == TrafficPreference.TRAFFIC_AWARE_REQUIRED:
            return False
        return now <= entry.expires_at + timedelta(
            seconds=self.settings.stale_if_error_seconds
        )

    @staticmethod
    def _cached_route_response(entry, request_id, *, stale):
        response = TrafficRouteResponse.model_validate(entry.payload)
        route = response.route.model_copy(update={"request_id": request_id})
        baseline = (
            response.baseline_route.model_copy(update={"request_id": request_id})
            if response.baseline_route
            else None
        )
        return response.model_copy(
            update={
                "request_id": request_id,
                "cache_hit": True,
                "stale": stale,
                "degraded": response.degraded or stale,
                "selection_reason": (
                    "stale_cache_after_provider_failure"
                    if stale
                    else response.selection_reason
                ),
                "route": route,
                "baseline_route": baseline,
            }
        )

    @staticmethod
    def _cached_matrix_response(entry, request_id, *, stale):
        response = TrafficMatrixResponse.model_validate(entry.payload)
        matrix = response.matrix.model_copy(update={"request_id": request_id})
        baseline = (
            response.baseline_matrix.model_copy(update={"request_id": request_id})
            if response.baseline_matrix
            else None
        )
        return response.model_copy(
            update={
                "request_id": request_id,
                "cache_hit": True,
                "stale": stale,
                "degraded": response.degraded or stale,
                "selection_reason": (
                    "stale_cache_after_provider_failure"
                    if stale
                    else response.selection_reason
                ),
                "matrix": matrix,
                "baseline_matrix": baseline,
            }
        )

    @staticmethod
    def _fallback_reason(error: TrafficError | None) -> str:
        return type(error).__name__ if error else "here_unavailable"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("traffic service clock must be timezone-aware")
        return value.astimezone(UTC)
