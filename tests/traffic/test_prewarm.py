from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nextrip_pipeline.schemas import RoutingProvider, TransportMode
from nextrip_traffic.models import PrewarmPair, PrewarmPlan, TrafficPreference
from nextrip_traffic.prewarm import load_prewarm_plan, run_prewarm


NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


class FakeService:
    def __init__(
        self,
        *,
        fail_modes: set[TransportMode] | None = None,
        degraded_modes: set[TransportMode] | None = None,
    ) -> None:
        self.fail_modes = fail_modes or set()
        self.degraded_modes = degraded_modes or set()
        self.requests = []

    def route(self, request):
        self.requests.append(request)
        if request.mode in self.fail_modes:
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(
            route=SimpleNamespace(observation_id=f"obs-{request.mode.value}"),
            degraded=request.mode in self.degraded_modes,
            selection_reason=(
                "valhalla_free_flow_fallback"
                if request.mode in self.degraded_modes
                else "here_traffic_aware_selected"
            ),
        )


def test_run_prewarm_isolates_failures_and_forwards_policy() -> None:
    service = FakeService(fail_modes={TransportMode.WALK})
    plan = PrewarmPlan(
        pairs=[
            PrewarmPair(
                origin_id="city_quy_nhon",
                destination_id="city_da_nang",
                modes=[TransportMode.DRIVE, TransportMode.WALK],
            )
        ]
    )
    times = iter([NOW, NOW + timedelta(seconds=2)])

    result = run_prewarm(
        service,
        plan,
        departure_time=NOW + timedelta(hours=1),
        traffic_preference=TrafficPreference.FREE_FLOW,
        provider_hint=RoutingProvider.VALHALLA,
        include_baseline=False,
        force_refresh=True,
        allow_stale_on_error=False,
        clock=lambda: next(times),
    )

    assert result.requested == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.observation_ids == ["obs-drive"]
    assert "city_quy_nhon->city_da_nang/walk" in result.errors[0]
    assert "RuntimeError: provider unavailable" in result.errors[0]
    assert result.started_at == NOW
    assert result.finished_at == NOW + timedelta(seconds=2)
    assert len(service.requests) == 2
    request = service.requests[0]
    assert request.departure_time == NOW + timedelta(hours=1)
    assert request.traffic_preference == TrafficPreference.FREE_FLOW
    assert request.provider_hint == RoutingProvider.VALHALLA
    assert request.include_baseline is False
    assert request.force_refresh is True
    assert request.allow_stale_on_error is False


def test_load_prewarm_plan_validates_json(tmp_path) -> None:
    path = tmp_path / "prewarm.json"
    path.write_text(
        """
        {
          "pairs": [
            {
              "origin_id": "city_quy_nhon",
              "destination_id": "city_da_nang",
              "modes": ["drive", "two_wheeler"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    plan = load_prewarm_plan(path)

    assert plan.pairs[0].modes == [
        TransportMode.DRIVE,
        TransportMode.TWO_WHEELER,
    ]


def test_load_prewarm_plan_rejects_invalid_mode(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        '{"pairs":[{"origin_id":"a","destination_id":"b",'
        '"modes":["helicopter"]}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_prewarm_plan(path)


def test_run_prewarm_rejects_naive_clock() -> None:
    plan = PrewarmPlan(
        pairs=[PrewarmPair(origin_id="a", destination_id="b")]
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        run_prewarm(
            FakeService(),
            plan,
            clock=lambda: datetime(2026, 8, 20, 10, 0),
        )


def test_run_prewarm_can_fail_quality_gate_on_degraded_fallback() -> None:
    plan = PrewarmPlan(
        pairs=[PrewarmPair(origin_id="a", destination_id="b")]
    )
    times = iter([NOW, NOW + timedelta(seconds=1)])

    result = run_prewarm(
        FakeService(degraded_modes={TransportMode.DRIVE}),
        plan,
        fail_on_degraded=True,
        clock=lambda: next(times),
    )

    assert result.requested == 1
    assert result.succeeded == 0
    assert result.failed == 1
    assert result.observation_ids == []
    assert "degraded result: valhalla_free_flow_fallback" in result.errors[0]
