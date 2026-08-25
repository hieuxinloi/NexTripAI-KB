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
    # Airflow remains optional for developer and unit-test environments.
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


def _rollout_env() -> dict[str, str]:
    run_root = os.getenv(
        "NEXTRIP_CANONICAL_ROLLOUT_RUN_ROOT",
        "data/runs/canonical_rollout",
    ).rstrip("/")
    return {
        "ROLLOUT_BASE_DATASET": (
            "{{ dag_run.conf.get('base_dataset', '') if dag_run else '' }}"
        ),
        "ROLLOUT_CANDIDATE_DATASET": (
            "{{ dag_run.conf.get('candidate_dataset', '') if dag_run else '' }}"
        ),
        "ROLLOUT_READINESS": (
            "{{ dag_run.conf.get('readiness', '') if dag_run else '' }}"
        ),
        "ROLLOUT_PATCH": "{{ dag_run.conf.get('patch', '') if dag_run else '' }}",
        "ROLLOUT_RUN_ROOT": (
            f"{run_root}/"
            "run={{ run_id | replace(':', '_') | replace('+', '_') }}"
        ),
    }


def _prefix() -> str:
    return (
        "set -euo pipefail; "
        'KB_ROOT="${NEXTRIP_KB_ROOT:-/opt/airflow/nextrip}"; '
        'cd "$KB_ROOT"; '
        ': "${ROLLOUT_BASE_DATASET:?missing base_dataset in dag_run.conf}"; '
        ': "${ROLLOUT_CANDIDATE_DATASET:?missing candidate_dataset in dag_run.conf}"; '
        ': "${ROLLOUT_READINESS:?missing readiness in dag_run.conf}"; '
        ': "${ROLLOUT_PATCH:?missing patch in dag_run.conf}"; '
        ': "${ROLLOUT_RUN_ROOT:?missing rollout run root}"; '
        'mkdir -p "$ROLLOUT_RUN_ROOT"; '
    )


def _verify_command() -> str:
    return _prefix() + (
        "python -m nextrip_graphrag.canonical_rollout_cli "
        '--canonical-root "${NEXTRIP_CANONICAL_ROOT:-data/canonical}" '
        '--base-dataset "$ROLLOUT_BASE_DATASET" '
        '--candidate-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        '--readiness "$ROLLOUT_READINESS" '
        '--patch "$ROLLOUT_PATCH" '
        "--verify-only"
    )


def _google_registry_command() -> str:
    return _prefix() + (
        "python -m nextrip_pipeline.cli build-google-maps-registry "
        '--canonical-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        '--output "$ROLLOUT_RUN_ROOT/google-maps-registry.json" '
        '--report "$ROLLOUT_RUN_ROOT/google-maps-registry-report.json" '
        '--batch-manifest-output "$ROLLOUT_RUN_ROOT/google-maps-manifest.json"'
    )


def _trivago_registry_command() -> str:
    return _prefix() + (
        "python -m nextrip_pipeline.cli build-trivago-registry "
        '--canonical-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        "--override config/trivago-mapping.json "
        "--current-mapping-dir "
        '"${NEXTRIP_CURRENT_TRIVAGO_MAPPING_ROOT:-data/current/trivago_mappings}" '
        '--output "$ROLLOUT_RUN_ROOT/trivago-registry.json" '
        '--report "$ROLLOUT_RUN_ROOT/trivago-registry-report.json"'
    )


