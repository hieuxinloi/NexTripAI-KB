from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import TrivagoMcpDiscoveryAdapter
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoRegistryBuilder,
    TrivagoRegistryStatus,
    TrivagoSearchReviewConfig,
)
from nextrip_pipeline.decision_gate import (
    HotelPriceDecisionGate,
    HotelPriceDecisionWriter,
)
from nextrip_pipeline.jobs.trivago_mcp_batch import (
    TrivagoPriceBatchContext,
)
from nextrip_pipeline.jobs.trivago_stay_availability import (
    TrivagoStayAvailabilityResult,
    TrivagoStayAvailabilityResultWriter,
    TrivagoStayAvailabilityRunner,
)
from nextrip_pipeline.preprocessing import NormalizedHotelPriceWriter
from nextrip_pipeline.publishing import (
    AcceptedObservationStore,
    CurrentHotelAvailabilityWriter,
    CurrentHotelPriceWriter,
)
from nextrip_pipeline.quality import (
    CurrentTrivagoMappingWriter,
    TrivagoDiscoveryAuditWriter,
)
from nextrip_pipeline.schemas import ExternalEntityMapping
from nextrip_pipeline.validators import (
    HotelPriceValidatorOrchestrator,
    ValidationResultWriter,
)

if TYPE_CHECKING:
    from nextrip_current.models import HotelOfferSearchRequest


class HotelOfferRefreshError(RuntimeError):
    """Raised when a bounded on-demand Trivago refresh cannot complete."""


@dataclass(frozen=True, slots=True)
class TrivagoRefreshPaths:
    """All durable inputs and outputs used by one on-demand refresh."""

    canonical_dataset_file: Path
    evidence_root: Path
    checked_in_mapping_files: tuple[Path, ...]
    search_review_file: Path | None
    current_mapping_directory: Path
    raw_directory: Path
    normalized_directory: Path
    quality_directory: Path
    validation_directory: Path
    decision_directory: Path
    current_price_directory: Path
    current_availability_directory: Path
    summary_directory: Path
    accepted_observation_directory: Path | None = None

    @classmethod
    def from_kb_root(
        cls,
        root: str | Path,
        *,
        canonical_dataset: str | Path | None = None,
    ) -> TrivagoRefreshPaths:
        kb_root = Path(root).expanduser().resolve()
        configured_dataset = canonical_dataset or os.getenv("NEXTRIP_CANONICAL_DATASET")
        if configured_dataset is None or not str(configured_dataset).strip():
            raise ValueError(
                "NEXTRIP_CANONICAL_DATASET is required for Trivago refresh"
            )
        canonical_path = Path(configured_dataset).expanduser()
        if not canonical_path.is_absolute():
            canonical_path = kb_root / canonical_path
        default_mapping = kb_root / "config" / "trivago-mapping.json"
        return cls(
            canonical_dataset_file=canonical_path.resolve(),
            evidence_root=kb_root,
            checked_in_mapping_files=(default_mapping,),
            search_review_file=kb_root / "config" / "trivago-search-review.json",
            current_mapping_directory=kb_root / "data" / "current" / "trivago_mappings",
            raw_directory=kb_root / "data" / "raw",
            normalized_directory=kb_root / "data" / "normalized",
            quality_directory=kb_root / "data" / "quality" / "trivago_mapping",
            validation_directory=kb_root / "data" / "validation",
            decision_directory=kb_root / "data" / "decisions",
            current_price_directory=kb_root / "data" / "current" / "hotel_price",
            current_availability_directory=(
                kb_root / "data" / "current" / "hotel_availability"
            ),
            summary_directory=kb_root / "data" / "runs" / "trivago_mcp",
            accepted_observation_directory=kb_root / "data" / "observations",
        )


