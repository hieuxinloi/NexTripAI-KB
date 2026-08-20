from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from nextrip_pipeline.quality.google_maps_mapping import (
    GoogleMapsMappingResolver,
    LLMMappingReviewResponse,
    LLMReviewQueueConfig,
    LLMReviewQueueDisposition,
    LLMReviewRequestWriter,
    MappingResolutionStatus,
    apply_auto_confirmation,
)
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    GoogleMapsPlaceObservation,
    MappingStatus,
    OpeningStatusObservation,
    ValidationStatus,
)
from nextrip_pipeline.validators import GoogleMapsValidatorOrchestrator


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _mapping() -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="google-maps-cafe-dn-062",
        entity_id="cafe_dn_062",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Quán cà phê Xóm Mèo Coffee & Petshop Đà Nẵng",
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.7,
        matched_at=NOW,
        attributes={
            "master_name": "Quán cà phê Xóm Mèo Coffee & Petshop Đà Nẵng",
            "master_address": "123 Trần Hưng Đạo, Sơn Trà, Đà Nẵng",
            "master_city": "Đà Nẵng",
            "master_latitude": 16.0621,
            "master_longitude": 108.2350,
        },
    )


def _observation(
    *,
    observation_id: str = "maps-record-1:place-status",
    source_record_id: str = "maps-record-1",
    name: str = "Xóm Mèo Coffee",
    address: str = "123 Trần Hưng Đạo, Sơn Trà, Đà Nẵng, Vietnam",
    category: str = "Coffee shop",
    latitude: float = 16.0622,
    longitude: float = 108.2351,
    location_source: str = "google-maps-web",
    accuracy: str = "google_maps_place_page",
) -> GoogleMapsPlaceObservation:
    opening = OpeningStatusObservation(
        observation_id=f"{source_record_id}:opening",
        run_id="maps-run-1",
        place_id="cafe_dn_062",
        source_record_ids=[source_record_id],
        local_date=date(2026, 8, 19),
        status=DailyOpeningStatus.UNKNOWN,
        observed_at=NOW,
    )
    return GoogleMapsPlaceObservation(
        observation_id=observation_id,
        run_id="maps-run-1",
        place_id="cafe_dn_062",
        source_record_id=source_record_id,
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/xom-meo",
        name=name,
        address=address,
        category=category,
        location=GeoPoint(
            latitude=latitude,
            longitude=longitude,
            source=location_source,
            accuracy=accuracy,
        ),
        opening=opening,
        observed_at=NOW,
    )


def test_strong_safe_identity_is_auto_confirmed_and_materialized() -> None:
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    observation = _observation()

    resolution, confirmed = resolver.resolve_and_update(
        _mapping(),
        observation,
        canonical_url="https://www.google.com/maps/place/canonical-xom-meo",
    )

    assert resolution.status is MappingResolutionStatus.AUTO_CONFIRM
    assert resolution.reason_codes == ("STRONG_IDENTITY_MATCH",)
    assert resolution.evidence.city_score == 1
    assert resolution.evidence.coordinate_distance_m < 500
    assert confirmed.status is MappingStatus.CONFIRMED
    assert confirmed.external_id == "Xóm Mèo Coffee"
    assert str(confirmed.external_url) == (
        "https://www.google.com/maps/place/canonical-xom-meo"
    )
    assert confirmed.source_record_ids == ["maps-record-1"]
    assert confirmed.attributes["mapping_evidence_hash"] == resolution.evidence_hash


def test_auto_confirmed_multifield_identity_is_not_rejected_by_name_only_check(
) -> None:
    mapping = _mapping().model_copy(
        update={
            "external_id": "Quán cafe Châu Sơn Đà Nẵng",
            "attributes": {
                **_mapping().attributes,
                "master_name": "Quán cafe Châu Sơn Đà Nẵng",
            },
        }
    )
    observation = _observation(name="Cafe Châu Sơn 1 (Châu Nga)")
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    resolution, confirmed = resolver.resolve_and_update(mapping, observation)

    assert resolution.status is MappingResolutionStatus.AUTO_CONFIRM
    validations = GoogleMapsValidatorOrchestrator(clock=lambda: NOW).validate(
        observation,
        confirmed,
        mapping_resolution=resolution,
    )
    identity = next(
        item for item in validations if item.validator == "GoogleMapsIdentityValidator"
    )
    assert identity.status is ValidationStatus.PASS


