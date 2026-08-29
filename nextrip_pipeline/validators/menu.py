from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    MappingStatus,
    MenuObservation,
    SuggestedAction,
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)


class MenuValidatorOrchestrator:
    validator_version = "1.0.0"

    def __init__(
        self,
        *,
        minimum_price: Decimal = Decimal("5000"),
        maximum_price: Decimal = Decimal("5000000"),
        minimum_ocr_confidence: float = 0.55,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.minimum_price = minimum_price
        self.maximum_price = maximum_price
        self.minimum_ocr_confidence = minimum_ocr_confidence
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate(
        self, observation: MenuObservation, mapping: ExternalEntityMapping
    ) -> list[ValidationResult]:
        now = self.clock()
        return [
            self._mapping(observation, mapping, now),
            self._items_present(observation, now),
            self._prices(observation, now),
            self._ocr_confidence(observation, now),
            self._semantic_review(observation, now),
        ]

    def _result(
        self,
        observation: MenuObservation,
        validator: str,
        status: ValidationStatus,
        now: datetime,
        *,
        score: float,
        reason: str | None = None,
        evidence: list[ValidationEvidence] | None = None,
        semantic_review: bool = False,
    ) -> ValidationResult:
        return ValidationResult(
            validation_id=f"{observation.observation_id}:{validator}",
            run_id=observation.run_id,
            record_id=observation.observation_id,
            validator=validator,
            validator_version=self.validator_version,
            status=status,
            score=score,
            reason_codes=[reason] if reason else [],
            evidence=evidence or [],
            suggested_action=(
                SuggestedAction.AUTO_ACCEPT
                if status is ValidationStatus.PASS
                else SuggestedAction.HUMAN_REVIEW
                if status is ValidationStatus.WARN
                else SuggestedAction.QUARANTINE
            ),
            requires_human_review=status is ValidationStatus.WARN,
            requires_semantic_review=semantic_review,
            validated_at=now,
        )

    def _mapping(self, observation, mapping, now):
        correct = observation.place_id == mapping.entity_id
        if not correct or mapping.status in {
            MappingStatus.PENDING_REVIEW,
            MappingStatus.REJECTED,
        }:
            status, reason, score = ValidationStatus.FAIL, "INVALID_MAPPING", 0.0
        elif mapping.status is MappingStatus.AUTO_MATCHED:
            status, reason, score = (
                ValidationStatus.WARN,
                "MAPPING_NEEDS_CONFIRMATION",
                0.8,
            )
        else:
            status, reason, score = ValidationStatus.PASS, None, 1.0
        return self._result(
            observation, "MenuMappingValidator", status, now, score=score, reason=reason
        )

    def _items_present(self, observation, now):
        passed = bool(observation.items)
        return self._result(
            observation,
            "MenuItemsValidator",
            ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            now,
            score=1.0 if passed else 0.0,
            reason=None if passed else "NO_MENU_ITEMS_EXTRACTED",
        )

    def _prices(self, observation, now):
        invalid = [
            item.name
            for item in observation.items
            if item.amount is None
            or not self.minimum_price <= item.amount <= self.maximum_price
        ]
        passed = not invalid
        return self._result(
            observation,
            "MenuPriceRangeValidator",
            ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            now,
            score=(len(observation.items) - len(invalid)) / len(observation.items)
            if observation.items
            else 0.0,
            reason=None if passed else "MENU_PRICE_OUT_OF_RANGE_OR_MISSING",
            evidence=[
                ValidationEvidence(
                    code="INVALID_MENU_ITEMS",
                    source_record_id=observation.source_record_id,
                    observed_value=invalid,
                    expected_value={
                        "minimum": str(self.minimum_price),
                        "maximum": str(self.maximum_price),
                    },
                )
            ],
        )

    def _ocr_confidence(self, observation, now):
        scores = [
            item.ocr_confidence
            for item in observation.items
            if item.ocr_confidence is not None
        ]
        average = sum(scores) / len(scores) if scores else 0.0
        if average >= self.minimum_ocr_confidence:
            status, reason = ValidationStatus.PASS, None
        else:
            status, reason = ValidationStatus.WARN, "LOW_MENU_OCR_CONFIDENCE"
        return self._result(
            observation,
            "MenuOcrConfidenceValidator",
            status,
            now,
            score=average,
            reason=reason,
        )

    def _semantic_review(self, observation, now):
        return self._result(
            observation,
            "MenuSemanticReviewValidator",
            ValidationStatus.WARN,
            now,
            score=0.7,
            reason="MENU_OCR_REQUIRES_REVIEW",
            evidence=[
                ValidationEvidence(
                    code="OCR_EXTRACTED_ITEM_COUNT",
                    source_record_id=observation.source_record_id,
                    observed_value=len(observation.items),
                )
            ],
            semantic_review=True,
        )


class MenuValidationWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, result: ValidationResult) -> Path:
        destination = (
            self.root_directory
            / "entity=menu"
            / f"run={quote(result.run_id, safe='-_.')}"
            / f"validation={quote(result.validation_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Menu validation already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(result.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
