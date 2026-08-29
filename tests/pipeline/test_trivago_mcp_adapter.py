from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from nextrip_pipeline.crawl.adapters import (
    TrivagoMcpPriceAdapter,
    TrivagoPriceRequest,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    Occupancy,
    RecordSubjectType,
)


NOW = datetime(2026, 8, 18, 9, tzinfo=timezone.utc)


def test_trivago_mcp_adapter_captures_structured_live_price() -> None:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        calls.append(payload)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": "2025-03-26"},
                },
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
                                "accommodation_id": "292003c34d4f",
                                "accommodation_name": "Fleur De Lys Hotel Quy Nhon",
                                "currency": "VND",
                                "price_per_night": "1.582.500 đ",
                                "price_per_stay": "1.582.500 đ",
                                "advertisers": "Booking.com",
                            }
                        ]
                    }
                },
            },
        )

    adapter = TrivagoMcpPriceAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    mapping = ExternalEntityMapping(
        mapping_id="mapping-1",
        entity_id="hotel-1",
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id="hotel-1",
        status=MappingStatus.CONFIRMED,
        matched_at=NOW,
        verified_at=NOW,
    )
    record = adapter.fetch(
        mapping,
        TrivagoPriceRequest(
            hotel_name="Fleur De Lys Hotel Quy Nhon",
            destination="Quy Nhơn",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
            occupancy=Occupancy(adults=2, rooms=1),
        ),
        run_id="price-run",
    )

    assert record.subject_type is RecordSubjectType.HOTEL_PRICE
    assert record.source_id == "trivago-mcp"
    accommodations = record.raw_payload["response"]["result"]["structuredContent"][
        "accommodations"
    ]
    assert accommodations[0]["price_per_night"] == "1.582.500 đ"
    tool_call = calls[-1]
    assert tool_call["params"]["name"] == "trivago-accommodation-search"
    assert tool_call["params"]["arguments"]["currency"] == "VND"


def test_trivago_mcp_adapter_accepts_streamable_http_sse() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "session-sse"},
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        event_body = (
            ": keepalive\n"
            "event: message\n"
            'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            "id: response-2\n"
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":2,"result":{"structuredContent":'
            '{"accommodations":[]}}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            text=event_body,
        )

    adapter = TrivagoMcpPriceAdapter(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    mapping = ExternalEntityMapping(
        mapping_id="mapping-sse",
        entity_id="hotel-sse",
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id="known-id",
        status=MappingStatus.CONFIRMED,
        matched_at=NOW,
        verified_at=NOW,
    )

    record = adapter.fetch(
        mapping,
        TrivagoPriceRequest(
            hotel_name="SSE Hotel",
            destination="Đà Nẵng",
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
        ),
        run_id="sse-run",
    )

    assert record.raw_payload["response"]["result"]["structuredContent"] == {
        "accommodations": []
    }
