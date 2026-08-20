from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from nextrip_pipeline.crawl import (
    MenuImageCapture,
    RawJsonWriter,
    google_image_high_resolution_url,
)
from nextrip_pipeline.decision_gate import MenuDecisionStatus, MenuDecisionWriter
from nextrip_pipeline.jobs import MenuRefreshPipeline
from nextrip_pipeline.preprocessing import (
    MenuOcrCache,
    MenuOcrNormalizer,
    NormalizedMenuWriter,
)
from nextrip_pipeline.publishing import CurrentMenuWriter
from nextrip_pipeline.review import MenuReviewQueue, MenuReviewStatus
from nextrip_pipeline.schemas import NormalizedMenu
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    MenuOcrLine,
)
from nextrip_pipeline.validators import MenuValidationWriter


class FakeDownloader:
    def __init__(self) -> None:
        content = b"fake-menu-image"
        self.capture = MenuImageCapture(
            requested_url="https://lh3.googleusercontent.com/menu=s2000",
            final_url="https://lh3.googleusercontent.com/menu=s2000",
            content=content,
            content_type="image/jpeg",
            content_hash=hashlib.sha256(content).hexdigest(),
            width=1000,
            height=2000,
            crawled_at=datetime(2026, 8, 18, 8, tzinfo=timezone.utc),
        )

    def fetch(self, url: str) -> MenuImageCapture:
        return self.capture


class FakeOcrEngine:
    name = "fake-ocr"
    version = "1.0"

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, image_path: Path) -> list[MenuOcrLine]:
        self.calls += 1
        return _ocr_lines()


def _ocr_lines() -> list[MenuOcrLine]:
    return [
        MenuOcrLine(
            text="Cafe",
            confidence=0.98,
            bounding_box=[[10, 0], [80, 0], [80, 8], [10, 8]],
        ),
        MenuOcrLine(
            text="Cà phê sữa SG 22k",
            confidence=0.94,
            bounding_box=[[10, 10], [180, 10], [180, 30], [10, 30]],
        ),
        MenuOcrLine(
            text="Cà phê đen",
            confidence=0.90,
            bounding_box=[[10, 50], [120, 50], [120, 70], [10, 70]],
        ),
        MenuOcrLine(
            text="16k",
            confidence=0.88,
            bounding_box=[[180, 50], [220, 50], [220, 70], [180, 70]],
        ),
        MenuOcrLine(
            text="Wifi: 8888",
            confidence=0.99,
            bounding_box=[[10, 90], [120, 90], [120, 110], [10, 110]],
        ),
    ]


def _mapping() -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="google-maps-cafe-dn-062",
        entity_id="cafe_dn_062",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Xóm Mèo Coffee",
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.8,
        matched_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        attributes={
            "verified_menu_image_url": "https://lh3.googleusercontent.com/menu=w203-h586-k-no"
        },
    )


def test_google_menu_image_url_requests_high_resolution() -> None:
    assert (
        google_image_high_resolution_url(
            "https://lh3.googleusercontent.com/menu=w203-h586-k-no"
        )
        == "https://lh3.googleusercontent.com/menu=s2000"
    )


def test_menu_ocr_normalizer_extracts_inline_and_spatial_prices(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    result = pipeline.run(_mapping(), run_id="menu-test-normalize")
    observation = __import__("json").loads(
        result.normalized_path.read_text(encoding="utf-8")
    )

    assert set(observation) == {"place_id", "items"}
    assert observation["place_id"] == "cafe_dn_062"
    assert observation["items"] == [
        {
            "name": "Cà phê sữa SG",
            "section": "Cafe",
            "currency": "VND",
            "amount": 22000,
        },
        {
            "name": "Cà phê đen",
            "section": "Cafe",
            "currency": "VND",
            "amount": 16000,
        },
    ]
    assert result.decision.status is MenuDecisionStatus.REVIEW
    assert "MENU_OCR_REQUIRES_REVIEW" in result.decision.reason_codes
    assert result.raw_image_path.read_bytes() == b"fake-menu-image"
    assert result.review_created is True
    assert result.review_task_path is not None
    task = json.loads(result.review_task_path.read_text(encoding="utf-8"))
    assert task["status"] == "pending"
    assert task["candidate"] == observation


def test_menu_ocr_cache_skips_repeated_ocr_for_identical_image(tmp_path: Path) -> None:
    pipeline, engine = _pipeline(tmp_path)

    first = pipeline.run(_mapping(), run_id="menu-test-first")
    second = pipeline.run(_mapping(), run_id="menu-test-second")

    assert first.ocr_cache_hit is False
    assert second.ocr_cache_hit is True
    assert engine.calls == 1
    assert first.review_created is True
    assert second.review_created is False
    assert first.review_task_path == second.review_task_path


def test_human_approval_publishes_exact_current_menu(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    result = pipeline.run(_mapping(), run_id="menu-review-approve")
    queue = MenuReviewQueue(tmp_path / "review")
    task = queue.list_pending()[0]
    export_path = queue.export_candidate(task.review_id, tmp_path / "edited-menu.json")
    reviewed = NormalizedMenu.model_validate_json(
        export_path.read_text(encoding="utf-8")
    )
    reviewed.items[0].name = "Cafe sữa Sài Gòn"

    resolution, resolution_path = queue.approve(
        task.review_id,
        reviewed,
        reviewer="oanh",
    )
    current_path = CurrentMenuWriter(tmp_path / "current-menu").publish(resolution)

    assert result.review_task_path is not None
    assert result.review_task_path.exists()
    assert resolution_path.exists()
    assert resolution.status is MenuReviewStatus.APPROVED
    assert queue.list_pending() == []
    assert json.loads(current_path.read_text(encoding="utf-8")) == reviewed.model_dump(
        mode="json"
    )


def test_human_rejection_is_audited_without_current_menu(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    pipeline.run(_mapping(), run_id="menu-review-reject")
    queue = MenuReviewQueue(tmp_path / "review")
    task = queue.list_pending()[0]

    resolution, resolution_path = queue.reject(
        task.review_id,
        reviewer="oanh",
        reason="Ảnh menu quá mờ",
    )

    assert resolution.status is MenuReviewStatus.REJECTED
    assert resolution_path.exists()
    assert queue.list_pending() == []
    assert not (tmp_path / "current-menu").exists()


def _pipeline(tmp_path: Path) -> tuple[MenuRefreshPipeline, FakeOcrEngine]:
    engine = FakeOcrEngine()
    return (
        MenuRefreshPipeline(
            FakeDownloader(),
            RawJsonWriter(tmp_path / "raw"),
            NormalizedMenuWriter(tmp_path / "normalized"),
            MenuValidationWriter(tmp_path / "validation"),
            MenuDecisionWriter(tmp_path / "decisions"),
            engine,
            MenuOcrCache(tmp_path / "cache"),
            normalizer=MenuOcrNormalizer(),
            review_queue=MenuReviewQueue(tmp_path / "review"),
        ),
        engine,
    )
