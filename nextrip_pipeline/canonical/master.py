from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from nextrip_pipeline.schemas import EntityType, NexTripModel

from .manifest import read_canonical_identity_manifest
from .models import (
    CanonicalIdentityManifest,
    DistinctIdentityDecision,
    DuplicateIdentityDecision,
    EntityCityQuota,
    LegacyPlaceSlot,
    VacancyReplacementDecision,
    VacancyStatus,
    stable_sha256,
)
from .resolver import build_canonical_identity_manifest


MASTER_FILES: tuple[tuple[EntityType, str, str], ...] = (
    (EntityType.ATTRACTION, "attraction_final.json", "attr"),
    (EntityType.CAFE, "cafe_final.json", "cafe"),
    (EntityType.HOTEL, "hotel_final.json", "hotel"),
    (EntityType.NIGHTLIFE, "nightlife_final.json", "night"),
    (EntityType.RESTAURANT, "restaurant_final.json", "rest"),
)

# Deliberately keep the real Vietnamese Unicode spellings here. NFC permits a
# canonically equivalent decomposed input without accepting unaccented aliases.
_CITY_SPECS: dict[str, tuple[str, str, str]] = {
    "Đà Nẵng": ("city_da_nang", "dn", "da_nang_count"),
    "Quy Nhơn": ("city_quy_nhon", "qn", "quy_nhon_count"),
}
_PLACE_ID = re.compile(
    r"^(?P<entity>attr|cafe|hotel|night|rest)_"
    r"(?P<city>dn|qn)_(?P<sequence>[0-9]{3,})$"
)
_SHA256 = r"^[0-9a-f]{64}$"
_DUPLICATE_MARKER = "duplicate_record"
_DUPLICATE_TARGET_PREFIX = "duplicate_of_"


class CanonicalMasterDataError(ValueError):
    """Raised when verified master inputs cannot safely define identities."""


class MasterQuotaCount(NexTripModel):
    source_filename: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    count: int = Field(ge=0)


class MasterSourceFileAudit(NexTripModel):
    source_filename: str = Field(min_length=1)
    entity_type: EntityType
    content_hash: str = Field(pattern=_SHA256)
    record_count: int = Field(ge=0)
    quota_counts: list[MasterQuotaCount]

    @model_validator(mode="after")
    def validate_counts(self) -> MasterSourceFileAudit:
        if any(item.source_filename != self.source_filename for item in self.quota_counts):
            raise ValueError("quota source filename must match its source audit")
        if any(item.entity_type is not self.entity_type for item in self.quota_counts):
            raise ValueError("quota entity type must match its source audit")
        if sum(item.count for item in self.quota_counts) != self.record_count:
            raise ValueError("source quota counts must sum to record_count")
        return self


class MasterRawRecord(NexTripModel):
    place_id: str = Field(min_length=1)
    source_filename: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    raw_record: dict[str, Any]


class CanonicalMasterLoad(NexTripModel):
    """Validated slots plus an immutable-on-disk raw provenance lookup."""

    slots: list[LegacyPlaceSlot]
    raw_records_by_id: dict[str, MasterRawRecord]
    source_files: list[MasterSourceFileAudit]
    quota_counts: list[MasterQuotaCount]
    explicit_duplicate_decisions: list[DuplicateIdentityDecision]

    def raw_record(self, place_id: str) -> MasterRawRecord | None:
        return self.raw_records_by_id.get(place_id)


