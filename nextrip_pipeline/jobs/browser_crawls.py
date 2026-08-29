from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import (
    GoogleMapsPlaceAdapter,
    TrivagoPriceRequest,
)
from nextrip_pipeline.schemas import ExternalEntityMapping, MappingStatus, SourceRecord

from .common import JobFailure, JobRunResult


class HotelPriceCaptureAdapter(Protocol):
    def fetch(
        self,
        mapping: ExternalEntityMapping,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
    ) -> SourceRecord: ...


class TrivagoHotelPriceJob:
    schedule_interval_minutes = 300

    def __init__(
        self,
        adapter: HotelPriceCaptureAdapter,
        writer: RawJsonWriter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.writer = writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        requests: Sequence[tuple[ExternalEntityMapping, TrivagoPriceRequest]],
        *,
        run_id: str | None = None,
    ) -> JobRunResult:
        effective_run_id = run_id or self._new_run_id("hotel-price")
        result = JobRunResult(run_id=effective_run_id)
        for mapping, request in requests:
            if mapping.status not in {
                MappingStatus.CONFIRMED,
                MappingStatus.AUTO_MATCHED,
            }:
                continue
            try:
                record = self.adapter.fetch(mapping, request, run_id=effective_run_id)
                result.written_paths.append(self.writer.write(record))
            except Exception as error:
                result.failures.append(
                    JobFailure(
                        entity_ids=(mapping.entity_id,),
                        error=f"{type(error).__name__}: {error}",
                    )
                )
        return result

    def _new_run_id(self, prefix: str) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}-{timestamp}-{uuid4().hex[:8]}"


class GoogleMapsOpeningJob:
    schedule_interval_minutes = 1440

    def __init__(
        self,
        adapter: GoogleMapsPlaceAdapter,
        writer: RawJsonWriter,
        *,
        max_requests_per_run: int = 32,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_requests_per_run < 1:
            raise ValueError("max_requests_per_run must be positive")
        self.adapter = adapter
        self.writer = writer
        self.max_requests_per_run = max_requests_per_run
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        mappings: Sequence[ExternalEntityMapping],
        *,
        run_id: str | None = None,
    ) -> JobRunResult:
        effective_run_id = run_id or self._new_run_id("opening-status")
        result = JobRunResult(run_id=effective_run_id)
        eligible = sorted(
            (
                mapping
                for mapping in mappings
                if mapping.status
                in {MappingStatus.CONFIRMED, MappingStatus.AUTO_MATCHED}
            ),
            key=lambda mapping: mapping.mapping_id,
        )
        for mapping in self._daily_rotation(eligible):
            try:
                record = self.adapter.fetch(mapping, run_id=effective_run_id)
                result.written_paths.append(self.writer.write(record))
            except Exception as error:
                result.failures.append(
                    JobFailure(
                        entity_ids=(mapping.entity_id,),
                        error=f"{type(error).__name__}: {error}",
                    )
                )
        return result

    def _daily_rotation(
        self, mappings: Sequence[ExternalEntityMapping]
    ) -> list[ExternalEntityMapping]:
        if len(mappings) <= self.max_requests_per_run:
            return list(mappings)
        day_number = self.clock().astimezone(timezone.utc).date().toordinal()
        start = (day_number * self.max_requests_per_run) % len(mappings)
        rotated = list(mappings[start:]) + list(mappings[:start])
        return rotated[: self.max_requests_per_run]

    def _new_run_id(self, prefix: str) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}-{timestamp}-{uuid4().hex[:8]}"
