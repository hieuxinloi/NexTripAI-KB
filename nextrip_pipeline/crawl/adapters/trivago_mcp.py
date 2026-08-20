from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    RecordSubjectType,
    SourceRecord,
)

from .common import json_object
from .mcp_http import parse_mcp_response
from .trivago import TrivagoPriceRequest


class TrivagoMcpError(RuntimeError):
    """Raised when the official Trivago MCP cannot complete a search."""


class TrivagoMcpPriceAdapter:
    """Captures live Trivago prices through its official remote MCP server."""

    protocol_version = "2025-03-26"
    tool_name = "trivago-accommodation-search"

    def __init__(
        self,
        *,
        endpoint: str = "https://mcp.trivago.com/mcp",
        source_id: str = "trivago-mcp",
        parser_version: str = "1.0.0",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.source_id = source_id
        self.parser_version = parser_version
        self.client = client or httpx.Client(timeout=60)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self,
        mapping: ExternalEntityMapping,
        request: TrivagoPriceRequest,
        *,
        run_id: str,
    ) -> SourceRecord:
        if mapping.entity_type is not EntityType.HOTEL:
            raise ValueError("Trivago MCP price mapping must belong to a hotel")
        if mapping.source_id != self.source_id:
            raise ValueError(f"mapping {mapping.mapping_id} belongs to another source")

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
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

        arguments = self._tool_arguments(request)
        search_response = self.client.post(
            self.endpoint,
            headers=session_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": self.tool_name,
                    "arguments": arguments,
                },
            },
        )
        search_response.raise_for_status()
        response_payload = parse_mcp_response(search_response)
        self._validate_response(response_payload)

        raw_payload = {
            "request": {
                "tool": self.tool_name,
                "arguments": arguments,
                "entity_id": mapping.entity_id,
            },
            "response": json_object(response_payload),
        }
        return SourceRecord(
            source_record_id=f"trivago-mcp-{uuid4().hex}",
            run_id=run_id,
            source_id=self.source_id,
            entity_type=EntityType.HOTEL,
            subject_type=RecordSubjectType.HOTEL_PRICE,
            subject_id=mapping.entity_id,
            crawled_at=self.clock(),
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version=self.parser_version,
            source_url=self.endpoint,
            http_status=search_response.status_code,
            content_type=search_response.headers.get("content-type"),
        )

    def _tool_arguments(self, request: TrivagoPriceRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "query": f"{request.hotel_name}, {request.destination}, Vietnam",
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
        return arguments

    def _validate_response(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise TrivagoMcpError("Trivago MCP returned a non-object response")
        if payload.get("error"):
            raise TrivagoMcpError(f"Trivago MCP error: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TrivagoMcpError("Trivago MCP response has no result")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise TrivagoMcpError("Trivago MCP response has no structured content")
        accommodations = structured.get("accommodations")
        if not isinstance(accommodations, list):
            raise TrivagoMcpError("Trivago MCP response has no accommodations list")
