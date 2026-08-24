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
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    RecordSubjectType,
    SourceRecord,
    ValidationStatus,
    Weekday,
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
        GoogleMapsPlaceNormalizer().normalize(record, _mapping(MappingStatus.CONFIRMED))


def test_master_coordinates_are_retained_when_maps_spa_omits_place_coordinates() -> (
    None
):
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo GiÃ³ - Google Maps</title>Open now",
                    "title": "Eo GiÃ³ - Google Maps",
                    "final_url": (
                        "https://www.google.com/maps/search/Eo-Gio/@12.345,106.789,17z"
                    ),
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


def test_selected_place_plus_code_overrides_master_search_viewport() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Gong Cha - Google Maps</title>",
                    "title": "Gong Cha - Google Maps",
                    "final_url": (
                        "https://www.google.com/maps/search/Gong-Cha/"
                        "@12.345,106.789,17z"
                    ),
                    "used_master_coordinates_for_viewport": True,
                    "structured_data": {
                        "name": "Gong Cha",
                        "plus_code": "366F+85",
                    },
                }
            }
        }
    )
    mapping = _mapping(MappingStatus.CONFIRMED).model_copy(
        update={
            "attributes": {
                **_mapping(MappingStatus.CONFIRMED).attributes,
                "city_id": "city_da_nang",
                "master_city": "ÄÃ  Náºµng",
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(record, mapping)

    assert observation.location is not None
    assert observation.location.latitude == 16.0608125
    assert observation.location.longitude == 108.2229375
    assert observation.location.accuracy == "google_maps_plus_code_area_center"
    assert observation.location.source == "google-maps-web"


def test_explicit_historical_plus_code_label_is_used_conservatively() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Gong Cha - Google Maps</title>",
                    "title": "Gong Cha - Google Maps",
                    "final_url": "https://www.google.com/maps/search/Gong-Cha",
                    "used_master_coordinates_for_viewport": True,
                    "structured_data": {
                        "name": "Gong Cha",
                        "aria_labels": [
                            "Plus code: 366F+85 Háº£i ChÃ¢u, ÄÃ  Náºµng"
                        ],
                    },
                }
            }
        }
    )
    mapping = _mapping(MappingStatus.CONFIRMED).model_copy(
        update={
            "attributes": {
                **_mapping(MappingStatus.CONFIRMED).attributes,
                "city_id": "city_da_nang",
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(record, mapping)

    assert observation.location is not None
    assert observation.location.accuracy == "google_maps_plus_code_area_center"


def test_resolved_place_url_coordinates_override_master_search_viewport() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo Gio - Google Maps</title>Open now",
                    "title": "Eo Gio - Google Maps",
                    "final_url": (
                        "https://www.google.com/maps/place/Eo-Gio/"
                        "@13.887777,109.299999,17z"
                    ),
                    "used_master_coordinates_for_viewport": True,
                    "structured_data": {"name": "Eo Gio"},
                }
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.location is not None
    assert observation.location.latitude == 13.887777
    assert observation.location.longitude == 109.299999
    assert observation.location.accuracy == "google_maps_place_page"
    assert observation.location.source == "google-maps-web"


def test_resolved_scoped_listing_without_closure_badge_is_active() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    # Unrelated shell text must not be used as place evidence.
                    "html": "<title>Eo Gio - Google Maps</title>Permanently closed",
                    "title": "Eo Gio - Google Maps",
                    "final_url": "https://www.google.com/maps/place/eo-gio",
                    "structured_data": {
                        "name": "Eo Gio",
                        "status_evidence_scoped": True,
                        "business_status_text": None,
                    },
                }
            }
        }
    )
    mapping = _mapping(MappingStatus.CONFIRMED)
    observation = GoogleMapsPlaceNormalizer().normalize(record, mapping)
    validations = GoogleMapsValidatorOrchestrator(clock=lambda: NOW).validate(
        observation,
        mapping,
    )
    decision = GoogleMapsDecisionGate(clock=lambda: NOW).decide(
        observation,
        validations,
    )

    assert observation.business_status is BusinessStatus.ACTIVE
    assert "BUSINESS_STATUS_UNVERIFIED" not in decision.reason_codes


