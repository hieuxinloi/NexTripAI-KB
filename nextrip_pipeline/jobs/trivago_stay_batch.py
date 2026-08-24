from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field

from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.schemas import NexTripModel

from .trivago_mcp_batch import TrivagoPriceBatchContext
from .trivago_stay_availability import (
    TrivagoStayAvailabilityResult,
    TrivagoStayAvailabilityResultWriter,
    TrivagoStayStopReason,
)


class TrivagoStayRunner(Protocol):
    def run(
        self,
        entry: TrivagoHotelRegistryEntry,
        requested_context: TrivagoPriceBatchContext,
        *,
        run_id: str,
        lookahead_days: int = 1,
    ) -> TrivagoStayAvailabilityResult: ...


class TrivagoStayBatchItemStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class TrivagoStayBatchItem(NexTripModel):
    hotel_id: str = Field(min_length=1)
    search_query: str = Field(min_length=1)
    status: TrivagoStayBatchItemStatus
    result_run_id: str = Field(min_length=1)
    stop_reason: TrivagoStayStopReason | None = None
    selected_available_offset_days: int | None = Field(default=None, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    availability_counts: dict[str, int] = Field(default_factory=dict)
    result_path: str | None = None
    error_stage: str | None = None
    error: str | None = None


class TrivagoStayBatchSummary(NexTripModel):
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    request_context: TrivagoPriceBatchContext
    lookahead_days: int = Field(ge=0)
    registry_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    stop_reason_counts: dict[str, int] = Field(default_factory=dict)
    availability_counts: dict[str, int] = Field(default_factory=dict)
    items: list[TrivagoStayBatchItem] = Field(default_factory=list)


class TrivagoStayBatchSummaryWriter:
    """Persist one immutable summary for a scheduled availability batch."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, summary: TrivagoStayBatchSummary) -> Path:
        destination = (
            self.root_directory / f"run={quote(summary.run_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Trivago stay batch summary already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(summary.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination


class TrivagoStayAvailabilityBatchRunner:
    """Run contextual stay availability for a bounded registry selection.

    Each hotel gets an independent child run. A capture, parser, or storage
    failure is retained on that item and does not stop subsequent hotels.
    Scheduled refreshes only call hotels with confirmed provider identities.
    Unresolved/review identities require the explicit discovery option, while
    rejected and terminal review outcomes are never sent to the provider.
    """

    def __init__(
        self,
        stay_runner: TrivagoStayRunner,
        result_writer: TrivagoStayAvailabilityResultWriter,
        summary_writer: TrivagoStayBatchSummaryWriter,
        *,
        max_requests: int | None = None,
        offset: int = 0,
        entity_ids: Sequence[str] = (),
        include_identity_discovery: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be positive or None")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        self.stay_runner = stay_runner
        self.result_writer = result_writer
        self.summary_writer = summary_writer
        self.max_requests = max_requests
        self.offset = offset
        self.entity_ids = frozenset(entity_ids)
        self.include_identity_discovery = include_identity_discovery
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        registry: TrivagoHotelRegistry,
        request_context: TrivagoPriceBatchContext,
        *,
        run_id: str,
        lookahead_days: int = 1,
    ) -> tuple[TrivagoStayBatchSummary, Path]:
        if not 0 <= lookahead_days <= 14:
            raise ValueError("lookahead_days must be between 0 and 14")

        started_at = self.clock()
        eligible_statuses = {TrivagoRegistryStatus.CONFIRMED}
        if self.include_identity_discovery:
            eligible_statuses.update(
                {
                    TrivagoRegistryStatus.UNRESOLVED,
                    TrivagoRegistryStatus.REVIEW,
                }
            )
        eligible = sorted(
            (
                entry
                for entry in registry.entries
                if entry.status in eligible_statuses
                and (not self.entity_ids or entry.entity_id in self.entity_ids)
            ),
            key=lambda entry: entry.entity_id,
        )
        end = None if self.max_requests is None else self.offset + self.max_requests
        selected = eligible[self.offset : end]
        items = [
            self._process(
                entry,
                request_context,
                batch_run_id=run_id,
                lookahead_days=lookahead_days,
            )
            for entry in selected
        ]

        item_statuses = Counter(item.status.value for item in items)
        stop_reasons = Counter(
            item.stop_reason.value for item in items if item.stop_reason is not None
        )
        availability: Counter[str] = Counter()
        for item in items:
            availability.update(item.availability_counts)

        summary = TrivagoStayBatchSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=self.clock(),
            request_context=request_context,
            lookahead_days=lookahead_days,
            registry_count=len(registry.entries),
            eligible_count=len(eligible),
            selected_count=len(selected),
            completed_count=item_statuses[TrivagoStayBatchItemStatus.COMPLETED.value],
            failed_count=item_statuses[TrivagoStayBatchItemStatus.FAILED.value],
            stop_reason_counts=dict(sorted(stop_reasons.items())),
            availability_counts=dict(sorted(availability.items())),
            items=items,
        )
        return summary, self.summary_writer.write(summary)

    def _process(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        batch_run_id: str,
        lookahead_days: int,
    ) -> TrivagoStayBatchItem:
        result_run_id = f"{batch_run_id}-hotel-{entry.entity_id}"
        stage = "stay_availability"
        try:
            result = self.stay_runner.run(
                entry,
                context,
                run_id=result_run_id,
                lookahead_days=lookahead_days,
            )
            stage = "result_write"
            result_path = self.result_writer.write(result)
            availability_counts = Counter(
                attempt.availability.status.value for attempt in result.attempts
            )
            return TrivagoStayBatchItem(
                hotel_id=entry.entity_id,
                search_query=entry.search_query,
                status=TrivagoStayBatchItemStatus.COMPLETED,
                result_run_id=result_run_id,
                stop_reason=result.stop_reason,
                selected_available_offset_days=(result.selected_available_offset_days),
                attempt_count=len(result.attempts),
                availability_counts=dict(sorted(availability_counts.items())),
                result_path=str(result_path),
            )
        except Exception as error:
            return TrivagoStayBatchItem(
                hotel_id=entry.entity_id,
                search_query=entry.search_query,
                status=TrivagoStayBatchItemStatus.FAILED,
                result_run_id=result_run_id,
                error_stage=stage,
                error=f"{type(error).__name__}: {error}",
            )
