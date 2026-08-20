from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.manifest import CanonicalIdentityManifestWriter
from nextrip_pipeline.canonical.master import (
    MASTER_FILES,
    CanonicalMasterDataError,
    build_manifest_from_master,
    load_duplicate_identity_decisions,
    load_verified_master,
)
from nextrip_pipeline.canonical.models import stable_identifier
from nextrip_pipeline.canonical.projection import (
    MaterializedReplacementRecord,
    ReplacementApprovalMethod,
    ReplacementCoordinates,
    ReplacementProvenance,
    ReplacementSource,
)
from nextrip_pipeline.canonical.resolver import CanonicalIdentityError
from nextrip_pipeline.schemas import EntityType, VerificationStatus


UTC = timezone.utc
GENERATED_AT = datetime(2026, 8, 20, 10, tzinfo=UTC)


def _record(
    place_id: str,
    entity_type: EntityType,
    city: str,
    *,
    tags: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": place_id,
        "entity_type": entity_type.value,
        "name": f"Place {place_id}",
        "city": city,
        "coordinates": {"lat": 16.05, "lng": 108.2},
        "tags": tags or ["verified"],
    }


def _base_records() -> dict[EntityType, list[dict[str, object]]]:
    return {
        EntityType.ATTRACTION: [
            _record("attr_dn_001", EntityType.ATTRACTION, "Đà Nẵng"),
            _record(
                "attr_dn_002",
                EntityType.ATTRACTION,
                "Đà Nẵng",
                tags=[
                    "must_try",
                    "duplicate_record",
                    "duplicate_of_attr_dn_001",
                ],
            ),
        ],
        EntityType.CAFE: [
            _record("cafe_qn_001", EntityType.CAFE, "Quy Nhơn"),
        ],
        EntityType.HOTEL: [
            _record("hotel_dn_001", EntityType.HOTEL, "Đà Nẵng"),
        ],
        EntityType.NIGHTLIFE: [
            _record("night_qn_001", EntityType.NIGHTLIFE, "Quy Nhơn"),
        ],
        EntityType.RESTAURANT: [
            _record("rest_dn_001", EntityType.RESTAURANT, "Đà Nẵng"),
        ],
    }


def _write_master(
    root: Path,
    records: dict[EntityType, list[dict[str, object]]] | None = None,
) -> dict[EntityType, Path]:
    records = records or _base_records()
    paths: dict[EntityType, Path] = {}
    for entity_type, filename, _ in MASTER_FILES:
        items = records[entity_type]
        city_counts = {
            "Đà Nẵng": sum(
                unicodedata.normalize("NFC", str(item["city"])) == "Đà Nẵng"
                for item in items
            ),
            "Quy Nhơn": sum(
                unicodedata.normalize("NFC", str(item["city"])) == "Quy Nhơn"
                for item in items
            ),
        }
        document = {
            "metadata": {
                "entity_type": entity_type.value,
                "total_count": len(items),
                "da_nang_count": city_counts["Đà Nẵng"],
                "quy_nhon_count": city_counts["Quy Nhơn"],
            },
            "data": items,
        }
        path = root / filename
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paths[entity_type] = path
    return paths


def _write_decisions(
    path: Path,
    *,
    decisions: list[dict[str, object]] | None = None,
    replacements: list[dict[str, object]] | None = None,
    distinct_decisions: list[dict[str, object]] | None = None,
    schema_version: str = "1.0.0",
) -> Path:
    document: dict[str, object] = {
        "schema_version": schema_version,
        "decisions": (
            decisions
            if decisions is not None
            else [
                {
                    "keeper_legacy_place_id": "cafe_qn_001",
                    "duplicate_legacy_place_ids": ["night_qn_001"],
                    "reason": "reviewed_cross_type_duplicate",
                }
            ]
        ),
    }
    if replacements is not None:
        document["replacements"] = replacements
    if distinct_decisions is not None:
        document["distinct_decisions"] = distinct_decisions
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_decision_document_accepts_optional_distinct_decisions_and_old_shape(
    tmp_path: Path,
) -> None:
    old_shape = load_duplicate_identity_decisions(
        _write_decisions(tmp_path / "old.json", decisions=[])
    )
    assert old_shape.distinct_decisions == []

    path = _write_decisions(
        tmp_path / "new.json",
        decisions=[],
        distinct_decisions=[
            {
                "place_ids": ["night_dn_002", "cafe_dn_002"],
                "reason": "human_review_verified_distinct",
            }
        ],
    )
    loaded = load_duplicate_identity_decisions(path)

    assert loaded.distinct_decisions[0].place_ids == [
        "cafe_dn_002",
        "night_dn_002",
    ]


