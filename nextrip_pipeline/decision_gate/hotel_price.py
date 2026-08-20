from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from pydantic import AwareDatetime, Field

from nextrip_pipeline.schemas import (
    HotelPriceObservation,
    NexTripModel,
    ValidationResult,
    ValidationStatus,
)


class HotelPriceDecisionStatus(StrEnum):
    PASS = "pass"
    QUARANTINE = "quarantine"


class HotelPriceDecision(NexTripModel):
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    hotel_id: str = Field(min_length=1)
    status: HotelPriceDecisionStatus
    validation_ids: list[str] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    decided_at: AwareDatetime


class HotelPriceDecisionGate:
    """Price changes pass immediately; only invalid evidence is quarantined."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def decide(
        self,
        observation: HotelPriceObservation,
        validation_results: Sequence[ValidationResult],
    ) -> HotelPriceDecision:
        if not validation_results:
            raise ValueError("hotel price decision requires validation results")
        if any(
            item.record_id != observation.observation_id for item in validation_results
        ):
            raise ValueError("validation result belongs to another observation")

        failed = [
            item
            for item in validation_results
            if item.status in {ValidationStatus.FAIL, ValidationStatus.ERROR}
        ]
        status = (
            HotelPriceDecisionStatus.QUARANTINE
            if failed
            else HotelPriceDecisionStatus.PASS
        )
        reason_codes = sorted(
            {code for result in failed for code in result.reason_codes}
        )
        return HotelPriceDecision(
            decision_id=f"{observation.observation_id}:decision",
            run_id=observation.run_id,
            observation_id=observation.observation_id,
            hotel_id=observation.hotel_id,
            status=status,
            validation_ids=[item.validation_id for item in validation_results],
            reason_codes=reason_codes,
            decided_at=self.clock(),
        )


class HotelPriceDecisionWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, decision: HotelPriceDecision) -> Path:
        destination = (
            self.root_directory
            / "entity=hotel_price"
            / f"run={quote(decision.run_id, safe='-_.')}"
            / f"decision={quote(decision.decision_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise FileExistsError(f"Decision already exists: {destination}") from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(decision.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
