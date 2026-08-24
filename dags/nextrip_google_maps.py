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


CANONICAL_GOOGLE_MAPS_MANIFEST = (
    "config/generated/canonical-google-maps-batch-manifest.json"
)
CANONICAL_GOOGLE_MAPS_ENTITY_TYPES = (
    "attraction",
    "cafe",
    "nightlife",
    "restaurant",
)


def _entity_type_arguments() -> str:
    return " ".join(
        f"--entity-type {entity_type}"
        for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES
    )


def _place_task_id(entity_type: str) -> str:
    if entity_type not in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES:
        raise ValueError(f"unsupported canonical Google Maps type: {entity_type}")
    return f"refresh_google_maps_{entity_type}"


def _registry_command() -> str:
    """Build a derived Maps registry from the pinned canonical snapshot only."""

    return (
        "set -euo pipefail; "
        ': "${NEXTRIP_CANONICAL_DATASET:?set NEXTRIP_CANONICAL_DATASET}"; '
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        "python -m nextrip_pipeline.cli build-google-maps-registry "
        '--canonical-dataset "$NEXTRIP_CANONICAL_DATASET" '
        "--output config/generated/canonical-google-maps-mapping-registry.json "
        "--report config/generated/canonical-google-maps-registry-report.json "
        f"--batch-manifest-output {CANONICAL_GOOGLE_MAPS_MANIFEST}"
    )


def _place_batch_command(entity_type: str, default_limit: int = 10_000) -> str:
    """Run one entity profile with an attempt-unique caller-owned run ID."""

    _place_task_id(entity_type)

    return (
        "set -euo pipefail; "
        ': "${NEXTRIP_CANONICAL_DATASET:?set NEXTRIP_CANONICAL_DATASET}"; '
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        'SOURCE_RUN_ID="google-maps-place-{{ run_id }}-'
        f'{entity_type}'
        '-try{{ ti.try_number }}"; '
        'MAPS_SCRATCH="${NEXTRIP_MAPS_SCRATCH_ROOT:-/tmp/nextrip-google-maps}/'
        '$SOURCE_RUN_ID"; '
        'NORMALIZED_ROOT="${NEXTRIP_MAPS_NORMALIZED_ROOT:-data/normalized}"; '
        'DECISION_ROOT="${NEXTRIP_MAPS_DECISION_ROOT:-data/decisions}"; '
        'BATCH_OUTPUT="$(python -m nextrip_pipeline.cli batch-google-maps '
        f"--manifest {CANONICAL_GOOGLE_MAPS_MANIFEST} "
        "--mode place "
        f"--entity-type {entity_type} "
        '--run-id "$SOURCE_RUN_ID" '
        f'--max-requests "${{NEXTRIP_MAPS_DAILY_LIMIT:-{default_limit}}}" '
        '--normalized-dir "$NORMALIZED_ROOT" '
        '--decision-dir "$DECISION_ROOT" '
        '--current-mapping-dir "$MAPS_SCRATCH/current-mappings" '
        '--menu-source-dir "$MAPS_SCRATCH/menu-sources" '
        '--current-menu-dir "$MAPS_SCRATCH/current-menu")"; '
        'printf "%s\\n" "$BATCH_OUTPUT"; '
        'RETURNED_RUN_ID="$(printf "%s\\n" "$BATCH_OUTPUT" '
        "| sed -n 's/^run_id=//p' | tail -n 1)\"; "
        'test "$RETURNED_RUN_ID" = "$SOURCE_RUN_ID"; '
        'printf "%s\\n" "$RETURNED_RUN_ID"'
    )


def _apply_canonical_refresh_command() -> str:
    """Apply exactly the four immediately-upstream entity batch attempts."""

    source_bindings = "".join(
        (
            f'SOURCE_RUN_ID_{entity_type.upper()}="'
            "{{ ti.xcom_pull(task_ids='"
            f"{_place_task_id(entity_type)}"
            "') }}\"; "
            f'test -n "$SOURCE_RUN_ID_{entity_type.upper()}"; '
        )
        for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES
    )
    source_arguments = " ".join(
        f'--run-id "$SOURCE_RUN_ID_{entity_type.upper()}"'
        for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES
    )
    source_values = " ".join(
        f'"$SOURCE_RUN_ID_{entity_type.upper()}"'
        for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES
    )

    return (
        "set -euo pipefail; "
        ': "${NEXTRIP_CANONICAL_DATASET:?set NEXTRIP_CANONICAL_DATASET}"; '
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        f"{source_bindings}"
        'UNIQUE_SOURCE_RUN_ID_COUNT="$(printf "%s\\n" '
        f'{source_values} | sort -u | wc -l)"; '
        f'test "$UNIQUE_SOURCE_RUN_ID_COUNT" -eq '
        f"{len(CANONICAL_GOOGLE_MAPS_ENTITY_TYPES)}; "
        'NORMALIZED_ROOT="${NEXTRIP_MAPS_NORMALIZED_ROOT:-data/normalized}"; '
        'DECISION_ROOT="${NEXTRIP_MAPS_DECISION_ROOT:-data/decisions}"; '
        'APPLY_OUTPUT="$(python -m nextrip_pipeline.cli '
        "apply-google-maps-canonical-refresh "
        '--canonical-dataset "$NEXTRIP_CANONICAL_DATASET" '
        '--observation-root "$NORMALIZED_ROOT" '
        '--decision-root "$DECISION_ROOT" '
        f"{_entity_type_arguments()} "
        f'{source_arguments})"; '
        'printf "%s\\n" "$APPLY_OUTPUT"; '
        'DATASET_PATH="$(printf "%s\\n" "$APPLY_OUTPUT" '
        "| sed -n 's/^dataset=//p' | tail -n 1)\"; "
        'test -n "$DATASET_PATH"; '
        'printf "%s\\n" "$DATASET_PATH"'
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
        description="Canonical-only daily Google Maps refresh for non-hotel places",
        start_date=pendulum.datetime(2026, 8, 18, 2, 0, tz=timezone),
        schedule="0 2 * * *",
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "google-maps", "playwright", "canonical"],
    ) as google_maps_daily_dag:
        build_google_maps_registry = BashOperator(
            task_id="build_google_maps_registry",
            bash_command=_registry_command(),
            execution_timeout=timedelta(minutes=10),
        )
        refresh_google_maps_places = [
            BashOperator(
                task_id=_place_task_id(entity_type),
                bash_command=_place_batch_command(entity_type),
                execution_timeout=timedelta(hours=8),
                do_xcom_push=True,
            )
            for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES
        ]
        apply_google_maps_canonical_refresh = BashOperator(
            task_id="apply_google_maps_canonical_refresh",
            bash_command=_apply_canonical_refresh_command(),
            execution_timeout=timedelta(hours=1),
            do_xcom_push=True,
        )

        build_google_maps_registry >> refresh_google_maps_places
        refresh_google_maps_places >> apply_google_maps_canonical_refresh
