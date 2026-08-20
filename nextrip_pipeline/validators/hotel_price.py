from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
from urllib.parse import quote

from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    HotelPriceObservation,
    MappingStatus,
    SuggestedAction,
    ValidationEvidence,
    ValidationResult,
    ValidationStatus,
)


class HotelPriceValidatorOrchestrator:
    """Deterministic price checks; price movement is never a review trigger."""

    validator_version = "1.0.0"

    def __init__(
        self,
        *,
        max_age: timedelta = timedelta(hours=5),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_age = max_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate(
        self,
        observation: HotelPriceObservation,
        mapping: ExternalEntityMapping,
    ) -> list[ValidationResult]:
        now = self.clock()
        return [
            self._mapping_result(observation, mapping, now),
            self._price_result(observation, now),
            self._stay_result(observation, now),
            self._freshness_result(observation, now),
        ]

    def _result(
        self,
        observation: HotelPriceObservation,
        validator: str,
        passed: bool,
        now: datetime,
        *,
        reason_code: str,
        evidence: list[ValidationEvidence] | None = None,
    ) -> ValidationResult:
        return ValidationResult(
            validation_id=f"{observation.observation_id}:{validator}",
            run_id=observation.run_id,
            record_id=observation.observation_id,
            validator=validator,
            validator_version=self.validator_version,
            status=ValidationStatus.PASS if passed else ValidationStatus.FAIL,
            score=1 if passed else 0,
            reason_codes=[] if passed else [reason_code],
            evidence=evidence or [],
            suggested_action=(
                SuggestedAction.AUTO_ACCEPT if passed else SuggestedAction.QUARANTINE
            ),
            validated_at=now,
        )

    def _mapping_result(
        self,
        observation: HotelPriceObservation,
        mapping: ExternalEntityMapping,
        now: datetime,
    ) -> ValidationResult:
        passed = (
            mapping.status is MappingStatus.CONFIRMED
            and mapping.entity_id == observation.hotel_id
            and mapping.source_id == observation.source_id
        )
        return self._result(
            observation,
            "HotelPriceMappingValidator",
            passed,
            now,
            reason_code="MAPPING_NOT_CONFIRMED",
            evidence=[
                ValidationEvidence(
                    code="HOTEL_MAPPING",
                    source_record_id=observation.source_record_id,
                    observed_value={
                        "hotel_id": observation.hotel_id,
                        "source_id": observation.source_id,
                        "mapping_status": mapping.status.value,
                    },
                    expected_value={
                        "hotel_id": mapping.entity_id,
                        "source_id": mapping.source_id,
                        "mapping_status": MappingStatus.CONFIRMED.value,
                    },
                )
            ],
        )

    def _price_result(
        self,
        observation: HotelPriceObservation,
        now: datetime,
    ) -> ValidationResult:
        values = [observation.nightly_amount, observation.total_amount]
        passed = observation.currency == "VND" and all(
            value is not None and value > Decimal(0) for value in values
        )
        return self._result(
            observation,
            "HotelPriceValueValidator",
            passed,
            now,
            reason_code="INVALID_PRICE_VALUE",
            evidence=[
                ValidationEvidence(
                    code="PRICE_VALUES",
                    source_record_id=observation.source_record_id,
                    observed_value={
                        "currency": observation.currency,
                        "nightly_amount": observation.nightly_amount,
                        "total_amount": observation.total_amount,
                    },
                    expected_value="positive VND nightly and total amounts",
                )
            ],
        )

    def _stay_result(
        self,
        observation: HotelPriceObservation,
        now: datetime,
    ) -> ValidationResult:
        occupancy = observation.occupancy
        passed = (
            observation.check_out > observation.check_in
            and occupancy.adults >= 1
            and occupancy.rooms >= 1
            and occupancy.children >= 0
        )
        return self._result(
            observation,
            "HotelPriceStayValidator",
            passed,
            now,
            reason_code="INVALID_STAY_OR_OCCUPANCY",
        )

    def _freshness_result(
        self,
        observation: HotelPriceObservation,
        now: datetime,
    ) -> ValidationResult:
        age = now - observation.observed_at
        passed = timedelta(0) <= age <= self.max_age
        return self._result(
            observation,
            "HotelPriceFreshnessValidator",
            passed,
            now,
            reason_code="STALE_OR_FUTURE_PRICE",
            evidence=[
                ValidationEvidence(
                    code="OBSERVATION_AGE_SECONDS",
                    source_record_id=observation.source_record_id,
                    observed_value=age.total_seconds(),
                    expected_value={
                        "minimum": 0,
                        "maximum": self.max_age.total_seconds(),
                    },
                )
            ],
        )


class ValidationResultWriter:
    """Immutable audit writer for validator results."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, result: ValidationResult) -> Path:
        destination = (
            self.root_directory
            / "entity=hotel_price"
            / f"run={quote(result.run_id, safe='-_.')}"
            / f"validation={quote(result.validation_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise FileExistsError(
                f"Validation result already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(result.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
