from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDatasetWriter,
    CanonicalDatasetAlreadyExistsError,
    CanonicalDatasetMaterializationError,
    CanonicalRecordSource,
    materialize_canonical_active_dataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import (
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    VacancyReplacementDecision,
    stable_identifier,
)
from nextrip_pipeline.canonical.projection import (
    MaterializedReplacementRecord,
    ReplacementApprovalMethod,
    ReplacementCoordinates,
    ReplacementProvenance,
    ReplacementSource,
)
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.schemas import EntityType, VerificationStatus


UTC = timezone.utc
GENERATED_AT = datetime(2026, 8, 20, 4, tzinfo=UTC)
APPROVED_AT = GENERATED_AT + timedelta(hours=1)
GOOGLE_TOKEN = "0x314219caaa000001:0x1000000000000001"
GOOGLE_URL = (
    "https://www.google.com/maps/place/New+Cafe/"
    "@16.051,108.202,17z/data=!3m1!4b1!4m6!3m5!1s"
    f"{GOOGLE_TOKEN}!8m2"
)


def _raw(
    place_id: str,
    entity_type: EntityType,
    name: str,
    *,
    index: int,
    latitude: float = 16.05,
) -> MasterRawRecord:
    return MasterRawRecord(
        place_id=place_id,
        source_filename=(
            "cafe_final.json"
            if entity_type is EntityType.CAFE
            else "nightlife_final.json"
        ),
        record_index=index,
        raw_record={
            "id": place_id,
            "entity_type": entity_type.value,
            "name": name,
            "city": "Đà Nẵng",
            "address": f"{index + 1} Canonical Street",
            "coordinates": {"lat": latitude, "lng": 108.2},
            "category": entity_type.value,
            "aliases": [f"Known {name}"],
            "tags": ["verified"],
            "entity_specific": {"retained": True, "source_index": index},
        },
    )


def _master(slots: list[LegacyPlaceSlot], raws: list[MasterRawRecord]):
    return CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={item.place_id: item for item in raws},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )


def _cross_type_inputs():
    slots = [
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
            tags=["keeper"],
        ),
        LegacyPlaceSlot(
            legacy_place_id="night_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.NIGHTLIFE,
            tags=["night"],
        ),
    ]
    raws = [
        _raw("cafe_dn_001", EntityType.CAFE, "Canonical Cafe", index=0),
        _raw("night_dn_001", EntityType.NIGHTLIFE, "Night Alias", index=1),
    ]
    master = _master(slots, raws)
    manifest = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=[
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
                reason="reviewed physical duplicate",
            )
        ],
        generated_at=GENERATED_AT,
    )
    return master, manifest


def _replacement(
    retired_place_id: str = "cafe_dn_002",
    *,
    vacancy_entity_type: EntityType = EntityType.CAFE,
    entity_type: EntityType = EntityType.CAFE,
    allocated_place_id: str = "cafe_dn_100",
):
    vacancy_id = stable_identifier(
        "vacancy",
        "city_da_nang",
        vacancy_entity_type.value,
        retired_place_id,
    )
    return MaterializedReplacementRecord(
        id=allocated_place_id,
        entity_type=entity_type,
        primary_type=entity_type,
        place_types=[entity_type],
        name="New Google Cafe",
        city="Đà Nẵng",
        city_id="city_da_nang",
        address="100 Replacement Street",
        coordinates=ReplacementCoordinates(lat=16.051, lng=108.202),
        phone="0905000100",
        website_url="https://replacement.example",
        tags=["canonical_replacement"],
        source=ReplacementSource(url=GOOGLE_URL, crawled_at=GENERATED_AT),
        embedding_text="New Google Cafe | Đà Nẵng | cafe",
        verification_status=VerificationStatus.AUTO_VERIFIED,
        last_updated=APPROVED_AT,
        provenance=ReplacementProvenance(
            approval_id="replacement-approval-test",
            approval_hash="a" * 64,
            candidate_detail_id="candidate-detail-test",
            candidate_detail_hash="b" * 64,
            candidate_key="candidate-new-google-cafe",
            source_ids=["google-maps-web"],
            source_record_ids=["source-google-cafe"],
            observation_ids=["observation-google-cafe"],
            vacancy_id=vacancy_id,
            replacement_of=retired_place_id,
            google_external_id=GOOGLE_TOKEN,
            google_external_url=GOOGLE_URL,
            approval_method=ReplacementApprovalMethod.DETERMINISTIC,
            reviewer="canonical-distinct-gate-v1",
            approved_at=APPROVED_AT,
        ),
    )


