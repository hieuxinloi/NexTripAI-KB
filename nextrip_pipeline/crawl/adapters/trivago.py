from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from uuid import uuid4

from pydantic import Field, model_validator

from nextrip_pipeline.crawl.browser import BrowserClient
from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    NexTripModel,
    Occupancy,
    RecordSubjectType,
    SourceRecord,
)

from .playwright_common import extract_json_ld


class TrivagoPriceRequest(NexTripModel):
    hotel_name: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def validate_stay(self) -> TrivagoPriceRequest:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        return self


class TrivagoPriceAdapter:
    """Captures a Trivago hotel result page without a partner API."""

    def __init__(
        self,
        browser: BrowserClient,
        *,
        base_url: str = "https://www.trivago.vn/",
        source_id: str = "trivago-web",
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
        request: TrivagoPriceRequest,
        *,
        run_id: str,
    ) -> SourceRecord:
        if mapping.entity_type is not EntityType.HOTEL:
            raise ValueError("Trivago price mapping must belong to a hotel")
        if mapping.source_id != self.source_id:
            raise ValueError(f"mapping {mapping.mapping_id} belongs to another source")

        query = urlencode(
            {
                "search": f"{request.hotel_name}, {request.destination}",
                "checkin": request.check_in.isoformat(),
                "checkout": request.check_out.isoformat(),
                "adults": request.occupancy.adults,
                "rooms": request.occupancy.rooms,
                "currency": request.currency,
            }
        )
        requested_url = f"{self.base_url}/srl?{query}"
        snapshot = self.browser.capture(
            requested_url,
            timeout_seconds=self.timeout_seconds,
        )
        raw_payload = {
            "request": request.model_dump(mode="json"),
            "mapping": {
                "entity_id": mapping.entity_id,
                "external_id": mapping.external_id,
            },
            "page": {
                "requested_url": snapshot.requested_url,
                "final_url": snapshot.final_url,
                "title": snapshot.title,
                "html": snapshot.html,
                "json_ld": extract_json_ld(snapshot.html),
            },
        }
        return SourceRecord(
            source_record_id=f"trivago-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=EntityType.HOTEL,
            subject_type=RecordSubjectType.HOTEL_PRICE,
            subject_id=mapping.entity_id,
            crawled_at=self.clock(),
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=snapshot.final_url,
            http_status=snapshot.http_status,
            content_type="text/html",
        )
