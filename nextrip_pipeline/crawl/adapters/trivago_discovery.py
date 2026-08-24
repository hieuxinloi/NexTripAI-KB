from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

import httpx

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.crawl.trivago_registry import TrivagoHotelRegistryEntry
from nextrip_pipeline.schemas import EntityType, RecordSubjectType, SourceRecord

from .common import json_object
from .mcp_http import parse_mcp_response
from .trivago import TrivagoPriceRequest
from .trivago_mcp import TrivagoMcpError


class TrivagoSearchStrategy(StrEnum):
    """Identity-oriented lookup strategy for the Trivago MCP."""

    NAME = "name"
    RADIUS = "radius"


class TrivagoMcpDiscoveryAdapter:
    """Search Trivago without inventing an accommodation identifier."""

    protocol_version = "2025-03-26"
    radius_tool_name = "trivago-accommodation-radius-search"
    text_tool_name = "trivago-accommodation-search"
    tool_name = text_tool_name

    def __init__(
        self,
        *,
        endpoint: str = "https://mcp.trivago.com/mcp",
        source_id: str = "trivago-mcp",
        parser_version: str = "1.2.0",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.source_id = source_id
        self.parser_version = parser_version
        self.client = client or httpx.Client(timeout=60)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._session_id: str | None = None

    def search(
        self,
        target: TrivagoHotelRegistryEntry,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
        strategy: TrivagoSearchStrategy = TrivagoSearchStrategy.NAME,
        query: str | None = None,
    ) -> SourceRecord:
        if (
            request.hotel_name != target.search_name
            or request.destination != target.city
        ):
            raise ValueError("price request identity must match the registry target")
        if strategy is TrivagoSearchStrategy.RADIUS and query is not None:
            raise ValueError("radius search cannot accept a text query")
        if (
            strategy is TrivagoSearchStrategy.NAME
            and query is not None
            and query not in target.identity_search_queries
        ):
            raise ValueError("text query must be pinned in the registry target")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        session_headers = self._session_headers(headers)

        tool_name, arguments = self._tool_call(
            target,
            request,
            strategy=strategy,
            query=query,
        )
        search_response = self.client.post(
            self.endpoint,
            headers=session_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        search_response.raise_for_status()
        response_payload = parse_mcp_response(search_response)
        if response_payload.get("error"):
            raise TrivagoMcpError(f"Trivago MCP error: {response_payload['error']}")

        raw_payload = {
            "request": {
                "tool": tool_name,
                "arguments": arguments,
                "entity_id": target.entity_id,
                "registry_status": target.status.value,
                "known_external_id": target.external_id,
                "search_strategy": strategy.value,
                "registry_search_query": arguments.get("query"),
                "review_target_hash": target.review_target_hash,
            },
            "response": json_object(response_payload),
        }
        return SourceRecord(
            source_record_id=f"trivago-mcp-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=EntityType.HOTEL,
            subject_type=RecordSubjectType.HOTEL_PRICE,
            subject_id=target.entity_id,
            crawled_at=self.clock(),
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=self.endpoint,
            http_status=search_response.status_code,
            content_type=search_response.headers.get("content-type"),
        )

    def _session_headers(self, headers: dict[str, str]) -> dict[str, str]:
        if self._session_id is not None:
            return {**headers, "mcp-session-id": self._session_id}

        initialize_response = self.client.post(
            self.endpoint,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "nextrip-pipeline",
                        "version": self.parser_version,
                    },
                },
            },
        )
        initialize_response.raise_for_status()
        session_id = initialize_response.headers.get("mcp-session-id")
        if not session_id:
            raise TrivagoMcpError("Trivago MCP did not return a session id")

        session_headers = {**headers, "mcp-session-id": session_id}
        self.client.post(
            self.endpoint,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ).raise_for_status()
        self._session_id = session_id
        return session_headers

    @classmethod
    def _tool_call(
        cls,
        target: TrivagoHotelRegistryEntry,
        request: TrivagoPriceRequest,
        *,
        strategy: TrivagoSearchStrategy = TrivagoSearchStrategy.NAME,
        query: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        arguments: dict[str, Any] = {
            "arrival": request.check_in.isoformat(),
            "departure": request.check_out.isoformat(),
            "adults": request.occupancy.adults,
            "children": request.occupancy.children,
            "rooms": request.occupancy.rooms,
            "country": "VN",
            "currency": request.currency,
            "language": "VI_VN",
        }
        if request.occupancy.children:
            arguments["children_ages"] = "-".join(
                str(age) for age in request.children_ages
            )
        if (
            strategy is TrivagoSearchStrategy.RADIUS
            and target.latitude is not None
            and target.longitude is not None
        ):
            arguments["latitude"] = target.latitude
            arguments["longitude"] = target.longitude
            return cls.radius_tool_name, arguments
        # Name search is deliberately the default even when verified master
        # coordinates exist. Radius search returns nearby bookable properties
        # and cannot by itself establish which listing belongs to the master
        # hotel. Coordinates remain resolver evidence or an explicit fallback.
        arguments["query"] = query or target.search_query
        return cls.text_tool_name, arguments
