from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
)

if TYPE_CHECKING:
    from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset


class TrivagoRegistryError(ValueError):
    """Raised when verified hotel master data cannot form a safe registry."""


class TrivagoRegistryStatus(StrEnum):
    UNRESOLVED = "unresolved"
    CONFIRMED = "confirmed"
    REVIEW = "review"
    REJECTED = "rejected"
    PROVIDER_NOT_LISTED = "provider_not_listed"
    IDENTITY_REVERIFY = "identity_reverify"


class TrivagoSearchReviewOverride(NexTripModel):
    """Source-backed lookup hint approved for one unresolved hotel.

    An expected identity is not published from this file alone. The provider
    must return the exact external/property identity and provider-owned name
    in fresh entity-owned evidence before the resolver can confirm it.
    """

    entity_id: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    expected_external_id: str | None = None
    expected_property_id: str | None = Field(default=None, pattern=r"^\d+$")
    expected_name: str | None = None
    final_status: TrivagoRegistryStatus | None = None
    reviewer: str | None = None
    reviewed_at: AwareDatetime | None = None
    reason: str | None = None
    evidence: list[TrivagoSearchReviewEvidence] = Field(default_factory=list)
    supporting_urls: list[HttpUrl] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_review_target(self) -> TrivagoSearchReviewOverride:
        normalized_aliases: list[str] = []
        alias_keys: set[str] = set()
        for value in self.aliases:
            alias = value.strip()
            if not alias:
                continue
            key = " ".join(
                "".join(
                    character
                    for character in unicodedata.normalize("NFKD", alias.casefold())
                    if not unicodedata.combining(character)
                ).split()
            )
            if key in alias_keys:
                continue
            normalized_aliases.append(alias)
            alias_keys.add(key)
        object.__setattr__(self, "aliases", normalized_aliases)
        target_fields = (
            self.expected_external_id,
            self.expected_property_id,
            self.expected_name,
        )
        if any(value is not None for value in target_fields):
            if not all(value is not None for value in target_fields):
                raise ValueError(
                    "review target requires external_id, property_id, and name"
                )
            if not self.reviewer or self.reviewed_at is None or not self.reason:
                raise ValueError(
                    "review target requires reviewer, reviewed_at, and reason"
                )
            if not self.evidence:
                raise ValueError("review target requires immutable evidence")
        terminal_statuses = {
            TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
            TrivagoRegistryStatus.IDENTITY_REVERIFY,
        }
        if self.final_status is not None and self.final_status not in terminal_statuses:
            raise ValueError(
                "search review final_status must be provider_not_listed or "
                "identity_reverify"
            )
        if self.final_status is not None:
            if any(value is not None for value in target_fields):
                raise ValueError(
                    "terminal search review cannot also declare an expected identity"
                )
            if not self.reviewer or self.reviewed_at is None or not self.reason:
                raise ValueError(
                    "terminal search review requires reviewer, reviewed_at, and reason"
                )
            if not self.evidence:
                raise ValueError("terminal search review requires immutable evidence")
        return self