def test_decision_document_rejects_duplicate_distinct_groups(tmp_path: Path) -> None:
    path = _write_decisions(
        tmp_path / "duplicate-distinct.json",
        decisions=[],
        distinct_decisions=[
            {"place_ids": ["cafe_dn_002", "night_dn_002"]},
            {"place_ids": ["night_dn_002", "cafe_dn_002"]},
        ],
    )

    with pytest.raises(CanonicalMasterDataError, match="one distinct decision"):
        load_duplicate_identity_decisions(path)


def _file_hashes(paths: dict[EntityType, Path]) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths.values()
    }


def _write_approved_nightlife_replacement(root: Path) -> MaterializedReplacementRecord:
    token = "0x314219caaa000001:0x1000000000000001"
    maps_url = (
        "https://www.google.com/maps/place/New+Nightlife/"
        f"@13.77,109.22,17z/data=!4m2!3m1!1s{token}"
    )
    record = MaterializedReplacementRecord(
        id="night_qn_100",
        entity_type=EntityType.NIGHTLIFE,
        primary_type=EntityType.NIGHTLIFE,
        place_types=[EntityType.NIGHTLIFE],
        name="New Distinct Nightlife",
        city=str(_base_records()[EntityType.NIGHTLIFE][0]["city"]),
        city_id="city_quy_nhon",
        address="100 Replacement Street",
        coordinates=ReplacementCoordinates(lat=13.77, lng=109.22),
        phone="0905000100",
        tags=["canonical_replacement"],
        source=ReplacementSource(
            url=maps_url,
            crawled_at=GENERATED_AT,
        ),
        embedding_text="New Distinct Nightlife | Quy Nhon | nightlife",
        verification_status=VerificationStatus.HUMAN_VERIFIED,
        last_updated=GENERATED_AT,
        provenance=ReplacementProvenance(
            approval_id="replacement-approval-test",
            approval_hash="a" * 64,
            candidate_detail_id="candidate-detail-test",
            candidate_detail_hash="b" * 64,
            candidate_key="candidate-new-nightlife",
            source_ids=["google-maps-web"],
            source_record_ids=["source-record-test"],
            observation_ids=["observation-test"],
            vacancy_id=stable_identifier(
                "vacancy",
                "city_quy_nhon",
                EntityType.NIGHTLIFE.value,
                "night_qn_001",
            ),
            replacement_of="night_qn_001",
            google_external_id=token,
            google_external_url=maps_url,
            approval_method=ReplacementApprovalMethod.HUMAN,
            reviewer="reviewer@example.com",
            approved_at=GENERATED_AT,
        ),
    )
    destination = root / "records" / "entity=nightlife" / "place=night_qn_100.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return record


def test_loader_returns_slots_raw_lookup_source_audit_and_tagged_decision(
    tmp_path: Path,
) -> None:
    _write_master(tmp_path)

    loaded = load_verified_master(tmp_path)

    assert len(loaded.slots) == 6
    assert list(loaded.raw_records_by_id) == sorted(loaded.raw_records_by_id)
    raw = loaded.raw_record("attr_dn_002")
    assert raw is not None
    assert raw.source_filename == "attraction_final.json"
    assert raw.record_index == 1
    assert raw.raw_record["tags"] == [
        "must_try",
        "duplicate_record",
        "duplicate_of_attr_dn_001",
    ]

    duplicate_slot = next(
        item for item in loaded.slots if item.legacy_place_id == "attr_dn_002"
    )
    assert duplicate_slot.city_id == "city_da_nang"
    # Control tags stay in raw provenance but do not pollute semantic place tags.
    assert duplicate_slot.tags == ["must_try"]
    assert [item.source_filename for item in loaded.source_files] == [
        "attraction_final.json",
        "cafe_final.json",
        "hotel_final.json",
        "nightlife_final.json",
        "restaurant_final.json",
    ]
    quotas = {
        (item.entity_type, item.city_id): item.count for item in loaded.quota_counts
    }
    assert quotas[(EntityType.ATTRACTION, "city_da_nang")] == 2
    assert quotas[(EntityType.ATTRACTION, "city_quy_nhon")] == 0
    assert quotas[(EntityType.CAFE, "city_quy_nhon")] == 1
    assert loaded.explicit_duplicate_decisions[0].model_dump(mode="json") == {
        "keeper_legacy_place_id": "attr_dn_001",
        "duplicate_legacy_place_ids": ["attr_dn_002"],
        "reason": "verified_master_duplicate_tag",
    }


