from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import nextrip_pipeline.canonical.active_pointer as active_pointer_module
import nextrip_pipeline.cli as cli_module
from nextrip_pipeline.canonical.active_pointer import (
    ACTIVE_DATASET_POINTER_FILENAME,
    CanonicalActiveDatasetPointer,
    CanonicalActivePointerError,
    CanonicalPromotionAlreadyExistsError,
    promote_canonical_active_dataset,
    read_canonical_active_dataset_pointer,
    resolve_active_canonical_dataset,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActiveDatasetReport,
    CanonicalActiveDatasetWriter,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetNotReadyError,
    CanonicalDatasetReadinessReport,
    CanonicalDatasetReadinessWriter,
)
from nextrip_pipeline.cli import main


UTC = timezone.utc
ACTIVATED_AT = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)


def _dataset(seed: str) -> CanonicalActiveDataset:
    manifest_id = f"canonical-manifest-{seed}"
    manifest_hash = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    report_values = {
        "schema_version": "1.0.0",
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "master_source_record_count": 0,
        "approved_replacement_count": 0,
        "canonical_record_count": 0,
        "master_materialized_count": 0,
        "replacement_materialized_count": 0,
        "retired_duplicate_count": 0,
        "open_vacancy_count": 0,
        "filled_vacancy_count": 0,
        "entity_city_counts": [],
        "quotas": [],
    }
    report = CanonicalActiveDatasetReport(
        report_hash=stable_sha256(report_values),
        **report_values,
    )
    dataset_values = {
        "schema_version": "1.0.0",
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "records": [],
        "report": report,
    }
    dataset_hash = stable_sha256(
        {
            **dataset_values,
            "report": report.model_dump(mode="json"),
        }
    )
    return CanonicalActiveDataset(
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        **dataset_values,
    )


def _readiness(
    dataset: CanonicalActiveDataset,
    *,
    publish_ready: bool = True,
) -> CanonicalDatasetReadinessReport:
    values = {
        "schema_version": "1.3.0",
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "audit_id": "duplicate_evidence_test",
        "audit_hash": hashlib.sha256(b"audit").hexdigest(),
        "manifest_id": dataset.manifest_id,
        "manifest_hash": dataset.manifest_hash,
        "group_count": 0,
        "resolved_merge": [],
        "resolved_distinct": [],
        "resolved_quarantined": [],
        "unresolved_groups": [],
        "explicit_distinct_decisions": [],
        "open_vacancy_count": 0 if publish_ready else 1,
        "publish_ready": publish_ready,
    }
    readiness_hash = stable_sha256(values)
    return CanonicalDatasetReadinessReport(
        readiness_id=f"canonical-readiness-{readiness_hash[:20]}",
        readiness_hash=readiness_hash,
        **values,
    )


def _write_pair(
    root: Path,
    seed: str,
    *,
    publish_ready: bool = True,
) -> tuple[Path, Path, CanonicalActiveDataset, CanonicalDatasetReadinessReport]:
    root.mkdir(parents=True, exist_ok=True)
    dataset = _dataset(seed)
    readiness = _readiness(dataset, publish_ready=publish_ready)
    dataset_path = CanonicalActiveDatasetWriter(root / "datasets").write(dataset)
    readiness_path = CanonicalDatasetReadinessWriter(root / "readiness").write(
        readiness
    )
    return dataset_path, readiness_path, dataset, readiness


