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
    # Airflow is optional in developer/test environments. The module only
    # creates a DAG when loaded inside an Airflow scheduler image.
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


def _command_prefix() -> str:
    return (
        "set -euo pipefail; "
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        'POINTER_PATH="${NEXTRIP_CANONICAL_DATASET_POINTER:-data/canonical/'
        'active-dataset-pointer.json}"; '
        'if [ -f "$POINTER_PATH" ]; then '
        'RESOLVE_OUTPUT="$(python -m nextrip_pipeline.cli '
        'resolve-active-canonical-dataset --pointer "$POINTER_PATH")"; '
        'CANONICAL_DATASET="$(printf "%s\\n" "$RESOLVE_OUTPUT" '
        "| sed -n 's/^dataset=//p' | tail -n 1)\"; "
        'elif [ -n "${NEXTRIP_CANONICAL_DATASET:-}" ]; then '
        'CANONICAL_DATASET="$NEXTRIP_CANONICAL_DATASET"; '
        'else echo "No active canonical dataset pointer or legacy dataset is '
        'configured" >&2; exit 2; fi; '
        'test -n "$CANONICAL_DATASET"; '
    )


def _registry_command() -> str:
    return _command_prefix() + (
        "python -m nextrip_pipeline.cli build-trivago-registry "
        '--canonical-dataset "$CANONICAL_DATASET" '
        "--override config/trivago-mapping.json "
        "--current-mapping-dir "
        '"${NEXTRIP_CURRENT_TRIVAGO_MAPPING_ROOT:-data/current/trivago_mappings}" '
        "--output config/generated/trivago-hotel-registry.json "
        "--report config/generated/trivago-registry-report.json"
    )


def _batch_command() -> str:
    return (
        'set -euo pipefail; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "MAX_REQUEST_ARGS=(); "
        'if [ -n "${NEXTRIP_TRIVAGO_MAX_REQUESTS:-}" ]; then '
        'MAX_REQUEST_ARGS=(--max-requests "$NEXTRIP_TRIVAGO_MAX_REQUESTS"); fi; '
        'CHECK_IN_OFFSET_SPEC="${NEXTRIP_HOTEL_CHECK_IN_OFFSETS:-}"; '
        'if [ -z "$CHECK_IN_OFFSET_SPEC" ]; then '
        'CHECK_IN_OFFSET_SPEC="${NEXTRIP_HOTEL_CHECK_IN_OFFSET_DAYS:-0,1}"; fi; '
        'IFS="," read -r -a CHECK_IN_OFFSETS <<< "$CHECK_IN_OFFSET_SPEC"; '
        'for CHECK_IN_OFFSET in "${CHECK_IN_OFFSETS[@]}"; do '
        'CHECK_IN_OFFSET="${CHECK_IN_OFFSET//[[:space:]]/}"; '
        'test -n "$CHECK_IN_OFFSET"; '
        "python -m nextrip_pipeline.cli batch-trivago-availability "
        "--registry config/generated/trivago-hotel-registry.json "
        '"--check-in-offset-days" "$CHECK_IN_OFFSET" '
        '"--stay-nights" "${NEXTRIP_HOTEL_STAY_NIGHTS:-1}" '
        '"--lookahead-days" "${NEXTRIP_HOTEL_LOOKAHEAD_DAYS:-1}" '
        '"--adults" "${NEXTRIP_HOTEL_ADULTS:-2}" '
        '"--rooms" "${NEXTRIP_HOTEL_ROOMS:-1}" '
        '"--currency" "${NEXTRIP_HOTEL_CURRENCY:-VND}" '
        '--raw-dir "${NEXTRIP_RAW_ROOT:-data/raw}" '
        '--normalized-dir "${NEXTRIP_NORMALIZED_ROOT:-data/normalized}" '
        "--accepted-observation-dir "
        '"${NEXTRIP_ACCEPTED_OBSERVATION_ROOT:-data/observations}" '
        '--quality-dir "${NEXTRIP_TRIVAGO_QUALITY_ROOT:-data/quality/trivago_mapping}" '
        '--validation-dir "${NEXTRIP_VALIDATION_ROOT:-data/validation}" '
        '--decision-dir "${NEXTRIP_DECISION_ROOT:-data/decisions}" '
        "--current-mapping-dir "
        '"${NEXTRIP_CURRENT_TRIVAGO_MAPPING_ROOT:-data/current/trivago_mappings}" '
        "--current-price-dir "
        '"${NEXTRIP_CURRENT_HOTEL_PRICE_ROOT:-data/current/hotel_price}" '
        "--current-availability-dir "
        '"${NEXTRIP_CURRENT_HOTEL_AVAILABILITY_ROOT:-data/current/hotel_availability}" '
        '--stay-result-dir "${NEXTRIP_TRIVAGO_STAY_RESULT_ROOT:-data/runs/trivago_stay}" '
        "--summary-dir "
        '"${NEXTRIP_TRIVAGO_BATCH_SUMMARY_ROOT:-data/runs/trivago_availability_batch}" '
        '"${MAX_REQUEST_ARGS[@]}"; '
        "done"
    )


hotel_prices_dag = None

if DAG is not None and _env_flag("NEXTRIP_HOTEL_PRICES_AIRFLOW_ENABLED"):
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
            # The batch replaces current price and availability files. Keep that
            # write window mutually exclusive with the Neo4j snapshot reader.
            pool=os.getenv("NEXTRIP_CURRENT_DATA_POOL", "current_data_snapshot"),
        )
        build_trivago_registry >> refresh_trivago_availability
