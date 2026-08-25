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
    # Airflow is optional for application processes and local unit tests.
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


def _publish_observations_command() -> str:
    """Build the bounded V8 observation-publish command.

    The scheduler-level gate prevents the DAG from being registered by default.
    The shell guard is deliberately repeated because worker environment variables
    can differ from the scheduler's environment. Missing or disabled gates skip
    successfully without contacting Neo4j.
    """

    return (
        "set -euo pipefail; "
        'case "${NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED:-false}" in '
        "1|true|TRUE|yes|YES|on|ON) ;; *) "
        'echo "Neo4j V8 observation publishing disabled"; exit 0 ;; esac; '
        ': "${NEO4J_V8_URI:?set NEO4J_V8_URI}"; '
        ': "${NEO4J_V8_USER:?set NEO4J_V8_USER}"; '
        ': "${NEO4J_V8_PASSWORD:?set NEO4J_V8_PASSWORD}"; '
        ': "${NEO4J_V8_DATABASE:?set NEO4J_V8_DATABASE}"; '
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
        "python -m nextrip_graphrag.observation_cli "
        '--canonical-dataset "$CANONICAL_DATASET" '
        '--hotel-price-root "${NEXTRIP_CURRENT_HOTEL_PRICE_ROOT:-data/current/hotel_price}" '
        '--hotel-availability-root "${NEXTRIP_CURRENT_HOTEL_AVAILABILITY_ROOT:-data/current/hotel_availability}" '
        '--menu-root "${NEXTRIP_CURRENT_MENU_ROOT:-data/current/menu}" '
        '--output-root "${NEXTRIP_NEO4J_V8_OBSERVATION_RUN_ROOT:-data/neo4j/v8/observation_runs}" '
        "--apply"
    )


neo4j_v8_observations_dag = None

# Static canonical releases are intentionally manual. This scheduled DAG only
# appends operational observations (price, availability, opening/place and menu)
# to an already-published V8 canonical release. Traffic remains request-time data.
if DAG is not None and _env_flag("NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED"):
    timezone = pendulum.timezone("Asia/Ho_Chi_Minh")
    common = {
        "owner": "nextrip-data",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    }

    with DAG(
        dag_id="nextrip_neo4j_v8_observations",
        description="Append current data observations to the isolated Neo4j V8 graph",
        start_date=pendulum.datetime(2026, 8, 23, 0, 0, tz=timezone),
        schedule=os.getenv(
            "NEXTRIP_NEO4J_V8_OBSERVATIONS_SCHEDULE",
            "*/15 * * * *",
        ),
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "neo4j", "v8", "observations"],
    ) as neo4j_v8_observations_dag:
        BashOperator(
            task_id="publish_current_observations",
            bash_command=_publish_observations_command(),
            execution_timeout=timedelta(minutes=30),
            # Do not read the current JSON roots while a scheduled crawler is
            # replacing a related price/availability snapshot.
            pool=os.getenv("NEXTRIP_CURRENT_DATA_POOL", "current_data_snapshot"),
        )
