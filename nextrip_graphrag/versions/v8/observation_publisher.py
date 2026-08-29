from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.place_projection import (
    project_canonical_dataset_places,
)
from nextrip_pipeline.jobs.trivago_stay_batch import (
    TrivagoStayBatchItemStatus,
    TrivagoStayBatchSummary,
)
from nextrip_pipeline.publishing.current_availability import (
    CurrentHotelAvailabilitySnapshot,
)
from nextrip_pipeline.publishing.current_menu import CurrentMenuMetadata
from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.quality.opening_status_approval import (
    OpeningStatusReviewApproval,
)
from nextrip_pipeline.schemas import (
    HotelAvailabilityStatus,
    NexTripModel,
    NormalizedMenu,
    VerificationStatus,
)
from nextrip_pipeline.schemas.current_place import CurrentPlaceSnapshot


KB_VERSION = "v8"
HOTEL_PRICE_RETENTION_TIMEZONE = "Asia/Ho_Chi_Minh"
HOTEL_PRICE_CLEANUP_MAX_SUMMARY_AGE = timedelta(hours=8)
HOTEL_PRICE_CLEANUP_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
_HOTEL_PRICE_RETENTION_TZINFO = timezone(
    timedelta(hours=7),
    HOTEL_PRICE_RETENTION_TIMEZONE,
)
_PUBLISHABLE_VERIFICATION_STATUSES = {
    VerificationStatus.LEGACY_VERIFIED,
    VerificationStatus.AUTO_VERIFIED,
    VerificationStatus.AGENT_VERIFIED,
    VerificationStatus.HUMAN_VERIFIED,
}
_MENU_PLACE_TYPES = {"cafe", "restaurant", "nightlife"}
_INPUT_FAMILIES = {
    "current_menu",
    "current_menu_approval",
    "hotel_availability",
    "hotel_price",
    "opening_status_approval",
}


class V8ObservationInputError(ValueError):
    """Raised when current artifacts cannot safely be attached to V8 places."""


class V8ObservationPublishError(RuntimeError):
    """Raised when the graph does not match a validated observation plan."""


class V8ObservationKind(StrEnum):
    HOTEL_PRICE = "hotel_price"
    HOTEL_AVAILABILITY = "hotel_availability"
    OPENING_STATUS = "opening_status"
    MENU_SNAPSHOT = "menu_snapshot"


class V8ObservationPublishStatus(StrEnum):
    DRY_RUN = "dry_run"
    PUBLISHED = "published"


class V8HotelPriceCleanupGate(NexTripModel):
    """Auditable decision derived from the latest immutable hotel batch."""

    contract: str = "TrivagoStayBatchSummary"
    passed: bool = False
    reason: str = Field(min_length=1)
    summary_path: str | None = None
    summary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_id: str | None = None
    finished_at: AwareDatetime | None = None
    eligible_count: int | None = Field(default=None, ge=0)
    selected_count: int | None = Field(default=None, ge=0)
    completed_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_gate(self) -> V8HotelPriceCleanupGate:
        if self.passed and not (
            self.reason == "batch_complete"
            and self.summary_path
            and self.summary_sha256
            and self.run_id
            and self.finished_at is not None
            and self.eligible_count is not None
            and self.eligible_count > 0
            and self.selected_count is not None
            and self.selected_count > 0
            and self.selected_count == self.eligible_count
            and self.completed_count == self.selected_count
            and self.failed_count == 0
        ):
            raise ValueError(
                "passed hotel cleanup gate requires a complete zero-failure batch"
            )
        return self


class V8HotelPriceRetentionResult(NexTripModel):
    """Audit result for bounded hotel price history in Neo4j.

    ``previous_calendar_days=1`` means that an anchor crawl on local day D
    retains observations from D and D-1. Accepted/raw JSON history is outside
    this graph-only policy and remains available for audit and replay.
    """

    policy: str = "current_and_previous_calendar_days"
    enabled: bool = True
    applied: bool = False
    previous_calendar_days: int = Field(default=1, ge=0)
    timezone: str = HOTEL_PRICE_RETENTION_TIMEZONE
    anchor_observed_at: AwareDatetime | None = None
    cutoff_observed_at: AwareDatetime | None = None
    deleted_price_observations: int = Field(default=0, ge=0)
    deleted_availability_observations: int = Field(default=0, ge=0)
    quality_gate: V8HotelPriceCleanupGate = Field(
        default_factory=lambda: V8HotelPriceCleanupGate(
            reason="batch_summary_not_configured"
        )
    )
    skip_reason: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> V8HotelPriceRetentionResult:
        if not self.enabled and (
            self.applied
            or self.anchor_observed_at is not None
            or self.cutoff_observed_at is not None
            or self.deleted_price_observations
            or self.deleted_availability_observations
        ):
            raise ValueError("disabled hotel price retention cannot report changes")
        if self.applied and (
            self.anchor_observed_at is None or self.cutoff_observed_at is None
        ):
            raise ValueError("applied hotel price retention requires its time window")
        if self.applied and not self.quality_gate.passed:
            raise ValueError("hotel price retention requires a passed quality gate")
        if not self.applied and (
            self.deleted_price_observations
            or self.deleted_availability_observations
        ):
            raise ValueError("unapplied hotel price retention cannot delete rows")
        if not self.applied and not self.skip_reason:
            raise ValueError("unapplied hotel price retention requires a skip reason")
        return self


class V8ObservationInputArtifact(NexTripModel):
    family: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class V8ObservationRow(NexTripModel):
    graph_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: V8ObservationKind
    place_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    properties: dict[str, JsonValue]
    referenced_observation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content_hash(self) -> V8ObservationRow:
        expected = stable_sha256(_observation_content_payload(self))
        if self.content_hash != expected:
            raise ValueError("observation content_hash does not match its content")
        return self


class V8MenuItemRow(NexTripModel):
    graph_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    menu_graph_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    position: int = Field(ge=0)
    properties: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_content_hash(self) -> V8MenuItemRow:
        expected = stable_sha256(_menu_item_content_payload(self))
        if self.content_hash != expected:
            raise ValueError("menu item content_hash does not match its content")
        return self


class V8ObservationPublishPlan(NexTripModel):
    schema_version: str = "1.0.0"
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kb_version: str = KB_VERSION
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    built_at: AwareDatetime
    input_artifacts: list[V8ObservationInputArtifact]
    observations: list[V8ObservationRow]
    menu_items: list[V8MenuItemRow]
    counts: dict[str, int]
    hotel_price_prefilter_gate_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_plan(self) -> V8ObservationPublishPlan:
        if self.kb_version != KB_VERSION:
            raise ValueError("observation publisher only supports kb_version=v8")
        graph_ids = [row.graph_id for row in self.observations]
        if len(graph_ids) != len(set(graph_ids)):
            raise ValueError("observation graph IDs must be unique")
        menu_item_ids = [row.graph_id for row in self.menu_items]
        if len(menu_item_ids) != len(set(menu_item_ids)):
            raise ValueError("menu item graph IDs must be unique")
        menu_ids = {
            row.graph_id
            for row in self.observations
            if row.kind is V8ObservationKind.MENU_SNAPSHOT
        }
        if any(row.menu_graph_id not in menu_ids for row in self.menu_items):
            raise ValueError("menu items must reference a menu snapshot in the plan")
        if any(item.family not in _INPUT_FAMILIES for item in self.input_artifacts):
            raise ValueError("observation plan contains an unsupported input family")
        expected_artifacts = sorted(
            self.input_artifacts,
            key=lambda item: (item.family, item.relative_path),
        )
        if self.input_artifacts != expected_artifacts:
            raise ValueError("input artifacts must be sorted")
        expected_observations = sorted(
            self.observations,
            key=lambda row: (
                row.kind.value,
                row.place_id,
                _iso_datetime(row.observed_at),
                row.graph_id,
            ),
        )
        if self.observations != expected_observations:
            raise ValueError("observation rows must be sorted")
        expected_menu_items = sorted(
            self.menu_items,
            key=lambda row: (row.menu_graph_id, row.position, row.graph_id),
        )
        if self.menu_items != expected_menu_items:
            raise ValueError("menu item rows must be sorted")
        price_ids = {
            row.graph_id
            for row in self.observations
            if row.kind is V8ObservationKind.HOTEL_PRICE
        }
        for row in self.observations:
            if row.properties.get("place_id") != row.place_id:
                raise ValueError("observation property place_id does not match its row")
            if row.properties.get("source_observation_id") != row.source_observation_id:
                raise ValueError("source observation property does not match its row")
            if row.referenced_observation_ids != sorted(
                set(row.referenced_observation_ids)
            ):
                raise ValueError("observation references must be unique and sorted")
            if any(
                reference not in price_ids
                for reference in row.referenced_observation_ids
            ):
                raise ValueError("observation references a price outside the plan")
        expected_counts = _plan_counts(self.observations, self.menu_items)
        if self.counts != expected_counts:
            raise ValueError("observation plan counts do not match its rows")
        expected_hash = stable_sha256(
            _plan_payload(
                dataset_id=self.dataset_id,
                dataset_hash=self.dataset_hash,
                artifacts=self.input_artifacts,
                observations=self.observations,
                menu_items=self.menu_items,
                hotel_price_prefilter_gate_sha256=(
                    self.hotel_price_prefilter_gate_sha256
                ),
            )
        )
        if self.plan_hash != expected_hash:
            raise ValueError("plan_hash does not match observation plan content")
        if self.plan_id != f"v8-observation-plan-{expected_hash[:20]}":
            raise ValueError("plan_id does not match plan_hash")
        return self


