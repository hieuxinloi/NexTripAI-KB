from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from nextrip_pipeline.crawl.browser import BrowserClient
from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    RecordSubjectType,
    SourceRecord,
)

from .playwright_common import extract_json_ld


class GoogleMapsPlaceAdapter:
    """Captures a public Google Maps place page without Places API credentials."""

    def __init__(
        self,
        browser: BrowserClient,
        *,
        base_url: str = "https://www.google.com/maps/search/",
        source_id: str = "google-maps-web",
        parser_version: str = "1.0.0",
        timeout_seconds: float = 45,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.source_id = source_id
        self.parser_version = parser_version
        self.timeout_seconds = timeout_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        mapping: ExternalEntityMapping,
        *,
        run_id: str,
    ) -> SourceRecord:
        if mapping.source_id != self.source_id:
            raise ValueError(f"mapping {mapping.mapping_id} belongs to another source")
        query = str(mapping.attributes.get("search_query") or mapping.external_id)
        latitude = mapping.attributes.get("master_latitude")
        longitude = mapping.attributes.get("master_longitude")
        centered_place_url = isinstance(latitude, (int, float)) and isinstance(
            longitude, (int, float)
        )
        if mapping.external_url is not None:
            requested_url = str(mapping.external_url)
            centered_place_url = False
        elif centered_place_url:
            requested_url = (
                "https://www.google.com/maps/search/"
                f"{quote(query, safe='')}/@{latitude},{longitude},17z?hl=en"
            )
        else:
            requested_url = f"{self.base_url}/{quote(query, safe='')}?hl=en"
        detail_capture = getattr(self.browser, "capture_google_maps_place", None)
        if callable(detail_capture):
            snapshot = detail_capture(
                requested_url,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            snapshot = self.browser.capture(
                requested_url,
                timeout_seconds=self.timeout_seconds,
            )
        raw_payload = {
            "request": {
                "entity_id": mapping.entity_id,
                "query": query,
                "used_master_coordinates_for_viewport": centered_place_url,
            },
            "page": {
                "requested_url": snapshot.requested_url,
                "final_url": snapshot.final_url,
                "title": snapshot.title,
                "html": snapshot.html,
                "json_ld": extract_json_ld(snapshot.html),
                "structured_data": snapshot.structured_data or {},
                "used_master_coordinates_for_viewport": centered_place_url,
            },
        }
        return SourceRecord(
            source_record_id=f"google-maps-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=mapping.entity_type,
            subject_type=RecordSubjectType.OPENING_STATUS,
            subject_id=mapping.entity_id,
            crawled_at=self.clock(),
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=snapshot.final_url,
            http_status=snapshot.http_status,
            content_type="text/html",
        )
