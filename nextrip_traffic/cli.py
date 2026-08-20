from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nextrip_pipeline.schemas import AccessPointType, RoutingProvider, TransportMode

from .errors import TrafficError
from .models import (
    RecommendationObjective,
    RecommendationStatus,
    TrafficMatrixRequest,
    TrafficPreference,
    TrafficRouteRequest,
    TransportRecommendationRequest,
)
from .prewarm import load_prewarm_plan, run_prewarm


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8010
DEFAULT_PREWARM_CONFIG = Path("config/traffic-prewarm.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nextrip-traffic",
        description="NexTrip hybrid Valhalla/HERE traffic service.",
    )
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="Run the traffic HTTP API.")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--reload", action="store_true")

    route = commands.add_parser("route", help="Compute and cache one route.")
    route.add_argument("origin_id")
    route.add_argument("destination_id")
    _add_routing_arguments(route)

    recommend = commands.add_parser(
        "recommend-transport",
        help="Compare supported road modes and recommend one option.",
    )
    recommend.add_argument("origin_id")
    recommend.add_argument("destination_id")
    recommend.add_argument(
        "--modes",
        nargs="+",
        type=TransportMode,
        choices=list(TransportMode),
        help="Candidate modes; defaults to all supported road modes.",
    )
    recommend.add_argument(
        "--objective",
        type=RecommendationObjective,
        choices=list(RecommendationObjective),
        default=RecommendationObjective.BALANCED,
    )
    recommend.add_argument(
        "--departure-time",
        type=_aware_datetime,
        help="ISO-8601 departure time; defaults to now.",
    )
    recommend.add_argument(
        "--motorized-traffic-preference",
        type=TrafficPreference,
        choices=list(TrafficPreference),
        default=TrafficPreference.TRAFFIC_AWARE_PREFERRED,
    )
    recommend.add_argument("--max-walk-minutes", type=_positive_float, default=30)
    recommend.add_argument(
        "--max-bicycle-minutes",
        type=_positive_float,
        default=60,
    )
    recommend.add_argument(
        "--max-two-wheeler-km",
        type=_positive_float,
        default=80,
    )
    recommend.add_argument(
        "--include-baseline",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    recommend.add_argument("--force-refresh", action="store_true")
    recommend.add_argument(
        "--allow-stale-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    recommend.add_argument("--request-id")

    matrix = commands.add_parser(
        "matrix",
        help="Compute and cache a bounded route matrix.",
    )
    matrix.add_argument("--origins", nargs="+", required=True)
    matrix.add_argument("--destinations", nargs="+", required=True)
    _add_routing_arguments(matrix)

    provider_health = commands.add_parser(
        "provider-health",
        help="Check configured Valhalla and HERE providers.",
    )
    provider_health.add_argument(
        "--require",
        action="append",
        type=RoutingProvider,
        choices=[RoutingProvider.VALHALLA, RoutingProvider.HERE],
        default=[],
        help=(
            "Fail unless this provider is configured and not unavailable; "
            "repeat for multiple providers."
        ),
    )

    access_points = commands.add_parser(
        "list-access-points",
        help="List canonical endpoints derived from current place data.",
    )
    access_points.add_argument(
        "--access-type",
        type=AccessPointType,
        choices=list(AccessPointType),
    )
    access_points.add_argument("--owner-entity-id")

    cleanup = commands.add_parser(
        "cleanup-cache",
        help="Delete expired route and matrix cache entries.",
    )
    cleanup.add_argument(
        "--now",
        type=_aware_datetime,
        help="Optional ISO-8601 cleanup time; defaults to current UTC time.",
    )

    prewarm = commands.add_parser(
        "prewarm",
        help="Populate cache entries from a bounded JSON plan.",
    )
    prewarm.add_argument("--config", type=Path, default=DEFAULT_PREWARM_CONFIG)
    prewarm.add_argument(
        "--departure-time",
        type=_aware_datetime,
        help="ISO-8601 departure time shared by all planned routes.",
    )
    prewarm.add_argument(
        "--traffic-preference",
        type=TrafficPreference,
        choices=list(TrafficPreference),
        default=TrafficPreference.TRAFFIC_AWARE_PREFERRED,
    )
    prewarm.add_argument(
        "--provider",
        dest="provider_hint",
        type=RoutingProvider,
        choices=[RoutingProvider.VALHALLA, RoutingProvider.HERE],
    )
    prewarm.add_argument(
        "--include-baseline",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    prewarm.add_argument(
        "--force-refresh",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    prewarm.add_argument(
        "--allow-stale-on-error",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    prewarm.add_argument(
        "--fail-on-degraded",
        action="store_true",
        help="Return a failed prewarm result when only a provider fallback is available.",
    )

    return parser


def _add_routing_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        type=TransportMode,
        choices=list(TransportMode),
        default=TransportMode.DRIVE,
    )
    parser.add_argument(
        "--departure-time",
        type=_aware_datetime,
        help="ISO-8601 departure time; defaults to now.",
    )
    parser.add_argument(
        "--traffic-preference",
        type=TrafficPreference,
        choices=list(TrafficPreference),
        default=TrafficPreference.TRAFFIC_AWARE_PREFERRED,
    )
    parser.add_argument(
        "--provider",
        dest="provider_hint",
        type=RoutingProvider,
        choices=[RoutingProvider.VALHALLA, RoutingProvider.HERE],
    )
    parser.add_argument(
        "--include-baseline",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--allow-stale-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--request-id")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command is None:
        return _serve(host=DEFAULT_HOST, port=DEFAULT_PORT, reload=False)
    if args.command == "serve":
        return _serve(host=args.host, port=args.port, reload=args.reload)

    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "route": _route,
        "recommend-transport": _recommend_transport,
        "matrix": _matrix,
        "provider-health": _provider_health,
        "list-access-points": _list_access_points,
        "cleanup-cache": _cleanup_cache,
        "prewarm": _prewarm,
    }
    try:
        return handlers[args.command](args)
    except (OSError, RuntimeError, TrafficError, ValidationError, ValueError) as error:
        _write_json(
            {
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            stream=sys.stderr,
        )
        return 2


def _serve(*, host: str, port: int, reload: bool) -> int:
    import uvicorn

    uvicorn.run(
        "nextrip_traffic.api:app",
        host=host,
        port=port,
        reload=reload,
    )
    return 0


def _route(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "origin_id": args.origin_id,
        "destination_id": args.destination_id,
        **_routing_payload(args),
    }
    request = TrafficRouteRequest.model_validate(payload)

    service = _build_service()
    try:
        response = service.route(request)
        _write_json(response)
    finally:
        service.close()
    return 0


def _matrix(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "origin_ids": args.origins,
        "destination_ids": args.destinations,
        **_routing_payload(args),
    }
    request = TrafficMatrixRequest.model_validate(payload)

    service = _build_service()
    try:
        response = service.matrix(request)
        _write_json(response)
    finally:
        service.close()
    return 0


def _recommend_transport(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {
        "origin_id": args.origin_id,
        "destination_id": args.destination_id,
        "objective": args.objective,
        "motorized_traffic_preference": args.motorized_traffic_preference,
        "max_walk_duration_seconds": round(args.max_walk_minutes * 60),
        "max_bicycle_duration_seconds": round(args.max_bicycle_minutes * 60),
        "max_two_wheeler_distance_meters": round(args.max_two_wheeler_km * 1_000),
        "include_baseline": args.include_baseline,
        "force_refresh": args.force_refresh,
        "allow_stale_on_error": args.allow_stale_on_error,
    }
    if args.modes is not None:
        payload["candidate_modes"] = args.modes
    if args.departure_time is not None:
        payload["departure_time"] = args.departure_time
    if args.request_id:
        payload["request_id"] = args.request_id
    request = TransportRecommendationRequest.model_validate(payload)

    service = _build_service()
    try:
        response = service.recommend_transport(request)
        _write_json(response)
    finally:
        service.close()
    return 0 if response.status == RecommendationStatus.RECOMMENDED else 1


def _routing_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": args.mode,
        "traffic_preference": args.traffic_preference,
        "provider_hint": args.provider_hint,
        "include_baseline": args.include_baseline,
        "force_refresh": args.force_refresh,
        "allow_stale_on_error": args.allow_stale_on_error,
    }
    if args.departure_time is not None:
        payload["departure_time"] = args.departure_time
    if args.request_id:
        payload["request_id"] = args.request_id
    return payload


def _provider_health(args: argparse.Namespace) -> int:
    service = _build_service()
    try:
        providers = service.provider_health()
        requirements = [
            _provider_requirement(provider, providers)
            for provider in dict.fromkeys(args.require)
        ]
        healthy = all(item["satisfied"] for item in requirements)
        _write_json(
            {
                "healthy": healthy,
                "providers": providers,
                "requirements": requirements,
            }
        )
    finally:
        service.close()
    return 0 if healthy else 1


def _provider_requirement(
    required: RoutingProvider,
    providers: list[object],
) -> dict[str, object]:
    health = next(
        (
            item
            for item in providers
            if _health_value(item, "provider") in {required, required.value}
        ),
        None,
    )
    if health is None:
        return {
            "provider": required.value,
            "satisfied": False,
            "status": "missing",
        }

    configured = _health_value(health, "configured") is True
    available = _health_value(health, "available")
    if not configured:
        status = "not_configured"
        satisfied = False
    elif available is False:
        status = "unavailable"
        satisfied = False
    elif available is None:
        # HERE intentionally does not spend routing quota on health probes.
        status = "configured_unprobed"
        satisfied = True
    else:
        status = "available"
        satisfied = True
    return {
        "provider": required.value,
        "satisfied": satisfied,
        "status": status,
    }


def _health_value(value: object, field: str) -> object:
    if isinstance(value, dict):
        return value.get(field)
    return getattr(value, field, None)


def _list_access_points(args: argparse.Namespace) -> int:
    service = _build_service()
    try:
        records = service.registry.list(
            access_type=args.access_type,
            owner_entity_id=args.owner_entity_id,
        )
        _write_json(
            {
                "count": len(records),
                "stats": service.registry.stats(),
                "access_points": records,
            }
        )
    finally:
        service.close()
    return 0


def _cleanup_cache(args: argparse.Namespace) -> int:
    service = _build_service()
    try:
        before = service.cache.stats(now=args.now)
        deleted = service.cache.delete_expired(now=args.now)
        after = service.cache.stats(now=args.now)
        _write_json(
            {
                "deleted_entries": deleted,
                "before": before,
                "after": after,
            }
        )
    finally:
        service.close()
    return 0


def _prewarm(args: argparse.Namespace) -> int:
    plan = load_prewarm_plan(args.config)
    service = _build_service()
    try:
        result = run_prewarm(
            service,
            plan,
            departure_time=args.departure_time,
            traffic_preference=args.traffic_preference,
            provider_hint=args.provider_hint,
            include_baseline=args.include_baseline,
            force_refresh=args.force_refresh,
            allow_stale_on_error=args.allow_stale_on_error,
            fail_on_degraded=args.fail_on_degraded,
        )
        _write_json(result)
    finally:
        service.close()
    return 1 if result.failed else 0


def _build_service():
    # Runtime construction loads place JSON, opens SQLite and creates provider
    # clients. Keeping the import here makes parser/help/serve imports inert.
    from .runtime import build_traffic_service

    return build_traffic_service()


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 datetime with timezone"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _write_json(value: Any, *, stream=None) -> None:
    target = stream or sys.stdout
    serialized = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    try:
        print(serialized, file=target)
    except UnicodeEncodeError:
        # Some Windows PowerShell hosts expose a legacy cp1252 stream. Keep
        # commands usable there even when a caller supplied a non-reconfigurable
        # stream by falling back to escaped, still-valid JSON.
        print(
            json.dumps(
                value,
                default=_json_default,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=target,
        )


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (LookupError, OSError):
            # `_write_json` retains an ASCII-safe fallback for legacy hosts.
            continue


def _json_default(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    raise SystemExit(main())
