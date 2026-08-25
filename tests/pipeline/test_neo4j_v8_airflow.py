from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def _load_dag_module():
    dag_path = ROOT / "dags" / "nextrip_neo4j_v8.py"
    spec = importlib.util.spec_from_file_location("nextrip_neo4j_v8_dag", dag_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v8_observation_dag_is_disabled_by_default_and_command_is_bounded(
    monkeypatch,
) -> None:
    monkeypatch.delenv("NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED", raising=False)
    module = _load_dag_module()

    assert module.neo4j_v8_observations_dag is None
    command = module._publish_observations_command()
    assert "NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED:-false" in command
    assert "python -m nextrip_graphrag.observation_cli" in command
    assert "NEXTRIP_CANONICAL_DATASET_POINTER" in command
    assert "resolve-active-canonical-dataset" in command
    assert 'elif [ -n "${NEXTRIP_CANONICAL_DATASET:-}" ]' in command
    assert '--canonical-dataset "$CANONICAL_DATASET"' in command
    assert "data/current/hotel_price" in command
    assert "data/current/hotel_availability" in command
    assert "data/current/place" not in command
    assert "data/current/menu" in command
    assert "data/neo4j/v8/observation_runs" in command
    assert command.endswith("--apply")


def test_v8_observation_command_requires_only_v8_neo4j_credentials() -> None:
    command = _load_dag_module()._publish_observations_command()

    assert "NEO4J_V8_URI:?" in command
    assert "NEO4J_V8_USER:?" in command
    assert "NEO4J_V8_PASSWORD:?" in command
    assert "NEO4J_V8_DATABASE:?" in command
    assert "NEO4J_URI" not in command
    assert "NEO4J_USER" not in command
    assert "NEO4J_PASSWORD" not in command
    assert "traffic" not in command.casefold()
    assert "v8-canonical" not in command


def test_v8_observation_environment_gate_is_strict_and_fail_closed(
    monkeypatch,
) -> None:
    module = _load_dag_module()

    monkeypatch.delenv("TEST_V8_FLAG", raising=False)
    assert module._env_flag("TEST_V8_FLAG") is False
    monkeypatch.setenv("TEST_V8_FLAG", "YES")
    assert module._env_flag("TEST_V8_FLAG") is True
    monkeypatch.setenv("TEST_V8_FLAG", "off")
    assert module._env_flag("TEST_V8_FLAG") is False
    monkeypatch.setenv("TEST_V8_FLAG", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        module._env_flag("TEST_V8_FLAG")


def test_v8_observation_schedule_is_configurable_without_a_dataset_default() -> None:
    source = (ROOT / "dags" / "nextrip_neo4j_v8.py").read_text("utf-8")

    assert "NEXTRIP_NEO4J_V8_OBSERVATIONS_SCHEDULE" in source
    assert '"*/15 * * * *"' in source
    assert "692" not in source
    assert "canonical-active-" not in source
    assert "schedule=None" not in source
    assert "publish_current_observations" in source
    assert 'NEXTRIP_CURRENT_DATA_POOL", "current_data_snapshot"' in source
