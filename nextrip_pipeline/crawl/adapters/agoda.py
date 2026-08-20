from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from uuid import uuid4

import httpx
from pydantic import Field, model_validator

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    NexTripModel,
    Occupancy,
    SourceRecord,
)

from .common import json_object, response_content_type, response_payload


class AgodaPriceRequest(NexTripModel):
    check_in: date
    check_out: date
    occupancy: Occupancy = Field(default_factory=Occupancy)
    children_ages: list[int] = Field(default_factory=list)
    language: str = Field(default="vi-vn", min_length=2)
    currency: str = Field(default="VND", pattern=r"^[A-Z]{3}$")
    user_country: str = Field(default="VN", pattern=r"^[A-Z]{2}$")
    rates_per_property: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def validate_stay_and_children(self) -> AgodaPriceRequest:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if len(self.children_ages) != self.occupancy.children:
            raise ValueError("children_ages must match occupancy.children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        return self


class AgodaPriceAdapter:
    """Captures Agoda Search API responses as SourceRecord evidence."""

    MAX_PROPERTIES_PER_REQUEST = 100

    def __init__(
        self,
        *,
        endpoint: str,
        site_id: str,
        api_key: str,
        source_id: str = "agoda-demand-api",
        parser_version: str = "1.0.0",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.site_id = site_id
        self.api_key = api_key
        self.source_id = source_id
        self.parser_version = parser_version
        self.client = client or httpx.Client(timeout=60)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        mappings: Sequence[ExternalEntityMapping],
        request: AgodaPriceRequest,
        *,
        run_id: str,
    ) -> SourceRecord:
        if not mappings:
            raise ValueError("at least one Agoda hotel mapping is required")
        if len(mappings) > self.MAX_PROPERTIES_PER_REQUEST:
            raise ValueError("Agoda supports at most 100 properties per request")

        property_ids: list[int] = []
        for mapping in mappings:
            if mapping.source_id != self.source_id:
                raise ValueError(
                    f"mapping {mapping.mapping_id} belongs to another source"
                )
            if mapping.entity_type is not EntityType.HOTEL:
                raise ValueError(f"mapping {mapping.mapping_id} is not a hotel")
            try:
                property_ids.append(int(mapping.external_id))
            except ValueError as error:
                raise ValueError(
                    f"Agoda external_id must be numeric: {mapping.external_id!r}"
                ) from error

        criteria: dict[str, object] = {
            "propertyIds": property_ids,
            "checkIn": request.check_in.isoformat(),
            "checkOut": request.check_out.isoformat(),
            "rooms": request.occupancy.rooms,
            "adults": request.occupancy.adults,
            "children": request.occupancy.children,
            "language": request.language,
            "currency": request.currency,
            "userCountry": request.user_country,
        }
        if request.children_ages:
            criteria["childrenAges"] = request.children_ages

        request_body = {
            "waitTime": 60,
            "criteria": criteria,
            "features": {
                "ratesPerProperty": request.rates_per_property,
                "extra": [
                    "content",
                    "cancellationDetail",
                    "rateDetail",
                    "taxDetail",
                ],
            },
        }
        response = self.client.post(
            self.endpoint,
            headers={
                "Authorization": f"{self.site_id}:{self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=request_body,
        )
        crawled_at = self.clock()
        raw_payload = {
            "request": json_object(request_body),
            "response": response_payload(response),
        }
        return SourceRecord(
            source_record_id=f"agoda-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=EntityType.HOTEL,
            crawled_at=crawled_at,
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=self.endpoint,
            http_status=response.status_code,
            content_type=response_content_type(response),
        )
