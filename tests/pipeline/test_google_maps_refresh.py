from __future__ import annotations

from datetime import datetime, timezone

from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionGate,
    GoogleMapsDecisionStatus,
)
import pytest

from nextrip_pipeline.preprocessing import (
    GoogleMapsNormalizationError,
    GoogleMapsPlaceNormalizer,
)
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    RecordSubjectType,
    SourceRecord,
    ValidationStatus,
)
from nextrip_pipeline.validators import GoogleMapsValidatorOrchestrator


NOW = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)


def _mapping(status: MappingStatus) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="maps-eo-gio",
        entity_id="attr_qn_001",
        entity_type=EntityType.ATTRACTION,
        source_id="google-maps-web",
        external_id="Eo Gió Quy Nhơn",
        status=status,
        confidence=1,
        matched_at=NOW,
        verified_at=NOW if status is MappingStatus.CONFIRMED else None,
        attributes={
            "master_name": "Eo Gió",
            "master_latitude": 13.8863066,
            "master_longitude": 109.2925910,
        },
    )


def _record() -> SourceRecord:
    html = "<html><head><title>Eo Gió - Google Maps</title></head><body>Đang mở cửa "
    html += (
        "https://maps.google.com/place/x/data=!3d13.8861097!4d109.2925926</body></html>"
    )
    return SourceRecord(
        source_record_id="maps-record-1",
        run_id="maps-run-1",
        source_id="google-maps-web",
        entity_type=EntityType.ATTRACTION,
        subject_type=RecordSubjectType.OPENING_STATUS,
        subject_id="attr_qn_001",
        crawled_at=NOW,
        raw_payload={
            "page": {
                "html": html,
                "title": "Eo Gió - Google Maps",
                "final_url": "https://www.google.com/maps/place/eo-gio",
            }
        },
        content_hash="0" * 64,
        parser_version="1.0.0",
        source_url="https://www.google.com/maps/place/eo-gio",
        http_status=200,
    )


def test_confirmed_google_place_passes_validation_and_decision() -> None:
    mapping = _mapping(MappingStatus.CONFIRMED)
    observation = GoogleMapsPlaceNormalizer().normalize(_record(), mapping)
    validations = GoogleMapsValidatorOrchestrator(clock=lambda: NOW).validate(
        observation, mapping
    )
    decision = GoogleMapsDecisionGate(clock=lambda: NOW).decide(
        observation, validations
    )
    assert observation.name == "Eo Gió"
    assert observation.opening.status is DailyOpeningStatus.OPEN_TODAY
    assert observation.opening.open_now is True
    assert observation.location is not None
    assert all(item.status is ValidationStatus.PASS for item in validations)
    assert decision.status is GoogleMapsDecisionStatus.PASS


def test_auto_matched_google_place_waits_for_human_review() -> None:
    mapping = _mapping(MappingStatus.AUTO_MATCHED)
    observation = GoogleMapsPlaceNormalizer().normalize(_record(), mapping)
    validations = GoogleMapsValidatorOrchestrator(clock=lambda: NOW).validate(
        observation, mapping
    )
    decision = GoogleMapsDecisionGate(clock=lambda: NOW).decide(
        observation, validations
    )

    assert any(item.status is ValidationStatus.WARN for item in validations)
    assert decision.status is GoogleMapsDecisionStatus.REVIEW
    assert decision.reason_codes == ["MAPPING_NEEDS_CONFIRMATION"]


def test_generic_google_maps_shell_is_rejected() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<html><title>Google Maps</title></html>",
                    "title": "Google Maps",
                    "final_url": "https://www.google.com/maps",
                    "structured_data": {"name": "Google Maps"},
                }
            }
        }
    )

    with pytest.raises(
        GoogleMapsNormalizationError,
        match="place detail was not resolved",
    ):
        GoogleMapsPlaceNormalizer().normalize(
            record, _mapping(MappingStatus.CONFIRMED)
        )