def _filled_inputs():
    slots = [
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_001",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
        LegacyPlaceSlot(
            legacy_place_id="cafe_dn_002",
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
        ),
    ]
    raws = [
        _raw("cafe_dn_001", EntityType.CAFE, "Keeper Cafe", index=0),
        _raw("cafe_dn_002", EntityType.CAFE, "Duplicate Cafe", index=1),
    ]
    master = _master(slots, raws)
    duplicate = DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_dn_001",
        duplicate_legacy_place_ids=["cafe_dn_002"],
        reason="reviewed physical duplicate",
    )
    initial = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=[duplicate],
        generated_at=GENERATED_AT,
    )
    replacement = _replacement()
    filled = build_canonical_identity_manifest(
        [*slots, replacement.to_legacy_place_slot()],
        duplicate_decisions=[duplicate],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="cafe_dn_002",
                replacement_place_id="cafe_dn_100",
            )
        ],
        previous_manifest=initial,
        generated_at=APPROVED_AT,
    )
    return master, filled, replacement


def _cross_type_filled_inputs():
    master, initial = _cross_type_inputs()
    replacement = _replacement(
        "night_dn_001",
        vacancy_entity_type=EntityType.NIGHTLIFE,
    )
    duplicate = DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_dn_001",
        duplicate_legacy_place_ids=["night_dn_001"],
        reason="reviewed physical duplicate",
    )
    filled = build_canonical_identity_manifest(
        [*master.slots, replacement.to_legacy_place_slot()],
        duplicate_decisions=[duplicate],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="night_dn_001",
                replacement_place_id="cafe_dn_100",
            )
        ],
        previous_manifest=initial,
        generated_at=APPROVED_AT,
    )
    return master, filled, replacement


def test_materializes_one_active_record_and_preserves_aliases_and_provenance() -> None:
    master, manifest = _cross_type_inputs()
    before = master.model_dump(mode="json")

    dataset = materialize_canonical_active_dataset(master, manifest)

    assert master.model_dump(mode="json") == before
    assert [item.place_id for item in dataset.records] == ["cafe_dn_001"]
    record = dataset.records[0]
    assert record.primary_type is EntityType.CAFE
    assert record.secondary_types == [EntityType.NIGHTLIFE]
    assert record.place_types == [EntityType.CAFE, EntityType.NIGHTLIFE]
    assert record.aliases == ["Known Canonical Cafe", "Known Night Alias", "Night Alias"]
    assert record.data["entity_specific"] == {
        "retained": True,
        "source_index": 0,
    }
    assert record.data["id"] == "cafe_dn_001"
    assert record.data["place_types"] == ["cafe", "nightlife"]
    assert record.provenance.source_kind is CanonicalRecordSource.VERIFIED_MASTER
    assert record.provenance.legacy_place_ids == [
        "cafe_dn_001",
        "night_dn_001",
    ]
    assert record.provenance.retired_alias_ids == ["night_dn_001"]
    assert [item.legacy_place_id for item in record.provenance.master_records] == [
        "cafe_dn_001",
        "night_dn_001",
    ]
    assert dataset.report.canonical_record_count == 1
    assert dataset.report.retired_duplicate_count == 1
    assert dataset.report.open_vacancy_count == 1
    assert dataset.report.filled_vacancy_count == 0
    assert dataset.report.master_materialized_count == 1
    assert dataset.report.replacement_materialized_count == 0
    assert dataset.report.entity_city_counts[0].count == 1


