from __future__ import annotations

import copy
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import (
    TrivagoPriceRequest,
    TrivagoSearchStrategy,
)
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.decision_gate import (
    HotelPriceDecisionGate,
    HotelPriceDecisionStatus,
    HotelPriceDecisionWriter,
)
from nextrip_pipeline.preprocessing import (
    HotelPriceNormalizationError,
    InvalidPriceError,
    NormalizedHotelPriceWriter,
    TrivagoMcpPriceNormalizer,
)
from nextrip_pipeline.publishing import (
    AcceptedObservationStore,
    CurrentHotelAvailabilityWriter,
    CurrentHotelPriceWriter,
)
from nextrip_pipeline.quality import (
    CurrentTrivagoMappingWriter,
    TrivagoDiscoveryAuditWriter,
    TrivagoDiscoveryResolution,
    TrivagoDiscoveryResolver,
    TrivagoDiscoveryStatus,
    apply_trivago_resolution,
)
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    NexTripModel,
    SourceRecord,
    VerificationStatus,
)
from nextrip_pipeline.validators import (
    HotelPriceValidatorOrchestrator,
    ValidationResultWriter,
)

from .trivago_mcp_batch import TrivagoPriceBatchContext


class TrivagoStayCapture(Protocol):
    def search(
        self,
        target: TrivagoHotelRegistryEntry,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
        strategy: TrivagoSearchStrategy = TrivagoSearchStrategy.NAME,
        query: str | None = None,
    ) -> SourceRecord: ...


class TrivagoStayStopReason(StrEnum):
    AVAILABLE_FOUND = "available_found"
    LOOKAHEAD_EXHAUSTED = "lookahead_exhausted"
    UNKNOWN_RESULT = "unknown_result"


class TrivagoStayWindowAttempt(NexTripModel):
    attempt_run_id: str = Field(min_length=1)
    fallback_offset_days: int = Field(ge=0)
    availability: HotelAvailabilityObservation
    prices: list[HotelPriceObservation] = Field(default_factory=list)
    resolution_status: TrivagoDiscoveryStatus | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    error_stage: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_attempt(self) -> TrivagoStayWindowAttempt:
        if self.availability.fallback_offset_days != self.fallback_offset_days:
            raise ValueError("attempt and availability fallback offsets must match")
        if self.availability.run_id != self.attempt_run_id:
            raise ValueError("attempt and availability run IDs must match")
        if self.availability.status is HotelAvailabilityStatus.AVAILABLE:
            if [item.observation_id for item in self.prices] != (
                self.availability.price_observation_ids
            ):
                raise ValueError("available attempt must expose its priced offers")
            expected_mapping = (
                self.availability.mapping_id,
                self.availability.external_id,
            )
            if any(
                (item.mapping_id, item.external_id) != expected_mapping
                for item in self.prices
            ):
                raise ValueError(
                    "available prices must use the availability mapping identity"
                )
        elif self.prices:
            raise ValueError("non-available attempt cannot expose priced offers")
        return self


class TrivagoStayAvailabilityResult(NexTripModel):
    run_id: str = Field(min_length=1)
    hotel_id: str = Field(min_length=1)
    requested_context: TrivagoPriceBatchContext
    lookahead_days: int = Field(ge=0)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    stop_reason: TrivagoStayStopReason
    selected_available_offset_days: int | None = Field(default=None, ge=0)
    attempts: list[TrivagoStayWindowAttempt] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> TrivagoStayAvailabilityResult:
        if len(self.attempts) > self.lookahead_days + 1:
            raise ValueError("stay attempts exceed lookahead_days")
        offsets = [item.fallback_offset_days for item in self.attempts]
        if offsets != list(range(len(offsets))):
            raise ValueError("stay attempts must be contiguous from offset zero")
        available_offsets = [
            item.fallback_offset_days
            for item in self.attempts
            if item.availability.status is HotelAvailabilityStatus.AVAILABLE
        ]
        expected = available_offsets[0] if available_offsets else None
        if self.selected_available_offset_days != expected:
            raise ValueError("selected offset must identify the first available stay")
        nights = (
            self.requested_context.check_out - self.requested_context.check_in
        ).days
        for attempt in self.attempts:
            observation = attempt.availability
            expected_check_in = self.requested_context.check_in + timedelta(
                days=attempt.fallback_offset_days
            )
            if (
                observation.hotel_id != self.hotel_id
                or observation.requested_check_in != self.requested_context.check_in
                or observation.check_in != expected_check_in
                or observation.check_out != expected_check_in + timedelta(days=nights)
                or observation.occupancy != self.requested_context.occupancy
                or observation.children_ages != self.requested_context.children_ages
                or observation.currency != self.requested_context.currency
            ):
                raise ValueError("stay attempt does not match requested context")

        expected_stop_reason = (
            TrivagoStayStopReason.AVAILABLE_FOUND
            if available_offsets
            else TrivagoStayStopReason.UNKNOWN_RESULT
            if self.attempts[-1].availability.status is HotelAvailabilityStatus.UNKNOWN
            else TrivagoStayStopReason.LOOKAHEAD_EXHAUSTED
        )
        if self.stop_reason is not expected_stop_reason:
            raise ValueError("stop_reason is inconsistent with stay attempts")
        return self


