from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nextrip_graphrag.canonical_rollout_cli import build_parser


ROOT = Path(__file__).parents[2]


def _load_dag_module():
    path = ROOT / "dags" / "nextrip_canonical_rollout.py"
    spec = importlib.util.spec_from_file_location("nextrip_rollout_dag", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rollout_dag_is_disabled_by_default_and_fail_closed(monkeypatch) -> None:
    monkeypatch.delenv("NEXTRIP_CANONICAL_ROLLOUT_AIRFLOW_ENABLED", raising=False)
    module = _load_dag_module()

    assert module.canonical_rollout_dag is None
    assert "--verify-only" in module._verify_command()
    assert "audit-canonical-completeness" in module._completeness_command()
    assert "canonical_rollout_cli" in module._apply_rollout_command()
    assert "--apply" in module._apply_rollout_command()
    assert "observation_cli" in module._publish_observations_command()
    assert "v8_embedding_cli" in module._embedding_command()
    assert "--apply" in module._embedding_command()
    assert "GEMINI_EMBEDDING_MODEL" in module._embedding_command()
    assert "$EXECUTION_ID" in module._embedding_command()
    assert "data/current/place" not in (
        module._verify_command()
        + module._completeness_command()
        + module._apply_rollout_command()
    )


def test_rollout_flag_is_strict(monkeypatch) -> None:
    module = _load_dag_module()

    monkeypatch.setenv("TEST_ROLLOUT_FLAG", "yes")
    assert module._env_flag("TEST_ROLLOUT_FLAG") is True
    monkeypatch.setenv("TEST_ROLLOUT_FLAG", "invalid")
    with pytest.raises(ValueError, match="must be a boolean"):
        module._env_flag("TEST_ROLLOUT_FLAG")


def test_rollout_cli_requires_pinned_source_artifacts() -> None:
    arguments = build_parser().parse_args(
        [
            "--base-dataset",
            "base.json",
            "--candidate-dataset",
            "candidate.json",
            "--readiness",
            "readiness.json",
            "--patch",
            "patch.json",
            "--verify-only",
        ]
    )

    assert arguments.verify_only is True
    assert arguments.completeness_audit is None
    assert arguments.apply is False


def test_rollout_source_is_manual_serialized_and_count_agnostic() -> None:
    source = (ROOT / "dags" / "nextrip_canonical_rollout.py").read_text("utf-8")

    assert 'dag_id="nextrip_canonical_release_rollout"' in source
    assert "schedule=None" in source
    assert "max_active_runs=1" in source
    assert "canonical_release_rollout" in source
    assert "current_data_snapshot" in source
    assert "NEXTRIP_V8_EMBEDDING_ENABLED" in source
    assert "embed_active_canonical_release" in source
    assert "692" not in source
