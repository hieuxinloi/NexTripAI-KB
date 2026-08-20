from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import TrivagoPriceRequest
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.preprocessing import (
    NormalizedHotelPriceWriter,
    TrivagoMcpPriceNormalizer,
)
from nextrip_pipeline.decision_gate import (
    HotelPriceDecisionGate,
    HotelPriceDecisionStatus,
    HotelPriceDecisionWriter,
)
from nextrip_pipeline.publishing import CurrentHotelPriceWriter
from nextrip_pipeline.quality.trivago_mapping import (
    CurrentTrivagoMappingWriter,
    TrivagoDiscoveryAuditWriter,
    TrivagoDiscoveryResolver,
    TrivagoDiscoveryStatus,
    apply_trivago_resolution,
)
from nextrip_pipeline.schemas import NexTripModel, Occupancy, SourceRecord
from nextrip_pipeline.validators import (
    HotelPriceValidatorOrchestrator,
    ValidationResultWriter,
)


class TrivagoDiscoveryCapture(Protocol):
    def search(
        self,
        target: TrivagoHotelRegistryEntry,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
    ) -> SourceRecord: ...


class TrivagoPriceBatchContext(NexTripModel):
    """The immutable stay/occupancy context shared by one price batch."""

    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_context(self) -> TrivagoPriceBatchContext:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        return self

    def request_for(self, target: TrivagoHotelRegistryEntry) -> TrivagoPriceRequest:
        return TrivagoPriceRequest(
            hotel_name=target.search_name,
            destination=target.city,
            check_in=self.check_in,
            check_out=self.check_out,
            occupancy=self.occupancy,
            children_ages=self.children_ages,
            currency=self.currency,
        )


class TrivagoBatchItemStatus(StrEnum):
    SUCCEEDED = "succeeded"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class TrivagoBatchItem(NexTripModel):
    entity_id: str = Field(min_length=1)
    search_query: str = Field(min_length=1)
    status: TrivagoBatchItemStatus
    resolution_status: TrivagoDiscoveryStatus | None = None
    external_id: str | None = None
    normalized_observation_count: int = Field(default=0, ge=0)
    validation_count: int = Field(default=0, ge=0)
    decision_counts: dict[str, int] = Field(default_factory=dict)
    published_current_count: int = Field(default=0, ge=0)
    artifact_paths: list[str] = Field(default_factory=list)
    error_stage: str | None = None
    error: str | None = None


class TrivagoBatchSummary(NexTripModel):
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    request_context: TrivagoPriceBatchContext
    registry_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    normalized_observation_count: int = Field(ge=0)
    validation_count: int = Field(default=0, ge=0)
    decision_counts: dict[str, int] = Field(default_factory=dict)
    published_current_count: int = Field(default=0, ge=0)
    resolution_counts: dict[str, int]
    items: list[TrivagoBatchItem] = Field(default_factory=list)