class DuplicateIdentityDecisionDocument(NexTripModel):
    schema_version: Literal["1.0.0"]
    decisions: list[DuplicateIdentityDecision] = Field(default_factory=list)
    distinct_decisions: list[DistinctIdentityDecision] = Field(default_factory=list)
    replacements: list[VacancyReplacementDecision] = Field(default_factory=list)

    @field_validator("decisions")
    @classmethod
    def sort_decisions(
        cls, values: list[DuplicateIdentityDecision]
    ) -> list[DuplicateIdentityDecision]:
        return sorted(
            values,
            key=lambda item: (
                item.keeper_legacy_place_id,
                item.duplicate_legacy_place_ids,
                item.reason,
            ),
        )

    @field_validator("replacements")
    @classmethod
    def sort_replacements(
        cls, values: list[VacancyReplacementDecision]
    ) -> list[VacancyReplacementDecision]:
        return sorted(
            values,
            key=lambda item: (
                item.retired_place_id,
                item.replacement_place_id,
                item.reason,
            ),
        )

    @field_validator("distinct_decisions")
    @classmethod
    def sort_distinct_decisions(
        cls, values: list[DistinctIdentityDecision]
    ) -> list[DistinctIdentityDecision]:
        ordered = sorted(values, key=lambda item: (item.place_ids, item.reason))
        group_ids = [item.group_id for item in ordered]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("one distinct decision is allowed per candidate group")
        return ordered

    @property
    def document_hash(self) -> str:
        return stable_sha256(self.model_dump(mode="json"))


class CanonicalMasterBuildReport(NexTripModel):
    """Content-addressed audit of master, decisions, and identity output."""

    schema_version: str = "1.0.0"
    report_hash: str = Field(pattern=_SHA256)
    source_files: list[MasterSourceFileAudit]
    source_record_count: int = Field(ge=0)
    source_quota_counts: list[MasterQuotaCount]
    explicit_duplicate_decisions: list[DuplicateIdentityDecision]
    decision_filename: str = Field(min_length=1)
    decision_schema_version: str = Field(min_length=1)
    decision_document_hash: str = Field(pattern=_SHA256)
    file_duplicate_decisions: list[DuplicateIdentityDecision]
    file_distinct_decisions: list[DistinctIdentityDecision] = Field(
        default_factory=list
    )
    file_replacement_decisions: list[VacancyReplacementDecision]
    include_tagged_duplicates: bool
    applied_duplicate_decisions: list[DuplicateIdentityDecision]
    applied_replacement_decisions: list[VacancyReplacementDecision]
    approved_replacement_count: int = Field(default=0, ge=0)
    approved_replacement_ids: list[str] = Field(default_factory=list)
    approved_replacement_hashes: list[str] = Field(default_factory=list)
    previous_manifest_id: str | None = None
    previous_manifest_hash: str | None = Field(default=None, pattern=_SHA256)
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=_SHA256)
    canonical_place_count: int = Field(ge=0)
    retired_place_id_count: int = Field(ge=0)
    vacancy_count: int = Field(ge=0)
    open_vacancy_count: int = Field(ge=0)
    filled_vacancy_count: int = Field(ge=0)
    output_quotas: list[EntityCityQuota]

    @model_validator(mode="after")
    def validate_report(self) -> CanonicalMasterBuildReport:
        if self.source_record_count != sum(
            item.record_count for item in self.source_files
        ):
            raise ValueError("source_record_count does not match source files")
        if self.source_quota_counts != sorted(
            self.source_quota_counts,
            key=lambda item: (
                item.entity_type.value,
                item.city_id,
                item.source_filename,
            ),
        ):
            raise ValueError("source_quota_counts must use deterministic ordering")
        if self.vacancy_count != (
            self.open_vacancy_count + self.filled_vacancy_count
        ):
            raise ValueError(
                "vacancy_count must equal open_vacancy_count + "
                "filled_vacancy_count"
            )
        expected_hash = stable_sha256(
            self.model_dump(mode="json", exclude={"report_hash"})
        )
        if self.report_hash != expected_hash:
            raise ValueError("report_hash does not match report content")
        return self


