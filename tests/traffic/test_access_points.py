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
from tests.canonical_dataset_support import (
    CanonicalTestPlace,
    write_canonical_dataset,
)


NOW = "2026-08-19T12:00:00Z"


def test_loads_places_and_derives_city_center_from_verified_median(
    tmp_path: Path,
) -> None:
    dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [
            CanonicalTestPlace("one", "Place one", 16.0, 108.0),
            CanonicalTestPlace("two", "Place two", 18.0, 110.0),
        ],
    )

    registry = AccessPointRegistry(canonical_dataset_path=dataset)

    assert registry.resolve("one").access_point_id == "place:one:main"
    assert registry.resolve("place:two:main").owner_entity_id == "two"
    center = registry.resolve("city_da_nang")
    assert center.access_point_id == "city:city_da_nang:center"
    assert center.access_type == AccessPointType.CITY_CENTER
    assert center.location.latitude == 17.0
    assert center.location.longitude == 109.0
    assert center.verification_status == VerificationStatus.AUTO_VERIFIED
    assert center.source_record_ids == [
        "derived:median:canonical-place:city_da_nang:count=2"
    ]
    assert TransportMode.TRANSIT not in center.supported_modes
    assert len(registry) == 3


def test_invalid_canonical_is_reported(
    tmp_path: Path,
) -> None:
    invalid_dataset = tmp_path / "invalid-canonical.json"
    invalid_dataset.write_text("{broken", encoding="utf-8")

    registry = AccessPointRegistry(canonical_dataset_path=invalid_dataset)

    codes = {issue.code for issue in registry.last_report.issues}
    assert codes == {"invalid_canonical_dataset"}
    assert registry.last_report.skipped_records == 1
    assert len(registry) == 0
    with pytest.raises(AccessPointNotFoundError):
        registry.resolve("one")


def test_registry_requires_canonical_dataset_argument() -> None:
    with pytest.raises(TypeError, match="canonical_dataset_path"):
        AccessPointRegistry()  # type: ignore[call-arg]


def test_get_list_stats_and_reload_return_isolated_snapshots(tmp_path: Path) -> None:
    first_dataset = write_canonical_dataset(
        tmp_path / "canonical-first",
        [CanonicalTestPlace("one", "Place one", 16.0, 108.0)],
    )
    registry = AccessPointRegistry(canonical_dataset_path=first_dataset)

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

    second_dataset = write_canonical_dataset(
        tmp_path / "canonical-second",
        [
            CanonicalTestPlace("one", "Place one", 16.0, 108.0),
            CanonicalTestPlace("two", "Place two", 16.2, 108.2),
        ],
    )
    registry.canonical_dataset_path = second_dataset
    report = registry.reload()
    assert report.place_access_points_loaded == 2
    assert registry.resolve("two").owner_entity_id == "two"

    with pytest.raises(AccessPointNotFoundError):
        registry.resolve("unknown")


def test_curated_overrides_add_hubs_replace_generated_records_and_add_aliases(
    tmp_path: Path,
) -> None:
    dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [CanonicalTestPlace("one", "Place one", 16.0, 108.0)],
    )
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

    registry = AccessPointRegistry(
        canonical_dataset_path=dataset,
        overrides_path=overrides,
    )

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
    dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [CanonicalTestPlace("one", "Place one", 16.0, 108.0)],
    )
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

    registry = AccessPointRegistry(
        canonical_dataset_path=dataset,
        overrides_path=overrides,
    )

    codes = {issue.code for issue in registry.last_report.issues}
    assert "duplicate_override_access_point_id" in codes
    assert "unknown_override_alias_target" in codes
    assert registry.resolve("hub:station").owner_entity_id == "station"