class V8ObservationPublishManifest(NexTripModel):
    schema_version: str = "1.1.0"
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kb_version: str = KB_VERSION
    status: V8ObservationPublishStatus
    created_at: AwareDatetime
    counts: dict[str, int]
    processed_counts: dict[str, int]
    hotel_price_retention: V8HotelPriceRetentionResult

    @model_validator(mode="after")
    def validate_manifest(self) -> V8ObservationPublishManifest:
        if self.status is V8ObservationPublishStatus.DRY_RUN:
            if any(self.processed_counts.values()):
                raise ValueError("dry-run manifest cannot contain processed rows")
        elif self.processed_counts != self.counts:
            raise ValueError("published manifest must process every planned row")
        payload = _manifest_payload(self)
        expected_hash = stable_sha256(payload)
        if self.manifest_hash != expected_hash:
            raise ValueError("manifest_hash does not match publish result")
        if self.manifest_id != f"v8-observation-publish-{expected_hash[:20]}":
            raise ValueError("manifest_id does not match manifest_hash")
        return self


class V8ObservationStore(Protocol):
    """Small store boundary shared by Neo4jGraphStore and unit-test fakes."""

    driver: Any
    settings: Any

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


def load_latest_hotel_price_cleanup_gate(
    summary_root: str | Path | None,
    *,
    evaluated_at: datetime | None = None,
    max_summary_age: timedelta = HOTEL_PRICE_CLEANUP_MAX_SUMMARY_AGE,
    future_clock_skew: timedelta = HOTEL_PRICE_CLEANUP_FUTURE_CLOCK_SKEW,
) -> V8HotelPriceCleanupGate:
    """Evaluate the newest immutable Trivago stay-batch summary.

    The newest artifact is selected by the contract's timezone-aware
    ``finished_at`` value, never by a hard-coded hotel count or filename.
    Invalid artifacts fail the cleanup gate closed without blocking observation
    publication.
    """

    if max_summary_age <= timedelta(0):
        raise ValueError("max_summary_age must be positive")
    if future_clock_skew < timedelta(0):
        raise ValueError("future_clock_skew cannot be negative")
    gate_evaluated_at = _aware_utc(
        evaluated_at or datetime.now(timezone.utc)
    )
    if summary_root is None:
        return V8HotelPriceCleanupGate(reason="batch_summary_not_configured")
    root = Path(summary_root)
    if root.is_file():
        paths = [root]
    elif root.is_dir():
        paths = sorted(root.glob("run=*.json"))
    else:
        return V8HotelPriceCleanupGate(
            reason="batch_summary_missing",
            summary_path=str(root),
        )
    if not paths:
        return V8HotelPriceCleanupGate(
            reason="batch_summary_missing",
            summary_path=str(root),
        )

    validated: list[tuple[TrivagoStayBatchSummary, Path, bytes]] = []
    invalid_paths: list[Path] = []
    for path in paths:
        try:
            content = path.read_bytes()
            summary = TrivagoStayBatchSummary.model_validate_json(content)
        except (OSError, TypeError, ValueError):
            invalid_paths.append(path)
            continue
        validated.append((summary, path, content))
    if invalid_paths:
        return V8HotelPriceCleanupGate(
            reason="batch_summary_invalid",
            summary_path=str(invalid_paths[-1]),
        )
    if not validated:
        return V8HotelPriceCleanupGate(
            reason="batch_summary_invalid",
            summary_path=str(root),
        )

    summary, path, content = max(
        validated,
        key=lambda item: (
            item[0].finished_at,
            item[0].started_at,
            item[0].run_id,
        ),
    )
    common = {
        "summary_path": str(path),
        "summary_sha256": hashlib.sha256(content).hexdigest(),
        "run_id": summary.run_id,
        "finished_at": summary.finished_at,
        "eligible_count": summary.eligible_count,
        "selected_count": summary.selected_count,
        "completed_count": summary.completed_count,
        "failed_count": summary.failed_count,
    }
    if summary.selected_count == 0:
        return V8HotelPriceCleanupGate(reason="batch_empty", **common)
    if summary.selected_count != summary.eligible_count:
        return V8HotelPriceCleanupGate(reason="batch_partial", **common)
    if summary.failed_count != 0:
        return V8HotelPriceCleanupGate(reason="batch_failed", **common)
    if summary.completed_count != summary.selected_count:
        return V8HotelPriceCleanupGate(reason="batch_incomplete", **common)
    if summary.finished_at > gate_evaluated_at + future_clock_skew:
        return V8HotelPriceCleanupGate(reason="batch_from_future", **common)
    if summary.finished_at < gate_evaluated_at - max_summary_age:
        return V8HotelPriceCleanupGate(reason="batch_stale", **common)
    item_completed = sum(
        item.status == TrivagoStayBatchItemStatus.COMPLETED
        for item in summary.items
    )
    item_failed = sum(
        item.status == TrivagoStayBatchItemStatus.FAILED
        for item in summary.items
    )
    item_hotel_ids = [item.hotel_id for item in summary.items]
    if (
        summary.finished_at < summary.started_at
        or len(summary.items) != summary.selected_count
        or len(set(item_hotel_ids)) != len(item_hotel_ids)
        or summary.completed_count + summary.failed_count != summary.selected_count
        or item_completed != summary.completed_count
        or item_failed != summary.failed_count
    ):
        return V8HotelPriceCleanupGate(
            reason="batch_summary_inconsistent",
            **common,
        )
    return V8HotelPriceCleanupGate(
        passed=True,
        reason="batch_complete",
        **common,
    )


def build_v8_observation_plan(
    canonical_dataset_path: str | Path,
    *,
    hotel_price_root: str | Path,
    hotel_availability_root: str | Path,
    current_menu_root: str | Path | None = None,
    opening_approval_root: str | Path | None = None,
    hotel_price_previous_calendar_days: int = 1,
    hotel_price_cleanup_gate: V8HotelPriceCleanupGate | None = None,
    built_at: datetime | None = None,
) -> V8ObservationPublishPlan:
    """Build a deterministic plan from canonical and contextual artifacts.

    Place identity and opening evidence come exclusively from the pinned
    canonical dataset. Traffic is deliberately absent from this API. Passing a
    current menu root is optional; every menu file must have the approval
    metadata emitted by ``CurrentMenuWriter`` or plan construction fails closed.
    """

    dataset = read_canonical_active_dataset(canonical_dataset_path)
    return build_v8_observation_plan_for_dataset(
        dataset,
        hotel_price_root=hotel_price_root,
        hotel_availability_root=hotel_availability_root,
        current_menu_root=current_menu_root,
        opening_approval_root=opening_approval_root,
        hotel_price_previous_calendar_days=(
            hotel_price_previous_calendar_days
        ),
        hotel_price_cleanup_gate=hotel_price_cleanup_gate,
        built_at=built_at,
    )