def test_verified_master_coordinate_fallback_is_not_google_geo_evidence() -> None:
    observation = _observation(
        location_source="verified-master-data",
        accuracy="verified_master_fallback",
    )

    resolution = GoogleMapsMappingResolver(clock=lambda: NOW).resolve(
        _mapping(), observation
    )

    assert resolution.status is MappingResolutionStatus.REVIEW
    assert resolution.evidence.coordinate_distance_m is None
    assert "COORDINATE_EVIDENCE_MISSING" in resolution.reason_codes


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (
            _observation(address="12 Xuân Diệu, Quy Nhơn, Gia Lai, Vietnam"),
            "CITY_CONFLICT",
        ),
        (
            _observation(latitude=15.95, longitude=108.10),
            "COORDINATE_CONFLICT",
        ),
    ],
)
def test_hard_city_or_coordinate_conflict_is_rejected(
    observation: GoogleMapsPlaceObservation, reason: str
) -> None:
    resolution = GoogleMapsMappingResolver(clock=lambda: NOW).resolve(
        _mapping(), observation
    )

    assert resolution.status is MappingResolutionStatus.REJECT
    assert reason in resolution.reason_codes


def test_evidence_hash_ignores_run_provenance_but_tracks_identity_content() -> None:
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    first = resolver.resolve(_mapping(), _observation())
    second_observation = _observation(
        observation_id="maps-record-2:place-status",
        source_record_id="maps-record-2",
    ).model_copy(update={"run_id": "maps-run-2"})
    second = resolver.resolve(_mapping(), second_observation)
    changed = resolver.resolve(
        _mapping(),
        _observation(name="Một quán cà phê hoàn toàn khác"),
    )

    assert first.evidence_hash == second.evidence_hash
    assert changed.evidence_hash != first.evidence_hash


def test_review_writer_is_immutable_cached_and_bounded(tmp_path) -> None:
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    review = resolver.resolve(
        _mapping(),
        _observation(
            location_source="verified-master-data",
            accuracy="verified_master_fallback",
        ),
    )
    writer = LLMReviewRequestWriter(
        LLMReviewQueueConfig(
            root_directory=tmp_path,
            max_requests_per_run=1,
            max_tokens_per_run=5000,
        ),
        clock=lambda: NOW,
    )

    first = writer.enqueue(review, run_id="quality-run-1")
    assert first.disposition is LLMReviewQueueDisposition.QUEUED
    content = writer.request_path(review.evidence_hash).read_bytes()
    cached = writer.enqueue(review, run_id="quality-run-2")
    assert cached.disposition is LLMReviewQueueDisposition.CACHED
    assert writer.request_path(review.evidence_hash).read_bytes() == content

    another_review = resolver.resolve(
        _mapping(),
        _observation(
            name="Xóm Mèo Petshop Coffee",
            location_source="verified-master-data",
            accuracy="verified_master_fallback",
        ),
    )
    capped = writer.enqueue(another_review, run_id="quality-run-1")
    assert capped.disposition is LLMReviewQueueDisposition.RUN_REQUEST_CAP_REACHED
    assert not writer.request_path(another_review.evidence_hash).exists()


def test_review_writer_enforces_per_request_token_limit_and_skips_pass(tmp_path) -> None:
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    review = resolver.resolve(
        _mapping(),
        _observation(
            location_source="verified-master-data",
            accuracy="verified_master_fallback",
        ),
    )
    writer = LLMReviewRequestWriter(
        LLMReviewQueueConfig(
            root_directory=tmp_path,
            max_input_tokens_per_request=1,
        ),
        clock=lambda: NOW,
    )

    limited = writer.enqueue(review, run_id="quality-run")
    assert (
        limited.disposition
        is LLMReviewQueueDisposition.REQUEST_TOKEN_LIMIT_EXCEEDED
    )
    assert not writer.request_path(review.evidence_hash).exists()

    auto_confirm = resolver.resolve(_mapping(), _observation())
    skipped = writer.enqueue(auto_confirm, run_id="quality-run")
    assert skipped.disposition is LLMReviewQueueDisposition.NOT_REVIEWABLE


def test_auto_confirmation_helper_and_llm_response_are_strict() -> None:
    resolver = GoogleMapsMappingResolver(clock=lambda: NOW)
    observation = _observation()
    resolution = resolver.resolve(_mapping(), observation)
    confirmed = apply_auto_confirmation(_mapping(), observation, resolution)
    assert confirmed.verified_at == NOW

    with pytest.raises(ValidationError):
        LLMMappingReviewResponse.model_validate(
            {
                "request_id": "request-1",
                "evidence_hash": "0" * 64,
                "verdict": "same_place",
                "confidence": 0.9,
                "rationale": "Names, city and address agree.",
                "unexpected": True,
            }
        )