def test_promotion_is_idempotent_portable_and_resolves_after_root_moves(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, dataset, readiness = _write_pair(root, "one")

    first = promote_canonical_active_dataset(
        root,
        dataset_path,
        readiness_path,
        activated_at=ACTIVATED_AT,
    )
    original_pointer_bytes = (root / ACTIVE_DATASET_POINTER_FILENAME).read_bytes()
    retry = promote_canonical_active_dataset(
        root,
        dataset_path.relative_to(root),
        readiness_path.relative_to(root),
        activated_at=ACTIVATED_AT,
    )

    assert retry == first
    assert (root / ACTIVE_DATASET_POINTER_FILENAME).read_bytes() == (
        original_pointer_bytes
    )
    assert not Path(first.dataset_path).is_absolute()
    assert not Path(first.readiness_path).is_absolute()
    assert "\\" not in first.dataset_path
    assert "\\" not in first.readiness_path
    assert len(list((root / "promotions").rglob("*.json"))) == 1

    moved_root = tmp_path / "moved-canonical"
    shutil.copytree(root, moved_root)
    resolved = resolve_active_canonical_dataset(moved_root)

    assert resolved.dataset == dataset
    assert resolved.readiness == readiness
    assert resolved.dataset_path.is_relative_to(moved_root.resolve())
    assert resolved.readiness_path.is_relative_to(moved_root.resolve())
    assert resolved.pointer.promotion_id == resolved.promotion.promotion_id


def test_promotion_rejects_readiness_for_another_dataset(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    first_dataset_path, _, _, _ = _write_pair(root, "one")
    _, second_readiness_path, _, _ = _write_pair(root, "two")

    with pytest.raises(CanonicalActivePointerError, match="different snapshots"):
        promote_canonical_active_dataset(
            root,
            first_dataset_path,
            second_readiness_path,
            activated_at=ACTIVATED_AT,
        )

    assert not (root / ACTIVE_DATASET_POINTER_FILENAME).exists()


def test_promotion_compare_and_swap_rejects_a_changed_active_dataset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    first_dataset_path, first_readiness_path, first, _ = _write_pair(root, "one")
    promote_canonical_active_dataset(
        root,
        first_dataset_path,
        first_readiness_path,
        activated_at=ACTIVATED_AT,
    )
    second_dataset_path, second_readiness_path, _, _ = _write_pair(root, "two")

    with pytest.raises(CanonicalActivePointerError, match="changed before promotion"):
        promote_canonical_active_dataset(
            root,
            second_dataset_path,
            second_readiness_path,
            activated_at=ACTIVATED_AT,
            expected_active_dataset_id="canonical-active-unrelated",
            expected_active_dataset_hash="0" * 64,
        )

    assert resolve_active_canonical_dataset(root).dataset == first


def test_promotion_compare_and_swap_is_idempotent_after_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    first_dataset_path, first_readiness_path, first, _ = _write_pair(root, "one")
    promote_canonical_active_dataset(
        root,
        first_dataset_path,
        first_readiness_path,
        activated_at=ACTIVATED_AT,
    )
    second_dataset_path, second_readiness_path, second, _ = _write_pair(root, "two")
    promoted = promote_canonical_active_dataset(
        root,
        second_dataset_path,
        second_readiness_path,
        activated_at=ACTIVATED_AT,
        expected_active_dataset_id=first.dataset_id,
        expected_active_dataset_hash=first.dataset_hash,
    )
    retried = promote_canonical_active_dataset(
        root,
        second_dataset_path,
        second_readiness_path,
        activated_at=ACTIVATED_AT,
        expected_active_dataset_id=first.dataset_id,
        expected_active_dataset_hash=first.dataset_hash,
    )

    assert retried == promoted
    assert resolve_active_canonical_dataset(root).dataset == second


def test_promotion_fails_closed_when_readiness_gate_is_not_ready(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(
        root,
        "blocked",
        publish_ready=False,
    )

    with pytest.raises(CanonicalDatasetNotReadyError, match="open vacancy"):
        promote_canonical_active_dataset(
            root,
            dataset_path,
            readiness_path,
            activated_at=ACTIVATED_AT,
        )

    assert not (root / ACTIVE_DATASET_POINTER_FILENAME).exists()


def test_pointer_rejects_non_portable_or_parent_paths(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "one")
    pointer = promote_canonical_active_dataset(
        root,
        dataset_path,
        readiness_path,
        activated_at=ACTIVATED_AT,
    )
    payload = pointer.model_dump(mode="json")
    payload["dataset_path"] = "../outside.json"

    with pytest.raises(ValidationError, match="relative to the canonical root"):
        CanonicalActiveDatasetPointer.model_validate(payload)

    payload["dataset_path"] = r"datasets\artifact.json"
    with pytest.raises(ValidationError, match="POSIX separators"):
        CanonicalActiveDatasetPointer.model_validate(payload)


def test_resolve_detects_tampered_dataset_even_if_pointer_is_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "one")
    promote_canonical_active_dataset(
        root,
        dataset_path,
        readiness_path,
        activated_at=ACTIVATED_AT,
    )
    document = json.loads(dataset_path.read_text(encoding="utf-8"))
    document["dataset_hash"] = "0" * 64
    dataset_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CanonicalActivePointerError, match="cannot validate"):
        resolve_active_canonical_dataset(root)


def test_failed_atomic_pointer_replace_preserves_last_known_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "canonical"
    first_dataset_path, first_readiness_path, _, _ = _write_pair(root, "one")
    first = promote_canonical_active_dataset(
        root,
        first_dataset_path,
        first_readiness_path,
        activated_at=ACTIVATED_AT,
    )
    pointer_path = root / ACTIVE_DATASET_POINTER_FILENAME
    original_pointer_bytes = pointer_path.read_bytes()
    second_dataset_path, second_readiness_path, _, _ = _write_pair(root, "two")
    real_replace = os.replace

    def fail_pointer_replace(source: Path, destination: Path) -> None:
        if Path(destination) == pointer_path:
            raise OSError("simulated pointer replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(active_pointer_module.os, "replace", fail_pointer_replace)

    with pytest.raises(OSError, match="simulated pointer replace failure"):
        promote_canonical_active_dataset(
            root,
            second_dataset_path,
            second_readiness_path,
            activated_at=ACTIVATED_AT,
        )

    assert pointer_path.read_bytes() == original_pointer_bytes
    assert read_canonical_active_dataset_pointer(pointer_path) == first


def test_existing_invalid_promotion_audit_is_never_replaced(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "one")
    pointer = promote_canonical_active_dataset(
        root,
        dataset_path,
        readiness_path,
        activated_at=ACTIVATED_AT,
    )
    promotion_path = next((root / "promotions").rglob("*.json"))
    promotion_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        CanonicalPromotionAlreadyExistsError,
        match="immutable promotion path is invalid",
    ):
        promote_canonical_active_dataset(
            root,
            dataset_path,
            readiness_path,
            activated_at=ACTIVATED_AT,
        )

    assert promotion_path.read_text(encoding="utf-8") == "{}"
    assert read_canonical_active_dataset_pointer(
        root / ACTIVE_DATASET_POINTER_FILENAME
    ) == pointer


def test_cli_promotes_and_resolves_dataset_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "cli")

    assert main(
        [
            "promote-canonical-dataset",
            "--canonical-root",
            str(root),
            "--dataset",
            str(dataset_path),
            "--readiness",
            str(readiness_path),
        ]
    ) == 0
    promote_output = capsys.readouterr().out
    assert f"dataset={dataset_path.resolve()}" in promote_output

    assert main(
        [
            "resolve-active-canonical-dataset",
            "--pointer",
            str(root / ACTIVE_DATASET_POINTER_FILENAME),
        ]
    ) == 0
    assert capsys.readouterr().out.strip() == f"dataset={dataset_path.resolve()}"


