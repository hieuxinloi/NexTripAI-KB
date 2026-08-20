from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.decision_gate import (
    GoogleMapsDecisionGate,
    GoogleMapsDecisionStatus,
    GoogleMapsDecisionWriter,
)
from nextrip_pipeline.preprocessing import NormalizedGoogleMapsWriter
from nextrip_pipeline.publishing import CurrentPlaceWriter
from nextrip_pipeline.quality import (
    apply_rejected_resolution,
    CurrentGoogleMapsMappingWriter,
    GoogleMapsMappingResolutionWriter,
    GoogleMapsMappingResolver,
    LLMReviewQueueDisposition,
    LLMReviewQueueReceipt,
    LLMReviewRequestWriter,
    MappingResolutionStatus,
)
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    GeoPoint,
    GoogleMapsPlaceObservation,
    MappingStatus,
    NexTripModel,
)
from nextrip_pipeline.validators import (
    GoogleMapsValidationWriter,
    GoogleMapsValidatorOrchestrator,
)


class GoogleMapsReprocessItemStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GoogleMapsReprocessError(NexTripModel):
    stage: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)
    place_id: str | None = None
    path: str | None = None


class GoogleMapsReprocessItem(NexTripModel):
    place_id: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    source_observation_id: str = Field(min_length=1)
    derived_observation_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    status: GoogleMapsReprocessItemStatus
    coordinate_fallback_applied: bool = False
    resolution_status: MappingResolutionStatus | None = None
    decision_status: GoogleMapsDecisionStatus | None = None
    llm_queue_disposition: LLMReviewQueueDisposition | None = None
    normalized_path: str | None = None
    resolution_path: str | None = None
    validation_paths: list[str] = Field(default_factory=list)
    decision_path: str | None = None
    current_mapping_path: str | None = None
    current_place_path: str | None = None
    current_place_published: bool = False
    error: str | None = None


class GoogleMapsReprocessSummary(NexTripModel):
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    mapping_count: int = Field(ge=0)
    observation_candidate_count: int = Field(ge=0)
    selected_observation_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    coordinate_fallback_count: int = Field(ge=0)
    published_mapping_count: int = Field(ge=0)
    published_place_count: int = Field(ge=0)
    llm_queued_count: int = Field(ge=0)
    resolution_status_counts: dict[str, int] = Field(default_factory=dict)
    decision_status_counts: dict[str, int] = Field(default_factory=dict)
    llm_queue_status_counts: dict[str, int] = Field(default_factory=dict)
    missing_mapping_place_ids: list[str] = Field(default_factory=list)
    missing_observation_place_ids: list[str] = Field(default_factory=list)
    errors: list[GoogleMapsReprocessError] = Field(default_factory=list)
    items: list[GoogleMapsReprocessItem] = Field(default_factory=list)