def load_verified_master(master_directory: str | Path) -> CanonicalMasterLoad:
    """Strictly load all five verified master files without mutating them."""

    root = Path(master_directory)
    missing = [filename for _, filename, _ in MASTER_FILES if not (root / filename).is_file()]
    if missing:
        raise CanonicalMasterDataError(
            "verified master files are missing: " + ", ".join(missing)
        )

    slots: list[LegacyPlaceSlot] = []
    raw_records: dict[str, MasterRawRecord] = {}
    source_audits: list[MasterSourceFileAudit] = []
    all_quota_counts: list[MasterQuotaCount] = []
    duplicate_tags_by_id: dict[str, tuple[str, ...]] = {}

    for expected_type, filename, expected_prefix in MASTER_FILES:
        path = root / filename
        raw_bytes = path.read_bytes()
        try:
            document = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CanonicalMasterDataError(f"{filename}: invalid UTF-8 JSON: {error}") from error
        if not isinstance(document, dict):
            raise CanonicalMasterDataError(f"{filename}: root must be an object")
        metadata = document.get("metadata")
        records = document.get("data")
        if not isinstance(metadata, dict):
            raise CanonicalMasterDataError(f"{filename}: metadata must be an object")
        if not isinstance(records, list):
            raise CanonicalMasterDataError(f"{filename}: data must be an array")

        _validate_metadata_header(
            metadata,
            filename=filename,
            expected_type=expected_type,
            record_count=len(records),
        )
        file_counts: Counter[str] = Counter()
        for index, raw_record in enumerate(records):
            if not isinstance(raw_record, dict):
                raise CanonicalMasterDataError(
                    f"{filename}[{index}]: record must be an object"
                )
            slot, city_name, control_tags = _slot_from_record(
                raw_record,
                filename=filename,
                record_index=index,
                expected_type=expected_type,
                expected_prefix=expected_prefix,
            )
            if slot.legacy_place_id in raw_records:
                previous = raw_records[slot.legacy_place_id]
                raise CanonicalMasterDataError(
                    f"duplicate master ID {slot.legacy_place_id}: "
                    f"{previous.source_filename} and {filename}"
                )
            slots.append(slot)
            raw_records[slot.legacy_place_id] = MasterRawRecord(
                place_id=slot.legacy_place_id,
                source_filename=filename,
                record_index=index,
                raw_record=raw_record,
            )
            file_counts[city_name] += 1
            if control_tags:
                duplicate_tags_by_id[slot.legacy_place_id] = control_tags

        quota_counts = [
            MasterQuotaCount(
                source_filename=filename,
                entity_type=expected_type,
                city_id=city_id,
                count=file_counts[city_name],
            )
            for city_name, (city_id, _, _) in sorted(
                _CITY_SPECS.items(), key=lambda item: item[1][0]
            )
        ]
        _validate_metadata_city_counts(
            metadata,
            filename=filename,
            file_counts=file_counts,
        )
        source_audits.append(
            MasterSourceFileAudit(
                source_filename=filename,
                entity_type=expected_type,
                content_hash=hashlib.sha256(raw_bytes).hexdigest(),
                record_count=len(records),
                quota_counts=quota_counts,
            )
        )
        all_quota_counts.extend(quota_counts)

    explicit_decisions = _tagged_duplicate_decisions(
        duplicate_tags_by_id,
        raw_records=raw_records,
        slots={item.legacy_place_id: item for item in slots},
    )
    slots.sort(key=lambda item: item.legacy_place_id)
    source_audits.sort(key=lambda item: item.source_filename)
    all_quota_counts.sort(
        key=lambda item: (
            item.entity_type.value,
            item.city_id,
            item.source_filename,
        )
    )
    return CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={key: raw_records[key] for key in sorted(raw_records)},
        source_files=source_audits,
        quota_counts=all_quota_counts,
        explicit_duplicate_decisions=explicit_decisions,
    )


def load_duplicate_identity_decisions(
    path: str | Path,
) -> DuplicateIdentityDecisionDocument:
    decision_path = Path(path)
    try:
        raw_text = decision_path.read_text(encoding="utf-8-sig")
        document = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanonicalMasterDataError(
            f"{decision_path.name}: invalid decision document: {error}"
        ) from error
    try:
        return DuplicateIdentityDecisionDocument.model_validate(document)
    except (TypeError, ValueError) as error:
        raise CanonicalMasterDataError(
            f"{decision_path.name}: invalid decision document: {error}"
        ) from error