class TrivagoMcpBatchRunner:
    """Run a bounded, failure-isolated MCP discovery and price batch.

    Quality writers are optional for discovery-only use. When supplied as a
    complete set, every normalized offer is validated, decided, and a PASS is
    materialized to the local contextual current-price store. This runner never
    writes to Neo4j.
    """

    def __init__(
        self,
        adapter: TrivagoDiscoveryCapture,
        raw_writer: RawJsonWriter,
        normalized_writer: NormalizedHotelPriceWriter,
        audit_writer: TrivagoDiscoveryAuditWriter,
        mapping_writer: CurrentTrivagoMappingWriter,
        summary_writer: TrivagoBatchSummaryWriter,
        *,
        validation_writer: ValidationResultWriter | None = None,
        decision_writer: HotelPriceDecisionWriter | None = None,
        current_writer: CurrentHotelPriceWriter | None = None,
        resolver: TrivagoDiscoveryResolver | None = None,
        normalizer: TrivagoMcpPriceNormalizer | None = None,
        validator: HotelPriceValidatorOrchestrator | None = None,
        decision_gate: HotelPriceDecisionGate | None = None,
        max_requests: int | None = None,
        offset: int = 0,
        entity_ids: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_requests is not None and max_requests < 1:
            raise ValueError("max_requests must be positive or None")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        quality_writers = (validation_writer, decision_writer, current_writer)
        if any(writer is not None for writer in quality_writers) and not all(
            writer is not None for writer in quality_writers
        ):
            raise ValueError(
                "validation, decision, and current writers must be configured together"
            )
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.normalized_writer = normalized_writer
        self.audit_writer = audit_writer
        self.mapping_writer = mapping_writer
        self.summary_writer = summary_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.current_writer = current_writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resolver = resolver or TrivagoDiscoveryResolver(clock=self.clock)
        self.normalizer = normalizer or TrivagoMcpPriceNormalizer()
        self.validator = validator or HotelPriceValidatorOrchestrator(clock=self.clock)
        self.decision_gate = decision_gate or HotelPriceDecisionGate(clock=self.clock)
        self.max_requests = max_requests
        self.offset = offset
        self.entity_ids = frozenset(entity_ids)

    def run(
        self,
        registry: TrivagoHotelRegistry,
        request_context: TrivagoPriceBatchContext,
        *,
        run_id: str,
    ) -> tuple[TrivagoBatchSummary, Path]:
        started_at = self.clock()
        eligible = sorted(
            (
                entry
                for entry in registry.entries
                if entry.status is not TrivagoRegistryStatus.REJECTED
                and (not self.entity_ids or entry.entity_id in self.entity_ids)
            ),
            key=lambda entry: entry.entity_id,
        )
        end = None if self.max_requests is None else self.offset + self.max_requests
        selected = eligible[self.offset : end]
        items = [
            self._process(entry, request_context, run_id=run_id) for entry in selected
        ]
        status_counts = Counter(item.status.value for item in items)
        resolution_counts = Counter(
            item.resolution_status.value
            for item in items
            if item.resolution_status is not None
        )
        decision_counts: Counter[str] = Counter()
        for item in items:
            decision_counts.update(item.decision_counts)
        summary = TrivagoBatchSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=self.clock(),
            request_context=request_context,
            registry_count=len(registry.entries),
            eligible_count=len(eligible),
            selected_count=len(selected),
            succeeded_count=status_counts[TrivagoBatchItemStatus.SUCCEEDED.value],
            unresolved_count=status_counts[TrivagoBatchItemStatus.UNRESOLVED.value],
            failed_count=status_counts[TrivagoBatchItemStatus.FAILED.value],
            normalized_observation_count=sum(
                item.normalized_observation_count for item in items
            ),
            validation_count=sum(item.validation_count for item in items),
            decision_counts=dict(sorted(decision_counts.items())),
            published_current_count=sum(item.published_current_count for item in items),
            resolution_counts=dict(sorted(resolution_counts.items())),
            items=items,
        )
        return summary, self.summary_writer.write(summary)

    def _process(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        run_id: str,
    ) -> TrivagoBatchItem:
        artifacts: list[str] = []
        resolution_status = None
        external_id = entry.external_id
        stage = "capture"
        try:
            request = context.request_for(entry)
            record = self.adapter.search(entry, request, run_id=run_id)
            stage = "raw_write"
            artifacts.append(str(self.raw_writer.write(record)))
            stage = "resolve"
            resolution = self.resolver.resolve(entry, record)
            resolution_status = resolution.status
            stage = "audit_write"
            artifacts.append(str(self.audit_writer.write(resolution, run_id=run_id)))
            resolved_entry = apply_trivago_resolution(entry, resolution)
            if resolution.status is not TrivagoDiscoveryStatus.CONFIRMED:
                return TrivagoBatchItem(
                    entity_id=entry.entity_id,
                    search_query=entry.search_query,
                    status=TrivagoBatchItemStatus.UNRESOLVED,
                    resolution_status=resolution.status,
                    external_id=resolution.selected_external_id,
                    artifact_paths=artifacts,
                )

            mapping = resolved_entry.to_mapping()
            external_id = mapping.external_id
            stage = "mapping_write"
            artifacts.append(str(self.mapping_writer.publish(mapping)))
            stage = "normalize"
            observations = self.normalizer.normalize(record, mapping)
            stage = "normalized_write"
            validation_count = 0
            decision_counts: Counter[str] = Counter()
            published_current_count = 0
            for observation in observations:
                artifacts.append(str(self.normalized_writer.write(observation)))
                if self.validation_writer is None:
                    continue
                assert self.decision_writer is not None
                assert self.current_writer is not None
                stage = "validate"
                validations = self.validator.validate(observation, mapping)
                stage = "validation_write"
                for validation in validations:
                    artifacts.append(str(self.validation_writer.write(validation)))
                    validation_count += 1
                stage = "decide"
                decision = self.decision_gate.decide(observation, validations)
                decision_counts[decision.status.value] += 1
                stage = "decision_write"
                artifacts.append(str(self.decision_writer.write(decision)))
                if decision.status is HotelPriceDecisionStatus.PASS:
                    stage = "current_write"
                    artifacts.append(
                        str(self.current_writer.publish(observation, decision))
                    )
                    published_current_count += 1
            return TrivagoBatchItem(
                entity_id=entry.entity_id,
                search_query=entry.search_query,
                status=TrivagoBatchItemStatus.SUCCEEDED,
                resolution_status=resolution.status,
                external_id=mapping.external_id,
                normalized_observation_count=len(observations),
                validation_count=validation_count,
                decision_counts=dict(sorted(decision_counts.items())),
                published_current_count=published_current_count,
                artifact_paths=artifacts,
            )
        except Exception as error:
            return TrivagoBatchItem(
                entity_id=entry.entity_id,
                search_query=entry.search_query,
                status=TrivagoBatchItemStatus.FAILED,
                resolution_status=resolution_status,
                external_id=external_id,
                artifact_paths=artifacts,
                error_stage=stage,
                error=f"{type(error).__name__}: {error}",
            )


class TrivagoBatchSummaryWriter:
    """Write one immutable batch summary containing the request context."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, summary: TrivagoBatchSummary) -> Path:
        destination = (
            self.root_directory / f"run={quote(summary.run_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Trivago batch summary already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(summary.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