def build_v8_observation_plan_for_dataset(
    dataset: CanonicalActiveDataset,
    *,
    hotel_price_root: str | Path,
    hotel_availability_root: str | Path,
    current_menu_root: str | Path | None = None,
    opening_approval_root: str | Path | None = None,
    hotel_price_previous_calendar_days: int = 1,
    hotel_price_cleanup_gate: V8HotelPriceCleanupGate | None = None,
    built_at: datetime | None = None,
) -> V8ObservationPublishPlan:
    """Variant accepting an already validated dataset for orchestration/tests."""

    if hotel_price_previous_calendar_days < 0:
        raise V8ObservationInputError(
            "hotel_price_previous_calendar_days cannot be negative"
        )
    canonical_types = {
        record.place_id: {item.value for item in record.place_types}
        for record in dataset.records
    }
    if len(canonical_types) != len(dataset.records):
        raise V8ObservationInputError("canonical dataset contains duplicate place IDs")

    price_snapshots, price_artifacts = _load_artifacts(
        hotel_price_root,
        CurrentHotelPriceSnapshot,
        family="hotel_price",
    )
    availability_snapshots, availability_artifacts = _load_artifacts(
        hotel_availability_root,
        CurrentHotelAvailabilitySnapshot,
        family="hotel_availability",
    )
    # A failed/missing crawl summary must not make a fresh graph silently lose
    # otherwise valid current projections. Bound the plan to D/D-1 only when
    # the exact batch gate used by the publisher has passed.
    if hotel_price_cleanup_gate is not None and hotel_price_cleanup_gate.passed:
        (
            price_snapshots,
            price_artifacts,
            availability_snapshots,
            availability_artifacts,
        ) = _filter_hotel_inputs_for_graph_retention(
            price_snapshots,
            price_artifacts,
            availability_snapshots,
            availability_artifacts,
            previous_calendar_days=hotel_price_previous_calendar_days,
        )
        hotel_price_prefilter_gate_sha256 = stable_sha256(
            hotel_price_cleanup_gate.model_dump(mode="json")
        )
    else:
        hotel_price_prefilter_gate_sha256 = None
    canonical_place_snapshots = _canonical_opening_snapshots(dataset)
    opening_approvals, opening_approval_artifacts = _load_optional_artifacts(
        opening_approval_root,
        OpeningStatusReviewApproval,
        family="opening_status_approval",
    )
    approvals_by_observation_id: dict[str, list[OpeningStatusReviewApproval]] = {}
    approval_artifact_by_id: dict[str, V8ObservationInputArtifact] = {}
    for approval, artifact in zip(
        opening_approvals,
        opening_approval_artifacts,
        strict=True,
    ):
        approvals_by_observation_id.setdefault(
            approval.source_observation_id,
            [],
        ).append(approval)
        approval_artifact_by_id[approval.approval_id] = artifact
    canonical_records = {record.place_id: record for record in dataset.records}

    observations: list[V8ObservationRow] = []
    used_opening_approval_artifacts: list[V8ObservationInputArtifact] = []
    for snapshot in price_snapshots:
        observations.append(_price_row(snapshot, canonical_types))
    price_rows_by_source_id = _unique_price_rows_by_source_id(observations)

    for snapshot in availability_snapshots:
        # UNKNOWN is a crawl/identity outcome retained in run audit only. Old
        # current snapshots from pre-hardening releases must not block valid
        # observations or become graph facts.
        if snapshot.observation.status is HotelAvailabilityStatus.UNKNOWN:
            continue
        observations.append(
            _availability_row(
                snapshot,
                canonical_types,
                price_rows_by_source_id,
            )
        )
    for snapshot in canonical_place_snapshots:
        if snapshot.opening is None:
            continue
        approval = _matching_opening_approval(
            dataset,
            canonical_records[snapshot.place_id].record_hash,
            snapshot,
            approvals_by_observation_id.get(
                snapshot.opening.observation_id,
                [],
            ),
        )
        # Canonical datasets deliberately retain unresolved operational
        # evidence for audit and later human review.  Those records must not be
        # published as trusted observations, but they also must not prevent
        # independently verified observations from being planned.
        if (
            snapshot.opening.verification_status
            not in _PUBLISHABLE_VERIFICATION_STATUSES
            and approval is None
        ):
            continue
        observations.append(
            _opening_row(
                snapshot,
                canonical_types,
                approval=approval,
            )
        )
        if approval is not None:
            used_opening_approval_artifacts.append(
                approval_artifact_by_id[approval.approval_id]
            )

    menu_items: list[V8MenuItemRow] = []
    menu_artifacts: list[V8ObservationInputArtifact] = []
    if current_menu_root is not None:
        menu_rows, menu_items, menu_artifacts = _menu_rows(
            current_menu_root,
            canonical_types,
        )
        observations.extend(menu_rows)

    observations = _deduplicate_observation_rows(observations)
    observations.sort(
        key=lambda row: (
            row.kind.value,
            row.place_id,
            _iso_datetime(row.observed_at),
            row.graph_id,
        )
    )
    menu_items.sort(key=lambda row: (row.menu_graph_id, row.position, row.graph_id))
    artifacts = sorted(
        [
            *price_artifacts,
            *availability_artifacts,
            *used_opening_approval_artifacts,
            *menu_artifacts,
        ],
        key=lambda item: (item.family, item.relative_path),
    )
    payload = _plan_payload(
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        artifacts=artifacts,
        observations=observations,
        menu_items=menu_items,
        hotel_price_prefilter_gate_sha256=(
            hotel_price_prefilter_gate_sha256
        ),
    )
    plan_hash = stable_sha256(payload)
    return V8ObservationPublishPlan(
        plan_id=f"v8-observation-plan-{plan_hash[:20]}",
        plan_hash=plan_hash,
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        built_at=_aware_utc(built_at),
        input_artifacts=artifacts,
        observations=observations,
        menu_items=menu_items,
        counts=_plan_counts(observations, menu_items),
        hotel_price_prefilter_gate_sha256=(
            hotel_price_prefilter_gate_sha256
        ),
    )


def read_v8_observation_plan(path: str | Path) -> V8ObservationPublishPlan:
    """Load and revalidate an immutable plan before an apply-only task."""

    return V8ObservationPublishPlan.model_validate_json(Path(path).read_bytes())


def _canonical_opening_snapshots(
    dataset: CanonicalActiveDataset,
) -> list[CurrentPlaceSnapshot]:
    projected = project_canonical_dataset_places(dataset)
    return [
        projected[place_id]
        for place_id in sorted(projected)
        if projected[place_id].opening is not None
    ]