def test_cli_accepts_repository_relative_materialize_output_and_retry_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / "data" / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "relative-cli")
    monkeypatch.chdir(workspace)
    arguments = [
        "promote-canonical-dataset",
        "--canonical-root",
        "data/canonical",
        "--dataset",
        dataset_path.relative_to(workspace).as_posix(),
        "--readiness",
        readiness_path.relative_to(workspace).as_posix(),
    ]

    assert main(arguments) == 0
    pointer_path = root / ACTIVE_DATASET_POINTER_FILENAME
    first_bytes = pointer_path.read_bytes()
    first_promotions = list((root / "promotions").rglob("*.json"))
    assert main(arguments) == 0

    assert pointer_path.read_bytes() == first_bytes
    assert list((root / "promotions").rglob("*.json")) == first_promotions


def test_cli_non_ready_promotion_fails_cleanly_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(
        root,
        "not-ready-cli",
        publish_ready=False,
    )

    result = main(
        [
            "promote-canonical-dataset",
            "--canonical-root",
            str(root),
            "--dataset",
            str(dataset_path),
            "--readiness",
            str(readiness_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "Cannot promote canonical dataset" in captured.err
    assert "Traceback" not in captured.err
    assert not (root / ACTIVE_DATASET_POINTER_FILENAME).exists()


def test_cli_non_ready_resolve_fails_cleanly_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pointer_path = tmp_path / "canonical" / ACTIVE_DATASET_POINTER_FILENAME

    def reject(_: Path) -> None:
        raise CanonicalDatasetNotReadyError("not publish-ready")

    monkeypatch.setattr(cli_module, "resolve_active_canonical_dataset", reject)

    result = main(
        [
            "resolve-active-canonical-dataset",
            "--pointer",
            str(pointer_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "Cannot resolve canonical dataset" in captured.err
    assert "Traceback" not in captured.err


def test_concurrent_retry_creates_one_semantic_promotion(tmp_path: Path) -> None:
    root = tmp_path / "canonical"
    dataset_path, readiness_path, _, _ = _write_pair(root, "concurrent")

    def promote(_: int) -> CanonicalActiveDatasetPointer:
        return promote_canonical_active_dataset(
            root,
            dataset_path,
            readiness_path,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        pointers = list(executor.map(promote, range(8)))

    assert len({item.promotion_id for item in pointers}) == 1
    assert len(list((root / "promotions").rglob("*.json"))) == 1
