from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.schemas import (
    AccessPointRecord,
    NexTripModel,
    RouteMatrixResult,
    RouteObservation,
    RoutingProvider,
    TransportMode,
    ValidationResult,
)


def _request_id() -> str:
    return f"traffic-{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TrafficPreference(StrEnum):
    """How strongly a caller needs time-dependent provider data."""

    TRAFFIC_AWARE_PREFERRED = "traffic_aware_preferred"
    TRAFFIC_AWARE_REQUIRED = "traffic_aware_required"
    FREE_FLOW = "free_flow"


class RecommendationObjective(StrEnum):
    """Deterministic objective used to rank viable transport modes."""

    BALANCED = "balanced"
    FASTEST = "fastest"


class RecommendationStatus(StrEnum):
    """Outcome of evaluating the requested transport candidates."""

    RECOMMENDED = "recommended"
    NO_ELIGIBLE_MODE = "no_eligible_mode"
    NO_ROUTE_AVAILABLE = "no_route_available"


class TransportOptionStatus(StrEnum):
    """Per-mode outcome retained for an explainable recommendation."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class TrafficRouteRequest(NexTripModel):
    request_id: str = Field(default_factory=_request_id, min_length=1)
    origin_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    mode: TransportMode = TransportMode.DRIVE
    departure_time: AwareDatetime = Field(default_factory=_now)
    traffic_preference: TrafficPreference = TrafficPreference.TRAFFIC_AWARE_PREFERRED
    provider_hint: RoutingProvider | None = None
    include_baseline: bool = True
    force_refresh: bool = False
    allow_stale_on_error: bool = True

    @model_validator(mode="after")
    def distinct_endpoints(self) -> TrafficRouteRequest:
        if self.origin_id == self.destination_id:
            raise ValueError("origin_id and destination_id must be different")
        if self.provider_hint not in {
            None,
            RoutingProvider.HERE,
            RoutingProvider.VALHALLA,
        }:
            raise ValueError("provider_hint must be here or valhalla")
        if (
            self.traffic_preference == TrafficPreference.FREE_FLOW
            and self.provider_hint == RoutingProvider.HERE
        ):
            raise ValueError(
                "provider_hint=here cannot guarantee a free_flow-only result"
            )
        return self


class TransportRecommendationRequest(NexTripModel):
    """Request bounded road-mode options for one origin/destination pair."""

    request_id: str = Field(default_factory=_request_id, min_length=1)
    origin_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    candidate_modes: list[TransportMode] = Field(
        default_factory=lambda: [
            TransportMode.WALK,
            TransportMode.BICYCLE,
            TransportMode.TWO_WHEELER,
            TransportMode.DRIVE,
        ],
        min_length=1,
        max_length=5,
    )
    objective: RecommendationObjective = RecommendationObjective.BALANCED
    departure_time: AwareDatetime = Field(default_factory=_now)
    motorized_traffic_preference: TrafficPreference = (
        TrafficPreference.TRAFFIC_AWARE_PREFERRED
    )
    max_walk_duration_seconds: int | None = Field(default=1_800, gt=0)
    max_bicycle_duration_seconds: int | None = Field(default=3_600, gt=0)
    max_two_wheeler_distance_meters: int | None = Field(default=80_000, gt=0)
    include_baseline: bool = False
    force_refresh: bool = False
    allow_stale_on_error: bool = True

    @model_validator(mode="after")
    def validate_recommendation_request(self) -> TransportRecommendationRequest:
        if self.origin_id == self.destination_id:
            raise ValueError("origin_id and destination_id must be different")
        if len(set(self.candidate_modes)) != len(self.candidate_modes):
            raise ValueError("candidate_modes cannot contain duplicates")
        return self


class TrafficMatrixRequest(NexTripModel):
    request_id: str = Field(default_factory=_request_id, min_length=1)
    origin_ids: list[str] = Field(min_length=1, max_length=10)
    destination_ids: list[str] = Field(min_length=1, max_length=10)
    mode: TransportMode = TransportMode.DRIVE
    departure_time: AwareDatetime = Field(default_factory=_now)
    traffic_preference: TrafficPreference = TrafficPreference.TRAFFIC_AWARE_PREFERRED
    provider_hint: RoutingProvider | None = None
    include_baseline: bool = True
    force_refresh: bool = False
    allow_stale_on_error: bool = True

    @model_validator(mode="after")
    def unique_endpoints(self) -> TrafficMatrixRequest:
        if len(set(self.origin_ids)) != len(self.origin_ids):
            raise ValueError("origin_ids cannot contain duplicates")
        if len(set(self.destination_ids)) != len(self.destination_ids):
            raise ValueError("destination_ids cannot contain duplicates")
        if self.provider_hint not in {
            None,
            RoutingProvider.HERE,
            RoutingProvider.VALHALLA,
        }:
            raise ValueError("provider_hint must be here or valhalla")
        if (
            self.traffic_preference == TrafficPreference.FREE_FLOW
            and self.provider_hint == RoutingProvider.HERE
        ):
            raise ValueError(
                "provider_hint=here cannot guarantee a free_flow-only result"
            )
        return self


class ProviderAttempt(NexTripModel):
    provider: RoutingProvider
    purpose: Literal["selected", "baseline", "fallback"]
    succeeded: bool
    duration_ms: int = Field(ge=0)
    error_code: str | None = None
    detail: str | None = None


class TrafficRouteResponse(NexTripModel):
    request_id: str
    cache_hit: bool = False
    stale: bool = False
    degraded: bool = False
    selection_reason: str
    origin: AccessPointRecord
    destination: AccessPointRecord
    route: RouteObservation
    baseline_route: RouteObservation | None = None
    validations: list[ValidationResult] = Field(default_factory=list)
    provider_attempts: list[ProviderAttempt] = Field(default_factory=list)


class TransportRecommendationOption(NexTripModel):
    """One evaluated mode, including failures and policy exclusions."""

    mode: TransportMode
    status: TransportOptionStatus
    route: TrafficRouteResponse | None = None
    distance_meters: int | None = Field(default=None, gt=0)
    duration_seconds: int | None = Field(default=None, gt=0)
    generalized_duration_seconds: int | None = Field(default=None, ge=0)
    rank: int | None = Field(default=None, ge=1)
    recommended: bool = False
    reason_codes: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_option(self) -> TransportRecommendationOption:
        routed = self.status in {
            TransportOptionStatus.ELIGIBLE,
            TransportOptionStatus.INELIGIBLE,
        }
        if routed and self.route is None:
            raise ValueError("eligible and ineligible options require a route")
        if routed and (
            self.distance_meters is None
            or self.duration_seconds is None
            or self.generalized_duration_seconds is None
        ):
            raise ValueError("routed options require distance and duration summaries")
        if not routed and self.route is not None:
            raise ValueError("failed and unsupported options cannot contain a route")
        if self.recommended and self.status != TransportOptionStatus.ELIGIBLE:
            raise ValueError("only an eligible option can be recommended")
        if self.rank is not None and self.status != TransportOptionStatus.ELIGIBLE:
            raise ValueError("only eligible options can have a rank")
        return self


class TransportRecommendationResponse(NexTripModel):
    """Ranked, explainable route options for an external itinerary model."""

    request_id: str
    origin: AccessPointRecord
    destination: AccessPointRecord
    objective: RecommendationObjective
    status: RecommendationStatus
    recommended_mode: TransportMode | None = None
    selection_reason: str
    options: list[TransportRecommendationOption] = Field(min_length=1)
    computed_at: AwareDatetime
    degraded: bool = False
    partial: bool = False

    @model_validator(mode="after")
    def validate_recommendation(self) -> TransportRecommendationResponse:
        if len({option.mode for option in self.options}) != len(self.options):
            raise ValueError("recommendation options must have unique modes")
        selected = [option for option in self.options if option.recommended]
        if self.status == RecommendationStatus.RECOMMENDED:
            if self.recommended_mode is None or len(selected) != 1:
                raise ValueError(
                    "recommended status requires exactly one selected mode"
                )
            if selected[0].mode != self.recommended_mode:
                raise ValueError("recommended_mode must match the selected option")
        elif self.recommended_mode is not None or selected:
            raise ValueError("non-recommended status cannot select a mode")
        return self


class TrafficMatrixResponse(NexTripModel):
    request_id: str
    cache_hit: bool = False
    stale: bool = False
    degraded: bool = False
    selection_reason: str
    origins: list[AccessPointRecord]
    destinations: list[AccessPointRecord]
    matrix: RouteMatrixResult
    baseline_matrix: RouteMatrixResult | None = None
    validations: list[ValidationResult] = Field(default_factory=list)
    provider_attempts: list[ProviderAttempt] = Field(default_factory=list)


class ProviderRuntimeStatus(NexTripModel):
    provider: RoutingProvider
    role: Literal["traffic", "baseline"]
    enabled: bool
    configured: bool
    reachable: bool | None = None
    detail: str | None = None


class AccessPointStats(NexTripModel):
    total: int
    places: int
    cities: int
    overrides: int
    skipped_files: int = 0
    invalid_files: int = 0


class TrafficHealthResponse(NexTripModel):
    status: Literal["ok", "degraded"]
    service: Literal["nextrip-traffic"] = "nextrip-traffic"
    access_points: AccessPointStats | None = None
    providers: list[ProviderRuntimeStatus] = Field(default_factory=list)


class PrewarmPair(NexTripModel):
    origin_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    modes: list[TransportMode] = Field(default_factory=lambda: [TransportMode.DRIVE])


class PrewarmPlan(NexTripModel):
    pairs: list[PrewarmPair] = Field(min_length=1, max_length=50)


class PrewarmResult(NexTripModel):
    started_at: AwareDatetime
    finished_at: AwareDatetime
    requested: int
    succeeded: int
    failed: int
    observation_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
