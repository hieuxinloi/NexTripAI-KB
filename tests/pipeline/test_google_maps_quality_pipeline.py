from __future__ import annotations

from datetime import datetime, timezone

from nextrip_pipeline.crawl import RawJsonWriter, compute_content_hash
from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionGate,
    GoogleMapsDecisionStatus,
    GoogleMapsDecisionWriter,
)
from nextrip_pipeline.jobs import GoogleMapsRefreshPipeline
from nextrip_pipeline.preprocessing import (
    GoogleMapsPlaceNormalizer,
    NormalizedGoogleMapsWriter,
)
from nextrip_pipeline.publishing import CurrentPlaceWriter
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueConfig,
    LLMReviewQueueDisposition,
    LLMReviewRequestWriter,
    MappingResolutionStatus,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    RecordSubjectType,
    SourceRecord,
)
from nextrip_pipeline.validators import (
    GoogleMapsValidationWriter,
    GoogleMapsValidatorOrchestrator,
)


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)
GOOGLE_TOKEN = "0x31421b00723d9291:0x46d9f1c4fa5c9f78"
GOOGLE_URL = (
    "https://www.google.com/maps/place/Cafe-One/"
    f"data=!4m7!3m6!1s{GOOGLE_TOKEN}!8m2"
)


class RecordAdapter:
    def __init__(self, *, include_google_coordinates: bool) -> None:
        self.include_google_coordinates = include_google_coordinates

    def fetch(self, mapping, *, run_id: str) -> SourceRecord:
        coordinate_html = (
            "!3d16.0601!4d108.2201" if self.include_google_coordinates else ""
        )
        raw_payload = {
            "page": {
                "requested_url": "https://www.google.com/maps/search/Cafe-One",
                "final_url": GOOGLE_URL,
                "title": "Cafe One - Google Maps",
                "html": f"<title>Cafe One - Google Maps</title>Open now {coordinate_html}",
                "used_master_coordinates_for_viewport": (
                    not self.include_google_coordinates
                ),
                "structured_data": {
                    "name": "Cafe One",
                    "category": "Cafe",
                    "address": "1 Bach Dang, Da Nang, Vietnam",
                    "aria_labels": [
                        "Wednesday, Open 24 hours, Copy open hours"
                    ],
                    "image_urls": ["https://example.com/cover.jpg"],
                },
            }
        }
        return SourceRecord(
            source_record_id=f"source-{run_id}",
            run_id=run_id,
            source_id="google-maps-web",
            entity_type=EntityType.CAFE,
            subject_type=RecordSubjectType.OPENING_STATUS,
            subject_id=mapping.entity_id,
            crawled_at=NOW,
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version="1.0.0",
            source_url=GOOGLE_URL,
            http_status=200,
            content_type="text/html",
        )


def _mapping() -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="maps-cafe-1",
        entity_id="cafe-1",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Cafe One",
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.7,
        matched_at=NOW,
        attributes={
            "master_name": "Cafe One",
            "master_address": "1 Bach Dang, Da Nang, Vietnam",
            "master_city": "Da Nang",
            "city_id": "city_da_nang",
            "master_latitude": 16.06,
            "master_longitude": 108.22,
        },
    )


def _pipeline(tmp_path, *, include_google_coordinates: bool):
    return GoogleMapsRefreshPipeline(
        RecordAdapter(include_google_coordinates=include_google_coordinates),
        RawJsonWriter(tmp_path / "raw"),
        NormalizedGoogleMapsWriter(tmp_path / "normalized"),
        GoogleMapsValidationWriter(tmp_path / "validation"),
        GoogleMapsDecisionWriter(tmp_path / "decision"),
        normalizer=GoogleMapsPlaceNormalizer(),
        validator=GoogleMapsValidatorOrchestrator(clock=lambda: NOW),
        decision_gate=GoogleMapsDecisionGate(clock=lambda: NOW),
        mapping_resolver=GoogleMapsMappingResolver(clock=lambda: NOW),
        resolution_writer=GoogleMapsMappingResolutionWriter(tmp_path / "resolution"),
        current_mapping_writer=CurrentGoogleMapsMappingWriter(
            tmp_path / "current-mapping"
        ),
        llm_review_writer=LLMReviewRequestWriter(
            LLMReviewQueueConfig(root_directory=tmp_path / "llm-review"),
            clock=lambda: NOW,
        ),
        current_place_writer=CurrentPlaceWriter(
            tmp_path / "current-place", clock=lambda: NOW
        ),
    )


def test_strong_mapping_is_confirmed_and_published_current(tmp_path) -> None:
    result = _pipeline(tmp_path, include_google_coordinates=True).run(
        _mapping(), run_id="quality-pass"
    )

    assert result.mapping_resolution is not None
    assert result.mapping_resolution.status is MappingResolutionStatus.AUTO_CONFIRM
    assert result.decision.status is GoogleMapsDecisionStatus.PASS
    assert result.current_mapping_path is not None
    assert result.current_place_path is not None
    assert result.current_mapping_path.exists()
    assert result.current_place_path.exists()
    assert result.llm_review_receipt is None


def test_missing_google_geo_is_bounded_review_and_not_current(tmp_path) -> None:
    result = _pipeline(tmp_path, include_google_coordinates=False).run(
        _mapping(), run_id="quality-review"
    )

    assert result.mapping_resolution is not None
    assert result.mapping_resolution.status is MappingResolutionStatus.REVIEW
    assert result.decision.status is GoogleMapsDecisionStatus.REVIEW
    assert result.current_mapping_path is None
    assert result.current_place_path is None
    assert result.llm_review_receipt is not None
    assert (
        result.llm_review_receipt.disposition
        is LLMReviewQueueDisposition.QUEUED
    )