def test_loader_accepts_canonically_equivalent_vietnamese_unicode(
    tmp_path: Path,
) -> None:
    records = _base_records()
    records[EntityType.HOTEL][0]["city"] = unicodedata.normalize("NFD", "Đà Nẵng")
    _write_master(tmp_path, records)

    loaded = load_verified_master(tmp_path)

    hotel = next(item for item in loaded.slots if item.legacy_place_id == "hotel_dn_001")
    assert hotel.city_id == "city_da_nang"


def test_workflow_applies_file_and_verified_tag_decisions_without_mutating_master(
    tmp_path: Path,
) -> None:
    master_paths = _write_master(tmp_path)
    decisions_path = _write_decisions(tmp_path / "identity-decisions.json")
    before = _file_hashes(master_paths)

    manifest, report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT,
    )

    assert _file_hashes(master_paths) == before
    assert len(manifest.identities) == 4
    assert len(manifest.retired_place_ids) == 2
    assert len(manifest.vacancies) == 2
    assert report.source_record_count == 6
    assert report.canonical_place_count == 4
    assert report.retired_place_id_count == 2
    assert report.vacancy_count == 2
    assert report.open_vacancy_count == 2
    assert report.filled_vacancy_count == 0
    assert report.decision_filename == "identity-decisions.json"
    assert report.include_tagged_duplicates is True
    assert len(report.explicit_duplicate_decisions) == 1
    assert len(report.file_duplicate_decisions) == 1
    assert report.file_replacement_decisions == []
    assert len(report.applied_duplicate_decisions) == 2
    assert report.applied_replacement_decisions == []
    assert report.manifest_hash == manifest.manifest_hash

    output_quotas = {
        (item.entity_type, item.city_id): (
            item.target_count,
            item.active_count,
            item.vacancy_count,
        )
        for item in report.output_quotas
    }
    assert output_quotas[(EntityType.ATTRACTION, "city_da_nang")] == (2, 1, 1)
    assert output_quotas[(EntityType.NIGHTLIFE, "city_quy_nhon")] == (1, 0, 1)


def test_workflow_merges_approved_overlay_without_mutating_verified_master(
    tmp_path: Path,
) -> None:
    master_paths = _write_master(tmp_path)
    decisions_path = _write_decisions(tmp_path / "identity-decisions.json")
    approved_root = tmp_path / "approved-replacements"
    approved = _write_approved_nightlife_replacement(approved_root)
    before = _file_hashes(master_paths)

    baseline, _ = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT,
    )
    manifest, report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        approved_replacement_root=approved_root,
        generated_at=GENERATED_AT,
    )

    assert _file_hashes(master_paths) == before
    assert len(manifest.identities) == len(baseline.identities) + 1
    replacement = next(
        item
        for item in manifest.identities
        if item.canonical_place_id == approved.id
    )
    assert replacement.legacy_place_ids == [approved.id]
    vacancy = next(
        item
        for item in manifest.vacancies
        if item.retired_place_id == "night_qn_001"
    )
    assert vacancy.status.value == "filled"
    assert vacancy.replacement_place_id == approved.id

    nightlife_quota = next(
        item
        for item in manifest.quotas
        if (item.entity_type, item.city_id)
        == (EntityType.NIGHTLIFE, "city_quy_nhon")
    )
    assert (
        nightlife_quota.target_count,
        nightlife_quota.active_count,
        nightlife_quota.vacancy_count,
    ) == (1, 1, 0)

    assert report.approved_replacement_count == 1
    assert report.approved_replacement_ids == [approved.provenance.approval_id]
    assert report.approved_replacement_hashes == [
        approved.provenance.approval_hash
    ]
    assert report.open_vacancy_count == 1
    assert report.filled_vacancy_count == 1
    assert any(
        item.retired_place_id == "night_qn_001"
        and item.replacement_place_id == approved.id
        and item.reason == f"approved_replacement:{approved.provenance.approval_id}"
        for item in report.applied_replacement_decisions
    )


