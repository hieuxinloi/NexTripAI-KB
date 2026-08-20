from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, Field, field_validator, model_validator

from nextrip_pipeline.schemas import EntityType, NexTripModel


_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def stable_sha256(value: Any) -> str:
    """Return a platform-independent digest for one JSON-compatible value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_identifier(namespace: str, *parts: str, length: int = 20) -> str:
    """Build an opaque, deterministic identifier without exposing raw names."""

    digest = stable_sha256([namespace, *parts])
    return f"{namespace}_{digest[:length]}"


def _normalized_tags(values: list[str]) -> list[str]:
    normalized: set[str] = set()
    for value in values:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
        if not cleaned:
            raise ValueError("tags must not contain blank values")
        normalized.add(cleaned)
    return sorted(normalized)


def _sorted_types(values: list[EntityType]) -> list[EntityType]:
    return sorted(set(values), key=lambda item: item.value)


class LegacyPlaceSlot(NexTripModel):
    """One immutable entity/city slot from the pre-canonical master data."""

    legacy_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    city_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    primary_type: EntityType
    secondary_types: list[EntityType] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("secondary_types")
    @classmethod
    def normalize_secondary_types(
        cls, values: list[EntityType]
    ) -> list[EntityType]:
        return _sorted_types(values)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return _normalized_tags(values)

    @model_validator(mode="after")
    def primary_is_not_secondary(self) -> LegacyPlaceSlot:
        if self.primary_type in self.secondary_types:
            raise ValueError("primary_type must not be repeated in secondary_types")
        return self


class DuplicateIdentityDecision(NexTripModel):
    """An explicit, reviewable decision to merge legacy IDs into one place."""

    keeper_legacy_place_id: str = Field(
        min_length=1,
        pattern=_IDENTIFIER_PATTERN,
    )
    duplicate_legacy_place_ids: list[str] = Field(min_length=1)
    reason: str = Field(default="verified_duplicate", min_length=1)

    @field_validator("duplicate_legacy_place_ids")
    @classmethod
    def normalize_duplicate_ids(cls, values: list[str]) -> list[str]:
        cleaned = sorted({value.strip() for value in values if value.strip()})
        if len(cleaned) != len(values):
            raise ValueError("duplicate_legacy_place_ids must be unique and non-empty")
        for value in cleaned:
            if re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
                raise ValueError(f"invalid duplicate legacy place ID: {value}")
        return cleaned

    @model_validator(mode="after")
    def keeper_is_not_a_duplicate(self) -> DuplicateIdentityDecision:
        if self.keeper_legacy_place_id in self.duplicate_legacy_place_ids:
            raise ValueError("keeper cannot also be a duplicate legacy ID")
        return self


class DistinctIdentityDecision(NexTripModel):
    """An explicit human decision that candidate IDs are separate places."""

    place_ids: list[str] = Field(min_length=2)
    reason: str = Field(default="human_review_verified_distinct", min_length=1)

    @field_validator("place_ids")
    @classmethod
    def normalize_place_ids(cls, values: list[str]) -> list[str]:
        cleaned = sorted({value.strip() for value in values if value.strip()})
        if len(cleaned) != len(values):
            raise ValueError("place_ids must be unique and non-empty")
        for value in cleaned:
            if re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
                raise ValueError(f"invalid distinct legacy place ID: {value}")
        return cleaned

    @property
    def group_id(self) -> str:
        """Return the candidate-group ID this decision resolves."""

        return stable_identifier("duplicate_group", *self.place_ids)


class VacancyReplacementDecision(NexTripModel):
    """Explicitly assign one new place identity to a retired quota slot."""

    retired_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    replacement_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    reason: str = Field(default="verified_distinct_replacement", min_length=1)

    @model_validator(mode="after")
    def replacement_is_not_the_retired_id(self) -> VacancyReplacementDecision:
        if self.replacement_place_id == self.retired_place_id:
            raise ValueError("replacement_place_id cannot reuse retired_place_id")
        return self


def canonical_identity_payload(
    *,
    canonical_place_id: str,
    active_legacy_place_id: str,
    legacy_place_ids: list[str],
    city_id: str,
    primary_type: EntityType,
    secondary_types: list[EntityType],
    tags: list[str],
) -> dict[str, object]:
    return {
        "canonical_place_id": canonical_place_id,
        "active_legacy_place_id": active_legacy_place_id,
        "legacy_place_ids": legacy_place_ids,
        "city_id": city_id,
        "primary_type": primary_type.value,
        "secondary_types": [item.value for item in secondary_types],
        "tags": tags,
    }


class CanonicalPlaceIdentity(NexTripModel):
    """The sole active identity for one physical place."""

    canonical_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    active_legacy_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    legacy_place_ids: list[str] = Field(min_length=1)
    city_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    primary_type: EntityType
    secondary_types: list[EntityType] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    identity_hash: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("secondary_types")
    @classmethod
    def normalize_secondary_types(
        cls, values: list[EntityType]
    ) -> list[EntityType]:
        return _sorted_types(values)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return _normalized_tags(values)

    @model_validator(mode="after")
    def validate_identity(self) -> CanonicalPlaceIdentity:
        expected_ids = [self.active_legacy_place_id] + sorted(
            set(self.legacy_place_ids) - {self.active_legacy_place_id}
        )
        if self.legacy_place_ids != expected_ids:
            raise ValueError(
                "legacy_place_ids must be unique with the active ID first"
            )
        if self.primary_type in self.secondary_types:
            raise ValueError("primary_type must not be repeated in secondary_types")
        expected_hash = stable_sha256(
            canonical_identity_payload(
                canonical_place_id=self.canonical_place_id,
                active_legacy_place_id=self.active_legacy_place_id,
                legacy_place_ids=self.legacy_place_ids,
                city_id=self.city_id,
                primary_type=self.primary_type,
                secondary_types=self.secondary_types,
                tags=self.tags,
            )
        )
        if self.identity_hash != expected_hash:
            raise ValueError("identity_hash does not match canonical identity content")
        return self

    @property
    def place_types(self) -> list[EntityType]:
        """Compatibility projection matching ``PlaceRecord.place_types``."""

        return [self.primary_type, *self.secondary_types]


class RetiredPlaceId(NexTripModel):
    """Permanent tombstone for a duplicate legacy ID."""

    retirement_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    retired_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    canonical_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    city_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    entity_type: EntityType
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_retirement(self) -> RetiredPlaceId:
        if self.retired_place_id == self.canonical_place_id:
            raise ValueError("a canonical place ID cannot retire into itself")
        expected = stable_identifier("retirement", self.retired_place_id)
        if self.retirement_id != expected:
            raise ValueError("retirement_id is not deterministic for retired_place_id")
        return self


class VacancyStatus(StrEnum):
    VACANT = "vacant"
    FILLED = "filled"


class EntityCityVacancy(NexTripModel):
    """Historical quota slot left behind by one retired duplicate ID."""

    vacancy_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    retired_place_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    city_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    entity_type: EntityType
    status: VacancyStatus = VacancyStatus.VACANT
    replacement_place_id: str | None = Field(
        default=None,
        min_length=1,
        pattern=_IDENTIFIER_PATTERN,
    )

    @model_validator(mode="after")
    def validate_vacancy_id(self) -> EntityCityVacancy:
        expected = stable_identifier(
            "vacancy",
            self.city_id,
            self.entity_type.value,
            self.retired_place_id,
        )
        if self.vacancy_id != expected:
            raise ValueError("vacancy_id is not deterministic for its source slot")
        if self.status is VacancyStatus.VACANT and self.replacement_place_id is not None:
            raise ValueError("a vacant slot cannot have replacement_place_id")
        if self.status is VacancyStatus.FILLED and self.replacement_place_id is None:
            raise ValueError("a filled slot requires replacement_place_id")
        if self.replacement_place_id == self.retired_place_id:
            raise ValueError("replacement_place_id cannot reuse retired_place_id")
        return self


class EntityCityQuota(NexTripModel):
    """Invariant: target slots equal active places plus open vacancies."""

    city_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    entity_type: EntityType
    target_count: int = Field(ge=0)
    active_count: int = Field(ge=0)
    vacancy_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_quota(self) -> EntityCityQuota:
        if self.target_count != self.active_count + self.vacancy_count:
            raise ValueError("target_count must equal active_count + vacancy_count")
        return self


def canonical_manifest_payload(
    *,
    schema_version: str,
    identities: list[CanonicalPlaceIdentity],
    retired_place_ids: list[RetiredPlaceId],
    vacancies: list[EntityCityVacancy],
    quotas: list[EntityCityQuota],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "identities": [item.model_dump(mode="json") for item in identities],
        "retired_place_ids": [
            item.model_dump(mode="json") for item in retired_place_ids
        ],
        # ``exclude_none`` keeps hashes compatible with manifests produced
        # before replacement_place_id was introduced. Filled assignments are
        # still part of the immutable content and therefore change the hash.
        "vacancies": [
            item.model_dump(mode="json", exclude_none=True) for item in vacancies
        ],
        "quotas": [item.model_dump(mode="json") for item in quotas],
    }


class CanonicalIdentityManifest(NexTripModel):
    """Deterministic identity snapshot; ``generated_at`` is audit metadata only."""

    schema_version: str = "1.0.0"
    manifest_id: str = Field(min_length=1, pattern=_IDENTIFIER_PATTERN)
    manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    generated_at: AwareDatetime
    identities: list[CanonicalPlaceIdentity]
    retired_place_ids: list[RetiredPlaceId] = Field(default_factory=list)
    vacancies: list[EntityCityVacancy] = Field(default_factory=list)
    quotas: list[EntityCityQuota] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_manifest(self) -> CanonicalIdentityManifest:
        canonical_ids = [item.canonical_place_id for item in self.identities]
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("canonical_place_id values must be unique")

        owners: dict[str, str] = {}
        identities_by_id = {
            item.canonical_place_id: item for item in self.identities
        }
        for identity in self.identities:
            for legacy_id in identity.legacy_place_ids:
                if legacy_id in owners:
                    raise ValueError(f"legacy place ID has multiple owners: {legacy_id}")
                owners[legacy_id] = identity.canonical_place_id

        retired_ids = [item.retired_place_id for item in self.retired_place_ids]
        if len(retired_ids) != len(set(retired_ids)):
            raise ValueError("retired_place_id values must be unique")
        active_legacy_ids = {
            item.active_legacy_place_id for item in self.identities
        }
        if (set(canonical_ids) | active_legacy_ids) & set(retired_ids):
            raise ValueError("a retired place ID can never be reused as active")

        expected_retired = {
            legacy_id
            for identity in self.identities
            for legacy_id in identity.legacy_place_ids
            if legacy_id != identity.active_legacy_place_id
        }
        if set(retired_ids) != expected_retired:
            raise ValueError(
                "retired_place_ids must exactly cover inactive legacy aliases"
            )
        for retirement in self.retired_place_ids:
            owner = owners.get(retirement.retired_place_id)
            if owner != retirement.canonical_place_id:
                raise ValueError("retired place ID must point to its canonical owner")
            identity = identities_by_id.get(retirement.canonical_place_id)
            if identity is None or identity.city_id != retirement.city_id:
                raise ValueError("retirement city must match its canonical identity")
            if retirement.entity_type not in identity.place_types:
                raise ValueError(
                    "retired entity type must remain on its canonical identity"
                )

        vacancy_ids = [item.vacancy_id for item in self.vacancies]
        if len(vacancy_ids) != len(set(vacancy_ids)):
            raise ValueError("vacancy_id values must be unique")
        vacancy_retired_ids = [item.retired_place_id for item in self.vacancies]
        if len(vacancy_retired_ids) != len(set(vacancy_retired_ids)):
            raise ValueError("each retired place ID can have only one vacancy")
        vacancy_by_retired = {item.retired_place_id: item for item in self.vacancies}
        if set(vacancy_by_retired) != set(retired_ids):
            raise ValueError("every retired place ID must retain exactly one vacancy")
        retirement_by_id = {
            item.retired_place_id: item for item in self.retired_place_ids
        }
        for retired_id, vacancy in vacancy_by_retired.items():
            retirement = retirement_by_id[retired_id]
            if (vacancy.city_id, vacancy.entity_type) != (
                retirement.city_id,
                retirement.entity_type,
            ):
                raise ValueError("vacancy must retain the retired entity/city slot")

        filled_vacancies = [
            vacancy
            for vacancy in self.vacancies
            if vacancy.status is VacancyStatus.FILLED
        ]
        replacement_ids = [
            vacancy.replacement_place_id for vacancy in filled_vacancies
        ]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise ValueError("one replacement place can fill only one vacancy")
        for vacancy in filled_vacancies:
            replacement_id = vacancy.replacement_place_id
            assert replacement_id is not None
            if replacement_id in retired_ids:
                raise ValueError("a retired place ID cannot be used as a replacement")
            replacement = identities_by_id.get(replacement_id)
            if replacement is None:
                raise ValueError("vacancy replacement must be an active identity")
            if replacement.legacy_place_ids != [replacement_id]:
                raise ValueError("vacancy replacement must be a distinct singleton")
            if (replacement.city_id, replacement.primary_type) != (
                vacancy.city_id,
                vacancy.entity_type,
            ):
                raise ValueError(
                    "vacancy replacement must preserve its entity/city slot"
                )

        quota_keys = [(item.city_id, item.entity_type) for item in self.quotas]
        if len(quota_keys) != len(set(quota_keys)):
            raise ValueError("entity/city quota rows must be unique")
        active_counts = Counter(
            (identity.city_id, identity.primary_type) for identity in self.identities
        )
        vacancy_counts = Counter(
            (vacancy.city_id, vacancy.entity_type)
            for vacancy in self.vacancies
            if vacancy.status is VacancyStatus.VACANT
        )
        expected_keys = set(active_counts) | set(vacancy_counts)
        if set(quota_keys) != expected_keys:
            raise ValueError("quotas must cover every active place and vacancy")
        for quota in self.quotas:
            key = (quota.city_id, quota.entity_type)
            if quota.active_count != active_counts[key]:
                raise ValueError("quota active_count does not match identities")
            if quota.vacancy_count != vacancy_counts[key]:
                raise ValueError("quota vacancy_count does not match vacancies")

        expected_hash = stable_sha256(
            canonical_manifest_payload(
                schema_version=self.schema_version,
                identities=self.identities,
                retired_place_ids=self.retired_place_ids,
                vacancies=self.vacancies,
                quotas=self.quotas,
            )
        )
        if self.manifest_hash != expected_hash:
            raise ValueError("manifest_hash does not match manifest content")
        if self.manifest_id != f"canonical_identity_{expected_hash[:20]}":
            raise ValueError("manifest_id does not match manifest_hash")
        return self
