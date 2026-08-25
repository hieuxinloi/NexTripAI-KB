from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import GoogleMapsPlaceAdapter
from nextrip_pipeline.decision_gate import (
    GoogleMapsDecision,
    GoogleMapsDecisionGate,
    GoogleMapsDecisionWriter,
)
from nextrip_pipeline.preprocessing import (
    GoogleMapsPlaceNormalizer,
    NormalizedGoogleMapsWriter,
)
from nextrip_pipeline.publishing import AcceptedObservationStore, CurrentPlaceWriter
from nextrip_pipeline.quality import (
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingResolution,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueReceipt,
    LLMReviewRequestWriter,
    MappingResolutionStatus,
    apply_rejected_resolution,
)
from nextrip_pipeline.schemas import ExternalEntityMapping
from nextrip_pipeline.validators import (
    GoogleMapsValidationWriter,
    GoogleMapsValidatorOrchestrator,
)


@dataclass(slots=True)
class GoogleMapsRefreshResult:
    run_id: str
    raw_path: Path
    normalized_path: Path
    validation_paths: list[Path] = field(default_factory=list)
    decision_path: Path | None = None
    decision: GoogleMapsDecision | None = None
    mapping_resolution: GoogleMapsMappingResolution | None = None
    resolution_path: Path | None = None
    current_mapping_path: Path | None = None
    current_place_path: Path | None = None
    accepted_observation_paths: list[Path] = field(default_factory=list)
    llm_review_receipt: LLMReviewQueueReceipt | None = None


class GoogleMapsRefreshPipeline:
    """Runs Maps crawl -> normalize -> validate -> decision."""

    def __init__(
        self,
        adapter: GoogleMapsPlaceAdapter,
        raw_writer: RawJsonWriter,
        normalized_writer: NormalizedGoogleMapsWriter,
        validation_writer: GoogleMapsValidationWriter,
        decision_writer: GoogleMapsDecisionWriter,
        *,
        normalizer: GoogleMapsPlaceNormalizer | None = None,
        validator: GoogleMapsValidatorOrchestrator | None = None,
        decision_gate: GoogleMapsDecisionGate | None = None,
        mapping_resolver: GoogleMapsMappingResolver | None = None,
        resolution_writer: GoogleMapsMappingResolutionWriter | None = None,
        current_mapping_writer: CurrentGoogleMapsMappingWriter | None = None,
        llm_review_writer: LLMReviewRequestWriter | None = None,
        current_place_writer: CurrentPlaceWriter | None = None,
        accepted_observation_store: AcceptedObservationStore | None = None,
    ) -> None:
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.normalized_writer = normalized_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.normalizer = normalizer or GoogleMapsPlaceNormalizer()
        self.validator = validator or GoogleMapsValidatorOrchestrator()
        self.decision_gate = decision_gate or GoogleMapsDecisionGate()
        self.mapping_resolver = mapping_resolver
        self.resolution_writer = resolution_writer
        self.current_mapping_writer = current_mapping_writer
        self.llm_review_writer = llm_review_writer
        self.current_place_writer = current_place_writer
        self.accepted_observation_store = accepted_observation_store

    def run(
        self, mapping: ExternalEntityMapping, *, run_id: str
    ) -> GoogleMapsRefreshResult:
        record = self.adapter.fetch(mapping, run_id=run_id)
        raw_path = self.raw_writer.write(record)
        observation = self.normalizer.normalize(record, mapping)
        resolution = None
        resolution_path = None
        current_mapping_path = None
        llm_review_receipt = None
        effective_mapping = mapping
        mapping_to_publish: ExternalEntityMapping | None = None
        if self.mapping_resolver is not None:
            resolution, effective_mapping = self.mapping_resolver.resolve_and_update(
                mapping,
                observation,
                canonical_url=str(observation.source_url),
            )
            if self.resolution_writer is not None:
                resolution_path = self.resolution_writer.write(
                    resolution,
                    run_id=run_id,
                )
            if (
                resolution.status is MappingResolutionStatus.AUTO_CONFIRM
                and self.current_mapping_writer is not None
            ):
                mapping_to_publish = effective_mapping
                observation = self.normalizer.normalize(record, effective_mapping)
            elif (
                resolution.status is MappingResolutionStatus.REJECT
                and self.current_mapping_writer is not None
            ):
                effective_mapping = apply_rejected_resolution(mapping, resolution)
                mapping_to_publish = effective_mapping
            elif (
                resolution.status is MappingResolutionStatus.REVIEW
                and mapping.status.value == "auto_matched"
                and self.llm_review_writer is not None
            ):
                llm_review_receipt = self.llm_review_writer.enqueue(
                    resolution,
                    run_id=run_id,
                )
        normalized_path = self.normalized_writer.write(observation)
        validations = self.validator.validate(
            observation,
            effective_mapping,
            mapping_resolution=resolution,
        )
        validation_paths = [self.validation_writer.write(item) for item in validations]
        decision = self.decision_gate.decide(observation, validations)
        decision_path = self.decision_writer.write(decision)
        current_place_path = None
        accepted_observation_paths: list[Path] = []
        if (
            decision.status.value == "pass"
            and self.accepted_observation_store is not None
        ):
            # Persist accepted history before updating any mutable projection.
            # If this write fails, the current/canonical publish cannot advance.
            accepted_observation_paths = self.accepted_observation_store.write_many(
                [observation, observation.opening]
            )
        if mapping_to_publish is not None and (
            (
                resolution is not None
                and resolution.status is MappingResolutionStatus.AUTO_CONFIRM
                and decision.status.value == "pass"
            )
            or (
                resolution is not None
                and resolution.status is MappingResolutionStatus.REJECT
                and decision.status.value == "quarantine"
            )
        ):
            # For an accepted identity, do not advance the mutable mapping until
            # the immutable accepted observation has been durably persisted.
            assert self.current_mapping_writer is not None
            current_mapping_path = self.current_mapping_writer.publish(
                mapping_to_publish
            )
        if (
            decision.status.value == "pass"
            and self.current_place_writer is not None
        ):
            current_place_path = self.current_place_writer.publish(
                observation,
                decision,
                effective_mapping,
            )
        return GoogleMapsRefreshResult(
            run_id=run_id,
            raw_path=raw_path,
            normalized_path=normalized_path,
            validation_paths=validation_paths,
            decision_path=decision_path,
            decision=decision,
            mapping_resolution=resolution,
            resolution_path=resolution_path,
            current_mapping_path=current_mapping_path,
            current_place_path=current_place_path,
            accepted_observation_paths=accepted_observation_paths,
            llm_review_receipt=llm_review_receipt,
        )
