from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nextrip_traffic.models import PrewarmPlan


ROOT = Path(__file__).parents[2]


def _load_dag_module():
    dag_path = ROOT / "dags" / "nextrip_traffic.py"
    spec = importlib.util.spec_from_file_location("nextrip_traffic_dag", dag_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dag_module_is_safe_without_airflow_and_keeps_realtime_out() -> None:
    module = _load_dag_module()

    assert module.traffic_maintenance_dag is None
    assert module.traffic_prewarm_dag is None
    assert "provider-health --require valhalla" in module._provider_health_command()
    prewarm_health = module._provider_health_command(require_here=True)
    assert "--require valhalla --require here" in prewarm_health
    assert "cleanup-cache" in module._cleanup_cache_command()
    prewarm = module._prewarm_command()
    assert "prewarm --config" in prewarm
    assert "config/traffic-prewarm.json" in prewarm
    assert "--force-refresh" in prewarm
    assert "--no-allow-stale-on-error" in prewarm
    assert "--fail-on-degraded" in prewarm
    assert " nextrip_traffic.cli route " not in prewarm
    assert " nextrip_traffic.cli matrix " not in prewarm


def test_dag_environment_flags_are_strict_and_fail_closed(monkeypatch) -> None:
    module = _load_dag_module()

    monkeypatch.delenv("TEST_TRAFFIC_FLAG", raising=False)
    assert module._env_flag("TEST_TRAFFIC_FLAG") is False
    monkeypatch.setenv("TEST_TRAFFIC_FLAG", "YES")
    assert module._env_flag("TEST_TRAFFIC_FLAG") is True
    monkeypatch.setenv("TEST_TRAFFIC_FLAG", "off")
    assert module._env_flag("TEST_TRAFFIC_FLAG") is False
    monkeypatch.setenv("TEST_TRAFFIC_FLAG", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        module._env_flag("TEST_TRAFFIC_FLAG")


def test_prewarm_plan_is_bounded_to_two_city_directions() -> None:
    plan = PrewarmPlan.model_validate_json(
        (ROOT / "config" / "traffic-prewarm.json").read_text("utf-8")
    )

    assert len(plan.pairs) == 2
    assert {
        (pair.origin_id, pair.destination_id)
        for pair in plan.pairs
    } == {
        ("city_quy_nhon", "city_da_nang"),
        ("city_da_nang", "city_quy_nhon"),
    }
    assert sum(len(pair.modes) for pair in plan.pairs) == 4


def test_valhalla_deployment_uses_official_immutable_image_and_local_data() -> None:
    compose = (ROOT / "deploy" / "traffic" / "compose.yaml").read_text("utf-8")

    assert (
        "ghcr.io/valhalla/valhalla-scripted:3.8.3@"
        "sha256:24ef7955899dececb94e26c6dfb89d64fabfae875f980432694b0261eb6c251b"
    ) in compose
    assert "binhnguyen" not in compose.casefold()
    assert "tile_urls:" not in compose
    assert "VALHALLA_DATASET_VERSION" in compose
    assert "http://127.0.0.1:8002/status" in compose
    assert "traffic_cache:/app/data/current/traffic" in compose
    assert 'NEXTRIP_CANONICAL_DATASET: "${NEXTRIP_CANONICAL_DATASET:?' in compose
    assert 'source: "${NEXTRIP_DATA_ROOT:-../../data}/canonical"' in compose
    assert "target: /app/data/canonical" in compose
    assert "data/current/place" not in compose
    assert "TRAFFIC_API_KEY:?set TRAFFIC_API_KEY" in compose
    dockerfile = (ROOT / "deploy" / "traffic" / "Dockerfile").read_text("utf-8")
    assert "http://127.0.0.1:8010/ready" in dockerfile
    assert "/app/data/canonical" in dockerfile
    assert "data/current/place" not in dockerfile


def test_airflow_overlay_is_pinned_and_disabled_by_default() -> None:
    dockerfile = (
        ROOT / "deploy" / "traffic" / "Dockerfile.airflow"
    ).read_text("utf-8")
    compose = (
        ROOT / "deploy" / "traffic" / "compose.airflow.yaml"
    ).read_text("utf-8")

    assert "FROM apache/airflow:3.3.0-python3.11" in dockerfile
    assert (
        "COPY --chown=airflow:0 dags/nextrip_traffic.py "
        "/opt/airflow/dags/nextrip_traffic.py"
    ) in dockerfile
    for dag in (
        "nextrip_hotel_prices.py",
        "nextrip_google_maps.py",
        "nextrip_neo4j_v8.py",
        "nextrip_canonical_rollout.py",
    ):
        assert f"dags/{dag}" in dockerfile
    assert "COPY --chown=airflow:0 nextrip_graphrag" in dockerfile
    # The canonical rollout performs Gemini embeddings in the Airflow image.
    assert '"google-genai>=2.14.0,<3"' in dockerfile
    assert "neo4j-graphrag" not in dockerfile
    assert "playwright install --with-deps chromium" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in dockerfile
    assert "COPY --chown=airflow:0 dags ./dags" not in dockerfile
    assert "_PIP_ADDITIONAL_REQUIREMENTS" not in compose
    assert "NEXTRIP_TRAFFIC_AIRFLOW_ENABLED:-false" in compose
    assert "NEXTRIP_TRAFFIC_PREWARM_ENABLED:-false" in compose
    assert "LocalExecutor" in compose
    assert "command: api-server" in compose
    assert "command: scheduler" in compose
    assert "command: dag-processor" in compose
    assert "traffic_cache:/opt/airflow/nextrip/data/current/traffic" in compose
    assert "NEXTRIP_CANONICAL_DATASET_POINTER" in compose
    assert 'NEXTRIP_CANONICAL_DATASET: "${NEXTRIP_CANONICAL_DATASET:-}"' in compose
    assert 'source: "${NEXTRIP_DATA_ROOT:-../../data}"' in compose
    assert "target: /opt/airflow/nextrip/data" in compose
    assert "read_only: true" not in compose
    assert "    valhalla:\n      condition: service_healthy" not in compose
    assert "source: ../../config/generated" in compose
    assert "NEXTRIP_ACCEPTED_OBSERVATION_ROOT: data/observations" in compose
    assert "NEXTRIP_GOOGLE_MAPS_AIRFLOW_ENABLED:-false" in compose
    assert "NEXTRIP_HOTEL_PRICES_AIRFLOW_ENABLED:-false" in compose
    assert "NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED:-false" in compose
    assert "NEXTRIP_GOOGLE_MAPS_POOL:-google_maps_web" in compose
    assert "NEXTRIP_CURRENT_DATA_POOL:-current_data_snapshot" in compose
    assert "NEXTRIP_CURRENT_DATA_POOL_SLOTS:-1" in compose
    assert "NEXTRIP_CANONICAL_ROLLOUT_AIRFLOW_ENABLED:-false" in compose
    assert "NEXTRIP_CANONICAL_ROLLOUT_POOL:-canonical_release_rollout" in compose
    assert "NEXTRIP_CANONICAL_ROLLOUT_POOL_SLOTS:-1" in compose
    assert "airflow pools set" in compose
    assert "data/current/place" not in compose
    assert "/opt/airflow/nextrip/data/canonical" in dockerfile
    assert "data/current/place" not in dockerfile


def test_valhalla_preparation_requires_version_and_checksum() -> None:
    script = (
        ROOT / "deploy" / "traffic" / "prepare-valhalla-data.ps1"
    ).read_text("utf-8")

    assert "[Parameter(Mandatory = $true)]" in script
    assert "Get-FileHash" in script
    assert "SHA-256 mismatch" in script
    assert "DatasetVersion" in script
    assert "dataset-manifest.json" in script
    assert "valhalla_version = '3.8.3'" in script


def test_environment_template_uses_the_documented_dotenv_name() -> None:
    template = (
        ROOT / "deploy" / "traffic" / "traffic.env.example"
    ).read_text("utf-8")
    ignores = (ROOT / "deploy" / "traffic" / ".gitignore").read_text("utf-8")

    assert "deploy/traffic/.env" in template
    assert "NEXTRIP_DATA_ROOT=../../data" in template
    assert "F:\\nextrip-runtime\\data" in template
    assert "NEXTRIP_CANONICAL_DATASET=data/canonical/datasets/" in template
    assert "TRAFFIC_API_KEY=replace-with-a-long-random-secret" in template
    assert ".env" in ignores.splitlines()
    assert "traffic.env" not in template
