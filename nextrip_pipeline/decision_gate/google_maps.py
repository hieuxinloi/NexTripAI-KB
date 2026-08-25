from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from pydantic import AwareDatetime, Field

from nextrip_pipeline.schemas import (
    GoogleMapsPlaceObservation,
    NexTripModel,
    ValidationResult,
    ValidationStatus,
)


class GoogleMapsDecisionStatus(StrEnum):
    PASS = "pass"
    REVIEW = "review"
    QUARANTINE = "quarantine"


class GoogleMapsDecisionPolicy(StrEnum):
    """Controls how deterministic warnings are handled by the gate.

    Scheduled crawls must never create an unbounded human-review queue.  They
    may accept a small, explicit set of non-destructive missing-detail warnings;
    identity and location ambiguity still fail closed into quarantine.
    """

    STANDARD = "standard"
    TRUSTED_SCHEDULED = "trusted_scheduled"


class GoogleMapsDecision(NexTripModel):
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    status: GoogleMapsDecisionStatus
    validation_ids: list[str] = Field(min_length=1)
    reason_codes: list[str] = Field(default_factory=list)
    decided_at: AwareDatetime


class GoogleMapsDecisionGate:
    # Unattended execution removes the REVIEW outcome; it does not weaken data
    # quality. Any warning is quarantined and retried while the last accepted
    # value remains active.
    _SCHEDULED_PASS_WARNING_CODES: frozenset[str] = frozenset()

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        *,
        policy: GoogleMapsDecisionPolicy = GoogleMapsDecisionPolicy.STANDARD,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = policy

    def decide(
        self,
        observation: GoogleMapsPlaceObservation,
        validations: Sequence[ValidationResult],
    ) -> GoogleMapsDecision:
        if not validations or any(
            item.record_id != observation.observation_id for item in validations
        ):
            raise ValueError("validations are missing or belong to another observation")
        reasons = sorted(
            {
                code
                for item in validations
                if item.status is not ValidationStatus.PASS
                for code in item.reason_codes
            }
        )
        if any(
            item.status in {ValidationStatus.FAIL, ValidationStatus.ERROR}
            for item in validations
        ):
            status = GoogleMapsDecisionStatus.QUARANTINE
        elif any(item.status is ValidationStatus.WARN for item in validations):
            if (
                self.policy is GoogleMapsDecisionPolicy.TRUSTED_SCHEDULED
                and reasons
                and set(reasons) <= self._SCHEDULED_PASS_WARNING_CODES
            ):
                status = GoogleMapsDecisionStatus.PASS
            elif self.policy is GoogleMapsDecisionPolicy.TRUSTED_SCHEDULED:
                status = GoogleMapsDecisionStatus.QUARANTINE
            else:
                status = GoogleMapsDecisionStatus.REVIEW
        else:
            status = GoogleMapsDecisionStatus.PASS
        return GoogleMapsDecision(
            decision_id=f"{observation.observation_id}:decision",
            run_id=observation.run_id,
            observation_id=observation.observation_id,
            place_id=observation.place_id,
            status=status,
            validation_ids=[item.validation_id for item in validations],
            reason_codes=reasons,
            decided_at=self.clock(),
        )


class GoogleMapsDecisionWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, decision: GoogleMapsDecision) -> Path:
        destination = (
            self.root_directory
            / "entity=opening_status"
            / f"run={quote(decision.run_id, safe='-_.')}"
            / f"decision={quote(decision.decision_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(f"Decision already exists: {destination}") from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(decision.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
