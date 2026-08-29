from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.jobs.master_place_bootstrap import (
    MASTER_PLACE_FILES,
    MasterPlaceBootstrapSummaryWriter,
    VerifiedMasterCurrentPlaceWriter,
    VerifiedMasterPlaceBootstrapper,
)
from nextrip_pipeline.publishing.current_place import CurrentPlaceWriter
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    GoogleMapsPlaceObservation,
    MappingStatus,
    OpeningStatusObservation,
    VerificationStatus,
)
from tests.canonical_dataset_support import (
    ACTIVE_CANONICAL_DATASET,
    write_legacy_projection_from_canonical,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def _record(entity_type: EntityType, index: int = 1) -> dict[str, object]:
    prefix = {
        EntityType.ATTRACTION: "attr",
        EntityType.CAFE: "cafe",
        EntityType.HOTEL: "hotel",
        EntityType.NIGHTLIFE: "night",
        EntityType.RESTAURANT: "rest",
    }[entity_type]
    return {
        "id": f"{prefix}_dn_{index:03d}",
        "entity_type": entity_type.value,
        "name": f"Verified {entity_type.value}",
        "city": "Đà Nẵng",
        "address": "1 Test Street",
        "coordinates": {"lat": 16.06, "lng": 108.22},
        "category": entity_type.value,
        "opening_hours": {
            "open": "07:00",
            "close": "22:00",
            "closed_days": [],
            "note": "Verified schedule",
        },
        "images": ["https://example.com/cover.jpg"],
        "source": {
            "url": "https://example.com/source",
            "crawled_at": "2026-06-01T00:00:00",
        },
        "last_updated": "2026-06-02",
    }


def _write_master_files(root: Path) -> None:
    root.mkdir(parents=True)
    for entity_type, filename in MASTER_PLACE_FILES.items():
        record = _record(entity_type)
        if entity_type is EntityType.HOTEL:
            record["last_verified"] = "2026-07-27"
            record["verified_sources"] = [
                {"name": "review", "url": "https://example.com/review"}
            ]
        if entity_type is EntityType.CAFE:
            record["phone"] = None
            record["website"] = None
        (root / filename).write_text(
            json.dumps({"metadata": {}, "data": [record]}, ensure_ascii=False),
            encoding="utf-8",
        )


def _bootstrapper(output: Path) -> VerifiedMasterPlaceBootstrapper:
    return VerifiedMasterPlaceBootstrapper(
        VerifiedMasterCurrentPlaceWriter(output / "current"),
        MasterPlaceBootstrapSummaryWriter(output / "runs"),
        clock=lambda: NOW,
    )


def test_bootstrap_seeds_all_entity_types_with_master_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "master"
    _write_master_files(source)
    bootstrapper = _bootstrapper(tmp_path)

    summary, summary_path = bootstrapper.run(source, run_id="bootstrap-one")

    assert summary.source_file_count == 5
    assert summary.source_record_count == 5
    assert summary.seeded_count == 5
    assert summary.skipped_existing_count == 0
    assert summary.failed_count == 0
    assert summary_path.exists()

    cafe = bootstrapper.current_writer.get("cafe_dn_001")
    assert cafe is not None
    assert cafe.name == "Verified cafe"
    assert cafe.city_id == "city_da_nang"
    assert cafe.location == GeoPoint(
        latitude=16.06,
        longitude=108.22,
        accuracy="verified_master",
        source="verified-master-data",
        verified_at=datetime(2026, 6, 2, tzinfo=UTC),
    )
    assert cafe.opening is None
    assert cafe.opening_hours.opens_at == "07:00"
    assert cafe.phone is None
    assert cafe.website_url is None
    assert str(cafe.cover_image_url) == "https://example.com/cover.jpg"
    assert cafe.price_level is None
    assert cafe.provenance.source_id == "verified-master-data"
    assert cafe.provenance.mapping_id is None
    assert cafe.provenance.decision_id is None
    assert (
        cafe.provenance.verification_status
        is VerificationStatus.LEGACY_VERIFIED
    )
    assert cafe.stale_after is None
    assert not cafe.is_stale(NOW + timedelta(days=365))

    hotel = bootstrapper.current_writer.get("hotel_dn_001")
    assert (
        hotel.provenance.verification_status
        is VerificationStatus.HUMAN_VERIFIED
    )

    cafe_coverage = next(
        item
        for item in summary.entity_coverage
        if item.entity_type is EntityType.CAFE
    )
    assert cafe_coverage.present_field_counts["phone"] == 0
    assert cafe_coverage.missing_field_counts["phone"] == 1


def test_second_bootstrap_is_seed_only_and_preserves_existing_content(
    tmp_path: Path,
) -> None:
    source = tmp_path / "master"
    _write_master_files(source)
    bootstrapper = _bootstrapper(tmp_path)
    bootstrapper.run(source, run_id="bootstrap-first")
    current_path = bootstrapper.current_writer.path_for("cafe_dn_001")
    original = current_path.read_bytes()

    payload = json.loads((source / "cafe_final.json").read_text(encoding="utf-8"))
    payload["data"][0]["name"] = "A later master edit"
    (source / "cafe_final.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    summary, _ = bootstrapper.run(source, run_id="bootstrap-second")

    assert summary.seeded_count == 0
    assert summary.skipped_existing_count == 5
    assert current_path.read_bytes() == original


def test_google_pass_can_replace_master_baseline_and_keep_missing_fallbacks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "master"
    _write_master_files(source)
    bootstrapper = _bootstrapper(tmp_path)
    bootstrapper.run(source, run_id="bootstrap")

    observed_at = NOW + timedelta(hours=1)
    opening = OpeningStatusObservation(
        observation_id="google-opening",
        run_id="google-run",
        place_id="cafe_dn_001",
        source_record_ids=["google-source-record"],
        local_date=date(2026, 8, 19),
        status=DailyOpeningStatus.OPEN_TODAY,
        is_24_hours=True,
        observed_at=observed_at,
    )
    observation = GoogleMapsPlaceObservation(
        observation_id="google-observation",
        run_id="google-run",
        place_id="cafe_dn_001",
        source_record_id="google-source-record",
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/test",
        name="Current Google name",
        category=None,
        address=None,
        location=None,
        business_status=BusinessStatus.ACTIVE,
        opening=opening,
        observed_at=observed_at,
    )
    decision = GoogleMapsDecision(
        decision_id="google-decision",
        run_id="google-run",
        observation_id="google-observation",
        place_id="cafe_dn_001",
        status=GoogleMapsDecisionStatus.PASS,
        validation_ids=["google-validation"],
        decided_at=observed_at,
    )
    mapping = ExternalEntityMapping(
        mapping_id="google-maps-cafe-dn-001",
        entity_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Current Google name",
        status=MappingStatus.CONFIRMED,
        confidence=1,
        matched_at=observed_at,
        verified_at=observed_at,
        attributes={
            "master_city": "Đà Nẵng",
            "city_id": "city_da_nang",
        },
    )

    writer = CurrentPlaceWriter(tmp_path / "current", clock=lambda: observed_at)
    writer.publish(observation, decision, mapping)
    current = writer.get("cafe_dn_001")

    assert current.name == "Current Google name"
    assert current.address == "1 Test Street"
    assert current.location.source == "verified-master-data"
    assert current.opening_hours.opens_at == "07:00"
    assert current.provenance.source_id == "google-maps-web"
    assert current.field_sources["name"] == "google-maps-web"
    assert current.field_sources["address"] == "verified-master-data"


def test_missing_master_file_stops_before_any_seed(tmp_path: Path) -> None:
    source = tmp_path / "master"
    _write_master_files(source)
    (source / "restaurant_final.json").unlink()
    bootstrapper = _bootstrapper(tmp_path)

    with pytest.raises(FileNotFoundError, match="restaurant_final.json"):
        bootstrapper.run(source, run_id="incomplete")

    assert not bootstrapper.current_writer.root_directory.exists()


def test_canonical_dataset_can_supply_temporary_legacy_bootstrap_fixture(
    tmp_path: Path,
) -> None:
    dataset = read_canonical_active_dataset(ACTIVE_CANONICAL_DATASET)
    source = write_legacy_projection_from_canonical(tmp_path / "legacy-master-fixture")
    bootstrapper = _bootstrapper(tmp_path)

    summary, _ = bootstrapper.run(
        source,
        run_id="canonical-derived-master-coverage",
    )

    assert summary.source_file_count == 5
    assert summary.source_record_count == len(dataset.records)
    assert summary.seeded_count == len(dataset.records)
    assert summary.failed_count == 0
    assert {item.entity_type for item in summary.entity_coverage} == set(EntityType)
