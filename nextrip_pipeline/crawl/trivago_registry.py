from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
)


class TrivagoRegistryError(ValueError):
    """Raised when verified hotel master data cannot form a safe registry."""


class TrivagoRegistryStatus(StrEnum):
    UNRESOLVED = "unresolved"
    CONFIRMED = "confirmed"
    REVIEW = "review"
    REJECTED = "rejected"


class TrivagoHotelRegistryEntry(NexTripModel):
    entity_id: str = Field(min_length=1)
    master_name: str = Field(min_length=1)
    # Provider-owned display name. Optional for backward compatibility with
    # registries created before Trivago canonical names were retained.
    trivago_name: str | None = None
    city: str = Field(min_length=1)
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    search_query: str = Field(min_length=1)
    status: TrivagoRegistryStatus = TrivagoRegistryStatus.UNRESOLVED
    external_id: str | None = None
    external_url: HttpUrl | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    matched_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    source_record_ids: list[str] = Field(default_factory=list)

    @property
    def search_name(self) -> str:
        """Use the provider-owned name after identity has been confirmed."""

        return self.trivago_name or self.master_name

    @model_validator(mode="after")
    def validate_resolution_state(self) -> TrivagoHotelRegistryEntry:
        if self.status is TrivagoRegistryStatus.CONFIRMED:
            if not self.external_id:
                raise ValueError("confirmed registry entry requires external_id")
            if self.verified_at is None:
                raise ValueError("confirmed registry entry requires verified_at")
        if self.external_id is not None and not self.external_id.strip():
            raise ValueError("external_id cannot be blank")
        return self

    def to_mapping(self) -> ExternalEntityMapping:
        if self.status is not TrivagoRegistryStatus.CONFIRMED:
            raise ValueError("only a confirmed Trivago entry can become a mapping")
        assert self.external_id is not None
        assert self.verified_at is not None
        return ExternalEntityMapping(
            mapping_id=f"trivago-mcp-{self.entity_id}",
            entity_id=self.entity_id,
            entity_type=EntityType.HOTEL,
            source_id="trivago-mcp",
            external_id=self.external_id,
            external_url=self.external_url,
            status=MappingStatus.CONFIRMED,
            confidence=self.confidence,
            matched_at=self.matched_at or self.verified_at,
            verified_at=self.verified_at,
            last_checked_at=self.verified_at,
            source_record_ids=self.source_record_ids,
            attributes={
                # ``hotel_name`` is the legacy master-name key and must remain
                # available to existing consumers.
                "hotel_name": self.master_name,
                "master_name": self.master_name,
                "trivago_name": self.trivago_name,
                "destination": self.city,
                "master_address": self.address,
                "master_latitude": self.latitude,
                "master_longitude": self.longitude,
                "search_query": self.search_query,
            },
        )


class TrivagoHotelRegistry(NexTripModel):
    registry_version: str = "1.0.0"
    generated_at: AwareDatetime
    source_file: str = Field(min_length=1)
    entries: list[TrivagoHotelRegistryEntry] = Field(min_length=1)

    def confirmed_mappings(self) -> list[ExternalEntityMapping]:
        return [
            entry.to_mapping()
            for entry in self.entries
            if entry.status is TrivagoRegistryStatus.CONFIRMED
        ]


class TrivagoRegistryReport(NexTripModel):
    generated_at: AwareDatetime
    total_records: int = Field(ge=0)
    registry_count: int = Field(ge=0)
    status_counts: dict[str, int]
    city_counts: dict[str, int]
    overrides_applied: int = Field(ge=0)


