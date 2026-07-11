from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchRequest:
    query: str
    limit: int
    city_id: str | None = None
    entity_types: list[str] | None = None


@dataclass
class SearchResponse:
    strategy: str
    results: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
