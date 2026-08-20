from __future__ import annotations

from pydantic import Field

from nextrip_pipeline.schemas import NexTripModel


class ScheduledJobDefinition(NexTripModel):
    job_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    interval_minutes: int = Field(ge=1)
    enabled: bool = False
    max_requests_per_run: int | None = Field(default=None, ge=1)


HOTEL_PRICE_SCHEDULE = ScheduledJobDefinition(
    job_id="hotel-price-capture",
    source_id="trivago-mcp",
    interval_minutes=300,
)

OPENING_STATUS_SCHEDULE = ScheduledJobDefinition(
    job_id="opening-status-capture",
    source_id="google-maps-web",
    interval_minutes=1440,
    max_requests_per_run=32,
)

GOOGLE_MAPS_DETAILS_SCHEDULE = ScheduledJobDefinition(
    job_id="google-maps-details-refresh",
    source_id="google-maps-web",
    interval_minutes=10080,
    max_requests_per_run=32,
)

PLACE_MENU_SCHEDULE = ScheduledJobDefinition(
    job_id="place-menu-refresh",
    source_id="google-maps-web",
    interval_minutes=20160,
    max_requests_per_run=16,
)

PLACE_MEDIA_SCHEDULE = ScheduledJobDefinition(
    job_id="place-media-refresh",
    source_id="google-maps-web",
    interval_minutes=43200,
    max_requests_per_run=32,
)
