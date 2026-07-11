from __future__ import annotations

from typing import Protocol

from ..gemini_client import GeminiClient
from ..neo4j_store import Neo4jGraphStore
from .models import SearchRequest, SearchResponse


class RetrievalStrategy(Protocol):
    name: str

    def search(
        self,
        request: SearchRequest,
        store: Neo4jGraphStore,
        embedder: GeminiClient,
    ) -> SearchResponse:
        ...


def trace_error(step: str, exc: Exception) -> dict[str, str]:
    return {
        "step": step,
        "status": "error",
        "error_type": exc.__class__.__name__,
        "message": str(exc),
    }
