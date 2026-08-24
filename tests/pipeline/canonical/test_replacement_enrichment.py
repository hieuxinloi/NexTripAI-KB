from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import (
    CanonicalRecordSource,
    materialize_canonical_active_dataset,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import (
    DuplicateIdentityDecision,
    LegacyPlaceSlot,
    VacancyReplacementDecision,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.canonical.projection import (
    MaterializedReplacementRecord,
    ReplacementApprovalMethod,
    ReplacementCoordinates,
    ReplacementProvenance,
    ReplacementSource,
    load_approved_replacements,
)
from nextrip_pipeline.canonical.replacement_enrichment import (
    ReplacementEnrichmentError,
    apply_replacement_enrichments,
    build_canonical_replacement_enrichment_overlay,
)
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.crawl.raw_writer import RawJsonWriter, compute_content_hash
from nextrip_pipeline.schemas import (
    EntityType,
    RecordSubjectType,
    SourceRecord,
    VerificationStatus,
)


UTC = timezone.utc
CRAWLED_AT = datetime(2026, 8, 20, 4, tzinfo=UTC)
APPROVED_AT = CRAWLED_AT + timedelta(hours=1)
GOOGLE_TOKEN = "0x314219caaa000001:0x1000000000000001"
GOOGLE_URL = (
    "https://www.google.com/maps/place/New+Google+Cafe/"
    "@16.051,108.202,17z/data=!4m2!3m1!1s"
    f"{GOOGLE_TOKEN}"
)
SOURCE_RECORD_ID = "source-google-cafe"
CANDIDATE_KEY = "candidate-new-google-cafe"
PLACE_ID = "cafe_dn_100"
COVER_URL = "https://lh3.googleusercontent.com/p/cafe-cover=w800-h600-k-no"
THUMBNAIL_URL = "https://lh3.googleusercontent.com/p/cafe-thumb=w32-h32-p-k-no"


def _replacement() -> MaterializedReplacementRecord:
    return MaterializedReplacementRecord(
        id=PLACE_ID,
        entity_type=EntityType.CAFE,
        primary_type=EntityType.CAFE,
        place_types=[EntityType.CAFE],
        name="New Google Cafe",
        city="Đà Nẵng",
        city_id="city_da_nang",
        address=None,
        coordinates=ReplacementCoordinates(lat=16.051, lng=108.202),
        phone="0905000100",
        website_url="https://replacement.example",
        tags=["canonical_replacement"],
        source=ReplacementSource(url=GOOGLE_URL, crawled_at=CRAWLED_AT),
        embedding_text="New Google Cafe | Đà Nẵng | cafe",
        verification_status=VerificationStatus.AUTO_VERIFIED,
        last_updated=APPROVED_AT,
        provenance=ReplacementProvenance(
            approval_id="replacement-approval-test",
            approval_hash="a" * 64,
            candidate_detail_id="candidate-detail-test",
            candidate_detail_hash="b" * 64,
            candidate_key=CANDIDATE_KEY,
            source_ids=["google-maps-web"],
            source_record_ids=[SOURCE_RECORD_ID],
            observation_ids=["observation-google-cafe"],
            vacancy_id=stable_identifier(
                "vacancy",
                "city_da_nang",
                EntityType.CAFE.value,
                "cafe_dn_002",
            ),
            replacement_of="cafe_dn_002",
            google_external_id=GOOGLE_TOKEN,
            google_external_url=GOOGLE_URL,
            approval_method=ReplacementApprovalMethod.DETERMINISTIC,
            reviewer="canonical-distinct-gate-v1",
            approved_at=APPROVED_AT,
        ),
    )


def _write_approved_replacement(
    root: Path,
    replacement: MaterializedReplacementRecord,
) -> Path:
    destination = (
        root
        / "records"
        / "entity=cafe"
        / "city=city_da_nang"
        / f"place={replacement.id}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            replacement.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def _raw_payload(*, image_urls: list[str] | None = None) -> dict[str, object]:
    return {
        "page": {
            "structured_data": {
                "name": "New Google Cafe",
                "address": "100 Google Maps Street, Đà Nẵng",
                "category": "Coffee shop",
                "image_urls": image_urls if image_urls is not None else [],
            }
        }
    }


def _write_raw_record(
    root: Path,
    *,
    image_urls: list[str] | None = None,
) -> tuple[SourceRecord, Path]:
    payload = _raw_payload(image_urls=image_urls)
    record = SourceRecord(
        source_record_id=SOURCE_RECORD_ID,
        run_id="run-google-maps-enrichment",
        source_id="google-maps-web",
        entity_type=EntityType.CAFE,
        subject_type=RecordSubjectType.OPENING_STATUS,
        subject_id=CANDIDATE_KEY,
        crawled_at=CRAWLED_AT,
        raw_payload=payload,
        content_hash=compute_content_hash(payload),
        parser_version="google-maps-playwright-v1",
        source_url=GOOGLE_URL,
        http_status=200,
        content_type="text/html",
    )
    return record, RawJsonWriter(root).write(record)


def _master_manifest_and_dataset(
    replacement: MaterializedReplacementRecord,
):
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
    raw_records = [
        MasterRawRecord(
            place_id=place_id,
            source_filename="cafe_final.json",
            record_index=index,
            raw_record={
                "id": place_id,
                "entity_type": "cafe",
                "name": name,
                "city": "Đà Nẵng",
                "address": f"{index + 1} Master Street",
                "coordinates": {"lat": 16.04 + index / 100, "lng": 108.2},
                "category": "cafe",
                "aliases": [],
                "tags": [],
            },
        )
        for index, (place_id, name) in enumerate(
            [
                ("cafe_dn_001", "Keeper Cafe"),
                ("cafe_dn_002", "Duplicate Cafe"),
            ]
        )
    ]
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={item.place_id: item for item in raw_records},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    duplicate = DuplicateIdentityDecision(
        keeper_legacy_place_id="cafe_dn_001",
        duplicate_legacy_place_ids=["cafe_dn_002"],
        reason="reviewed physical duplicate",
    )
    initial = build_canonical_identity_manifest(
        slots,
        duplicate_decisions=[duplicate],
        generated_at=CRAWLED_AT,
    )
    manifest = build_canonical_identity_manifest(
        [*slots, replacement.to_legacy_place_slot()],
        duplicate_decisions=[duplicate],
        replacement_decisions=[
            VacancyReplacementDecision(
                retired_place_id="cafe_dn_002",
                replacement_place_id=replacement.id,
            )
        ],
        previous_manifest=initial,
        generated_at=APPROVED_AT,
    )
    return materialize_canonical_active_dataset(
        master,
        manifest,
        approved_replacements=[replacement],
    )


def test_builds_from_pinned_raw_record_without_mutating_approval(
    tmp_path: Path,
) -> None:
    approval_root = tmp_path / "approved"
    raw_root = tmp_path / "raw"
    approval_path = _write_approved_replacement(approval_root, _replacement())
    approval_bytes = approval_path.read_bytes()
    source_record, raw_path = _write_raw_record(
        raw_root,
        image_urls=[THUMBNAIL_URL, COVER_URL],
    )

    approved = load_approved_replacements(approval_root)
    overlay = build_canonical_replacement_enrichment_overlay(approved, raw_root)

    assert approval_path.read_bytes() == approval_bytes
    assert approved[0].address is None
    assert overlay.approved_replacement_count == 1
    assert overlay.enrichment_count == 1
    assert (overlay.address_count, overlay.category_count, overlay.cover_count) == (
        1,
        1,
        1,
    )
    enrichment = overlay.enrichments[0]
    assert enrichment.place_id == PLACE_ID
    assert enrichment.enriched_fields == [
        "address",
        "google_maps_category",
        "cover_image_url",
    ]
    assert enrichment.address == "100 Google Maps Street, Đà Nẵng"
    assert enrichment.google_maps_category == "Coffee shop"
    assert str(enrichment.cover_image_url) == COVER_URL
    assert enrichment.provenance.approval_id == "replacement-approval-test"
    assert enrichment.provenance.approval_hash == "a" * 64
    assert enrichment.provenance.candidate_key == CANDIDATE_KEY
    assert enrichment.provenance.source_record_content_hash == (
        source_record.content_hash
    )
    assert enrichment.provenance.source_record_envelope_hash == stable_sha256(
        source_record.model_dump(mode="json")
    )
    assert enrichment.provenance.raw_relative_path == raw_path.relative_to(
        raw_root
    ).as_posix()


def test_rejects_raw_payload_tampered_after_immutable_write(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    _, raw_path = _write_raw_record(raw_root, image_urls=[COVER_URL])
    envelope = json.loads(raw_path.read_text(encoding="utf-8"))
    envelope["raw_payload"]["page"]["structured_data"]["address"] = (
        "Tampered Street"
    )
    raw_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ReplacementEnrichmentError, match="content_hash mismatch"):
        build_canonical_replacement_enrichment_overlay(
            [_replacement()],
            raw_root,
        )


def test_ignores_thumbnail_when_raw_record_has_no_eligible_cover(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    _write_raw_record(raw_root, image_urls=[THUMBNAIL_URL])

    overlay = build_canonical_replacement_enrichment_overlay(
        [_replacement()],
        raw_root,
    )

    enrichment = overlay.enrichments[0]
    assert overlay.cover_count == 0
    assert enrichment.cover_image_url is None
    assert enrichment.enriched_fields == ["address", "google_maps_category"]


def test_missing_pinned_raw_record_fails_instead_of_silently_skipping(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    raw_root.mkdir()

    with pytest.raises(ReplacementEnrichmentError, match="missing raw source"):
        build_canonical_replacement_enrichment_overlay(
            [_replacement()],
            raw_root,
        )


def test_apply_enriches_only_replacement_and_rehashes_dataset(
    tmp_path: Path,
) -> None:
    replacement = _replacement()
    raw_root = tmp_path / "raw"
    _write_raw_record(raw_root, image_urls=[THUMBNAIL_URL, COVER_URL])
    overlay = build_canonical_replacement_enrichment_overlay(
        [replacement],
        raw_root,
    )
    dataset = _master_manifest_and_dataset(replacement)
    original = {item.place_id: item for item in dataset.records}

    enriched_dataset = apply_replacement_enrichments(dataset, overlay)
    enriched = {item.place_id: item for item in enriched_dataset.records}

    assert enriched_dataset.dataset_id != dataset.dataset_id
    assert enriched_dataset.dataset_hash != dataset.dataset_hash
    assert enriched_dataset.report == dataset.report
    assert enriched["cafe_dn_001"] == original["cafe_dn_001"]
    assert original[PLACE_ID].address is None
    assert "replacement_enrichment" not in original[PLACE_ID].data
    replacement_record = enriched[PLACE_ID]
    assert replacement_record.provenance.source_kind is (
        CanonicalRecordSource.APPROVED_REPLACEMENT
    )
    assert replacement_record.record_hash != original[PLACE_ID].record_hash
    assert replacement_record.address == "100 Google Maps Street, Đà Nẵng"
    assert replacement_record.data["address"] == replacement_record.address
    assert replacement_record.data["google_maps_category"] == "Coffee shop"
    assert replacement_record.data["cover_image_url"] == COVER_URL
    assert replacement_record.data["images"] == [COVER_URL]
    provenance = replacement_record.data["replacement_enrichment"]
    assert provenance["overlay_id"] == overlay.overlay_id
    assert provenance["overlay_hash"] == overlay.overlay_hash
    assert provenance["approval_id"] == replacement.provenance.approval_id
    assert provenance["approval_hash"] == replacement.provenance.approval_hash
    assert provenance["source_record_id"] == SOURCE_RECORD_ID
