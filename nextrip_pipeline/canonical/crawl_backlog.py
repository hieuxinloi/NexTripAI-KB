from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from nextrip_pipeline.canonical.completeness import (
    ArtifactInputDigest,
    CanonicalCompletenessAudit,
    CanonicalCompletenessGap,
    CanonicalSourcePolicy,
    CompletenessArtifactKind,
    CompletenessField,
    CompletenessStatus,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import EntityType, NexTripModel


class CanonicalCrawlBacklogError(ValueError):
    """Raised when a crawl backlog cannot be derived safely."""


class CanonicalCrawlBacklogAlreadyExistsError(FileExistsError):
    """Raised rather than replacing another immutable crawl backlog."""


class CanonicalCrawlJob(StrEnum):
    GOOGLE_MAPS_PLACE = "google_maps_place"
    TRIVAGO_AVAILABILITY = "trivago_availability"
    MENU_HUMAN_REVIEW = "menu_human_review"
    # Retained so immutable legacy backlogs remain readable. New canonical
    # backlogs never schedule this provider crawl for menu work.
    GOOGLE_MAPS_MENU = "google_maps_menu"


class CanonicalCrawlReason(StrEnum):
    BUSINESS_STATUS_MISSING = "business_status_missing"
    BUSINESS_STATUS_STALE = "business_status_stale"
    DAILY_OPENING_MISSING = "daily_opening_missing"
    DAILY_OPENING_STALE = "daily_opening_stale"
    MAPPING_UNRESOLVED = "mapping_unresolved"
    MAPPING_REJECTED = "mapping_rejected"
    STATIC_BACKFILL = "static_backfill"
    MENU_SOURCE_MISSING = "menu_source_missing"
    MENU_VERIFICATION_PENDING = "menu_verification_pending"
    HOTEL_MAPPING_MISSING = "hotel_mapping_missing"
    HOTEL_AVAILABILITY_MISSING = "hotel_availability_missing"
    HOTEL_AVAILABILITY_STALE = "hotel_availability_stale"
    HOTEL_PRICE_MISSING = "hotel_price_missing"
    HOTEL_PRICE_STALE = "hotel_price_stale"
    PROVIDER_NOT_LISTED = "provider_not_listed"
    IDENTITY_REVERIFY = "identity_reverify"


class CanonicalCrawlBlockedReason(StrEnum):
    SOURCE_DISABLED = "source_disabled"
    SOURCE_NOT_REGISTERED = "source_not_registered"
    IDENTITY_REVIEW = "identity_review"
    MAPPING_REJECTED = "mapping_rejected"
    PROVIDER_NOT_LISTED = "provider_not_listed"
    IDENTITY_REVERIFY = "identity_reverify"
    MANUAL_MENU_VERIFICATION = "manual_menu_verification"


class CanonicalCrawlRequiredField(StrEnum):
    NAME = "name"
    ADDRESS = "address"
    CATEGORY = "category"
    BUSINESS_STATUS = "business_status"
    COVER_IMAGE = "cover_image"
    OPENING_SCHEDULE = "opening_schedule"
    REFERENCE_PRICE = "reference_price"
    DAILY_OPENING = "daily_opening"
    MENU_SOURCE = "menu_source"
    VERIFIED_MENU = "verified_menu"
    HOTEL_MAPPING = "hotel_mapping"
    HOTEL_AVAILABILITY = "hotel_availability"
    HOTEL_PRICE = "hotel_price"


def _task_payload(
    *,
    dedupe_key: str,
    place_id: str,
    entity_type: EntityType,
    city_id: str,
    source_id: str,
    job: CanonicalCrawlJob,
    priority: int,
    schedule_interval_minutes: int,
    due_at: datetime,
    reason_codes: list[CanonicalCrawlReason],
    required_fields: list[CanonicalCrawlRequiredField],
    context: dict[str, JsonValue],
    automatic: bool,
    blocked_reasons: list[CanonicalCrawlBlockedReason],
    completeness_gap_hashes: list[str],
) -> dict[str, object]:
    return {
        "dedupe_key": dedupe_key,
        "place_id": place_id,
        "entity_type": entity_type.value,
        "city_id": city_id,
        "source_id": source_id,
        "job": job.value,
        "priority": priority,
        "schedule_interval_minutes": schedule_interval_minutes,
        "due_at": due_at.isoformat().replace("+00:00", "Z"),
        "reason_codes": [item.value for item in reason_codes],
        "required_fields": [item.value for item in required_fields],
        "context": context,
        "automatic": automatic,
        "blocked_reasons": [item.value for item in blocked_reasons],
        "completeness_gap_hashes": completeness_gap_hashes,
    }


class CanonicalCrawlTask(NexTripModel):
    task_id: str = Field(min_length=1)
    task_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dedupe_key: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    job: CanonicalCrawlJob
    priority: int = Field(ge=0)
    schedule_interval_minutes: int = Field(ge=1)
    due_at: AwareDatetime
    reason_codes: list[CanonicalCrawlReason] = Field(min_length=1)
    required_fields: list[CanonicalCrawlRequiredField] = Field(min_length=1)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    automatic: bool
    blocked_reasons: list[CanonicalCrawlBlockedReason] = Field(default_factory=list)
    completeness_gap_hashes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_task(self) -> CanonicalCrawlTask:
        if self.reason_codes != sorted(
            set(self.reason_codes), key=lambda item: item.value
        ):
            raise ValueError("crawl reason codes must be unique and sorted")
        if self.required_fields != sorted(
            set(self.required_fields), key=lambda item: item.value
        ):
            raise ValueError("crawl required fields must be unique and sorted")
        if self.blocked_reasons != sorted(
            set(self.blocked_reasons), key=lambda item: item.value
        ):
            raise ValueError("crawl blocked reasons must be unique and sorted")
        if self.completeness_gap_hashes != sorted(set(self.completeness_gap_hashes)):
            raise ValueError("completeness gap hashes must be unique and sorted")
        if self.automatic != (not self.blocked_reasons):
            raise ValueError("automatic task state must match blocked reasons")
        payload = _task_payload(
            dedupe_key=self.dedupe_key,
            place_id=self.place_id,
            entity_type=self.entity_type,
            city_id=self.city_id,
            source_id=self.source_id,
            job=self.job,
            priority=self.priority,
            schedule_interval_minutes=self.schedule_interval_minutes,
            due_at=self.due_at,
            reason_codes=self.reason_codes,
            required_fields=self.required_fields,
            context=self.context,
            automatic=self.automatic,
            blocked_reasons=self.blocked_reasons,
            completeness_gap_hashes=self.completeness_gap_hashes,
        )
        expected_hash = stable_sha256(payload)
        if self.task_hash != expected_hash:
            raise ValueError("task_hash does not match crawl task content")
        if self.task_id != f"canonical-crawl-task-{expected_hash[:20]}":
            raise ValueError("task_id does not match task_hash")
        return self


def _backlog_payload(
    *,
    schema_version: Literal["1.0.0", "1.1.0"],
    completeness_id: str,
    completeness_hash: str,
    dataset_id: str,
    dataset_hash: str,
    as_of: datetime,
    policy_version: str,
    source_policies: list[CanonicalSourcePolicy],
    input_digests: list[ArtifactInputDigest],
    tasks: list[CanonicalCrawlTask],
    task_count: int,
    automatic_count: int,
    blocked_count: int,
    manual_count: int,
    job_counts: dict[str, int],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "completeness_id": completeness_id,
        "completeness_hash": completeness_hash,
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "policy_version": policy_version,
        "source_policies": [item.model_dump(mode="json") for item in source_policies],
        "tasks": [item.model_dump(mode="json") for item in tasks],
        "task_count": task_count,
        "automatic_count": automatic_count,
        "blocked_count": blocked_count,
        "manual_count": manual_count,
        "job_counts": job_counts,
    }
    if schema_version == "1.1.0":
        payload["input_digests"] = [
            item.model_dump(mode="json") for item in input_digests
        ]
    return payload


class CanonicalCrawlBacklog(NexTripModel):
    schema_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    backlog_id: str = Field(min_length=1)
    backlog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    completeness_id: str = Field(min_length=1)
    completeness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: AwareDatetime
    policy_version: Literal["canonical-crawl-policy-v1"]
    source_policies: list[CanonicalSourcePolicy]
    input_digests: list[ArtifactInputDigest] = Field(default_factory=list)
    tasks: list[CanonicalCrawlTask]
    task_count: int = Field(ge=0)
    automatic_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    manual_count: int = Field(ge=0)
    job_counts: dict[str, int]

    @model_validator(mode="after")
    def validate_backlog(self) -> CanonicalCrawlBacklog:
        if self.source_policies != sorted(
            self.source_policies, key=lambda item: item.source_id
        ):
            raise ValueError("crawl source policies must be sorted")
        if len({item.source_id for item in self.source_policies}) != len(
            self.source_policies
        ):
            raise ValueError("crawl source policy IDs must be unique")
        if self.input_digests != sorted(
            self.input_digests, key=lambda item: item.kind.value
        ):
            raise ValueError("crawl input digests must be sorted")
        if len({item.kind for item in self.input_digests}) != len(self.input_digests):
            raise ValueError("crawl input digest kinds must be unique")
        expected_tasks = sorted(self.tasks, key=_task_sort_key)
        if self.tasks != expected_tasks:
            raise ValueError("crawl tasks must be sorted deterministically")
        dedupe_keys = [item.dedupe_key for item in self.tasks]
        if len(dedupe_keys) != len(set(dedupe_keys)):
            raise ValueError("crawl task dedupe keys must be unique")
        expected_jobs = dict(
            sorted(Counter(item.job.value for item in self.tasks).items())
        )
        expected_manual = sum(
            CanonicalCrawlBlockedReason.MANUAL_MENU_VERIFICATION in item.blocked_reasons
            for item in self.tasks
        )
        if (
            self.task_count != len(self.tasks)
            or self.automatic_count != sum(item.automatic for item in self.tasks)
            or self.blocked_count != sum(not item.automatic for item in self.tasks)
            or self.manual_count != expected_manual
            or self.job_counts != expected_jobs
        ):
            raise ValueError("crawl backlog counts do not match tasks")
        payload = _backlog_payload(
            schema_version=self.schema_version,
            completeness_id=self.completeness_id,
            completeness_hash=self.completeness_hash,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            as_of=self.as_of,
            policy_version=self.policy_version,
            source_policies=self.source_policies,
            input_digests=self.input_digests,
            tasks=self.tasks,
            task_count=self.task_count,
            automatic_count=self.automatic_count,
            blocked_count=self.blocked_count,
            manual_count=self.manual_count,
            job_counts=self.job_counts,
        )
        expected_hash = stable_sha256(payload)
        if self.backlog_hash != expected_hash:
            raise ValueError("backlog_hash does not match backlog content")
        if self.backlog_id != f"canonical-crawl-backlog-{expected_hash[:20]}":
            raise ValueError("backlog_id does not match backlog_hash")
        return self


def build_canonical_crawl_backlog(
    audit: CanonicalCompletenessAudit,
    source_policies: list[CanonicalSourcePolicy],
) -> CanonicalCrawlBacklog:
    """Coalesce completeness gaps into provider-specific scheduled work."""

    validated = CanonicalCompletenessAudit.model_validate_json(audit.model_dump_json())
    policies = sorted(
        (
            CanonicalSourcePolicy.model_validate_json(item.model_dump_json())
            for item in source_policies
        ),
        key=lambda item: item.source_id,
    )
    if len({item.source_id for item in policies}) != len(policies):
        raise CanonicalCrawlBacklogError("source policy IDs must be unique")
    policy_by_source = {item.source_id: item for item in policies}
    gaps_by_place: dict[str, list[CanonicalCompletenessGap]] = defaultdict(list)
    for gap in validated.gaps:
        gaps_by_place[gap.place_id].append(gap)
    identity_review = set(validated.identity_review_place_ids)
    tasks: list[CanonicalCrawlTask] = []
    for place_id in sorted(gaps_by_place):
        gaps = gaps_by_place[place_id]
        entity_type = gaps[0].entity_type
        if entity_type is EntityType.HOTEL:
            hotel_task = _build_hotel_task(
                gaps,
                audit=validated,
                policy=policy_by_source.get("trivago-mcp"),
                identity_review=place_id in identity_review,
            )
            if hotel_task is not None:
                tasks.append(hotel_task)
        else:
            maps_task = _build_google_task(
                gaps,
                audit=validated,
                policy=policy_by_source.get("google-maps-web"),
                identity_review=place_id in identity_review,
            )
            if maps_task is not None:
                tasks.append(maps_task)
            menu_task = _build_menu_task(
                gaps,
                audit=validated,
            )
            if menu_task is not None:
                tasks.append(menu_task)
    tasks.sort(key=_task_sort_key)
    job_counts = dict(sorted(Counter(item.job.value for item in tasks).items()))
    automatic_count = sum(item.automatic for item in tasks)
    manual_count = sum(
        CanonicalCrawlBlockedReason.MANUAL_MENU_VERIFICATION in item.blocked_reasons
        for item in tasks
    )
    payload = _backlog_payload(
        schema_version="1.1.0",
        completeness_id=validated.audit_id,
        completeness_hash=validated.audit_hash,
        dataset_id=validated.dataset_id,
        dataset_hash=validated.dataset_hash,
        as_of=validated.as_of,
        policy_version="canonical-crawl-policy-v1",
        source_policies=policies,
        input_digests=validated.input_digests,
        tasks=tasks,
        task_count=len(tasks),
        automatic_count=automatic_count,
        blocked_count=len(tasks) - automatic_count,
        manual_count=manual_count,
        job_counts=job_counts,
    )
    backlog_hash = stable_sha256(payload)
    return CanonicalCrawlBacklog(
        backlog_id=f"canonical-crawl-backlog-{backlog_hash[:20]}",
        backlog_hash=backlog_hash,
        **payload,
    )


def _build_google_task(
    gaps: list[CanonicalCompletenessGap],
    *,
    audit: CanonicalCompletenessAudit,
    policy: CanonicalSourcePolicy | None,
    identity_review: bool,
) -> CanonicalCrawlTask | None:
    relevant_fields = {
        CompletenessField.NAME: CanonicalCrawlRequiredField.NAME,
        CompletenessField.ADDRESS: CanonicalCrawlRequiredField.ADDRESS,
        CompletenessField.CATEGORY: CanonicalCrawlRequiredField.CATEGORY,
        CompletenessField.BUSINESS_STATUS: (
            CanonicalCrawlRequiredField.BUSINESS_STATUS
        ),
        CompletenessField.COVER_IMAGE: CanonicalCrawlRequiredField.COVER_IMAGE,
        CompletenessField.OPENING_SCHEDULE: (
            CanonicalCrawlRequiredField.OPENING_SCHEDULE
        ),
        CompletenessField.REFERENCE_PRICE: (
            CanonicalCrawlRequiredField.REFERENCE_PRICE
        ),
        CompletenessField.DAILY_OPENING: CanonicalCrawlRequiredField.DAILY_OPENING,
    }
    selected = [item for item in gaps if item.field in relevant_fields]
    if not selected:
        return None
    record = selected[0]
    reasons: set[CanonicalCrawlReason] = set()
    required = {
        relevant_fields[item.field]
        for item in selected
        if item.field in relevant_fields
    }
    opening_gap = next(
        (item for item in selected if item.field is CompletenessField.DAILY_OPENING),
        None,
    )
    if opening_gap is not None:
        reasons.add(
            CanonicalCrawlReason.DAILY_OPENING_STALE
            if opening_gap.status is CompletenessStatus.STALE
            else CanonicalCrawlReason.DAILY_OPENING_MISSING
        )
        if opening_gap.status is CompletenessStatus.UNRESOLVED:
            reasons.add(CanonicalCrawlReason.MAPPING_UNRESOLVED)
        if opening_gap.status is CompletenessStatus.REJECTED:
            reasons.add(CanonicalCrawlReason.MAPPING_REJECTED)
    business_gap = next(
        (item for item in selected if item.field is CompletenessField.BUSINESS_STATUS),
        None,
    )
    if business_gap is not None:
        reasons.add(
            CanonicalCrawlReason.BUSINESS_STATUS_STALE
            if business_gap.status is CompletenessStatus.STALE
            else CanonicalCrawlReason.BUSINESS_STATUS_MISSING
        )
    if any(
        item.field
        in {
            CompletenessField.NAME,
            CompletenessField.ADDRESS,
            CompletenessField.CATEGORY,
            CompletenessField.COVER_IMAGE,
            CompletenessField.OPENING_SCHEDULE,
            CompletenessField.REFERENCE_PRICE,
        }
        for item in selected
    ):
        reasons.add(CanonicalCrawlReason.STATIC_BACKFILL)
    blocked = _provider_blocks(policy)
    if identity_review:
        blocked.add(CanonicalCrawlBlockedReason.IDENTITY_REVIEW)
    if any(item.status is CompletenessStatus.REJECTED for item in selected):
        blocked.add(CanonicalCrawlBlockedReason.MAPPING_REJECTED)
    context: dict[str, JsonValue] = {"local_date": audit.local_date.isoformat()}
    external_identity = next(
        (
            evidence.details
            for item in selected
            for evidence in item.evidence
            if evidence.kind.value == "canonical_replacement"
        ),
        None,
    )
    if external_identity:
        context["source_backed_external_identity"] = external_identity
    return _make_task(
        record,
        source_id="google-maps-web",
        job=CanonicalCrawlJob.GOOGLE_MAPS_PLACE,
        priority=10 if opening_gap is not None or business_gap is not None else 20,
        schedule_interval_minutes=(
            policy.schedule_interval_minutes if policy is not None else 1440
        ),
        due_at=audit.as_of,
        reasons=reasons,
        required=required,
        context=context,
        blocked=blocked,
        gaps=selected,
    )


def _build_hotel_task(
    gaps: list[CanonicalCompletenessGap],
    *,
    audit: CanonicalCompletenessAudit,
    policy: CanonicalSourcePolicy | None,
    identity_review: bool,
) -> CanonicalCrawlTask | None:
    fields = {
        CompletenessField.HOTEL_MAPPING: CanonicalCrawlRequiredField.HOTEL_MAPPING,
        CompletenessField.HOTEL_AVAILABILITY: (
            CanonicalCrawlRequiredField.HOTEL_AVAILABILITY
        ),
        CompletenessField.HOTEL_PRICE: CanonicalCrawlRequiredField.HOTEL_PRICE,
    }
    selected = [item for item in gaps if item.field in fields]
    if not selected:
        return None
    reasons: set[CanonicalCrawlReason] = set()
    required = {fields[item.field] for item in selected}
    for gap in selected:
        if gap.field is CompletenessField.HOTEL_MAPPING:
            reasons.add(CanonicalCrawlReason.HOTEL_MAPPING_MISSING)
        elif gap.field is CompletenessField.HOTEL_AVAILABILITY:
            reasons.add(
                CanonicalCrawlReason.HOTEL_AVAILABILITY_STALE
                if gap.status is CompletenessStatus.STALE
                else CanonicalCrawlReason.HOTEL_AVAILABILITY_MISSING
            )
        elif gap.field is CompletenessField.HOTEL_PRICE:
            reasons.add(
                CanonicalCrawlReason.HOTEL_PRICE_STALE
                if gap.status is CompletenessStatus.STALE
                else CanonicalCrawlReason.HOTEL_PRICE_MISSING
            )
    blocked = _provider_blocks(policy)
    registry_statuses = {
        evidence.semantic_status
        for gap in selected
        for evidence in gap.evidence
        if evidence.kind is CompletenessArtifactKind.TRIVAGO_REGISTRY
    }
    if "provider_not_listed" in registry_statuses:
        reasons.add(CanonicalCrawlReason.PROVIDER_NOT_LISTED)
        blocked.add(CanonicalCrawlBlockedReason.PROVIDER_NOT_LISTED)
    if "identity_reverify" in registry_statuses:
        reasons.add(CanonicalCrawlReason.IDENTITY_REVERIFY)
        blocked.add(CanonicalCrawlBlockedReason.IDENTITY_REVERIFY)
    if identity_review:
        blocked.add(CanonicalCrawlBlockedReason.IDENTITY_REVIEW)
    if any(item.status is CompletenessStatus.REJECTED for item in selected):
        blocked.add(CanonicalCrawlBlockedReason.MAPPING_REJECTED)
    context = audit.stay_context.model_dump(mode="json")
    context["lookahead_days"] = 1
    return _make_task(
        selected[0],
        source_id="trivago-mcp",
        job=CanonicalCrawlJob.TRIVAGO_AVAILABILITY,
        priority=0,
        schedule_interval_minutes=(
            policy.schedule_interval_minutes if policy is not None else 300
        ),
        due_at=audit.as_of,
        reasons=reasons,
        required=required,
        context=context,
        blocked=blocked,
        gaps=selected,
    )


def _build_menu_task(
    gaps: list[CanonicalCompletenessGap],
    *,
    audit: CanonicalCompletenessAudit,
) -> CanonicalCrawlTask | None:
    gap = next(
        (
            item
            for item in gaps
            if item.field is CompletenessField.VERIFIED_MENU
            and item.status
            in {CompletenessStatus.MISSING, CompletenessStatus.SOURCE_ONLY}
        ),
        None,
    )
    if gap is None:
        return None
    context: dict[str, JsonValue] = {}
    if gap.evidence:
        context.update(gap.evidence[0].details)
        context["source_record_id"] = gap.evidence[0].artifact_id
    return _make_task(
        gap,
        source_id="human-review",
        job=CanonicalCrawlJob.MENU_HUMAN_REVIEW,
        priority=30,
        schedule_interval_minutes=1440,
        due_at=audit.as_of,
        reasons={CanonicalCrawlReason.MENU_VERIFICATION_PENDING},
        required={CanonicalCrawlRequiredField.VERIFIED_MENU},
        context=context,
        blocked={CanonicalCrawlBlockedReason.MANUAL_MENU_VERIFICATION},
        gaps=[gap],
    )


def _provider_blocks(
    policy: CanonicalSourcePolicy | None,
) -> set[CanonicalCrawlBlockedReason]:
    if policy is None:
        return {CanonicalCrawlBlockedReason.SOURCE_NOT_REGISTERED}
    if not policy.enabled:
        return {CanonicalCrawlBlockedReason.SOURCE_DISABLED}
    return set()


def _make_task(
    reference: CanonicalCompletenessGap,
    *,
    source_id: str,
    job: CanonicalCrawlJob,
    priority: int,
    schedule_interval_minutes: int,
    due_at: datetime,
    reasons: set[CanonicalCrawlReason],
    required: set[CanonicalCrawlRequiredField],
    context: dict[str, JsonValue],
    blocked: set[CanonicalCrawlBlockedReason],
    gaps: list[CanonicalCompletenessGap],
) -> CanonicalCrawlTask:
    context_hash = stable_sha256(context)
    dedupe_key = f"{reference.place_id}:{job.value}:{context_hash[:16]}"
    reason_codes = sorted(reasons, key=lambda item: item.value)
    required_fields = sorted(required, key=lambda item: item.value)
    blocked_reasons = sorted(blocked, key=lambda item: item.value)
    gap_hashes = sorted({item.gap_hash for item in gaps})
    payload = _task_payload(
        dedupe_key=dedupe_key,
        place_id=reference.place_id,
        entity_type=reference.entity_type,
        city_id=reference.city_id,
        source_id=source_id,
        job=job,
        priority=priority,
        schedule_interval_minutes=schedule_interval_minutes,
        due_at=due_at,
        reason_codes=reason_codes,
        required_fields=required_fields,
        context=context,
        automatic=not blocked_reasons,
        blocked_reasons=blocked_reasons,
        completeness_gap_hashes=gap_hashes,
    )
    task_hash = stable_sha256(payload)
    return CanonicalCrawlTask(
        task_id=f"canonical-crawl-task-{task_hash[:20]}",
        task_hash=task_hash,
        **payload,
    )


def _task_sort_key(task: CanonicalCrawlTask) -> tuple[object, ...]:
    return (
        task.priority,
        task.due_at,
        task.city_id,
        task.entity_type.value,
        task.place_id,
        task.job.value,
    )


def _claim_payload(
    *,
    backlog_id: str,
    backlog_hash: str,
    dataset_id: str,
    job: CanonicalCrawlJob,
    task_ids: list[str],
    selection_context: dict[str, JsonValue],
) -> dict[str, object]:
    return {
        "backlog_id": backlog_id,
        "backlog_hash": backlog_hash,
        "dataset_id": dataset_id,
        "job": job.value,
        "task_ids": task_ids,
        "selection_context": selection_context,
    }


class CanonicalCrawlExecutionClaim(NexTripModel):
    """Atomic one-shot claim preventing the same backlog slice from replaying."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    claim_id: str = Field(min_length=1)
    claim_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    backlog_id: str = Field(min_length=1)
    backlog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    job: CanonicalCrawlJob
    task_ids: list[str] = Field(min_length=1)
    selection_context: dict[str, JsonValue]
    claimed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_claim(self) -> CanonicalCrawlExecutionClaim:
        if self.task_ids != sorted(set(self.task_ids)):
            raise ValueError("claimed task IDs must be unique and sorted")
        expected_hash = stable_sha256(
            _claim_payload(
                backlog_id=self.backlog_id,
                backlog_hash=self.backlog_hash,
                dataset_id=self.dataset_id,
                job=self.job,
                task_ids=self.task_ids,
                selection_context=self.selection_context,
            )
        )
        if self.claim_hash != expected_hash:
            raise ValueError("claim_hash does not match execution selection")
        if self.claim_id != f"canonical-crawl-claim-{expected_hash[:20]}":
            raise ValueError("claim_id does not match claim_hash")
        expected_run_id = f"canonical-{self.job.value}-{expected_hash[:16]}"
        if self.run_id != expected_run_id:
            raise ValueError("run_id does not match execution claim")
        return self


def build_canonical_crawl_execution_claim(
    backlog: CanonicalCrawlBacklog,
    *,
    job: CanonicalCrawlJob,
    task_ids: Iterable[str],
    selection_context: dict[str, JsonValue],
    claimed_at: datetime,
) -> CanonicalCrawlExecutionClaim:
    if claimed_at.tzinfo is None or claimed_at.utcoffset() is None:
        raise CanonicalCrawlBacklogError("claimed_at must be timezone-aware")
    validated = CanonicalCrawlBacklog.model_validate_json(backlog.model_dump_json())
    selected = sorted(set(task_ids))
    if not selected:
        raise CanonicalCrawlBacklogError("execution claim requires at least one task")
    known = {
        item.task_id for item in validated.tasks if item.job is job and item.automatic
    }
    unknown = sorted(set(selected) - known)
    if unknown:
        raise CanonicalCrawlBacklogError(
            "execution claim includes non-automatic or unknown tasks: "
            + ", ".join(unknown)
        )
    payload = _claim_payload(
        backlog_id=validated.backlog_id,
        backlog_hash=validated.backlog_hash,
        dataset_id=validated.dataset_id,
        job=job,
        task_ids=selected,
        selection_context=selection_context,
    )
    claim_hash = stable_sha256(payload)
    return CanonicalCrawlExecutionClaim(
        claim_id=f"canonical-crawl-claim-{claim_hash[:20]}",
        claim_hash=claim_hash,
        run_id=f"canonical-{job.value}-{claim_hash[:16]}",
        claimed_at=claimed_at,
        **payload,
    )


class CanonicalCrawlExecutionClaimWriter:
    """Atomically claim a deterministic backlog slice once."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, claim: CanonicalCrawlExecutionClaim) -> Path:
        return (
            self.output_root
            / f"backlog={quote(claim.backlog_id, safe='-_.')}"
            / f"job={claim.job.value}"
            / f"claim={quote(claim.claim_id, safe='-_.')}.json"
        )

    def claim(
        self,
        value: CanonicalCrawlExecutionClaim,
    ) -> tuple[Path, bool]:
        validated = CanonicalCrawlExecutionClaim.model_validate_json(
            value.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                existing = CanonicalCrawlExecutionClaim.model_validate_json(
                    destination.read_bytes()
                )
                if existing.claim_hash != validated.claim_hash:
                    raise CanonicalCrawlBacklogAlreadyExistsError(
                        f"crawl claim path contains another selection: {destination}"
                    )
                return destination, False
            serialized = validated.model_dump_json(indent=2) + "\n"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination, True


class CanonicalCrawlBacklogWriter:
    """Write one content-addressed backlog without replacing prior work."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, backlog: CanonicalCrawlBacklog) -> Path:
        return (
            self.output_root
            / f"backlog={quote(backlog.backlog_id, safe='-_.')}"
            / "canonical-crawl-backlog.json"
        )

    def write(self, backlog: CanonicalCrawlBacklog) -> Path:
        validated = CanonicalCrawlBacklog.model_validate_json(backlog.model_dump_json())
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = read_canonical_crawl_backlog(destination)
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalCrawlBacklogAlreadyExistsError(
                        f"immutable crawl backlog is invalid: {destination}"
                    ) from error
                if existing.backlog_hash == validated.backlog_hash:
                    return destination
                raise CanonicalCrawlBacklogAlreadyExistsError(
                    f"immutable crawl backlog already exists: {destination}"
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
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(serialized)
                file.flush()
                os.fsync(file.fileno())
        return destination


def read_canonical_crawl_backlog(path: str | Path) -> CanonicalCrawlBacklog:
    return CanonicalCrawlBacklog.model_validate_json(Path(path).read_bytes())
