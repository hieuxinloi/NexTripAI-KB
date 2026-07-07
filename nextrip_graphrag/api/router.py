from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..config import Settings
from ..gemini_client import GeminiClient
from ..neo4j_store import Neo4jGraphStore
from ..normalizer import CITY_DEFINITIONS, canonical_city
from ..rag import TravelGraphRAG
from .schemas import (
    GraphContext,
    HealthResponse,
    KbAnswerRequest,
    KbAnswerResponse,
    KbSearchRequest,
    KbSearchResponse,
    KbSearchResult,
    SourceInfo,
)

router = APIRouter()


def _trace_error(step: str, exc: Exception) -> dict[str, str]:
    return {
        "step": step,
        "status": "error",
        "error_type": exc.__class__.__name__,
        "message": str(exc),
    }


def _resolve_city_id(city: str | None) -> str | None:
    if not city:
        return None
    try:
        city_name = canonical_city(city)
    except ValueError:
        return None
    return CITY_DEFINITIONS[city_name]["id"]


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _vector_search(
    request: KbSearchRequest,
    settings: Settings,
    store: Neo4jGraphStore,
) -> list[dict[str, Any]]:
    embedding = GeminiClient(settings).embed_query(request.query)
    return store.vector_search(
        embedding=embedding,
        limit=request.top_k,
        city_id=_resolve_city_id(request.city),
        entity_types=request.entity_types,
    )


def _keyword_search(request: KbSearchRequest, store: Neo4jGraphStore) -> list[dict[str, Any]]:
    return store.keyword_search(
        query_text=request.query,
        limit=request.top_k,
        city_id=_resolve_city_id(request.city),
        entity_types=request.entity_types,
    )


def _search_with_trace(
    request: KbSearchRequest,
    settings: Settings,
    store: Neo4jGraphStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trace: list[dict[str, Any]] = []
    try:
        results = _vector_search(request, settings, store)
        trace.append({"step": "vector_search", "status": "ok", "count": len(results)})
    except Exception as exc:
        results = []
        trace.append(_trace_error("vector_search", exc))

    if results:
        return results, trace

    try:
        results = _keyword_search(request, store)
        trace.append({"step": "keyword_search", "status": "ok", "count": len(results)})
    except Exception as exc:
        results = []
        trace.append(_trace_error("keyword_search", exc))

    return results, trace


def _to_search_result(row: dict[str, Any]) -> KbSearchResult:
    place = row.get("place") or {}
    return KbSearchResult(
        place_id=str(_first_present(place, ("id",)) or ""),
        name=place.get("name"),
        city=place.get("city"),
        entity_type=place.get("entity_type"),
        category=_first_present(place, ("category_name", "category")),
        score=row.get("score"),
        source=SourceInfo(
            name=_first_present(place, ("source_name", "source_source_name")),
            url=place.get("source_url"),
        ),
        graph_context=GraphContext(
            facets=list(row.get("facets") or []),
            nearby=list(row.get("nearby") or []),
        ),
    )


def _neo4j_health(settings: Settings) -> str:
    store = Neo4jGraphStore(settings)
    try:
        store.run("RETURN 1 AS ok")
    finally:
        store.close()
    return "ready"


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = Settings.from_env()
    try:
        neo4j_status = _neo4j_health(settings)
    except Exception as exc:
        neo4j_status = f"not_ready:{exc.__class__.__name__}"
    return HealthResponse(
        neo4j=neo4j_status,
        embedding_model=settings.embedding_model,
    )


@router.post("/api/kb/search", response_model=KbSearchResponse)
def search(request: KbSearchRequest) -> KbSearchResponse:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    try:
        rows, trace = _search_with_trace(request, settings, store)
    finally:
        store.close()
    return KbSearchResponse(
        results=[_to_search_result(row) for row in rows],
        trace=trace,
    )


@router.post("/api/kb/answer", response_model=KbAnswerResponse)
def answer(request: KbAnswerRequest) -> KbAnswerResponse:
    settings = Settings.from_env()
    store = Neo4jGraphStore(settings)
    gemini = GeminiClient(settings)
    rag = TravelGraphRAG(store, gemini)
    try:
        text = rag.answer(
            question=request.query,
            city=request.city,
            entity_types=request.entity_types,
            top_k=request.top_k,
        )
    finally:
        store.close()
    return KbAnswerResponse(
        answer=text,
        trace=[{"step": "kb_answer", "status": "ok"}],
    )