class TrivagoStayAvailabilityResultWriter:
    """Write one immutable, fully contextual stay-search result."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, result: TrivagoStayAvailabilityResult) -> Path:
        destination = (
            self.root_directory / f"run={quote(result.run_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Trivago stay availability result already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(result.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination


class TrivagoStayAvailabilityRunner:
    """Crawl one exact stay and optionally shift the whole stay window forward.

    The runner only advances after fresh, trusted ``UNAVAILABLE`` evidence. An
    identity, parser, transport, or price problem is ``UNKNOWN`` and stops the
    search, preventing technical failures from being shown as sold-out rooms.
    """

    max_lookahead_days = 14
    max_identity_retry_limit = 4
    _NO_ACCOMMODATIONS_TEXT = {
        "no accommodation found",
        "no accommodations found",
        "no bookable accommodation found",
        "no bookable accommodations found",
    }
    _SOLD_OUT_TEXT = {
        "sold out",
        "sold_out",
        "unavailable",
        "not available",
        "no availability",
    }
    _STATUS_FIELDS = (
        "availability",
        "availability_status",
        "booking_status",
        "status",
    )

    def __init__(
        self,
        adapter: TrivagoStayCapture,
        raw_writer: RawJsonWriter,
        normalized_writer: NormalizedHotelPriceWriter,
        audit_writer: TrivagoDiscoveryAuditWriter,
        mapping_writer: CurrentTrivagoMappingWriter,
        availability_writer: CurrentHotelAvailabilityWriter,
        *,
        validation_writer: ValidationResultWriter | None = None,
        decision_writer: HotelPriceDecisionWriter | None = None,
        current_price_writer: CurrentHotelPriceWriter | None = None,
        resolver: TrivagoDiscoveryResolver | None = None,
        normalizer: TrivagoMcpPriceNormalizer | None = None,
        validator: HotelPriceValidatorOrchestrator | None = None,
        decision_gate: HotelPriceDecisionGate | None = None,
        accepted_observation_store: AcceptedObservationStore | None = None,
        identity_retry_limit: int = 2,
        include_radius_identity_retry: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        quality_writers = (
            validation_writer,
            decision_writer,
            current_price_writer,
        )
        if any(writer is not None for writer in quality_writers) and not all(
            writer is not None for writer in quality_writers
        ):
            raise ValueError(
                "validation, decision, and current-price writers must be "
                "configured together"
            )
        if accepted_observation_store is not None and not all(
            writer is not None for writer in quality_writers
        ):
            raise ValueError(
                "accepted observation storage requires validation, decision, "
                "and current-price writers"
            )
        if not 0 <= identity_retry_limit <= self.max_identity_retry_limit:
            raise ValueError(
                "identity_retry_limit must be between 0 and "
                f"{self.max_identity_retry_limit}"
            )
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.normalized_writer = normalized_writer
        self.audit_writer = audit_writer
        self.mapping_writer = mapping_writer
        self.availability_writer = availability_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.current_price_writer = current_price_writer
        self.accepted_observation_store = accepted_observation_store
        self.identity_retry_limit = identity_retry_limit
        self.include_radius_identity_retry = include_radius_identity_retry
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resolver = resolver or TrivagoDiscoveryResolver(clock=self.clock)
        self.normalizer = normalizer or TrivagoMcpPriceNormalizer()
        self.validator = validator or HotelPriceValidatorOrchestrator(clock=self.clock)
        self.decision_gate = decision_gate or HotelPriceDecisionGate(clock=self.clock)

    def run(
        self,
        entry: TrivagoHotelRegistryEntry,
        requested_context: TrivagoPriceBatchContext,
        *,
        run_id: str,
        lookahead_days: int = 1,
    ) -> TrivagoStayAvailabilityResult:
        if not 0 <= lookahead_days <= self.max_lookahead_days:
            raise ValueError(
                f"lookahead_days must be between 0 and {self.max_lookahead_days}"
            )

        started_at = self.clock()
        attempts: list[TrivagoStayWindowAttempt] = []
        for offset in range(lookahead_days + 1):
            shifted_context = requested_context.model_copy(
                update={
                    "check_in": requested_context.check_in + timedelta(days=offset),
                    "check_out": requested_context.check_out + timedelta(days=offset),
                }
            )
            attempt = self._attempt(
                entry,
                shifted_context,
                requested_check_in=requested_context.check_in,
                fallback_offset_days=offset,
                attempt_run_id=f"{run_id}-offset-{offset}",
            )
            attempts.append(attempt)
            if attempt.availability.status is not HotelAvailabilityStatus.UNAVAILABLE:
                break

        available = next(
            (
                item
                for item in attempts
                if item.availability.status is HotelAvailabilityStatus.AVAILABLE
            ),
            None,
        )
        if available is not None:
            stop_reason = TrivagoStayStopReason.AVAILABLE_FOUND
            selected_offset = available.fallback_offset_days
        elif attempts[-1].availability.status is HotelAvailabilityStatus.UNKNOWN:
            stop_reason = TrivagoStayStopReason.UNKNOWN_RESULT
            selected_offset = None
        else:
            stop_reason = TrivagoStayStopReason.LOOKAHEAD_EXHAUSTED
            selected_offset = None

        return TrivagoStayAvailabilityResult(
            run_id=run_id,
            hotel_id=entry.entity_id,
            requested_context=requested_context,
            lookahead_days=lookahead_days,
            started_at=started_at,
            finished_at=self.clock(),
            stop_reason=stop_reason,
            selected_available_offset_days=selected_offset,
            attempts=attempts,
        )

    def _attempt(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        requested_check_in: date,
        fallback_offset_days: int,
        attempt_run_id: str,
    ) -> TrivagoStayWindowAttempt:
        try:
            record, resolution, artifacts = self._resolve_identity(
                entry,
                context,
                attempt_run_id=attempt_run_id,
            )
        except Exception as error:
            return self._technical_failure_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                stage="capture",
                error=error,
            )

        mapping = self._confirmed_mapping(entry, resolution)
        if (
            mapping is not None
            and resolution.status is TrivagoDiscoveryStatus.CONFIRMED
        ):
            artifacts.append(str(self.mapping_writer.publish(mapping)))

        accommodations, provider_text, parse_error = self._provider_outcome(record)
        if parse_error is not None:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.CRAWL_ERROR,
                raw_status_text=parse_error,
                resolution_status=resolution.status,
                artifact_paths=artifacts,
                error_stage="parse",
                error=parse_error,
            )

        if mapping is None:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=None,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.MAPPING_UNRESOLVED,
                raw_status_text="hotel identity was not confirmed",
                resolution_status=resolution.status,
                artifact_paths=artifacts,
            )

        assert accommodations is not None
        exact_candidates = [
            candidate
            for candidate in accommodations
            if self._text(candidate.get("accommodation_id")) == mapping.external_id
        ]
        if not exact_candidates:
            identity_reverify = (
                entry.status is TrivagoRegistryStatus.CONFIRMED
                and resolution.status is TrivagoDiscoveryStatus.REVIEW
                and "external_id_change_requires_review" in resolution.reason_codes
            )
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=(
                    HotelAvailabilityReason.IDENTITY_REVERIFY
                    if identity_reverify
                    else HotelAvailabilityReason.CONFIRMED_LISTING_NOT_RETURNED
                ),
                raw_status_text=(
                    "stable property identity indicates an external-ID change; "
                    "human approval is required"
                    if identity_reverify
                    else provider_text or "confirmed provider hotel was not returned"
                ),
                resolution_status=resolution.status,
                artifact_paths=artifacts,
            )

        explicit_status = self._explicit_unavailable_status(exact_candidates)
        if explicit_status is not None:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNAVAILABLE,
                reason=HotelAvailabilityReason.SOLD_OUT,
                raw_status_text=explicit_status,
                resolution_status=resolution.status,
                artifact_paths=artifacts,
            )

        priced_candidates = [
            candidate
            for candidate in exact_candidates
            if self._has_complete_price(candidate)
        ]
        if not priced_candidates:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.NO_PRICE,
                raw_status_text="exact hotel returned without a complete price",
                resolution_status=resolution.status,
                artifact_paths=artifacts,
            )

        try:
            # Trivago can return duplicate rows for the same accommodation and
            # seller context. Normalize the complete rows from the immutable raw
            # record so one incomplete duplicate cannot discard a valid offer.
            normalized = self.normalizer.normalize(
                self._price_normalization_view(record, priced_candidates),
                mapping,
            )
        except InvalidPriceError as error:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.NO_PRICE,
                raw_status_text=str(error),
                resolution_status=resolution.status,
                artifact_paths=artifacts,
                error_stage="normalize",
                error=str(error),
            )
        except HotelPriceNormalizationError as error:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.CRAWL_ERROR,
                raw_status_text=str(error),
                resolution_status=resolution.status,
                artifact_paths=artifacts,
                error_stage="normalize",
                error=str(error),
            )

        usable_prices = self._write_and_validate_prices(
            normalized,
            mapping,
            artifacts,
        )
        if not usable_prices:
            return self._publish_attempt(
                entry,
                context,
                requested_check_in=requested_check_in,
                fallback_offset_days=fallback_offset_days,
                attempt_run_id=attempt_run_id,
                record=record,
                mapping=mapping,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.NO_PRICE,
                raw_status_text="priced offer did not pass the quality gate",
                resolution_status=resolution.status,
                artifact_paths=artifacts,
            )

        return self._publish_attempt(
            entry,
            context,
            requested_check_in=requested_check_in,
            fallback_offset_days=fallback_offset_days,
            attempt_run_id=attempt_run_id,
            record=record,
            mapping=mapping,
            status=HotelAvailabilityStatus.AVAILABLE,
            reason=HotelAvailabilityReason.OFFER_FOUND,
            raw_status_text="bookable priced offer returned",
            resolution_status=resolution.status,
            artifact_paths=artifacts,
            prices=usable_prices,
        )

    def _resolve_identity(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        attempt_run_id: str,
    ) -> tuple[SourceRecord, TrivagoDiscoveryResolution, list[str]]:
        """Try a bounded set of registry-pinned identity searches.

        Alias text only influences provider retrieval.  Every response is
        resolved against the trusted master identity, and radius-only evidence
        therefore remains REVIEW under ``TrivagoDiscoveryResolver``.
        """

        request = context.request_for(entry)
        artifacts: list[str] = []
        best: tuple[SourceRecord, TrivagoDiscoveryResolution] | None = None
        status_rank = {
            TrivagoDiscoveryStatus.REJECTED: 0,
            TrivagoDiscoveryStatus.MISSING: 1,
            TrivagoDiscoveryStatus.REVIEW: 2,
            TrivagoDiscoveryStatus.CONFIRMED: 3,
        }
        for index, (strategy, query) in enumerate(self._identity_search_plans(entry)):
            child_run_id = f"{attempt_run_id}-identity-{index}"
            search_arguments: dict[str, object] = {
                "run_id": child_run_id,
                "strategy": strategy,
            }
            if strategy is TrivagoSearchStrategy.NAME and query != entry.search_query:
                search_arguments["query"] = query
            record = self.adapter.search(entry, request, **search_arguments)
            artifacts.append(str(self.raw_writer.write(record)))
            resolution = self.resolver.resolve(entry, record)
            artifacts.append(
                str(self.audit_writer.write(resolution, run_id=child_run_id))
            )
            if best is None or (
                status_rank[resolution.status],
                resolution.confidence,
            ) > (status_rank[best[1].status], best[1].confidence):
                best = (record, resolution)
            if resolution.status is TrivagoDiscoveryStatus.CONFIRMED:
                break

        assert best is not None
        return best[0], best[1], artifacts

    def _identity_search_plans(
        self,
        entry: TrivagoHotelRegistryEntry,
    ) -> list[tuple[TrivagoSearchStrategy, str | None]]:
        retry_slots = self.identity_retry_limit
        use_radius = (
            self.include_radius_identity_retry
            and retry_slots > 0
            and entry.latitude is not None
            and entry.longitude is not None
        )
        alias_slots = retry_slots - int(use_radius)
        queries = list(entry.identity_search_queries)
        plans: list[tuple[TrivagoSearchStrategy, str | None]] = [
            (TrivagoSearchStrategy.NAME, queries[0])
        ]
        plans.extend(
            (TrivagoSearchStrategy.NAME, query)
            for query in queries[1 : 1 + alias_slots]
        )
        if use_radius:
            plans.append((TrivagoSearchStrategy.RADIUS, None))
        return plans

    def _write_and_validate_prices(
        self,
        observations: list[HotelPriceObservation],
        mapping: ExternalEntityMapping,
        artifacts: list[str],
    ) -> list[HotelPriceObservation]:
        expected_mapping = (mapping.mapping_id, mapping.external_id)
        if any(
            (observation.mapping_id, observation.external_id) != expected_mapping
            for observation in observations
        ):
            raise ValueError(
                "normalized prices do not match the confirmed mapping identity"
            )

        usable: list[HotelPriceObservation] = []
        for observation in observations:
            artifacts.append(str(self.normalized_writer.write(observation)))
            if self.validation_writer is None:
                usable.append(observation)
                continue
            assert self.decision_writer is not None
            assert self.current_price_writer is not None
            validations = self.validator.validate(observation, mapping)
            for validation in validations:
                artifacts.append(str(self.validation_writer.write(validation)))
            decision = self.decision_gate.decide(observation, validations)
            artifacts.append(str(self.decision_writer.write(decision)))
            if decision.status is HotelPriceDecisionStatus.PASS:
                if self.accepted_observation_store is not None:
                    artifacts.append(
                        str(self.accepted_observation_store.write(observation))
                    )
                artifacts.append(
                    str(self.current_price_writer.publish(observation, decision))
                )
                usable.append(observation)
        return usable

    def _technical_failure_attempt(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        requested_check_in: date,
        fallback_offset_days: int,
        attempt_run_id: str,
        stage: str,
        error: Exception,
        record: SourceRecord | None = None,
        artifact_paths: list[str] | None = None,
    ) -> TrivagoStayWindowAttempt:
        mapping = (
            entry.to_mapping()
            if entry.status is TrivagoRegistryStatus.CONFIRMED
            else None
        )
        return self._publish_attempt(
            entry,
            context,
            requested_check_in=requested_check_in,
            fallback_offset_days=fallback_offset_days,
            attempt_run_id=attempt_run_id,
            record=record,
            mapping=mapping,
            status=HotelAvailabilityStatus.UNKNOWN,
            reason=HotelAvailabilityReason.CRAWL_ERROR,
            raw_status_text=f"{type(error).__name__}: {error}",
            resolution_status=None,
            artifact_paths=artifact_paths or [],
            error_stage=stage,
            error=f"{type(error).__name__}: {error}",
        )

    def _publish_attempt(
        self,
        entry: TrivagoHotelRegistryEntry,
        context: TrivagoPriceBatchContext,
        *,
        requested_check_in: date,
        fallback_offset_days: int,
        attempt_run_id: str,
        record: SourceRecord | None,
        mapping: ExternalEntityMapping | None,
        status: HotelAvailabilityStatus,
        reason: HotelAvailabilityReason,
        raw_status_text: str,
        resolution_status: TrivagoDiscoveryStatus | None,
        artifact_paths: list[str],
        prices: list[HotelPriceObservation] | None = None,
        error_stage: str | None = None,
        error: str | None = None,
    ) -> TrivagoStayWindowAttempt:
        usable_prices = prices or []
        observed_at = record.crawled_at if record is not None else self.clock()
        source_record_id = (
            record.source_record_id
            if record is not None
            else f"{attempt_run_id}:capture-error"
        )
        source_id = record.source_id if record is not None else "trivago-mcp"
        source_url = record.source_url if record is not None else None
        observation = HotelAvailabilityObservation(
            observation_id=f"{source_record_id}:availability",
            run_id=attempt_run_id,
            hotel_id=entry.entity_id,
            source_record_id=source_record_id,
            source_id=source_id,
            source_url=source_url,
            mapping_id=mapping.mapping_id if mapping is not None else None,
            external_id=mapping.external_id if mapping is not None else None,
            requested_check_in=requested_check_in,
            fallback_offset_days=fallback_offset_days,
            check_in=context.check_in,
            check_out=context.check_out,
            nights=(context.check_out - context.check_in).days,
            occupancy=context.occupancy,
            children_ages=context.children_ages,
            currency=context.currency,
            status=status,
            reason=reason,
            offer_count=len(usable_prices),
            price_observation_ids=[item.observation_id for item in usable_prices],
            raw_status_text=raw_status_text,
            observed_at=observed_at,
            verification_status=VerificationStatus.AUTO_VERIFIED,
        )
        artifacts = list(artifact_paths)
        if status is not HotelAvailabilityStatus.UNKNOWN:
            if self.accepted_observation_store is not None:
                artifacts.append(
                    str(self.accepted_observation_store.write(observation))
                )
            artifacts.append(str(self.availability_writer.publish(observation)))
        return TrivagoStayWindowAttempt(
            attempt_run_id=attempt_run_id,
            fallback_offset_days=fallback_offset_days,
            availability=observation,
            prices=usable_prices,
            resolution_status=resolution_status,
            artifact_paths=artifacts,
            error_stage=error_stage,
            error=error,
        )

    @staticmethod
    def _confirmed_mapping(
        entry: TrivagoHotelRegistryEntry,
        resolution,
    ) -> ExternalEntityMapping | None:
        if resolution.status is TrivagoDiscoveryStatus.CONFIRMED:
            return apply_trivago_resolution(entry, resolution).to_mapping()
        if entry.status is TrivagoRegistryStatus.CONFIRMED:
            return entry.to_mapping()
        return None

    @classmethod
    def _provider_outcome(
        cls,
        record: SourceRecord,
    ) -> tuple[list[dict[str, object]] | None, str | None, str | None]:
        response = record.raw_payload.get("response")
        if not isinstance(response, dict):
            return None, None, "response must be an object"
        result = response.get("result")
        if not isinstance(result, dict):
            return None, None, "response.result must be an object"
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            return None, None, "structuredContent must be an object"
        accommodations = structured.get("accommodations")
        if isinstance(accommodations, list):
            if any(not isinstance(item, dict) for item in accommodations):
                return None, None, "accommodations must contain objects"
            return list(accommodations), None, None
        provider_text = cls._text(structured.get("error"))
        if (
            provider_text is not None
            and cls._normalized_text(provider_text) in cls._NO_ACCOMMODATIONS_TEXT
        ):
            return [], provider_text, None
        return None, provider_text, "accommodations missing from structuredContent"

    @classmethod
    def _explicit_unavailable_status(
        cls,
        candidates: list[dict[str, object]],
    ) -> str | None:
        for candidate in candidates:
            for field in cls._STATUS_FIELDS:
                text = cls._text(candidate.get(field))
                if (
                    text is not None
                    and cls._normalized_text(text) in cls._SOLD_OUT_TEXT
                ):
                    return text
        return None

    @staticmethod
    def _has_complete_price(candidate: dict[str, object]) -> bool:
        return all(
            isinstance(candidate.get(field), str)
            and bool(str(candidate[field]).strip())
            for field in (
                "currency",
                "price_per_night",
                "price_per_stay",
                "advertisers",
            )
        )

    @staticmethod
    def _price_normalization_view(
        record: SourceRecord,
        candidates: list[dict[str, object]],
    ) -> SourceRecord:
        raw_payload = copy.deepcopy(record.raw_payload)
        response = raw_payload["response"]
        assert isinstance(response, dict)
        result = response["result"]
        assert isinstance(result, dict)
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        structured["accommodations"] = candidates
        # The projection is never persisted; it retains the original source
        # identity so normalized observations still point to the raw evidence.
        return record.model_copy(update={"raw_payload": raw_payload})

    @classmethod
    def _is_name_search(cls, record: SourceRecord) -> bool:
        request = record.raw_payload.get("request")
        if not isinstance(request, dict):
            return False
        if request.get("search_strategy") == TrivagoSearchStrategy.NAME.value:
            return True
        tool = cls._text(request.get("tool"))
        if tool == "trivago-accommodation-search":
            return True
        arguments = request.get("arguments")
        return isinstance(arguments, dict) and bool(cls._text(arguments.get("query")))

    @staticmethod
    def _text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(value.split()).casefold()
