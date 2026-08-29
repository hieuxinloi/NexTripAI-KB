from __future__ import annotations

import json

from bs4 import BeautifulSoup
from pydantic import JsonValue


def extract_json_ld(html: str) -> list[JsonValue]:
    soup = BeautifulSoup(html, "html.parser")
    values: list[JsonValue] = []
    for script in soup.select('script[type="application/ld+json"]'):
        content = script.string or script.get_text(strip=True)
        if not content:
            continue
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            values.extend(parsed)
        else:
            values.append(parsed)
    return values
