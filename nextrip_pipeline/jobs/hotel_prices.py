from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from uuid import uuid4

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import AgodaPriceAdapter, AgodaPriceRequest
from nextrip_pipeline.schemas import ExternalEntityMapping, MappingStatus

from .common import JobFailure, JobRunResult


class HotelPriceJob:
    """Five-hour Agoda raw capture job; normalization is intentionally separate."""

    schedule_interval_minutes = 300

    def __init__(
        self,
        adapter: AgodaPriceAdapter,
        writer: RawJsonWriter,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.writer = writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        mappings: Sequence[ExternalEntityMapping],
        request: AgodaPriceRequest,
        *,
        run_id: str | None = None,
    ) -> JobRunResult:
        effective_run_id = run_id or self._new_run_id("hotel-price")
        result = JobRunResult(run_id=effective_run_id)
        eligible_mappings = [
            mapping
            for mapping in mappings
            if mapping.status in {MappingStatus.CONFIRMED, MappingStatus.AUTO_MATCHED}
        ]

        batch_size = self.adapter.MAX_PROPERTIES_PER_REQUEST
        for offset in range(0, len(eligible_mappings), batch_size):
            batch = eligible_mappings[offset : offset + batch_size]
            try:
                record = self.adapter.fetch(
                    batch,
                    request,
                    run_id=effective_run_id,
                )
                result.written_paths.append(self.writer.write(record))
            except Exception as error:
                result.failures.append(
                    JobFailure(
                        entity_ids=tuple(mapping.entity_id for mapping in batch),
                        error=f"{type(error).__name__}: {error}",
                    )
                )
        return result

    def _new_run_id(self, prefix: str) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}-{timestamp}-{uuid4().hex[:8]}"
