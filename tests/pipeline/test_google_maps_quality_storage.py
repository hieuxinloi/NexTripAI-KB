from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nextrip_pipeline.quality import (
    apply_rejected_resolution,
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingResolutionWriter,
    OlderResolvedMappingError,
)
from nextrip_pipeline.quality.google_maps_mapping import (
    GoogleMapsMappingResolution,
    MappingResolutionStatus,
    PlaceIdentityEvidence,
    PlaceIdentitySnapshot,
)
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping, MappingStatus


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def _mapping(verified_at: datetime = NOW) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="maps-cafe-1",
        entity_id="cafe-1",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Cafe One",
        external_url="https://www.google.com/maps/place/cafe-one",
        status=MappingStatus.CONFIRMED,
        confidence=0.9,
        matched_at=NOW,
        verified_at=verified_at,
        last_checked_at=verified_at,
    )


def _resolution() -> GoogleMapsMappingResolution:
    snapshot = PlaceIdentitySnapshot(name="Cafe One", city="Da Nang")
    return GoogleMapsMappingResolution(
        resolver_version="1.0.0",
        mapping_id="maps-cafe-1",
        place_id="cafe-1",
        observation_id="observation-1",
        source_record_id="source-1",
        status=MappingResolutionStatus.AUTO_CONFIRM,
        score=0.9,
        reason_codes=("STRONG_IDENTITY_MATCH",),
        evidence_hash="0" * 64,
        evidence=PlaceIdentityEvidence(master=snapshot, observed=snapshot),
        resolved_at=NOW,
    )


def test_resolution_history_is_immutable(tmp_path) -> None:
    writer = GoogleMapsMappingResolutionWriter(tmp_path)
    path = writer.write(_resolution(), run_id="quality-run")

    assert path.exists()
    with pytest.raises(FileExistsError):
        writer.write(_resolution(), run_id="quality-run")


def test_current_mapping_is_atomic_and_rejects_older_evidence(tmp_path) -> None:
    writer = CurrentGoogleMapsMappingWriter(tmp_path)
    newest = _mapping()

    path = writer.publish(newest)

    assert writer.get("cafe-1") == newest
    assert path == tmp_path / "cafe-1.json"
    with pytest.raises(OlderResolvedMappingError):
        writer.publish(_mapping(NOW - timedelta(hours=1)))
    assert writer.get("cafe-1") == newest


def test_current_mapping_rejects_unresolved_input(tmp_path) -> None:
    writer = CurrentGoogleMapsMappingWriter(tmp_path)
    candidate = _mapping().model_copy(
        update={"status": MappingStatus.AUTO_MATCHED, "verified_at": None}
    )

    with pytest.raises(ValueError, match="only confirmed or rejected"):
        writer.publish(candidate)


def test_hard_rejection_is_persisted_as_ineligible_overlay(tmp_path) -> None:
    writer = CurrentGoogleMapsMappingWriter(tmp_path)
    resolution = _resolution().model_copy(
        update={
            "status": MappingResolutionStatus.REJECT,
            "reason_codes": ("CITY_CONFLICT",),
        }
    )
    rejected = apply_rejected_resolution(_mapping(), resolution)

    writer.publish(rejected)

    assert writer.get("cafe-1").status is MappingStatus.REJECTED
    assert rejected.attributes["mapping_rejection_reason_codes"] == [
        "CITY_CONFLICT"
    ]