def test_workflow_applies_replacement_and_reports_filled_historical_vacancy(
    tmp_path: Path,
) -> None:
    records = _base_records()
    records[EntityType.NIGHTLIFE].append(
        _record(
            "night_qn_002",
            EntityType.NIGHTLIFE,
            str(records[EntityType.NIGHTLIFE][0]["city"]),
        )
    )
    _write_master(tmp_path, records)
    decisions_path = _write_decisions(
        tmp_path / "identity-decisions.json",
        replacements=[
            {
                "retired_place_id": "night_qn_001",
                "replacement_place_id": "night_qn_002",
                "reason": "human_verified_distinct_replacement",
            }
        ],
    )

    manifest, report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT,
    )

    vacancy = next(
        item
        for item in manifest.vacancies
        if item.retired_place_id == "night_qn_001"
    )
    assert vacancy.status.value == "filled"
    assert vacancy.replacement_place_id == "night_qn_002"
    assert report.vacancy_count == 2
    assert report.open_vacancy_count == 1
    assert report.filled_vacancy_count == 1
    assert len(report.file_replacement_decisions) == 1
    assert report.applied_replacement_decisions == report.file_replacement_decisions
    nightlife_quota = next(
        item
        for item in manifest.quotas
        if (item.entity_type, item.city_id)
        == (EntityType.NIGHTLIFE, "city_quy_nhon")
    )
    assert (
        nightlife_quota.target_count,
        nightlife_quota.active_count,
        nightlife_quota.vacancy_count,
    ) == (1, 1, 0)


def test_workflow_requires_replacement_id_in_supplied_master_dataset(
    tmp_path: Path,
) -> None:
    _write_master(tmp_path)
    decisions_path = _write_decisions(
        tmp_path / "identity-decisions.json",
        replacements=[
            {
                "retired_place_id": "night_qn_001",
                "replacement_place_id": "night_qn_002",
            }
        ],
    )

    with pytest.raises(
        CanonicalIdentityError,
        match="replacement place must be supplied as a new active slot",
    ):
        build_manifest_from_master(
            tmp_path,
            decisions_path,
            generated_at=GENERATED_AT,
        )


def test_workflow_report_is_content_deterministic_and_tracks_previous_manifest(
    tmp_path: Path,
) -> None:
    _write_master(tmp_path)
    decisions_path = _write_decisions(tmp_path / "identity-decisions.json")
    first, first_report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT,
    )
    retry, retry_report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT + timedelta(days=1),
    )

    assert retry.manifest_hash == first.manifest_hash
    assert retry_report.report_hash == first_report.report_hash
    previous_path = tmp_path / "previous-manifest.json"
    CanonicalIdentityManifestWriter(previous_path).write(first)
    rebuilt, previous_report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        previous_manifest_path=previous_path,
        generated_at=GENERATED_AT + timedelta(days=2),
    )
    assert rebuilt.manifest_hash == first.manifest_hash
    assert previous_report.previous_manifest_id == first.manifest_id
    assert previous_report.previous_manifest_hash == first.manifest_hash


def test_tagged_duplicates_can_be_reported_without_automatic_merge(
    tmp_path: Path,
) -> None:
    _write_master(tmp_path)
    decisions_path = _write_decisions(tmp_path / "identity-decisions.json")

    manifest, report = build_manifest_from_master(
        tmp_path,
        decisions_path,
        generated_at=GENERATED_AT,
        include_tagged_duplicates=False,
    )

    assert len(manifest.identities) == 5
    assert len(manifest.retired_place_ids) == 1
    assert len(report.explicit_duplicate_decisions) == 1
    assert len(report.applied_duplicate_decisions) == 1
    assert report.include_tagged_duplicates is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: document["data"][0].update(
                {"entity_type": "restaurant"}
            ),
            "entity_type must be 'attraction'",
        ),
        (
            lambda document: document["data"][0].update({"city": "Da Nang"}),
            "unsupported Unicode city",
        ),
        (
            lambda document: document["data"][0].update({"id": "cafe_dn_999"}),
            "ID prefix does not match attraction",
        ),
        (
            lambda document: document["data"][0].update(
                {"coordinates": {"lat": True, "lng": 108.2}}
            ),
            "coordinates.lat is invalid",
        ),
        (
            lambda document: document["metadata"].update({"total_count": 99}),
            "metadata.total_count",
        ),
    ],
)
def test_loader_rejects_invalid_identity_city_coordinates_and_metadata(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    paths = _write_master(tmp_path)
    path = paths[EntityType.ATTRACTION]
    document = json.loads(path.read_text(encoding="utf-8"))
    mutation(document)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CanonicalMasterDataError, match=message):
        load_verified_master(tmp_path)


