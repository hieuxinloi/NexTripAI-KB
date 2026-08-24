from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.quality import (
    CurrentTrivagoMappingWriter,
    TrivagoCandidateEvidence,
    TrivagoDiscoveryResolution,
    TrivagoDiscoveryStatus,
    TrivagoMappingApproval,
    TrivagoMappingApprovalError,
    TrivagoMappingApprovalWriter,
    approve_trivago_review,
    compute_trivago_resolution_evidence_hash,
)
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping, MappingStatus


NOW = datetime(2026, 8, 20, 9, tzinfo=timezone.utc)


def _resolution(
    *,
    entity_id: str = "hotel_dn_004",
    external_id: str = "trivago-new-004",
    status: TrivagoDiscoveryStatus = TrivagoDiscoveryStatus.REVIEW,
    resolver_version: str = "1.2.0",
) -> TrivagoDiscoveryResolution:
    candidate = TrivagoCandidateEvidence(
        external_id=external_id,
        name="Carol Homestay & Apartment Da Nang 4",
        location_text="Da Nang",
        external_url=f"https://www.trivago.vn/vi/oar/{external_id}",
        latitude=16.05,
        longitude=108.21,
        distance_from_master_m=2.0,
        name_score=0.97,
        city_evidence="match",
    )
    resolution = TrivagoDiscoveryResolution(
        entity_id=entity_id,
        source_record_id=f"source-{entity_id}",
        status=status,
        resolved_at=NOW,
        selected_external_id=external_id,
        selected_external_url=candidate.external_url,
        selected_name=candidate.name,
        confidence=0.97,
        reason_codes=["candidate_requires_review"],
        returned_candidate_count=1,
        candidates=[candidate],
        resolver_version=resolver_version,
        evidence_hash="0" * 64,
    )
    return resolution.model_copy(
        update={"evidence_hash": compute_trivago_resolution_evidence_hash(resolution)}
    )


def _entry(
    entity_id: str = "hotel_dn_004",
    *,
    status: TrivagoRegistryStatus = TrivagoRegistryStatus.UNRESOLVED,
    external_id: str | None = None,
) -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id=entity_id,
        master_name="Carol's Homestay & Apartment Da Nang 4",
        city="Da Nang",
        address="45 Test Street",
        latitude=16.05,
        longitude=108.21,
        search_query="Carol's Homestay & Apartment Da Nang 4, Da Nang, Vietnam",
        status=status,
        external_id=external_id,
        external_url=(
            f"https://www.trivago.vn/vi/oar/{external_id}" if external_id else None
        ),
        matched_at=NOW if external_id else None,
        verified_at=NOW if status is TrivagoRegistryStatus.CONFIRMED else None,
    )


def _write_inputs(tmp_path, resolution, entries):
    resolution_path = tmp_path / "resolution.json"
    registry_path = tmp_path / "registry.json"
    resolution_path.write_text(
        resolution.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=entries,
    )
    registry_path.write_text(
        registry.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return resolution_path, registry_path


def _mapping(entity_id: str, external_id: str) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"trivago-mcp-{entity_id}",
        entity_id=entity_id,
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id=external_id,
        external_url=f"https://www.trivago.vn/vi/oar/{external_id}",
        status=MappingStatus.CONFIRMED,
        confidence=1.0,
        matched_at=NOW,
        verified_at=NOW,
        last_checked_at=NOW,
        source_record_ids=[f"source-{entity_id}-old"],
        attributes={"trivago_name": "Old provider name"},
    )


def test_review_approval_is_immutable_and_materializes_audited_mapping(
    tmp_path,
) -> None:
    resolution_path, registry_path = _write_inputs(tmp_path, _resolution(), [_entry()])
    approval_writer = TrivagoMappingApprovalWriter(tmp_path / "approvals")
    mapping_writer = CurrentTrivagoMappingWriter(tmp_path / "mappings")

    approval, mapping, approval_path, mapping_path = approve_trivago_review(
        resolution_path,
        registry_path,
        reviewer="Oanhh",
        approval_writer=approval_writer,
        mapping_writer=mapping_writer,
        approved_at=NOW + timedelta(hours=1),
    )

    stored = TrivagoMappingApproval.model_validate_json(approval_path.read_bytes())
    assert stored == approval
    assert (
        approval.resolution_file_sha256
        == hashlib.sha256(resolution_path.read_bytes()).hexdigest()
    )
    assert (
        approval.registry_file_sha256
        == hashlib.sha256(registry_path.read_bytes()).hexdigest()
    )
    assert mapping_path == mapping_writer.path_for("hotel_dn_004")
    assert mapping.attributes["human_mapping_reviewer"] == "Oanhh"
    assert mapping.attributes["human_mapping_approval_hash"] == approval.approval_hash
    assert mapping.attributes["trivago_name"] == approval.trivago_name
    assert mapping_writer.get("hotel_dn_004") == mapping
    immutable_bytes = approval_path.read_bytes()

    repeated = approve_trivago_review(
        resolution_path,
        registry_path,
        reviewer="Oanhh",
        approval_writer=approval_writer,
        mapping_writer=mapping_writer,
        approved_at=NOW + timedelta(hours=1),
    )
    assert approval_path.read_bytes() == immutable_bytes
    assert repeated[2].exists()
    assert repeated[3] == mapping_path


