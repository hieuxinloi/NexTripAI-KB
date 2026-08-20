from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from nextrip_pipeline.crawl import RawJsonWriter
from nextrip_pipeline.crawl.adapters import (
    AgodaPriceAdapter,
    AgodaPriceRequest,
    GooglePlacesOpeningAdapter,
)
from nextrip_pipeline.jobs import HotelPriceJob, OpeningStatusJob
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    Occupancy,
)


NOW = datetime(2026, 8, 18, 5, tzinfo=timezone.utc)


def _mapping(
    index: int, source_id: str, entity_type: EntityType
) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"mapping-{index}",
        entity_id=f"entity-{index}",
        entity_type=entity_type,
        source_id=source_id,
        external_id=str(10000 + index),
        status=MappingStatus.CONFIRMED,
        matched_at=NOW,
        verified_at=NOW,
    )


def test_hotel_job_batches_agoda_limit_and_writes_raw(tmp_path) -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"searchId": call_count, "properties": []})

    adapter = AgodaPriceAdapter(
        endpoint="https://sandbox.agoda.example/search",
        site_id="site",
        api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    job = HotelPriceJob(adapter, RawJsonWriter(tmp_path), clock=lambda: NOW)
    mappings = [
        _mapping(index, "agoda-demand-api", EntityType.HOTEL) for index in range(101)
    ]

    result = job.run(
        mappings,
        AgodaPriceRequest(
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            occupancy=Occupancy(adults=2, rooms=1),
        ),
        run_id="hotel-run",
    )

    assert result.succeeded
    assert call_count == 2
    assert len(result.written_paths) == 2


def test_opening_job_enforces_free_tier_daily_cap(tmp_path) -> None:
    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"businessStatus": "OPERATIONAL"})

    adapter = GooglePlacesOpeningAdapter(
        api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    job = OpeningStatusJob(adapter, RawJsonWriter(tmp_path), clock=lambda: NOW)
    mappings = [
        _mapping(index, "google-places-api", EntityType.CAFE) for index in range(40)
    ]

    result = job.run(mappings, run_id="opening-run")

    assert result.succeeded
    assert call_count == 32
    assert len(result.written_paths) == 32