@pytest.mark.parametrize(
    ("tags", "message"),
    [
        (["duplicate_record"], "requires duplicate_record"),
        (["duplicate_of_attr_dn_001"], "requires duplicate_record"),
        (
            [
                "duplicate_record",
                "duplicate_of_attr_dn_001",
                "duplicate_of_hotel_dn_001",
            ],
            "exactly one",
        ),
        (
            ["duplicate_record", "duplicate_of_attr_dn_999"],
            "does not exist",
        ),
        (
            ["duplicate_record", "duplicate_of_cafe_qn_001"],
            "same city",
        ),
    ],
)
def test_loader_rejects_incomplete_or_unsafe_duplicate_tags(
    tmp_path: Path,
    tags: list[str],
    message: str,
) -> None:
    records = _base_records()
    records[EntityType.ATTRACTION][1]["tags"] = tags
    _write_master(tmp_path, records)

    with pytest.raises(CanonicalMasterDataError, match=message):
        load_verified_master(tmp_path)


def test_workflow_delegates_unknown_and_overlapping_decisions_to_resolver(
    tmp_path: Path,
) -> None:
    _write_master(tmp_path)
    unknown_path = _write_decisions(
        tmp_path / "unknown.json",
        decisions=[
            {
                "keeper_legacy_place_id": "cafe_qn_001",
                "duplicate_legacy_place_ids": ["night_qn_999"],
            }
        ],
    )
    with pytest.raises(CanonicalIdentityError, match="unknown legacy IDs"):
        build_manifest_from_master(
            tmp_path,
            unknown_path,
            generated_at=GENERATED_AT,
        )

    overlap_path = _write_decisions(
        tmp_path / "overlap.json",
        decisions=[
            {
                "keeper_legacy_place_id": "attr_dn_001",
                "duplicate_legacy_place_ids": ["attr_dn_002"],
            }
        ],
    )
    with pytest.raises(CanonicalIdentityError, match="multiple duplicate decisions"):
        build_manifest_from_master(
            tmp_path,
            overlap_path,
            generated_at=GENERATED_AT,
        )


def test_decision_document_requires_supported_schema_version(tmp_path: Path) -> None:
    path = _write_decisions(
        tmp_path / "decisions.json",
        decisions=[],
        schema_version="2.0.0",
    )

    with pytest.raises(CanonicalMasterDataError, match="schema_version"):
        load_duplicate_identity_decisions(path)


def test_real_verified_master_has_expected_unicode_quotas_and_explicit_tags() -> None:
    repository_root = Path(__file__).resolve().parents[3]

    loaded = load_verified_master(repository_root / "travel_data_verified")

    assert len(loaded.slots) == 692
    assert {
        (item.entity_type.value, item.city_id): item.count
        for item in loaded.quota_counts
    } == {
        ("attraction", "city_da_nang"): 67,
        ("attraction", "city_quy_nhon"): 51,
        ("cafe", "city_da_nang"): 71,
        ("cafe", "city_quy_nhon"): 35,
        ("hotel", "city_da_nang"): 43,
        ("hotel", "city_quy_nhon"): 30,
        ("nightlife", "city_da_nang"): 99,
        ("nightlife", "city_quy_nhon"): 96,
        ("restaurant", "city_da_nang"): 106,
        ("restaurant", "city_quy_nhon"): 94,
    }
    assert [
        (item.keeper_legacy_place_id, item.duplicate_legacy_place_ids)
        for item in loaded.explicit_duplicate_decisions
    ] == [
        ("attr_dn_001", ["attr_dn_037"]),
        ("attr_dn_008", ["attr_dn_040"]),
        ("attr_dn_025", ["attr_dn_051"]),
        ("attr_dn_011", ["attr_dn_053"]),
    ]