def build_manifest_from_master(
    master_directory: str | Path,
    decisions_path: str | Path,
    previous_manifest_path: str | Path | None = None,
    generated_at: datetime | None = None,
    *,
    include_tagged_duplicates: bool = True,
    approved_replacement_root: str | Path | None = None,
) -> tuple[CanonicalIdentityManifest, CanonicalMasterBuildReport]:
    """Build a canonical manifest and deterministic source-to-output audit."""

    loaded = load_verified_master(master_directory)
    decision_path = Path(decisions_path)
    document = load_duplicate_identity_decisions(decision_path)
    applied_decisions = [*document.decisions]
    applied_replacements = [*document.replacements]
    approved_records = []
    approved_slots: list[LegacyPlaceSlot] = []
    if approved_replacement_root is not None:
        # Imported lazily because projection consumes CanonicalMasterLoad.
        # Keeping this edge inside the workflow avoids a module import cycle.
        from .projection import (
            load_approved_replacements,
            project_approved_replacement_slots,
        )

        approved_records = load_approved_replacements(approved_replacement_root)
        approved_slots = project_approved_replacement_slots(approved_records)
        original_ids = {item.legacy_place_id for item in loaded.slots}
        overlapping_ids = original_ids & {
            item.legacy_place_id for item in approved_slots
        }
        if overlapping_ids:
            raise CanonicalMasterDataError(
                "approved replacement IDs overlap verified master IDs: "
                + ", ".join(sorted(overlapping_ids))
            )
        applied_replacements.extend(
            VacancyReplacementDecision(
                retired_place_id=item.provenance.replacement_of,
                replacement_place_id=item.id,
                reason=(
                    "approved_replacement:"
                    f"{item.provenance.approval_id}"
                ),
            )
            for item in approved_records
        )
    if include_tagged_duplicates:
        applied_decisions.extend(loaded.explicit_duplicate_decisions)

    previous_manifest = (
        read_canonical_identity_manifest(previous_manifest_path)
        if previous_manifest_path is not None
        else None
    )
    # Unknown IDs, overlapping decisions, cross-city merges, and retired-ID
    # reuse are intentionally rejected by the canonical resolver itself.
    manifest = build_canonical_identity_manifest(
        [*loaded.slots, *approved_slots],
        duplicate_decisions=applied_decisions,
        replacement_decisions=applied_replacements,
        previous_manifest=previous_manifest,
        generated_at=generated_at,
    )
    report_values: dict[str, object] = {
        "schema_version": "1.0.0",
        "source_files": loaded.source_files,
        "source_record_count": len(loaded.slots),
        "source_quota_counts": loaded.quota_counts,
        "explicit_duplicate_decisions": loaded.explicit_duplicate_decisions,
        "decision_filename": decision_path.name,
        "decision_schema_version": document.schema_version,
        "decision_document_hash": document.document_hash,
        "file_duplicate_decisions": document.decisions,
        "file_distinct_decisions": document.distinct_decisions,
        "file_replacement_decisions": document.replacements,
        "include_tagged_duplicates": include_tagged_duplicates,
        "applied_duplicate_decisions": applied_decisions,
        "applied_replacement_decisions": applied_replacements,
        "approved_replacement_count": len(approved_records),
        "approved_replacement_ids": sorted(
            item.provenance.approval_id for item in approved_records
        ),
        "approved_replacement_hashes": sorted(
            item.provenance.approval_hash for item in approved_records
        ),
        "previous_manifest_id": (
            previous_manifest.manifest_id if previous_manifest else None
        ),
        "previous_manifest_hash": (
            previous_manifest.manifest_hash if previous_manifest else None
        ),
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "canonical_place_count": len(manifest.identities),
        "retired_place_id_count": len(manifest.retired_place_ids),
        "vacancy_count": len(manifest.vacancies),
        "open_vacancy_count": sum(
            item.status is VacancyStatus.VACANT for item in manifest.vacancies
        ),
        "filled_vacancy_count": sum(
            item.status is VacancyStatus.FILLED for item in manifest.vacancies
        ),
        "output_quotas": manifest.quotas,
    }
    report_payload = _jsonable(report_values)
    return manifest, CanonicalMasterBuildReport(
        report_hash=stable_sha256(report_payload),
        **report_values,
    )


