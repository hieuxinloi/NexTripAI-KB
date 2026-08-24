from __future__ import annotations

from nextrip_pipeline.crawl.browser import (
    _first_official_google_maps_share_url,
    _google_maps_price_text_from_scoped_labels,
    _google_maps_weekday_aria_label_count,
)
from nextrip_pipeline.google_maps_identity import (
    google_maps_official_share_url,
    google_maps_stable_place_url,
)
from nextrip_pipeline.google_maps_plus_code import (
    google_maps_plus_code_center,
    google_maps_plus_code_from_scoped_labels,
    google_maps_plus_code_from_text,
)


STABLE_PLACE_URL = (
    "https://www.google.com/maps/place/Eo+Gio/"
    "data=!4m7!3m6!1s0x314219c8f123:0xabc456!8m2!3d13.88!4d109.29"
    "?entry=ttu"
)


def test_weekday_aria_count_uses_vi_labels_and_distinct_days() -> None:
    labels = [
        "Hiện giờ mở cửa trong tuần",
        "Thứ Hai, 08:00 đến 22:00",
        "Thứ Hai, 08:00 đến 22:00",
        "Thứ Ba, 08:00 đến 22:00",
        "Chủ Nhật, Đóng cửa",
        None,
    ]

    assert _google_maps_weekday_aria_label_count(labels) == 3
    assert _google_maps_weekday_aria_label_count(["Thứ Hai, 08:00 đến 22:00"]) == 1


def test_share_candidate_must_be_an_official_google_url() -> None:
    short_url = "https://maps.app.goo.gl/AbCdEf123?g_st=ic"

    assert (
        _first_official_google_maps_share_url(
            ["https://example.com/not-google", short_url]
        )
        == short_url
    )
    assert google_maps_official_share_url("javascript:alert(1)") is None
    assert google_maps_official_share_url("https://goo.gl/not-maps") is None


def test_stable_place_url_requires_place_path_and_provider_token() -> None:
    assert google_maps_stable_place_url(STABLE_PLACE_URL) == STABLE_PLACE_URL
    assert (
        google_maps_stable_place_url(
            "https://www.google.com/maps/search/Eo+Gio?query_place_id="
            "0x314219c8f123:0xabc456"
        )
        is None
    )
    assert (
        google_maps_stable_place_url(
            "https://www.google.com/maps/place/Eo+Gio?entry=ttu"
        )
        is None
    )
    assert google_maps_stable_place_url("https://maps.app.goo.gl/AbCdEf123") is None


def test_scoped_price_prefers_explicit_header_evidence() -> None:
    assert (
        _google_maps_price_text_from_scoped_labels(
            [
                "4,9 sao ",
                "437 bài đánh giá",
                "1.000.000\N{NO-BREAK SPACE}₫ trở lên",
                " Khoảng giá, Trên 1.000.000 ₫/người",
            ]
        )
        == "1.000.000 ₫ trở lên"
    )


def test_scoped_price_rejects_labels_without_price_evidence() -> None:
    assert (
        _google_maps_price_text_from_scoped_labels(
            [
                "Giá phòng cho Luxtery Hotel",
                "Giáo - Hội Tăng - Già Khất - Sĩ Việt",
                "437 bài đánh giá",
            ]
        )
        is None
    )


def test_scoped_price_accepts_provider_price_level() -> None:
    assert _google_maps_price_text_from_scoped_labels(["Price: $$$"]) == "Price: $$$"


def test_plus_code_helper_requires_valid_open_location_code_syntax() -> None:
    assert google_maps_plus_code_from_text("366F+85 Háº£i ChÃ¢u") == "366F+85"
    assert google_maps_plus_code_from_text("2WXGR+RM") is None
    assert google_maps_plus_code_from_text("366F+8") is None


def test_historical_aria_fallback_requires_explicit_provider_label() -> None:
    assert (
        google_maps_plus_code_from_scoped_labels(
            ["Plus code: 366F+85 Háº£i ChÃ¢u, ÄÃ  Náºµng, Viá»‡t Nam"]
        )
        == "366F+85"
    )
    assert (
        google_maps_plus_code_from_scoped_labels(
            ["A review happens to mention 366F+85"]
        )
        is None
    )


def test_short_plus_code_uses_fixed_service_area_not_place_coordinates() -> None:
    center = google_maps_plus_code_center("366F+85", city_id="city_da_nang")

    assert center is not None
    assert center[0] == 16.0608125
    assert center[1] == 108.2229375
    assert google_maps_plus_code_center("366F+85", city_id="unknown") is None


def test_full_plus_code_matches_open_location_code_specification_vector() -> None:
    assert google_maps_plus_code_center("8FVC9G8F+6W") == (
        47.3655625,
        8.5248125,
    )
