from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, JsonValue, ValidationError, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.readiness import CanonicalDatasetReadinessReport
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.publishing import (
    CurrentHotelAvailabilitySnapshot,
    CurrentHotelPriceSnapshot,
    GoogleMapsMenuSourceEntry,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
    NormalizedMenu,
    Occupancy,
    OpeningStatusObservation,
)


class CanonicalCompletenessInputError(ValueError):
    """Raised when completeness inputs do not describe one safe snapshot."""


class CanonicalCompletenessAlreadyExistsError(FileExistsError):
    """Raised rather than replacing another immutable completeness audit."""


class CompletenessDomain(StrEnum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    DEFERRED = "deferred"


class CompletenessSeverity(StrEnum):
    BLOCKING = "blocking"
    BACKFILL = "backfill"
    OPERATIONAL = "operational"
    DEFERRED = "deferred"


class CompletenessStatus(StrEnum):
    MISSING = "missing"
    STALE = "stale"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"
    SOURCE_ONLY = "source_only"


class CompletenessField(StrEnum):
    NAME = "name"
    ADDRESS = "address"
    CATEGORY = "category"
    BUSINESS_STATUS = "business_status"
    DESCRIPTION = "description"
    COVER_IMAGE = "cover_image"
    OPENING_SCHEDULE = "opening_schedule"
    REFERENCE_PRICE = "reference_price"
    HOTEL_AMENITIES = "hotel_amenities"
    DAILY_OPENING = "daily_opening"
    HOTEL_MAPPING = "hotel_mapping"
    HOTEL_AVAILABILITY = "hotel_availability"
    HOTEL_PRICE = "hotel_price"
    VERIFIED_MENU = "verified_menu"


class CompletenessArtifactKind(StrEnum):
    CANONICAL_REPLACEMENT = "canonical_replacement"
    CANONICAL_GOOGLE_REFRESH = "canonical_google_refresh"
    CURRENT_PLACE = "current_place"
    GOOGLE_REGISTRY = "google_registry"
    GOOGLE_MAPPING = "google_mapping"
    TRIVAGO_MAPPING = "trivago_mapping"
    TRIVAGO_REGISTRY = "trivago_registry"
    HOTEL_AVAILABILITY = "hotel_availability"
    HOTEL_PRICE = "hotel_price"
    CURRENT_MENU = "current_menu"
    MENU_SOURCE = "menu_source"


_ALL_TYPES = sorted(EntityType, key=lambda item: item.value)
_NON_HOTEL_TYPES = [item for item in _ALL_TYPES if item is not EntityType.HOTEL]
_MENU_TYPES = [EntityType.CAFE, EntityType.RESTAURANT]


class CanonicalCompletenessRule(NexTripModel):
    field: CompletenessField
    entity_types: list[EntityType] = Field(min_length=1)
    domain: CompletenessDomain
    severity: CompletenessSeverity
    ttl_minutes: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_rule(self) -> CanonicalCompletenessRule:
        expected = sorted(set(self.entity_types), key=lambda item: item.value)
        if self.entity_types != expected:
            raise ValueError("completeness rule entity_types must be unique and sorted")
        if self.domain is CompletenessDomain.DYNAMIC and self.ttl_minutes is None:
            raise ValueError("dynamic completeness rules require ttl_minutes")
        if (
            self.domain is not CompletenessDomain.DYNAMIC
            and self.ttl_minutes is not None
        ):
            raise ValueError("only dynamic completeness rules can define a TTL")
        return self


def _policy_payload(rules: list[CanonicalCompletenessRule]) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "rules": [item.model_dump(mode="json") for item in rules],
    }


