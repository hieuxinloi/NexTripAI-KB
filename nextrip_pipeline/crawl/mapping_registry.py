from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.google_maps_identity import (
    google_maps_search_placeholder,
    google_maps_stable_external_id,
    google_maps_stable_external_ids,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
)

if TYPE_CHECKING:
    from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset


_STABLE_ID_ATTRIBUTE_KEYS = (
    "canonical_google_maps_url",
    "google_external_id",
    "google_place_id",
    "stable_external_id",
)


def _mapping_stable_external_ids(
    mapping: ExternalEntityMapping,
) -> tuple[str, ...]:
    return google_maps_stable_external_ids(
        mapping.external_id,
        mapping.external_url,
        *(mapping.attributes.get(key) for key in _STABLE_ID_ATTRIBUTE_KEYS),
    )


def _mapping_registry_identity_errors(
    mappings: Sequence[ExternalEntityMapping],
) -> list[str]:
    """Return every identity collision that makes a registry unsafe to run."""

    errors: list[str] = []
    mapping_ids = [mapping.mapping_id for mapping in mappings]
    duplicate_mapping_ids = sorted(
        value for value, count in Counter(mapping_ids).items() if count > 1
    )
    if duplicate_mapping_ids:
        errors.append(
            "duplicate Google mapping_id values: "
            + ", ".join(duplicate_mapping_ids)
        )

    entity_ids = [mapping.entity_id for mapping in mappings]
    duplicate_entity_ids = sorted(
        value for value, count in Counter(entity_ids).items() if count > 1
    )
    if duplicate_entity_ids:
        errors.append(
            "duplicate Google mapping entity_id values: "
            + ", ".join(duplicate_entity_ids)
        )

    owners_by_token: defaultdict[str, list[str]] = defaultdict(list)
    for mapping in mappings:
        if mapping.status is MappingStatus.REJECTED:
            continue
        tokens = _mapping_stable_external_ids(mapping)
        if len(tokens) > 1:
            errors.append(
                "conflicting stable Google external IDs for "
                f"{mapping.entity_id}: {', '.join(tokens)}"
            )
            continue
        if tokens:
            owners_by_token[tokens[0]].append(mapping.entity_id)

    for token, owners in sorted(owners_by_token.items()):
        unique_owners = sorted(set(owners))
        if len(unique_owners) > 1:
            errors.append(
                f"duplicate active Google stable external_id {token!r}: "
                + ", ".join(unique_owners)
            )
    return errors


class MasterDataValidationError(ValueError):
    """Raised when master data cannot safely produce a mapping registry."""


class GoogleMapsMappingRegistry(NexTripModel):
    registry_version: str = "1.0.0"
    generated_at: AwareDatetime
    source_files: list[str] = Field(min_length=1)
    mappings: list[ExternalEntityMapping] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> GoogleMapsMappingRegistry:
        errors = _mapping_registry_identity_errors(self.mappings)
        if errors:
            raise ValueError("; ".join(errors))
        return self


class GoogleMapsRegistryReport(NexTripModel):
    generated_at: AwareDatetime
    total_records: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    entity_counts: dict[str, int]
    city_counts: dict[str, int]
    overrides_applied: int = Field(ge=0)
    reused_mapping_count: int = Field(default=0, ge=0)
    duplicate_identity_candidates: list[list[str]] = Field(default_factory=list)


class GoogleMapsBatchManifestDocument(NexTripModel):
    """Minimal immutable input document consumed by the existing batch loader."""

    registry_file: str = Field(min_length=1)
    resolved_mapping_dir: str | None = Field(default=None, min_length=1)