def write_v8_observation_plan(
    path: str | Path,
    plan: V8ObservationPublishPlan,
) -> Path:
    """Persist a content-addressed plan without replacing different content."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = read_v8_observation_plan(destination)
        if existing.plan_hash == plan.plan_hash:
            return destination
        raise FileExistsError(
            f"refusing to replace a different V8 observation plan: {destination}"
        )
    content = plan.model_dump_json(indent=2) + "\n"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    return destination


class V8ObservationPublisher:
    """Idempotently append current observations to an existing V8 graph.

    ``MERGE`` is applied only to observation IDs and relationships. Place nodes
    are always matched, never created, and existing observation properties are
    never updated. A re-run is therefore idempotent while later crawl evidence
    remains a separate observation node.
    """

    def __init__(
        self,
        store: V8ObservationStore,
        *,
        batch_size: int = 500,
        hotel_price_previous_calendar_days: int = 1,
        hotel_batch_summary_root: str | Path | None = None,
        hotel_price_cleanup_gate: V8HotelPriceCleanupGate | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if hotel_price_previous_calendar_days < 0:
            raise ValueError(
                "hotel_price_previous_calendar_days cannot be negative"
            )
        self.store = store
        self.batch_size = batch_size
        self.hotel_price_previous_calendar_days = (
            hotel_price_previous_calendar_days
        )
        if (
            hotel_batch_summary_root is not None
            and hotel_price_cleanup_gate is not None
        ):
            raise ValueError(
                "configure a hotel batch summary root or a pre-evaluated gate, not both"
            )
        self.hotel_batch_summary_root = hotel_batch_summary_root
        self.hotel_price_cleanup_gate = hotel_price_cleanup_gate
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(
        self,
        plan: V8ObservationPublishPlan,
        *,
        dry_run: bool = False,
        manifest_path: str | Path | None = None,
    ) -> V8ObservationPublishManifest:
        published_at = self.clock()
        cleanup_gate = self.hotel_price_cleanup_gate or (
            load_latest_hotel_price_cleanup_gate(
                self.hotel_batch_summary_root,
                evaluated_at=published_at,
            )
        )
        if plan.hotel_price_prefilter_gate_sha256 is not None:
            publish_gate_sha256 = stable_sha256(
                cleanup_gate.model_dump(mode="json")
            )
            if publish_gate_sha256 != plan.hotel_price_prefilter_gate_sha256:
                raise V8ObservationPublishError(
                    "hotel price prefilter gate does not match publish gate"
                )
        if dry_run:
            manifest = _new_publish_manifest(
                plan,
                V8ObservationPublishStatus.DRY_RUN,
                created_at=published_at,
                processed_counts={key: 0 for key in plan.counts},
                hotel_price_retention=V8HotelPriceRetentionResult(
                    previous_calendar_days=(
                        self.hotel_price_previous_calendar_days
                    ),
                    quality_gate=cleanup_gate,
                    skip_reason="dry_run",
                ),
            )
            _write_manifest(manifest_path, manifest)
            return manifest

        self._ensure_schema()
        database = getattr(
            getattr(self.store, "settings", None),
            "neo4j_database",
            None,
        )
        session_options = {"database": database} if database else {}
        with self.store.driver.session(**session_options) as session:
            processed, hotel_price_retention = session.execute_write(
                lambda transaction: self._publish_transaction(
                    transaction,
                    plan,
                    cleanup_gate,
                )
            )

        manifest = _new_publish_manifest(
            plan,
            V8ObservationPublishStatus.PUBLISHED,
            created_at=published_at,
            processed_counts=processed,
            hotel_price_retention=hotel_price_retention,
        )
        _write_manifest(manifest_path, manifest)
        return manifest

    def _publish_transaction(
        self,
        transaction: Any,
        plan: V8ObservationPublishPlan,
        cleanup_gate: V8HotelPriceCleanupGate,
    ) -> tuple[dict[str, int], V8HotelPriceRetentionResult]:
        """Validate and append every planned row in one write transaction."""

        self._require_active_release(transaction, plan)
        self._require_graph_places(transaction, plan)
        processed = {key: 0 for key in plan.counts}

        for kind in V8ObservationKind:
            rows = [row for row in plan.observations if row.kind is kind]
            for batch in _chunks(rows, self.batch_size):
                count = self._merge_observations(
                    transaction,
                    kind,
                    batch,
                    plan,
                )
                processed[kind.value] += count

        references = [
            {
                "availability_graph_id": row.graph_id,
                "price_graph_ids": row.referenced_observation_ids,
            }
            for row in plan.observations
            if row.kind is V8ObservationKind.HOTEL_AVAILABILITY
            and row.referenced_observation_ids
        ]
        expected_reference_count = sum(
            len(row["price_graph_ids"]) for row in references
        )
        processed_references = 0
        for batch in _chunks(references, self.batch_size):
            result = transaction.run(
                _AVAILABILITY_PRICE_QUERY,
                rows=batch,
                kb_version=KB_VERSION,
            )
            processed_references += _processed_count(_result_rows(result))
        if processed_references != expected_reference_count:
            raise V8ObservationPublishError(
                "not every availability-to-price reference was published"
            )
        processed["availability_price_relationships"] = processed_references

        for batch in _chunks(plan.menu_items, self.batch_size):
            result = transaction.run(
                _MENU_ITEM_QUERY,
                rows=[row.model_dump(mode="json") for row in batch],
                kb_version=KB_VERSION,
                dataset_id=plan.dataset_id,
                dataset_hash=plan.dataset_hash,
            )
            result_rows = _result_rows(result)
            _raise_immutable_mismatches(result_rows, "menu items")
            processed["menu_items"] += _processed_count(result_rows)

        processed["total_observations"] = sum(
            processed[kind.value] for kind in V8ObservationKind
        )

        for key, expected in plan.counts.items():
            if processed.get(key, 0) != expected:
                raise V8ObservationPublishError(
                    f"published {processed.get(key, 0)} {key}; expected {expected}"
                )

        hotel_price_retention = self._apply_hotel_price_retention(
            transaction,
            plan,
            cleanup_gate,
        )
        # Re-check the release and active place labels after all writes. This
        # makes a concurrent canonical-release switch fail the transaction
        # instead of committing observations against a superseded snapshot.
        self._require_active_release(transaction, plan)
        self._require_graph_places(transaction, plan)
        return processed, hotel_price_retention

    def _apply_hotel_price_retention(
        self,
        transaction: Any,
        plan: V8ObservationPublishPlan,
        cleanup_gate: V8HotelPriceCleanupGate,
    ) -> V8HotelPriceRetentionResult:
        """Keep the newest crawl day and N preceding Vietnam calendar days.

        The anchor is the newest price already present after this plan's merge.
        This makes retries deterministic and prevents an older retried plan from
        widening the retention window or resurrecting deleted revisions.
        Linked availability observations are removed first so no retained graph
        node is left pointing at a deleted price.
        """

        if not any(
            row.kind is V8ObservationKind.HOTEL_PRICE
            for row in plan.observations
        ):
            return V8HotelPriceRetentionResult(
                previous_calendar_days=(
                    self.hotel_price_previous_calendar_days
                ),
                quality_gate=cleanup_gate,
                skip_reason="no_hotel_price_observations",
            )
        if not cleanup_gate.passed:
            return V8HotelPriceRetentionResult(
                previous_calendar_days=(
                    self.hotel_price_previous_calendar_days
                ),
                quality_gate=cleanup_gate,
                skip_reason=cleanup_gate.reason,
            )

        latest_rows = _result_rows(
            transaction.run(
                _LATEST_HOTEL_PRICE_OBSERVED_AT_QUERY,
                kb_version=KB_VERSION,
            )
        )
        if len(latest_rows) != 1:
            raise V8ObservationPublishError(
                "hotel price retention could not determine its anchor"
            )
        raw_anchor = latest_rows[0].get("latest_observed_at")
        if raw_anchor is None:
            raise V8ObservationPublishError(
                "hotel price retention found no published price anchor"
            )
        anchor = _parse_graph_datetime(raw_anchor)
        cutoff = _hotel_price_retention_cutoff(
            anchor,
            self.hotel_price_previous_calendar_days,
        )
        cutoff_value = _iso_datetime(cutoff)

        availability_rows = _result_rows(
            transaction.run(
                _PRUNE_HOTEL_AVAILABILITY_QUERY,
                kb_version=KB_VERSION,
                cutoff_observed_at=cutoff_value,
            )
        )
        deleted_availability = _deleted_count(
            availability_rows,
            "hotel availability retention",
        )
        price_rows = _result_rows(
            transaction.run(
                _PRUNE_HOTEL_PRICE_QUERY,
                kb_version=KB_VERSION,
                cutoff_observed_at=cutoff_value,
            )
        )
        deleted_prices = _deleted_count(
            price_rows,
            "hotel price retention",
        )
        return V8HotelPriceRetentionResult(
            applied=True,
            previous_calendar_days=self.hotel_price_previous_calendar_days,
            anchor_observed_at=anchor,
            cutoff_observed_at=cutoff,
            deleted_price_observations=deleted_prices,
            deleted_availability_observations=deleted_availability,
            quality_gate=cleanup_gate,
        )

    def _ensure_schema(self) -> None:
        ensure_v8_observation_schema(self.store)

    def _require_active_release(
        self,
        transaction: Any,
        plan: V8ObservationPublishPlan,
    ) -> None:
        rows = _result_rows(
            transaction.run(
                _ACTIVE_RELEASE_QUERY,
                kb_version=KB_VERSION,
                dataset_id=plan.dataset_id,
                dataset_hash=plan.dataset_hash,
            )
        )
        matches = int(rows[0].get("matches", 0)) if rows else 0
        if matches != 1:
            raise V8ObservationPublishError(
                "expected exactly one active V8 release for the canonical dataset; "
                f"found {matches}"
            )

    def _require_graph_places(
        self,
        transaction: Any,
        plan: V8ObservationPublishPlan,
    ) -> None:
        place_ids = sorted(
            {
                *(row.place_id for row in plan.observations),
                *(row.place_id for row in plan.menu_items),
            }
        )
        if not place_ids:
            return
        rows = _result_rows(
            transaction.run(
                _PLACE_VALIDATION_QUERY,
                place_ids=place_ids,
                kb_version=KB_VERSION,
            )
        )
        invalid = [row for row in rows if int(row.get("matches", 0)) != 1]
        if invalid:
            details = ", ".join(
                f"{row.get('place_id')}({row.get('matches', 0)})" for row in invalid
            )
            raise V8ObservationPublishError(
                f"observation places are missing or ambiguous in V8: {details}"
            )

    def _merge_observations(
        self,
        transaction: Any,
        kind: V8ObservationKind,
        batch: list[V8ObservationRow],
        plan: V8ObservationPublishPlan,
    ) -> int:
        if not batch:
            return 0
        result = transaction.run(
            _OBSERVATION_QUERIES[kind],
            rows=[row.model_dump(mode="json") for row in batch],
            kb_version=KB_VERSION,
            dataset_id=plan.dataset_id,
            dataset_hash=plan.dataset_hash,
            ingested_at=_iso_datetime(plan.built_at),
        )
        rows = _result_rows(result)
        _raise_immutable_mismatches(rows, f"{kind.value} observations")
        count = _processed_count(rows)
        if count != len(batch):
            raise V8ObservationPublishError(
                f"published {count} {kind.value} observations; expected {len(batch)}"
            )
        return count


def _load_artifacts(
    root: str | Path,
    model: type[NexTripModel],
    *,
    family: str,
    recursive: bool = True,
) -> tuple[list[Any], list[V8ObservationInputArtifact]]:
    directory = Path(root)
    if not directory.is_dir():
        raise V8ObservationInputError(f"{family} root is not a directory: {directory}")
    iterator = directory.rglob("*.json") if recursive else directory.glob("*.json")
    paths = sorted(
        (path for path in iterator if not path.name.startswith(".")),
        key=lambda path: path.relative_to(directory).as_posix(),
    )
    values: list[Any] = []
    artifacts: list[V8ObservationInputArtifact] = []
    for path in paths:
        content = path.read_bytes()
        try:
            values.append(model.model_validate_json(content))
        except (TypeError, ValueError) as error:
            raise V8ObservationInputError(
                f"invalid {family} artifact {path}: {error}"
            ) from error
        artifacts.append(
            V8ObservationInputArtifact(
                family=family,
                relative_path=path.relative_to(directory).as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return values, artifacts


def _load_optional_artifacts(
    root: str | Path | None,
    model: type[NexTripModel],
    *,
    family: str,
) -> tuple[list[Any], list[V8ObservationInputArtifact]]:
    if root is None:
        return [], []
    directory = Path(root)
    if not directory.exists():
        return [], []
    return _load_artifacts(directory, model, family=family)


def _filter_hotel_inputs_for_graph_retention(
    price_snapshots: list[CurrentHotelPriceSnapshot],
    price_artifacts: list[V8ObservationInputArtifact],
    availability_snapshots: list[CurrentHotelAvailabilitySnapshot],
    availability_artifacts: list[V8ObservationInputArtifact],
    *,
    previous_calendar_days: int,
) -> tuple[
    list[CurrentHotelPriceSnapshot],
    list[V8ObservationInputArtifact],
    list[CurrentHotelAvailabilitySnapshot],
    list[V8ObservationInputArtifact],
]:
    """Exclude stale current projections before they can be re-merged.

    The immutable accepted JSON store is unaffected. This filter only bounds
    the Neo4j publish plan and avoids re-creating rows that retention deleted on
    the preceding scheduled run.
    """

    if previous_calendar_days < 0:
        raise V8ObservationInputError(
            "hotel_price_previous_calendar_days cannot be negative"
        )
    if not price_snapshots:
        return (
            price_snapshots,
            price_artifacts,
            availability_snapshots,
            availability_artifacts,
        )
    anchor = max(
        snapshot.observation.observed_at for snapshot in price_snapshots
    )
    cutoff = _hotel_price_retention_cutoff(
        anchor,
        previous_calendar_days,
    )
    retained_prices = [
        (snapshot, artifact)
        for snapshot, artifact in zip(
            price_snapshots,
            price_artifacts,
            strict=True,
        )
        if snapshot.observation.observed_at >= cutoff
    ]
    retained_availability = [
        (snapshot, artifact)
        for snapshot, artifact in zip(
            availability_snapshots,
            availability_artifacts,
            strict=True,
        )
        if snapshot.observation.observed_at >= cutoff
    ]
    return (
        [snapshot for snapshot, _ in retained_prices],
        [artifact for _, artifact in retained_prices],
        [snapshot for snapshot, _ in retained_availability],
        [artifact for _, artifact in retained_availability],
    )


def _matching_opening_approval(
    dataset: CanonicalActiveDataset,
    canonical_record_hash: str,
    snapshot: CurrentPlaceSnapshot,
    candidates: list[OpeningStatusReviewApproval],
) -> OpeningStatusReviewApproval | None:
    observation = snapshot.opening
    if observation is None:
        return None
    observation_hash = stable_sha256(observation.model_dump(mode="json"))
    matches = [
        approval
        for approval in candidates
        if (
            approval.canonical_dataset_id == dataset.dataset_id
            and approval.canonical_dataset_hash == dataset.dataset_hash
            and approval.canonical_record_hash == canonical_record_hash
            and approval.place_id == snapshot.place_id
            and approval.entity_type is snapshot.entity_type
            and approval.city_id == snapshot.city_id
            and approval.source_observation_id == observation.observation_id
            and approval.source_observation_hash == observation_hash
            and approval.source_verification_status
            is observation.verification_status
            and approval.opening_status is observation.status
            and approval.local_date == observation.local_date.isoformat()
            and approval.observed_at == observation.observed_at
        )
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: (item.approved_at, item.approval_id))


def _new_observation_row(
    *,
    graph_id: str,
    kind: V8ObservationKind,
    place_id: str,
    source_observation_id: str,
    observed_at: datetime,
    properties: dict[str, JsonValue],
    referenced_observation_ids: list[str] | None = None,
) -> V8ObservationRow:
    values = {
        "graph_id": graph_id,
        "kind": kind,
        "place_id": place_id,
        "source_observation_id": source_observation_id,
        "observed_at": observed_at,
        "properties": properties,
        "referenced_observation_ids": referenced_observation_ids or [],
    }
    draft = V8ObservationRow.model_construct(content_hash="0" * 64, **values)
    return V8ObservationRow(
        content_hash=stable_sha256(_observation_content_payload(draft)),
        **values,
    )


def _new_menu_item_row(
    *,
    graph_id: str,
    menu_graph_id: str,
    place_id: str,
    position: int,
    properties: dict[str, JsonValue],
) -> V8MenuItemRow:
    values = {
        "graph_id": graph_id,
        "menu_graph_id": menu_graph_id,
        "place_id": place_id,
        "position": position,
        "properties": properties,
    }
    draft = V8MenuItemRow.model_construct(content_hash="0" * 64, **values)
    return V8MenuItemRow(
        content_hash=stable_sha256(_menu_item_content_payload(draft)),
        **values,
    )


def _price_row(
    snapshot: CurrentHotelPriceSnapshot,
    canonical_types: dict[str, set[str]],
) -> V8ObservationRow:
    observation = snapshot.observation
    _require_matching_snapshot_ids(
        snapshot.hotel_id,
        snapshot.observation_id,
        observation.hotel_id,
        observation.observation_id,
        family="hotel_price",
    )
    _require_canonical_type(observation.hotel_id, "hotel", canonical_types)
    _require_publishable(observation.verification_status, "hotel_price")
    identity = {
        "source_observation_id": observation.observation_id,
        "place_id": observation.hotel_id,
        "observed_at": _iso_datetime(observation.observed_at),
        "offer_key": observation.offer_key,
        "check_in": observation.check_in.isoformat(),
        "check_out": observation.check_out.isoformat(),
        "occupancy": observation.occupancy.model_dump(mode="json"),
        "children_ages": observation.children_ages,
        "currency": observation.currency,
    }
    graph_id = _observation_graph_id(V8ObservationKind.HOTEL_PRICE, identity)
    properties = _without_none(
        {
            "source_observation_id": observation.observation_id,
            "run_id": observation.run_id,
            "place_id": observation.hotel_id,
            "offer_key": observation.offer_key,
            "source_record_id": observation.source_record_id,
            "source_id": observation.source_id,
            "mapping_id": observation.mapping_id,
            "external_id": observation.external_id,
            "seller": observation.seller,
            "room_type": observation.room_type,
            "check_in": observation.check_in.isoformat(),
            "check_out": observation.check_out.isoformat(),
            "nights": (observation.check_out - observation.check_in).days,
            "adults": observation.occupancy.adults,
            "children": observation.occupancy.children,
            "rooms": observation.occupancy.rooms,
            "children_ages": observation.children_ages,
            "currency": observation.currency,
            "amount": _neo4j_number(observation.amount),
            "nightly_amount": _neo4j_number(observation.nightly_amount),
            "total_amount": _neo4j_number(observation.total_amount),
            "tax_amount": _neo4j_number(observation.tax_amount),
            "fee_amount": _neo4j_number(observation.fee_amount),
            "min_amount": _neo4j_number(observation.min_amount),
            "max_amount": _neo4j_number(observation.max_amount),
            "tax_included": observation.tax_included,
            "meal_plan": observation.meal_plan,
            "cancellation_policy": observation.cancellation_policy,
            "refundable": observation.refundable,
            "booking_url": _url(observation.booking_url),
            "availability": observation.availability.value,
            "raw_text": observation.raw_text,
            "observed_at": _iso_datetime(observation.observed_at),
            "verification_status": observation.verification_status.value,
            "decision_id": snapshot.decision_id,
            "snapshot_updated_at": _iso_datetime(snapshot.updated_at),
            "stale_after": _iso_datetime(snapshot.stale_after),
        }
    )
    return _new_observation_row(
        graph_id=graph_id,
        kind=V8ObservationKind.HOTEL_PRICE,
        place_id=observation.hotel_id,
        source_observation_id=observation.observation_id,
        observed_at=observation.observed_at,
        properties=properties,
    )


def _availability_row(
    snapshot: CurrentHotelAvailabilitySnapshot,
    canonical_types: dict[str, set[str]],
    price_rows_by_source_id: dict[str, V8ObservationRow],
) -> V8ObservationRow:
    observation = snapshot.observation
    _require_matching_snapshot_ids(
        snapshot.hotel_id,
        snapshot.observation_id,
        observation.hotel_id,
        observation.observation_id,
        family="hotel_availability",
    )
    _require_canonical_type(observation.hotel_id, "hotel", canonical_types)
    _require_publishable(observation.verification_status, "hotel_availability")

    referenced_graph_ids: list[str] = []
    for source_id in observation.price_observation_ids:
        price_row = price_rows_by_source_id.get(source_id)
        if price_row is None:
            raise V8ObservationInputError(
                f"availability {observation.observation_id} references missing "
                f"current price {source_id}"
            )
        _require_same_stay_context(observation, price_row)
        referenced_graph_ids.append(price_row.graph_id)
    if (
        observation.status is HotelAvailabilityStatus.AVAILABLE
        and not referenced_graph_ids
    ):
        raise V8ObservationInputError(
            "available hotel availability must reference a current price"
        )

    identity = {
        "source_observation_id": observation.observation_id,
        "place_id": observation.hotel_id,
        "observed_at": _iso_datetime(observation.observed_at),
        "check_in": observation.check_in.isoformat(),
        "check_out": observation.check_out.isoformat(),
        "occupancy": observation.occupancy.model_dump(mode="json"),
        "children_ages": observation.children_ages,
        "currency": observation.currency,
    }
    graph_id = _observation_graph_id(V8ObservationKind.HOTEL_AVAILABILITY, identity)
    properties = _without_none(
        {
            "source_observation_id": observation.observation_id,
            "run_id": observation.run_id,
            "place_id": observation.hotel_id,
            "source_record_id": observation.source_record_id,
            "source_id": observation.source_id,
            "source_url": _url(observation.source_url),
            "mapping_id": observation.mapping_id,
            "external_id": observation.external_id,
            "requested_check_in": observation.requested_check_in.isoformat(),
            "fallback_offset_days": observation.fallback_offset_days,
            "check_in": observation.check_in.isoformat(),
            "check_out": observation.check_out.isoformat(),
            "nights": observation.nights,
            "adults": observation.occupancy.adults,
            "children": observation.occupancy.children,
            "rooms": observation.occupancy.rooms,
            "children_ages": observation.children_ages,
            "currency": observation.currency,
            "status": observation.status.value,
            "reason": observation.reason.value,
            "offer_count": observation.offer_count,
            "source_price_observation_ids": observation.price_observation_ids,
            "raw_status_text": observation.raw_status_text,
            "observed_at": _iso_datetime(observation.observed_at),
            "verification_status": observation.verification_status.value,
            "snapshot_updated_at": _iso_datetime(snapshot.updated_at),
            "stale_after": _iso_datetime(snapshot.stale_after),
        }
    )
    return _new_observation_row(
        graph_id=graph_id,
        kind=V8ObservationKind.HOTEL_AVAILABILITY,
        place_id=observation.hotel_id,
        source_observation_id=observation.observation_id,
        observed_at=observation.observed_at,
        properties=properties,
        referenced_observation_ids=sorted(referenced_graph_ids),
    )


def _opening_row(
    snapshot: CurrentPlaceSnapshot,
    canonical_types: dict[str, set[str]],
    *,
    approval: OpeningStatusReviewApproval | None = None,
) -> V8ObservationRow:
    observation = snapshot.opening
    if observation is None:  # pragma: no cover - guarded by caller
        raise V8ObservationInputError("opening observation is missing")
    if snapshot.place_id != observation.place_id:
        raise V8ObservationInputError(
            f"canonical place {snapshot.place_id} contains opening for "
            f"{observation.place_id}"
        )
    if snapshot.provenance.source_record_id not in observation.source_record_ids:
        raise V8ObservationInputError(
            f"opening {observation.observation_id} does not reference its "
            "canonical place source record"
        )
    _require_canonical_type(
        snapshot.place_id,
        snapshot.entity_type.value,
        canonical_types,
    )
    effective_verification_status = (
        approval.approved_verification_status
        if approval is not None
        else observation.verification_status
    )
    _require_publishable(effective_verification_status, "opening_status")
    identity = {
        "source_observation_id": observation.observation_id,
        "place_id": observation.place_id,
        "observed_at": _iso_datetime(observation.observed_at),
        "local_date": observation.local_date.isoformat(),
        "timezone": observation.timezone,
    }
    if approval is not None:
        # A later human approval is a new immutable review event even when it
        # refers to the same source observation.  Keep auto-verified graph IDs
        # stable while preventing approved revisions from content-conflicting.
        identity["human_approval_id"] = approval.approval_id
    graph_id = _observation_graph_id(V8ObservationKind.OPENING_STATUS, identity)
    interval_payload = [
        item.model_dump(mode="json") for item in observation.opening_intervals
    ]
    properties = _without_none(
        {
            "source_observation_id": observation.observation_id,
            "run_id": observation.run_id,
            "place_id": observation.place_id,
            "source_record_ids": observation.source_record_ids,
            "source_id": snapshot.provenance.source_id,
            "mapping_id": snapshot.provenance.mapping_id,
            "decision_id": snapshot.provenance.decision_id,
            "local_date": observation.local_date.isoformat(),
            "timezone": observation.timezone,
            "status": observation.status.value,
            "opening_intervals_json": json.dumps(
                interval_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "is_24_hours": observation.is_24_hours,
            "special_hours": observation.special_hours,
            "open_now": observation.open_now,
            "raw_status_text": observation.raw_status_text,
            "next_open_at": _optional_datetime(observation.next_open_at),
            "next_close_at": _optional_datetime(observation.next_close_at),
            "observed_at": _iso_datetime(observation.observed_at),
            "verification_status": effective_verification_status.value,
            "source_verification_status": (
                observation.verification_status.value
                if approval is not None
                else None
            ),
            "human_approval_id": approval.approval_id if approval else None,
            "human_approval_hash": approval.approval_hash if approval else None,
            "human_approval_reviewer": approval.reviewer if approval else None,
            "human_approval_approved_at": (
                _iso_datetime(approval.approved_at) if approval else None
            ),
            "human_approval_reason": approval.reason if approval else None,
            "snapshot_updated_at": _iso_datetime(snapshot.updated_at),
            "stale_after": _optional_datetime(snapshot.stale_after),
        }
    )
    return _new_observation_row(
        graph_id=graph_id,
        kind=V8ObservationKind.OPENING_STATUS,
        place_id=observation.place_id,
        source_observation_id=observation.observation_id,
        observed_at=observation.observed_at,
        properties=properties,
    )


def _menu_rows(
    root: str | Path,
    canonical_types: dict[str, set[str]],
) -> tuple[
    list[V8ObservationRow],
    list[V8MenuItemRow],
    list[V8ObservationInputArtifact],
]:
    directory = Path(root)
    if not directory.exists():
        return [], [], []
    if not directory.is_dir():
        raise V8ObservationInputError(
            f"current_menu root is not a directory: {directory}"
        )
    menu_paths = sorted(
        (path for path in directory.glob("*.json") if not path.name.startswith(".")),
        key=lambda path: path.name,
    )
    rows: list[V8ObservationRow] = []
    items: list[V8MenuItemRow] = []
    artifacts: list[V8ObservationInputArtifact] = []
    for menu_path in menu_paths:
        audit_path = directory / "_audit" / menu_path.name
        if not audit_path.is_file():
            raise V8ObservationInputError(
                f"current menu has no human-approval metadata: {menu_path}"
            )
        menu_content = menu_path.read_bytes()
        audit_content = audit_path.read_bytes()
        try:
            menu = NormalizedMenu.model_validate_json(menu_content)
            metadata = CurrentMenuMetadata.model_validate_json(audit_content)
        except (TypeError, ValueError) as error:
            raise V8ObservationInputError(
                f"invalid current menu or approval metadata for {menu_path}: {error}"
            ) from error
        if metadata.place_id != menu.place_id:
            raise V8ObservationInputError(
                f"menu approval place {metadata.place_id} does not match {menu.place_id}"
            )
        _require_canonical_menu_type(menu.place_id, canonical_types)
        identity = {
            "review_id": metadata.review_id,
            "place_id": menu.place_id,
            "source_image_hash": metadata.source_image_hash,
            "reviewed_at": _iso_datetime(metadata.reviewed_at),
        }
        graph_id = _observation_graph_id(V8ObservationKind.MENU_SNAPSHOT, identity)
        rows.append(
            _new_observation_row(
                graph_id=graph_id,
                kind=V8ObservationKind.MENU_SNAPSHOT,
                place_id=menu.place_id,
                source_observation_id=metadata.review_id,
                observed_at=metadata.reviewed_at,
                properties={
                    "source_observation_id": metadata.review_id,
                    "place_id": menu.place_id,
                    "review_id": metadata.review_id,
                    "reviewer": metadata.reviewer,
                    "reviewed_at": _iso_datetime(metadata.reviewed_at),
                    "observed_at": _iso_datetime(metadata.reviewed_at),
                    "source_image_hash": metadata.source_image_hash.lower(),
                    "item_count": len(menu.items),
                    "verification_status": VerificationStatus.HUMAN_VERIFIED.value,
                },
            )
        )
        for position, item in enumerate(menu.items):
            payload = item.model_dump(mode="json")
            item_id = "v8menuitem:" + stable_sha256([graph_id, position, payload])[:32]
            items.append(
                _new_menu_item_row(
                    graph_id=item_id,
                    menu_graph_id=graph_id,
                    place_id=menu.place_id,
                    position=position,
                    properties={
                        "place_id": menu.place_id,
                        "name": item.name,
                        "section": item.section,
                        "currency": item.currency,
                        "amount": item.amount,
                        "position": position,
                    },
                )
            )
        for family, path, content in (
            ("current_menu", menu_path, menu_content),
            ("current_menu_approval", audit_path, audit_content),
        ):
            artifacts.append(
                V8ObservationInputArtifact(
                    family=family,
                    relative_path=path.relative_to(directory).as_posix(),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
    return rows, items, artifacts


def _unique_price_rows_by_source_id(
    observations: Iterable[V8ObservationRow],
) -> dict[str, V8ObservationRow]:
    result: dict[str, V8ObservationRow] = {}
    for row in observations:
        if row.kind is not V8ObservationKind.HOTEL_PRICE:
            continue
        current = result.get(row.source_observation_id)
        if current is not None and current.graph_id != row.graph_id:
            raise V8ObservationInputError(
                "one source price observation ID resolves to multiple contexts: "
                f"{row.source_observation_id}"
            )
        result[row.source_observation_id] = row
    return result


def _require_same_stay_context(
    availability: Any,
    price_row: V8ObservationRow,
) -> None:
    props = price_row.properties
    expected = (
        availability.hotel_id,
        availability.check_in.isoformat(),
        availability.check_out.isoformat(),
        availability.occupancy.adults,
        availability.occupancy.children,
        availability.occupancy.rooms,
        availability.children_ages,
        availability.currency,
        availability.source_record_id,
        availability.source_id,
        availability.mapping_id,
        availability.external_id,
    )
    actual = (
        price_row.place_id,
        props.get("check_in"),
        props.get("check_out"),
        props.get("adults"),
        props.get("children"),
        props.get("rooms"),
        props.get("children_ages"),
        props.get("currency"),
        props.get("source_record_id"),
        props.get("source_id"),
        props.get("mapping_id"),
        props.get("external_id"),
    )
    if actual != expected:
        raise V8ObservationInputError(
            f"availability {availability.observation_id} and price "
            f"{price_row.source_observation_id} have different stay contexts"
        )


def _require_matching_snapshot_ids(
    outer_place_id: str,
    outer_observation_id: str,
    inner_place_id: str,
    inner_observation_id: str,
    *,
    family: str,
) -> None:
    if (outer_place_id, outer_observation_id) != (
        inner_place_id,
        inner_observation_id,
    ):
        raise V8ObservationInputError(
            f"{family} snapshot identity does not match its observation"
        )


def _require_canonical_type(
    place_id: str,
    required_type: str,
    canonical_types: dict[str, set[str]],
) -> None:
    place_types = canonical_types.get(place_id)
    if place_types is None:
        raise V8ObservationInputError(
            f"observation refers to non-canonical place: {place_id}"
        )
    if required_type not in place_types:
        raise V8ObservationInputError(
            f"place {place_id} does not have required type {required_type}"
        )


def _require_canonical_menu_type(
    place_id: str,
    canonical_types: dict[str, set[str]],
) -> None:
    place_types = canonical_types.get(place_id)
    if place_types is None:
        raise V8ObservationInputError(f"menu refers to non-canonical place: {place_id}")
    if not place_types & _MENU_PLACE_TYPES:
        raise V8ObservationInputError(
            f"menu place {place_id} is not cafe, restaurant, or nightlife"
        )


def _require_publishable(status: VerificationStatus, family: str) -> None:
    if status not in _PUBLISHABLE_VERIFICATION_STATUSES:
        raise V8ObservationInputError(
            f"{family} observation is not verified: {status.value}"
        )


def _deduplicate_observation_rows(
    rows: Iterable[V8ObservationRow],
) -> list[V8ObservationRow]:
    result: dict[str, V8ObservationRow] = {}
    for row in rows:
        current = result.get(row.graph_id)
        if current is not None and current != row:
            raise V8ObservationInputError(
                f"observation graph ID collision: {row.graph_id}"
            )
        result[row.graph_id] = row
    return list(result.values())


def _observation_graph_id(
    kind: V8ObservationKind,
    identity: dict[str, JsonValue],
) -> str:
    digest = stable_sha256({"kind": kind.value, "identity": identity})
    return f"v8obs:{kind.value}:{digest[:32]}"


def _observation_content_payload(
    row: V8ObservationRow,
) -> dict[str, JsonValue]:
    return {
        "graph_id": row.graph_id,
        "kind": row.kind.value,
        "place_id": row.place_id,
        "source_observation_id": row.source_observation_id,
        "observed_at": _iso_datetime(row.observed_at),
        "properties": row.properties,
        "referenced_observation_ids": row.referenced_observation_ids,
    }


def _menu_item_content_payload(row: V8MenuItemRow) -> dict[str, JsonValue]:
    return {
        "graph_id": row.graph_id,
        "menu_graph_id": row.menu_graph_id,
        "place_id": row.place_id,
        "position": row.position,
        "properties": row.properties,
    }


def _plan_payload(
    *,
    dataset_id: str,
    dataset_hash: str,
    artifacts: list[V8ObservationInputArtifact],
    observations: list[V8ObservationRow],
    menu_items: list[V8MenuItemRow],
    hotel_price_prefilter_gate_sha256: str | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "schema_version": "1.0.0",
        "kb_version": KB_VERSION,
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "input_artifacts": [item.model_dump(mode="json") for item in artifacts],
        "observations": [item.model_dump(mode="json") for item in observations],
        "menu_items": [item.model_dump(mode="json") for item in menu_items],
    }
    if hotel_price_prefilter_gate_sha256 is not None:
        payload["hotel_price_prefilter_gate_sha256"] = (
            hotel_price_prefilter_gate_sha256
        )
    return payload


def _plan_counts(
    observations: Iterable[V8ObservationRow],
    menu_items: Iterable[V8MenuItemRow],
) -> dict[str, int]:
    observation_rows = list(observations)
    item_rows = list(menu_items)
    counts = {
        kind.value: sum(row.kind is kind for row in observation_rows)
        for kind in V8ObservationKind
    }
    counts["menu_items"] = len(item_rows)
    counts["availability_price_relationships"] = sum(
        len(row.referenced_observation_ids)
        for row in observation_rows
        if row.kind is V8ObservationKind.HOTEL_AVAILABILITY
    )
    counts["total_observations"] = len(observation_rows)
    return counts


def _new_publish_manifest(
    plan: V8ObservationPublishPlan,
    status: V8ObservationPublishStatus,
    *,
    created_at: datetime,
    processed_counts: dict[str, int],
    hotel_price_retention: V8HotelPriceRetentionResult,
) -> V8ObservationPublishManifest:
    values: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "dataset_id": plan.dataset_id,
        "dataset_hash": plan.dataset_hash,
        "status": status,
        "created_at": _aware_utc(created_at),
        "counts": plan.counts,
        "processed_counts": processed_counts,
        "hotel_price_retention": hotel_price_retention,
    }
    draft = V8ObservationPublishManifest.model_construct(
        manifest_id="pending",
        manifest_hash="0" * 64,
        **values,
    )
    manifest_hash = stable_sha256(_manifest_payload(draft))
    return V8ObservationPublishManifest(
        manifest_id=f"v8-observation-publish-{manifest_hash[:20]}",
        manifest_hash=manifest_hash,
        **values,
    )


def _manifest_payload(
    manifest: V8ObservationPublishManifest,
) -> dict[str, JsonValue]:
    return {
        "schema_version": manifest.schema_version,
        "kb_version": manifest.kb_version,
        "plan_id": manifest.plan_id,
        "plan_hash": manifest.plan_hash,
        "dataset_id": manifest.dataset_id,
        "dataset_hash": manifest.dataset_hash,
        "status": manifest.status.value,
        "created_at": _iso_datetime(manifest.created_at),
        "counts": manifest.counts,
        "processed_counts": manifest.processed_counts,
        "hotel_price_retention": manifest.hotel_price_retention.model_dump(
            mode="json"
        ),
    }


def _write_manifest(
    destination: str | Path | None,
    manifest: V8ObservationPublishManifest,
) -> None:
    if destination is None:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = manifest.model_dump_json(indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _processed_count(rows: list[dict[str, Any]]) -> int:
    if len(rows) != 1 or "processed" not in rows[0]:
        raise V8ObservationPublishError(
            "Neo4j publish query did not return a processed count"
        )
    return int(rows[0]["processed"])


def _deleted_count(rows: list[dict[str, Any]], label: str) -> int:
    if len(rows) != 1 or "deleted" not in rows[0]:
        raise V8ObservationPublishError(
            f"{label} query did not return a deleted count"
        )
    count = int(rows[0]["deleted"])
    if count < 0:
        raise V8ObservationPublishError(
            f"{label} query returned a negative deleted count"
        )
    return count


def _parse_graph_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif hasattr(value, "to_native"):
        parsed = value.to_native()
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise V8ObservationPublishError(
                f"invalid Neo4j observation timestamp: {value}"
            ) from error
    else:
        raise V8ObservationPublishError(
            "Neo4j observation timestamp has an unsupported type"
        )
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise V8ObservationPublishError(
            "Neo4j observation timestamp must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _hotel_price_retention_cutoff(
    anchor: datetime,
    previous_calendar_days: int,
) -> datetime:
    if previous_calendar_days < 0:
        raise ValueError("previous_calendar_days cannot be negative")
    if anchor.tzinfo is None or anchor.utcoffset() is None:
        raise ValueError("hotel price retention anchor must be timezone-aware")
    anchor_local = anchor.astimezone(_HOTEL_PRICE_RETENTION_TZINFO)
    cutoff_local_date = anchor_local.date() - timedelta(
        days=previous_calendar_days
    )
    return datetime.combine(
        cutoff_local_date,
        time.min,
        tzinfo=_HOTEL_PRICE_RETENTION_TZINFO,
    ).astimezone(timezone.utc)


def _result_rows(result: Any) -> list[dict[str, Any]]:
    if hasattr(result, "data"):
        return [dict(row) for row in result.data()]
    return [
        record.data() if hasattr(record, "data") else dict(record) for record in result
    ]


def _raise_immutable_mismatches(
    rows: list[dict[str, Any]],
    label: str,
) -> None:
    if len(rows) != 1:
        raise V8ObservationPublishError(
            f"publish {label} expected one result row, received {len(rows)}"
        )
    mismatched = sorted(set(rows[0].get("mismatched_ids", [])))
    if mismatched:
        raise V8ObservationPublishError(
            f"immutable {label} content conflict: {', '.join(mismatched[:5])}"
        )


def _without_none(values: dict[str, JsonValue | None]) -> dict[str, JsonValue]:
    return {key: value for key, value in values.items() if value is not None}


def _neo4j_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _url(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise V8ObservationInputError("timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return _iso_datetime(value) if value is not None else None


_SCHEMA_QUERIES = (
    "CREATE CONSTRAINT observation_id IF NOT EXISTS "
    "FOR (node:Observation) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT menu_item_id IF NOT EXISTS "
    "FOR (node:MenuItem) REQUIRE node.id IS UNIQUE",
    "CREATE RANGE INDEX observation_place IF NOT EXISTS "
    "FOR (node:Observation) ON (node.place_id)",
    "CREATE RANGE INDEX observation_observed_at IF NOT EXISTS "
    "FOR (node:Observation) ON (node.observed_at)",
)


def ensure_v8_observation_schema(store: V8ObservationStore) -> None:
    """Create the neutral observation constraints and indexes if needed."""

    for query in _SCHEMA_QUERIES:
        store.run(query)


_ACTIVE_RELEASE_QUERY = """
// v8-observation:validate-active-release
MATCH (release:DatasetRelease {
  dataset_id: $dataset_id,
  dataset_hash: $dataset_hash,
  kb_version: $kb_version,
  status: 'active'
})
RETURN count(release) AS matches
"""

_PLACE_VALIDATION_QUERY = """
// v8-observation:validate-places
UNWIND $place_ids AS place_id
OPTIONAL MATCH (place:Place {id: place_id, kb_version: $kb_version})
RETURN place_id, count(place) AS matches
ORDER BY place_id
"""


def _observation_query(label: str, relationship: str) -> str:
    return f"""
