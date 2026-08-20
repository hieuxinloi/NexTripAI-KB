from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx

from nextrip_pipeline.crawl.adapters import (
    TrivagoMcpDiscoveryAdapter,
    TrivagoPriceRequest,
    TrivagoSearchStrategy,
)
from nextrip_pipeline.crawl.trivago_registry import TrivagoHotelRegistryEntry


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def test_discovery_prefers_name_search_and_reuses_mcp_session() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "discovery-session"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "structuredContent": {
                        "accommodations": [
                            {
                                "accommodation_id": "returned-real-id",
                                "accommodation_name": "Hotel One",
                            }
                        ]
                    }
                },
            },
        )

    target = TrivagoHotelRegistryEntry(
        entity_id="hotel-1",
        master_name="Hotel One",
        city="Đà Nẵng",
        address="1 Bạch Đằng",
        latitude=16.0544,
        longitude=108.2022,
        search_query="Hotel One, 1 Bạch Đằng, Đà Nẵng, Việt Nam",
    )
    adapter = TrivagoMcpDiscoveryAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )

    record = adapter.search(
        target,
        TrivagoPriceRequest(
            hotel_name="Hotel One",
            destination="Đà Nẵng",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
        ),
        run_id="discovery-run",
    )

    second_record = adapter.search(
        target,
        TrivagoPriceRequest(
            hotel_name="Hotel One",
            destination="Đà Nẵng",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
        ),
        run_id="discovery-run-2",
    )

    tool_calls = [call for call in calls if call["method"] == "tools/call"]
    assert len([call for call in calls if call["method"] == "initialize"]) == 1
    assert len(tool_calls) == 2
    arguments = tool_calls[0]["params"]["arguments"]
    assert tool_calls[0]["params"]["name"] == "trivago-accommodation-search"
    assert arguments["query"] == "Hotel One, Đà Nẵng, Việt Nam"
    assert "latitude" not in arguments
    assert "longitude" not in arguments
    assert record.raw_payload["request"]["tool"] == "trivago-accommodation-search"
    assert record.raw_payload["request"]["search_strategy"] == "name"
    assert record.raw_payload["request"]["known_external_id"] is None
    assert record.subject_id == "hotel-1"
    assert second_record.run_id == "discovery-run-2"


def test_radius_search_requires_explicit_strategy() -> None:
    target = TrivagoHotelRegistryEntry(
        entity_id="hotel-1",
        master_name="Hotel One",
        city="ÄÃ  Náºµng",
        latitude=16.0544,
        longitude=108.2022,
        search_query="Hotel One, ÄÃ  Náºµng, Viá»‡t Nam",
    )
    request = TrivagoPriceRequest(
        hotel_name=target.master_name,
        destination=target.city,
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
    )

    tool_name, arguments = TrivagoMcpDiscoveryAdapter._tool_call(
        target,
        request,
        strategy=TrivagoSearchStrategy.RADIUS,
    )

    assert tool_name == "trivago-accommodation-radius-search"
    assert arguments["latitude"] == 16.0544
    assert arguments["longitude"] == 108.2022
    assert "query" not in arguments


def test_discovery_falls_back_to_text_search_only_without_coordinates() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "text-session"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"structuredContent": {"error": "No accommodations found"}},
            },
        )

    target = TrivagoHotelRegistryEntry(
        entity_id="hotel-without-coordinates",
        master_name="Hotel Without Coordinates",
        city="Đà Nẵng",
        search_query="Hotel Without Coordinates, Đà Nẵng, Việt Nam",
    )
    adapter = TrivagoMcpDiscoveryAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )

    record = adapter.search(
        target,
        TrivagoPriceRequest(
            hotel_name=target.master_name,
            destination=target.city,
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
        ),
        run_id="text-fallback",
    )

    tool_call = calls[-1]
    assert tool_call["params"]["name"] == "trivago-accommodation-search"
    assert tool_call["params"]["arguments"]["query"] == target.search_query
    assert "latitude" not in tool_call["params"]["arguments"]
    assert record.raw_payload["request"]["tool"] == ("trivago-accommodation-search")
