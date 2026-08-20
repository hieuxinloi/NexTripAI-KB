from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator

from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset
from nextrip_pipeline.canonical.evidence import (
    DuplicateEvidenceAudit,
    DuplicateEvidenceStatus,
    DuplicateGroupEvidence,
)
from nextrip_pipeline.canonical.models import (
    DistinctIdentityDecision,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.canonical.resolver import CanonicalIdentityResolver
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import NexTripModel


class CanonicalReadinessInputError(ValueError):
    """Raised when dataset, evidence, and manifest do not form one snapshot."""


class CanonicalReadinessAlreadyExistsError(FileExistsError):
    """Raised rather than overwrite another immutable readiness artifact."""


class CanonicalDatasetNotReadyError(RuntimeError):
    """Raised when a caller attempts to publish an unresolved dataset."""


class ReadinessResolution(StrEnum):
    RESOLVED_MERGE = "resolved_merge"
    RESOLVED_DISTINCT = "resolved_distinct"
    UNRESOLVED = "unresolved"


class ReadinessReason(StrEnum):
    CONFIRMED_MERGED = "confirmed_evidence_resolves_to_one_canonical"
    DISTINCT_SEPARATE = "distinct_evidence_resolves_to_separate_canonicals"
    REVIEW_EXPLICITLY_DISTINCT = "review_evidence_resolved_by_explicit_distinct"
    REVIEW_EXPLICITLY_MERGED = "review_evidence_resolved_by_explicit_merge"
    CONFIRMED_NOT_MERGED = "confirmed_evidence_not_merged"
    DISTINCT_COLLAPSED = "distinct_evidence_collapsed"
    REVIEW_ACTIVE_DISTINCT = "review_ids_remain_distinct_active_identities"
    REVIEW_DISTINCT_COLLAPSED = "review_explicit_distinct_ids_were_collapsed"
    UNKNOWN_IDENTITY = "evidence_references_unknown_identity"


class CanonicalReadinessGroupResult(NexTripModel):
    group_id: str = Field(min_length=1)
    evidence_status: DuplicateEvidenceStatus
    place_ids: list[str] = Field(min_length=2)
    canonical_by_place_id: dict[str, str | None]
    active_canonical_ids: list[str] = Field(default_factory=list)
    unknown_place_ids: list[str] = Field(default_factory=list)
    resolution: ReadinessResolution
    reason: ReadinessReason

    @model_validator(mode="after")
    def validate_group_result(self) -> CanonicalReadinessGroupResult:
        if self.place_ids != sorted(set(self.place_ids)):
            raise ValueError("place_ids must be unique and sorted")
        if list(self.canonical_by_place_id) != self.place_ids:
            raise ValueError("canonical resolution must cover place_ids in order")
        expected_active = sorted(
            {
                canonical_id
                for canonical_id in self.canonical_by_place_id.values()
                if canonical_id is not None
            }
        )
        if self.active_canonical_ids != expected_active:
            raise ValueError("active_canonical_ids do not match resolved identities")
        expected_unknown = sorted(
            place_id
            for place_id, canonical_id in self.canonical_by_place_id.items()
            if canonical_id is None
        )
        if self.unknown_place_ids != expected_unknown:
            raise ValueError("unknown_place_ids do not match unresolved identities")
        if self.resolution is ReadinessResolution.RESOLVED_MERGE:
            if self.unknown_place_ids or len(self.active_canonical_ids) != 1:
                raise ValueError("resolved_merge requires one known canonical identity")
            if self.evidence_status not in {
                DuplicateEvidenceStatus.CONFIRMED,
                DuplicateEvidenceStatus.REVIEW,
            }:
                raise ValueError("only confirmed/review evidence can resolve as merge")
            expected_reason = (
                ReadinessReason.CONFIRMED_MERGED
                if self.evidence_status is DuplicateEvidenceStatus.CONFIRMED
                else ReadinessReason.REVIEW_EXPLICITLY_MERGED
            )
            if self.reason is not expected_reason:
                raise ValueError("resolved_merge reason does not match evidence status")
        elif self.resolution is ReadinessResolution.RESOLVED_DISTINCT:
            if self.unknown_place_ids or len(self.active_canonical_ids) != len(
                self.place_ids
            ):
                raise ValueError(
                    "resolved_distinct requires one active canonical per place ID"
                )
            if self.evidence_status not in {
                DuplicateEvidenceStatus.DISTINCT,
                DuplicateEvidenceStatus.REVIEW,
            }:
                raise ValueError("only distinct/review evidence can resolve as distinct")
            expected_reason = (
                ReadinessReason.DISTINCT_SEPARATE
                if self.evidence_status is DuplicateEvidenceStatus.DISTINCT
                else ReadinessReason.REVIEW_EXPLICITLY_DISTINCT
            )
            if self.reason is not expected_reason:
                raise ValueError("resolved_distinct reason does not match evidence status")
        else:
            expected_reasons = (
                {ReadinessReason.UNKNOWN_IDENTITY}
                if self.unknown_place_ids
                else {
                    DuplicateEvidenceStatus.CONFIRMED: {
                        ReadinessReason.CONFIRMED_NOT_MERGED
                    },
                    DuplicateEvidenceStatus.DISTINCT: {
                        ReadinessReason.DISTINCT_COLLAPSED
                    },
                    DuplicateEvidenceStatus.REVIEW: {
                        ReadinessReason.REVIEW_ACTIVE_DISTINCT,
                        ReadinessReason.REVIEW_DISTINCT_COLLAPSED,
                    },
                }[self.evidence_status]
            )
            if self.reason not in expected_reasons:
                raise ValueError("unresolved reason does not match identity state")
        return self


def _readiness_payload(
    *,
    schema_version: Literal["1.0.0", "1.1.0"],
    dataset_id: str,
    dataset_hash: str,
    audit_id: str,
    audit_hash: str,
    manifest_id: str,
    manifest_hash: str,
    group_count: int,
    resolved_merge: list[CanonicalReadinessGroupResult],
    resolved_distinct: list[CanonicalReadinessGroupResult],
    unresolved_groups: list[CanonicalReadinessGroupResult],
    publish_ready: bool,
    explicit_distinct_decisions: list[DistinctIdentityDecision],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "audit_id": audit_id,
        "audit_hash": audit_hash,
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "group_count": group_count,
        "resolved_merge": [item.model_dump(mode="json") for item in resolved_merge],
        "resolved_distinct": [
            item.model_dump(mode="json") for item in resolved_distinct
        ],
        "unresolved_groups": [
            item.model_dump(mode="json") for item in unresolved_groups
        ],
        "publish_ready": publish_ready,
    }
    if schema_version == "1.1.0":
        payload["explicit_distinct_decisions"] = [
            item.model_dump(mode="json") for item in explicit_distinct_decisions
        ]
    return payload


class CanonicalDatasetReadinessReport(NexTripModel):
    """Content-addressed decision controlling whether V8 may ingest a dataset."""

    schema_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    readiness_id: str = Field(min_length=1)
    readiness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_id: str = Field(min_length=1)
    audit_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_count: int = Field(ge=0)
    resolved_merge: list[CanonicalReadinessGroupResult] = Field(default_factory=list)
    resolved_distinct: list[CanonicalReadinessGroupResult] = Field(
        default_factory=list
    )
    unresolved_groups: list[CanonicalReadinessGroupResult] = Field(
        default_factory=list
    )
    explicit_distinct_decisions: list[DistinctIdentityDecision] = Field(
        default_factory=list
    )
    publish_ready: bool

    @model_validator(mode="after")
    def validate_report(self) -> CanonicalDatasetReadinessReport:
        collections = (
            (ReadinessResolution.RESOLVED_MERGE, self.resolved_merge),
            (ReadinessResolution.RESOLVED_DISTINCT, self.resolved_distinct),
            (ReadinessResolution.UNRESOLVED, self.unresolved_groups),
        )
        all_groups: list[CanonicalReadinessGroupResult] = []
        for expected_resolution, values in collections:
            if values != sorted(values, key=lambda item: item.group_id):
                raise ValueError("readiness group lists must be sorted")
            if any(item.resolution is not expected_resolution for item in values):
                raise ValueError("readiness group is in the wrong result list")
            all_groups.extend(values)
        group_ids = [item.group_id for item in all_groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("each evidence group must appear exactly once")
        if len(all_groups) != self.group_count:
            raise ValueError("readiness groups do not match group_count")
        if self.publish_ready != (not self.unresolved_groups):
            raise ValueError("publish_ready must be false when any group is unresolved")
        if self.schema_version == "1.0.0" and self.explicit_distinct_decisions:
            raise ValueError(
                "schema 1.0.0 cannot contain explicit distinct decisions"
            )
        expected_decisions = sorted(
            self.explicit_distinct_decisions,
            key=lambda item: (item.group_id, item.reason),
        )
        if self.explicit_distinct_decisions != expected_decisions:
            raise ValueError("explicit distinct decisions must be sorted")
        decision_group_ids = [item.group_id for item in expected_decisions]
        if len(decision_group_ids) != len(set(decision_group_ids)):
            raise ValueError(
                "one explicit distinct decision is allowed per candidate group"
            )
        payload = _readiness_payload(
            schema_version=self.schema_version,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            audit_id=self.audit_id,
            audit_hash=self.audit_hash,
            manifest_id=self.manifest_id,
            manifest_hash=self.manifest_hash,
            group_count=self.group_count,
            resolved_merge=self.resolved_merge,
            resolved_distinct=self.resolved_distinct,
            unresolved_groups=self.unresolved_groups,
            publish_ready=self.publish_ready,
            explicit_distinct_decisions=self.explicit_distinct_decisions,
        )
        expected_hash = stable_sha256(payload)
        if self.readiness_hash != expected_hash:
            raise ValueError("readiness_hash does not match report content")
        if self.readiness_id != f"canonical-readiness-{expected_hash[:20]}":
            raise ValueError("readiness_id does not match readiness_hash")
        return self


def evaluate_canonical_dataset_readiness(
    dataset: CanonicalActiveDataset,
    evidence_audit: DuplicateEvidenceAudit,
    resolver: CanonicalIdentityResolver,
    *,
    distinct_decisions: Sequence[DistinctIdentityDecision] = (),
) -> CanonicalDatasetReadinessReport:
    """Fail closed on evidence that is not reflected by canonical identity state."""

    validated_dataset = CanonicalActiveDataset.model_validate_json(
        dataset.model_dump_json()
    )
    audit = DuplicateEvidenceAudit.model_validate_json(
        evidence_audit.model_dump_json()
    )
    manifest = resolver.manifest
    if (validated_dataset.manifest_id, validated_dataset.manifest_hash) != (
        manifest.manifest_id,
        manifest.manifest_hash,
    ):
        raise CanonicalReadinessInputError(
            "dataset and identity resolver refer to different manifests"
        )
    dataset_ids = {item.place_id for item in validated_dataset.records}
    manifest_ids = {item.canonical_place_id for item in manifest.identities}
    if dataset_ids != manifest_ids:
        raise CanonicalReadinessInputError(
            "dataset does not exactly cover resolver active identities"
        )
    _validate_evidence_audit(audit)
    validated_distinct_decisions = _validate_distinct_decisions(
        audit, distinct_decisions
    )
    distinct_group_ids = {
        item.group_id for item in validated_distinct_decisions
    }

    results = [
        _evaluate_group(
            group,
            resolver,
            explicitly_distinct=group.group_id in distinct_group_ids,
        )
        for group in audit.groups
    ]
    resolved_merge = sorted(
        (
            item
            for item in results
            if item.resolution is ReadinessResolution.RESOLVED_MERGE
        ),
        key=lambda item: item.group_id,
    )
    resolved_distinct = sorted(
        (
            item
            for item in results
            if item.resolution is ReadinessResolution.RESOLVED_DISTINCT
        ),
        key=lambda item: item.group_id,
    )
    unresolved = sorted(
        (
            item
            for item in results
            if item.resolution is ReadinessResolution.UNRESOLVED
        ),
        key=lambda item: item.group_id,
    )
    publish_ready = not unresolved
    values: dict[str, object] = {
        "schema_version": "1.1.0",
        "dataset_id": validated_dataset.dataset_id,
        "dataset_hash": validated_dataset.dataset_hash,
        "audit_id": audit.audit_id,
        "audit_hash": audit.audit_hash,
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "group_count": len(results),
        "resolved_merge": resolved_merge,
        "resolved_distinct": resolved_distinct,
        "unresolved_groups": unresolved,
        "explicit_distinct_decisions": validated_distinct_decisions,
        "publish_ready": publish_ready,
    }
    readiness_hash = stable_sha256(_readiness_payload(**values))
    return CanonicalDatasetReadinessReport(
        readiness_id=f"canonical-readiness-{readiness_hash[:20]}",
        readiness_hash=readiness_hash,
        **values,
    )


def require_canonical_dataset_publish_ready(
    report: CanonicalDatasetReadinessReport,
) -> CanonicalDatasetReadinessReport:
    """Return a validated ready report or stop the downstream publish step."""

    validated = CanonicalDatasetReadinessReport.model_validate_json(
        report.model_dump_json()
    )
    if not validated.publish_ready:
        group_ids = ", ".join(
            item.group_id for item in validated.unresolved_groups[:5]
        )
        suffix = "" if len(validated.unresolved_groups) <= 5 else ", ..."
        raise CanonicalDatasetNotReadyError(
            "canonical dataset is not publish-ready: "
            f"{len(validated.unresolved_groups)} unresolved groups"
            + (f" ({group_ids}{suffix})" if group_ids else "")
        )
    return validated


def _evaluate_group(
    group: DuplicateGroupEvidence,
    resolver: CanonicalIdentityResolver,
    *,
    explicitly_distinct: bool = False,
) -> CanonicalReadinessGroupResult:
    place_ids = sorted(set(group.place_ids))
    canonical_by_place_id = {
        place_id: resolver.resolve(place_id) for place_id in place_ids
    }
    unknown = sorted(
        place_id
        for place_id, canonical_id in canonical_by_place_id.items()
        if canonical_id is None
    )
    active = sorted(
        {
            canonical_id
            for canonical_id in canonical_by_place_id.values()
            if canonical_id is not None
        }
    )
    if unknown:
        resolution = ReadinessResolution.UNRESOLVED
        reason = ReadinessReason.UNKNOWN_IDENTITY
    elif group.status is DuplicateEvidenceStatus.CONFIRMED:
        if len(active) == 1:
            resolution = ReadinessResolution.RESOLVED_MERGE
            reason = ReadinessReason.CONFIRMED_MERGED
        else:
            resolution = ReadinessResolution.UNRESOLVED
            reason = ReadinessReason.CONFIRMED_NOT_MERGED
    elif group.status is DuplicateEvidenceStatus.DISTINCT:
        if len(active) == len(place_ids):
            resolution = ReadinessResolution.RESOLVED_DISTINCT
            reason = ReadinessReason.DISTINCT_SEPARATE
        else:
            resolution = ReadinessResolution.UNRESOLVED
            reason = ReadinessReason.DISTINCT_COLLAPSED
    elif explicitly_distinct:
        if len(active) == len(place_ids):
            resolution = ReadinessResolution.RESOLVED_DISTINCT
            reason = ReadinessReason.REVIEW_EXPLICITLY_DISTINCT
        else:
            resolution = ReadinessResolution.UNRESOLVED
            reason = ReadinessReason.REVIEW_DISTINCT_COLLAPSED
    elif len(active) == 1:
        resolution = ReadinessResolution.RESOLVED_MERGE
        reason = ReadinessReason.REVIEW_EXPLICITLY_MERGED
    else:
        resolution = ReadinessResolution.UNRESOLVED
        reason = ReadinessReason.REVIEW_ACTIVE_DISTINCT
    return CanonicalReadinessGroupResult(
        group_id=group.group_id,
        evidence_status=group.status,
        place_ids=place_ids,
        canonical_by_place_id=canonical_by_place_id,
        active_canonical_ids=active,
        unknown_place_ids=unknown,
        resolution=resolution,
        reason=reason,
    )


def _validate_distinct_decisions(
    audit: DuplicateEvidenceAudit,
    decisions: Sequence[DistinctIdentityDecision],
) -> list[DistinctIdentityDecision]:
    """Bind reviewed DISTINCT decisions to exact audited candidate groups."""

    validated = [
        DistinctIdentityDecision.model_validate_json(item.model_dump_json())
        for item in decisions
    ]
    group_ids = [item.group_id for item in validated]
    if len(group_ids) != len(set(group_ids)):
        raise CanonicalReadinessInputError(
            "one distinct decision is allowed per candidate group"
        )
    audit_by_group_id = {item.group_id: item for item in audit.groups}
    unknown = sorted(set(group_ids) - set(audit_by_group_id))
    if unknown:
        raise CanonicalReadinessInputError(
            "distinct decisions reference unknown evidence groups: "
            + ", ".join(unknown)
        )
    for decision in validated:
        group = audit_by_group_id[decision.group_id]
        if decision.place_ids != group.place_ids:
            raise CanonicalReadinessInputError(
                "distinct decision does not exactly cover evidence group: "
                + decision.group_id
            )
        if group.status is DuplicateEvidenceStatus.CONFIRMED:
            raise CanonicalReadinessInputError(
                "distinct decision conflicts with confirmed duplicate evidence: "
                + decision.group_id
            )
    return sorted(validated, key=lambda item: (item.group_id, item.reason))


def _validate_evidence_audit(audit: DuplicateEvidenceAudit) -> None:
    groups = sorted(audit.groups, key=lambda item: item.group_id)
    if audit.groups != groups:
        raise CanonicalReadinessInputError("evidence groups must be sorted")
    group_ids = [item.group_id for item in groups]
    if len(group_ids) != len(set(group_ids)):
        raise CanonicalReadinessInputError("evidence group IDs must be unique")
    if audit.group_count != len(groups):
        raise CanonicalReadinessInputError("evidence group_count is invalid")
    for group in groups:
        place_ids = sorted(set(group.place_ids))
        if group.place_ids != place_ids:
            raise CanonicalReadinessInputError(
                f"evidence group place IDs are invalid: {group.group_id}"
            )
        expected_group_id = stable_identifier("duplicate_group", *place_ids)
        if group.group_id != expected_group_id:
            raise CanonicalReadinessInputError(
                f"evidence group_id is invalid: {group.group_id}"
            )
        if sorted(item.place_id for item in group.master_identities) != place_ids:
            raise CanonicalReadinessInputError(
                f"evidence master identities are incomplete: {group.group_id}"
            )
        observed_ids = [item.place_id for item in group.google_observations]
        if len(observed_ids) != len(set(observed_ids)) or not set(
            observed_ids
        ).issubset(place_ids):
            raise CanonicalReadinessInputError(
                f"evidence Google identities are invalid: {group.group_id}"
            )
        expected_missing = sorted(set(place_ids) - set(observed_ids))
        if group.missing_google_observation_place_ids != expected_missing:
            raise CanonicalReadinessInputError(
                f"evidence group missing summary is invalid: {group.group_id}"
            )
        expected_pairs = set(combinations(place_ids, 2))
        actual_pairs = {
            (item.left_place_id, item.right_place_id)
            for item in group.pair_evidence
        }
        if actual_pairs != expected_pairs or len(group.pair_evidence) != len(
            expected_pairs
        ):
            raise CanonicalReadinessInputError(
                f"evidence pair coverage is invalid: {group.group_id}"
            )
        pair_statuses = {item.status for item in group.pair_evidence}
        expected_status = (
            DuplicateEvidenceStatus.CONFIRMED
            if pair_statuses == {DuplicateEvidenceStatus.CONFIRMED}
            else (
                DuplicateEvidenceStatus.DISTINCT
                if pair_statuses == {DuplicateEvidenceStatus.DISTINCT}
                else DuplicateEvidenceStatus.REVIEW
            )
        )
        if group.status is not expected_status:
            raise CanonicalReadinessInputError(
                f"evidence group status is invalid: {group.group_id}"
            )
    counts = Counter(item.status.value for item in groups)
    expected_counts = {
        status.value: counts[status.value] for status in DuplicateEvidenceStatus
    }
    if audit.status_counts != expected_counts:
        raise CanonicalReadinessInputError("evidence status_counts are invalid")
    expected_missing = sorted(
        {
            place_id
            for group in groups
            for place_id in group.missing_google_observation_place_ids
        }
    )
    if audit.missing_google_observation_place_ids != expected_missing:
        raise CanonicalReadinessInputError(
            "evidence missing-observation summary is invalid"
        )
    payload = {
        "schema_version": audit.schema_version,
        "groups": [item.model_dump(mode="json") for item in groups],
    }
    expected_hash = stable_sha256(payload)
    if audit.audit_hash != expected_hash:
        raise CanonicalReadinessInputError("evidence audit_hash is invalid")
    if audit.audit_id != f"duplicate_evidence_{expected_hash[:20]}":
        raise CanonicalReadinessInputError("evidence audit_id is invalid")


class CanonicalDatasetReadinessWriter:
    """Persist one content-addressed readiness report without overwriting."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, report: CanonicalDatasetReadinessReport) -> Path:
        return (
            self.output_root
            / f"readiness={quote(report.readiness_id, safe='-_.')}"
            / "canonical-dataset-readiness.json"
        )

    def write(self, report: CanonicalDatasetReadinessReport) -> Path:
        validated = CanonicalDatasetReadinessReport.model_validate_json(
            report.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = CanonicalDatasetReadinessReport.model_validate_json(
                        destination.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalReadinessAlreadyExistsError(
                        f"immutable readiness path is invalid: {destination}"
                    ) from error
                if existing.readiness_hash == validated.readiness_hash:
                    return destination
                raise CanonicalReadinessAlreadyExistsError(
                    f"immutable readiness path already exists: {destination}"
                )
            serialized = (
                json.dumps(
                    validated.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination


def read_canonical_dataset_readiness(
    path: str | Path,
) -> CanonicalDatasetReadinessReport:
    return CanonicalDatasetReadinessReport.model_validate_json(
        Path(path).read_bytes()
    )