def _slot_from_record(
    record: dict[str, Any],
    *,
    filename: str,
    record_index: int,
    expected_type: EntityType,
    expected_prefix: str,
) -> tuple[LegacyPlaceSlot, str, tuple[str, ...]]:
    context = f"{filename}[{record_index}]"
    place_id = _required_string(record.get("id"), context=context, field="id")
    match = _PLACE_ID.fullmatch(place_id)
    if match is None:
        raise CanonicalMasterDataError(f"{context}: invalid place ID {place_id!r}")
    if match.group("entity") != expected_prefix:
        raise CanonicalMasterDataError(
            f"{context}: ID prefix does not match {expected_type.value}"
        )

    raw_type = _required_string(
        record.get("entity_type"), context=context, field="entity_type"
    )
    if raw_type != expected_type.value:
        raise CanonicalMasterDataError(
            f"{context}: entity_type must be {expected_type.value!r}"
        )

    raw_city = _required_string(record.get("city"), context=context, field="city")
    city_name = unicodedata.normalize("NFC", raw_city)
    try:
        city_id, city_prefix, _ = _CITY_SPECS[city_name]
    except KeyError as error:
        raise CanonicalMasterDataError(
            f"{context}: unsupported Unicode city {raw_city!r}; "
            "expected 'Đà Nẵng' or 'Quy Nhơn'"
        ) from error
    if match.group("city") != city_prefix:
        raise CanonicalMasterDataError(f"{context}: ID city prefix does not match city")

    _validate_coordinates(record.get("coordinates"), context=context)
    tags = _strict_tags(record.get("tags"), context=context)
    control_tags = tuple(
        tag
        for tag in tags
        if tag == _DUPLICATE_MARKER or tag.startswith(_DUPLICATE_TARGET_PREFIX)
    )
    semantic_tags = [tag for tag in tags if tag not in control_tags]
    secondary_types = _secondary_types(record, expected_type, context=context)
    try:
        slot = LegacyPlaceSlot(
            legacy_place_id=place_id,
            city_id=city_id,
            primary_type=expected_type,
            secondary_types=secondary_types,
            tags=semantic_tags,
        )
    except ValueError as error:
        raise CanonicalMasterDataError(f"{context}: {error}") from error
    return slot, city_name, control_tags


def _tagged_duplicate_decisions(
    duplicate_tags_by_id: dict[str, tuple[str, ...]],
    *,
    raw_records: dict[str, MasterRawRecord],
    slots: dict[str, LegacyPlaceSlot],
) -> list[DuplicateIdentityDecision]:
    decisions: list[DuplicateIdentityDecision] = []
    for duplicate_id in sorted(duplicate_tags_by_id):
        tags = duplicate_tags_by_id[duplicate_id]
        has_marker = tags.count(_DUPLICATE_MARKER) == 1
        target_tags = [
            tag for tag in tags if tag.startswith(_DUPLICATE_TARGET_PREFIX)
        ]
        if not has_marker or len(target_tags) != 1:
            source = raw_records[duplicate_id]
            raise CanonicalMasterDataError(
                f"{source.source_filename}[{source.record_index}]: duplicate metadata "
                "requires duplicate_record and exactly one duplicate_of_<id> tag"
            )
        target_id = target_tags[0][len(_DUPLICATE_TARGET_PREFIX) :]
        if _PLACE_ID.fullmatch(target_id) is None:
            raise CanonicalMasterDataError(
                f"duplicate target for {duplicate_id} is not a valid place ID: "
                f"{target_id!r}"
            )
        target = slots.get(target_id)
        if target is None:
            raise CanonicalMasterDataError(
                f"duplicate target for {duplicate_id} does not exist: {target_id}"
            )
        duplicate = slots[duplicate_id]
        if duplicate.city_id != target.city_id:
            raise CanonicalMasterDataError(
                f"duplicate target for {duplicate_id} must be in the same city"
            )
        decisions.append(
            DuplicateIdentityDecision(
                keeper_legacy_place_id=target_id,
                duplicate_legacy_place_ids=[duplicate_id],
                reason="verified_master_duplicate_tag",
            )
        )
    return decisions


