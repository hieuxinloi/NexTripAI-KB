from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical import hotel_identity_replacement as replacement_module
from nextrip_pipeline.canonical import (
    hotel_identity_replacement_rollback as rollback_module,
)
from nextrip_pipeline.canonical.hotel_identity_replacement import (
    HotelIdentityReplacementConfig,
    load_and_validate_replacements,
    replace_hotel_identities,
)
from nextrip_pipeline.canonical.hotel_identity_replacement_rollback import (
    HotelIdentityReplacementRollbackError,
    rollback_hotel_identity_replacement,
)
from tests.pipeline.canonical.test_hotel_identity_replacement import (
    TARGET_ID,
    _create_repository,
    _legacy_master_path,
)


def test_rollback_requires_explicit_legacy_master() -> None:
    batch_hash = "0" * 64
    with pytest.raises(TypeError, match="master_path"):
        rollback_hotel_identity_replacement(  # type: ignore[call-arg]
            batch_hash=batch_hash
        )

    with pytest.raises(SystemExit, match="2"):
        rollback_module.build_parser().parse_args(["--batch-hash", batch_hash])


def test_optional_null_location_evidence_preserves_legacy_batch_payload(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    master_path = _legacy_master_path(tmp_path)
    checked = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
    )
    config = HotelIdentityReplacementConfig.model_validate_json(
        (tmp_path / "config/hotel-identity-replacements.json").read_bytes()
    )
    _, validated, _, _ = load_and_validate_replacements(
        repository_root=tmp_path,
        config_path="config/hotel-identity-replacements.json",
        master_path=master_path,
        current_mapping_directory="data/current/trivago_mappings",
    )
    serialized_proposals = [item.proposal.model_dump(mode="json") for item in validated]
    assert serialized_proposals[0]["location_evidence"] is None
    legacy_proposals = [
        {key: value for key, value in proposal.items() if key != "location_evidence"}
        for proposal in serialized_proposals
    ]
    expected_hash = replacement_module._stable_sha256(
        {
            "schema_version": config.schema_version,
            "approved_by": config.approved_by,
            "approved_at": config.approved_at.isoformat(),
            "reason": config.reason,
            "proposal_artifacts": [
                item.model_dump(mode="json") for item in config.proposal_artifacts
            ],
            "proposals": legacy_proposals,
        }
    )

    assert checked.batch_hash == expected_hash


def test_rollback_restores_exact_preimage_and_retains_source_artifacts(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    master_path = _legacy_master_path(tmp_path)
    master_preimage = master_path.read_bytes()
    applied = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    assert applied.audit_path is not None
    source_audit_path = tmp_path / applied.audit_path
    source_audit = json.loads(source_audit_path.read_text(encoding="utf-8"))
    source_intent_path = tmp_path / source_audit["intent_path"]
    source_audit_bytes = source_audit_path.read_bytes()
    source_intent_bytes = source_intent_path.read_bytes()
    removed_place_path = tmp_path / f"data/current/place/{TARGET_ID}.json"
    assert not removed_place_path.exists()

    checked = rollback_hotel_identity_replacement(
        repository_root=tmp_path,
        batch_hash=applied.batch_hash,
        master_path=master_path,
    )
    assert checked.status == "eligible"
    assert master_path.read_bytes() != master_preimage

    rolled_back = rollback_hotel_identity_replacement(
        repository_root=tmp_path,
        batch_hash=applied.batch_hash,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 23, 4, tzinfo=timezone.utc),
    )

    assert rolled_back.status == "rolled_back"
    assert master_path.read_bytes() == master_preimage
    assert source_audit_path.read_bytes() == source_audit_bytes
    assert source_intent_path.read_bytes() == source_intent_bytes
    assert not removed_place_path.exists()
    assert rolled_back.rollback_intent_path is not None
    assert rolled_back.rollback_audit_path is not None
    assert (tmp_path / rolled_back.rollback_intent_path).is_file()
    rollback_audit_path = tmp_path / rolled_back.rollback_audit_path
    rollback_audit = json.loads(rollback_audit_path.read_text(encoding="utf-8"))
    assert rollback_audit["master_before_rollback_sha256"] == (
        applied.master_after_sha256
    )
    assert rollback_audit["master_after_rollback_sha256"] == (
        applied.master_before_sha256
    )

    retried = rollback_hotel_identity_replacement(
        repository_root=tmp_path,
        batch_hash=applied.batch_hash,
        master_path=master_path,
        apply=True,
    )
    assert retried.status == "already_rolled_back"
    assert master_path.read_bytes() == master_preimage


def test_rollback_rejects_master_drift_before_writing_rollback_artifacts(
    tmp_path: Path,
) -> None:
    _create_repository(tmp_path)
    master_path = _legacy_master_path(tmp_path)
    applied = replace_hotel_identities(
        repository_root=tmp_path,
        master_path=master_path,
        apply=True,
        now=datetime(2026, 8, 23, 3, tzinfo=timezone.utc),
    )
    master_path.write_bytes(master_path.read_bytes() + b" ")

    with pytest.raises(
        HotelIdentityReplacementRollbackError,
        match="current master SHA does not match",
    ):
        rollback_hotel_identity_replacement(
            repository_root=tmp_path,
            batch_hash=applied.batch_hash,
            master_path=master_path,
            apply=True,
        )

    audit_root = tmp_path / "data/audit/hotel_identity_replacements"
    assert not list(audit_root.glob("rollback-intent=*.json"))
    assert not list(audit_root.glob("rollback=*.json"))
