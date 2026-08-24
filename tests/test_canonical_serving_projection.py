from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from nextrip_current.config import CurrentDataSettings
from nextrip_current.errors import CurrentDataCorruptError
from nextrip_current.repository import CurrentDataRepository
from nextrip_current.runtime import build_current_data_service
from nextrip_graphrag.versions.registry import kb_version_manifests
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDatasetWriter,
    materialize_canonical_active_dataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.place_projection import (
    CANONICAL_PLACE_SOURCE_ID,
    project_canonical_dataset_places,
)
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    OpeningStatusObservation,
    VerificationStatus,
)
from tests.canonical_dataset_support import CanonicalTestPlace, write_canonical_dataset
from nextrip_traffic.access_points import AccessPointRegistry
from nextrip_traffic.config import TrafficSettings
from nextrip_traffic.runtime import build_traffic_service


NOW = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)


def _write_canonical_dataset(root: Path) -> Path:
    raws = [
        MasterRawRecord(
            place_id=place_id,
            source_filename="cafe_final.json",
            record_index=index,
            raw_record={
                "id": place_id,
                "entity_type": "cafe",
                "name": name,
                "aliases": [],
                "tags": ["canonical"],
                "city": "Da Nang",
                "city_id": "city_da_nang",
                "address": f"{index + 1} Canonical Street",
                "coordinates": {"lat": latitude, "lng": longitude},
                "category": "coffee_shop",
                "business_status": "active",
                "opening_hours": {
                    "open": "07:00",
                    "close": "22:00",
                    "closed_days": [],
                },
                "price_level": 2,
                "last_updated": NOW.isoformat(),
                "source": {
                    "url": "https://example.test/source",
                    "crawled_at": NOW.isoformat(),
                },
            },
        )
        for index, (place_id, name, latitude, longitude) in enumerate(
            (
                ("cafe_test_001", "Canonical One", 16.0, 108.0),
                ("cafe_test_002", "Canonical Two", 18.0, 110.0),
            )
        )
    ]
    slots = [
        LegacyPlaceSlot(
            legacy_place_id=raw.place_id,
            city_id="city_da_nang",
            primary_type=EntityType.CAFE,
            tags=["canonical"],
        )
        for raw in raws
    ]
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={raw.place_id: raw for raw in raws},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=NOW)
    dataset = materialize_canonical_active_dataset(master, manifest)
    return CanonicalActiveDatasetWriter(root / "datasets").write(dataset)


def test_projection_preserves_canonical_place_fields(tmp_path: Path) -> None:
    dataset_path = _write_canonical_dataset(tmp_path)
    dataset = read_canonical_active_dataset(dataset_path)

    places = project_canonical_dataset_places(dataset)
    place = places["cafe_test_001"]

    assert place.name == "Canonical One"
    assert place.category == "coffee_shop"
    assert place.location is not None
    assert (place.location.latitude, place.location.longitude) == (16.0, 108.0)
    assert place.opening_hours is not None
    assert place.opening_hours.opens_at == "07:00"
    assert place.price_level == 2
    assert place.provenance.source_id == CANONICAL_PLACE_SOURCE_ID
    assert place.provenance.run_id == dataset.dataset_id


def test_daily_opening_ttl_uses_opening_observed_at_not_stale_last_verified(
    tmp_path: Path,
) -> None:
    opening_observed_at = NOW
    stale_last_verified = NOW - timedelta(days=30)
    source_record_id = "raw-google-maps-opening-1"
    opening = OpeningStatusObservation(
        observation_id="opening-cafe-test-001",
        run_id="google-maps-run-1",
        place_id="cafe_test_001",
        source_record_ids=[source_record_id],
        local_date=opening_observed_at.date(),
        status=DailyOpeningStatus.OPEN_TODAY,
        open_now=True,
        observed_at=opening_observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    dataset_path = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace(
                place_id="cafe_test_001",
                name="Canonical One",
                latitude=16.0,
                longitude=108.0,
                data={
                    "last_verified": stale_last_verified.isoformat(),
                    "last_updated": opening_observed_at.isoformat(),
                    "opening_status": opening.model_dump(mode="json"),
                    "google_maps_refresh": {
                        "source_id": "google-maps-web",
                        "source_record_id": source_record_id,
                        "observation_id": "google-maps-place-1",
                        "decision_id": "google-maps-decision-1",
                        "run_id": "google-maps-run-1",
                        "observed_at": opening_observed_at.isoformat(),
                    },
                },
            )
        ],
        generated_at=NOW,
    )
    dataset = read_canonical_active_dataset(dataset_path)

    place = project_canonical_dataset_places(dataset)["cafe_test_001"]

    assert place.opening is not None
    assert place.opening.observed_at == opening_observed_at
    assert place.stale_after == opening_observed_at + timedelta(days=1)
    assert place.stale_after >= place.opening.observed_at
    assert place.updated_at == opening_observed_at
    assert place.provenance.observed_at == opening_observed_at


