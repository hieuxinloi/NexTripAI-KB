from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from copy import deepcopy
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
    CanonicalCoordinates,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import EntityType, NexTripModel


class CanonicalHotelIdentityPatchError(ValueError):
    """Raised when a reviewed hotel identity patch is unsafe or inconsistent."""


class HotelIdentityPatchMode(StrEnum):
    RENAME = "rename"
    REPLACE = "replace"


class HotelIdentityPatchEvidence(NexTripModel):
    source_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: HttpUrl | None = None


class CanonicalHotelIdentityPatchRecord(NexTripModel):
    place_id: str = Field(pattern=r"^hotel_(dn|qn)_\d{3}$")
    mode: HotelIdentityPatchMode
    expected_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    phone: str | None = None
    website_url: HttpUrl | None = None
    trivago_external_id: str = Field(min_length=1)
    trivago_property_id: str = Field(pattern=r"^\d+$")
    trivago_url: HttpUrl
    google_external_id: str | None = None
    google_url: HttpUrl | None = None
    star_rating: float | None = Field(default=None, ge=0, le=5)
    review_rating: float | None = Field(default=None, ge=0, le=10)
    review_count: int | None = Field(default=None, ge=0)
    main_image: HttpUrl | None = None
    evidence: list[HotelIdentityPatchEvidence] = Field(min_length=1)
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> CanonicalHotelIdentityPatchRecord:
        if self.mode is HotelIdentityPatchMode.REPLACE:
            if self.address is None or self.latitude is None or self.longitude is None:
                raise ValueError("replacement requires address and coordinates")
            sources = {item.source_id for item in self.evidence}
            if not {"trivago-mcp", "google-maps-web"}.issubset(sources):
                raise ValueError("replacement requires Trivago and Google evidence")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        if (self.google_external_id is None) != (self.google_url is None):
            raise ValueError("Google external ID and URL must be supplied together")
        values = self.model_dump(mode="json", exclude={"record_hash"})
        expected = stable_sha256(values)
        if self.record_hash == "0" * 64:
            object.__setattr__(self, "record_hash", expected)
        elif self.record_hash != expected:
            raise ValueError("record_hash does not match correction content")
        return self


