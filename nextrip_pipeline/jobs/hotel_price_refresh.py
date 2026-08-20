from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import TrivagoPriceRequest
from nextrip_pipeline.decision_gate import (
    HotelPriceDecision,
    HotelPriceDecisionGate,
    HotelPriceDecisionStatus,
    HotelPriceDecisionWriter,
)
from nextrip_pipeline.preprocessing import (
    NormalizedHotelPriceWriter,
    TrivagoMcpPriceNormalizer,
)
from nextrip_pipeline.publishing import CurrentHotelPriceWriter
from nextrip_pipeline.schemas import ExternalEntityMapping
from nextrip_pipeline.validators import (
    HotelPriceValidatorOrchestrator,
    ValidationResultWriter,
)

from .browser_crawls import HotelPriceCaptureAdapter


@dataclass(slots=True)
class HotelPriceRefreshResult:
    run_id: str
    raw_path: Path
    normalized_paths: list[Path] = field(default_factory=list)
    validation_paths: list[Path] = field(default_factory=list)
    decision_paths: list[Path] = field(default_factory=list)
    current_paths: list[Path] = field(default_factory=list)
    decisions: list[HotelPriceDecision] = field(default_factory=list)


class HotelPriceRefreshPipeline:
    """Runs crawl → normalize → validate → decide → publish current."""

    def __init__(
        self,
        adapter: HotelPriceCaptureAdapter,
        raw_writer: RawJsonWriter,
        normalized_writer: NormalizedHotelPriceWriter,
        validation_writer: ValidationResultWriter,
        decision_writer: HotelPriceDecisionWriter,
        current_writer: CurrentHotelPriceWriter,
        *,
        normalizer: TrivagoMcpPriceNormalizer | None = None,
        validator: HotelPriceValidatorOrchestrator | None = None,
        decision_gate: HotelPriceDecisionGate | None = None,
    ) -> None:
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.normalized_writer = normalized_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.current_writer = current_writer
        self.normalizer = normalizer or TrivagoMcpPriceNormalizer()
        self.validator = validator or HotelPriceValidatorOrchestrator()
        self.decision_gate = decision_gate or HotelPriceDecisionGate()

    def run(
        self,
        mapping: ExternalEntityMapping,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
    ) -> HotelPriceRefreshResult:
        record = self.adapter.fetch(mapping, request, run_id=run_id)
        result = HotelPriceRefreshResult(
            run_id=run_id,
            raw_path=self.raw_writer.write(record),
        )
        observations = self.normalizer.normalize(record, mapping)
        for observation in observations:
            result.normalized_paths.append(self.normalized_writer.write(observation))
            validations = self.validator.validate(observation, mapping)
            result.validation_paths.extend(
                self.validation_writer.write(item) for item in validations
            )
            decision = self.decision_gate.decide(observation, validations)
            result.decisions.append(decision)
            result.decision_paths.append(self.decision_writer.write(decision))
            if decision.status is HotelPriceDecisionStatus.PASS:
                result.current_paths.append(
                    self.current_writer.publish(observation, decision)
                )
        return result
