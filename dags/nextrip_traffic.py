from __future__ import annotations

import os
from datetime import timedelta


try:
    import pendulum

    try:
        from airflow.sdk import DAG
    except ImportError:
        from airflow import DAG

    try:
        from airflow.providers.standard.operators.bash import BashOperator
    except ImportError:
        from airflow.operators.bash import BashOperator
except ImportError:
    # Airflow stays optional for application processes and local unit tests.
    DAG = None


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _command_prefix(*, require_prewarm: bool = False) -> str:
    guards = [
        "case \"${NEXTRIP_TRAFFIC_AIRFLOW_ENABLED:-false}\" in "
        "1|true|TRUE|yes|YES|on|ON) ;; *) "
        'echo "traffic Airflow automation disabled"; exit 0 ;; esac',
    ]
    if require_prewarm:
        guards.append(
            "case \"${NEXTRIP_TRAFFIC_PREWARM_ENABLED:-false}\" in "
            "1|true|TRUE|yes|YES|on|ON) ;; *) "
            'echo "traffic prewarm disabled"; exit 0 ;; esac'
        )
    return (
        "set -euo pipefail; "
        + "; ".join(guards)
        + '; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
    )


def _provider_health_command(*, require_here: bool = False) -> str:
    command = "python -m nextrip_traffic.cli provider-health --require valhalla"
    if require_here:
        command += " --require here"
    return _command_prefix() + command


def _cleanup_cache_command() -> str:
    return _command_prefix() + "python -m nextrip_traffic.cli cleanup-cache"


def _prewarm_command() -> str:
    return _command_prefix(require_prewarm=True) + (
        "python -m nextrip_traffic.cli prewarm "
        '--config "${NEXTRIP_TRAFFIC_PREWARM_CONFIG:-config/traffic-prewarm.json}" '
        "--traffic-preference traffic_aware_preferred "
        "--include-baseline --force-refresh --no-allow-stale-on-error "
        "--fail-on-degraded"
    )


traffic_maintenance_dag = None
traffic_prewarm_dag = None

# Traffic route/matrix requests remain synchronous API calls. Airflow only
# performs bounded cache warming and operational maintenance.
if DAG is not None and _env_flag("NEXTRIP_TRAFFIC_AIRFLOW_ENABLED"):
    timezone = pendulum.timezone("Asia/Ho_Chi_Minh")
    common = {
        "owner": "nextrip-traffic",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
    }

    with DAG(
        dag_id="nextrip_traffic_maintenance",
        description="Provider health and expired traffic-cache cleanup",
        start_date=pendulum.datetime(2026, 8, 19, 0, 0, tz=timezone),
        schedule=os.getenv(
            "NEXTRIP_TRAFFIC_MAINTENANCE_SCHEDULE",
            "17 * * * *",
        ),
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "traffic", "maintenance"],
    ) as traffic_maintenance_dag:
        # Independent tasks ensure cache cleanup still runs if a provider is
        # unhealthy and the health task correctly marks the DAG run degraded.
        BashOperator(
            task_id="provider_health",
            bash_command=_provider_health_command(),
            execution_timeout=timedelta(minutes=3),
        )
        BashOperator(
            task_id="cleanup_expired_cache",
            bash_command=_cleanup_cache_command(),
            execution_timeout=timedelta(minutes=5),
        )

    if _env_flag("NEXTRIP_TRAFFIC_PREWARM_ENABLED"):
        with DAG(
            dag_id="nextrip_traffic_prewarm",
            description="Bounded prewarm for configured key traffic routes",
            start_date=pendulum.datetime(2026, 8, 19, 0, 0, tz=timezone),
            schedule=os.getenv(
                "NEXTRIP_TRAFFIC_PREWARM_SCHEDULE",
                "*/10 * * * *",
            ),
            catchup=False,
            max_active_runs=1,
            default_args=common,
            tags=["nextrip", "traffic", "prewarm", "here", "valhalla"],
        ) as traffic_prewarm_dag:
            prewarm_provider_health = BashOperator(
                task_id="provider_health",
                bash_command=_provider_health_command(require_here=True),
                execution_timeout=timedelta(minutes=3),
            )
            prewarm_routes = BashOperator(
                task_id="prewarm_routes",
                bash_command=_prewarm_command(),
                execution_timeout=timedelta(minutes=20),
                # A failed partial batch may already have consumed HERE quota;
                # the next bounded schedule is safer than retrying immediately.
                retries=0,
            )
            prewarm_provider_health >> prewarm_routes
