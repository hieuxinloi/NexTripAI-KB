from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from nextrip_pipeline.crawl.adapters import (
    AgodaPriceAdapter,
    AgodaPriceRequest,
    GooglePlacesOpeningAdapter,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    Occupancy,
)


NOW = datetime(2026, 8, 18, 5, tzinfo=timezone.utc)


def _mapping(
    *,
    source_id: str,
    external_id: str,
    entity_type: EntityType,
) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"{source_id}-{external_id}",
        entity_id=f"internal-{external_id}",
        entity_type=entity_type,
        source_id=source_id,
        external_id=external_id,
        status=MappingStatus.CONFIRMED,
        confidence=1,
        matched_at=NOW,
        verified_at=NOW,
    )


def test_agoda_adapter_builds_search_request_and_source_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "site-1:secret-key"
        request_payload = __import__("json").loads(request.content)
        assert request_payload["criteria"]["propertyIds"] == [12157]
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={"searchId": 123, "properties": [{"propertyId": 12157}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = AgodaPriceAdapter(
        endpoint="https://sandbox.agoda.example/search",
        site_id="site-1",
        api_key="secret-key",
        client=client,
        clock=lambda: NOW,
    )

    record = adapter.fetch(
        [
            _mapping(
                source_id="agoda-demand-api",
                external_id="12157",
                entity_type=EntityType.HOTEL,
            )
        ],
        AgodaPriceRequest(
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            occupancy=Occupancy(adults=2, rooms=1),
        ),
        run_id="hotel-price-run",
    )

    assert record.http_status == 200
    assert record.raw_payload["response"]["searchId"] == 123
    assert "secret-key" not in str(record.raw_payload)


def test_google_adapter_uses_minimal_opening_field_mask() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Goog-Api-Key"] == "google-secret"
        assert request.headers["X-Goog-FieldMask"] == (
            "id,businessStatus,currentOpeningHours,timeZone"
        )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "ChIJ-place",
                "businessStatus": "OPERATIONAL",
                "currentOpeningHours": {"openNow": True},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = GooglePlacesOpeningAdapter(
        api_key="google-secret",
        client=client,
        clock=lambda: NOW,
    )
    mapping = _mapping(
        source_id="google-places-api",
        external_id="ChIJ-place",
        entity_type=EntityType.RESTAURANT,
    )

    record = adapter.fetch(mapping, run_id="opening-run")

    assert record.entity_type is EntityType.RESTAURANT
    assert record.raw_payload["response"]["businessStatus"] == "OPERATIONAL"
    assert "google-secret" not in str(record.raw_payload)