class CanonicalCompletenessPolicy(NexTripModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    policy_id: str = Field(min_length=1)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rules: list[CanonicalCompletenessRule] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_policy(self) -> CanonicalCompletenessPolicy:
        expected = sorted(
            self.rules,
            key=lambda item: (item.field.value, item.domain.value),
        )
        if self.rules != expected:
            raise ValueError("completeness policy rules must be sorted")
        fields = [item.field for item in self.rules]
        if len(fields) != len(set(fields)):
            raise ValueError("one completeness rule is allowed per field")
        expected_hash = stable_sha256(_policy_payload(self.rules))
        if self.policy_hash != expected_hash:
            raise ValueError("policy_hash does not match completeness rules")
        if self.policy_id != f"canonical-completeness-policy-{expected_hash[:20]}":
            raise ValueError("policy_id does not match policy_hash")
        return self


def default_canonical_completeness_policy() -> CanonicalCompletenessPolicy:
    rules = sorted(
        [
            CanonicalCompletenessRule(
                field=CompletenessField.NAME,
                entity_types=_NON_HOTEL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BLOCKING,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.ADDRESS,
                entity_types=_ALL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BLOCKING,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.BUSINESS_STATUS,
                entity_types=_NON_HOTEL_TYPES,
                domain=CompletenessDomain.DYNAMIC,
                severity=CompletenessSeverity.OPERATIONAL,
                ttl_minutes=1440,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.CATEGORY,
                entity_types=_ALL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BLOCKING,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.DESCRIPTION,
                entity_types=_ALL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.COVER_IMAGE,
                entity_types=_ALL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.OPENING_SCHEDULE,
                entity_types=_NON_HOTEL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.REFERENCE_PRICE,
                entity_types=_NON_HOTEL_TYPES,
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.HOTEL_AMENITIES,
                entity_types=[EntityType.HOTEL],
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.DAILY_OPENING,
                entity_types=_NON_HOTEL_TYPES,
                domain=CompletenessDomain.DYNAMIC,
                severity=CompletenessSeverity.OPERATIONAL,
                ttl_minutes=1440,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.HOTEL_MAPPING,
                entity_types=[EntityType.HOTEL],
                domain=CompletenessDomain.STATIC,
                severity=CompletenessSeverity.BACKFILL,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.HOTEL_AVAILABILITY,
                entity_types=[EntityType.HOTEL],
                domain=CompletenessDomain.DYNAMIC,
                severity=CompletenessSeverity.OPERATIONAL,
                ttl_minutes=300,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.HOTEL_PRICE,
                entity_types=[EntityType.HOTEL],
                domain=CompletenessDomain.DYNAMIC,
                severity=CompletenessSeverity.OPERATIONAL,
                ttl_minutes=300,
            ),
            CanonicalCompletenessRule(
                field=CompletenessField.VERIFIED_MENU,
                entity_types=_MENU_TYPES,
                domain=CompletenessDomain.DEFERRED,
                severity=CompletenessSeverity.DEFERRED,
            ),
        ],
        key=lambda item: (item.field.value, item.domain.value),
    )
    policy_hash = stable_sha256(_policy_payload(rules))
    return CanonicalCompletenessPolicy(
        policy_id=f"canonical-completeness-policy-{policy_hash[:20]}",
        policy_hash=policy_hash,
        rules=rules,
    )


class CanonicalHotelStayContext(NexTripModel):
    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_context(self) -> CanonicalHotelStayContext:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        if self.children_ages != sorted(self.children_ages):
            raise ValueError("children_ages must be sorted")
        return self


class ArtifactInputDigest(NexTripModel):
    kind: CompletenessArtifactKind
    root_label: str = Field(min_length=1)
    file_count: int = Field(ge=0)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactEvidence(NexTripModel):
    kind: CompletenessArtifactKind
    artifact_id: str = Field(min_length=1)
    source_id: str | None = Field(default=None, min_length=1)
    observed_at: AwareDatetime | None = None
    stale_after: AwareDatetime | None = None
    semantic_status: str | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


def _gap_payload(
    *,
    place_id: str,
    entity_type: EntityType,
    city_id: str,
    field: CompletenessField,
    status: CompletenessStatus,
    severity: CompletenessSeverity,
    reason: str,
    evidence: list[ArtifactEvidence],
) -> dict[str, object]:
    return {
        "place_id": place_id,
        "entity_type": entity_type.value,
        "city_id": city_id,
        "field": field.value,
        "status": status.value,
        "severity": severity.value,
        "reason": reason,
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }


class CanonicalCompletenessGap(NexTripModel):
    gap_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    field: CompletenessField
    status: CompletenessStatus
    severity: CompletenessSeverity
    reason: str = Field(min_length=1)
    evidence: list[ArtifactEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_gap(self) -> CanonicalCompletenessGap:
        expected = sorted(
            self.evidence,
            key=lambda item: (item.kind.value, item.artifact_id),
        )
        if self.evidence != expected:
            raise ValueError("gap evidence must be sorted")
        expected_hash = stable_sha256(
            _gap_payload(
                place_id=self.place_id,
                entity_type=self.entity_type,
                city_id=self.city_id,
                field=self.field,
                status=self.status,
                severity=self.severity,
                reason=self.reason,
                evidence=self.evidence,
            )
        )
        if self.gap_hash != expected_hash:
            raise ValueError("gap_hash does not match gap content")
        return self


class CanonicalCompletenessFieldSummary(NexTripModel):
    entity_type: EntityType
    field: CompletenessField
    target_count: int = Field(ge=0)
    present_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    source_only_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> CanonicalCompletenessFieldSummary:
        observed = (
            self.present_count
            + self.missing_count
            + self.stale_count
            + self.unresolved_count
            + self.rejected_count
            + self.source_only_count
        )
        if observed != self.target_count:
            raise ValueError("field summary counts must equal target_count")
        return self


def _audit_payload(
    *,
    schema_version: Literal["1.0.0", "1.1.0"],
    dataset_id: str,
    dataset_hash: str,
    readiness_id: str,
    readiness_hash: str,
    identity_publish_ready: bool,
    identity_review_place_ids: list[str],
    open_vacancy_count: int | None,
    policy: CanonicalCompletenessPolicy,
    as_of: datetime,
    local_date: date,
    stay_context: CanonicalHotelStayContext,
    input_digests: list[ArtifactInputDigest],
    gaps: list[CanonicalCompletenessGap],
    summaries: list[CanonicalCompletenessFieldSummary],
    static_ingest_ready: bool,
    operational_fresh: bool,
    deferred_complete: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "dataset_id": dataset_id,
        "dataset_hash": dataset_hash,
        "readiness_id": readiness_id,
        "readiness_hash": readiness_hash,
        "identity_publish_ready": identity_publish_ready,
        "identity_review_place_ids": identity_review_place_ids,
        "policy": policy.model_dump(mode="json"),
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "local_date": local_date.isoformat(),
        "stay_context": stay_context.model_dump(mode="json"),
        "input_digests": [item.model_dump(mode="json") for item in input_digests],
        "gaps": [item.model_dump(mode="json") for item in gaps],
        "summaries": [item.model_dump(mode="json") for item in summaries],
        "static_ingest_ready": static_ingest_ready,
        "operational_fresh": operational_fresh,
        "deferred_complete": deferred_complete,
    }
    if schema_version == "1.1.0":
        if open_vacancy_count is None:
            raise ValueError("completeness schema 1.1.0 requires open_vacancy_count")
        payload["open_vacancy_count"] = open_vacancy_count
    elif open_vacancy_count is not None:
        raise ValueError("completeness schema 1.0.0 cannot pin open vacancies")
    return payload


class CanonicalCompletenessAudit(NexTripModel):
    schema_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    audit_id: str = Field(min_length=1)
    audit_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    readiness_id: str = Field(min_length=1)
    readiness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_publish_ready: bool
    identity_review_place_ids: list[str] = Field(default_factory=list)
    open_vacancy_count: int | None = Field(
        default=None,
        ge=0,
        exclude_if=lambda value: value is None,
    )
    policy: CanonicalCompletenessPolicy
    as_of: AwareDatetime
    local_date: date
    stay_context: CanonicalHotelStayContext
    input_digests: list[ArtifactInputDigest]
    gaps: list[CanonicalCompletenessGap]
    summaries: list[CanonicalCompletenessFieldSummary]
    static_ingest_ready: bool
    operational_fresh: bool
    deferred_complete: bool

    @model_validator(mode="after")
    def validate_audit(self) -> CanonicalCompletenessAudit:
        if self.identity_review_place_ids != sorted(
            set(self.identity_review_place_ids)
        ):
            raise ValueError("identity review place IDs must be unique and sorted")
        if self.schema_version == "1.1.0":
            if self.open_vacancy_count is None:
                raise ValueError(
                    "completeness schema 1.1.0 requires open_vacancy_count"
                )
            expected_identity_publish_ready = (
                not self.identity_review_place_ids and self.open_vacancy_count == 0
            )
        else:
            if self.open_vacancy_count is not None:
                raise ValueError("completeness schema 1.0.0 cannot pin open vacancies")
            expected_identity_publish_ready = not self.identity_review_place_ids
        if self.identity_publish_ready != expected_identity_publish_ready:
            raise ValueError(
                "identity_publish_ready does not match unresolved identity "
                "places and open vacancies"
            )
        expected_digests = sorted(self.input_digests, key=lambda item: item.kind.value)
        if self.input_digests != expected_digests:
            raise ValueError("input digests must be sorted")
        if len({item.kind for item in self.input_digests}) != len(self.input_digests):
            raise ValueError("input digest kinds must be unique")
        expected_gaps = sorted(
            self.gaps,
            key=lambda item: (item.place_id, item.field.value),
        )
        if self.gaps != expected_gaps:
            raise ValueError("completeness gaps must be sorted")
        gap_keys = [(item.place_id, item.field) for item in self.gaps]
        if len(gap_keys) != len(set(gap_keys)):
            raise ValueError("one completeness gap is allowed per place and field")
        expected_summaries = sorted(
            self.summaries,
            key=lambda item: (item.entity_type.value, item.field.value),
        )
        if self.summaries != expected_summaries:
            raise ValueError("completeness summaries must be sorted")
        blocking = any(
            item.severity is CompletenessSeverity.BLOCKING for item in self.gaps
        )
        operational = any(
            item.severity is CompletenessSeverity.OPERATIONAL for item in self.gaps
        )
        deferred = any(
            item.severity is CompletenessSeverity.DEFERRED for item in self.gaps
        )
        if self.static_ingest_ready == blocking:
            raise ValueError("static_ingest_ready does not match blocking gaps")
        if self.operational_fresh == operational:
            raise ValueError("operational_fresh does not match operational gaps")
        if self.deferred_complete == deferred:
            raise ValueError("deferred_complete does not match deferred gaps")
        payload = _audit_payload(
            schema_version=self.schema_version,
            dataset_id=self.dataset_id,
            dataset_hash=self.dataset_hash,
            readiness_id=self.readiness_id,
            readiness_hash=self.readiness_hash,
            identity_publish_ready=self.identity_publish_ready,
            identity_review_place_ids=self.identity_review_place_ids,
            open_vacancy_count=self.open_vacancy_count,
            policy=self.policy,
            as_of=self.as_of,
            local_date=self.local_date,
            stay_context=self.stay_context,
            input_digests=self.input_digests,
            gaps=self.gaps,
            summaries=self.summaries,
            static_ingest_ready=self.static_ingest_ready,
            operational_fresh=self.operational_fresh,
            deferred_complete=self.deferred_complete,
        )
        expected_hash = stable_sha256(payload)
        if self.audit_hash != expected_hash:
            raise ValueError("audit_hash does not match completeness content")
        if self.audit_id != f"canonical-completeness-{expected_hash[:20]}":
            raise ValueError("audit_id does not match audit_hash")
        return self


class CanonicalSourcePolicy(NexTripModel):
    source_id: str = Field(min_length=1)
    enabled: bool
    schedule_interval_minutes: int = Field(ge=1)
    parser_version: str = Field(min_length=1)


_ModelT = TypeVar("_ModelT", bound=NexTripModel)


def load_artifact_directory(
    root: str | Path,
    model: type[_ModelT],
    *,
    kind: CompletenessArtifactKind,
    recursive: bool = True,
    excluded_directories: Sequence[str] = (),
) -> tuple[list[_ModelT], ArtifactInputDigest]:
    """Strictly load an artifact family and hash its portable input files."""

    directory = Path(root)
    if not directory.exists():
        files: list[Path] = []
    elif not directory.is_dir():
        raise CanonicalCompletenessInputError(
            f"{kind.value} input is not a directory: {directory}"
        )
    else:
        iterator = directory.rglob("*.json") if recursive else directory.glob("*.json")
        files = sorted(
            (
                path
                for path in iterator
                if not set(path.relative_to(directory).parts)
                & set(excluded_directories)
                and not path.name.startswith(".")
            ),
            key=lambda item: item.as_posix(),
        )
    loaded: list[_ModelT] = []
    fingerprints = []
    for path in files:
        content = path.read_bytes()
        try:
            loaded.append(model.model_validate_json(content))
        except (TypeError, ValueError) as error:
            raise CanonicalCompletenessInputError(
                f"invalid {kind.value} artifact {path}: {error}"
            ) from error
        fingerprints.append(
            {
                "relative_path": path.relative_to(directory).as_posix(),
                "file_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return loaded, ArtifactInputDigest(
        kind=kind,
        root_label=kind.value,
        file_count=len(files),
        input_hash=stable_sha256(fingerprints),
    )


def digest_artifact_file(
    path: str | Path,
    *,
    kind: CompletenessArtifactKind,
) -> ArtifactInputDigest:
    artifact = Path(path)
    try:
        content = artifact.read_bytes()
    except OSError as error:
        raise CanonicalCompletenessInputError(
            f"cannot read {kind.value} artifact {artifact}: {error}"
        ) from error
    return ArtifactInputDigest(
        kind=kind,
        root_label=kind.value,
        file_count=1,
        input_hash=stable_sha256(
            [
                {
                    "relative_path": artifact.name,
                    "file_sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
        ),
    )


def build_canonical_completeness_audit(
    dataset: CanonicalActiveDataset,
    readiness: CanonicalDatasetReadinessReport,
    *,
    as_of: datetime,
    stay_context: CanonicalHotelStayContext,
    google_mappings: Iterable[ExternalEntityMapping] = (),
    trivago_mappings: Iterable[ExternalEntityMapping] = (),
    trivago_entries: Iterable[TrivagoHotelRegistryEntry] = (),
    hotel_availability: Iterable[CurrentHotelAvailabilitySnapshot] = (),
    hotel_prices: Iterable[CurrentHotelPriceSnapshot] = (),
    current_menus: Iterable[NormalizedMenu] = (),
    menu_sources: Iterable[GoogleMapsMenuSourceEntry] = (),
    input_digests: Iterable[ArtifactInputDigest] = (),
    policy: CanonicalCompletenessPolicy | None = None,
) -> CanonicalCompletenessAudit:
    """Audit static, contextual, and deferred coverage without crawling."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise CanonicalCompletenessInputError("as_of must be timezone-aware")
    validated_dataset = CanonicalActiveDataset.model_validate_json(
        dataset.model_dump_json()
    )
    validated_readiness = CanonicalDatasetReadinessReport.model_validate_json(
        readiness.model_dump_json()
    )
    if (validated_dataset.dataset_id, validated_dataset.dataset_hash) != (
        validated_readiness.dataset_id,
        validated_readiness.dataset_hash,
    ):
        raise CanonicalCompletenessInputError(
            "dataset and readiness report refer to different snapshots"
        )
    selected_policy = policy or default_canonical_completeness_policy()
    records = {item.place_id: item for item in validated_dataset.records}
    google_by_id = _unique_source_mapping(
        google_mappings,
        "google-maps-web",
        allowed_ids=set(records),
    )
    trivago_by_id = _unique_source_mapping(
        trivago_mappings,
        "trivago-mcp",
        allowed_ids=set(records),
    )
    trivago_entry_by_id = _unique_by_id(
        trivago_entries,
        "entity_id",
        "Trivago registry",
        allowed_ids=set(records),
    )
    menu_by_id = _unique_by_id(
        current_menus, "place_id", "current menu", allowed_ids=set(records)
    )
    menu_source_by_id = _unique_by_id(
        menu_sources, "place_id", "menu source", allowed_ids=set(records)
    )
    prices = [
        CurrentHotelPriceSnapshot.model_validate_json(item.model_dump_json())
        for item in hotel_prices
        if item.hotel_id in records
    ]
    availability = [
        CurrentHotelAvailabilitySnapshot.model_validate_json(item.model_dump_json())
        for item in hotel_availability
        if item.hotel_id in records
    ]
    rule_by_field = {item.field: item for item in selected_policy.rules}
    local_date = as_of.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).date()
    identity_review_place_ids = sorted(
        {
            place_id
            for group in validated_readiness.unresolved_groups
            for place_id in group.active_canonical_ids
        }
    )
    if validated_readiness.schema_version == "1.3.0":
        readiness_open_vacancy_count = validated_readiness.open_vacancy_count
        assert readiness_open_vacancy_count is not None
        if readiness_open_vacancy_count != validated_dataset.report.open_vacancy_count:
            raise CanonicalCompletenessInputError(
                "readiness and dataset disagree on open vacancy count"
            )
    else:
        readiness_open_vacancy_count = validated_dataset.report.open_vacancy_count
    identity_publish_ready = (
        validated_readiness.publish_ready and readiness_open_vacancy_count == 0
    )
    gaps: list[CanonicalCompletenessGap] = []
    for record in validated_dataset.records:
        gaps.extend(
            _place_gaps(
                record,
                google_mapping=google_by_id.get(record.place_id),
                trivago_mapping=trivago_by_id.get(record.place_id),
                trivago_entry=trivago_entry_by_id.get(record.place_id),
                prices=prices,
                availability=availability,
                has_current_menu=record.place_id in menu_by_id,
                menu_source=menu_source_by_id.get(record.place_id),
                rules=rule_by_field,
                as_of=as_of,
                local_date=local_date,
                stay_context=stay_context,
            )
        )
    gaps.sort(key=lambda item: (item.place_id, item.field.value))
    summaries = _summarize(validated_dataset, selected_policy, gaps)
    digests = sorted(
        (
            ArtifactInputDigest.model_validate_json(item.model_dump_json())
            for item in input_digests
        ),
        key=lambda item: item.kind.value,
    )
    if len({item.kind for item in digests}) != len(digests):
        raise CanonicalCompletenessInputError("input digest kinds must be unique")
    static_ready = not any(
        item.severity is CompletenessSeverity.BLOCKING for item in gaps
    )
    operational_fresh = not any(
        item.severity is CompletenessSeverity.OPERATIONAL for item in gaps
    )
    deferred_complete = not any(
        item.severity is CompletenessSeverity.DEFERRED for item in gaps
    )
    payload = _audit_payload(
        schema_version="1.1.0",
        dataset_id=validated_dataset.dataset_id,
        dataset_hash=validated_dataset.dataset_hash,
        readiness_id=validated_readiness.readiness_id,
        readiness_hash=validated_readiness.readiness_hash,
        identity_publish_ready=identity_publish_ready,
        identity_review_place_ids=identity_review_place_ids,
        open_vacancy_count=readiness_open_vacancy_count,
        policy=selected_policy,
        as_of=as_of,
        local_date=local_date,
        stay_context=stay_context,
        input_digests=digests,
        gaps=gaps,
        summaries=summaries,
        static_ingest_ready=static_ready,
        operational_fresh=operational_fresh,
        deferred_complete=deferred_complete,
    )
    audit_hash = stable_sha256(payload)
    return CanonicalCompletenessAudit(
        audit_id=f"canonical-completeness-{audit_hash[:20]}",
        audit_hash=audit_hash,
        **payload,
    )


def _unique_by_id(
    values: Iterable[_ModelT],
    field_name: str,
    label: str,
    *,
    allowed_ids: set[str],
) -> dict[str, _ModelT]:
    result: dict[str, _ModelT] = {}
    for value in values:
        validated = type(value).model_validate_json(value.model_dump_json())
        identity = getattr(validated, field_name)
        if identity not in allowed_ids:
            continue
        if identity in result:
            raise CanonicalCompletenessInputError(
                f"duplicate {label} artifact for {identity}"
            )
        result[identity] = validated
    return result


def _unique_source_mapping(
    values: Iterable[ExternalEntityMapping],
    source_id: str,
    *,
    allowed_ids: set[str],
) -> dict[str, ExternalEntityMapping]:
    filtered = (
        item
        for item in values
        if item.source_id == source_id and item.entity_id in allowed_ids
    )
    return _unique_by_id(
        filtered,
        "entity_id",
        f"{source_id} mapping",
        allowed_ids=allowed_ids,
    )


def _place_gaps(
    record: CanonicalActivePlaceRecord,
    *,
    google_mapping: ExternalEntityMapping | None,
    trivago_mapping: ExternalEntityMapping | None,
    trivago_entry: TrivagoHotelRegistryEntry | None,
    prices: list[CurrentHotelPriceSnapshot],
    availability: list[CurrentHotelAvailabilitySnapshot],
    has_current_menu: bool,
    menu_source: GoogleMapsMenuSourceEntry | None,
    rules: dict[CompletenessField, CanonicalCompletenessRule],
    as_of: datetime,
    local_date: date,
    stay_context: CanonicalHotelStayContext,
) -> list[CanonicalCompletenessGap]:
    gaps: list[CanonicalCompletenessGap] = []
    google_refresh = _canonical_google_refresh(record)
    refresh_evidence = [google_refresh[1]] if google_refresh is not None else []

    def missing_static(
        field: CompletenessField,
        present: bool,
        reason: str,
        evidence: Iterable[ArtifactEvidence] = (),
    ) -> None:
        rule = rules[field]
        if record.primary_type in rule.entity_types and not present:
            gaps.append(
                _make_gap(
                    record,
                    rule,
                    CompletenessStatus.MISSING,
                    reason,
                    evidence,
                )
            )

    missing_static(
        CompletenessField.NAME,
        _present(record.name),
        "canonical place name is missing",
        refresh_evidence,
    )
    missing_static(
        CompletenessField.ADDRESS,
        _present(record.address),
        "canonical address is missing",
    )
    missing_static(
        CompletenessField.CATEGORY,
        _present(record.data.get("google_maps_category"))
        or _present(record.data.get("category")),
        "canonical category and Google Maps category are both missing",
    )
    missing_static(
        CompletenessField.DESCRIPTION,
        _present(record.data.get("description")),
        "source-backed description is missing",
    )
    missing_static(
        CompletenessField.COVER_IMAGE,
        _has_cover(record.data),
        "no source-backed cover image is available",
    )
    missing_static(
        CompletenessField.OPENING_SCHEDULE,
        google_refresh is not None and _google_schedule_evidence_present(record.data),
        "canonical Google Maps refresh has no accepted opening-hours evidence",
        refresh_evidence,
    )
    missing_static(
        CompletenessField.REFERENCE_PRICE,
        _canonical_reference_price_present(record.data)
        or (google_refresh is not None and _observed_google_price_present(record.data)),
        "canonical record has no actual reference price or observed Google price",
        refresh_evidence,
    )
    missing_static(
        CompletenessField.HOTEL_AMENITIES,
        _present(record.data.get("amenities")),
        "hotel amenities are missing",
    )

    if record.primary_type is not EntityType.HOTEL:
        business_status_gap = _business_status_gap(
            record,
            google_refresh=google_refresh,
            rule=rules[CompletenessField.BUSINESS_STATUS],
            as_of=as_of,
        )
        if business_status_gap is not None:
            gaps.append(business_status_gap)
        opening_gap = _opening_gap(
            record,
            google_refresh=google_refresh,
            google_mapping=google_mapping,
            rule=rules[CompletenessField.DAILY_OPENING],
            as_of=as_of,
            local_date=local_date,
        )
        if opening_gap is not None:
            gaps.append(opening_gap)
    else:
        mapping_gap = _hotel_mapping_gap(
            record,
            mapping=trivago_mapping,
            registry_entry=trivago_entry,
            rule=rules[CompletenessField.HOTEL_MAPPING],
        )
        if mapping_gap is not None:
            gaps.append(mapping_gap)
        availability_match = _matching_availability(
            availability,
            record.place_id,
            stay_context,
        )
        availability_gap = _availability_gap(
            record,
            availability_match,
            rules[CompletenessField.HOTEL_AVAILABILITY],
            as_of,
        )
        if availability_gap is not None:
            gaps.append(availability_gap)
        price_gap = _price_gap(
            record,
            _matching_prices(prices, record.place_id, stay_context),
            availability_match,
            rules[CompletenessField.HOTEL_PRICE],
            as_of,
        )
        if price_gap is not None:
            gaps.append(price_gap)

    if record.primary_type in _MENU_TYPES and not has_current_menu:
        rule = rules[CompletenessField.VERIFIED_MENU]
        if menu_source is None:
            gaps.append(
                _make_gap(
                    record,
                    rule,
                    CompletenessStatus.MISSING,
                    "manual menu collection and human verification are pending",
                )
            )
        else:
            evidence = ArtifactEvidence(
                kind=CompletenessArtifactKind.MENU_SOURCE,
                artifact_id=menu_source.source_record_id,
                source_id="google-maps-web",
                observed_at=menu_source.observed_at,
                semantic_status="menu_source_only",
                details={"image_url": str(menu_source.image_url)},
            )
            gaps.append(
                _make_gap(
                    record,
                    rule,
                    CompletenessStatus.SOURCE_ONLY,
                    "menu source exists but human verification is still pending",
                    [evidence],
                )
            )
    return gaps


def _opening_gap(
    record: CanonicalActivePlaceRecord,
    *,
    google_refresh: tuple[Mapping[str, object], ArtifactEvidence] | None,
    google_mapping: ExternalEntityMapping | None,
    rule: CanonicalCompletenessRule,
    as_of: datetime,
    local_date: date,
) -> CanonicalCompletenessGap | None:
    if google_refresh is not None:
        refresh, refresh_evidence = google_refresh
        try:
            opening = OpeningStatusObservation.model_validate(
                record.data.get("opening_status")
            )
        except (TypeError, ValidationError, ValueError):
            opening = None
        if opening is not None and opening.place_id == record.place_id:
            stale_after = refresh_evidence.observed_at + timedelta(
                minutes=rule.ttl_minutes or 1440
            )
            evidence = ArtifactEvidence(
                kind=refresh_evidence.kind,
                artifact_id=refresh_evidence.artifact_id,
                source_id=refresh_evidence.source_id,
                observed_at=refresh_evidence.observed_at,
                stale_after=stale_after,
                semantic_status=opening.status.value,
                details={
                    **refresh_evidence.details,
                    "local_date": opening.local_date.isoformat(),
                    "observation_id": str(refresh.get("observation_id")),
                },
            )
            if opening.status.value == "unknown":
                return _make_gap(
                    record,
                    rule,
                    CompletenessStatus.UNRESOLVED,
                    "canonical Google opening observation is unknown",
                    [evidence],
                )
            if stale_after >= as_of and opening.local_date == local_date:
                return None
            return _make_gap(
                record,
                rule,
                CompletenessStatus.STALE,
                "canonical Google opening observation is outside its daily TTL",
                [evidence],
            )

    mapping_evidence: list[ArtifactEvidence] = []
    if google_refresh is not None:
        mapping_evidence.append(google_refresh[1])
    status = CompletenessStatus.MISSING
    reason = "canonical dataset has no accepted Google daily opening observation"
    if google_mapping is not None:
        mapping_evidence.append(
            ArtifactEvidence(
                kind=CompletenessArtifactKind.GOOGLE_MAPPING,
                artifact_id=google_mapping.mapping_id,
                source_id=google_mapping.source_id,
                observed_at=google_mapping.last_checked_at,
                semantic_status=google_mapping.status.value,
            )
        )
        if google_mapping.status is MappingStatus.REJECTED:
            status = CompletenessStatus.REJECTED
            reason = "Google Maps mapping was rejected and must be remapped"
        elif google_mapping.status is not MappingStatus.CONFIRMED:
            status = CompletenessStatus.UNRESOLVED
            reason = "Google Maps mapping is not confirmed"
    else:
        google_identity = next(
            (
                item
                for item in record.external_identities
                if item.get("source_id") == "google-maps-web"
            ),
            None,
        )
        if google_identity is not None:
            mapping_evidence.append(
                ArtifactEvidence(
                    kind=CompletenessArtifactKind.CANONICAL_REPLACEMENT,
                    artifact_id=record.provenance.approval_id or record.place_id,
                    source_id="google-maps-web",
                    semantic_status="source_backed_identity",
                    details={
                        "external_id": google_identity.get("external_id"),
                        "external_url": google_identity.get("external_url"),
                    },
                )
            )
    return _make_gap(record, rule, status, reason, mapping_evidence)


def _business_status_gap(
    record: CanonicalActivePlaceRecord,
    *,
    google_refresh: tuple[Mapping[str, object], ArtifactEvidence] | None,
    rule: CanonicalCompletenessRule,
    as_of: datetime,
) -> CanonicalCompletenessGap | None:
    if google_refresh is None:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.MISSING,
            "canonical dataset has no source-pinned Google business status",
        )
    _, base_evidence = google_refresh
    status = record.data.get("business_status")
    stale_after = base_evidence.observed_at + timedelta(
        minutes=rule.ttl_minutes or 1440
    )
    evidence = ArtifactEvidence(
        kind=base_evidence.kind,
        artifact_id=base_evidence.artifact_id,
        source_id=base_evidence.source_id,
        observed_at=base_evidence.observed_at,
        stale_after=stale_after,
        semantic_status=status if isinstance(status, str) else None,
        details=base_evidence.details,
    )
    if status not in {"active", "temporarily_closed", "permanently_closed"}:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.UNRESOLVED,
            "canonical Google business status is missing or unknown",
            [evidence],
        )
    if stale_after < as_of:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.STALE,
            "canonical Google business status is outside its daily TTL",
            [evidence],
        )
    return None


def _canonical_google_refresh(
    record: CanonicalActivePlaceRecord,
) -> tuple[Mapping[str, object], ArtifactEvidence] | None:
    refresh = record.data.get("google_maps_refresh")
    if (
        not isinstance(refresh, Mapping)
        or refresh.get("source_id") != "google-maps-web"
    ):
        return None
    source_record_id = refresh.get("source_record_id")
    observed_at = _parse_aware_datetime(refresh.get("observed_at"))
    if (
        not isinstance(source_record_id, str)
        or not source_record_id
        or observed_at is None
    ):
        return None
    details: dict[str, JsonValue] = {}
    for field_name in ("refresh_hash", "observation_id", "run_id", "source_url"):
        value = refresh.get(field_name)
        if isinstance(value, str) and value:
            details[field_name] = value
    return refresh, ArtifactEvidence(
        kind=CompletenessArtifactKind.CANONICAL_GOOGLE_REFRESH,
        artifact_id=source_record_id,
        source_id="google-maps-web",
        observed_at=observed_at,
        semantic_status="accepted_canonical_refresh",
        details=details,
    )


def _google_schedule_evidence_present(data: Mapping[str, object]) -> bool:
    status = data.get("google_maps_schedule_status")
    if status == "not_listed":
        return True
    return status == "observed" and _present(data.get("opening_hours"))


def _canonical_reference_price_present(data: Mapping[str, object]) -> bool:
    return any(
        _present(data.get(field_name))
        for field_name in (
            "ticket_price",
            "price_range",
            "price_per_person",
            "drink_price",
            "entry_fee",
        )
    )


def _observed_google_price_present(data: Mapping[str, object]) -> bool:
    price = data.get("google_maps_price")
    if not isinstance(price, Mapping):
        return False
    status = price.get("status")
    level = price.get("level")
    valid_level = (
        isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 4
    )
    return status == "observed" and (valid_level or _present(price.get("raw_text")))


def _parse_aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def _hotel_mapping_gap(
    record,
    *,
    mapping: ExternalEntityMapping | None,
    registry_entry: TrivagoHotelRegistryEntry | None,
    rule: CanonicalCompletenessRule,
) -> CanonicalCompletenessGap | None:
    evidence: list[ArtifactEvidence] = []
    if mapping is not None:
        evidence.append(
            ArtifactEvidence(
                kind=CompletenessArtifactKind.TRIVAGO_MAPPING,
                artifact_id=mapping.mapping_id,
                source_id=mapping.source_id,
                observed_at=mapping.last_checked_at,
                semantic_status=mapping.status.value,
            )
        )
    if registry_entry is not None:
        evidence.append(
            ArtifactEvidence(
                kind=CompletenessArtifactKind.TRIVAGO_REGISTRY,
                artifact_id=registry_entry.entity_id,
                source_id="trivago-mcp",
                observed_at=registry_entry.matched_at,
                semantic_status=registry_entry.status.value,
            )
        )
    terminal_status = (
        registry_entry.status
        if registry_entry is not None
        and registry_entry.status
        in {
            TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
            TrivagoRegistryStatus.IDENTITY_REVERIFY,
        }
        else None
    )
    if terminal_status is TrivagoRegistryStatus.PROVIDER_NOT_LISTED:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.UNRESOLVED,
            "reviewed hotel has no confirmed Trivago provider listing",
            evidence,
        )
    if terminal_status is TrivagoRegistryStatus.IDENTITY_REVERIFY:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.UNRESOLVED,
            "Trivago hotel identity requires re-verification",
            evidence,
        )
    if mapping is not None and mapping.status is MappingStatus.CONFIRMED:
        return None
    if (
        registry_entry is not None
        and registry_entry.status is TrivagoRegistryStatus.CONFIRMED
    ):
        return None
    if (mapping is not None and mapping.status is MappingStatus.REJECTED) or (
        registry_entry is not None
        and registry_entry.status is TrivagoRegistryStatus.REJECTED
    ):
        return _make_gap(
            record,
            rule,
            CompletenessStatus.REJECTED,
            "Trivago mapping was rejected",
            evidence,
        )
    return _make_gap(
        record,
        rule,
        (CompletenessStatus.UNRESOLVED if evidence else CompletenessStatus.MISSING),
        "Trivago hotel identity is not confirmed",
        evidence,
    )


def _matching_availability(
    values: list[CurrentHotelAvailabilitySnapshot],
    hotel_id: str,
    context: CanonicalHotelStayContext,
) -> CurrentHotelAvailabilitySnapshot | None:
    nights = (context.check_out - context.check_in).days
    matches = [
        item
        for item in values
        if item.hotel_id == hotel_id
        and item.observation.requested_check_in == context.check_in
        and item.observation.fallback_offset_days == 0
        and item.observation.check_in == context.check_in
        and item.observation.check_out == context.check_out
        and item.observation.nights == nights
        and item.observation.occupancy == context.occupancy
        and item.observation.children_ages == context.children_ages
        and item.observation.currency == context.currency
    ]
    return max(
        matches,
        key=lambda item: (item.observation.observed_at, item.updated_at),
        default=None,
    )


def _matching_prices(
    values: list[CurrentHotelPriceSnapshot],
    hotel_id: str,
    context: CanonicalHotelStayContext,
) -> list[CurrentHotelPriceSnapshot]:
    return [
        item
        for item in values
        if item.hotel_id == hotel_id
        and item.observation.check_in == context.check_in
        and item.observation.check_out == context.check_out
        and item.observation.occupancy == context.occupancy
        and item.observation.children_ages == context.children_ages
        and item.observation.currency == context.currency
    ]


def _availability_gap(
    record,
    snapshot: CurrentHotelAvailabilitySnapshot | None,
    rule: CanonicalCompletenessRule,
    as_of: datetime,
) -> CanonicalCompletenessGap | None:
    if snapshot is None:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.MISSING,
            "no hotel availability exists for the exact stay context",
        )
    observation = snapshot.observation
    evidence = ArtifactEvidence(
        kind=CompletenessArtifactKind.HOTEL_AVAILABILITY,
        artifact_id=snapshot.observation_id,
        source_id=observation.source_id,
        observed_at=observation.observed_at,
        stale_after=snapshot.stale_after,
        semantic_status=observation.status.value,
        details={
            "reason": observation.reason.value,
            "fallback_offset_days": observation.fallback_offset_days,
        },
    )
    if snapshot.stale_after < as_of:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.STALE,
            "hotel availability is outside its five-hour TTL",
            [evidence],
        )
    if observation.status.value == "unknown":
        return _make_gap(
            record,
            rule,
            CompletenessStatus.UNRESOLVED,
            "provider result is unknown and cannot prove unavailability",
            [evidence],
        )
    return None


def _price_gap(
    record,
    snapshots: list[CurrentHotelPriceSnapshot],
    availability: CurrentHotelAvailabilitySnapshot | None,
    rule: CanonicalCompletenessRule,
    as_of: datetime,
) -> CanonicalCompletenessGap | None:
    if (
        availability is not None
        and availability.stale_after >= as_of
        and availability.observation.status.value == "unavailable"
    ):
        return None
    latest = max(
        snapshots,
        key=lambda item: (item.observation.observed_at, item.updated_at),
        default=None,
    )
    if latest is None:
        status = (
            CompletenessStatus.UNRESOLVED
            if availability is not None
            and availability.stale_after >= as_of
            and availability.observation.status.value == "unknown"
            else CompletenessStatus.MISSING
        )
        return _make_gap(
            record,
            rule,
            status,
            "no verified price exists for the exact stay context",
        )
    observation = latest.observation
    evidence = ArtifactEvidence(
        kind=CompletenessArtifactKind.HOTEL_PRICE,
        artifact_id=latest.observation_id,
        source_id=observation.source_id,
        observed_at=observation.observed_at,
        stale_after=latest.stale_after,
        semantic_status=observation.availability.value,
        details={"offer_key": observation.offer_key},
    )
    if latest.stale_after < as_of:
        return _make_gap(
            record,
            rule,
            CompletenessStatus.STALE,
            "hotel price is outside its five-hour TTL",
            [evidence],
        )
    if observation.availability.value != "available":
        return _make_gap(
            record,
            rule,
            CompletenessStatus.UNRESOLVED,
            "hotel offer does not contain a verified available price",
            [evidence],
        )
    return None


def _make_gap(
    record,
    rule: CanonicalCompletenessRule,
    status: CompletenessStatus,
    reason: str,
    evidence: Iterable[ArtifactEvidence] = (),
) -> CanonicalCompletenessGap:
    ordered_evidence = sorted(
        evidence,
        key=lambda item: (item.kind.value, item.artifact_id),
    )
    payload = _gap_payload(
        place_id=record.place_id,
        entity_type=record.primary_type,
        city_id=record.city_id,
        field=rule.field,
        status=status,
        severity=rule.severity,
        reason=reason,
        evidence=ordered_evidence,
    )
    return CanonicalCompletenessGap(
        gap_hash=stable_sha256(payload),
        **payload,
    )


def _present(value: object) -> bool:
    return value not in (None, "", [], {})


def _has_cover(data: dict[str, JsonValue]) -> bool:
    if _present(data.get("cover_image_url")):
        return True
    images = data.get("images")
    return isinstance(images, list) and any(_present(item) for item in images)


def _summarize(
    dataset: CanonicalActiveDataset,
    policy: CanonicalCompletenessPolicy,
    gaps: list[CanonicalCompletenessGap],
) -> list[CanonicalCompletenessFieldSummary]:
    counts_by_type = Counter(item.primary_type for item in dataset.records)
    gap_counts = Counter((item.entity_type, item.field, item.status) for item in gaps)
    summaries = []
    for rule in policy.rules:
        for entity_type in rule.entity_types:
            target = counts_by_type[entity_type]
            counts = {
                status: gap_counts[(entity_type, rule.field, status)]
                for status in CompletenessStatus
            }
            non_present = sum(counts.values())
            summaries.append(
                CanonicalCompletenessFieldSummary(
                    entity_type=entity_type,
                    field=rule.field,
                    target_count=target,
                    present_count=target - non_present,
                    missing_count=counts[CompletenessStatus.MISSING],
                    stale_count=counts[CompletenessStatus.STALE],
                    unresolved_count=counts[CompletenessStatus.UNRESOLVED],
                    rejected_count=counts[CompletenessStatus.REJECTED],
                    source_only_count=counts[CompletenessStatus.SOURCE_ONLY],
                )
            )
    return sorted(
        summaries,
        key=lambda item: (item.entity_type.value, item.field.value),
    )


class CanonicalCompletenessWriter:
    """Write one content-addressed completeness audit without replacement."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, audit: CanonicalCompletenessAudit) -> Path:
        return (
            self.output_root
            / f"audit={quote(audit.audit_id, safe='-_.')}"
            / "canonical-completeness-audit.json"
        )

    def write(self, audit: CanonicalCompletenessAudit) -> Path:
        validated = CanonicalCompletenessAudit.model_validate_json(
            audit.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = read_canonical_completeness_audit(destination)
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalCompletenessAlreadyExistsError(
                        f"immutable completeness audit is invalid: {destination}"
                    ) from error
                if existing.audit_hash == validated.audit_hash:
                    return destination
                raise CanonicalCompletenessAlreadyExistsError(
                    f"immutable completeness audit already exists: {destination}"
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


def read_canonical_completeness_audit(
    path: str | Path,
) -> CanonicalCompletenessAudit:
    return CanonicalCompletenessAudit.model_validate_json(Path(path).read_bytes())
