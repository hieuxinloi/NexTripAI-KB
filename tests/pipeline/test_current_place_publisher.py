from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.publishing import current_place as publisher_module
from nextrip_pipeline.publishing.current_place import (
    CurrentPlaceIdentityError,
    CurrentPlaceWriter,
    OlderPlaceObservationError,
)
from nextrip_pipeline.schemas.common import EntityType, VerificationStatus
from nextrip_pipeline.schemas.external_mapping import (
    ExternalEntityMapping,
    MappingStatus,
)
from nextrip_pipeline.schemas.google_maps import GoogleMapsPlaceObservation
from nextrip_pipeline.schemas.media import (
    PlaceMediaAsset,
    PlaceMediaObservation,
    PlaceMediaRole,
)
from nextrip_pipeline.schemas.opening_status import (
    DailyOpeningSchedule,
    DailyOpeningStatus,
    OpeningStatusObservation,
    Weekday,
    WeeklyOpeningScheduleObservation,
)
from nextrip_pipeline.schemas.place import BusinessStatus, GeoPoint


UTC = timezone.utc
NOW = datetime(2026, 8, 19, 3, tzinfo=UTC)


def _mapping(*, city: str = "Đà Nẵng") -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="google-maps-cafe-dn-001",
        entity_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Master Cafe",
        status=MappingStatus.CONFIRMED,
        confidence=1,
        matched_at=NOW,
        verified_at=NOW,
        attributes={"master_city": city, "city_id": "city_da_nang"},
    )