def test_review_approval_canonicalizes_unicode_trivago_url_before_hashing(
    tmp_path,
) -> None:
    resolution = _resolution()
    unicode_url = (
        "https://www.trivago.vn/vi/lm/căn-hộ-dịch-vụ-carol-đà-nẵng"
        "?currencyCode=VND&search=100-34929924"
    )
    candidate = resolution.candidates[0].model_copy(
        update={"external_url": unicode_url}
    )
    resolution = resolution.model_copy(
        update={
            "selected_external_url": unicode_url,
            "candidates": [candidate],
            "evidence_hash": "0" * 64,
        }
    )
    resolution = resolution.model_copy(
        update={"evidence_hash": compute_trivago_resolution_evidence_hash(resolution)}
    )
    resolution_path, registry_path = _write_inputs(tmp_path, resolution, [_entry()])

    approval, mapping, _, _ = approve_trivago_review(
        resolution_path,
        registry_path,
        reviewer="Oanhh",
        approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
        mapping_writer=CurrentTrivagoMappingWriter(tmp_path / "mappings"),
        approved_at=NOW + timedelta(hours=1),
    )

    assert "%C4%83n-h%E1%BB%99" in str(approval.external_url)
    assert str(mapping.external_url) == str(approval.external_url)


def test_approval_rejects_tampered_resolution_evidence(tmp_path) -> None:
    resolution_path, registry_path = _write_inputs(tmp_path, _resolution(), [_entry()])
    document = json.loads(resolution_path.read_text(encoding="utf-8"))
    document["candidates"][0]["name"] = "Different hotel"
    resolution_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(TrivagoMappingApprovalError, match="evidence_hash"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
            mapping_writer=CurrentTrivagoMappingWriter(tmp_path / "mappings"),
            approved_at=NOW + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    [
        ("confidence", 0.1),
        ("returned_candidate_count", 99),
    ],
)
def test_v13_approval_rejects_tampered_decision_fields(
    tmp_path,
    field,
    tampered_value,
) -> None:
    resolution = _resolution(resolver_version="1.3.0")
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        resolution.model_copy(update={field: tampered_value}),
        [_entry()],
    )

    with pytest.raises(TrivagoMappingApprovalError, match="evidence_hash"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
            mapping_writer=CurrentTrivagoMappingWriter(tmp_path / "mappings"),
            approved_at=NOW + timedelta(hours=1),
        )


def test_v13_approval_rejects_switching_selected_url_between_candidates(
    tmp_path,
) -> None:
    resolution = _resolution(resolver_version="1.3.0")
    alternate_url = "https://www.trivago.vn/vi/oar/trivago-new-004?search=100-99999999"
    alternate = resolution.candidates[0].model_copy(
        update={"external_url": alternate_url}
    )
    resolution = resolution.model_copy(
        update={
            "candidates": [*resolution.candidates, alternate],
            "returned_candidate_count": 2,
            "evidence_hash": "0" * 64,
        }
    )
    resolution = resolution.model_copy(
        update={"evidence_hash": compute_trivago_resolution_evidence_hash(resolution)}
    )
    tampered = resolution.model_copy(update={"selected_external_url": alternate_url})
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        tampered,
        [_entry()],
    )

    with pytest.raises(TrivagoMappingApprovalError, match="evidence_hash"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
            mapping_writer=CurrentTrivagoMappingWriter(tmp_path / "mappings"),
            approved_at=NOW + timedelta(hours=1),
        )


def test_legacy_v12_approval_keeps_original_evidence_hash_contract(tmp_path) -> None:
    resolution = _resolution(resolver_version="1.2.0")
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        resolution,
        [_entry()],
    )

    approval, mapping, _, _ = approve_trivago_review(
        resolution_path,
        registry_path,
        reviewer="Oanhh",
        approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
        mapping_writer=CurrentTrivagoMappingWriter(tmp_path / "mappings"),
        approved_at=NOW + timedelta(hours=1),
    )

    assert approval.resolver_version == "1.2.0"
    assert approval.resolution_evidence_hash == resolution.evidence_hash
    assert mapping.external_id == resolution.selected_external_id


