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
    # Airflow is intentionally optional in developer/test environments. The module
    # becomes an active DAG only inside the Airflow scheduler image.
    DAG = None


def _batch_command(mode: str, default_limit: int) -> str:
    limit_variable = (
        "NEXTRIP_MAPS_DAILY_LIMIT" if mode == "place" else "NEXTRIP_MAPS_MENU_LIMIT"
    )
    return (
        'set -euo pipefail; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "python -m nextrip_pipeline.cli batch-google-maps "
        "--manifest config/google-maps-batch-manifest.json "
        f'--mode {mode} --max-requests "${{{limit_variable}:-{default_limit}}}"'
    )


def _registry_command() -> str:
    return (
        'set -euo pipefail; KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "python -m nextrip_pipeline.cli build-google-maps-registry "
        "--master-dir travel_data_verified "
        "--override config/google-maps-mapping.json "
        "--override config/google-maps-mapping-cafe-dn-062.json"
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
        dag_id="nextrip_google_maps_daily",
        description="Daily Maps opening/location/media refresh for bounded mappings",
        start_date=pendulum.datetime(2026, 8, 18, 2, 0, tz=timezone),
        schedule="0 2 * * *",
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "google-maps", "playwright"],
    ) as google_maps_daily_dag:
        build_google_maps_registry = BashOperator(
            task_id="build_google_maps_registry",
            bash_command=_registry_command(),
            execution_timeout=timedelta(minutes=10),
        )
        refresh_google_maps_places = BashOperator(
            task_id="refresh_google_maps_places",
            bash_command=_batch_command("place", 32),
            execution_timeout=timedelta(hours=2),
        )
        build_google_maps_registry >> refresh_google_maps_places

    with DAG(
        dag_id="nextrip_google_maps_menu",
        description="Manual-only menu OCR and human-review queue refresh",
        start_date=pendulum.datetime(2026, 8, 18, 3, 0, tz=timezone),
        # Menu verification/OCR is explicitly pending. Keeping the DAG visible
        # but unscheduled prevents Airflow from creating automatic menu runs.
        schedule=None,
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "google-maps", "menu", "ocr"],
    ) as google_maps_menu_dag:
        BashOperator(
            task_id="refresh_google_maps_menus",
            bash_command=_batch_command("menu", 16),
            execution_timeout=timedelta(hours=2),
        )
