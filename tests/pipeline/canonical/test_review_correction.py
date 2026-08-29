from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import DuplicateIdentityDecision, LegacyPlaceSlot
from nextrip_pipeline.canonical.projection import (
    apply_review_corrections_to_identity_projection,
    build_existing_identity_projection,
)
from nextrip_pipeline.canonical.resolver import (
    CanonicalIdentityResolver,
    build_canonical_identity_manifest,
)
from nextrip_pipeline.canonical.review_correction import (
    CanonicalReviewCorrectionWriter,
    ReviewCorrectionInputError,
    ReviewCorrectionSkipReason,
    apply_review_corrections,
    build_canonical_review_correction_overlay,
    read_canonical_review_correction_overlay,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    GeoPoint,
    GoogleMapsPlaceObservation,
    OpeningStatusObservation,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _observation(
    place_id: str,
    *,
    name: str = "Google Name",
    location: GeoPoint | None = None,
    observed_at: datetime = NOW,
    phone: str | None = "+84 900 000 001",
    website_url: str | None = "https://example.com/place",
) -> GoogleMapsPlaceObservation:
    observation_id = f"obs-{place_id}-{observed_at.hour}"
    return GoogleMapsPlaceObservation(
        observation_id=observation_id,
        run_id="run-review-correction",
        place_id=place_id,
        source_record_id=f"source-{place_id}",
        source_id="google-maps-web",
        source_url=f"https://www.google.com/maps/place/{place_id}",
        name=name,
        address="12 Google Street, Da Nang",
        category="Coffee shop",
        phone=phone,
        website_url=website_url,
        location=location,
        business_status=BusinessStatus.ACTIVE,
        opening=OpeningStatusObservation(
            observation_id=f"opening-{place_id}",
            run_id="run-review-correction",
            place_id=place_id,
            source_record_ids=[f"source-{place_id}"],
            local_date=date(2026, 8, 20),
            status=DailyOpeningStatus.UNKNOWN,
            observed_at=observed_at,
        ),
        observed_at=observed_at,
    )


def _master_and_manifest():
    place_ids = ["cafe_dn_001", "night_dn_001"]
    slots = [
        LegacyPlaceSlot(
            legacy_place_id=place_id,
            city_id="city_da_nang",
            primary_type=(
                EntityType.CAFE if place_id.startswith("cafe") else EntityType.NIGHTLIFE
            ),
        )
        for place_id in place_ids
    ]
    raw_records = [
        MasterRawRecord(
            place_id=place_id,
            source_filename=f"{place_id}_final.json",
            record_index=index,
            raw_record={
                "id": place_id,
                "entity_type": slot.primary_type.value,
                "name": f"Master {index}",
                "aliases": [],
                "city": "Da Nang",
                "address": f"{index} Master Street",
                "coordinates": {"lat": 16.0 + index / 100, "lng": 108.2},
                "category": slot.primary_type.value,
                "tags": [],
            },
        )
        for index, (place_id, slot) in enumerate(zip(place_ids, slots, strict=True))
    ]
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={item.place_id: item for item in raw_records},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=NOW)
    return master, manifest


def _dataset():
    master, manifest = _master_and_manifest()
    return materialize_canonical_active_dataset(master, manifest)


def test_builder_uses_latest_clean_fields_and_rejects_fallback_coordinates() -> None:
    old = _observation(
        "cafe_dn_001",
        name="Old Name",
        location=GeoPoint(
            latitude=16.1,
            longitude=108.1,
            source="google-maps-web",
            accuracy="google_maps_place_page",
        ),
        observed_at=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )
    latest = _observation(
        "cafe_dn_001",
        location=GeoPoint(
            latitude=16.2,
            longitude=108.2,
            source="verified-master-data",
            accuracy="verified_master_fallback",
        ),
    )
    valid_google_location = GeoPoint(
        latitude=16.3,
        longitude=108.3,
        source="google-maps-web",
        accuracy="google_maps_place_page",
    )
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [old, latest, _observation("night_dn_001", location=valid_google_location)],
    )

    assert overlay.group_count == 1
    assert overlay.correction_count == 2
    by_id = {item.place_id: item for item in overlay.groups[0].corrections}
    assert by_id["cafe_dn_001"].name == "Google Name"
    assert by_id["cafe_dn_001"].location is None
    assert "location" not in by_id["cafe_dn_001"].corrected_fields
    assert by_id["night_dn_001"].location == valid_google_location
    assert by_id["night_dn_001"].identity_status == "review"


