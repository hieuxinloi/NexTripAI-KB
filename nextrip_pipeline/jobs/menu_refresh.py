from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nextrip_pipeline.crawl import (
    MenuImageDownloader,
    RawJsonWriter,
    RawMenuImageWriter,
    build_menu_source_record,
)
from nextrip_pipeline.decision_gate import (
    MenuDecision,
    MenuDecisionGate,
    MenuDecisionStatus,
    MenuDecisionWriter,
)
from nextrip_pipeline.preprocessing import (
    MenuOcrCache,
    MenuOcrEngine,
    MenuOcrNormalizer,
    NormalizedMenuWriter,
)
from nextrip_pipeline.review import MenuReviewQueue
from nextrip_pipeline.schemas import ExternalEntityMapping
from nextrip_pipeline.validators import MenuValidationWriter, MenuValidatorOrchestrator


@dataclass(slots=True)
class MenuRefreshResult:
    run_id: str
    raw_record_path: Path
    raw_image_path: Path
    normalized_path: Path
    validation_paths: list[Path] = field(default_factory=list)
    decision_path: Path | None = None
    decision: MenuDecision | None = None
    ocr_cache_hit: bool = False
    review_task_path: Path | None = None
    review_created: bool = False


class MenuRefreshPipeline:
    """Runs menu image download -> local OCR -> normalize -> validate -> decide."""

    def __init__(
        self,
        downloader: MenuImageDownloader,
        raw_writer: RawJsonWriter,
        normalized_writer: NormalizedMenuWriter,
        validation_writer: MenuValidationWriter,
        decision_writer: MenuDecisionWriter,
        ocr_engine: MenuOcrEngine,
        ocr_cache: MenuOcrCache,
        *,
        normalizer: MenuOcrNormalizer | None = None,
        validator: MenuValidatorOrchestrator | None = None,
        decision_gate: MenuDecisionGate | None = None,
        review_queue: MenuReviewQueue | None = None,
    ) -> None:
        self.downloader = downloader
        self.raw_writer = raw_writer
        self.raw_image_writer = RawMenuImageWriter(raw_writer)
        self.normalized_writer = normalized_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.ocr_engine = ocr_engine
        self.ocr_cache = ocr_cache
        self.normalizer = normalizer or MenuOcrNormalizer()
        self.validator = validator or MenuValidatorOrchestrator()
        self.decision_gate = decision_gate or MenuDecisionGate()
        self.review_queue = review_queue

    def run(self, mapping: ExternalEntityMapping, *, run_id: str) -> MenuRefreshResult:
        menu_url = mapping.attributes.get(
            "verified_menu_image_url"
        ) or mapping.attributes.get("discovered_menu_image_url")
        if not isinstance(menu_url, str) or not menu_url.startswith("https://"):
            raise ValueError(
                "a verified or discovered menu image URL is required for menu OCR"
            )
        capture = self.downloader.fetch(menu_url)
        record = build_menu_source_record(capture, mapping, run_id=run_id)
        raw_image_path = self.raw_image_writer.write(record, capture)
        raw_record_path = self.raw_writer.write(record)

        lines = self.ocr_cache.load(capture.content_hash, self.ocr_engine)
        cache_hit = lines is not None
        if lines is None:
            lines = self.ocr_engine.extract(raw_image_path)
            self.ocr_cache.store(capture.content_hash, self.ocr_engine, lines)

        observation = self.normalizer.normalize(
            record,
            mapping,
            lines,
            engine_name=self.ocr_engine.name,
            engine_version=self.ocr_engine.version,
        )
        normalized_path = self.normalized_writer.write(observation)
        validations = self.validator.validate(observation, mapping)
        validation_paths = [self.validation_writer.write(item) for item in validations]
        decision = self.decision_gate.decide(observation, validations)
        decision_path = self.decision_writer.write(decision)
        review_task_path = None
        review_created = False
        if (
            self.review_queue is not None
            and decision.status is MenuDecisionStatus.REVIEW
        ):
            _, review_task_path, review_created = self.review_queue.enqueue(
                observation,
                decision,
                self.normalized_writer.to_normalized_menu(observation),
                raw_image_path=raw_image_path,
                normalized_path=normalized_path,
            )
        return MenuRefreshResult(
            run_id=run_id,
            raw_record_path=raw_record_path,
            raw_image_path=raw_image_path,
            normalized_path=normalized_path,
            validation_paths=validation_paths,
            decision_path=decision_path,
            decision=decision,
            ocr_cache_hit=cache_hit,
            review_task_path=review_task_path,
            review_created=review_created,
        )
