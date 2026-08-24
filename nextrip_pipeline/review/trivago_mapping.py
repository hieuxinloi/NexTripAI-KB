from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlparse

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.jobs.trivago_stay_availability import (
    TrivagoStayAvailabilityResult,
)
from nextrip_pipeline.jobs.trivago_stay_batch import TrivagoStayBatchSummary
from nextrip_pipeline.quality.trivago_mapping import (
    TrivagoCandidateEvidence,
    TrivagoDiscoveryResolution,
    TrivagoDiscoveryStatus,
    compute_trivago_resolution_evidence_hash,
)
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    HotelAvailabilityReason,
    NexTripModel,
    SourceRecord,
)


class TrivagoReviewBatchError(ValueError):
    """Raised when a review queue cannot be proven from immutable evidence."""


class TrivagoReviewRecommendation(StrEnum):
    """Conservative machine triage; never a mapping approval."""

    APPROVAL_CANDIDATE = "approval_candidate"
    REJECT_SELECTED_CANDIDATE = "reject_selected_candidate"
    NEEDS_RESEARCH = "needs_research"


class TrivagoReviewAppearance(NexTripModel):
    resolution_path: str = Field(min_length=1)
    resolution_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolution_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_path: str = Field(min_length=1)
    raw_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_record_id: str = Field(min_length=1)
    source_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_id: str = Field(min_length=1)
    search_strategy: str = Field(min_length=1)
    search_query: str | None = None
    resolution_status: TrivagoDiscoveryStatus
    selected_by_resolver: bool
    candidate: TrivagoCandidateEvidence


class TrivagoReviewCandidate(NexTripModel):
    external_id: str = Field(min_length=1)
    property_ids: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    external_urls: list[str] = Field(default_factory=list)
    appearances: list[TrivagoReviewAppearance] = Field(min_length=1)
    selected_review_resolution_paths: list[str] = Field(default_factory=list)
    confirmed_collision_entity_ids: list[str] = Field(default_factory=list)
    shared_review_entity_ids: list[str] = Field(default_factory=list)
    approval_ready: bool = False

    @model_validator(mode="after")
    def validate_candidate(self) -> TrivagoReviewCandidate:
        if self.approval_ready and (
            not self.selected_review_resolution_paths
            or self.confirmed_collision_entity_ids
            or self.shared_review_entity_ids
        ):
            raise ValueError("approval-ready candidate has unresolved collision")
        return self


class TrivagoReviewTask(NexTripModel):
    entity_id: str = Field(min_length=1)
    master_name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    address: str | None = None
    registry_entry_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[TrivagoReviewCandidate] = Field(default_factory=list)
    selected_external_ids: list[str] = Field(default_factory=list)
    recommendation: TrivagoReviewRecommendation
    recommended_external_id: str | None = None
    recommendation_reason_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_recommendation(self) -> TrivagoReviewTask:
        if (
            self.recommendation
            is TrivagoReviewRecommendation.APPROVAL_CANDIDATE
        ) != (self.recommended_external_id is not None):
            raise ValueError(
                "recommended_external_id is required only for approval candidates"
            )
        if self.recommended_external_id is not None and not any(
            candidate.external_id == self.recommended_external_id
            and candidate.approval_ready
            for candidate in self.candidates
        ):
            raise ValueError("recommended candidate is not approval-ready")
        return self


def _batch_payload(batch: TrivagoReviewBatch) -> dict[str, object]:
    return {
        "schema_version": batch.schema_version,
        "generated_at": batch.generated_at.isoformat(),
        "source_batch_run_id": batch.source_batch_run_id,
        "source_batch_summary_path": batch.source_batch_summary_path,
        "source_batch_summary_sha256": batch.source_batch_summary_sha256,
        "registry_path": batch.registry_path,
        "registry_file_sha256": batch.registry_file_sha256,
        "current_mapping_snapshot_hash": batch.current_mapping_snapshot_hash,
        "stay_result_file_hashes": batch.stay_result_file_hashes,
        "task_count": batch.task_count,
        "candidate_count": batch.candidate_count,
        "recommendation_counts": batch.recommendation_counts,
        "warnings": batch.warnings,
        "tasks": [task.model_dump(mode="json") for task in batch.tasks],
    }