def test_current_data_reads_places_only_from_canonical_dataset(
    tmp_path: Path,
) -> None:
    dataset_path = _write_canonical_dataset(tmp_path)
    prices = tmp_path / "prices"
    availability = tmp_path / "availability"
    mappings = tmp_path / "mappings"
    for path in (prices, availability, mappings):
        path.mkdir()

    repository = CurrentDataRepository(
        canonical_dataset_path=dataset_path,
        hotel_price_root=prices,
        hotel_availability_root=availability,
        trivago_mapping_root=mappings,
    )

    assert repository.get_place("cafe_test_001").name == "Canonical One"
    assert repository.get_place("missing") is None
    assert repository.readiness().place_count == 2

    service = build_current_data_service(
        CurrentDataSettings(
            kb_root=tmp_path,
            canonical_dataset_path=dataset_path,
        )
    )
    try:
        assert service.get_place("cafe_test_001").place.name == "Canonical One"
    finally:
        service.close()


def test_current_data_repository_requires_canonical_dataset_argument(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="canonical_dataset_path"):
        CurrentDataRepository(
            hotel_price_root=tmp_path / "prices",
            hotel_availability_root=tmp_path / "availability",
            trivago_mapping_root=tmp_path / "mappings",
        )


def test_current_data_does_not_fallback_when_canonical_dataset_is_invalid(
    tmp_path: Path,
) -> None:
    invalid_dataset = tmp_path / "invalid-canonical.json"
    invalid_dataset.write_text("{broken", encoding="utf-8")

    with pytest.raises(CurrentDataCorruptError, match="invalid canonical"):
        CurrentDataRepository(
            canonical_dataset_path=invalid_dataset,
            hotel_price_root=tmp_path / "prices",
            hotel_availability_root=tmp_path / "availability",
            trivago_mapping_root=tmp_path / "mappings",
        )


def test_traffic_uses_canonical_and_derives_city_center(
    tmp_path: Path,
) -> None:
    dataset_path = _write_canonical_dataset(tmp_path)

    registry = AccessPointRegistry(canonical_dataset_path=dataset_path)

    assert registry.resolve("cafe_test_001").name == "Canonical One"
    assert registry.resolve("cafe_test_002").name == "Canonical Two"
    center = registry.resolve("city_da_nang")
    assert (center.location.latitude, center.location.longitude) == (17.0, 109.0)
    assert center.source_record_ids == [
        "derived:median:canonical-place:city_da_nang:count=2"
    ]
    assert registry.last_report.place_access_points_loaded == 2

    service = build_traffic_service(
        TrafficSettings(
            kb_root=tmp_path,
            canonical_dataset_path=dataset_path,
            cache_path=tmp_path / "traffic.sqlite3",
            valhalla_enabled=False,
            here_enabled=False,
        )
    )
    try:
        assert service.registry.resolve("cafe_test_001").name == "Canonical One"
    finally:
        service.close()


def test_environment_settings_prefer_relative_canonical_dataset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset_path = _write_canonical_dataset(tmp_path)
    relative_dataset = dataset_path.relative_to(tmp_path)
    monkeypatch.setenv("NEXTRIP_KB_ROOT", str(tmp_path))
    monkeypatch.setenv("NEXTRIP_CANONICAL_DATASET", str(relative_dataset))

    current = CurrentDataSettings.from_env()
    traffic = TrafficSettings.from_env()

    assert current.canonical_dataset_path == dataset_path.resolve()
    assert traffic.canonical_dataset_path == dataset_path.resolve()
    assert not hasattr(current, "current_place_root")
    assert not hasattr(traffic, "current_place_root")


def test_settings_reject_removed_current_place_root(tmp_path: Path) -> None:
    dataset_path = _write_canonical_dataset(tmp_path)

    with pytest.raises(ValidationError, match="current_place_root"):
        CurrentDataSettings(
            canonical_dataset_path=dataset_path,
            current_place_root=tmp_path / "legacy-place",
        )
    with pytest.raises(ValidationError, match="current_place_root"):
        TrafficSettings(
            canonical_dataset_path=dataset_path,
            current_place_root=tmp_path / "legacy-place",
        )


def test_v8_manifest_identifies_the_pinned_canonical_source() -> None:
    manifests = kb_version_manifests()

    assert manifests["v8"].dataset == "canonical-active:NEXTRIP_CANONICAL_DATASET"
    assert all(
        manifests[version].dataset == "travel_data_verified:692"
        for version in ("v1", "v2", "v3", "v4", "v5", "v6", "v7")
    )