class CanonicalHotelIdentityPatch(NexTripModel):
    schema_version: str = "1.0.0"
    patch_kind: str = "hotel_identity"
    patch_id: str = Field(pattern=r"^canonical-hotel-identity-[0-9a-f]{20}$")
    patch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_dataset_id: str = Field(min_length=1)
    base_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    records: list[CanonicalHotelIdentityPatchRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_patch(self) -> CanonicalHotelIdentityPatch:
        if self.records != sorted(self.records, key=lambda item: item.place_id):
            raise ValueError("hotel identity patch records must be sorted")
        ids = [item.place_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("hotel identity patch place IDs must be unique")
        values = self.model_dump(mode="json", exclude={"patch_id", "patch_hash"})
        expected = stable_sha256(values)
        if self.patch_hash == "0" * 64 and self.patch_id == (
            "canonical-hotel-identity-" + "0" * 20
        ):
            object.__setattr__(self, "patch_hash", expected)
            object.__setattr__(
                self, "patch_id", f"canonical-hotel-identity-{expected[:20]}"
            )
        elif self.patch_hash != expected:
            raise ValueError("patch_hash does not match hotel identity patch")
        if self.patch_id != f"canonical-hotel-identity-{expected[:20]}":
            raise ValueError("patch_id does not match patch_hash")
        return self


def build_canonical_hotel_identity_patch(
    dataset: CanonicalActiveDataset,
    correction_document: dict[str, Any],
    *,
    evidence_root: str | Path = ".",
    generated_at: datetime | None = None,
) -> CanonicalHotelIdentityPatch:
    canonical = CanonicalActiveDataset.model_validate_json(dataset.model_dump_json())
    root = Path(evidence_root).resolve()
    raw_records = correction_document.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise CanonicalHotelIdentityPatchError("correction records must be a list")
    by_id = {record.place_id: record for record in canonical.records}
    records: list[CanonicalHotelIdentityPatchRecord] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise CanonicalHotelIdentityPatchError("correction record must be an object")
        place_id = str(raw.get("place_id") or "")
        existing = by_id.get(place_id)
        if existing is None or existing.primary_type is not EntityType.HOTEL:
            raise CanonicalHotelIdentityPatchError(f"unknown hotel: {place_id}")
        if raw.get("previous_name") != existing.name:
            raise CanonicalHotelIdentityPatchError(
                f"stale previous_name for {place_id}: {existing.name}"
            )
        evidence_values = []
        for evidence in raw.get("evidence") or []:
            if not isinstance(evidence, dict):
                raise CanonicalHotelIdentityPatchError("evidence must be an object")
            relative = Path(str(evidence.get("path") or ""))
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise CanonicalHotelIdentityPatchError(
                    f"evidence escapes root for {place_id}"
                ) from error
            payload = path.read_bytes()
            evidence_values.append(
                {
                    **evidence,
                    "path": relative.as_posix(),
                    "file_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        values = {
            **raw,
            "expected_record_hash": existing.record_hash,
            "evidence": evidence_values,
        }
        values["record_hash"] = "0" * 64
        records.append(CanonicalHotelIdentityPatchRecord.model_validate(values))
    records.sort(key=lambda item: item.place_id)
    generated = generated_at or datetime.now().astimezone()
    values = {
        "schema_version": "1.0.0",
        "patch_kind": "hotel_identity",
        "base_dataset_id": canonical.dataset_id,
        "base_dataset_hash": canonical.dataset_hash,
        "generated_at": generated,
        "records": records,
    }
    return CanonicalHotelIdentityPatch(
        **values,
        patch_id="canonical-hotel-identity-" + "0" * 20,
        patch_hash="0" * 64,
    )


_REPLACEMENT_FIELDS = {
    "amenities",
    "booking_links",
    "check_in_time",
    "check_out_time",
    "description",
    "distance_to_beach",
    "distance_to_center",
    "embedding_text",
    "highlights",
    "hotel_style",
    "images",
    "initial_price_observation",
    "price_per_night",
    "price_range",
    "provider_identity",
    "rating",
    "review_count",
    "room_types",
    "slug",
    "source",
    "source_url",
    "verified_sources",
}


def apply_canonical_hotel_identity_patch(
    dataset: CanonicalActiveDataset,
    patch: CanonicalHotelIdentityPatch,
) -> CanonicalActiveDataset:
    canonical = CanonicalActiveDataset.model_validate_json(dataset.model_dump_json())
    if (canonical.dataset_id, canonical.dataset_hash) != (
        patch.base_dataset_id,
        patch.base_dataset_hash,
    ):
        raise CanonicalHotelIdentityPatchError("patch belongs to another dataset")
    patch_by_id = {item.place_id: item for item in patch.records}
    records = [
        _apply_record(record, patch_by_id[record.place_id])
        if record.place_id in patch_by_id
        else CanonicalActivePlaceRecord.model_validate_json(record.model_dump_json())
        for record in canonical.records
    ]
    payload = {
        "schema_version": canonical.schema_version,
        "manifest_id": canonical.manifest_id,
        "manifest_hash": canonical.manifest_hash,
        "records": [item.model_dump(mode="json") for item in records],
        "report": canonical.report.model_dump(mode="json"),
    }
    dataset_hash = stable_sha256(payload)
    return CanonicalActiveDataset(
        schema_version=canonical.schema_version,
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        manifest_id=canonical.manifest_id,
        manifest_hash=canonical.manifest_hash,
        records=records,
        report=canonical.report,
    )


def _apply_record(
    record: CanonicalActivePlaceRecord,
    patch: CanonicalHotelIdentityPatchRecord,
) -> CanonicalActivePlaceRecord:
    if record.record_hash != patch.expected_record_hash or record.name != patch.previous_name:
        raise CanonicalHotelIdentityPatchError(f"stale base record: {record.place_id}")
    name = _text(patch.name)
    aliases = _aliases([*record.aliases, record.name], name)
    address = patch.address if patch.mode is HotelIdentityPatchMode.REPLACE else record.address
    coordinates = record.coordinates
    if patch.latitude is not None and patch.longitude is not None:
        coordinates = CanonicalCoordinates(
            lat=patch.latitude,
            lng=patch.longitude,
            source="human-reviewed-provider-correction",
        )
    phone = patch.phone if patch.mode is HotelIdentityPatchMode.REPLACE else record.phone
    website_url = patch.website_url if patch.mode is HotelIdentityPatchMode.REPLACE else record.website_url
    data = deepcopy(record.data)
    if patch.mode is HotelIdentityPatchMode.REPLACE:
        for field in _REPLACEMENT_FIELDS:
            data.pop(field, None)
        data.update(
            {
                "category": "hotel",
                "description": (
                    f"Cơ sở lưu trú tại {record.city}, được xác minh bằng "
                    "listing Trivago và vị trí Google Maps. Giá phòng được "
                    "đọc từ observation theo đúng ngày lưu trú."
                ),
                "highlights": [],
                "amenities": [],
                "hotel_style": [],
                "room_types": [],
                "images": [str(patch.main_image)] if patch.main_image else [],
                "booking_links": {"trivago": str(patch.trivago_url)},
            }
        )
    data.update(
        {
            "id": record.place_id,
            "entity_type": EntityType.HOTEL.value,
            "primary_type": EntityType.HOTEL.value,
            "place_types": [item.value for item in record.place_types],
            "name": name,
            "aliases": aliases,
            "tags": record.tags,
            "address": address,
            "phone": phone,
            "website_url": str(website_url) if website_url else None,
            "coordinates": coordinates.model_dump(mode="json"),
            "star_rating": patch.star_rating,
            "rating": patch.review_rating,
            "review_count": patch.review_count,
            "last_updated": patch.reviewed_at.isoformat(),
            "last_verified": patch.reviewed_at.isoformat(),
            "verification_status": "human_verified_provider_identity",
            "provider_identity": {
                "source_id": "trivago-mcp",
                "external_id": patch.trivago_external_id,
                "property_id": patch.trivago_property_id,
                "provider_name": name,
                "external_url": str(patch.trivago_url),
            },
            "hotel_identity_patch": {
                "patch_record_hash": patch.record_hash,
                "mode": patch.mode.value,
                "previous_name": patch.previous_name,
                "reason": patch.reason,
                "reviewer": patch.reviewer,
                "reviewed_at": patch.reviewed_at.isoformat(),
                "evidence": [item.model_dump(mode="json") for item in patch.evidence],
            },
        }
    )
    external = [
        item
        for item in deepcopy(record.external_identities)
        if item.get("source_id") not in {"trivago-mcp", "google-maps-web"}
    ]
    external.append(
        {
            "source_id": "trivago-mcp",
            "external_id": patch.trivago_external_id,
            "external_url": str(patch.trivago_url),
            "verified_at": patch.reviewed_at.isoformat(),
        }
    )
    if patch.google_external_id and patch.google_url:
        external.append(
            {
                "source_id": "google-maps-web",
                "external_id": patch.google_external_id,
                "external_url": str(patch.google_url),
                "verified_at": patch.reviewed_at.isoformat(),
            }
        )
    external.sort(key=lambda item: (str(item.get("source_id")), str(item.get("external_id"))))
    values = record.model_dump(mode="json")
    values.update(
        {
            "name": name,
            "aliases": aliases,
            "address": address,
            "coordinates": coordinates.model_dump(mode="json"),
            "phone": phone,
            "website_url": str(website_url) if website_url else None,
            "external_identities": external,
            "data": data,
        }
    )
    values.pop("record_hash")
    return CanonicalActivePlaceRecord(record_hash=stable_sha256(values), **values)


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _aliases(values: list[str], name: str) -> list[str]:
    canonical_name = _text(name).casefold()
    by_key: dict[str, str] = {}
    for value in values:
        cleaned = _text(value)
        key = cleaned.casefold()
        if cleaned and key != canonical_name:
            by_key.setdefault(key, cleaned)
    return [by_key[key] for key in sorted(by_key)]


class CanonicalHotelIdentityPatchWriter:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write(self, patch: CanonicalHotelIdentityPatch) -> Path:
        validated = CanonicalHotelIdentityPatch.model_validate_json(
            patch.model_dump_json()
        )
        path = self.root / f"patch={validated.patch_id}" / "hotel-identity-patch.json"
        payload = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8") + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(path):
            if path.exists():
                if path.read_bytes() != payload:
                    raise FileExistsError(f"immutable hotel patch differs: {path}")
                return path
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        return path


__all__ = [
    "CanonicalHotelIdentityPatch",
    "CanonicalHotelIdentityPatchError",
    "CanonicalHotelIdentityPatchRecord",
    "CanonicalHotelIdentityPatchWriter",
    "HotelIdentityPatchEvidence",
    "HotelIdentityPatchMode",
    "apply_canonical_hotel_identity_patch",
    "build_canonical_hotel_identity_patch",
]