class TrivagoOnDemandPriceRefresher:
    """Run the existing audited Trivago pipeline for exactly one hotel.

    The caller must opt into refresh and enforce the one-hotel API limit.  A
    process-local lock prevents concurrent MCP sessions from writing the same
    context through this instance.  The immutable writers and current-price
    compare-and-replace checks remain the final concurrency safeguards.
    """

    def __init__(
        self,
        paths: TrivagoRefreshPaths,
        *,
        adapter: TrivagoMcpDiscoveryAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.adapter = adapter or TrivagoMcpDiscoveryAdapter()
        self._owns_adapter = adapter is None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = Lock()

    def refresh(
        self,
        hotel_id: str,
        request: HotelOfferSearchRequest,
    ) -> TrivagoStayAvailabilityResult:
        """Refresh one complete stay plus safe lookahead dates."""

        normalized_hotel_id = hotel_id.strip()
        if not normalized_hotel_id:
            raise HotelOfferRefreshError("hotel_id cannot be blank")

        with self._lock:
            registry = self._registry()
            if normalized_hotel_id not in {
                entry.entity_id for entry in registry.entries
            }:
                raise HotelOfferRefreshError(
                    f"hotel {normalized_hotel_id!r} is not in the active canonical "
                    "dataset"
                )

            try:
                context = TrivagoPriceBatchContext(
                    check_in=request.check_in,
                    check_out=request.check_out,
                    occupancy=request.occupancy,
                    children_ages=list(request.children_ages),
                    currency=request.currency or "VND",
                )
            except (AttributeError, ValidationError, ValueError) as error:
                raise HotelOfferRefreshError(
                    f"invalid hotel refresh context: {error}"
                ) from error

            matching_entries = [
                entry
                for entry in registry.entries
                if entry.entity_id == normalized_hotel_id
            ]
            if len(matching_entries) != 1:
                raise HotelOfferRefreshError(
                    "on-demand refresh could not resolve one registry entry"
                )
            entry = matching_entries[0]
            if entry.status in {
                TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
                TrivagoRegistryStatus.IDENTITY_REVERIFY,
                TrivagoRegistryStatus.REJECTED,
            }:
                raise HotelOfferRefreshError(
                    f"hotel {normalized_hotel_id!r} is not eligible for automatic "
                    f"Trivago refresh: {entry.status.value}"
                )

            run_id = self._run_id()
            result = TrivagoStayAvailabilityRunner(
                self.adapter,
                RawJsonWriter(self.paths.raw_directory),
                NormalizedHotelPriceWriter(self.paths.normalized_directory),
                TrivagoDiscoveryAuditWriter(self.paths.quality_directory),
                CurrentTrivagoMappingWriter(self.paths.current_mapping_directory),
                CurrentHotelAvailabilityWriter(
                    self.paths.current_availability_directory,
                    clock=self.clock,
                ),
                validation_writer=ValidationResultWriter(
                    self.paths.validation_directory
                ),
                decision_writer=HotelPriceDecisionWriter(self.paths.decision_directory),
                current_price_writer=CurrentHotelPriceWriter(
                    self.paths.current_price_directory,
                    clock=self.clock,
                ),
                accepted_observation_store=AcceptedObservationStore(
                    self.paths.accepted_observation_directory
                    or self.paths.evidence_root / "data" / "observations"
                ),
                validator=HotelPriceValidatorOrchestrator(clock=self.clock),
                decision_gate=HotelPriceDecisionGate(clock=self.clock),
                clock=self.clock,
            ).run(
                entry,
                context,
                run_id=run_id,
                lookahead_days=request.lookahead_days,
            )
            TrivagoStayAvailabilityResultWriter(self.paths.summary_directory).write(
                result
            )

            if result.hotel_id != normalized_hotel_id or not result.attempts:
                raise HotelOfferRefreshError(
                    "on-demand refresh produced an invalid stay result"
                )
            return result

    def close(self) -> None:
        if not self._owns_adapter:
            return
        client = getattr(self.adapter, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def _registry(self):  # type: ignore[no-untyped-def]
        overrides: dict[str, ExternalEntityMapping] = {}
        for path in self.paths.checked_in_mapping_files:
            if not path.is_file():
                continue
            mapping = ExternalEntityMapping.model_validate_json(
                path.read_text(encoding="utf-8-sig")
            )
            overrides[mapping.entity_id] = mapping

        for mapping in CurrentTrivagoMappingWriter(
            self.paths.current_mapping_directory
        ).all():
            overrides[mapping.entity_id] = mapping

        search_review_overrides = []
        if (
            self.paths.search_review_file is not None
            and self.paths.search_review_file.is_file()
        ):
            search_review_overrides = TrivagoSearchReviewConfig.model_validate_json(
                self.paths.search_review_file.read_bytes()
            ).overrides

        dataset = read_canonical_active_dataset(self.paths.canonical_dataset_file)
        registry, _ = TrivagoRegistryBuilder(
            clock=self.clock
        ).build_from_canonical_dataset(
            dataset,
            overrides=list(overrides.values()),
            search_review_overrides=search_review_overrides,
            evidence_root=self.paths.evidence_root,
        )
        return registry

    def _run_id(self) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"current-api-trivago-{timestamp}-{uuid4().hex[:8]}"


def checked_in_mapping_paths(paths: Sequence[str | Path]) -> tuple[Path, ...]:
    """Normalize configured mapping paths for runtime construction."""

    return tuple(Path(path) for path in paths)
