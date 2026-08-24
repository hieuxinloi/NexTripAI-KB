from __future__ import annotations

import pytest

from nextrip_pipeline.google_maps_price import (
    google_maps_price_evidence_text,
    google_maps_price_level,
)


@pytest.mark.parametrize(
    ("raw_text", "normalized", "level"),
    [
        ("1.000.000\N{NO-BREAK SPACE}₫ trở lên", "1.000.000 ₫ trở lên", None),
        ("800.000-900.000 ₫", "800.000-900.000 ₫", None),
        (
            " Khoảng giá, Trên 1.000.000 ₫/người, 21 người đã báo cáo ",
            "Khoảng giá, Trên 1.000.000 ₫/người, 21 người đã báo cáo",
            None,
        ),
        ("Price: $$$", "Price: $$$", 3),
        ("Mức giá: ₫₫", "Mức giá: ₫₫", 2),
        ("Very expensive", "Very expensive", 4),
        ("Rất đắt", "Rất đắt", 4),
    ],
)
def test_google_maps_price_evidence_accepts_complete_provider_labels(
    raw_text: str,
    normalized: str,
    level: int | None,
) -> None:
    assert google_maps_price_evidence_text(raw_text) == normalized
    assert google_maps_price_level(raw_text) == level


@pytest.mark.parametrize(
    "raw_text",
    [
        "Giá phòng cho Luxtery Hotel",
        "Giáo - Hội Tăng - Già Khất - Sĩ Việt",
        "Giá tour Ngũ Hành Sơn 466.726 ₫",
        "Tour Ngũ Hành Sơn 466.726 ₫",
        "Cami Riverside Resort 604.288 ₫ 4,4 sao",
        "437 bài đánh giá",
    ],
)
def test_google_maps_price_evidence_rejects_unscoped_contamination(
    raw_text: str,
) -> None:
    assert google_maps_price_evidence_text(raw_text) is None
    assert google_maps_price_level(raw_text) is None
