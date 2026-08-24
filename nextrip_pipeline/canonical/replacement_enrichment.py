from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
    CanonicalRecordSource,
)
from nextrip_pipeline.canonical.discovery import _google_maps_identity
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.canonical.projection import MaterializedReplacementRecord
from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import NexTripModel, RecordSubjectType, SourceRecord


class ReplacementEnrichmentError(ValueError):
    """Raised when raw evidence cannot safely enrich an approved replacement."""


class ReplacementEnrichmentAlreadyExistsError(FileExistsError):
    """Raised rather than replacing a different immutable enrichment overlay."""


_GOOGLE_SOURCE_ID = "google-maps-web"
_FIELD_ORDER = ("address", "google_maps_category", "cover_image_url")


class ReplacementEnrichmentProvenance(NexTripModel):
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_key: str = Field(min_length=1)
    source_id: Literal["google-maps-web"] = "google-maps-web"
    source_record_id: str = Field(min_length=1)
    source_record_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_envelope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: HttpUrl
    crawled_at: AwareDatetime
    raw_relative_path: str = Field(min_length=1)


def _enrichment_payload(
    *,
    place_id: str,
    enriched_fields: list[str],
    address: str | None,
    google_maps_category: str | None,
    cover_image_url: HttpUrl | None,
    provenance: ReplacementEnrichmentProvenance,
) -> dict[str, object]:
    return {
        "place_id": place_id,
        "enriched_fields": enriched_fields,
        "address": address,
        "google_maps_category": google_maps_category,
        "cover_image_url": (
            str(cover_image_url) if cover_image_url is not None else None
        ),
        "provenance": provenance.model_dump(mode="json"),
    }


class CanonicalReplacementEnrichment(NexTripModel):
    """Source-backed fields recovered without changing an approval artifact."""

    enrichment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    place_id: str = Field(min_length=1)
    enriched_fields: list[str] = Field(min_length=1)
    address: str | None = None
    google_maps_category: str | None = None
    cover_image_url: HttpUrl | None = None
    provenance: ReplacementEnrichmentProvenance

    @model_validator(mode="after")
    def validate_enrichment(self) -> CanonicalReplacementEnrichment:
        present = {
            "address": self.address is not None,
            "google_maps_category": self.google_maps_category is not None,
            "cover_image_url": self.cover_image_url is not None,
        }
        expected_fields = [field for field in _FIELD_ORDER if present[field]]
        if self.enriched_fields != expected_fields:
            raise ValueError(
                "enriched_fields must exactly describe source-backed values"
            )
        payload = _enrichment_payload(
            place_id=self.place_id,
            enriched_fields=self.enriched_fields,
            address=self.address,
            google_maps_category=self.google_maps_category,
            cover_image_url=self.cover_image_url,
            provenance=self.provenance,
        )
        if self.enrichment_hash != stable_sha256(payload):
            raise ValueError("enrichment_hash does not match enrichment content")
        return self


def _overlay_payload(
    enrichments: list[CanonicalReplacementEnrichment],
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "enrichments": [item.model_dump(mode="json") for item in enrichments],
    }


