from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import TrivagoMcpDiscoveryAdapter
from nextrip_pipeline.crawl.trivago_registry import TrivagoRegistryBuilder
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

    master_file: Path
    checked_in_mapping_files: tuple[Path, ...]
    current_mapping_directory: Path
    raw_directory: Path
    normalized_directory: Path
    quality_directory: Path
    validation_directory: Path
    decision_directory: Path
    current_price_directory: Path
    current_availability_directory: Path
    summary_directory: Path

    @classmethod
    def from_kb_root(cls, root: str | Path) -> TrivagoRefreshPaths:
        kb_root = Path(root)
        default_mapping = kb_root / "config" / "trivago-mapping.json"
        return cls(
            master_file=kb_root / "travel_data_verified" / "hotel_final.json",
            checked_in_mapping_files=(default_mapping,),
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
                    f"hotel {normalized_hotel_id!r} is not in verified master data"
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
                validator=HotelPriceValidatorOrchestrator(clock=self.clock),
                decision_gate=HotelPriceDecisionGate(clock=self.clock),
                clock=self.clock,
            ).run(
                matching_entries[0],
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

        registry, _ = TrivagoRegistryBuilder(clock=self.clock).build(
            self.paths.master_file,
            overrides=list(overrides.values()),
        )
        return registry

    def _run_id(self) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"current-api-trivago-{timestamp}-{uuid4().hex[:8]}"


def checked_in_mapping_paths(paths: Sequence[str | Path]) -> tuple[Path, ...]:
    """Normalize configured mapping paths for runtime construction."""

    return tuple(Path(path) for path in paths)
