from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import httpx

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import ExternalEntityMapping, SourceRecord

from .common import response_content_type, response_payload


class GooglePlacesOpeningAdapter:
    """Captures opening fields from Google Places API (New)."""

    FIELD_MASK = "id,businessStatus,currentOpeningHours,timeZone"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://places.googleapis.com/v1",
        source_id: str = "google-places-api",
        parser_version: str = "1.0.0",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.source_id = source_id
        self.parser_version = parser_version
        self.client = client or httpx.Client(timeout=30)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        mapping: ExternalEntityMapping,
        *,
        run_id: str,
    ) -> SourceRecord:
        if mapping.source_id != self.source_id:
            raise ValueError(f"mapping {mapping.mapping_id} belongs to another source")

        url = f"{self.base_url}/places/{quote(mapping.external_id, safe='')}"
        response = self.client.get(
            url,
            headers={
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": self.FIELD_MASK,
                "Accept": "application/json",
            },
        )
        crawled_at = self.clock()
        raw_payload = {
            "request": {
                "place_id": mapping.external_id,
                "field_mask": self.FIELD_MASK,
            },
            "response": response_payload(response),
        }
        return SourceRecord(
            source_record_id=f"google-places-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=mapping.entity_type,
            crawled_at=crawled_at,
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=url,
            http_status=response.status_code,
            content_type=response_content_type(response),
        )
