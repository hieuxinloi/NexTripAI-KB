from __future__ import annotations

import json
from io import BytesIO, TextIOWrapper
from datetime import UTC, datetime
from types import SimpleNamespace

from nextrip_pipeline.schemas import RoutingProvider, TransportMode
from nextrip_traffic import cli


NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


class FakeRegistry:
    def list(self, *, access_type=None, owner_entity_id=None):
        return [
            {
                "access_point_id": "city:city_da_nang:center",
                "access_type": access_type.value if access_type else "city_center",
                "owner_entity_id": owner_entity_id or "city_da_nang",
            }
        ]

    def stats(self):
        return {"total_access_points": 1}


class FakeCache:
    def __init__(self) -> None:
        self.times = []

    def stats(self, *, now=None):
        self.times.append(now)
        return {"total_entries": 3}

    def delete_expired(self, *, now=None):
        self.times.append(now)
        return 2


class FakeService:
    def __init__(self) -> None:
        self.route_requests = []
        self.matrix_requests = []
        self.registry = FakeRegistry()
        self.cache = FakeCache()
        self.closed = False
        self.health_values = [
            {
                "provider": "valhalla",
                "configured": True,
                "available": True,
            },
            {
                "provider": "here",
                "configured": True,
                "available": None,
            },
        ]

    def route(self, request):
        self.route_requests.append(request)
        if request.request_id.startswith("prewarm:"):
            return SimpleNamespace(
                route=SimpleNamespace(
                    observation_id=f"obs-{request.mode.value}"
                ),
                degraded=False,
                selection_reason="valhalla_selected",
            )
        return {
            "request_id": request.request_id,
            "provider": (request.provider_hint or RoutingProvider.HERE).value,
        }

    def matrix(self, request):
        self.matrix_requests.append(request)
        return {
            "request_id": request.request_id,
            "cells": len(request.origin_ids) * len(request.destination_ids),
        }

    def provider_health(self):
        return self.health_values

    def close(self) -> None:
        self.closed = True


def install_service(monkeypatch, service: FakeService) -> None:
    monkeypatch.setattr(cli, "_build_service", lambda: service)


def test_default_invocation_and_explicit_serve_are_compatible(monkeypatch) -> None:
    calls = []

    def fake_serve(*, host, port, reload):
        calls.append((host, port, reload))
        return 0

    monkeypatch.setattr(cli, "_serve", fake_serve)

    assert cli.main([]) == 0
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9000", "--reload"]) == 0
    assert calls == [
        ("127.0.0.1", 8010, False),
        ("0.0.0.0", 9000, True),
    ]


