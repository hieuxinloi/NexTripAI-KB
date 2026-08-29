from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.decision_gate import MenuDecision, MenuDecisionStatus
from nextrip_pipeline.schemas import MenuObservation, NexTripModel, NormalizedMenu


class MenuReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MenuReviewTask(NexTripModel):
    review_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_image_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    raw_image_path: str = Field(min_length=1)
    normalized_path: str = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    candidate: NormalizedMenu
    status: MenuReviewStatus = MenuReviewStatus.PENDING
    created_at: AwareDatetime


class MenuReviewResolution(NexTripModel):
    review_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    source_image_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    status: MenuReviewStatus
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    approved_menu: NormalizedMenu | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> MenuReviewResolution:
        if self.status is MenuReviewStatus.APPROVED and self.approved_menu is None:
            raise ValueError("approved review requires approved_menu")
        if self.status is MenuReviewStatus.REJECTED and not self.reason:
            raise ValueError("rejected review requires a reason")
        if self.status is MenuReviewStatus.PENDING:
            raise ValueError("a resolution cannot have pending status")
        return self


class HumanMenuValidator:
    def __init__(self, *, minimum_price: int = 5000, maximum_price: int = 5_000_000):
        self.minimum_price = minimum_price
        self.maximum_price = maximum_price

    def validate(self, task: MenuReviewTask, menu: NormalizedMenu) -> None:
        errors = []
        if menu.place_id != task.place_id:
            errors.append("place_id does not match the review task")
        if not menu.items:
            errors.append("approved menu must contain at least one item")
        seen = set()
        for index, item in enumerate(menu.items):
            if item.currency != "VND":
                errors.append(f"items[{index}].currency must be VND")
            if not self.minimum_price <= item.amount <= self.maximum_price:
                errors.append(f"items[{index}].amount is outside the allowed range")
            key = (
                self._key(item.name),
                self._key(item.section or ""),
                item.currency,
                item.amount,
            )
            if key in seen:
                errors.append(f"items[{index}] duplicates an earlier item")
            seen.add(key)
        if errors:
            raise ValueError("; ".join(errors))

    @staticmethod
    def _key(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class MenuReviewQueue:
    """Immutable pending tasks and immutable approval/rejection resolutions."""

    def __init__(
        self,
        root_directory: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        validator: HumanMenuValidator | None = None,
    ) -> None:
        self.root_directory = Path(root_directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.validator = validator or HumanMenuValidator()

    def enqueue(
        self,
        observation: MenuObservation,
        decision: MenuDecision,
        candidate: NormalizedMenu,
        *,
        raw_image_path: Path,
        normalized_path: Path,
    ) -> tuple[MenuReviewTask, Path, bool]:
        if decision.status is not MenuDecisionStatus.REVIEW:
            raise ValueError("only REVIEW menu decisions can enter the review queue")
        if decision.observation_id != observation.observation_id:
            raise ValueError("decision and menu observation do not match")
        existing = self._find_by_image(
            observation.place_id, observation.image_content_hash
        )
        if existing is not None:
            return existing, self.task_path(existing.review_id), False
        review_id = f"menu-{observation.place_id}-{observation.image_content_hash[:16]}"
        task = MenuReviewTask(
            review_id=review_id,
            run_id=observation.run_id,
            place_id=observation.place_id,
            observation_id=observation.observation_id,
            decision_id=decision.decision_id,
            source_record_id=observation.source_record_id,
            source_image_hash=observation.image_content_hash,
            raw_image_path=str(raw_image_path),
            normalized_path=str(normalized_path),
            reason_codes=decision.reason_codes,
            candidate=candidate,
            created_at=self.clock(),
        )
        destination = self.task_path(review_id)
        self._write_once(destination, task.model_dump_json(indent=2) + "\n")
        return task, destination, True

    def get(self, review_id: str) -> MenuReviewTask:
        path = self.task_path(review_id)
        if not path.exists():
            raise FileNotFoundError(f"Menu review task not found: {review_id}")
        return MenuReviewTask.model_validate_json(path.read_text(encoding="utf-8"))

    def list_pending(self) -> list[MenuReviewTask]:
        tasks = [
            MenuReviewTask.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.pending_directory.glob("review=*.json"))
        ]
        return [task for task in tasks if not self.is_resolved(task.review_id)]

    def export_candidate(self, review_id: str, output_path: Path) -> Path:
        task = self.get(review_id)
        self._write_once(output_path, task.candidate.model_dump_json(indent=2) + "\n")
        return output_path

    def approve(
        self,
        review_id: str,
        menu: NormalizedMenu,
        *,
        reviewer: str,
    ) -> tuple[MenuReviewResolution, Path]:
        task = self.get(review_id)
        self.validator.validate(task, menu)
        destination = self.resolution_path(review_id, MenuReviewStatus.APPROVED)
        if destination.exists():
            existing = MenuReviewResolution.model_validate_json(
                destination.read_text(encoding="utf-8")
            )
            if existing.approved_menu != menu:
                raise ValueError("review was already approved with different data")
            return existing, destination
        if self.resolution_path(review_id, MenuReviewStatus.REJECTED).exists():
            raise ValueError(f"Menu review task is already rejected: {review_id}")
        resolution = MenuReviewResolution(
            review_id=review_id,
            place_id=task.place_id,
            source_image_hash=task.source_image_hash,
            status=MenuReviewStatus.APPROVED,
            reviewer=reviewer,
            reviewed_at=self.clock(),
            approved_menu=menu,
        )
        self._write_once(destination, resolution.model_dump_json(indent=2) + "\n")
        return resolution, destination

    def reject(
        self,
        review_id: str,
        *,
        reviewer: str,
        reason: str,
    ) -> tuple[MenuReviewResolution, Path]:
        task = self.get(review_id)
        self._require_unresolved(review_id)
        resolution = MenuReviewResolution(
            review_id=review_id,
            place_id=task.place_id,
            source_image_hash=task.source_image_hash,
            status=MenuReviewStatus.REJECTED,
            reviewer=reviewer,
            reviewed_at=self.clock(),
            reason=reason,
        )
        destination = self.resolution_path(review_id, MenuReviewStatus.REJECTED)
        self._write_once(destination, resolution.model_dump_json(indent=2) + "\n")
        return resolution, destination

    @property
    def pending_directory(self) -> Path:
        return self.root_directory / "pending"

    def task_path(self, review_id: str) -> Path:
        return self.pending_directory / f"review={quote(review_id, safe='-_.')}.json"

    def resolution_path(self, review_id: str, status: MenuReviewStatus) -> Path:
        return (
            self.root_directory
            / status.value
            / f"review={quote(review_id, safe='-_.')}.json"
        )

    def is_resolved(self, review_id: str) -> bool:
        return any(
            self.resolution_path(review_id, status).exists()
            for status in (MenuReviewStatus.APPROVED, MenuReviewStatus.REJECTED)
        )

    def _require_unresolved(self, review_id: str) -> None:
        if self.is_resolved(review_id):
            raise ValueError(f"Menu review task is already resolved: {review_id}")

    def _find_by_image(self, place_id: str, image_hash: str) -> MenuReviewTask | None:
        for path in self.pending_directory.glob("review=*.json"):
            task = MenuReviewTask.model_validate_json(path.read_text(encoding="utf-8"))
            if task.place_id == place_id and task.source_image_hash == image_hash:
                return task
        return None

    @staticmethod
    def _write_once(destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Immutable review artifact exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