def test_filled_overlay_becomes_active_record_and_retired_duplicate_is_excluded() -> None:
    master, manifest, replacement = _filled_inputs()

    dataset = materialize_canonical_active_dataset(
        master,
        manifest,
        approved_replacements=[replacement],
    )

    assert [item.place_id for item in dataset.records] == [
        "cafe_dn_001",
        "cafe_dn_100",
    ]
    assert "cafe_dn_002" not in {item.place_id for item in dataset.records}
    projected = dataset.records[1]
    assert projected.name == "New Google Cafe"
    assert projected.address == "100 Replacement Street"
    assert projected.coordinates.source == "google-maps-web"
    assert projected.provenance.source_kind is (
        CanonicalRecordSource.APPROVED_REPLACEMENT
    )
    assert projected.provenance.replacement_of == "cafe_dn_002"
    assert projected.provenance.approval_id == "replacement-approval-test"
    assert projected.provenance.source_record_ids == ["source-google-cafe"]
    assert projected.external_identities[0]["external_id"] == GOOGLE_TOKEN
    assert dataset.report.approved_replacement_count == 1
    assert dataset.report.master_materialized_count == 1
    assert dataset.report.replacement_materialized_count == 1
    assert dataset.report.open_vacancy_count == 0
    assert dataset.report.filled_vacancy_count == 1
    quota = dataset.report.quotas[0]
    assert (quota.target_count, quota.active_count, quota.vacancy_count) == (2, 2, 0)


def test_cross_type_fill_materializes_actual_type_and_preserves_total_capacity() -> None:
    master, manifest, replacement = _cross_type_filled_inputs()

    dataset = materialize_canonical_active_dataset(
        master,
        manifest,
        approved_replacements=[replacement],
    )

    assert [item.place_id for item in dataset.records] == [
        "cafe_dn_001",
        "cafe_dn_100",
    ]
    added = dataset.records[1]
    assert added.primary_type is EntityType.CAFE
    assert added.data["entity_type"] == "cafe"
    assert added.provenance.replacement_of == "night_dn_001"
    assert dataset.report.canonical_record_count == 2
    assert dataset.report.filled_vacancy_count == 1
    assert dataset.report.open_vacancy_count == 0
    assert sum(item.target_count for item in dataset.report.quotas) == 2
    assert len(dataset.report.entity_city_counts) == 1
    assert dataset.report.entity_city_counts[0].count == 2
    assert dataset.report.entity_city_counts[0].entity_type is EntityType.CAFE


def test_missing_active_source_fails_instead_of_emitting_partial_dataset() -> None:
    slot = LegacyPlaceSlot(
        legacy_place_id="cafe_dn_001",
        city_id="city_da_nang",
        primary_type=EntityType.CAFE,
    )
    master = _master([slot], [])
    manifest = build_canonical_identity_manifest(
        [slot],
        generated_at=GENERATED_AT,
    )

    with pytest.raises(CanonicalDatasetMaterializationError, match="master record"):
        materialize_canonical_active_dataset(master, manifest)


def test_dataset_hash_and_immutable_writer_are_deterministic(tmp_path: Path) -> None:
    master, manifest = _cross_type_inputs()
    first = materialize_canonical_active_dataset(master, manifest)
    retry = materialize_canonical_active_dataset(master, manifest)
    writer = CanonicalActiveDatasetWriter(tmp_path / "canonical")

    assert retry.dataset_hash == first.dataset_hash
    assert retry.report.report_hash == first.report.report_hash
    path = writer.write(first)
    original_bytes = path.read_bytes()
    assert writer.write(retry) == path
    assert path.read_bytes() == original_bytes
    assert read_canonical_active_dataset(path) == first

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(CanonicalDatasetAlreadyExistsError, match="invalid"):
        writer.write(first)
