from __future__ import annotations

from typing import Any

import httpx
from pydantic import JsonValue


def response_payload(response: httpx.Response) -> JsonValue:
    try:
        return response.json()
    except ValueError:
        return {"unparsed_body": response.text}


def response_content_type(response: httpx.Response) -> str | None:
    content_type = response.headers.get("content-type")
    return content_type.split(";", 1)[0].strip() if content_type else None


def json_object(value: Any) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    return {"value": value}