// v8-observation:merge-{relationship.casefold().replace("_", "-")}
UNWIND $rows AS row
MATCH (place:Place {{id: row.place_id, kb_version: $kb_version}})
MERGE (observation:Observation:{label} {{id: row.graph_id}})
ON CREATE SET observation += row.properties,
              observation.content_hash = row.content_hash,
              observation.kb_version = $kb_version,
              observation.canonical_dataset_id = $dataset_id,
              observation.canonical_dataset_hash = $dataset_hash,
              observation.ingested_at = $ingested_at
WITH place, observation, row,
     observation.content_hash = row.content_hash AS content_matches
FOREACH (
  ignored IN CASE WHEN content_matches THEN [1] ELSE [] END |
  MERGE (place)-[:{relationship}]->(observation)
)
RETURN count(CASE WHEN content_matches THEN observation END) AS processed,
       [id IN collect(
         CASE WHEN content_matches THEN NULL ELSE row.graph_id END
       ) WHERE id IS NOT NULL] AS mismatched_ids
"""


_OBSERVATION_QUERIES = {
    V8ObservationKind.HOTEL_PRICE: _observation_query(
        "HotelPriceObservation",
        "HAS_PRICE",
    ),
    V8ObservationKind.HOTEL_AVAILABILITY: _observation_query(
        "HotelAvailabilityObservation",
        "HAS_AVAILABILITY",
    ),
    V8ObservationKind.OPENING_STATUS: _observation_query(
        "OpeningStatusObservation",
        "HAS_OPENING_STATUS",
    ),
    V8ObservationKind.MENU_SNAPSHOT: _observation_query(
        "MenuSnapshot",
        "HAS_MENU",
    ),
}

_AVAILABILITY_PRICE_QUERY = """
// v8-observation:merge-availability-price
UNWIND $rows AS row
MATCH (availability:HotelAvailabilityObservation {
  id: row.availability_graph_id,
  kb_version: $kb_version
})
UNWIND row.price_graph_ids AS price_graph_id
MATCH (price:HotelPriceObservation {
  id: price_graph_id,
  kb_version: $kb_version
})
MERGE (availability)-[:REFERENCES_PRICE]->(price)
RETURN count(price) AS processed
"""

_LATEST_HOTEL_PRICE_OBSERVED_AT_QUERY = """
// v8-observation:latest-hotel-price-observed-at
MATCH (price:HotelPriceObservation {kb_version: $kb_version})
WHERE price.observed_at IS NOT NULL
WITH max(datetime(price.observed_at)) AS latest_observed_at
RETURN CASE
  WHEN latest_observed_at IS NULL THEN NULL
  ELSE toString(latest_observed_at)