class GoogleMapsRegistryBuilder:
    filenames = {
        EntityType.ATTRACTION: "attraction_final.json",
        EntityType.CAFE: "cafe_final.json",
        EntityType.HOTEL: "hotel_final.json",
        EntityType.NIGHTLIFE: "nightlife_final.json",
        EntityType.RESTAURANT: "restaurant_final.json",
    }
    city_ids = {"Đà Nẵng": "city_da_nang", "Quy Nhơn": "city_quy_nhon"}

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        master_directory: str | Path,
        *,
        overrides: Sequence[ExternalEntityMapping] = (),
    ) -> tuple[GoogleMapsMappingRegistry, GoogleMapsRegistryReport]:
        root = Path(master_directory)
        generated_at = self.clock()
        errors = []
        records = []
        source_files = []
        for expected_type, filename in self.filenames.items():
            path = root / filename
            source_files.append(str(path))
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"{filename}: {error}")
                continue
            items = document.get("data")
            declared_count = document.get("metadata", {}).get("total_count")
            if not isinstance(items, list):
                errors.append(f"{filename}: data must be a list")
                continue
            if declared_count != len(items):
                errors.append(
                    f"{filename}: metadata.total_count={declared_count} "
                    f"but data has {len(items)} records"
                )
            for index, item in enumerate(items):
                item_errors = self._validate_item(item, expected_type)
                if item_errors:
                    errors.extend(
                        f"{filename}[{index}]: {message}" for message in item_errors
                    )
                else:
                    records.append((item, expected_type, filename))

        ids = [item[0]["id"] for item in records]
        duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
        if duplicates:
            errors.append(f"duplicate master IDs: {', '.join(duplicates)}")
        override_by_entity = {}
        for override in overrides:
            if override.source_id != "google-maps-web":
                errors.append(
                    f"override {override.mapping_id} is not a google-maps-web mapping"
                )
            elif override.entity_id in override_by_entity:
                errors.append(f"duplicate override for {override.entity_id}")
            else:
                override_by_entity[override.entity_id] = override
        unknown_overrides = sorted(set(override_by_entity) - set(ids))
        if unknown_overrides:
            errors.append(
                f"overrides reference unknown IDs: {', '.join(unknown_overrides)}"
            )
        if errors:
            preview = "\n".join(errors[:30])
            suffix = f"\n... and {len(errors) - 30} more" if len(errors) > 30 else ""
            raise MasterDataValidationError(preview + suffix)

        mappings = []
        entity_counts: Counter[str] = Counter()
        city_counts: Counter[str] = Counter()
        identity_groups: defaultdict[str, list[str]] = defaultdict(list)
        for item, entity_type, filename in records:
            generated = self._mapping(item, entity_type, filename, generated_at)
            override = override_by_entity.get(item["id"])
            if override is not None:
                generated = generated.model_copy(
                    update={
                        "mapping_id": override.mapping_id,
                        "external_id": override.external_id,
                        "external_url": override.external_url,
                        "status": override.status,
                        "confidence": override.confidence,
                        "matched_at": override.matched_at,
                        "verified_at": override.verified_at,
                        "last_checked_at": override.last_checked_at,
                        "source_record_ids": override.source_record_ids,
                        "attributes": {
                            **generated.attributes,
                            **override.attributes,
                        },
                    }
                )
            generated = self._safe_external_identity(generated)
            mappings.append(generated)
            entity_counts[entity_type.value] += 1
            city_counts[item["city"]] += 1
            identity_groups[
                f"{self._key(item['name'])}|{self._key(item['city'])}"
            ].append(item["id"])
        mappings.sort(key=lambda item: item.entity_id)
        duplicate_candidates = sorted(
            (sorted(group) for group in identity_groups.values() if len(group) > 1),
            key=lambda group: group[0],
        )
        identity_errors = _mapping_registry_identity_errors(mappings)
        if identity_errors:
            raise MasterDataValidationError("\n".join(identity_errors))
        registry = GoogleMapsMappingRegistry(
            generated_at=generated_at,
            source_files=source_files,
            mappings=mappings,
        )
        report = GoogleMapsRegistryReport(
            generated_at=generated_at,
            total_records=len(records),
            mapping_count=len(mappings),
            entity_counts=dict(sorted(entity_counts.items())),
            city_counts=dict(sorted(city_counts.items())),
            overrides_applied=len(override_by_entity),
            reused_mapping_count=0,
            duplicate_identity_candidates=duplicate_candidates,
        )
        return registry, report

    def build_from_canonical_dataset(
        self,
        dataset: CanonicalActiveDataset,
        *,
        base_mappings: Sequence[ExternalEntityMapping] = (),
        overrides: Sequence[ExternalEntityMapping] = (),
        entity_types: Sequence[EntityType] = (
            EntityType.ATTRACTION,
            EntityType.CAFE,
            EntityType.NIGHTLIFE,
            EntityType.RESTAURANT,
        ),
    ) -> tuple[GoogleMapsMappingRegistry, GoogleMapsRegistryReport]:
        """Build an active-only registry from the canonical dataset snapshot."""

        from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset

        canonical = CanonicalActiveDataset.model_validate_json(
            dataset.model_dump_json()
        )
        selected_types = set(entity_types)
        if not selected_types:
            raise MasterDataValidationError(
                "canonical Google Maps registry requires at least one entity type"
            )
        if EntityType.HOTEL in selected_types:
            raise MasterDataValidationError(
                "hotel refresh is owned by Trivago, not the daily Maps registry"
            )
        base_by_id = self._canonical_mapping_index(base_mappings, "base")
        override_by_id = self._canonical_mapping_index(overrides, "override")
        active_records = [
            item for item in canonical.records if item.primary_type in selected_types
        ]
        active_ids = {item.place_id for item in active_records}
        unknown_overrides = sorted(set(override_by_id) - active_ids)
        if unknown_overrides:
            raise MasterDataValidationError(
                "canonical overrides reference inactive IDs: "
                + ", ".join(unknown_overrides)
            )
        generated_at = self.clock()
        mappings: list[ExternalEntityMapping] = []
        identity_groups: defaultdict[str, list[str]] = defaultdict(list)
        entity_counts: Counter[str] = Counter()
        city_counts: Counter[str] = Counter()
        reused_count = 0
        for record in sorted(active_records, key=lambda item: item.place_id):
            source_mapping = override_by_id.get(record.place_id) or base_by_id.get(
                record.place_id
            )
            mapping = self._canonical_mapping(
                record,
                source_mapping=source_mapping,
                generated_at=generated_at,
                dataset_id=canonical.dataset_id,
            )
            if source_mapping is not None:
                reused_count += 1
            mappings.append(mapping)
            entity_counts[record.primary_type.value] += 1
            city_counts[record.city] += 1
            identity_groups[
                f"{self._key(record.name)}|{self._key(record.city)}"
            ].append(record.place_id)
        duplicate_candidates = sorted(
            (sorted(group) for group in identity_groups.values() if len(group) > 1),
            key=lambda group: group[0],
        )
        identity_errors = _mapping_registry_identity_errors(mappings)
        if identity_errors:
            raise MasterDataValidationError("\n".join(identity_errors))
        registry = GoogleMapsMappingRegistry(
            generated_at=generated_at,
            source_files=[f"canonical-dataset:{canonical.dataset_id}"],
            mappings=mappings,
        )
        report = GoogleMapsRegistryReport(
            generated_at=generated_at,
            total_records=len(active_records),
            mapping_count=len(mappings),
            entity_counts=dict(sorted(entity_counts.items())),
            city_counts=dict(sorted(city_counts.items())),
            overrides_applied=len(override_by_id),
            reused_mapping_count=reused_count,
            duplicate_identity_candidates=duplicate_candidates,
        )
        return registry, report

    @staticmethod
    def _canonical_mapping_index(
        mappings: Sequence[ExternalEntityMapping],
        label: str,
    ) -> dict[str, ExternalEntityMapping]:
        result: dict[str, ExternalEntityMapping] = {}
        for mapping in mappings:
            if mapping.source_id != "google-maps-web":
                raise MasterDataValidationError(
                    f"canonical {label} mapping is not from google-maps-web: "
                    f"{mapping.mapping_id}"
                )
            if mapping.entity_id in result:
                raise MasterDataValidationError(
                    f"duplicate canonical {label} mapping: {mapping.entity_id}"
                )
            result[mapping.entity_id] = mapping
        return result

    def _canonical_mapping(
        self,
        record,
        *,
        source_mapping: ExternalEntityMapping | None,
        generated_at: datetime,
        dataset_id: str,
    ) -> ExternalEntityMapping:
        google_identity = next(
            (
                item
                for item in record.external_identities
                if item.get("source_id") == "google-maps-web"
            ),
            None,
        )
        attributes = {
            "search_query": ", ".join(
                value
                for value in (record.name, record.address, record.city, "Việt Nam")
                if value
            ),
            "master_name": record.name,
            "google_place_name": record.name,
            "master_address": record.address,
            "master_latitude": record.coordinates.lat,
            "master_longitude": record.coordinates.lng,
            "city_id": record.city_id,
            "master_city": record.city,
            "canonical_dataset_id": dataset_id,
        }
        if source_mapping is not None:
            merged = source_mapping.model_copy(
                update={
                    "entity_type": record.primary_type,
                    "attributes": {**source_mapping.attributes, **attributes},
                }
            )
            return self._safe_external_identity(
                merged,
                canonical_identity=google_identity,
                generated_at=generated_at,
            )
        if google_identity is not None:
            external_id = google_identity.get("external_id")
            external_url = google_identity.get("external_url")
            if not isinstance(external_id, str) or not external_id.strip():
                raise MasterDataValidationError(
                    f"canonical Google identity has no external ID: {record.place_id}"
                )
            if not isinstance(external_url, str) or not external_url.strip():
                raise MasterDataValidationError(
                    f"canonical Google identity has no URL: {record.place_id}"
                )
            mapping = ExternalEntityMapping(
                mapping_id=f"google-maps-{record.place_id}",
                entity_id=record.place_id,
                entity_type=record.primary_type,
                source_id="google-maps-web",
                external_id=external_id,
                external_url=external_url,
                status=MappingStatus.CONFIRMED,
                confidence=1.0,
                matched_at=generated_at,
                verified_at=generated_at,
                last_checked_at=generated_at,
                source_record_ids=record.provenance.source_record_ids,
                attributes=attributes,
            )
            return self._safe_external_identity(
                mapping,
                canonical_identity=google_identity,
                generated_at=generated_at,
            )
        return ExternalEntityMapping(
            mapping_id=f"google-maps-{record.place_id}",
            entity_id=record.place_id,
            entity_type=record.primary_type,
            source_id="google-maps-web",
            external_id=google_maps_search_placeholder(record.place_id),
            status=MappingStatus.AUTO_MATCHED,
            confidence=0.7,
            matched_at=generated_at,
            attributes=attributes,
        )

    def _mapping(
        self,
        item: dict[str, object],
        entity_type: EntityType,
        filename: str,
        generated_at: datetime,
    ) -> ExternalEntityMapping:
        coordinates = item["coordinates"]
        name = str(item["name"])
        city = str(item["city"])
        address = str(item.get("address") or "").strip()
        query = ", ".join(value for value in (name, address, city, "Việt Nam") if value)
        return ExternalEntityMapping(
            mapping_id=f"google-maps-{item['id']}",
            entity_id=str(item["id"]),
            entity_type=entity_type,
            source_id="google-maps-web",
            external_id=google_maps_search_placeholder(str(item["id"])),
            status=MappingStatus.AUTO_MATCHED,
            confidence=0.7,
            matched_at=generated_at,
            attributes={
                "search_query": query,
                "master_name": name,
                "google_place_name": name,
                "master_address": address or None,
                "master_latitude": coordinates["lat"],
                "master_longitude": coordinates["lng"],
                "city_id": self.city_ids[city],
                "master_city": city,
                "master_source_file": filename,
            },
        )

    @staticmethod
    def _safe_external_identity(
        mapping: ExternalEntityMapping,
        *,
        canonical_identity: dict[str, object] | None = None,
        generated_at: datetime | None = None,
    ) -> ExternalEntityMapping:
        """Normalize provider identity without confusing a name for a place ID."""

        canonical_values: list[object] = []
        canonical_url: object = None
        if canonical_identity is not None:
            canonical_url = canonical_identity.get("external_url")
            canonical_values.extend(
                (canonical_identity.get("external_id"), canonical_url)
            )
        canonical_tokens = google_maps_stable_external_ids(*canonical_values)
        attribute_values = [
            mapping.attributes.get(key) for key in _STABLE_ID_ATTRIBUTE_KEYS
        ]
        mapping_tokens = google_maps_stable_external_ids(
            mapping.external_id,
            mapping.external_url,
            *attribute_values,
        )
        tokens = canonical_tokens or mapping_tokens
        if mapping.status is MappingStatus.REJECTED:
            tokens = ()

        if len(tokens) == 1:
            token = tokens[0]
            url_candidates = (
                canonical_url,
                mapping.external_url,
                mapping.attributes.get("canonical_google_maps_url"),
            )
            stable_url = next(
                (
                    value
                    for value in url_candidates
                    if google_maps_stable_external_id(value) == token
                ),
                None,
            )
            update: dict[str, object] = {
                "external_id": token,
                "external_url": stable_url,
            }
            if canonical_tokens:
                update.update(
                    status=MappingStatus.CONFIRMED,
                    confidence=1.0,
                    verified_at=generated_at or mapping.verified_at,
                    last_checked_at=generated_at or mapping.last_checked_at,
                )
            return ExternalEntityMapping.model_validate(
                {
                    **mapping.model_dump(mode="python"),
                    **update,
                }
            )

        status = mapping.status
        verified_at = mapping.verified_at
        if status is MappingStatus.CONFIRMED:
            status = MappingStatus.PENDING_REVIEW
            verified_at = None
        return ExternalEntityMapping.model_validate(
            {
                **mapping.model_dump(mode="python"),
                "external_id": google_maps_search_placeholder(mapping.entity_id),
                "external_url": None,
                "status": status,
                "verified_at": verified_at,
            }
        )

    def _validate_item(self, item: object, expected_type: EntityType) -> list[str]:
        if not isinstance(item, dict):
            return ["record must be an object"]
        errors = []
        for field in ("id", "name", "city"):
            if not isinstance(item.get(field), str) or not str(item[field]).strip():
                errors.append(f"{field} must be a non-empty string")
        if item.get("entity_type") != expected_type.value:
            errors.append(f"entity_type must be {expected_type.value}")
        city = item.get("city")
        if city not in self.city_ids:
            errors.append(f"unsupported city: {city}")
        coordinates = item.get("coordinates")
        if not isinstance(coordinates, dict):
            errors.append("coordinates must be an object")
        else:
            latitude, longitude = coordinates.get("lat"), coordinates.get("lng")
            if not isinstance(latitude, (int, float)) or not -90 <= latitude <= 90:
                errors.append("coordinates.lat is invalid")
            if not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
                errors.append("coordinates.lng is invalid")
        return errors

    @staticmethod
    def _key(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold())
        ascii_text = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


class GoogleMapsRegistryWriter:
    """Atomically replaces derived registry and report files."""

    @staticmethod
    def write(document: NexTripModel, destination: str | Path) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                document.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