def test_route_command_builds_request_and_outputs_json(
    monkeypatch,
    capsys,
) -> None:
    service = FakeService()
    install_service(monkeypatch, service)

    exit_code = cli.main(
        [
            "route",
            "city_quy_nhon",
            "city_da_nang",
            "--mode",
            "two_wheeler",
            "--departure-time",
            "2026-08-20T17:00:00+07:00",
            "--traffic-preference",
            "free_flow",
            "--provider",
            "valhalla",
            "--no-include-baseline",
            "--force-refresh",
            "--request-id",
            "cli-route-1",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "provider": "valhalla",
        "request_id": "cli-route-1",
    }
    request = service.route_requests[0]
    assert request.origin_id == "city_quy_nhon"
    assert request.destination_id == "city_da_nang"
    assert request.mode == TransportMode.TWO_WHEELER
    assert request.provider_hint == RoutingProvider.VALHALLA
    assert request.include_baseline is False
    assert request.force_refresh is True
    assert request.departure_time.utcoffset().total_seconds() == 7 * 3600
    assert service.closed is True


def test_matrix_command_accepts_multiple_endpoints(monkeypatch, capsys) -> None:
    service = FakeService()
    install_service(monkeypatch, service)

    exit_code = cli.main(
        [
            "matrix",
            "--origins",
            "origin-1",
            "origin-2",
            "--destinations",
            "target-1",
            "target-2",
            "--request-id",
            "cli-matrix-1",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["cells"] == 4
    assert service.matrix_requests[0].origin_ids == ["origin-1", "origin-2"]
    assert service.matrix_requests[0].destination_ids == ["target-1", "target-2"]
    assert service.closed is True


def test_provider_health_required_valhalla_passes(monkeypatch, capsys) -> None:
    service = FakeService()
    install_service(monkeypatch, service)

    exit_code = cli.main(["provider-health", "--require", "valhalla"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["healthy"] is True
    assert payload["requirements"] == [
        {
            "provider": "valhalla",
            "satisfied": True,
            "status": "available",
        }
    ]
    assert service.closed is True


def test_provider_health_allows_configured_unprobed_here(
    monkeypatch,
    capsys,
) -> None:
    service = FakeService()
    install_service(monkeypatch, service)

    exit_code = cli.main(["provider-health", "--require", "here"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["requirements"][0]["status"] == "configured_unprobed"
    assert payload["requirements"][0]["satisfied"] is True


def test_provider_health_fails_missing_or_unavailable_requirement(
    monkeypatch,
    capsys,
) -> None:
    service = FakeService()
    service.health_values = [
        {
            "provider": "valhalla",
            "configured": True,
            "available": False,
        }
    ]
    install_service(monkeypatch, service)

    exit_code = cli.main(
        [
            "provider-health",
            "--require",
            "valhalla",
            "--require",
            "here",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["healthy"] is False
    assert payload["requirements"] == [
        {
            "provider": "valhalla",
            "satisfied": False,
            "status": "unavailable",
        },
        {
            "provider": "here",
            "satisfied": False,
            "status": "missing",
        },
    ]


def test_list_access_points_and_cleanup_cache(monkeypatch, capsys) -> None:
    list_service = FakeService()
    install_service(monkeypatch, list_service)
    assert (
        cli.main(
            [
                "list-access-points",
                "--access-type",
                "city_center",
                "--owner-entity-id",
                "city_da_nang",
            ]
        )
        == 0
    )
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["count"] == 1
    assert list_payload["access_points"][0]["access_type"] == "city_center"

    cleanup_service = FakeService()
    install_service(monkeypatch, cleanup_service)
    assert (
        cli.main(
            ["cleanup-cache", "--now", "2026-08-20T10:00:00Z"]
        )
        == 0
    )
    cleanup_payload = json.loads(capsys.readouterr().out)
    assert cleanup_payload["deleted_entries"] == 2
    assert cleanup_service.cache.times == [NOW, NOW, NOW]


def test_prewarm_command_loads_plan_and_outputs_result(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    config = tmp_path / "prewarm.json"
    config.write_text(
        '{"pairs":[{"origin_id":"city_quy_nhon",'
        '"destination_id":"city_da_nang","modes":["drive"]}]}',
        encoding="utf-8",
    )
    service = FakeService()
    install_service(monkeypatch, service)

    exit_code = cli.main(
        [
            "prewarm",
            "--config",
            str(config),
            "--departure-time",
            "2026-08-20T17:00:00+07:00",
            "--provider",
            "valhalla",
            "--traffic-preference",
            "free_flow",
            "--fail-on-degraded",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["requested"] == 1
    assert payload["succeeded"] == 1
    assert payload["failed"] == 0
    assert payload["observation_ids"] == ["obs-drive"]
    request = service.route_requests[0]
    assert request.provider_hint == RoutingProvider.VALHALLA
    assert request.force_refresh is True
    assert request.allow_stale_on_error is False
    assert service.closed is True


def test_command_error_is_json_and_service_is_closed(monkeypatch, capsys) -> None:
    service = FakeService()

    def fail(_request):
        raise RuntimeError("routing failed")

    service.route = fail  # type: ignore[method-assign]
    install_service(monkeypatch, service)

    exit_code = cli.main(["route", "origin", "destination"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "error",
        "error_type": "RuntimeError",
        "message": "routing failed",
    }
    assert service.closed is True


def test_json_output_falls_back_on_legacy_windows_encoding() -> None:
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="cp1252")

    cli._write_json({"name": "Quy Nhơn"}, stream=stream)
    stream.flush()

    output = buffer.getvalue().decode("cp1252")
    assert json.loads(output) == {"name": "Quy Nhơn"}
    assert "\\u01a1" in output