END AS latest_observed_at
"""

_PRUNE_HOTEL_AVAILABILITY_QUERY = """
// v8-observation:prune-hotel-availability
MATCH (availability:HotelAvailabilityObservation {kb_version: $kb_version})
WHERE (
  availability.observed_at IS NOT NULL
  AND datetime(availability.observed_at) < datetime($cutoff_observed_at)
)
OR EXISTS {
  MATCH (availability)-[:REFERENCES_PRICE]->(
    price:HotelPriceObservation {kb_version: $kb_version}
  )
  WHERE price.observed_at IS NOT NULL
    AND datetime(price.observed_at) < datetime($cutoff_observed_at)
}
WITH availability
DETACH DELETE availability
RETURN count(*) AS deleted
"""

_PRUNE_HOTEL_PRICE_QUERY = """
// v8-observation:prune-hotel-price
MATCH (price:HotelPriceObservation {kb_version: $kb_version})
WHERE price.observed_at IS NOT NULL
  AND datetime(price.observed_at) < datetime($cutoff_observed_at)
WITH price
DETACH DELETE price
RETURN count(*) AS deleted
"""

_MENU_ITEM_QUERY = """
// v8-observation:merge-menu-items
UNWIND $rows AS row
MATCH (menu:MenuSnapshot {id: row.menu_graph_id})
MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
MERGE (item:MenuItem {id: row.graph_id})
ON CREATE SET item += row.properties,
              item.content_hash = row.content_hash,
              item.kb_version = $kb_version,
              item.canonical_dataset_id = $dataset_id,
              item.canonical_dataset_hash = $dataset_hash
WITH menu, place, item, row,
     item.content_hash = row.content_hash AS content_matches
FOREACH (
  ignored IN CASE WHEN content_matches THEN [1] ELSE [] END |
  MERGE (menu)-[contains:HAS_ITEM]->(item)
  ON CREATE SET contains.position = row.position
  MERGE (item)-[:MENU_ITEM_OF]->(place)
)
RETURN count(CASE WHEN content_matches THEN item END) AS processed,
       [id IN collect(
         CASE WHEN content_matches THEN NULL ELSE row.graph_id END
       ) WHERE id IS NOT NULL] AS mismatched_ids
"""


__all__ = [
    "V8HotelPriceCleanupGate",
    "V8HotelPriceRetentionResult",
    "V8ObservationInputError",
    "V8ObservationKind",
    "V8ObservationPublishError",
    "V8ObservationPublishManifest",
    "V8ObservationPublishPlan",
    "V8ObservationPublishStatus",
    "V8ObservationPublisher",
    "build_v8_observation_plan",
    "build_v8_observation_plan_for_dataset",
    "ensure_v8_observation_schema",
    "load_latest_hotel_price_cleanup_gate",
    "read_v8_observation_plan",
    "write_v8_observation_plan",
]