def _validate_metadata_header(
    metadata: dict[str, Any],
    *,
    filename: str,
    expected_type: EntityType,
    record_count: int,
) -> None:
    if metadata.get("entity_type") != expected_type.value:
        raise CanonicalMasterDataError(
            f"{filename}: metadata.entity_type must be {expected_type.value!r}"
        )
    declared_count = metadata.get("total_count")
    if (
        not isinstance(declared_count, int)
        or isinstance(declared_count, bool)
        or declared_count != record_count
    ):
        raise CanonicalMasterDataError(
            f"{filename}: metadata.total_count must equal {record_count}"
        )


def _validate_metadata_city_counts(
    metadata: dict[str, Any],
    *,
    filename: str,
    file_counts: Counter[str],
) -> None:
    for city_name, (_, _, metadata_key) in _CITY_SPECS.items():
        declared = metadata.get(metadata_key)
        expected = file_counts[city_name]
        if (
            not isinstance(declared, int)
            or isinstance(declared, bool)
            or declared != expected
        ):
            raise CanonicalMasterDataError(
                f"{filename}: metadata.{metadata_key} must equal {expected}"
            )


def _validate_coordinates(value: object, *, context: str) -> None:
    if not isinstance(value, dict):
        raise CanonicalMasterDataError(f"{context}: coordinates must be an object")
    for field_name, lower, upper in (
        ("lat", -90.0, 90.0),
        ("lng", -180.0, 180.0),
    ):
        item = value.get(field_name)
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            or not lower <= float(item) <= upper
        ):
            raise CanonicalMasterDataError(
                f"{context}: coordinates.{field_name} is invalid"
            )


def _strict_tags(value: object, *, context: str) -> list[str]:
    if not isinstance(value, list):
        raise CanonicalMasterDataError(f"{context}: tags must be an array")
    tags: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise CanonicalMasterDataError(
                f"{context}: tags[{index}] must be a non-empty string"
            )
        tags.append(unicodedata.normalize("NFKC", item.strip()))
    if len(tags) != len(set(tags)):
        raise CanonicalMasterDataError(f"{context}: tags must not contain duplicates")
    return tags


def _secondary_types(
    record: dict[str, Any],
    primary_type: EntityType,
    *,
    context: str,
) -> list[EntityType]:
    values: list[object] = []
    for field_name in ("secondary_types", "place_types"):
        raw = record.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise CanonicalMasterDataError(f"{context}: {field_name} must be an array")
        values.extend(raw)
    parsed: set[EntityType] = set()
    for item in values:
        if not isinstance(item, str):
            raise CanonicalMasterDataError(
                f"{context}: place type values must be strings"
            )
        try:
            parsed.add(EntityType(item))
        except ValueError as error:
            raise CanonicalMasterDataError(
                f"{context}: unsupported place type {item!r}"
            ) from error
    parsed.discard(primary_type)
    return sorted(parsed, key=lambda item: item.value)


def _required_string(value: object, *, context: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalMasterDataError(f"{context}: {field} must be a non-empty string")
    return value.strip()


def _jsonable(values: dict[str, object]) -> dict[str, object]:
    # Reuse the strict report schema's JSON conversion without constructing an
    # invalid report missing its content hash.
    return json.loads(
        json.dumps(
            values,
            default=lambda value: value.model_dump(mode="json"),
            ensure_ascii=False,
        )
    )
