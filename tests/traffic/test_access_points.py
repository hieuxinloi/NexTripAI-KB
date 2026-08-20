from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nextrip_pipeline.schemas import (
    AccessPointType,
    TransportMode,
    VerificationStatus,
)
from nextrip_traffic.access_points import (
    AccessPointNotFoundError,
    AccessPointRegistry,
)


NOW = "2026-08-19T12:00:00Z"


def _write_place(
    directory: Path,
    filename: str,
    *,
    place_id: str,
    latitude: float | None,
    longitude: float | None,
    city_id: str | None = "city_da_nang",
    status: str = "legacy_verified",
) -> None:
    location = None
    if latitude is not None and longitude is not None:
        location = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": "verified_master",
            "source": "verified-master-data",
            "verified_at": NOW,
        }
    payload = {
        "place_id": place_id,
        "entity_type": "cafe",
        "city": "Da Nang",
        "city_id": city_id,
        "name": f"Place {place_id}",
        "location": location,
        "field_sources": {},
        "provenance": {
            "run_id": "run-1",
            "source_record_id": f"source-{place_id}",
            "source_id": "verified-master-data",
            "verification_status": status,
        },
        "updated_at": NOW,
    }
    (directory / filename).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_loads_places_and_derives_city_center_from_verified_median(
    tmp_path: Path,
) -> None:
    _write_place(tmp_path, "one.json", place_id="one", latitude=16.0, longitude=108.0)
    _write_place(tmp_path, "two.json", place_id="two", latitude=18.0, longitude=110.0)
    _write_place(
        tmp_path,
        "pending.json",
        place_id="pending",
        latitude=50.0,
        longitude=50.0,
        status="pending_review",
    )

    registry = AccessPointRegistry(tmp_path)

    assert registry.resolve("one").access_point_id == "place:one:main"
    assert registry.resolve("place:two:main").owner_entity_id == "two"
    center = registry.resolve("city_da_nang")
    assert center.access_point_id == "city:city_da_nang:center"
    assert center.access_type == AccessPointType.CITY_CENTER
    assert center.location.latitude == 17.0
    assert center.location.longitude == 109.0
    assert center.verification_status == VerificationStatus.AUTO_VERIFIED
    assert center.source_record_ids == [
        "derived:median:current-place:city_da_nang:count=2"
    ]
    assert TransportMode.TRANSIT not in center.supported_modes
    assert len(registry) == 4


def test_reports_bad_missing_and_duplicate_records_without_failing(
    tmp_path: Path,
) -> None:
    _write_place(tmp_path, "a.json", place_id="same", latitude=16.0, longitude=108.0)
    _write_place(tmp_path, "b.json", place_id="same", latitude=16.1, longitude=108.1)
    _write_place(tmp_path, "missing.json", place_id="missing", latitude=None, longitude=None)
    (tmp_path / "broken.json").write_text("{broken", encoding="utf-8")

    registry = AccessPointRegistry(tmp_path)

    codes = {issue.code for issue in registry.last_report.issues}
    assert codes == {
        "duplicate_place_id",
        "invalid_current_place",
        "missing_location",
    }
    assert registry.last_report.skipped_records == 3
    assert registry.resolve("same").owner_entity_id == "same"
    assert registry.resolve("city_da_nang").access_type == AccessPointType.CITY_CENTER


def test_get_list_stats_and_reload_return_isolated_snapshots(tmp_path: Path) -> None:
    _write_place(tmp_path, "one.json", place_id="one", latitude=16.0, longitude=108.0)
    registry = AccessPointRegistry(tmp_path)

    fetched = registry.get("place:one:main")
    fetched.name = "mutated by caller"

    assert registry.get("place:one:main").name == "Place one"
    assert [item.access_point_id for item in registry.list()] == [
        "city:city_da_nang:center",
        "place:one:main",
    ]
    assert registry.stats()["by_origin"] == {"city": 1, "place": 1}
    assert "one" in registry
    assert "unknown" not in registry

    _write_place(tmp_path, "two.json", place_id="two", latitude=16.2, longitude=108.2)
    report = registry.reload()
    assert report.place_access_points_loaded == 2
    assert registry.resolve("two").owner_entity_id == "two"

    with pytest.raises(AccessPointNotFoundError):
        registry.resolve("unknown")


def test_curated_overrides_add_hubs_replace_generated_records_and_add_aliases(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    _write_place(current, "one.json", place_id="one", latitude=16.0, longitude=108.0)
    overrides = tmp_path / "access-points.json"
    overrides.write_text(
        json.dumps(
            {
                "access_points": [
                    {
                        "access_point_id": "place:one:main",
                        "owner_entity_id": "one",
                        "access_type": "parking",
                        "name": "Curated car entrance",
                        "location": {"latitude": 16.01, "longitude": 108.01},
                        "supported_modes": ["drive"],
                        "source_record_ids": ["manual-one"],
                        "verification_status": "human_verified",
                        "updated_at": NOW,
                        "aliases": ["one-parking"],
                    },
                    {
                        "access_point_id": "hub:dad",
                        "owner_entity_id": "airport_da_nang",
                        "access_type": "airport",
                        "name": "Da Nang Airport",
                        "location": {"latitude": 16.0439, "longitude": 108.1994},
                        "supported_modes": ["drive"],
                        "source_record_ids": ["manual-dad"],
                        "verification_status": "human_verified",
                        "updated_at": NOW,
                    },
                ],
                "aliases": {"DAD": "hub:dad"},
            }
        ),
        encoding="utf-8",
    )

    registry = AccessPointRegistry(current, overrides_path=overrides)

    assert registry.resolve("one").access_type == AccessPointType.PARKING
    assert registry.resolve("one-parking").access_type == AccessPointType.PARKING
    assert registry.resolve("DAD").access_point_id == "hub:dad"
    assert registry.last_report.override_access_points_loaded == 2
    assert "generated_access_point_overridden" in {
        issue.code for issue in registry.last_report.issues
    }
    assert registry.stats()["by_origin"] == {
        "city": 1,
        "override": 2,
    }


def test_duplicate_override_and_unknown_alias_are_reported(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    overrides = tmp_path / "access-points.json"
    base = {
        "access_point_id": "hub:station",
        "owner_entity_id": "station",
        "access_type": "station",
        "location": {"latitude": 13.77, "longitude": 109.22},
        "supported_modes": ["drive", "transit"],
        "source_record_ids": ["manual-station"],
        "verification_status": "human_verified",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    overrides.write_text(
        json.dumps(
            {
                "access_points": [base, base],
                "aliases": {"missing": "hub:not-found"},
            }
        ),
        encoding="utf-8",
    )

    registry = AccessPointRegistry(current, overrides_path=overrides)

    codes = {issue.code for issue in registry.last_report.issues}
    assert "duplicate_override_access_point_id" in codes
    assert "unknown_override_alias_target" in codes
    assert registry.resolve("hub:station").owner_entity_id == "station"