def test_external_id_change_requires_explicit_flag_and_pins_previous_file(
    tmp_path,
) -> None:
    old_external_id = "trivago-old-004"
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        _resolution(external_id="trivago-new-004"),
        [
            _entry(
                status=TrivagoRegistryStatus.CONFIRMED,
                external_id=old_external_id,
            )
        ],
    )
    mapping_writer = CurrentTrivagoMappingWriter(tmp_path / "mappings")
    old_mapping_path = mapping_writer.publish(_mapping("hotel_dn_004", old_external_id))
    old_mapping_hash = hashlib.sha256(old_mapping_path.read_bytes()).hexdigest()
    approval_writer = TrivagoMappingApprovalWriter(tmp_path / "approvals")

    with pytest.raises(TrivagoMappingApprovalError, match="--allow-external-id-change"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=approval_writer,
            mapping_writer=mapping_writer,
            approved_at=NOW + timedelta(hours=1),
        )
    assert not (tmp_path / "approvals").exists()

    approval, mapping, _, _ = approve_trivago_review(
        resolution_path,
        registry_path,
        reviewer="Oanhh",
        approval_writer=approval_writer,
        mapping_writer=mapping_writer,
        allow_external_id_change=True,
        approved_at=NOW + timedelta(hours=1),
    )
    assert approval.external_id_change_approved is True
    assert approval.previous_external_id == old_external_id
    assert approval.previous_mapping_file_sha256 == old_mapping_hash
    assert mapping.external_id == "trivago-new-004"


def test_approval_rejects_external_id_owned_by_another_hotel(tmp_path) -> None:
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        _resolution(external_id="shared-trivago-id"),
        [_entry(), _entry("hotel_dn_099")],
    )
    mapping_writer = CurrentTrivagoMappingWriter(tmp_path / "mappings")
    mapping_writer.publish(_mapping("hotel_dn_099", "shared-trivago-id"))

    with pytest.raises(TrivagoMappingApprovalError, match="another hotel"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=TrivagoMappingApprovalWriter(tmp_path / "approvals"),
            mapping_writer=mapping_writer,
            approved_at=NOW + timedelta(hours=1),
        )


def test_approval_rejects_property_owned_by_another_hotel_before_writing_audit(
    tmp_path,
) -> None:
    property_url = "https://www.trivago.vn/vi/oar/hotel?search=100-19017974"
    resolution = _resolution(external_id="new-external-id")
    candidate = resolution.candidates[0].model_copy(
        update={"external_url": property_url}
    )
    resolution = resolution.model_copy(
        update={
            "selected_external_url": property_url,
            "candidates": [candidate],
            "evidence_hash": "0" * 64,
        }
    )
    resolution = resolution.model_copy(
        update={"evidence_hash": compute_trivago_resolution_evidence_hash(resolution)}
    )
    resolution_path, registry_path = _write_inputs(
        tmp_path,
        resolution,
        [_entry()],
    )
    mapping_writer = CurrentTrivagoMappingWriter(tmp_path / "mappings")
    existing = _mapping("hotel_dn_099", "other-external-id")
    mapping_writer.publish(
        ExternalEntityMapping.model_validate(
            {**existing.model_dump(mode="python"), "external_url": property_url}
        )
    )
    approval_root = tmp_path / "approvals"

    with pytest.raises(TrivagoMappingApprovalError, match="property_id"):
        approve_trivago_review(
            resolution_path,
            registry_path,
            reviewer="Oanhh",
            approval_writer=TrivagoMappingApprovalWriter(approval_root),
            mapping_writer=mapping_writer,
            approved_at=NOW + timedelta(hours=1),
        )

    assert not approval_root.exists()


def test_approve_trivago_mapping_cli_materializes_mapping(tmp_path) -> None:
    resolution_path, registry_path = _write_inputs(tmp_path, _resolution(), [_entry()])

    exit_code = cli_module.main(
        [
            "approve-trivago-mapping",
            "--resolution",
            str(resolution_path),
            "--registry",
            str(registry_path),
            "--reviewer",
            "Oanhh",
            "--approval-dir",
            str(tmp_path / "approvals"),
            "--current-mapping-dir",
            str(tmp_path / "mappings"),
        ]
    )

    assert exit_code == 0
    assert (tmp_path / "mappings" / "hotel_dn_004.json").exists()


def test_same_identity_refresh_preserves_human_approval_provenance(
    tmp_path,
) -> None:
    writer = CurrentTrivagoMappingWriter(tmp_path / "mappings")
    approved = _mapping("hotel_dn_004", "stable-id").model_copy(
        update={
            "attributes": {
                "trivago_name": "Approved provider name",
                "human_mapping_approval_id": "approval-1",
                "human_mapping_approval_hash": "a" * 64,
                "previous_external_id": "old-id",
            }
        }
    )
    writer.publish(approved)
    refreshed = approved.model_copy(
        update={
            "verified_at": NOW + timedelta(hours=1),
            "last_checked_at": NOW + timedelta(hours=1),
            "source_record_ids": ["fresh-source"],
            "attributes": {
                "trivago_name": "Current provider name",
                "search_query": "Current provider name, Da Nang",
            },
        }
    )

    writer.publish(refreshed)
    current = writer.get("hotel_dn_004")

    assert current is not None
    assert current.attributes["human_mapping_approval_id"] == "approval-1"
    assert current.attributes["human_mapping_approval_hash"] == "a" * 64
    assert current.attributes["previous_external_id"] == "old-id"
    assert current.attributes["trivago_name"] == "Current provider name"
    assert current.source_record_ids == ["source-hotel_dn_004-old", "fresh-source"]
    assert current.matched_at == NOW