class TrivagoReviewBatch(NexTripModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    queue_id: str = Field(min_length=1)
    queue_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    generated_at: AwareDatetime
    source_batch_run_id: str = Field(min_length=1)
    source_batch_summary_path: str = Field(min_length=1)
    source_batch_summary_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_path: str = Field(min_length=1)
    registry_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_mapping_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    stay_result_file_hashes: dict[str, str] = Field(default_factory=dict)
    task_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    recommendation_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    tasks: list[TrivagoReviewTask] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch(self) -> TrivagoReviewBatch:
        if self.task_count != len(self.tasks):
            raise ValueError("task_count does not match tasks")
        if self.candidate_count != sum(
            len(task.candidates) for task in self.tasks
        ):
            raise ValueError("candidate_count does not match tasks")
        expected_counts = dict(
            sorted(Counter(task.recommendation.value for task in self.tasks).items())
        )
        if self.recommendation_counts != expected_counts:
            raise ValueError("recommendation_counts does not match tasks")
        expected_hash = _stable_sha256(_batch_payload(self))
        if self.queue_hash != expected_hash:
            raise ValueError("queue_hash does not match review batch content")
        if self.queue_id != f"trivago-review-{expected_hash[:20]}":
            raise ValueError("queue_id does not match queue_hash")
        return self


class TrivagoReviewBatchWriter:
    """Write an idempotent, content-addressed Trivago review queue."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, batch: TrivagoReviewBatch) -> Path:
        return (
            self.root_directory
            / f"queue={quote(batch.queue_id, safe='-_.')}"
            / "trivago-review-batch.json"
        )

    def write(self, batch: TrivagoReviewBatch) -> Path:
        validated = TrivagoReviewBatch.model_validate_json(batch.model_dump_json())
        destination = self.destination_for(validated)
        serialized = validated.model_dump_json(indent=2) + "\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_text(encoding="utf-8") == serialized:
                return destination
            raise FileExistsError(
                f"immutable Trivago review queue already exists: {destination}"
            )
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        return destination


class TrivagoReviewBatchBuilder:
    """Build review tasks only from one pinned availability batch."""

    strong_name_score = 0.94
    max_strong_distance_m = 500.0

    def build(
        self,
        batch_summary_path: str | Path,
        registry_path: str | Path,
        *,
        current_mapping_directory: str | Path,
        workspace_root: str | Path = ".",
    ) -> TrivagoReviewBatch:
        root = Path(workspace_root).resolve()
        summary_path = self._resolve_path(batch_summary_path, root)
        registry_file = self._resolve_path(registry_path, root)
        summary_bytes = summary_path.read_bytes()
        registry_bytes = registry_file.read_bytes()
        summary = TrivagoStayBatchSummary.model_validate_json(summary_bytes)
        registry = TrivagoHotelRegistry.model_validate_json(registry_bytes)
        entries = {entry.entity_id: entry for entry in registry.entries}
        current_mappings = self._load_current_mappings(
            self._resolve_path(current_mapping_directory, root)
        )
        confirmed_external_owners, confirmed_property_owners = (
            self._confirmed_owners(registry, current_mappings)
        )

        task_evidence: dict[
            str, list[tuple[TrivagoDiscoveryResolution, Path, SourceRecord, Path]]
        ] = defaultdict(list)
        stay_hashes: dict[str, str] = {}
        for item in summary.items:
            if item.result_path is None:
                continue
            result_path = self._resolve_path(item.result_path, root)
            result_bytes = result_path.read_bytes()
            result = TrivagoStayAvailabilityResult.model_validate_json(result_bytes)
            if result.hotel_id != item.hotel_id or result.run_id != item.result_run_id:
                raise TrivagoReviewBatchError(
                    f"stay result provenance mismatch for {item.hotel_id}"
                )
            stay_hashes[self._display_path(result_path, root)] = _sha256(result_bytes)
            for attempt in result.attempts:
                if (
                    attempt.availability.reason
                    is not HotelAvailabilityReason.MAPPING_UNRESOLVED
                ):
                    continue
                raw_by_id = self._read_attempt_raw_records(attempt.artifact_paths, root)
                for artifact in attempt.artifact_paths:
                    resolution_path = self._resolve_path(artifact, root)
                    if not self._is_resolution_path(resolution_path):
                        continue
                    resolution_bytes = resolution_path.read_bytes()
                    resolution = TrivagoDiscoveryResolution.model_validate_json(
                        resolution_bytes
                    )
                    self._validate_resolution(
                        resolution,
                        resolution_path,
                        item.hotel_id,
                        raw_by_id,
                    )
                    raw_record, raw_path = raw_by_id[resolution.source_record_id]
                    task_evidence[item.hotel_id].append(
                        (resolution, resolution_path, raw_record, raw_path)
                    )

        review_hotel_ids = sorted(
            hotel_id
            for hotel_id, resolutions in task_evidence.items()
            if any(
                resolution.status is TrivagoDiscoveryStatus.REVIEW
                for resolution, _, _, _ in resolutions
            )
        )
        missing_entries = sorted(set(review_hotel_ids) - set(entries))
        if missing_entries:
            raise TrivagoReviewBatchError(
                "review evidence references hotels absent from registry: "
                + ", ".join(missing_entries)
            )

        candidate_entities = self._candidate_entity_index(
            review_hotel_ids, task_evidence
        )
        tasks = [
            self._task(
                entries[hotel_id],
                task_evidence[hotel_id],
                root,
                confirmed_external_owners,
                confirmed_property_owners,
                candidate_entities,
            )
            for hotel_id in review_hotel_ids
        ]
        warnings = self._warnings(tasks)
        counts = dict(
            sorted(Counter(task.recommendation.value for task in tasks).items())
        )
        values: dict[str, object] = {
            "schema_version": "1.0.0",
            # Pin generation to the source batch, making rebuilds byte-idempotent.
            "generated_at": summary.finished_at,
            "source_batch_run_id": summary.run_id,
            "source_batch_summary_path": self._display_path(summary_path, root),
            "source_batch_summary_sha256": _sha256(summary_bytes),
            "registry_path": self._display_path(registry_file, root),
            "registry_file_sha256": _sha256(registry_bytes),
            "current_mapping_snapshot_hash": self._mapping_snapshot_hash(
                current_mappings
            ),
            "stay_result_file_hashes": dict(sorted(stay_hashes.items())),
            "task_count": len(tasks),
            "candidate_count": sum(len(task.candidates) for task in tasks),
            "recommendation_counts": counts,
            "warnings": warnings,
            "tasks": tasks,
        }
        provisional = TrivagoReviewBatch.model_construct(
            queue_id="pending",
            queue_hash="0" * 64,
            **values,
        )
        queue_hash = _stable_sha256(_batch_payload(provisional))
        return TrivagoReviewBatch(
            queue_id=f"trivago-review-{queue_hash[:20]}",
            queue_hash=queue_hash,
            **values,
        )

    def _task(
        self,
        entry: TrivagoHotelRegistryEntry,
        evidence: list[
            tuple[TrivagoDiscoveryResolution, Path, SourceRecord, Path]
        ],
        root: Path,
        external_owners: dict[str, set[str]],
        property_owners: dict[str, set[str]],
        candidate_entities: dict[tuple[str, str], set[str]],
    ) -> TrivagoReviewTask:
        grouped: dict[str, list[TrivagoReviewAppearance]] = defaultdict(list)
        property_ids_by_external: dict[str, set[str]] = defaultdict(set)
        names_by_external: dict[str, set[str]] = defaultdict(set)
        urls_by_external: dict[str, set[str]] = defaultdict(set)
        selected_ids: set[str] = set()

        for resolution, resolution_path, record, raw_path in evidence:
            resolution_bytes = resolution_path.read_bytes()
            raw_bytes = raw_path.read_bytes()
            request = record.raw_payload.get("request")
            request_dict = request if isinstance(request, dict) else {}
            arguments = request_dict.get("arguments")
            argument_dict = arguments if isinstance(arguments, dict) else {}
            search_strategy = _text(request_dict.get("search_strategy")) or "unknown"
            search_query = _text(argument_dict.get("query"))
            for candidate in resolution.candidates:
                if not candidate.external_id:
                    continue
                selected = (
                    resolution.selected_external_id == candidate.external_id
                    and resolution.selected_name == candidate.name
                    and resolution.selected_external_url == candidate.external_url
                )
                if selected:
                    selected_ids.add(candidate.external_id)
                appearance = TrivagoReviewAppearance(
                    resolution_path=self._display_path(resolution_path, root),
                    resolution_file_sha256=_sha256(resolution_bytes),
                    resolution_evidence_hash=resolution.evidence_hash,
                    raw_path=self._display_path(raw_path, root),
                    raw_file_sha256=_sha256(raw_bytes),
                    source_record_id=record.source_record_id,
                    source_content_hash=record.content_hash.lower(),
                    run_id=record.run_id,
                    search_strategy=search_strategy,
                    search_query=search_query,
                    resolution_status=resolution.status,
                    selected_by_resolver=selected,
                    candidate=candidate,
                )
                grouped[candidate.external_id].append(appearance)
                if candidate.name:
                    names_by_external[candidate.external_id].add(candidate.name)
                if candidate.external_url:
                    urls_by_external[candidate.external_id].add(candidate.external_url)
                    property_id = _trivago_property_id(candidate.external_url)
                    if property_id:
                        property_ids_by_external[candidate.external_id].add(
                            property_id
                        )

        candidates: list[TrivagoReviewCandidate] = []
        for external_id in sorted(grouped):
            appearances = sorted(
                grouped[external_id],
                key=lambda item: (
                    item.resolution_path,
                    item.source_record_id,
                    item.candidate.name or "",
                ),
            )
            property_ids = sorted(property_ids_by_external[external_id])
            confirmed_collisions = set(external_owners.get(external_id, set()))
            for property_id in property_ids:
                confirmed_collisions.update(property_owners.get(property_id, set()))
            confirmed_collisions.discard(entry.entity_id)
            shared_entities = set(
                candidate_entities.get(("external", external_id), set())
            )
            for property_id in property_ids:
                shared_entities.update(
                    candidate_entities.get(("property", property_id), set())
                )
            shared_entities.discard(entry.entity_id)
            review_paths = sorted(
                {
                    appearance.resolution_path
                    for appearance in appearances
                    if appearance.selected_by_resolver
                    and appearance.resolution_status
                    is TrivagoDiscoveryStatus.REVIEW
                }
            )
            candidates.append(
                TrivagoReviewCandidate(
                    external_id=external_id,
                    property_ids=property_ids,
                    names=sorted(names_by_external[external_id]),
                    external_urls=sorted(urls_by_external[external_id]),
                    appearances=appearances,
                    selected_review_resolution_paths=review_paths,
                    confirmed_collision_entity_ids=sorted(confirmed_collisions),
                    shared_review_entity_ids=sorted(shared_entities),
                    approval_ready=bool(review_paths)
                    and not confirmed_collisions
                    and not shared_entities,
                )
            )

        recommendation, recommended_id, reasons = self._recommend(
            candidates, selected_ids
        )
        return TrivagoReviewTask(
            entity_id=entry.entity_id,
            master_name=entry.master_name,
            city=entry.city,
            address=entry.address,
            registry_entry_hash=_stable_sha256(entry.model_dump(mode="json")),
            candidates=candidates,
            selected_external_ids=sorted(selected_ids),
            recommendation=recommendation,
            recommended_external_id=recommended_id,
            recommendation_reason_codes=reasons,
        )

    def _recommend(
        self,
        candidates: list[TrivagoReviewCandidate],
        selected_ids: set[str],
    ) -> tuple[TrivagoReviewRecommendation, str | None, list[str]]:
        selected = [
            candidate
            for candidate in candidates
            if candidate.external_id in selected_ids
        ]
        strong: list[TrivagoReviewCandidate] = []
        for candidate in selected:
            if not candidate.approval_ready:
                continue
            appearances = [
                appearance
                for appearance in candidate.appearances
                if appearance.selected_by_resolver
                and appearance.resolution_status is TrivagoDiscoveryStatus.REVIEW
            ]
            if any(
                appearance.candidate.city_evidence == "match"
                and appearance.candidate.name_score >= self.strong_name_score
                and (
                    appearance.candidate.distance_from_master_m is None
                    or appearance.candidate.distance_from_master_m
                    <= self.max_strong_distance_m
                )
                for appearance in appearances
            ):
                strong.append(candidate)
        if len(strong) == 1:
            return (
                TrivagoReviewRecommendation.APPROVAL_CANDIDATE,
                strong[0].external_id,
                ["unique_strong_selected_candidate", "no_global_collision"],
            )

        selected_with_collision = [
            candidate
            for candidate in selected
            if candidate.confirmed_collision_entity_ids
        ]
        if selected and len(selected_with_collision) == len(selected):
            return (
                TrivagoReviewRecommendation.REJECT_SELECTED_CANDIDATE,
                None,
                ["all_selected_candidates_have_confirmed_owner"],
            )
        reasons = ["human_identity_evidence_required"]
        if any(candidate.shared_review_entity_ids for candidate in selected):
            reasons.append("candidate_shared_across_review_hotels")
        if any(not candidate.selected_review_resolution_paths for candidate in candidates):
            reasons.append("non_selected_candidates_require_explicit_review")
        return TrivagoReviewRecommendation.NEEDS_RESEARCH, None, reasons

    @staticmethod
    def _read_attempt_raw_records(
        artifact_paths: list[str], root: Path
    ) -> dict[str, tuple[SourceRecord, Path]]:
        records: dict[str, tuple[SourceRecord, Path]] = {}
        for artifact in artifact_paths:
            path = TrivagoReviewBatchBuilder._resolve_path(artifact, root)
            if not TrivagoReviewBatchBuilder._is_raw_path(path):
                continue
            raw_bytes = path.read_bytes()
            record = SourceRecord.model_validate_json(raw_bytes)
            actual_hash = compute_content_hash(record.raw_payload)
            if record.content_hash.lower() != actual_hash:
                raise TrivagoReviewBatchError(
                    f"raw content_hash mismatch: {path}"
                )
            if record.source_record_id in records:
                raise TrivagoReviewBatchError(
                    f"duplicate raw source_record_id in attempt: "
                    f"{record.source_record_id}"
                )
            records[record.source_record_id] = (record, path)
        return records

    @staticmethod
    def _validate_resolution(
        resolution: TrivagoDiscoveryResolution,
        path: Path,
        hotel_id: str,
        raw_by_id: dict[str, tuple[SourceRecord, Path]],
    ) -> None:
        if resolution.entity_id != hotel_id:
            raise TrivagoReviewBatchError(
                f"resolution belongs to another hotel: {path}"
            )
        if resolution.source_record_id not in raw_by_id:
            raise TrivagoReviewBatchError(
                f"resolution has no pinned raw record: {path}"
            )
        if (
            compute_trivago_resolution_evidence_hash(resolution)
            != resolution.evidence_hash
        ):
            raise TrivagoReviewBatchError(
                f"resolution evidence_hash mismatch: {path}"
            )
        record = raw_by_id[resolution.source_record_id][0]
        if record.subject_id != hotel_id or record.source_id != "trivago-mcp":
            raise TrivagoReviewBatchError(
                f"raw record provenance mismatch: {path}"
            )

    @staticmethod
    def _candidate_entity_index(
        hotel_ids: list[str],
        task_evidence: dict[
            str, list[tuple[TrivagoDiscoveryResolution, Path, SourceRecord, Path]]
        ],
    ) -> dict[tuple[str, str], set[str]]:
        index: dict[tuple[str, str], set[str]] = defaultdict(set)
        for hotel_id in hotel_ids:
            for resolution, _, _, _ in task_evidence[hotel_id]:
                for candidate in resolution.candidates:
                    if candidate.external_id:
                        index[("external", candidate.external_id)].add(hotel_id)
                    if candidate.external_url:
                        property_id = _trivago_property_id(candidate.external_url)
                        if property_id:
                            index[("property", property_id)].add(hotel_id)
        return index

    @staticmethod
    def _confirmed_owners(
        registry: TrivagoHotelRegistry,
        mappings: list[ExternalEntityMapping],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        external: dict[str, set[str]] = defaultdict(set)
        properties: dict[str, set[str]] = defaultdict(set)
        for entry in registry.entries:
            if (
                entry.status is TrivagoRegistryStatus.CONFIRMED
                and entry.external_id
            ):
                external[entry.external_id].add(entry.entity_id)
                if entry.external_url:
                    property_id = _trivago_property_id(str(entry.external_url))
                    if property_id:
                        properties[property_id].add(entry.entity_id)
        for mapping in mappings:
            external[mapping.external_id].add(mapping.entity_id)
            if mapping.external_url:
                property_id = _trivago_property_id(str(mapping.external_url))
                if property_id:
                    properties[property_id].add(mapping.entity_id)
        return external, properties

    @staticmethod
    def _load_current_mappings(directory: Path) -> list[ExternalEntityMapping]:
        if not directory.exists():
            return []
        return [
            ExternalEntityMapping.model_validate_json(path.read_bytes())
            for path in sorted(directory.glob("*.json"))
        ]

    @staticmethod
    def _mapping_snapshot_hash(mappings: list[ExternalEntityMapping]) -> str:
        return _stable_sha256(
            [
                mapping.model_dump(mode="json")
                for mapping in sorted(mappings, key=lambda item: item.entity_id)
            ]
        )

    @staticmethod
    def _warnings(tasks: list[TrivagoReviewTask]) -> list[str]:
        shared = {
            candidate.external_id
            for task in tasks
            for candidate in task.candidates
            if candidate.shared_review_entity_ids
        }
        selected_shared = {
            candidate.external_id
            for task in tasks
            for candidate in task.candidates
            if candidate.external_id in task.selected_external_ids
            and candidate.shared_review_entity_ids
        }
        collided = {
            candidate.external_id
            for task in tasks
            for candidate in task.candidates
            if candidate.confirmed_collision_entity_ids
        }
        selected_collided = {
            candidate.external_id
            for task in tasks
            for candidate in task.candidates
            if candidate.external_id in task.selected_external_ids
            and candidate.confirmed_collision_entity_ids
        }
        warnings = []
        if selected_shared:
            warnings.append(
                f"{len(selected_shared)} resolver-selected candidate external IDs "
                "occur in multiple review hotels"
            )
        if selected_collided:
            warnings.append(
                f"{len(selected_collided)} resolver-selected candidate external "
                "IDs/property IDs already have a confirmed owner"
            )
        if shared:
            warnings.append(
                f"{len(shared)} total candidate external IDs occur in multiple "
                "review hotels"
            )
        if collided:
            warnings.append(
                f"{len(collided)} total candidate external IDs/property IDs already "
                "have a confirmed owner"
            )
        return warnings

    @staticmethod
    def _is_resolution_path(path: Path) -> bool:
        return "trivago_mapping" in path.parts and path.suffix == ".json"

    @staticmethod
    def _is_raw_path(path: Path) -> bool:
        return "raw" in path.parts and path.name.startswith("record=")

    @staticmethod
    def _resolve_path(value: str | Path, root: Path) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    @staticmethod
    def _display_path(path: Path, root: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path)


def _trivago_property_id(value: str) -> str | None:
    """Extract the durable numeric accommodation key from a Trivago URL."""

    parsed = urlparse(value)
    search_values = parse_qs(parsed.query).get("search", [])
    for search_value in search_values:
        match = re.search(r"(?:^|;)100-(\d+)(?:;|$)", search_value)
        if match:
            return match.group(1)
    # Some stored/escaped provider URLs do not parse the semicolon query in
    # the same way. The bounded fallback still requires Trivago's 100- marker.
    match = re.search(r"(?:[?&;]|^)search=100-(\d+)", value)
    return match.group(1) if match else None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_sha256(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
