from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    DailyOpeningStatus,
    EntityType,
    NexTripModel,
    OpeningStatusObservation,
    VerificationStatus,
)


class OpeningStatusApprovalError(ValueError):
    """Raised when an opening review cannot be approved without inventing data."""


def _approval_payload(
    *,
    canonical_dataset_id: str,
    canonical_dataset_hash: str,
    canonical_record_hash: str,
    place_id: str,
    entity_type: EntityType,
    city_id: str,
    source_observation_id: str,
    source_observation_hash: str,
    source_verification_status: VerificationStatus,
    approved_verification_status: VerificationStatus,
    opening_status: DailyOpeningStatus,
    local_date: str,
    observed_at: datetime,
    reviewer: str,
    approved_at: datetime,
    reason: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "canonical_dataset_id": canonical_dataset_id,
        "canonical_dataset_hash": canonical_dataset_hash,
        "canonical_record_hash": canonical_record_hash,
        "place_id": place_id,
        "entity_type": entity_type.value,
        "city_id": city_id,
        "source_observation_id": source_observation_id,
        "source_observation_hash": source_observation_hash,
        "source_verification_status": source_verification_status.value,
        "approved_verification_status": approved_verification_status.value,
        "opening_status": opening_status.value,
        "local_date": local_date,
        "observed_at": observed_at.isoformat(),
        "reviewer": reviewer,
        "approved_at": approved_at.isoformat(),
        "reason": reason,
    }


class OpeningStatusReviewApproval(NexTripModel):
    """Human approval pinned to one exact canonical opening observation."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_dataset_id: str = Field(min_length=1)
    canonical_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_verification_status: VerificationStatus
    approved_verification_status: VerificationStatus
    opening_status: DailyOpeningStatus
    local_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    observed_at: AwareDatetime
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_approval(self) -> OpeningStatusReviewApproval:
        if self.source_verification_status is not VerificationStatus.PENDING_REVIEW:
            raise ValueError("opening approval requires pending_review source evidence")
        if self.approved_verification_status is not VerificationStatus.HUMAN_VERIFIED:
            raise ValueError("opening approval must produce human_verified evidence")
        if self.approved_at < self.observed_at:
            raise ValueError("opening approved_at cannot precede observed_at")
        payload = _approval_payload(
            canonical_dataset_id=self.canonical_dataset_id,
            canonical_dataset_hash=self.canonical_dataset_hash,
            canonical_record_hash=self.canonical_record_hash,
            place_id=self.place_id,
            entity_type=self.entity_type,
            city_id=self.city_id,
            source_observation_id=self.source_observation_id,
            source_observation_hash=self.source_observation_hash,
            source_verification_status=self.source_verification_status,
            approved_verification_status=self.approved_verification_status,
            opening_status=self.opening_status,
            local_date=self.local_date,
            observed_at=self.observed_at,
            reviewer=self.reviewer,
            approved_at=self.approved_at,
            reason=self.reason,
        )
        expected_hash = stable_sha256(payload)
        if self.approval_hash != expected_hash:
            raise ValueError("approval_hash does not match opening approval content")
        if self.approval_id != f"opening-status-approval-{expected_hash[:20]}":
            raise ValueError("approval_id does not match approval_hash")
        return self


class OpeningStatusReviewApprovalWriter:
    """Write immutable, content-addressed opening-review approvals."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, approval: OpeningStatusReviewApproval) -> Path:
        return (
            self.root_directory
            / f"place={quote(approval.place_id, safe='-_.')}"
            / f"approval={quote(approval.approval_id, safe='-_.')}.json"
        )

    def write(self, approval: OpeningStatusReviewApproval) -> Path:
        validated = OpeningStatusReviewApproval.model_validate_json(
            approval.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                current = OpeningStatusReviewApproval.model_validate_json(
                    destination.read_bytes()
                )
                if current == validated:
                    return destination
                raise FileExistsError(
                    f"immutable opening approval already exists: {destination}"
                )
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(validated.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
        return destination

    def write_many(
        self,
        approvals: list[OpeningStatusReviewApproval],
    ) -> list[Path]:
        return [self.write(approval) for approval in approvals]


def build_opening_status_review_approvals(
    dataset: CanonicalActiveDataset,
    *,
    reviewer: str,
    place_ids: list[str] | None = None,
    approved_at: datetime | None = None,
    reason: str = "human_approved_pending_opening_review",
) -> list[OpeningStatusReviewApproval]:
    """Approve all, or an exact subset, of pending canonical openings."""

    reviewer_name = reviewer.strip()
    approval_reason = reason.strip()
    if not reviewer_name:
        raise OpeningStatusApprovalError("reviewer must not be blank")
    if not approval_reason:
        raise OpeningStatusApprovalError("approval reason must not be blank")
    reviewed_at = approved_at or datetime.now(timezone.utc)
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise OpeningStatusApprovalError("approved_at must be timezone-aware")

    records_by_id = {record.place_id: record for record in dataset.records}
    requested = sorted(set(place_ids or []))
    unknown = sorted(set(requested) - set(records_by_id))
    if unknown:
        raise OpeningStatusApprovalError(
            "opening approvals reference unknown canonical places: "
            + ", ".join(unknown)
        )

    selected_ids = requested or sorted(records_by_id)
    approvals: list[OpeningStatusReviewApproval] = []
    invalid_requested: list[str] = []
    for place_id in selected_ids:
        record = records_by_id[place_id]
        raw_opening = record.data.get("opening_status")
        try:
            observation = OpeningStatusObservation.model_validate(raw_opening)
        except (TypeError, ValueError):
            if requested:
                invalid_requested.append(place_id)
            continue
        if observation.verification_status is not VerificationStatus.PENDING_REVIEW:
            if requested:
                invalid_requested.append(place_id)
            continue
        if observation.place_id != record.place_id:
            raise OpeningStatusApprovalError(
                f"opening observation belongs to another place: {place_id}"
            )
        observation_hash = stable_sha256(observation.model_dump(mode="json"))
        payload = _approval_payload(
            canonical_dataset_id=dataset.dataset_id,
            canonical_dataset_hash=dataset.dataset_hash,
            canonical_record_hash=record.record_hash,
            place_id=record.place_id,
            entity_type=record.primary_type,
            city_id=record.city_id,
            source_observation_id=observation.observation_id,
            source_observation_hash=observation_hash,
            source_verification_status=observation.verification_status,
            approved_verification_status=VerificationStatus.HUMAN_VERIFIED,
            opening_status=observation.status,
            local_date=observation.local_date.isoformat(),
            observed_at=observation.observed_at,
            reviewer=reviewer_name,
            approved_at=reviewed_at,
            reason=approval_reason,
        )
        approval_hash = stable_sha256(payload)
        approvals.append(
            OpeningStatusReviewApproval(
                approval_id=f"opening-status-approval-{approval_hash[:20]}",
                approval_hash=approval_hash,
                **payload,
            )
        )

    if invalid_requested:
        raise OpeningStatusApprovalError(
            "requested places do not contain pending opening reviews: "
            + ", ".join(invalid_requested)
        )
    return approvals