def test_scoped_permanent_close_is_explicit_evidence() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>Eo Gio - Google Maps</title>",
                    "title": "Eo Gio - Google Maps",
                    "final_url": "https://www.google.com/maps/place/eo-gio",
                    "structured_data": {
                        "name": "Eo Gio",
                        "status_evidence_scoped": True,
                        "business_status_text": "Permanently closed",
                    },
                }
            }
        }
    )
    observation = GoogleMapsPlaceNormalizer().normalize(
        record,
        _mapping(MappingStatus.CONFIRMED),
    )

    assert observation.business_status is BusinessStatus.PERMANENTLY_CLOSED
    assert observation.opening.status is DailyOpeningStatus.PERMANENTLY_CLOSED


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


def test_google_maps_exact_price_is_raw_evidence_not_affordability_level() -> None:
    record = _record().model_copy(
        update={
            "raw_payload": {
                "page": {
                    "html": "<title>YUMI - Google Maps</title>",
                    "title": "YUMI - Google Maps",
                    "final_url": "https://www.google.com/maps/place/yumi",
                    "structured_data": {
                        "name": "YUMI Counter & Lounge",
                        "price_text": "1.000.000 ₫ trở lên",
                    },
                }
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.raw_price_text == "1.000.000 ₫ trở lên"
    assert observation.price_level is None


def test_google_maps_text_price_levels_match_most_specific_label() -> None:
    assert GoogleMapsPlaceNormalizer._price_level("Very expensive") == 4
    assert GoogleMapsPlaceNormalizer._price_level("Rất đắt") == 4


def test_vi_vn_weekly_hours_are_parsed_conservatively() -> None:
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
                        "aria_labels": [
                            (
                                "Thứ Sáu,06:00 đến 10:30,15:00 đến 23:30, "
                                "Sao chép giờ mở cửa"
                            ),
                            "Thứ Bảy,Mở cửa cả ngày, Sao chép giờ mở cửa",
                            "Chủ Nhật,Đóng cửa, Sao chép giờ mở cửa",
                            (
                                "Thứ Năm (Vu-lan),07:00 đến 23:00,"
                                "Giờ làm việc có thể thay đổi, Sao chép giờ mở cửa"
                            ),
                        ],
                    },
                }
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.weekly_opening is not None
    schedules = {item.day: item for item in observation.weekly_opening.days}
    assert schedules[Weekday.FRIDAY].intervals[1].closes_at.hour == 23
    assert schedules[Weekday.FRIDAY].intervals[1].closes_at.minute == 30
    assert schedules[Weekday.SATURDAY].open_24_hours is True
    assert schedules[Weekday.SUNDAY].closed is True
    assert schedules[Weekday.THURSDAY].intervals[0].closes_at.hour == 23


@pytest.mark.parametrize(
    "label",
    [
        "Thứ Sáu,08:00 đến 25:00, Sao chép giờ mở cửa",
        "Thứ Sáu,08:00 đến 22:00 không rõ, Sao chép giờ mở cửa",
        "Thứ Hai, 7 tháng 9 đến Thứ Ba, 8 tháng 9",
    ],
)
def test_malformed_vi_vn_hours_do_not_create_weekly_evidence(label: str) -> None:
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
                        "aria_labels": [label],
                    },
                }
            }
        }
    )

    observation = GoogleMapsPlaceNormalizer().normalize(
        record, _mapping(MappingStatus.CONFIRMED)
    )

    assert observation.weekly_opening is None


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


def test_daily_place_capture_does_not_restore_a_mapping_menu_image() -> None:
    mapping = _mapping(MappingStatus.CONFIRMED).model_copy(
        update={
            "attributes": {
                **_mapping(MappingStatus.CONFIRMED).attributes,
                "verified_menu_image_url": (
                    "https://lh3.googleusercontent.com/menu=w203-h586-k-no"
                ),
            }
        }
    )
    base = _record()
    page = dict(base.raw_payload["page"])
    page["structured_data"] = {
        "name": "Eo Gió",
        "menu_capture_enabled": False,
        "menu_url": None,
        "menu_image_urls": [],
    }
    record = base.model_copy(update={"raw_payload": {"page": page}})

    observation = GoogleMapsPlaceNormalizer().normalize(record, mapping)

    assert observation.menu_source is None
