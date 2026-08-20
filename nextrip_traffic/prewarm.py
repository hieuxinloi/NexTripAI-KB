from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from nextrip_pipeline.schemas import RoutingProvider

from .models import (
    PrewarmPlan,
    PrewarmResult,
    TrafficPreference,
    TrafficRouteRequest,
    TrafficRouteResponse,
)


class RouteService(Protocol):
    def route(self, request: TrafficRouteRequest) -> TrafficRouteResponse: ...


def load_prewarm_plan(path: str | Path) -> PrewarmPlan:
    """Load and validate a bounded prewarm plan from JSON."""

    config_path = Path(path)
    return PrewarmPlan.model_validate_json(config_path.read_text(encoding="utf-8-sig"))


def run_prewarm(
    service: RouteService,
    plan: PrewarmPlan,
    *,
    departure_time: datetime | None = None,
    traffic_preference: TrafficPreference = (TrafficPreference.TRAFFIC_AWARE_PREFERRED),
    provider_hint: RoutingProvider | None = None,
    include_baseline: bool = True,
    force_refresh: bool = True,
    allow_stale_on_error: bool = False,
    fail_on_degraded: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> PrewarmResult:
    """Populate route cache entries while isolating failures per route/mode."""

    get_now = clock or (lambda: datetime.now(UTC))
    started_at = _aware(get_now(), field_name="prewarm start time")
    effective_departure = _aware(
        departure_time or started_at,
        field_name="departure_time",
    )
    requested = 0
    succeeded = 0
    errors: list[str] = []
    observation_ids: list[str] = []

    for pair in plan.pairs:
        for mode in pair.modes:
            requested += 1
            try:
                response = service.route(
                    TrafficRouteRequest(
                        request_id=(
                            f"prewarm:{started_at.strftime('%Y%m%dT%H%M%SZ')}:"
                            f"{requested}"
                        ),
                        origin_id=pair.origin_id,
                        destination_id=pair.destination_id,
                        mode=mode,
                        departure_time=effective_departure,
                        traffic_preference=traffic_preference,
                        provider_hint=provider_hint,
                        include_baseline=include_baseline,
                        force_refresh=force_refresh,
                        allow_stale_on_error=allow_stale_on_error,
                    )
                )
            except Exception as error:
                errors.append(
                    f"{pair.origin_id}->{pair.destination_id}/{mode.value}: "
                    f"{type(error).__name__}: {error}"
                )
                continue

            if fail_on_degraded and response.degraded:
                errors.append(
                    f"{pair.origin_id}->{pair.destination_id}/{mode.value}: "
                    f"degraded result: {response.selection_reason}"
                )
                continue

            succeeded += 1
            observation_ids.append(response.route.observation_id)

    finished_at = _aware(get_now(), field_name="prewarm finish time")
    return PrewarmResult(
        started_at=started_at,
        finished_at=finished_at,
        requested=requested,
        succeeded=succeeded,
        failed=requested - succeeded,
        observation_ids=observation_ids,
        errors=errors,
    )


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value