class TrivagoSearchReviewEvidence(NexTripModel):
    path: str = Field(min_length=1)
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class TrivagoSearchReviewConfig(NexTripModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    overrides: list[TrivagoSearchReviewOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_hotels(self) -> TrivagoSearchReviewConfig:
        entity_ids = [item.entity_id for item in self.overrides]
        duplicates = sorted(
            entity_id for entity_id, count in Counter(entity_ids).items() if count > 1
        )
        if duplicates:
            raise ValueError(
                "duplicate Trivago search review overrides: " + ", ".join(duplicates)
            )
        return self


class TrivagoHotelRegistryEntry(NexTripModel):
    entity_id: str = Field(min_length=1)
    master_name: str = Field(min_length=1)

    trivago_name: str | None = None
    city: str = Field(min_length=1)
    address: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    search_query: str = Field(min_length=1)
    # Search aliases are discovery hints only.  The resolver deliberately
    # continues to score candidates against ``master_name`` (or an already
    # confirmed provider identity), so an alias can never prove identity.
    search_aliases: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    status: TrivagoRegistryStatus = TrivagoRegistryStatus.UNRESOLVED
    external_id: str | None = None
    external_url: HttpUrl | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    matched_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    source_record_ids: list[str] = Field(default_factory=list)
    review_target_external_id: str | None = None
    review_target_property_id: str | None = Field(default=None, pattern=r"^\d+$")
    review_target_name: str | None = None
    review_target_reviewer: str | None = None
    review_target_reviewed_at: AwareDatetime | None = None
    review_target_reason: str | None = None
    review_target_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    review_target_evidence: list[TrivagoSearchReviewEvidence] = Field(
        default_factory=list
    )
    review_target_supporting_urls: list[HttpUrl] = Field(default_factory=list)

    @property
    def search_name(self) -> str:
        """Use the provider-owned name after identity has been confirmed."""

        return self.trivago_name or self.master_name

    @property
    def identity_search_queries(self) -> tuple[str, ...]:
        """Return the persisted, ordered and de-duplicated lookup queries."""

        values = self.search_queries or [self.search_query]
        return tuple(dict.fromkeys([self.search_query, *values]))

    @model_validator(mode="after")
    def validate_resolution_state(self) -> TrivagoHotelRegistryEntry:
        if self.status is TrivagoRegistryStatus.CONFIRMED:
            if not self.external_id:
                raise ValueError("confirmed registry entry requires external_id")
            if self.verified_at is None:
                raise ValueError("confirmed registry entry requires verified_at")
        if self.external_id is not None and not self.external_id.strip():
            raise ValueError("external_id cannot be blank")
        if self.status in {
            TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
            TrivagoRegistryStatus.IDENTITY_REVERIFY,
        }:
            if self.external_id is not None:
                raise ValueError("terminal registry entry cannot contain external_id")
            if (
                not self.review_target_reviewer
                or self.review_target_reviewed_at is None
                or not self.review_target_reason
                or not self.review_target_hash
                or not self.review_target_evidence
            ):
                raise ValueError("terminal registry entry requires review provenance")
        if len(self.search_aliases) != len(dict.fromkeys(self.search_aliases)):
            raise ValueError("search_aliases must be unique and ordered")
        review_target_fields = (
            self.review_target_external_id,
            self.review_target_property_id,
            self.review_target_name,
        )
        if any(value is not None for value in review_target_fields) and not all(
            value is not None for value in review_target_fields
        ):
            raise ValueError("registry review target identity must be complete")
        if all(value is not None for value in review_target_fields) and (
            not self.review_target_reviewer
            or self.review_target_reviewed_at is None
            or not self.review_target_reason
            or not self.review_target_hash
            or not self.review_target_evidence
        ):
            raise ValueError("registry review target provenance must be complete")
        # Backward compatibility for legacy registries and for callers that
        # intentionally replace ``search_query`` through ``model_copy``.
        # The primary query always wins and stale duplicates are discarded.
        search_queries = list(dict.fromkeys([self.search_query, *self.search_queries]))
        object.__setattr__(self, "search_queries", search_queries)
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
                "search_aliases": self.search_aliases,
                "search_queries": self.search_queries,
                "review_target_external_id": self.review_target_external_id,
                "review_target_property_id": self.review_target_property_id,
                "review_target_name": self.review_target_name,
                "review_target_reviewer": self.review_target_reviewer,
                "review_target_reviewed_at": (
                    self.review_target_reviewed_at.isoformat()
                    if self.review_target_reviewed_at is not None
                    else None
                ),
                "review_target_reason": self.review_target_reason,
                "review_target_hash": self.review_target_hash,
                "review_target_evidence": [
                    item.model_dump(mode="json") for item in self.review_target_evidence
                ],
                "review_target_supporting_urls": [
                    str(url) for url in self.review_target_supporting_urls
                ],
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
    search_review_overrides_applied: int = Field(default=0, ge=0)


class TrivagoRegistryBuilder:
    """Build one deterministic Trivago search target per verified hotel."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        master_file: str | Path,
        *,
        overrides: Sequence[ExternalEntityMapping] = (),
        search_review_overrides: Sequence[TrivagoSearchReviewOverride] = (),
        evidence_root: str | Path = ".",
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

        return self._build_registry(
            valid_records,
            source_file=str(path),
            overrides=overrides,
            search_review_overrides=search_review_overrides,
            evidence_root=evidence_root,
            initial_errors=errors,
        )

    def build_from_canonical_dataset(
        self,
        dataset: CanonicalActiveDataset,
        *,
        overrides: Sequence[ExternalEntityMapping] = (),
        search_review_overrides: Sequence[TrivagoSearchReviewOverride] = (),
        evidence_root: str | Path = ".",
    ) -> tuple[TrivagoHotelRegistry, TrivagoRegistryReport]:
        """Build active hotel targets from one content-addressed canonical snapshot."""

        # Re-validate the complete content-addressed document at this boundary.
        # A caller cannot substitute a partially constructed model and bypass
        # the canonical dataset hash/identity invariants.
        from nextrip_pipeline.canonical.dataset import CanonicalActiveDataset

        canonical = CanonicalActiveDataset.model_validate_json(
            dataset.model_dump_json()
        )
        records: list[dict[str, object]] = []
        for record in canonical.records:
            if record.primary_type is not EntityType.HOTEL:
                continue
            records.append(
                {
                    "id": record.place_id,
                    "entity_type": EntityType.HOTEL.value,
                    "name": record.name,
                    "city": record.city,
                    "address": record.address,
                    "coordinates": {
                        "lat": record.coordinates.lat,
                        "lng": record.coordinates.lng,
                    },
                }
            )
        if not records:
            raise TrivagoRegistryError(
                "canonical dataset does not contain any active hotel records"
            )
        return self._build_registry(
            records,
            source_file=f"canonical-dataset:{canonical.dataset_id}",
            overrides=overrides,
            search_review_overrides=search_review_overrides,
            evidence_root=evidence_root,
        )

    def _build_registry(
        self,
        valid_records: Sequence[dict[str, object]],
        *,
        source_file: str,
        overrides: Sequence[ExternalEntityMapping],
        search_review_overrides: Sequence[TrivagoSearchReviewOverride],
        evidence_root: str | Path,
        initial_errors: Sequence[str] = (),
    ) -> tuple[TrivagoHotelRegistry, TrivagoRegistryReport]:
        errors = list(initial_errors)
        entity_ids = [str(record["id"]) for record in valid_records]

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
        errors.extend(self._validate_override_collisions(override_by_entity.values()))
        search_review_by_entity: dict[str, TrivagoSearchReviewOverride] = {}
        for search_override in search_review_overrides:
            if search_override.entity_id in search_review_by_entity:
                errors.append(
                    f"duplicate search review override for {search_override.entity_id}"
                )
            else:
                search_review_by_entity[search_override.entity_id] = search_override
            errors.extend(
                self._validate_search_review_evidence(
                    search_override,
                    evidence_root=Path(evidence_root),
                )
            )
        unknown_search_overrides = sorted(
            set(search_review_by_entity) - set(entity_ids)
        )
        if unknown_search_overrides:
            errors.append(
                "search review overrides reference unknown hotel IDs: "
                + ", ".join(unknown_search_overrides)
            )
        for entity_id, search_override in search_review_by_entity.items():
            if (
                search_override.final_status is not None
                and entity_id in override_by_entity
                and override_by_entity[entity_id].status is MappingStatus.CONFIRMED
            ):
                errors.append(
                    f"search review {entity_id} terminal status conflicts with "
                    "a confirmed mapping"
                )
        if errors:
            raise TrivagoRegistryError("\n".join(errors[:30]))

        generated_at = self.clock()
        entries = [
            self._entry(
                record,
                override_by_entity.get(str(record["id"])),
                search_review_by_entity.get(str(record["id"])),
            )
            for record in valid_records
        ]
        entries.sort(key=lambda entry: entry.entity_id)
        status_counts = Counter(entry.status.value for entry in entries)
        city_counts = Counter(entry.city for entry in entries)
        registry = TrivagoHotelRegistry(
            generated_at=generated_at,
            source_file=source_file,
            entries=entries,
        )
        report = TrivagoRegistryReport(
            generated_at=generated_at,
            total_records=len(valid_records),
            registry_count=len(entries),
            status_counts=dict(sorted(status_counts.items())),
            city_counts=dict(sorted(city_counts.items())),
            overrides_applied=len(override_by_entity),
            search_review_overrides_applied=len(search_review_by_entity),
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
        search_review_override: TrivagoSearchReviewOverride | None,
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
        search_name = trivago_name or name
        derived_aliases = TrivagoRegistryBuilder._search_aliases(
            search_name,
            master_name=name,
        )
        configured_aliases = (
            search_review_override.aliases if search_review_override is not None else []
        )
        aliases: list[str] = []
        alias_keys = {TrivagoRegistryBuilder._alias_key(search_name)}
        for alias in [*configured_aliases, *derived_aliases]:
            alias_key = TrivagoRegistryBuilder._alias_key(alias)
            if not alias_key or alias_key in alias_keys:
                continue
            aliases.append(alias)
            alias_keys.add(alias_key)
        # Keep the historically reliable name/city lookup first.  A street
        # address improves recall for ambiguous names, but Trivago may treat a
        # long address query as an unknown destination; it is therefore a
        # bounded retry and must never replace the primary query.
        search_queries: list[str] = []
        candidate_names = [search_name, *aliases]
        for candidate_name in candidate_names:
            search_queries.append(
                TrivagoRegistryBuilder._search_query(
                    candidate_name,
                    address=None,
                    city=city,
                )
            )
        if address:
            for candidate_name in candidate_names:
                search_queries.append(
                    TrivagoRegistryBuilder._search_query(
                        candidate_name,
                        address=address,
                        city=city,
                    )
                )
        search_queries = list(dict.fromkeys(search_queries))
        query = search_queries[0]
        review_values = TrivagoRegistryBuilder._review_target_values(
            search_review_override
        )
        if override is None:
            return TrivagoHotelRegistryEntry(
                entity_id=entity_id,
                master_name=name,
                city=city,
                address=address,
                latitude=latitude,
                longitude=longitude,
                search_query=query,
                search_aliases=aliases,
                search_queries=search_queries,
                status=(
                    search_review_override.final_status
                    if search_review_override is not None
                    and search_review_override.final_status is not None
                    else TrivagoRegistryStatus.UNRESOLVED
                ),
                **review_values,
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
            search_aliases=aliases,
            search_queries=search_queries,
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
            **review_values,
        )

    @staticmethod
    def _review_target_values(
        value: TrivagoSearchReviewOverride | None,
    ) -> dict[str, object]:
        if value is None:
            return {}
        return {
            "review_target_external_id": value.expected_external_id,
            "review_target_property_id": value.expected_property_id,
            "review_target_name": value.expected_name,
            "review_target_reviewer": value.reviewer,
            "review_target_reviewed_at": value.reviewed_at,
            "review_target_reason": value.reason,
            "review_target_hash": hashlib.sha256(
                json.dumps(
                    value.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "review_target_evidence": value.evidence,
            "review_target_supporting_urls": value.supporting_urls,
        }

    @staticmethod
    def _attribute_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _validate_search_review_evidence(
        value: TrivagoSearchReviewOverride,
        *,
        evidence_root: Path,
    ) -> list[str]:
        errors: list[str] = []
        target_found = False
        entity_evidence_found = False
        for evidence in value.evidence:
            path = Path(evidence.path)
            path = path if path.is_absolute() else evidence_root / path
            try:
                content = path.read_bytes()
            except OSError as error:
                errors.append(
                    f"search review {value.entity_id} evidence unreadable: {error}"
                )
                continue
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != evidence.file_sha256:
                errors.append(
                    f"search review {value.entity_id} evidence hash mismatch: {path}"
                )
                continue
            try:
                document = json.loads(content)
            except json.JSONDecodeError as error:
                errors.append(
                    f"search review {value.entity_id} evidence is not JSON: {error}"
                )
                continue
            if document.get("entity_id") == value.entity_id:
                entity_evidence_found = True
            tasks = document.get("tasks")
            if isinstance(tasks, list) and any(
                isinstance(task, dict) and task.get("entity_id") == value.entity_id
                for task in tasks
            ):
                entity_evidence_found = True
            candidates = document.get("candidates")
            if not isinstance(candidates, list):
                if value.expected_external_id is not None:
                    errors.append(
                        f"search review {value.entity_id} evidence lacks candidates: "
                        f"{path}"
                    )
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                external_url = candidate.get("external_url")
                if (
                    value.expected_external_id is not None
                    and value.expected_property_id is not None
                    and value.expected_name is not None
                    and candidate.get("external_id") == value.expected_external_id
                    and candidate.get("name") == value.expected_name
                    and candidate.get("city_evidence") == "match"
                    and isinstance(external_url, str)
                    and re.search(
                        rf"(?:[?&;]|^)search=100-{re.escape(value.expected_property_id)}(?:;|&|$)",
                        external_url,
                    )
                ):
                    target_found = True
        if value.final_status is not None and not entity_evidence_found:
            errors.append(
                f"search review {value.entity_id} terminal evidence is not entity-owned"
            )
        if value.expected_external_id is not None and not target_found:
            errors.append(
                f"search review {value.entity_id} target is absent from pinned evidence"
            )
        return errors

    @staticmethod
    def _validate_override_collisions(
        overrides: Sequence[ExternalEntityMapping],
    ) -> list[str]:
        errors: list[str] = []
        external_owners: dict[str, str] = {}
        property_owners: dict[str, str] = {}
        for override in overrides:
            if override.status is not MappingStatus.CONFIRMED:
                continue
            external_owner = external_owners.setdefault(
                override.external_id, override.entity_id
            )
            if external_owner != override.entity_id:
                errors.append(
                    "confirmed Trivago external_id collision "
                    f"{override.external_id}: {external_owner}, {override.entity_id}"
                )
            property_id = TrivagoRegistryBuilder._property_id(override.external_url)
            if property_id is None:
                continue
            property_owner = property_owners.setdefault(property_id, override.entity_id)
            if property_owner != override.entity_id:
                errors.append(
                    "confirmed Trivago property_id collision "
                    f"{property_id}: {property_owner}, {override.entity_id}"
                )
        return errors

    @staticmethod
    def _property_id(value: object) -> str | None:
        if value is None:
            return None
        match = re.search(r"(?:[?&;]|^)search=100-(\d+)(?:;|&|$)", str(value))
        return match.group(1) if match else None

    @staticmethod
    def _search_query(name: str, *, address: str | None, city: str) -> str:
        """Build a deterministic, address-aware provider lookup query."""

        components = [name]
        if address:
            components.append(address)
        # A reviewed alias may already be a complete provider query (for
        # example one that deliberately uses Trivago's English ``Vietnam``
        # destination spelling). Consider the name and address together so we
        # do not append the city/country twice and destroy that exact lookup.
        location_key = TrivagoRegistryBuilder._ascii_alias(
            " ".join(value for value in (name, address) if value)
        )
        if TrivagoRegistryBuilder._ascii_alias(city) not in location_key:
            components.append(city)
        if "viet nam" not in location_key and "vietnam" not in location_key:
            components.append("Việt Nam")
        return ", ".join(dict.fromkeys(components))

    @classmethod
    def _search_aliases(cls, primary_name: str, *, master_name: str) -> list[str]:
        """Create conservative deterministic aliases, never identity facts.

        Variants retain the complete hotel name.  We do not remove generic
        words such as ``hotel`` or ``homestay`` because doing so turns short
        names into unsafe, broad searches in dense tourist areas.
        """

        candidates: list[str] = []
        if cls._alias_key(master_name) != cls._alias_key(primary_name):
            candidates.append(master_name)
        for value in (primary_name, master_name):
            normalized = cls._normalized_alias(value)
            if normalized != value:
                candidates.append(normalized)
            ascii_alias = cls._ascii_alias(normalized)
            if ascii_alias and cls._alias_key(ascii_alias) != cls._alias_key(value):
                candidates.append(ascii_alias)

        aliases: list[str] = []
        seen = {cls._alias_key(primary_name)}
        for candidate in candidates:
            key = cls._alias_key(candidate)
            if not key or key in seen:
                continue
            aliases.append(candidate)
            seen.add(key)
            if len(aliases) == 4:
                break
        return aliases

    @staticmethod
    def _normalized_alias(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        normalized = re.sub(r"[\s,;:|/\\]+", " ", normalized)
        return normalized.strip(" -_")

    @staticmethod
    def _ascii_alias(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        ascii_text = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9&]+", " ", ascii_text).strip()

    @classmethod
    def _alias_key(cls, value: str) -> str:
        return cls._normalized_alias(value).casefold()


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