def _completeness_command() -> str:
    return _prefix() + (
        'AUDIT_OUTPUT="$(python -m nextrip_pipeline.cli '
        "audit-canonical-completeness "
        '--dataset "$ROLLOUT_CANDIDATE_DATASET" '
        '--readiness "$ROLLOUT_READINESS" '
        '--google-manifest "$ROLLOUT_RUN_ROOT/google-maps-manifest.json" '
        '--google-registry "$ROLLOUT_RUN_ROOT/google-maps-registry.json" '
        '--trivago-registry "$ROLLOUT_RUN_ROOT/trivago-registry.json" '
        "--trivago-mapping-dir "
        '"${NEXTRIP_CURRENT_TRIVAGO_MAPPING_ROOT:-data/current/trivago_mappings}" '
        "--hotel-availability-dir "
        '"${NEXTRIP_CURRENT_HOTEL_AVAILABILITY_ROOT:-data/current/hotel_availability}" '
        "--hotel-price-dir "
        '"${NEXTRIP_CURRENT_HOTEL_PRICE_ROOT:-data/current/hotel_price}" '
        '--current-menu-dir "${NEXTRIP_CURRENT_MENU_ROOT:-data/current/menu}" '
        "--menu-source-dir "
        '"${NEXTRIP_CURRENT_MENU_SOURCE_ROOT:-data/current/google_maps_menu_sources}" '
        '--output-dir "$ROLLOUT_RUN_ROOT/completeness")"; '
        'printf "%s\\n" "$AUDIT_OUTPUT"; '
        'COMPLETENESS_PATH="$(printf "%s\\n" "$AUDIT_OUTPUT" '
        "| sed -n 's/^completeness=//p' | tail -n 1)\"; "
        'test -n "$COMPLETENESS_PATH"; '
        'test -f "$COMPLETENESS_PATH"; '
        'printf "%s\\n" "$COMPLETENESS_PATH"'
    )


def _apply_rollout_command() -> str:
    return _prefix() + (
        ': "${ROLLOUT_COMPLETENESS:?missing completeness XCom}"; '
        ': "${NEO4J_V8_URI:?set NEO4J_V8_URI}"; '
        ': "${NEO4J_V8_USER:?set NEO4J_V8_USER}"; '
        ': "${NEO4J_V8_PASSWORD:?set NEO4J_V8_PASSWORD}"; '
        ': "${NEO4J_V8_DATABASE:?set NEO4J_V8_DATABASE}"; '
        "python -m nextrip_graphrag.canonical_rollout_cli "
        '--canonical-root "${NEXTRIP_CANONICAL_ROOT:-data/canonical}" '
        '--base-dataset "$ROLLOUT_BASE_DATASET" '
        '--candidate-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        '--readiness "$ROLLOUT_READINESS" '
        '--patch "$ROLLOUT_PATCH" '
        '--completeness-audit "$ROLLOUT_COMPLETENESS" '
        '--output-root "${NEXTRIP_NEO4J_V8_RELEASE_ROOT:-data/neo4j/v8/releases}" '
        '--batch-size "${NEXTRIP_NEO4J_V8_IMPORT_BATCH_SIZE:-500}" '
        "--apply"
    )


def _publish_observations_command() -> str:
    return _prefix() + (
        ': "${NEO4J_V8_URI:?set NEO4J_V8_URI}"; '
        ': "${NEO4J_V8_USER:?set NEO4J_V8_USER}"; '
        ': "${NEO4J_V8_PASSWORD:?set NEO4J_V8_PASSWORD}"; '
        ': "${NEO4J_V8_DATABASE:?set NEO4J_V8_DATABASE}"; '
        "python -m nextrip_graphrag.observation_cli "
        '--canonical-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        "--hotel-price-root "
        '"${NEXTRIP_CURRENT_HOTEL_PRICE_ROOT:-data/current/hotel_price}" '
        "--hotel-availability-root "
        '"${NEXTRIP_CURRENT_HOTEL_AVAILABILITY_ROOT:-data/current/hotel_availability}" '
        '--menu-root "${NEXTRIP_CURRENT_MENU_ROOT:-data/current/menu}" '
        "--output-root "
        '"${NEXTRIP_NEO4J_V8_OBSERVATION_RUN_ROOT:-data/neo4j/v8/observation_runs}" '
        "--apply"
    )