class GoogleMapsReprocessSummaryWriter:
    """Write one immutable report for a local quality-reprocessing run."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def path_for(self, run_id: str) -> Path:
        return self.root_directory / f"run={quote(run_id, safe='-_.')}.json"

    def write(self, summary: GoogleMapsReprocessSummary) -> Path:
        destination = self.path_for(summary.run_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Google Maps reprocess summary already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(summary.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination


@dataclass(slots=True)
class GoogleMapsReprocessResult:
    summary: GoogleMapsReprocessSummary
    summary_path: Path


class GoogleMapsQualityReprocessor:
    """Re-run Maps quality gates from normalized JSON without crawling.

    The source observation and its crawl timestamp remain the evidence
    provenance. A derived observation identity is used for the new quality run
    so all normalized, validation, resolution, and decision artifacts remain
    immutable.
    """

    source_id = "google-maps-web"

    def __init__(
        self,
        normalized_writer: NormalizedGoogleMapsWriter,
        resolution_writer: GoogleMapsMappingResolutionWriter,
        validation_writer: GoogleMapsValidationWriter,
        decision_writer: GoogleMapsDecisionWriter,
        current_mapping_writer: CurrentGoogleMapsMappingWriter,
        current_place_writer: CurrentPlaceWriter,
        summary_writer: GoogleMapsReprocessSummaryWriter,
        *,
        resolver: GoogleMapsMappingResolver | None = None,
        validator: GoogleMapsValidatorOrchestrator | None = None,
        decision_gate: GoogleMapsDecisionGate | None = None,
        llm_review_writer: LLMReviewRequestWriter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.normalized_writer = normalized_writer
        self.resolution_writer = resolution_writer
        self.validation_writer = validation_writer
        self.decision_writer = decision_writer
        self.current_mapping_writer = current_mapping_writer
        self.current_place_writer = current_place_writer
        self.summary_writer = summary_writer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resolver = resolver or GoogleMapsMappingResolver(clock=self.clock)
        self.validator = validator or GoogleMapsValidatorOrchestrator(
            clock=self.clock
        )
        self.decision_gate = decision_gate or GoogleMapsDecisionGate(
            clock=self.clock
        )
        self.llm_review_writer = llm_review_writer

    def run(
        self,
        mappings: Sequence[ExternalEntityMapping],
        *,
        observations: Sequence[GoogleMapsPlaceObservation] = (),
        observation_paths: Sequence[str | Path] = (),
        observation_root: str | Path | None = None,
        run_id: str | None = None,
    ) -> GoogleMapsReprocessResult:
        run_id = run_id or self._new_run_id()
        if self.summary_writer.path_for(run_id).exists():
            raise FileExistsError(f"Google Maps reprocess run already exists: {run_id}")

        started_at = self.clock()
        errors: list[GoogleMapsReprocessError] = []
        loaded = list(observations)
        candidate_count = len(loaded)
        paths = self._observation_paths(observation_paths, observation_root)
        candidate_count += len(paths)
        for path in paths:
            try:
                loaded.append(self._read_observation(path))
            except Exception as error:
                errors.append(self._error("load_observation", error, path=path))

        valid_observations = []
        for observation in loaded:
            # Reprocess always starts from crawl-derived normalized evidence,
            # never from output created by an earlier quality reprocess run.
            # This keeps provenance stable even when input and output share the
            # same canonical normalized root.
            if ":quality-reprocess:" in observation.observation_id:
                continue
            problem = self._observation_problem(observation)
            if problem is None:
                valid_observations.append(observation)
            else:
                errors.append(
                    GoogleMapsReprocessError(
                        stage="validate_observation",
                        error_type="InvalidObservation",
                        message=problem,
                        place_id=observation.place_id,
                    )
                )
        latest = self._latest_by_place(valid_observations)
        mapping_by_place = self._mapping_index(mappings, errors)

        missing_mapping = sorted(set(latest) - set(mapping_by_place))
        missing_observation = sorted(set(mapping_by_place) - set(latest))
        for place_id in missing_mapping:
            errors.append(
                GoogleMapsReprocessError(
                    stage="match_mapping",
                    error_type="MissingMapping",
                    message="No Google Maps mapping exists for the observation",
                    place_id=place_id,
                )
            )

        items: list[GoogleMapsReprocessItem] = []
        for place_id in sorted(set(latest) & set(mapping_by_place)):
            items.append(
                self._process_one(
                    mapping_by_place[place_id],
                    latest[place_id],
                    run_id,
                    errors,
                )
            )

        succeeded = sum(
            item.status is GoogleMapsReprocessItemStatus.SUCCEEDED for item in items
        )
        resolution_counts = Counter(
            item.resolution_status.value
            for item in items
            if item.resolution_status is not None
        )
        decision_counts = Counter(
            item.decision_status.value
            for item in items
            if item.decision_status is not None
        )
        llm_counts = Counter(
            item.llm_queue_disposition.value
            for item in items
            if item.llm_queue_disposition is not None
        )
        summary = GoogleMapsReprocessSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=self.clock(),
            mapping_count=len(mappings),
            observation_candidate_count=candidate_count,
            selected_observation_count=len(latest),
            processed_count=len(items),
            succeeded_count=succeeded,
            failed_count=len(items) - succeeded,
            coordinate_fallback_count=sum(
                item.coordinate_fallback_applied for item in items
            ),
            published_mapping_count=sum(
                item.current_mapping_path is not None for item in items
            ),
            published_place_count=sum(item.current_place_published for item in items),
            llm_queued_count=llm_counts.get(
                LLMReviewQueueDisposition.QUEUED.value, 0
            ),
            resolution_status_counts=dict(sorted(resolution_counts.items())),
            decision_status_counts=dict(sorted(decision_counts.items())),
            llm_queue_status_counts=dict(sorted(llm_counts.items())),
            missing_mapping_place_ids=missing_mapping,
            missing_observation_place_ids=missing_observation,
            errors=errors,
            items=items,
        )
        summary_path = self.summary_writer.write(summary)
        return GoogleMapsReprocessResult(summary=summary, summary_path=summary_path)

    def _process_one(
        self,
        mapping: ExternalEntityMapping,
        source_observation: GoogleMapsPlaceObservation,
        run_id: str,
        errors: list[GoogleMapsReprocessError],
    ) -> GoogleMapsReprocessItem:
        derived_id = self._derived_observation_id(run_id, source_observation)
        fallback_applied = False
        stage = "derive_observation"
        paths: dict[str, object] = {"validation_paths": []}
        resolution_status = None
        decision_status = None
        llm_receipt: LLMReviewQueueReceipt | None = None
        try:
            observation, fallback_applied = self._derive_observation(
                source_observation, mapping, run_id, derived_id
            )
            stage = "write_normalized"
            paths["normalized_path"] = self.normalized_writer.write(observation)

            stage = "resolve_mapping"
            resolution, effective_mapping = self.resolver.resolve_and_update(
                mapping,
                observation,
                canonical_url=str(observation.source_url),
            )
            resolution_status = resolution.status
            stage = "write_resolution"
            paths["resolution_path"] = self.resolution_writer.write(
                resolution, run_id=run_id
            )

            if (
                resolution.status is MappingResolutionStatus.REVIEW
                and mapping.status is MappingStatus.AUTO_MATCHED
                and self.llm_review_writer is not None
            ):
                stage = "queue_llm_review"
                llm_receipt = self.llm_review_writer.enqueue(
                    resolution, run_id=run_id
                )

            if resolution.status is MappingResolutionStatus.AUTO_CONFIRM:
                stage = "publish_current_mapping"
                effective_mapping = self._newest_confirmed_mapping(effective_mapping)
                paths["current_mapping_path"] = self.current_mapping_writer.publish(
                    effective_mapping
                )
            elif resolution.status is MappingResolutionStatus.REJECT:
                # Persist a hard identity conflict as an overlay.  The manifest
                # loader treats rejected overlays as ineligible, so a later
                # scheduled crawl cannot silently retry and publish the same
                # wrong Google place.
                stage = "publish_rejected_mapping"
                effective_mapping = apply_rejected_resolution(mapping, resolution)
                paths["current_mapping_path"] = self.current_mapping_writer.publish(
                    effective_mapping
                )

            stage = "validate"
            validations = self.validator.validate(
                observation,
                effective_mapping,
                mapping_resolution=resolution,
            )
            stage = "write_validations"
            paths["validation_paths"] = [
                self.validation_writer.write(validation)
                for validation in validations
            ]
            stage = "decide"
            decision = self.decision_gate.decide(observation, validations)
            decision_status = decision.status
            stage = "write_decision"
            paths["decision_path"] = self.decision_writer.write(decision)

            current_place_published = False
            if (
                decision.status is GoogleMapsDecisionStatus.PASS
                and effective_mapping.status is MappingStatus.CONFIRMED
            ):
                stage = "publish_current_place"
                current = self.current_place_writer.get(observation.place_id)
                if current is None or (
                    current.provenance.observed_at < observation.observed_at
                ) or (
                    current.provenance.observed_at == observation.observed_at
                    and current.provenance.source_record_id
                    == observation.source_record_id
                ):
                    paths["current_place_path"] = self.current_place_writer.publish(
                        observation, decision, effective_mapping
                    )
                    current_place_published = True
                else:
                    paths["current_place_path"] = self.current_place_writer.path_for(
                        observation.place_id
                    )

            return self._item(
                mapping,
                source_observation,
                derived_id,
                GoogleMapsReprocessItemStatus.SUCCEEDED,
                fallback_applied=fallback_applied,
                resolution_status=resolution_status,
                decision_status=decision_status,
                llm_receipt=llm_receipt,
                current_place_published=current_place_published,
                paths=paths,
            )
        except Exception as error:
            errors.append(
                self._error(stage, error, place_id=source_observation.place_id)
            )
            return self._item(
                mapping,
                source_observation,
                derived_id,
                GoogleMapsReprocessItemStatus.FAILED,
                fallback_applied=fallback_applied,
                resolution_status=resolution_status,
                decision_status=decision_status,
                llm_receipt=llm_receipt,
                current_place_published=False,
                paths=paths,
                error=f"{type(error).__name__}: {error}",
            )

    def _newest_confirmed_mapping(
        self, candidate: ExternalEntityMapping
    ) -> ExternalEntityMapping:
        current = self.current_mapping_writer.get(candidate.entity_id)
        if current is None or current.verified_at is None:
            return candidate
        if candidate.verified_at is None or current.verified_at > candidate.verified_at:
            return current
        return candidate

    @classmethod
    def _derive_observation(
        cls,
        source: GoogleMapsPlaceObservation,
        mapping: ExternalEntityMapping,
        run_id: str,
        observation_id: str,
    ) -> tuple[GoogleMapsPlaceObservation, bool]:
        location = source.location
        fallback_applied = False
        if location is None:
            latitude = mapping.attributes.get("master_latitude")
            longitude = mapping.attributes.get("master_longitude")
            if cls._coordinate(latitude, -90, 90) and cls._coordinate(
                longitude, -180, 180
            ):
                location = GeoPoint(
                    latitude=float(latitude),
                    longitude=float(longitude),
                    accuracy="verified_master_fallback",
                    source="verified-master-data",
                    verified_at=mapping.verified_at,
                )
                fallback_applied = True

        opening = source.opening.model_copy(
            update={
                "observation_id": f"{observation_id}:opening",
                "run_id": run_id,
            }
        )
        weekly = (
            source.weekly_opening.model_copy(
                update={
                    "observation_id": f"{observation_id}:weekly-opening",
                    "run_id": run_id,
                }
            )
            if source.weekly_opening is not None
            else None
        )
        menu_source = (
            source.menu_source.model_copy(
                update={
                    "observation_id": f"{observation_id}:menu-source",
                    "run_id": run_id,
                }
            )
            if source.menu_source is not None
            else None
        )
        media = (
            source.media.model_copy(
                update={
                    "observation_id": f"{observation_id}:media",
                    "run_id": run_id,
                }
            )
            if source.media is not None
            else None
        )
        derived = source.model_copy(
            update={
                "observation_id": observation_id,
                "run_id": run_id,
                "location": location,
                "opening": opening,
                "weekly_opening": weekly,
                "menu_source": menu_source,
                "media": media,
            }
        )
        return derived, fallback_applied

    @staticmethod
    def _coordinate(value: object, minimum: float, maximum: float) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and minimum <= float(value) <= maximum
        )

    @classmethod
    def _observation_problem(
        cls, observation: GoogleMapsPlaceObservation
    ) -> str | None:
        if observation.source_id != cls.source_id:
            return "Observation is not from google-maps-web"
        name = (observation.name or "").strip()
        if not name or name.casefold() == "google maps":
            return "Google Maps place detail was not resolved"
        if observation.opening.place_id != observation.place_id:
            return "Opening observation belongs to another place"
        if observation.source_record_id not in observation.opening.source_record_ids:
            return "Opening observation does not reference the source record"
        return None

    @staticmethod
    def _latest_by_place(
        observations: Sequence[GoogleMapsPlaceObservation],
    ) -> dict[str, GoogleMapsPlaceObservation]:
        latest: dict[str, GoogleMapsPlaceObservation] = {}
        for observation in observations:
            current = latest.get(observation.place_id)
            key = (observation.observed_at, observation.observation_id)
            if current is None or key > (current.observed_at, current.observation_id):
                latest[observation.place_id] = observation
        return latest

    @classmethod
    def _mapping_index(
        cls,
        mappings: Sequence[ExternalEntityMapping],
        errors: list[GoogleMapsReprocessError],
    ) -> dict[str, ExternalEntityMapping]:
        index: dict[str, ExternalEntityMapping] = {}
        duplicates: set[str] = set()
        for mapping in mappings:
            if mapping.source_id != cls.source_id:
                errors.append(
                    GoogleMapsReprocessError(
                        stage="index_mapping",
                        error_type="InvalidMapping",
                        message="Mapping is not from google-maps-web",
                        place_id=mapping.entity_id,
                    )
                )
                continue
            if mapping.entity_id in index:
                duplicates.add(mapping.entity_id)
            else:
                index[mapping.entity_id] = mapping
        for place_id in sorted(duplicates):
            index.pop(place_id, None)
            errors.append(
                GoogleMapsReprocessError(
                    stage="index_mapping",
                    error_type="DuplicateMapping",
                    message="More than one mapping exists for the place",
                    place_id=place_id,
                )
            )
        return index

    @staticmethod
    def _read_observation(path: Path) -> GoogleMapsPlaceObservation:
        document = json.loads(path.read_text(encoding="utf-8"))
        # A short-lived legacy schema stored a Maps share URL directly in
        # `menu_source`. It is neither a menu image nor a valid current
        # MenuSourceObservation. Menu/OCR is paused, so ignore only this legacy
        # value while preserving the immutable source file on disk.
        if isinstance(document, dict) and isinstance(
            document.get("menu_source"), str
        ):
            document = {**document, "menu_source": None}
        return GoogleMapsPlaceObservation.model_validate(document)

    @staticmethod
    def _observation_paths(
        explicit_paths: Sequence[str | Path], root: str | Path | None
    ) -> list[Path]:
        paths = [Path(path) for path in explicit_paths]
        if root is not None:
            directory = Path(root)
            canonical = directory / "entity=opening_status" / "source=google-maps-web"
            if canonical.exists():
                directory = canonical
            paths.extend(directory.rglob("observation=*.json"))
        return sorted(set(paths), key=lambda path: str(path))

    @staticmethod
    def _derived_observation_id(
        run_id: str, observation: GoogleMapsPlaceObservation
    ) -> str:
        digest = hashlib.sha256(
            f"{run_id}\0{observation.observation_id}".encode()
        ).hexdigest()[:16]
        return f"{observation.observation_id}:quality-reprocess:{digest}"

    @staticmethod
    def _item(
        mapping: ExternalEntityMapping,
        source: GoogleMapsPlaceObservation,
        derived_id: str,
        status: GoogleMapsReprocessItemStatus,
        *,
        fallback_applied: bool,
        resolution_status: MappingResolutionStatus | None,
        decision_status: GoogleMapsDecisionStatus | None,
        llm_receipt: LLMReviewQueueReceipt | None,
        current_place_published: bool,
        paths: dict[str, object],
        error: str | None = None,
    ) -> GoogleMapsReprocessItem:
        def path_value(key: str) -> str | None:
            value = paths.get(key)
            return str(value) if isinstance(value, Path) else None

        validation_paths = paths.get("validation_paths", [])
        return GoogleMapsReprocessItem(
            place_id=source.place_id,
            mapping_id=mapping.mapping_id,
            source_observation_id=source.observation_id,
            derived_observation_id=derived_id,
            source_record_id=source.source_record_id,
            observed_at=source.observed_at,
            status=status,
            coordinate_fallback_applied=fallback_applied,
            resolution_status=resolution_status,
            decision_status=decision_status,
            llm_queue_disposition=(
                llm_receipt.disposition if llm_receipt is not None else None
            ),
            normalized_path=path_value("normalized_path"),
            resolution_path=path_value("resolution_path"),
            validation_paths=[str(path) for path in validation_paths],
            decision_path=path_value("decision_path"),
            current_mapping_path=path_value("current_mapping_path"),
            current_place_path=path_value("current_place_path"),
            current_place_published=current_place_published,
            error=error,
        )

    @staticmethod
    def _error(
        stage: str,
        error: Exception,
        *,
        place_id: str | None = None,
        path: Path | None = None,
    ) -> GoogleMapsReprocessError:
        return GoogleMapsReprocessError(
            stage=stage,
            error_type=type(error).__name__,
            message=str(error) or type(error).__name__,
            place_id=place_id,
            path=str(path) if path else None,
        )

    def _new_run_id(self) -> str:
        timestamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"google-maps-reprocess-{timestamp}-{uuid4().hex[:8]}"
