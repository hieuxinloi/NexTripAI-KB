from __future__ import annotations

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
    # Airflow is optional in developer/test environments. The module only
    # creates a DAG when loaded inside an Airflow scheduler image.
    DAG = None


def _registry_command() -> str:
    return (
        'set -euo pipefail; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "python -m nextrip_pipeline.cli build-trivago-registry "
        "--master-file travel_data_verified/hotel_final.json "
        "--override config/trivago-mapping.json "
        "--current-mapping-dir data/current/trivago_mappings "
        "--output config/generated/trivago-hotel-registry.json "
        "--report config/generated/trivago-registry-report.json"
    )


def _batch_command() -> str:
    return (
        'set -euo pipefail; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "python -m nextrip_pipeline.cli batch-trivago-availability "
        "--registry config/generated/trivago-hotel-registry.json "
        '"--check-in-offset-days" "${NEXTRIP_HOTEL_CHECK_IN_OFFSET_DAYS:-1}" '
        '"--stay-nights" "${NEXTRIP_HOTEL_STAY_NIGHTS:-1}" '
        '"--lookahead-days" "${NEXTRIP_HOTEL_LOOKAHEAD_DAYS:-1}" '
        '"--adults" "${NEXTRIP_HOTEL_ADULTS:-2}" '
        '"--rooms" "${NEXTRIP_HOTEL_ROOMS:-1}" '
        '"--currency" "${NEXTRIP_HOTEL_CURRENCY:-VND}" '
        '"--max-requests" "${NEXTRIP_TRIVAGO_MAX_REQUESTS:-73}"'
    )


if DAG is not None:
    common = {
        "owner": "nextrip-data",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    }
    timezone = pendulum.timezone("Asia/Ho_Chi_Minh")

    with DAG(
        dag_id="nextrip_hotel_prices_5h",
        description="Five-hour Trivago stay availability and price refresh",
        start_date=pendulum.datetime(2026, 8, 18, 0, 0, tz=timezone),
        schedule=timedelta(hours=5),
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "hotel-price", "trivago", "mcp"],
    ) as hotel_prices_dag:
        build_trivago_registry = BashOperator(
            task_id="build_trivago_registry",
            bash_command=_registry_command(),
            execution_timeout=timedelta(minutes=10),
        )
        refresh_trivago_availability = BashOperator(
            task_id="refresh_trivago_availability",
            bash_command=_batch_command(),
            execution_timeout=timedelta(hours=2),
        )
        build_trivago_registry >> refresh_trivago_availability
