from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import (
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from nextrip_pipeline.schemas import EntityType, NexTripModel


class CrawlMethod(StrEnum):
    HTTP_JSON = "http_json"
    BEAUTIFULSOUP = "beautifulsoup"
    PLAYWRIGHT = "playwright"


class SourceKind(StrEnum):
    OFFICIAL_API = "official_api"
    OFFICIAL_WEBSITE = "official_website"
    PARTNER_API = "partner_api"
    AGGREGATOR = "aggregator"


class StorageMode(StrEnum):
    IMMUTABLE = "immutable"
    EXPIRING = "expiring"
    METADATA_ONLY = "metadata_only"


class CachePolicy(NexTripModel):
    enabled: bool = True
    ttl_minutes: int = Field(default=15, ge=1)
    stale_if_error_minutes: int = Field(default=0, ge=0)


class RetentionPolicy(NexTripModel):
    storage_mode: StorageMode = StorageMode.IMMUTABLE
    raw_retention_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def expiring_storage_requires_retention(self) -> RetentionPolicy:
        if (
            self.storage_mode is StorageMode.EXPIRING
            and self.raw_retention_days is None
        ):
            raise ValueError("expiring storage requires raw_retention_days")
        return self


class FallbackPolicy(NexTripModel):
    priority: int = Field(default=100, ge=0)
    fallback_source_ids: list[str] = Field(default_factory=list)
    fallback_on_empty: bool = True
    fallback_http_statuses: list[int] = Field(
        default_factory=lambda: [408, 429, 500, 502, 503, 504]
    )

    @field_validator("fallback_source_ids")
    @classmethod
    def fallback_ids_must_be_unique(cls, source_ids: list[str]) -> list[str]:
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("fallback_source_ids must not contain duplicates")
        return source_ids

    @field_validator("fallback_http_statuses")
    @classmethod
    def fallback_statuses_must_be_valid(cls, statuses: list[int]) -> list[int]:
        if len(statuses) != len(set(statuses)):
            raise ValueError("fallback_http_statuses must not contain duplicates")
        if any(status < 100 or status > 599 for status in statuses):
            raise ValueError("fallback_http_statuses must contain valid HTTP statuses")
        return statuses


class SourceDefinition(NexTripModel):
    """Configuration for one external crawl source; never contains secrets."""

    source_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1)
    kind: SourceKind
    crawl_method: CrawlMethod
    entity_types: list[EntityType] = Field(min_length=1)
    base_url: HttpUrl | None = None
    base_url_env_name: str | None = Field(default=None, min_length=1)
    parser_version: str = Field(min_length=1)
    enabled: bool = True
    schedule_interval_minutes: int | None = Field(default=None, ge=1)
    requests_per_second: float = Field(default=1.0, gt=0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    respect_robots_txt: bool = True
    secret_env_names: list[str] = Field(default_factory=list)
    cache_policy: CachePolicy = Field(default_factory=CachePolicy)
    retention_policy: RetentionPolicy = Field(default_factory=RetentionPolicy)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)

    @model_validator(mode="after")
    def exactly_one_base_url_source(self) -> SourceDefinition:
        if (self.base_url is None) == (self.base_url_env_name is None):
            raise ValueError("set exactly one of base_url or base_url_env_name")
        if self.source_id in self.fallback_policy.fallback_source_ids:
            raise ValueError("a source cannot fallback to itself")
        return self

    @field_validator("entity_types")
    @classmethod
    def entity_types_must_be_unique(
        cls, entity_types: list[EntityType]
    ) -> list[EntityType]:
        if len(entity_types) != len(set(entity_types)):
            raise ValueError("entity_types must not contain duplicates")
        return entity_types

    @field_validator("secret_env_names")
    @classmethod
    def secret_names_must_be_unique(cls, names: list[str]) -> list[str]:
        if len(names) != len(set(names)):
            raise ValueError("secret_env_names must not contain duplicates")
        return names


class SourceRegistryDocument(NexTripModel):
    version: int = Field(default=1, ge=1)
    sources: list[SourceDefinition]

    @field_validator("sources")
    @classmethod
    def source_ids_must_be_unique(
        cls, sources: list[SourceDefinition]
    ) -> list[SourceDefinition]:
        source_ids = [source.source_id for source in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")

        known_source_ids = set(source_ids)
        unknown_fallbacks = {
            fallback_id
            for source in sources
            for fallback_id in source.fallback_policy.fallback_source_ids
            if fallback_id not in known_source_ids
        }
        if unknown_fallbacks:
            raise ValueError(
                "fallback sources are not registered: "
                + ", ".join(sorted(unknown_fallbacks))
            )
        return sources


class SourceRegistryError(ValueError):
    """Raised when a source registry cannot be read or validated."""


class SourceNotFoundError(KeyError):
    """Raised when a requested source_id is absent from the registry."""


class SourceRegistry:
    def __init__(self, document: SourceRegistryDocument) -> None:
        self.document = document
        self._sources = {source.source_id: source for source in document.sources}

    @classmethod
    def load(cls, path: str | Path) -> SourceRegistry:
        registry_path = Path(path)
        try:
            raw_document = json.loads(registry_path.read_text(encoding="utf-8"))
            document = SourceRegistryDocument.model_validate(raw_document)
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise SourceRegistryError(
                f"Invalid source registry '{registry_path}': {error}"
            ) from error
        return cls(document)

    def get(self, source_id: str) -> SourceDefinition:
        try:
            return self._sources[source_id]
        except KeyError as error:
            raise SourceNotFoundError(source_id) from error

    def enabled_sources(self) -> list[SourceDefinition]:
        return [source for source in self.document.sources if source.enabled]

    def for_entity(
        self, entity_type: EntityType, *, enabled_only: bool = True
    ) -> list[SourceDefinition]:
        return [
            source
            for source in self.document.sources
            if entity_type in source.entity_types
            and (source.enabled or not enabled_only)
        ]
