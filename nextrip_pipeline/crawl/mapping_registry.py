from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
)


class MasterDataValidationError(ValueError):
    """Raised when master data cannot safely produce a mapping registry."""


class GoogleMapsMappingRegistry(NexTripModel):
    registry_version: str = "1.0.0"
    generated_at: AwareDatetime
    source_files: list[str] = Field(min_length=1)
    mappings: list[ExternalEntityMapping] = Field(min_length=1)


class GoogleMapsRegistryReport(NexTripModel):
    generated_at: AwareDatetime
    total_records: int = Field(ge=0)
    mapping_count: int = Field(ge=0)
    entity_counts: dict[str, int]
    city_counts: dict[str, int]
    overrides_applied: int = Field(ge=0)
    duplicate_identity_candidates: list[list[str]] = Field(default_factory=list)


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
            duplicate_identity_candidates=duplicate_candidates,
        )
        return registry, report

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
            external_id=name,
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