def _observation(
    *,
    suffix: str = "1",
    observed_at: datetime = NOW,
    business_status: BusinessStatus = BusinessStatus.ACTIVE,
) -> GoogleMapsPlaceObservation:
    source_record_id = f"source-{suffix}"
    opening_status = {
        BusinessStatus.ACTIVE: DailyOpeningStatus.OPEN_TODAY,
        BusinessStatus.TEMPORARILY_CLOSED: DailyOpeningStatus.TEMPORARILY_CLOSED,
        BusinessStatus.PERMANENTLY_CLOSED: DailyOpeningStatus.PERMANENTLY_CLOSED,
        BusinessStatus.UNKNOWN: DailyOpeningStatus.UNKNOWN,
    }[business_status]
    opening = OpeningStatusObservation(
        observation_id=f"{source_record_id}:opening",
        run_id=f"run-{suffix}",
        place_id="cafe_dn_001",
        source_record_ids=[source_record_id],
        local_date=date(2026, 8, 19),
        status=opening_status,
        is_24_hours=business_status is BusinessStatus.ACTIVE,
        open_now=business_status is BusinessStatus.ACTIVE,
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    weekly = WeeklyOpeningScheduleObservation(
        observation_id=f"{source_record_id}:weekly",
        run_id=f"run-{suffix}",
        place_id="cafe_dn_001",
        source_record_ids=[source_record_id],
        days=[DailyOpeningSchedule(day=Weekday.WEDNESDAY, open_24_hours=True)],
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    media = PlaceMediaObservation(
        observation_id=f"{source_record_id}:media",
        run_id=f"run-{suffix}",
        place_id="cafe_dn_001",
        source_record_id=source_record_id,
        source_url="https://www.google.com/maps/place/cafe",
        assets=[
            PlaceMediaAsset(
                url=f"https://example.com/cover-{suffix}.jpg",
                role=PlaceMediaRole.COVER,
            )
        ],
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    return GoogleMapsPlaceObservation(
        observation_id=f"observation-{suffix}",
        run_id=f"run-{suffix}",
        place_id="cafe_dn_001",
        source_record_id=source_record_id,
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/cafe",
        name=f"Google Cafe {suffix}",
        address=f"Google address {suffix}",
        location=GeoPoint(latitude=16.06, longitude=108.22),
        business_status=business_status,
        opening=opening,
        weekly_opening=weekly,
        media=media,
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )


def _decision(
    observation: GoogleMapsPlaceObservation,
    status: GoogleMapsDecisionStatus = GoogleMapsDecisionStatus.PASS,
) -> GoogleMapsDecision:
    return GoogleMapsDecision(
        decision_id=f"{observation.observation_id}:decision",
        run_id=observation.run_id,
        observation_id=observation.observation_id,
        place_id=observation.place_id,
        status=status,
        validation_ids=[f"{observation.observation_id}:identity"],
        decided_at=observation.observed_at + timedelta(minutes=1),
    )


def test_pass_publishes_master_identity_and_google_mutable_fields(
    tmp_path: Path,
) -> None:
    observation = _observation()
    writer = CurrentPlaceWriter(tmp_path, clock=lambda: NOW + timedelta(minutes=2))

    path = writer.publish(observation, _decision(observation), _mapping())
    current = writer.get("cafe_dn_001")

    assert path == tmp_path / "cafe_dn_001.json"
    assert current is not None
    assert current.place_id == "cafe_dn_001"
    assert current.entity_type is EntityType.CAFE
    assert current.city == "Đà Nẵng"
    assert current.city_id == "city_da_nang"
    assert current.name == "Google Cafe 1"
    assert current.address == "Google address 1"
    assert current.stale_after == observation.observed_at + timedelta(days=1)
    assert current.is_stale(observation.observed_at + timedelta(days=2))
    assert current.location == observation.location
    assert current.opening == observation.opening
    assert current.weekly_opening == observation.weekly_opening
    assert str(current.cover_image_url) == "https://example.com/cover-1.jpg"
    assert current.provenance.source_record_id == "source-1"
    assert current.provenance.validation_ids == ["observation-1:identity"]


@pytest.mark.parametrize(
    "status",
    [GoogleMapsDecisionStatus.REVIEW, GoogleMapsDecisionStatus.QUARANTINE],
)
def test_non_pass_decision_cannot_overwrite_last_known_good(
    tmp_path: Path,
    status: GoogleMapsDecisionStatus,
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    accepted = _observation()
    path = writer.publish(accepted, _decision(accepted), _mapping())
    original = path.read_bytes()
    rejected = _observation(suffix="2", observed_at=NOW + timedelta(hours=1))

    with pytest.raises(ValueError, match="only PASS"):
        writer.publish(rejected, _decision(rejected, status), _mapping())

    assert path.read_bytes() == original


def test_older_observation_is_rejected_without_overwriting_current(
    tmp_path: Path,
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    newest = _observation(suffix="new", observed_at=NOW)
    path = writer.publish(newest, _decision(newest), _mapping())
    original = path.read_bytes()
    older = _observation(suffix="old", observed_at=NOW - timedelta(hours=1))

    with pytest.raises(OlderPlaceObservationError):
        writer.publish(older, _decision(older), _mapping())

    assert path.read_bytes() == original


def test_equal_timestamp_can_rebuild_projection_from_the_same_source_record(
    tmp_path: Path,
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    original = _observation(suffix="same", observed_at=NOW)
    writer.publish(original, _decision(original), _mapping())
    rebuilt = original.model_copy(
        update={
            "observation_id": "observation-same-reprocessed",
            "run_id": "run-same-reprocessed",
            "opening": original.opening.model_copy(
                update={"verification_status": VerificationStatus.PENDING_REVIEW}
            ),
            "weekly_opening": original.weekly_opening.model_copy(
                update={"verification_status": VerificationStatus.PENDING_REVIEW}
            ),
        }
    )

    writer.publish(rebuilt, _decision(rebuilt), _mapping())
    current = writer.get("cafe_dn_001")

    assert current.provenance.observation_id == rebuilt.observation_id
    assert current.opening.verification_status is VerificationStatus.AUTO_VERIFIED
    assert (
        current.weekly_opening.verification_status
        is VerificationStatus.AUTO_VERIFIED
    )


def test_equal_timestamp_from_a_different_source_record_is_rejected(
    tmp_path: Path,
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    accepted = _observation(suffix="first", observed_at=NOW)
    writer.publish(accepted, _decision(accepted), _mapping())
    conflicting = _observation(suffix="second", observed_at=NOW)

    with pytest.raises(OlderPlaceObservationError):
        writer.publish(conflicting, _decision(conflicting), _mapping())


def test_permanently_closed_place_is_published_instead_of_deleted(
    tmp_path: Path,
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    active = _observation()
    path = writer.publish(active, _decision(active), _mapping())
    closed = _observation(
        suffix="closed",
        observed_at=NOW + timedelta(days=1),
        business_status=BusinessStatus.PERMANENTLY_CLOSED,
    )

    writer.publish(closed, _decision(closed), _mapping())

    assert path.exists()
    assert writer.get("cafe_dn_001").business_status is BusinessStatus.PERMANENTLY_CLOSED


def test_master_identity_change_is_rejected(tmp_path: Path) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    accepted = _observation()
    path = writer.publish(accepted, _decision(accepted), _mapping())
    original = path.read_bytes()
    newer = _observation(suffix="2", observed_at=NOW + timedelta(hours=1))

    with pytest.raises(CurrentPlaceIdentityError):
        writer.publish(newer, _decision(newer), _mapping(city="Quy Nhơn"))

    assert path.read_bytes() == original


def test_failed_atomic_replace_preserves_last_known_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = CurrentPlaceWriter(tmp_path)
    accepted = _observation()
    path = writer.publish(accepted, _decision(accepted), _mapping())
    original = path.read_bytes()
    newer = _observation(suffix="2", observed_at=NOW + timedelta(hours=1))

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"cannot replace {source} with {destination}")

    monkeypatch.setattr(publisher_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="cannot replace"):
        writer.publish(newer, _decision(newer), _mapping())

    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
