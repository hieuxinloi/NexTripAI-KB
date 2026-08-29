from __future__ import annotations

import json

import httpx


class McpResponseDecodeError(ValueError):
    """Raised when a Streamable HTTP response has no JSON-RPC message."""


def parse_mcp_response(response: httpx.Response) -> dict[str, object]:
    """Decode either JSON or the SSE form used by MCP Streamable HTTP.

    An SSE response can contain protocol notifications before the response to
    the request.  The last JSON object carrying ``result`` or ``error`` is the
    authoritative JSON-RPC response.  Other well-formed JSON objects are kept
    only as a fallback for servers that omit those members.
    """

    content_type = response.headers.get("content-type", "").casefold()
    body = response.text
    looks_like_sse = body.lstrip().startswith(("data:", "event:", ":"))
    if "text/event-stream" not in content_type and not looks_like_sse:
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as error:
            raise McpResponseDecodeError("MCP response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise McpResponseDecodeError("MCP response must be a JSON object")
        return payload

    messages: list[dict[str, object]] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if not data_lines:
            return
        value = "\n".join(data_lines).strip()
        data_lines.clear()
        if not value or value == "[DONE]":
            return
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise McpResponseDecodeError(
                "MCP event-stream contains invalid JSON data"
            ) from error
        if not isinstance(payload, dict):
            raise McpResponseDecodeError("MCP SSE data must be a JSON object")
        messages.append(payload)

    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            flush_event()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and field == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush_event()

    if not messages:
        raise McpResponseDecodeError("MCP event-stream contains no JSON data")
    for payload in reversed(messages):
        if "result" in payload or "error" in payload:
            return payload
    return messages[-1]