def _embedding_command() -> str:
    return _prefix() + (
        'export GOOGLE_API_KEY="${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}"; '
        ': "${GOOGLE_API_KEY:?set GOOGLE_API_KEY or GEMINI_API_KEY}"; '
        'EXECUTION_ID="$(python -c '
        "'from uuid import uuid4; print(uuid4().hex[:12])'"
        ')"; '
        'EMBEDDING_RUN_ID="v8-embedding-{{ run_id }}-'
        'try{{ ti.try_number }}-$EXECUTION_ID"; '
        "python -m nextrip_graphrag.v8_embedding_cli "
        '--canonical-dataset "$ROLLOUT_CANDIDATE_DATASET" '
        '--model "${GEMINI_EMBEDDING_MODEL:-gemini-embedding-001}" '
        '--dimension "${NEXTRIP_V8_EMBEDDING_DIMENSION:-1536}" '
        '--cache-root "${NEXTRIP_V8_EMBEDDING_CACHE_ROOT:-'
        'data/cache/v8_embeddings}" '
        '--output-root "${NEXTRIP_V8_EMBEDDING_RUN_ROOT:-'
        'data/neo4j/v8/embedding_runs}" '
        '--batch-size "${NEXTRIP_V8_EMBEDDING_BATCH_SIZE:-16}" '
        '--request-delay "${NEXTRIP_V8_EMBEDDING_REQUEST_DELAY:-2}" '
        '--max-retries "${NEXTRIP_V8_EMBEDDING_MAX_RETRIES:-5}" '
        '--run-id "$EMBEDDING_RUN_ID" '
        "--apply"
    )


canonical_rollout_dag = None

if DAG is not None and _env_flag("NEXTRIP_CANONICAL_ROLLOUT_AIRFLOW_ENABLED"):
    timezone = pendulum.timezone("Asia/Ho_Chi_Minh")
    common = {
        "owner": "nextrip-data",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    }
    rollout_env = _rollout_env()
    current_data_pool = os.getenv(
        "NEXTRIP_CURRENT_DATA_POOL",
        "current_data_snapshot",
    )
    release_pool = os.getenv(
        "NEXTRIP_CANONICAL_ROLLOUT_POOL",
        "canonical_release_rollout",
    )

    with DAG(
        dag_id="nextrip_canonical_release_rollout",
        description="Gate, activate and publish one immutable canonical candidate",
        start_date=pendulum.datetime(2026, 8, 25, 0, 0, tz=timezone),
        schedule=None,
        catchup=False,
        max_active_runs=1,
        default_args=common,
        tags=["nextrip", "canonical", "neo4j", "rollout"],
    ) as canonical_rollout_dag:
        verify_candidate = BashOperator(
            task_id="verify_candidate_parent",
            bash_command=_verify_command(),
            env=rollout_env,
            append_env=True,
            execution_timeout=timedelta(minutes=10),
            pool=release_pool,
        )
        build_google_registry = BashOperator(
            task_id="build_candidate_google_registry",
            bash_command=_google_registry_command(),
            env=rollout_env,
            append_env=True,
            execution_timeout=timedelta(minutes=10),
        )
        build_trivago_registry = BashOperator(
            task_id="build_candidate_trivago_registry",
            bash_command=_trivago_registry_command(),
            env=rollout_env,
            append_env=True,
            execution_timeout=timedelta(minutes=10),
        )
        audit_completeness = BashOperator(
            task_id="audit_candidate_completeness",
            bash_command=_completeness_command(),
            env=rollout_env,
            append_env=True,
            execution_timeout=timedelta(minutes=30),
            do_xcom_push=True,
            pool=current_data_pool,
        )
        apply_rollout_env = {
            **rollout_env,
            "ROLLOUT_COMPLETENESS": (
                "{{ ti.xcom_pull(task_ids='audit_candidate_completeness') }}"
            ),
        }
        activate_release = BashOperator(
            task_id="activate_canonical_release",
            bash_command=_apply_rollout_command(),
            env=apply_rollout_env,
            append_env=True,
            execution_timeout=timedelta(hours=2),
            pool=release_pool,
        )
        publish_observations = BashOperator(
            task_id="publish_release_observations",
            bash_command=_publish_observations_command(),
            env=rollout_env,
            append_env=True,
            execution_timeout=timedelta(minutes=30),
            pool=current_data_pool,
        )

        verify_candidate >> [build_google_registry, build_trivago_registry]
        [build_google_registry, build_trivago_registry] >> audit_completeness
        audit_completeness >> activate_release >> publish_observations
        if _env_flag("NEXTRIP_V8_EMBEDDING_ENABLED"):
            embed_release = BashOperator(
                task_id="embed_active_canonical_release",
                bash_command=_embedding_command(),
                env=rollout_env,
                append_env=True,
                execution_timeout=timedelta(hours=6),
                pool=release_pool,
            )
            publish_observations >> embed_release