class CanonicalReplacementEnrichmentOverlay(NexTripModel):
    """Complete immutable enrichment overlay for approved replacements."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    overlay_id: str = Field(min_length=1)
    overlay_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_replacement_count: int = Field(ge=0)
    enrichment_count: int = Field(ge=0)
    address_count: int = Field(ge=0)
    category_count: int = Field(ge=0)
    cover_count: int = Field(ge=0)
    enrichments: list[CanonicalReplacementEnrichment]

    @model_validator(mode="after")
    def validate_overlay(self) -> CanonicalReplacementEnrichmentOverlay:
        if self.enrichments != sorted(
            self.enrichments, key=lambda item: item.place_id
        ):
            raise ValueError("replacement enrichments must be sorted by place_id")
        place_ids = [item.place_id for item in self.enrichments]
        if len(place_ids) != len(set(place_ids)):
            raise ValueError("replacement enrichment place IDs must be unique")
        if (
            self.approved_replacement_count != len(self.enrichments)
            or self.enrichment_count != len(self.enrichments)
            or self.address_count
            != sum(item.address is not None for item in self.enrichments)
            or self.category_count
            != sum(
                item.google_maps_category is not None for item in self.enrichments
            )
            or self.cover_count
            != sum(item.cover_image_url is not None for item in self.enrichments)
        ):
            raise ValueError("replacement enrichment overlay counts do not match")
        expected_hash = stable_sha256(_overlay_payload(self.enrichments))
        if self.overlay_hash != expected_hash:
            raise ValueError("overlay_hash does not match overlay content")
        if self.overlay_id != f"canonical-replacement-enrichment-{expected_hash[:20]}":
            raise ValueError("overlay_id does not match overlay_hash")
        return self


def build_canonical_replacement_enrichment_overlay(
    replacements: Iterable[MaterializedReplacementRecord],
    raw_root: str | Path,
) -> CanonicalReplacementEnrichmentOverlay:
    """Recover fields from the exact raw records pinned by each approval."""

    approved = [
        MaterializedReplacementRecord.model_validate_json(item.model_dump_json())
        for item in replacements
    ]
    place_ids = [item.id for item in approved]
    if len(place_ids) != len(set(place_ids)):
        raise ReplacementEnrichmentError("approved replacement IDs must be unique")
    source_ids = [
        _required_source_record_id(item)
        for item in approved
    ]
    if len(source_ids) != len(set(source_ids)):
        raise ReplacementEnrichmentError(
            "one raw source record cannot enrich multiple replacements"
        )

    root = Path(raw_root)
    source_paths = _find_source_record_paths(root, set(source_ids))
    enrichments = [
        _build_enrichment(item, source_paths[_required_source_record_id(item)], root)
        for item in sorted(approved, key=lambda value: value.id)
    ]
    payload = _overlay_payload(enrichments)
    overlay_hash = stable_sha256(payload)
    return CanonicalReplacementEnrichmentOverlay(
        overlay_id=f"canonical-replacement-enrichment-{overlay_hash[:20]}",
        overlay_hash=overlay_hash,
        approved_replacement_count=len(approved),
        enrichment_count=len(enrichments),
        address_count=sum(item.address is not None for item in enrichments),
        category_count=sum(
            item.google_maps_category is not None for item in enrichments
        ),
        cover_count=sum(item.cover_image_url is not None for item in enrichments),
        enrichments=enrichments,
    )


def _required_source_record_id(record: MaterializedReplacementRecord) -> str:
    source_record_ids = record.provenance.source_record_ids
    if len(source_record_ids) != 1:
        raise ReplacementEnrichmentError(
            f"{record.id}: replacement must pin exactly one source record"
        )
    return source_record_ids[0]


def _find_source_record_paths(
    raw_root: Path,
    source_record_ids: set[str],
) -> dict[str, Path]:
    if not raw_root.is_dir():
        raise ReplacementEnrichmentError(
            f"raw root is not a directory: {raw_root}"
        )
    expected_names = {
        f"record={quote(source_id, safe='-_.')}.json": source_id
        for source_id in source_record_ids
    }
    found: dict[str, Path] = {}
    for path in sorted(raw_root.rglob("*.json"), key=lambda item: item.as_posix()):
        source_id = expected_names.get(path.name)
        if source_id is None:
            continue
        if source_id in found:
            raise ReplacementEnrichmentError(
                f"duplicate raw source record ID: {source_id}"
            )
        found[source_id] = path
    missing = sorted(source_record_ids - set(found))
    if missing:
        raise ReplacementEnrichmentError(
            "missing raw source records: " + ", ".join(missing)
        )
    return found


def _build_enrichment(
    replacement: MaterializedReplacementRecord,
    raw_path: Path,
    raw_root: Path,
) -> CanonicalReplacementEnrichment:
    try:
        source_record = SourceRecord.model_validate_json(raw_path.read_bytes())
    except (OSError, TypeError, ValueError) as error:
        raise ReplacementEnrichmentError(
            f"{replacement.id}: invalid raw source record {raw_path}: {error}"
        ) from error
    expected_source_id = _required_source_record_id(replacement)
    if source_record.source_record_id != expected_source_id:
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw source_record_id does not match approval"
        )
    actual_content_hash = compute_content_hash(source_record.raw_payload)
    if actual_content_hash != source_record.content_hash.casefold():
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw source content_hash mismatch"
        )
    if (
        source_record.source_id != _GOOGLE_SOURCE_ID
        or source_record.subject_type is not RecordSubjectType.OPENING_STATUS
        or source_record.entity_type != replacement.entity_type
        or source_record.subject_id != replacement.provenance.candidate_key
        or source_record.source_url is None
    ):
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw source provenance does not match approval"
        )
    try:
        _, source_token = _google_maps_identity(str(source_record.source_url))
    except ValueError as error:
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw source URL is not a Google Maps place URL"
        ) from error
    if (
        source_token is None
        or source_token != replacement.provenance.google_external_id
    ):
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw Google token does not match approval"
        )

    page = source_record.raw_payload.get("page")
    structured = page.get("structured_data") if isinstance(page, dict) else None
    if not isinstance(structured, dict):
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw page.structured_data is missing"
        )
    raw_name = _optional_text(structured.get("name"))
    if raw_name is None or _identity_key(raw_name) != _identity_key(replacement.name):
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw place name does not match approval"
        )
    address = _optional_text(structured.get("address"))
    category = _optional_text(structured.get("category"))
    cover = _cover_image_url(structured.get("image_urls"))
    values = {
        "address": address,
        "google_maps_category": category,
        "cover_image_url": cover,
    }
    enriched_fields = [field for field in _FIELD_ORDER if values[field] is not None]
    if not enriched_fields:
        raise ReplacementEnrichmentError(
            f"{replacement.id}: raw record has no source-backed enrichment fields"
        )
    provenance = ReplacementEnrichmentProvenance(
        approval_id=replacement.provenance.approval_id,
        approval_hash=replacement.provenance.approval_hash,
        candidate_key=replacement.provenance.candidate_key,
        source_record_id=source_record.source_record_id,
        source_record_content_hash=source_record.content_hash.casefold(),
        source_record_envelope_hash=stable_sha256(
            source_record.model_dump(mode="json")
        ),
        source_url=source_record.source_url,
        crawled_at=source_record.crawled_at,
        raw_relative_path=raw_path.relative_to(raw_root).as_posix(),
    )
    payload = _enrichment_payload(
        place_id=replacement.id,
        enriched_fields=enriched_fields,
        address=address,
        google_maps_category=category,
        cover_image_url=cover,
        provenance=provenance,
    )
    return CanonicalReplacementEnrichment(
        enrichment_hash=stable_sha256(payload),
        **payload,
    )


def _optional_text(value: object) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def _identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    ).strip()


def _cover_image_url(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for raw_url in value:
        url = _optional_text(raw_url)
        if url is None or not url.startswith(("http://", "https://")):
            continue
        dimensions = re.search(r"=w(\d+)-h(\d+)(?:-|$)", url)
        if dimensions and (
            int(dimensions.group(1)) < 128 or int(dimensions.group(2)) < 128
        ):
            continue
        return url
    return None


def apply_replacement_enrichments(
    dataset: CanonicalActiveDataset,
    overlay: CanonicalReplacementEnrichmentOverlay,
) -> CanonicalActiveDataset:
    """Apply a complete enrichment overlay and recompute immutable hashes."""

    validated_dataset = CanonicalActiveDataset.model_validate_json(
        dataset.model_dump_json()
    )
    validated_overlay = CanonicalReplacementEnrichmentOverlay.model_validate_json(
        overlay.model_dump_json()
    )
    enrichments = {item.place_id: item for item in validated_overlay.enrichments}
    replacement_ids = {
        item.place_id
        for item in validated_dataset.records
        if item.provenance.source_kind is CanonicalRecordSource.APPROVED_REPLACEMENT
    }
    if set(enrichments) != replacement_ids:
        missing = sorted(replacement_ids - set(enrichments))
        unknown = sorted(set(enrichments) - replacement_ids)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ReplacementEnrichmentError(
            "enrichment overlay must exactly cover approved replacements: "
            + " ".join(details)
        )
    records = [
        _apply_enrichment(item, enrichments[item.place_id], validated_overlay)
        if item.place_id in enrichments
        else CanonicalActivePlaceRecord.model_validate_json(item.model_dump_json())
        for item in validated_dataset.records
    ]
    dataset_payload = {
        "schema_version": validated_dataset.schema_version,
        "manifest_id": validated_dataset.manifest_id,
        "manifest_hash": validated_dataset.manifest_hash,
        "records": [item.model_dump(mode="json") for item in records],
        "report": validated_dataset.report.model_dump(mode="json"),
    }
    dataset_hash = stable_sha256(dataset_payload)
    return CanonicalActiveDataset(
        schema_version=validated_dataset.schema_version,
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        manifest_id=validated_dataset.manifest_id,
        manifest_hash=validated_dataset.manifest_hash,
        records=records,
        report=validated_dataset.report,
    )


def _apply_enrichment(
    record: CanonicalActivePlaceRecord,
    enrichment: CanonicalReplacementEnrichment,
    overlay: CanonicalReplacementEnrichmentOverlay,
) -> CanonicalActivePlaceRecord:
    provenance = enrichment.provenance
    if (
        record.provenance.approval_id != provenance.approval_id
        or record.provenance.approval_hash != provenance.approval_hash
        or provenance.source_record_id not in record.provenance.source_record_ids
    ):
        raise ReplacementEnrichmentError(
            f"{record.place_id}: enrichment provenance does not match replacement"
        )
    data = deepcopy(record.data)
    address = record.address
    if enrichment.address is not None:
        _reject_text_conflict(record.place_id, "address", address, enrichment.address)
        address = enrichment.address
        data["address"] = enrichment.address
    if enrichment.google_maps_category is not None:
        _reject_text_conflict(
            record.place_id,
            "google_maps_category",
            data.get("google_maps_category"),
            enrichment.google_maps_category,
        )
        data["google_maps_category"] = enrichment.google_maps_category
    if enrichment.cover_image_url is not None:
        cover = str(enrichment.cover_image_url)
        _reject_text_conflict(
            record.place_id,
            "cover_image_url",
            data.get("cover_image_url"),
            cover,
        )
        existing_images = data.get("images")
        if existing_images not in (None, [], [cover]):
            raise ReplacementEnrichmentError(
                f"{record.place_id}: images conflict with raw cover evidence"
            )
        data["cover_image_url"] = cover
        data["images"] = [cover]
    data["replacement_enrichment"] = {
        "overlay_id": overlay.overlay_id,
        "overlay_hash": overlay.overlay_hash,
        "enrichment_hash": enrichment.enrichment_hash,
        "enriched_fields": list(enrichment.enriched_fields),
        "approval_id": provenance.approval_id,
        "approval_hash": provenance.approval_hash,
        "source_id": provenance.source_id,
        "source_record_id": provenance.source_record_id,
        "source_record_content_hash": provenance.source_record_content_hash,
        "source_record_envelope_hash": provenance.source_record_envelope_hash,
        "source_url": str(provenance.source_url),
        "crawled_at": provenance.model_dump(mode="json")["crawled_at"],
        "raw_relative_path": provenance.raw_relative_path,
    }
    values = record.model_dump(mode="json")
    values.update({"address": address, "data": data})
    values.pop("record_hash")
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(values),
        **values,
    )


def _reject_text_conflict(
    place_id: str,
    field_name: str,
    current: object,
    proposed: str,
) -> None:
    if current is None:
        return
    if not isinstance(current, str) or _identity_key(current) != _identity_key(proposed):
        raise ReplacementEnrichmentError(
            f"{place_id}: {field_name} conflicts with raw enrichment evidence"
        )


class CanonicalReplacementEnrichmentWriter:
    """Write a content-addressed enrichment overlay once."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(
        self, overlay: CanonicalReplacementEnrichmentOverlay
    ) -> Path:
        return (
            self.output_root
            / f"overlay={quote(overlay.overlay_id, safe='-_.')}"
            / "canonical-replacement-enrichment.json"
        )

    def write(self, overlay: CanonicalReplacementEnrichmentOverlay) -> Path:
        validated = CanonicalReplacementEnrichmentOverlay.model_validate_json(
            overlay.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    existing = read_canonical_replacement_enrichment_overlay(
                        destination
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise ReplacementEnrichmentAlreadyExistsError(
                        f"immutable enrichment overlay is invalid: {destination}"
                    ) from error
                if existing.overlay_hash == validated.overlay_hash:
                    return destination
                raise ReplacementEnrichmentAlreadyExistsError(
                    f"immutable enrichment overlay already exists: {destination}"
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


def read_canonical_replacement_enrichment_overlay(
    path: str | Path,
) -> CanonicalReplacementEnrichmentOverlay:
    return CanonicalReplacementEnrichmentOverlay.model_validate_json(
        Path(path).read_bytes()
    )
