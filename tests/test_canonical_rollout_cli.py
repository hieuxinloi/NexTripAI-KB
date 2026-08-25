from __future__ import annotations

from types import SimpleNamespace

import pytest

import nextrip_graphrag.canonical_rollout_cli as rollout_module
from nextrip_graphrag.canonical_rollout_cli import (
    CanonicalRolloutError,
    validate_rollout_inputs,
)


def _identity(dataset_id: str, dataset_hash: str, manifest: str = "manifest"):
    return SimpleNamespace(
        dataset_id=dataset_id,
        dataset_hash=dataset_hash,
        manifest_id=manifest,
        manifest_hash="f" * 64,
    )


def _patch_reader(monkeypatch: pytest.MonkeyPatch, patch: object) -> None:
    parser = SimpleNamespace(model_validate_json=lambda _value: patch)
    monkeypatch.setattr(rollout_module, "CanonicalGoogleMapsRefreshPatch", parser)


def test_candidate_validation_proves_parent_patch_and_readiness(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _identity("canonical-active-base", "a" * 64)
    candidate = _identity("canonical-active-candidate", "b" * 64)
    readiness = _identity(
        candidate.dataset_id,
        candidate.dataset_hash,
        candidate.manifest_id,
    )
    patch = SimpleNamespace(
        patch_id="canonical-google-patch-test",
        base_dataset_id=base.dataset_id,
        base_dataset_hash=base.dataset_hash,
    )
    patch_path = tmp_path / "patch.json"
    patch_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        rollout_module,
        "resolve_active_canonical_dataset",
        lambda _root: SimpleNamespace(dataset=base),
    )
    monkeypatch.setattr(
        rollout_module,
        "read_canonical_active_dataset",
        lambda path: base if str(path) == "base.json" else candidate,
    )
    monkeypatch.setattr(
        rollout_module,
        "read_canonical_dataset_readiness",
        lambda _path: readiness,
    )
    monkeypatch.setattr(
        rollout_module,
        "require_canonical_dataset_publish_ready",
        lambda report: report,
    )
    monkeypatch.setattr(
        rollout_module,
        "apply_google_maps_canonical_refresh_patch",
        lambda selected_base, selected_patch: (
            candidate if selected_base is base and selected_patch is patch else None
        ),
    )
    _patch_reader(monkeypatch, patch)

    validated = validate_rollout_inputs(
        canonical_root=tmp_path,
        base_dataset="base.json",
        candidate_dataset="candidate.json",
        readiness="readiness.json",
        patch=patch_path,
    )

    assert validated.base is base
    assert validated.candidate is candidate
    assert validated.patch is patch
    assert validated.active_dataset_id == base.dataset_id


def test_candidate_validation_rejects_unrelated_active_pointer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _identity("canonical-active-base", "a" * 64)
    candidate = _identity("canonical-active-candidate", "b" * 64)
    unrelated = _identity("canonical-active-other", "c" * 64)
    readiness = _identity(
        candidate.dataset_id,
        candidate.dataset_hash,
        candidate.manifest_id,
    )
    patch = SimpleNamespace(
        patch_id="canonical-google-patch-test",
        base_dataset_id=base.dataset_id,
        base_dataset_hash=base.dataset_hash,
    )
    patch_path = tmp_path / "patch.json"
    patch_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        rollout_module,
        "resolve_active_canonical_dataset",
        lambda _root: SimpleNamespace(dataset=unrelated),
    )
    monkeypatch.setattr(
        rollout_module,
        "read_canonical_active_dataset",
        lambda path: base if str(path) == "base.json" else candidate,
    )
    monkeypatch.setattr(
        rollout_module,
        "read_canonical_dataset_readiness",
        lambda _path: readiness,
    )
    monkeypatch.setattr(
        rollout_module,
        "require_canonical_dataset_publish_ready",
        lambda report: report,
    )
    monkeypatch.setattr(
        rollout_module,
        "apply_google_maps_canonical_refresh_patch",
        lambda _base, _patch: candidate,
    )
    _patch_reader(monkeypatch, patch)

    with pytest.raises(CanonicalRolloutError, match="unrelated release"):
        validate_rollout_inputs(
            canonical_root=tmp_path,
            base_dataset="base.json",
            candidate_dataset="candidate.json",
            readiness="readiness.json",
            patch=patch_path,
        )