class TrivagoRegistryBuilder:
    """Build one deterministic Trivago search target per verified hotel."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        master_file: str | Path,
        *,
        overrides: Sequence[ExternalEntityMapping] = (),
    ) -> tuple[TrivagoHotelRegistry, TrivagoRegistryReport]:
        path = Path(master_file)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TrivagoRegistryError(str(error)) from error
        records = document.get("data")
        if not isinstance(records, list):
            raise TrivagoRegistryError("hotel master data must contain a data list")

        errors: list[str] = []
        declared_count = document.get("metadata", {}).get("total_count")
        if declared_count != len(records):
            errors.append(
                f"metadata.total_count={declared_count} but data has {len(records)}"
            )
        valid_records: list[dict[str, object]] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                errors.append(f"data[{index}] must be an object")
                continue
            item_errors = self._validate_record(record)
            errors.extend(f"data[{index}]: {message}" for message in item_errors)
            if not item_errors:
                valid_records.append(record)

        entity_ids = [str(record["id"]) for record in valid_records]
        duplicate_ids = sorted(
            entity_id for entity_id, count in Counter(entity_ids).items() if count > 1
        )
        if duplicate_ids:
            errors.append(f"duplicate hotel IDs: {', '.join(duplicate_ids)}")

        override_by_entity: dict[str, ExternalEntityMapping] = {}
        for override in overrides:
            if override.source_id != "trivago-mcp":
                errors.append(f"override {override.mapping_id} is not from trivago-mcp")
            elif override.entity_type is not EntityType.HOTEL:
                errors.append(f"override {override.mapping_id} is not a hotel")
            elif override.entity_id in override_by_entity:
                errors.append(f"duplicate override for {override.entity_id}")
            else:
                override_by_entity[override.entity_id] = override
        unknown_overrides = sorted(set(override_by_entity) - set(entity_ids))
        if unknown_overrides:
            errors.append(
                f"overrides reference unknown hotel IDs: {', '.join(unknown_overrides)}"
            )
        if errors:
            raise TrivagoRegistryError("\n".join(errors[:30]))

        generated_at = self.clock()
        entries = [
            self._entry(record, override_by_entity.get(str(record["id"])))
            for record in valid_records
        ]
        entries.sort(key=lambda entry: entry.entity_id)
        status_counts = Counter(entry.status.value for entry in entries)
        city_counts = Counter(entry.city for entry in entries)
        registry = TrivagoHotelRegistry(
            generated_at=generated_at,
            source_file=str(path),
            entries=entries,
        )
        report = TrivagoRegistryReport(
            generated_at=generated_at,
            total_records=len(valid_records),
            registry_count=len(entries),
            status_counts=dict(sorted(status_counts.items())),
            city_counts=dict(sorted(city_counts.items())),
            overrides_applied=len(override_by_entity),
        )
        return registry, report

    @staticmethod
    def _validate_record(record: dict[str, object]) -> list[str]:
        errors = []
        for field in ("id", "name", "city"):
            if not isinstance(record.get(field), str) or not str(record[field]).strip():
                errors.append(f"{field} must be a non-empty string")
        if record.get("entity_type") != EntityType.HOTEL.value:
            errors.append("entity_type must be hotel")
        address = record.get("address")
        if address is not None and not isinstance(address, str):
            errors.append("address must be a string or null")
        coordinates = record.get("coordinates")
        if coordinates is not None:
            if not isinstance(coordinates, dict):
                errors.append("coordinates must be an object or null")
            else:
                latitude = coordinates.get("lat")
                longitude = coordinates.get("lng")
                if not TrivagoRegistryBuilder._valid_coordinate(latitude, -90, 90):
                    errors.append("coordinates.lat must be between -90 and 90")
                if not TrivagoRegistryBuilder._valid_coordinate(longitude, -180, 180):
                    errors.append("coordinates.lng must be between -180 and 180")
        return errors

    @staticmethod
    def _valid_coordinate(value: object, minimum: float, maximum: float) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and minimum <= float(value) <= maximum
        )

    @staticmethod
    def _entry(
        record: dict[str, object],
        override: ExternalEntityMapping | None,
    ) -> TrivagoHotelRegistryEntry:
        entity_id = str(record["id"])
        name = str(record["name"])
        city = str(record["city"])
        address = str(record.get("address") or "").strip() or None
        coordinates = record.get("coordinates")
        latitude = None
        longitude = None
        if isinstance(coordinates, dict):
            coordinate_latitude = coordinates.get("lat")
            coordinate_longitude = coordinates.get("lng")
            if isinstance(coordinate_latitude, (int, float)) and not isinstance(
                coordinate_latitude, bool
            ):
                latitude = float(coordinate_latitude)
            if isinstance(coordinate_longitude, (int, float)) and not isinstance(
                coordinate_longitude, bool
            ):
                longitude = float(coordinate_longitude)
        # Discovery starts from the verified master name. Confirmed refreshes
        # must query the provider-owned name so a rename is not mistaken for
        # missing inventory.
        trivago_name = (
            TrivagoRegistryBuilder._attribute_text(
                override.attributes.get("trivago_name")
            )
            if override is not None
            else None
        )
        query = ", ".join((trivago_name or name, city, "Việt Nam"))
        if override is None:
            return TrivagoHotelRegistryEntry(
                entity_id=entity_id,
                master_name=name,
                city=city,
                address=address,
                latitude=latitude,
                longitude=longitude,
                search_query=query,
            )

        status = {
            MappingStatus.CONFIRMED: TrivagoRegistryStatus.CONFIRMED,
            MappingStatus.AUTO_MATCHED: TrivagoRegistryStatus.REVIEW,
            MappingStatus.PENDING_REVIEW: TrivagoRegistryStatus.REVIEW,
            MappingStatus.REJECTED: TrivagoRegistryStatus.REJECTED,
        }[override.status]
        return TrivagoHotelRegistryEntry(
            entity_id=entity_id,
            master_name=name,
            trivago_name=trivago_name,
            city=city,
            address=address,
            latitude=latitude,
            longitude=longitude,
            search_query=query,
            status=status,
            external_id=override.external_id,
            external_url=(
                override.external_url
                or override.attributes.get("trivago_accommodation_url")
            ),
            confidence=override.confidence,
            matched_at=override.matched_at,
            verified_at=override.verified_at,
            source_record_ids=override.source_record_ids,
        )

    @staticmethod
    def _attribute_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None


class TrivagoRegistryWriter:
    """Atomically replace derived registry and report documents."""

    @staticmethod
    def write(document: NexTripModel, destination: str | Path) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(document.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
