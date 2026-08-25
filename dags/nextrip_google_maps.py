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
        from airflow.providers.standard.operators.trigger_dagrun import (
            TriggerDagRunOperator,
        )
    except ImportError:
        from airflow.operators.bash import BashOperator
        from airflow.operators.trigger_dagrun import TriggerDagRunOperator
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
    """Enter the repository and resolve the current canonical dataset.

    The mutable pointer is the production selector. ``NEXTRIP_CANONICAL_DATASET``
    remains a legacy fallback so an existing deployment can be upgraded before
    its first pointer is promoted.
    """

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


def _pinned_dataset_prefix() -> str:
    """Bind downstream tasks to the dataset resolved by the registry task."""

    return (
        "set -euo pipefail; "
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        'CANONICAL_DATASET="{{ ti.xcom_pull('
        "task_ids='build_google_maps_registry') }}\"; "
        'test -n "$CANONICAL_DATASET"; '
        'test -f "$CANONICAL_DATASET"; '
    )


def _trusted_scheduled_guard() -> str:
    return (
        'case "${NEXTRIP_GOOGLE_MAPS_TRUSTED_SCHEDULED:-false}" in '
        "1|true|TRUE|yes|YES|on|ON) ;; *) "
        'echo "Trusted scheduled Google Maps mode is disabled" >&2; '
        "exit 2 ;; esac; "
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

    return _command_prefix() + (
        "python -m nextrip_pipeline.cli build-google-maps-registry "
        '--canonical-dataset "$CANONICAL_DATASET" '
        "--output config/generated/canonical-google-maps-mapping-registry.json "
        "--report config/generated/canonical-google-maps-registry-report.json "
        f"--batch-manifest-output {CANONICAL_GOOGLE_MAPS_MANIFEST}; "
        'printf "%s\\n" "$CANONICAL_DATASET"'
    )


def _place_batch_command(entity_type: str, default_limit: int = 10_000) -> str:
    """Run one entity profile with an attempt-unique caller-owned run ID."""

    _place_task_id(entity_type)

    return (
        _pinned_dataset_prefix()
        + _trusted_scheduled_guard()
        + (
            'EXECUTION_ID="$(python -c '
            "'from uuid import uuid4; print(uuid4().hex[:12])'"
            ')"; '
            'SOURCE_RUN_ID="google-maps-place-{{ run_id }}-'
            f"{entity_type}"
            '-try{{ ti.try_number }}-$EXECUTION_ID"; '
            'MAPS_SCRATCH="${NEXTRIP_MAPS_SCRATCH_ROOT:-/tmp/nextrip-google-maps}/'
            '$SOURCE_RUN_ID"; '
            'NORMALIZED_ROOT="${NEXTRIP_MAPS_NORMALIZED_ROOT:-data/normalized}"; '
            'DECISION_ROOT="${NEXTRIP_MAPS_DECISION_ROOT:-data/decisions}"; '
            'RAW_ROOT="${NEXTRIP_RAW_ROOT:-data/raw}"; '
            'ACCEPTED_ROOT="${NEXTRIP_ACCEPTED_OBSERVATION_ROOT:-data/observations}"; '
            'VALIDATION_ROOT="${NEXTRIP_VALIDATION_ROOT:-data/validation}"; '
            'QUALITY_ROOT="${NEXTRIP_MAPS_QUALITY_ROOT:-data/quality/google_maps_mapping}"; '
            'CRAWL_ARTIFACT_ROOT="${NEXTRIP_CRAWL_ARTIFACT_ROOT:-data/crawl_artifacts}"; '
            'SUMMARY_ROOT="${NEXTRIP_MAPS_SUMMARY_ROOT:-data/runs/google_maps}"; '
            'BATCH_OUTPUT="$(python -m nextrip_pipeline.cli batch-google-maps '
            f"--manifest {CANONICAL_GOOGLE_MAPS_MANIFEST} "
            "--mode place "
            f"--entity-type {entity_type} "
            '--run-id "$SOURCE_RUN_ID" '
            f'--max-requests "${{NEXTRIP_MAPS_WEEKLY_LIMIT:-'
            f'${{NEXTRIP_MAPS_DAILY_LIMIT:-{default_limit}}}}}" '
            '--max-no-update-ratio '
            '"${NEXTRIP_MAPS_MAX_NO_UPDATE_RATIO:-0.20}" '
            '--raw-dir "$RAW_ROOT" '
            '--artifact-dir "$CRAWL_ARTIFACT_ROOT" '
            '--normalized-dir "$NORMALIZED_ROOT" '
            '--accepted-observation-dir "$ACCEPTED_ROOT" '
            '--validation-dir "$VALIDATION_ROOT" '
            '--decision-dir "$DECISION_ROOT" '
            '--quality-dir "$QUALITY_ROOT" '
            '--summary-dir "$SUMMARY_ROOT" '
            '--mapping-review-dir "$MAPS_SCRATCH/mapping-review" '
            "--trusted-scheduled-crawl "
            "--disable-llm-review-queue "
            '--current-mapping-dir "$MAPS_SCRATCH/current-mappings" '
            '--menu-source-dir "$MAPS_SCRATCH/menu-sources" '
            '--current-menu-dir "$MAPS_SCRATCH/current-menu")"; '
            'printf "%s\\n" "$BATCH_OUTPUT"; '
            'RETURNED_RUN_ID="$(printf "%s\\n" "$BATCH_OUTPUT" '
            "| sed -n 's/^run_id=//p' | tail -n 1)\"; "
            'test "$RETURNED_RUN_ID" = "$SOURCE_RUN_ID"; '
            'printf "%s\\n" "$RETURNED_RUN_ID"'
        )
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
        _pinned_dataset_prefix()
        + _trusted_scheduled_guard()
        + (
            f"{source_bindings}"
            'UNIQUE_SOURCE_RUN_ID_COUNT="$(printf "%s\\n" '
            f'{source_values} | sort -u | wc -l)"; '
            f'test "$UNIQUE_SOURCE_RUN_ID_COUNT" -eq '
            f"{len(CANONICAL_GOOGLE_MAPS_ENTITY_TYPES)}; "
            'NORMALIZED_ROOT="${NEXTRIP_MAPS_NORMALIZED_ROOT:-data/normalized}"; '
            'DECISION_ROOT="${NEXTRIP_MAPS_DECISION_ROOT:-data/decisions}"; '
            'APPLY_OUTPUT="$(python -m nextrip_pipeline.cli '
            "apply-google-maps-canonical-refresh "
            '--canonical-dataset "$CANONICAL_DATASET" '
            '--observation-root "$NORMALIZED_ROOT" '
            '--decision-root "$DECISION_ROOT" '
            f"{_entity_type_arguments()} "
            f'{source_arguments})"; '
            'printf "%s\\n" "$APPLY_OUTPUT"; '
            'DATASET_PATH="$(printf "%s\\n" "$APPLY_OUTPUT" '
            "| sed -n 's/^dataset=//p' | tail -n 1)\"; "
            'READINESS_PATH="$(printf "%s\\n" "$APPLY_OUTPUT" '
            "| sed -n 's/^readiness=//p' | tail -n 1)\"; "
            'PATCH_PATH="$(printf "%s\\n" "$APPLY_OUTPUT" '
            "| sed -n 's/^patch=//p' | tail -n 1)\"; "
            'test -n "$DATASET_PATH"; '
            'test -n "$READINESS_PATH"; '
            'test -n "$PATCH_PATH"; '
            'DATASET_PATH="$(realpath "$DATASET_PATH")"; '
            'READINESS_PATH="$(realpath "$READINESS_PATH")"; '
            'PATCH_PATH="$(realpath "$PATCH_PATH")"; '
            "printf '{\"base_dataset\":\"%s\","
            "\"candidate_dataset\":\"%s\","
            "\"readiness\":\"%s\",\"patch\":\"%s\"}\\n' "
            '"$CANONICAL_DATASET" "$DATASET_PATH" '
            '"$READINESS_PATH" "$PATCH_PATH"'
        )
    )


google_maps_weekly_dag = None

if DAG is not None and _env_flag("NEXTRIP_GOOGLE_MAPS_AIRFLOW_ENABLED"):
    common = {
        "owner": "nextrip-data",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    }
    timezone = pendulum.timezone("Asia/Ho_Chi_Minh")

    with DAG(
        dag_id="nextrip_google_maps_weekly",
        description="Canonical-only weekly Google Maps refresh for non-hotel places",
        start_date=pendulum.datetime(2026, 8, 18, 2, 0, tz=timezone),
        schedule=os.getenv("NEXTRIP_GOOGLE_MAPS_SCHEDULE", "0 2 * * 1"),
        catchup=False,
        max_active_runs=1,
        render_template_as_native_obj=True,
        default_args=common,
        tags=["nextrip", "google-maps", "playwright", "canonical"],
    ) as google_maps_weekly_dag:
        build_google_maps_registry = BashOperator(
            task_id="build_google_maps_registry",
            bash_command=_registry_command(),
            execution_timeout=timedelta(minutes=10),
            do_xcom_push=True,
        )
        refresh_google_maps_places = [
            BashOperator(
                task_id=_place_task_id(entity_type),
                bash_command=_place_batch_command(entity_type),
                execution_timeout=timedelta(hours=8),
                do_xcom_push=True,
                pool=os.getenv("NEXTRIP_GOOGLE_MAPS_POOL", "google_maps_web"),
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
        if _env_flag("NEXTRIP_CANONICAL_ROLLOUT_AIRFLOW_ENABLED"):
            trigger_canonical_release_rollout = TriggerDagRunOperator(
                task_id="trigger_canonical_release_rollout",
                trigger_dag_id="nextrip_canonical_release_rollout",
                trigger_run_id="google-maps-rollout-{{ run_id }}",
                conf=(
                    "{{ ti.xcom_pull("
                    "task_ids='apply_google_maps_canonical_refresh') }}"
                ),
                wait_for_completion=False,
                skip_when_already_exists=True,
                fail_when_dag_is_paused=True,
            )
            apply_google_maps_canonical_refresh >> trigger_canonical_release_rollout
