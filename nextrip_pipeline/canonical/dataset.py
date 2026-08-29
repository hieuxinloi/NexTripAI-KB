from __future__ import annotations

import json
import os
import unicodedata
from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, HttpUrl, JsonValue, SkipValidation, model_validator

from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import (
    CanonicalIdentityManifest,
    CanonicalPlaceIdentity,
    EntityCityQuota,
    VacancyStatus,
    stable_sha256,
)
from nextrip_pipeline.canonical.projection import (
    ExistingIdentityProjectionError,
    MaterializedReplacementRecord,
    ProjectedExistingCanonicalIdentity,
    build_existing_identity_projection,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import EntityType, NexTripModel


class CanonicalDatasetMaterializationError(ValueError):
    """Raised when an active manifest identity cannot become one dataset row."""


class CanonicalDatasetAlreadyExistsError(FileExistsError):
    """Raised rather than overwrite a different immutable dataset artifact."""


class CanonicalRecordSource(StrEnum):
    VERIFIED_MASTER = "verified_master"
    APPROVED_REPLACEMENT = "approved_replacement"


class CanonicalCoordinates(NexTripModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    source: str = Field(min_length=1)


class CanonicalMasterRecordReference(NexTripModel):
    legacy_place_id: str = Field(min_length=1)
    source_filename: str = Field(min_length=1)
    record_index: int = Field(ge=0)
    source_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CanonicalRecordProvenance(NexTripModel):
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_kind: CanonicalRecordSource
    canonical_place_id: str = Field(min_length=1)
    active_legacy_place_id: str = Field(min_length=1)
    legacy_place_ids: list[str] = Field(min_length=1)
    retired_alias_ids: list[str] = Field(default_factory=list)
    master_records: list[CanonicalMasterRecordReference] = Field(default_factory=list)
    approval_id: str | None = Field(default=None, min_length=1)
    approval_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidate_detail_id: str | None = Field(default=None, min_length=1)
    source_record_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    replacement_of: str | None = Field(default=None, min_length=1)
    vacancy_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_source_contract(self) -> CanonicalRecordProvenance:
        if self.legacy_place_ids != [
            self.active_legacy_place_id,
            *sorted(
                set(self.legacy_place_ids) - {self.active_legacy_place_id}
            ),
        ]:
            raise ValueError("legacy_place_ids must be unique with active ID first")
        expected_retired = sorted(
            set(self.legacy_place_ids) - {self.active_legacy_place_id}
        )
        if self.retired_alias_ids != expected_retired:
            raise ValueError("retired_alias_ids must cover every inactive alias")
        references = [item.legacy_place_id for item in self.master_records]
        if len(references) != len(set(references)):
            raise ValueError("master record references must be unique")

        overlay_fields = (
            self.approval_id,
            self.approval_hash,
            self.candidate_detail_id,
            self.replacement_of,
            self.vacancy_id,
        )
        if self.source_kind is CanonicalRecordSource.VERIFIED_MASTER:
            if self.active_legacy_place_id not in references:
                raise ValueError(
                    "verified-master materialization requires its active raw record"
                )
            if any(value is not None for value in overlay_fields):
                raise ValueError(
                    "verified-master provenance cannot contain replacement approval"
                )
            if self.source_record_ids or self.observation_ids:
                raise ValueError(
                    "verified-master provenance cannot contain crawl observation IDs"
                )
        else:
            if any(value is None for value in overlay_fields):
                raise ValueError(
                    "replacement provenance requires approval, detail, and vacancy IDs"
                )
            if not self.source_record_ids or not self.observation_ids:
                raise ValueError(
                    "replacement provenance requires source and observation IDs"
                )
        return self


def _record_payload(
    *,
    place_id: str,
    primary_type: EntityType,
    secondary_types: list[EntityType],
    place_types: list[EntityType],
    name: str,
    aliases: list[str],
    tags: list[str],
    city_id: str,
    city: str,
    address: str | None,
    coordinates: CanonicalCoordinates,
    phone: str | None,
    website_url: HttpUrl | None,
    external_identities: list[dict[str, JsonValue]],
    data: dict[str, JsonValue],
    provenance: CanonicalRecordProvenance,
) -> dict[str, object]:
    return {
        "place_id": place_id,
        "primary_type": primary_type.value,
        "secondary_types": [item.value for item in secondary_types],
        "place_types": [item.value for item in place_types],
        "name": name,
        "aliases": aliases,
        "tags": tags,
        "city_id": city_id,
        "city": city,
        "address": address,
        "coordinates": coordinates.model_dump(mode="json"),
        "phone": phone,
        "website_url": str(website_url) if website_url is not None else None,
        "external_identities": external_identities,
        "data": data,
        "provenance": provenance.model_dump(mode="json"),
    }


class CanonicalActivePlaceRecord(NexTripModel):
    """One active physical place with its entity-specific payload preserved."""

    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    place_id: str = Field(min_length=1)
    primary_type: EntityType
    secondary_types: list[EntityType] = Field(default_factory=list)
    place_types: list[EntityType] = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    city_id: str = Field(min_length=1)
    city: str = Field(min_length=1)
    address: str | None = None
    coordinates: CanonicalCoordinates
    phone: str | None = None
    website_url: HttpUrl | None = None
    external_identities: list[dict[str, JsonValue]] = Field(default_factory=list)
    # The entity-specific payload is already round-tripped through strict JSON
    # by the builder. Skip recursive string coercion here so evidence such as a
    # source description is preserved byte-for-byte instead of being trimmed by
    # the shared model configuration.
    data: SkipValidation[dict[str, JsonValue]]
    provenance: CanonicalRecordProvenance

    @model_validator(mode="after")
    def validate_record(self) -> CanonicalActivePlaceRecord:
        try:
            json.dumps(
                self.data,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("data must contain strict JSON values") from error
        expected_secondary = sorted(
            set(self.secondary_types), key=lambda item: item.value
        )
        if self.secondary_types != expected_secondary:
            raise ValueError("secondary_types must be unique and sorted")
        if self.primary_type in self.secondary_types:
            raise ValueError("primary_type cannot be repeated in secondary_types")
        if self.place_types != [self.primary_type, *self.secondary_types]:
            raise ValueError("place_types must contain primary then secondary types")
        if self.aliases != _canonical_aliases(self.aliases, self.name):
            raise ValueError("aliases must be unique, sorted, and exclude the name")
        if self.tags != sorted(set(self.tags)):
            raise ValueError("tags must be unique and sorted")
        if self.provenance.active_legacy_place_id not in (
            self.provenance.legacy_place_ids
        ):
            raise ValueError("record provenance does not contain its active identity")
        if self.provenance.canonical_place_id != self.place_id:
            raise ValueError("record provenance belongs to another canonical place")
        expected_data = {
            "id": self.place_id,
            "entity_type": self.primary_type.value,
            "primary_type": self.primary_type.value,
            "place_types": [item.value for item in self.place_types],
            "aliases": self.aliases,
            "name": self.name,
            "tags": self.tags,
        }
        for field_name, expected in expected_data.items():
            if self.data.get(field_name) != expected:
                raise ValueError(f"data.{field_name} does not match canonical record")
        payload = _record_payload(
            place_id=self.place_id,
            primary_type=self.primary_type,
            secondary_types=self.secondary_types,
            place_types=self.place_types,
            name=self.name,
            aliases=self.aliases,
            tags=self.tags,
            city_id=self.city_id,
            city=self.city,
            address=self.address,
            coordinates=self.coordinates,
            phone=self.phone,
            website_url=self.website_url,
            external_identities=self.external_identities,
            data=self.data,
            provenance=self.provenance,
        )
        if self.record_hash != stable_sha256(payload):
            raise ValueError("record_hash does not match canonical record content")
        return self


class CanonicalEntityCityCount(NexTripModel):
    city_id: str = Field(min_length=1)
    entity_type: EntityType
    count: int = Field(ge=0)


def _report_payload(
    *,
    manifest_id: str,
    manifest_hash: str,
    master_source_record_count: int,
    approved_replacement_count: int,
    canonical_record_count: int,
    master_materialized_count: int,
    replacement_materialized_count: int,
    retired_duplicate_count: int,
    open_vacancy_count: int,
    filled_vacancy_count: int,
    entity_city_counts: list[CanonicalEntityCityCount],
    quotas: list[EntityCityQuota],
    schema_version: str = "1.0.0",
    quarantined_identity_count: int = 0,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "master_source_record_count": master_source_record_count,
        "approved_replacement_count": approved_replacement_count,
        "canonical_record_count": canonical_record_count,
        "master_materialized_count": master_materialized_count,
        "replacement_materialized_count": replacement_materialized_count,
        "retired_duplicate_count": retired_duplicate_count,
        "open_vacancy_count": open_vacancy_count,
        "filled_vacancy_count": filled_vacancy_count,
        "entity_city_counts": [
            item.model_dump(mode="json") for item in entity_city_counts
        ],
        "quotas": [item.model_dump(mode="json") for item in quotas],
    }
    if schema_version != "1.0.0":
        payload["quarantined_identity_count"] = quarantined_identity_count
    return payload


class CanonicalActiveDatasetReport(NexTripModel):
    schema_version: str = "1.0.0"
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    master_source_record_count: int = Field(ge=0)
    approved_replacement_count: int = Field(ge=0)
    canonical_record_count: int = Field(ge=0)
    master_materialized_count: int = Field(ge=0)
    replacement_materialized_count: int = Field(ge=0)
    retired_duplicate_count: int = Field(ge=0)
    quarantined_identity_count: int = Field(
        default=0,
        ge=0,
        exclude_if=lambda value: value == 0,
    )
    open_vacancy_count: int = Field(ge=0)
    filled_vacancy_count: int = Field(ge=0)
    entity_city_counts: list[CanonicalEntityCityCount]
    quotas: list[EntityCityQuota]

    @model_validator(mode="after")
    def validate_report(self) -> CanonicalActiveDatasetReport:
        if self.schema_version == "1.0.0" and self.quarantined_identity_count:
            raise ValueError(
                "dataset report schema 1.0.0 cannot count quarantined identities"
            )
        if self.schema_version not in {"1.0.0", "1.1.0"}:
            raise ValueError("unsupported canonical dataset report schema version")
        if self.canonical_record_count != (
            self.master_materialized_count + self.replacement_materialized_count
        ):
            raise ValueError(
                "canonical count must equal master plus replacement materializations"
            )
        if self.approved_replacement_count != self.replacement_materialized_count:
            raise ValueError(
                "approved replacement count must equal replacement materializations"
            )
        expected_counts = sorted(
            self.entity_city_counts,
            key=lambda item: (item.city_id, item.entity_type.value),
        )
        if self.entity_city_counts != expected_counts:
            raise ValueError("entity_city_counts must be sorted")
        keys = [(item.city_id, item.entity_type) for item in self.entity_city_counts]
        if len(keys) != len(set(keys)):
            raise ValueError("entity/city count rows must be unique")
        if sum(item.count for item in self.entity_city_counts) != (
            self.canonical_record_count
        ):
            raise ValueError("entity/city counts must sum to canonical_record_count")
        expected_quotas = sorted(
            self.quotas,
            key=lambda item: (item.city_id, item.entity_type.value),
        )
        if self.quotas != expected_quotas:
            raise ValueError("quotas must be sorted")
        quota_by_key = {
            (item.city_id, item.entity_type): item for item in self.quotas
        }
        for count in self.entity_city_counts:
            quota = quota_by_key.get((count.city_id, count.entity_type))
            if quota is None or quota.active_count != count.count:
                raise ValueError("entity/city count does not match manifest quota")
        payload = _report_payload(
            manifest_id=self.manifest_id,
            manifest_hash=self.manifest_hash,
            master_source_record_count=self.master_source_record_count,
            approved_replacement_count=self.approved_replacement_count,
            canonical_record_count=self.canonical_record_count,
            master_materialized_count=self.master_materialized_count,
            replacement_materialized_count=self.replacement_materialized_count,
            retired_duplicate_count=self.retired_duplicate_count,
            open_vacancy_count=self.open_vacancy_count,
            filled_vacancy_count=self.filled_vacancy_count,
            entity_city_counts=self.entity_city_counts,
            quotas=self.quotas,
            schema_version=self.schema_version,
            quarantined_identity_count=self.quarantined_identity_count,
        )
        if self.report_hash != stable_sha256(payload):
            raise ValueError("report_hash does not match report content")
        return self


def _dataset_payload(
    *,
    manifest_id: str,
    manifest_hash: str,
    records: list[CanonicalActivePlaceRecord],
    report: CanonicalActiveDatasetReport,
    schema_version: str = "1.0.0",
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "records": [item.model_dump(mode="json") for item in records],
        "report": report.model_dump(mode="json"),
    }


class CanonicalActiveDataset(NexTripModel):
    """Content-addressed active-only input for registries and GraphRAG V8."""

    schema_version: str = "1.0.0"
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_id: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: list[CanonicalActivePlaceRecord]
    report: CanonicalActiveDatasetReport

    @model_validator(mode="after")
    def validate_dataset(self) -> CanonicalActiveDataset:
        if self.schema_version not in {"1.0.0", "1.1.0"}:
            raise ValueError("unsupported canonical active dataset schema version")
        if self.schema_version != self.report.schema_version:
            raise ValueError("dataset and report schema versions must match")
        ids = [item.place_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("canonical active record IDs must be unique")
        if self.records != sorted(self.records, key=lambda item: item.place_id):
            raise ValueError("canonical active records must be sorted by place ID")
        if len(self.records) != self.report.canonical_record_count:
            raise ValueError("dataset record count does not match report")
        if (self.manifest_id, self.manifest_hash) != (
            self.report.manifest_id,
            self.report.manifest_hash,
        ):
            raise ValueError("dataset and report refer to different manifests")
        payload = _dataset_payload(
            manifest_id=self.manifest_id,
            manifest_hash=self.manifest_hash,
            records=self.records,
            report=self.report,
            schema_version=self.schema_version,
        )
        expected_hash = stable_sha256(payload)
        if self.dataset_hash != expected_hash:
            raise ValueError("dataset_hash does not match dataset content")
        if self.dataset_id != f"canonical-active-{expected_hash[:20]}":
            raise ValueError("dataset_id does not match dataset_hash")
        return self


def materialize_canonical_active_dataset(
    master: CanonicalMasterLoad,
    manifest: CanonicalIdentityManifest,
    *,
    approved_replacements: Iterable[MaterializedReplacementRecord] = (),
) -> CanonicalActiveDataset:
    """Build one immutable record per active canonical identity."""

    overlays = [
        MaterializedReplacementRecord.model_validate_json(item.model_dump_json())
        for item in approved_replacements
    ]
    overlay_by_id = {item.id: item for item in overlays}
    if len(overlay_by_id) != len(overlays):
        raise CanonicalDatasetMaterializationError(
            "approved replacement overlay IDs must be unique"
        )
    try:
        projection = build_existing_identity_projection(
            master,
            manifest,
            approved_replacements=overlays,
        )
    except ExistingIdentityProjectionError as error:
        raise CanonicalDatasetMaterializationError(str(error)) from error
    active_projection = {
        item.place_id: item for item in projection.identities if not item.retired
    }

    records: list[CanonicalActivePlaceRecord] = []
    for identity in sorted(
        manifest.identities, key=lambda item: item.canonical_place_id
    ):
        projected = active_projection.get(identity.canonical_place_id)
        if projected is None:
            raise CanonicalDatasetMaterializationError(
                "active manifest identity was not projected: "
                + identity.canonical_place_id
            )
        overlay = overlay_by_id.get(identity.canonical_place_id)
        records.append(
            _materialize_active_record(
                master,
                manifest=manifest,
                identity=identity,
                projected=projected,
                overlay=overlay,
            )
        )

    if {item.place_id for item in records} != {
        item.canonical_place_id for item in manifest.identities
    }:
        raise CanonicalDatasetMaterializationError(
            "dataset does not exactly cover active manifest identities"
        )
    quarantined_legacy_ids = {
        legacy_id
        for item in manifest.quarantined_identities
        for legacy_id in item.identity.legacy_place_ids
    }
    leaked_quarantined_ids = {
        item.place_id for item in records
    } & quarantined_legacy_ids
    if leaked_quarantined_ids:
        raise CanonicalDatasetMaterializationError(
            "quarantined identities leaked into the active dataset: "
            + ", ".join(sorted(leaked_quarantined_ids))
        )
    schema_version = (
        "1.1.0" if manifest.quarantined_identities else "1.0.0"
    )
    report = _build_report(
        master,
        manifest=manifest,
        records=records,
        approved_replacement_count=len(overlays),
        schema_version=schema_version,
    )
    payload = _dataset_payload(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        records=records,
        report=report,
        schema_version=schema_version,
    )
    dataset_hash = stable_sha256(payload)
    return CanonicalActiveDataset(
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        schema_version=schema_version,
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        records=records,
        report=report,
    )


def _materialize_active_record(
    master: CanonicalMasterLoad,
    *,
    manifest: CanonicalIdentityManifest,
    identity: CanonicalPlaceIdentity,
    projected: ProjectedExistingCanonicalIdentity,
    overlay: MaterializedReplacementRecord | None,
) -> CanonicalActivePlaceRecord:
    raw_records = [
        record
        for legacy_id in identity.legacy_place_ids
        if (record := master.raw_record(legacy_id)) is not None
    ]
    aliases = _aliases_from_sources(
        canonical_name=projected.name,
        raw_records=raw_records,
        overlay=overlay,
    )
    if overlay is None:
        active_raw = master.raw_record(identity.active_legacy_place_id)
        if active_raw is None:
            raise CanonicalDatasetMaterializationError(
                "active identity has neither master record nor approved overlay: "
                + identity.canonical_place_id
            )
        data = _json_copy(active_raw.raw_record)
        source_kind = CanonicalRecordSource.VERIFIED_MASTER
    else:
        data = _json_copy(overlay.model_dump(mode="json"))
        source_kind = CanonicalRecordSource.APPROVED_REPLACEMENT

    place_types = [identity.primary_type, *identity.secondary_types]
    data.update(
        {
            "id": identity.canonical_place_id,
            "entity_type": identity.primary_type.value,
            "primary_type": identity.primary_type.value,
            "place_types": [item.value for item in place_types],
            "aliases": aliases,
            "name": projected.name,
            "secondary_types": [item.value for item in identity.secondary_types],
            "tags": identity.tags,
            "city_id": identity.city_id,
            "legacy_place_ids": identity.legacy_place_ids,
            "canonical_identity_hash": identity.identity_hash,
        }
    )
    references = [_master_reference(item) for item in raw_records]
    replacement = overlay.provenance if overlay is not None else None
    provenance = CanonicalRecordProvenance(
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        identity_hash=identity.identity_hash,
        source_kind=source_kind,
        canonical_place_id=identity.canonical_place_id,
        active_legacy_place_id=identity.active_legacy_place_id,
        legacy_place_ids=identity.legacy_place_ids,
        retired_alias_ids=sorted(
            set(identity.legacy_place_ids) - {identity.active_legacy_place_id}
        ),
        master_records=references,
        approval_id=replacement.approval_id if replacement else None,
        approval_hash=replacement.approval_hash if replacement else None,
        candidate_detail_id=(
            replacement.candidate_detail_id if replacement else None
        ),
        source_record_ids=(replacement.source_record_ids if replacement else []),
        observation_ids=(replacement.observation_ids if replacement else []),
        replacement_of=replacement.replacement_of if replacement else None,
        vacancy_id=replacement.vacancy_id if replacement else None,
    )
    location = projected.location
    if location is None:
        raise CanonicalDatasetMaterializationError(
            f"canonical identity has no coordinates: {identity.canonical_place_id}"
        )
    coordinates = CanonicalCoordinates(
        lat=location.latitude,
        lng=location.longitude,
        source=location.source or source_kind.value,
    )
    external_identities = [
        item.model_dump(mode="json") for item in projected.external_identities
    ]
    payload = _record_payload(
        place_id=identity.canonical_place_id,
        primary_type=identity.primary_type,
        secondary_types=identity.secondary_types,
        place_types=place_types,
        name=projected.name,
        aliases=aliases,
        tags=identity.tags,
        city_id=identity.city_id,
        city=_city_name(data, identity.city_id),
        address=projected.address,
        coordinates=coordinates,
        phone=projected.phone,
        website_url=projected.website_url,
        external_identities=external_identities,
        data=data,
        provenance=provenance,
    )
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(payload),
        **payload,
    )


def _master_reference(record: MasterRawRecord) -> CanonicalMasterRecordReference:
    return CanonicalMasterRecordReference(
        legacy_place_id=record.place_id,
        source_filename=record.source_filename,
        record_index=record.record_index,
        source_record_hash=stable_sha256(record.raw_record),
    )


def _aliases_from_sources(
    *,
    canonical_name: str,
    raw_records: Iterable[MasterRawRecord],
    overlay: MaterializedReplacementRecord | None,
) -> list[str]:
    values: list[str] = []
    for record in raw_records:
        if isinstance(name := record.raw_record.get("name"), str):
            values.append(name)
        raw_aliases = record.raw_record.get("aliases")
        if isinstance(raw_aliases, list):
            values.extend(item for item in raw_aliases if isinstance(item, str))
    if overlay is not None:
        raw_aliases = overlay.model_dump(mode="json").get("aliases")
        if isinstance(raw_aliases, list):
            values.extend(item for item in raw_aliases if isinstance(item, str))
    return _canonical_aliases(values, canonical_name)


def _canonical_aliases(values: Iterable[str], canonical_name: str) -> list[str]:
    canonical_key = _text_key(canonical_name)
    selected: dict[str, str] = {}
    for value in values:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        key = _text_key(cleaned)
        if not key or key == canonical_key:
            continue
        selected.setdefault(key, cleaned)
    return [selected[key] for key in sorted(selected)]


def _text_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _city_name(data: dict[str, JsonValue], city_id: str) -> str:
    city = data.get("city")
    if isinstance(city, str) and city.strip():
        return city.strip()
    names = {"city_da_nang": "Đà Nẵng", "city_quy_nhon": "Quy Nhơn"}
    try:
        return names[city_id]
    except KeyError as error:
        raise CanonicalDatasetMaterializationError(
            f"canonical identity has no supported city name: {city_id}"
        ) from error


def _json_copy(value: dict[str, object]) -> dict[str, JsonValue]:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _build_report(
    master: CanonicalMasterLoad,
    *,
    manifest: CanonicalIdentityManifest,
    records: list[CanonicalActivePlaceRecord],
    approved_replacement_count: int,
    schema_version: str,
) -> CanonicalActiveDatasetReport:
    counts = Counter((item.city_id, item.primary_type) for item in records)
    entity_city_counts = [
        CanonicalEntityCityCount(
            city_id=city_id,
            entity_type=entity_type,
            count=count,
        )
        for (city_id, entity_type), count in sorted(
            counts.items(), key=lambda item: (item[0][0], item[0][1].value)
        )
    ]
    quotas = sorted(
        manifest.quotas,
        key=lambda item: (item.city_id, item.entity_type.value),
    )
    quota_by_key = {(item.city_id, item.entity_type): item for item in quotas}
    for count in entity_city_counts:
        quota = quota_by_key.get((count.city_id, count.entity_type))
        if quota is None or quota.active_count != count.count:
            raise CanonicalDatasetMaterializationError(
                "materialized count does not match manifest quota for "
                f"{count.entity_type.value}/{count.city_id}"
            )
    open_vacancy_count = sum(
        item.status is VacancyStatus.VACANT for item in manifest.vacancies
    )
    filled_vacancy_count = sum(
        item.status is VacancyStatus.FILLED for item in manifest.vacancies
    )
    replacement_materialized_count = sum(
        item.provenance.source_kind is CanonicalRecordSource.APPROVED_REPLACEMENT
        for item in records
    )
    values: dict[str, object] = {
        "schema_version": schema_version,
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "master_source_record_count": len(master.raw_records_by_id),
        "approved_replacement_count": approved_replacement_count,
        "canonical_record_count": len(records),
        "master_materialized_count": len(records)
        - replacement_materialized_count,
        "replacement_materialized_count": replacement_materialized_count,
        "retired_duplicate_count": len(manifest.retired_place_ids),
        "quarantined_identity_count": len(
            manifest.quarantined_identities
        ),
        "open_vacancy_count": open_vacancy_count,
        "filled_vacancy_count": filled_vacancy_count,
        "entity_city_counts": entity_city_counts,
        "quotas": quotas,
    }
    report_hash = stable_sha256(_report_payload(**values))
    return CanonicalActiveDatasetReport(report_hash=report_hash, **values)


class CanonicalActiveDatasetWriter:
    """Write a content-addressed dataset once; never replace another artifact."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, dataset: CanonicalActiveDataset) -> Path:
        return (
            self.output_root
            / f"dataset={quote(dataset.dataset_id, safe='-_.')}"
            / "canonical-active-dataset.json"
        )

    def write(self, dataset: CanonicalActiveDataset) -> Path:
        validated = CanonicalActiveDataset.model_validate_json(
            dataset.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = CanonicalActiveDataset.model_validate_json(
                        destination.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalDatasetAlreadyExistsError(
                        f"immutable dataset path is invalid: {destination}"
                    ) from error
                if existing.dataset_hash == validated.dataset_hash:
                    return destination
                raise CanonicalDatasetAlreadyExistsError(
                    f"immutable dataset path already exists: {destination}"
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


def read_canonical_active_dataset(path: str | Path) -> CanonicalActiveDataset:
    return CanonicalActiveDataset.model_validate_json(
        Path(path).read_bytes()
    )