def test_master_coordinates_are_retained_when_maps_spa_omits_place_coordinates() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo GiÃ³ - Google Maps</title>Open now",
                    "title": "Eo GiÃ³ - Google Maps",
                    "final_url": "https://www.google.com/maps/search/Eo-Gio",
                    "used_master_coordinates_for_viewport": True,
                    "structured_data": {"name": "Eo GiÃ³"},
                }
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.location is not None
    assert observation.location.latitude == 13.8863066
    assert observation.location.longitude == 109.292591
    assert observation.location.accuracy == "verified_master_fallback"
    assert observation.location.source == "verified-master-data"


def test_closed_now_is_not_misclassified_as_closed_for_entire_day() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": (
                        "<title>Eo Gió, Quy Nhơn, Việt Nam - Google Maps</title>"
                        "Đã đóng cửa · Mở cửa lúc 5:30 Thứ 4 "
                        "!3d13.8861097!4d109.2925926"
                    ),
                    "title": "Eo Gió, Quy Nhơn, Việt Nam - Google Maps",
                    "final_url": "https://www.google.com/maps/place/eo-gio",
                }
            }
        }
    )
    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.opening.open_now is False
    assert observation.opening.status is DailyOpeningStatus.UNKNOWN
    assert observation.opening.raw_status_text == "Đã đóng cửa · Mở cửa lúc 5:30 Thứ 4"


def test_google_maps_details_normalize_hours_menu_price_and_images() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo Gió - Google Maps</title>Open now "
                    "!3d13.8861097!4d109.2925926",
                    "title": "Eo Gió - Google Maps",
                    "final_url": "https://www.google.com/maps/place/eo-gio",
                    "structured_data": {
                        "name": "Eo Gió",
                        "category": "Cafe",
                        "address": "Address: Quy Nhơn, Vietnam",
                        "phone": "0905000000",
                        "website_url": "https://example.com",
                        "menu_url": "https://example.com/menu.pdf",
                        "price_text": "Price: ₫₫",
                        "aria_labels": [
                            "Monday, Closed, Copy open hours",
                            "Tuesday, 5:30 AM to 6:30 PM, Copy open hours",
                            "Wednesday, Open 24 hours, Copy open hours",
                        ],
                        "image_urls": [
                            "https://lh3.googleusercontent.com/cover",
                            "https://lh3.googleusercontent.com/gallery",
                        ],
                    },
                }
            }
        }
    )
    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.address == "Quy Nhơn, Vietnam"
    assert observation.price_level == 2
    assert observation.weekly_opening is not None
    assert len(observation.weekly_opening.days) == 3
    assert observation.opening.status is DailyOpeningStatus.OPEN_TODAY
    assert observation.opening.opening_intervals[0].opens_at.hour == 5
    assert observation.menu_source is not None
    assert str(observation.menu_source.menu_url) == "https://example.com/menu.pdf"
    assert observation.media is not None
    assert len(observation.media.assets) == 1


def test_menu_image_fallback_rejects_icon_and_does_not_fake_menu_url() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo Gió - Google Maps</title>Open now "
                    "!3d13.8861097!4d109.2925926",
                    "title": "Eo Gió - Google Maps",
                    "final_url": "https://www.google.com/maps/place/eo-gio",
                    "structured_data": {
                        "name": "Eo Gió",
                        "menu_url": None,
                        "menu_image_urls": [
                            "https://lh3.googleusercontent.com/icon=w32-h32-p-k-no",
                            "https://lh3.googleusercontent.com/menu=w203-h586-k-no",
                        ],
                    },
                }
            }
        }
    )
    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.menu_source is not None
    assert observation.menu_source.menu_url is None
    assert [str(item) for item in observation.menu_source.menu_image_urls] == [
        "https://lh3.googleusercontent.com/menu=w203-h586-k-no"
    ]


def test_verified_mapping_menu_image_is_used_when_headless_dom_omits_menu() -> None:
    mapping = _mapping(MappingStatus.CONFIRMED)
    mapping = mapping.model_copy(
        update={
            "attributes": {
                **mapping.attributes,
                "verified_menu_image_url": (
                    "https://lh3.googleusercontent.com/menu=w203-h586-k-no"
                ),
            }
        }
    )
    observation = GoogleMapsPlaceNormalizer().normalize(_record(), mapping)

    assert observation.menu_source is not None
    assert observation.menu_source.menu_url is None
    assert len(observation.menu_source.menu_image_urls) == 1
