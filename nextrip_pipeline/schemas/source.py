from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, Field, HttpUrl

from .common import EntityType, NexTripModel


class SourceRecord(NexTripModel):
    """Immutable evidence captured by one crawl or API request."""

    source_record_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    entity_type: EntityType
    crawled_at: AwareDatetime
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    parser_version: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None