def test_placeholder_capture_is_skipped_as_a_whole() -> None:
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [_observation("night_dn_001"), _observation("cafe_dn_001", name="Hours")],
    )

    assert overlay.correction_count == 1
    assert overlay.skipped_count == 1
    skip = overlay.groups[0].skips[0]
    assert skip.place_id == "cafe_dn_001"
    assert skip.reason is ReviewCorrectionSkipReason.INVALID_OBSERVED_IDENTITY


def test_apply_returns_new_hashed_dataset_and_preserves_review_identity() -> None:
    dataset = _dataset()
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [
            _observation(
                "night_dn_001",
                location=GeoPoint(
                    latitude=16.3,
                    longitude=108.3,
                    source="google-maps-web",
                    accuracy="google_maps_place_page",
                ),
            ),
            _observation("cafe_dn_001", name="Hours"),
        ],
    )

    corrected = apply_review_corrections(dataset, overlay)
    original_by_id = {item.place_id: item for item in dataset.records}
    corrected_by_id = {item.place_id: item for item in corrected.records}

    assert corrected.dataset_id != dataset.dataset_id
    assert original_by_id["night_dn_001"].name == "Master 1"
    assert corrected_by_id["night_dn_001"].name == "Google Name"
    assert corrected_by_id["night_dn_001"].aliases == ["Master 1"]
    assert corrected_by_id["night_dn_001"].coordinates.source == "google-maps-web"
    assert corrected_by_id["night_dn_001"].data["google_maps_category"] == "Coffee shop"
    assert corrected_by_id["night_dn_001"].data["business_status"] == "active"
    assert corrected_by_id["night_dn_001"].data["review_correction"][
        "identity_status"
    ] == "review"
    assert corrected_by_id["cafe_dn_001"] == original_by_id["cafe_dn_001"]


def test_apply_resolves_merged_legacy_corrections_and_selects_richest() -> None:
    master, _ = _master_and_manifest()
    manifest = build_canonical_identity_manifest(
        master.slots,
        duplicate_decisions=[
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
            )
        ],
        generated_at=NOW,
    )
    dataset = materialize_canonical_active_dataset(master, manifest)
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [
            _observation(
                "cafe_dn_001",
                phone=None,
                website_url=None,
                observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
            ),
            _observation("night_dn_001"),
        ],
    )

    corrected = apply_review_corrections(
        dataset,
        overlay,
        resolver=CanonicalIdentityResolver(manifest),
    )

    assert len(corrected.records) == 1
    correction = corrected.records[0].data["review_correction"]
    assert correction["source_legacy_place_id"] == "night_dn_001"
    assert corrected.records[0].phone == "+84 900 000 001"


def test_apply_rejects_conflicting_corrections_for_merged_identity() -> None:
    master, _ = _master_and_manifest()
    manifest = build_canonical_identity_manifest(
        master.slots,
        duplicate_decisions=[
            DuplicateIdentityDecision(
                keeper_legacy_place_id="cafe_dn_001",
                duplicate_legacy_place_ids=["night_dn_001"],
            )
        ],
        generated_at=NOW,
    )
    dataset = materialize_canonical_active_dataset(master, manifest)
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [
            _observation("cafe_dn_001", name="One physical place"),
            _observation("night_dn_001", name="Another physical place"),
        ],
    )

    with pytest.raises(ReviewCorrectionInputError, match="conflict on name"):
        apply_review_corrections(
            dataset,
            overlay,
            resolver=CanonicalIdentityResolver(manifest),
        )


def test_apply_to_identity_projection_rehashes_and_pins_overlay() -> None:
    master, manifest = _master_and_manifest()
    projection = build_existing_identity_projection(master, manifest)
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [
            _observation("night_dn_001", name="Corrected Night Name"),
            _observation("cafe_dn_001", name="Corrected Cafe Name"),
        ],
    )

    corrected = apply_review_corrections_to_identity_projection(
        projection,
        overlay,
    )

    assert corrected.projection_hash != projection.projection_hash
    assert corrected.review_correction_overlay_id == overlay.overlay_id
    assert corrected.review_correction_overlay_hash == overlay.overlay_hash
    assert {item.place_id: item.name for item in corrected.identities} == {
        "cafe_dn_001": "Corrected Cafe Name",
        "night_dn_001": "Corrected Night Name",
    }


def test_writer_is_idempotent_and_readable(tmp_path: Path) -> None:
    overlay = build_canonical_review_correction_overlay(
        {"group-1": ["night_dn_001", "cafe_dn_001"]},
        [_observation("night_dn_001"), _observation("cafe_dn_001")],
    )
    writer = CanonicalReviewCorrectionWriter(tmp_path)

    first = writer.write(overlay)
    second = writer.write(overlay)

    assert first == second
    assert read_canonical_review_correction_overlay(first) == overlay
