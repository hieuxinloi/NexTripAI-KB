from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from ..config import Settings
from ..logging import safe_text
from ..normalizer import CITY_DEFINITIONS, canonical_city
from ..rag import TravelGraphRAG
from ..retrieval import SearchRequest, available_strategies, get_strategy
from ..versions.registry import kb_version_manifests
from ..versions.v2.retrieval import V2RetrievalService
from ..versions.v2.schemas import V2QueryResponse
from .schemas import (
    GraphContext,
    HealthResponse,
    KbAnswerRequest,
    KbAnswerResponse,
    KbSearchRequest,
    KbSearchResponse,
    KbSearchResult,
    SourceInfo,
    V2QueryRequest,
)
from .dependencies import KbServices, get_kb_services

router = APIRouter()
SEARCH_STEPS = {
    "vector_search",
    "keyword_search",
    "text_unit_vector_search",
    "text_unit_keyword_search",
    "graph_filter_search",
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


def _run_search(
    request: KbSearchRequest,
    services: KbServices,
):
    try:
        strategy = get_strategy(request.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return strategy.search(
        SearchRequest(
            query=request.query,
            limit=request.top_k,
            city_id=_resolve_city_id(request.city),
            entity_types=request.entity_types,
        ),
        services.store,
        services.gemini,
    )


def _to_search_result(row: dict[str, Any]) -> KbSearchResult:
    place = row.get("place") or {}
    return KbSearchResult(
        place_id=str(place.get("id") or ""),
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
        retrieval=dict(row.get("retrieval") or {}),
        evidence=list(row.get("evidence") or []),
    )


def _all_retrieval_sources_failed(trace: list[dict[str, Any]]) -> bool:
    attempted = [event for event in trace if event.get("step") in SEARCH_STEPS]
    return bool(attempted) and not any(event.get("status") == "ok" for event in attempted)


def _neo4j_health(services: KbServices) -> str:
    services.store.run("RETURN 1 AS ok")
    return "ready"


@router.get("/health", response_model=HealthResponse)
def health(services: KbServices = Depends(get_kb_services)) -> HealthResponse:
    try:
        neo4j_status = _neo4j_health(services)
    except Exception as exc:
        neo4j_status = f"not_ready:{exc.__class__.__name__}"
    try:
        services.v2_store.run("RETURN 1 AS ok")
        neo4j_v2_status = "ready"
    except Exception as exc:
        neo4j_v2_status = f"not_ready:{exc.__class__.__name__}"
    return HealthResponse(
        neo4j=neo4j_status,
        neo4j_v2=neo4j_v2_status,
        embedding_model=services.settings.embedding_model,
        retrieval_strategies=available_strategies(),
    )


@router.get("/api/kb/versions")
def versions() -> dict[str, dict[str, Any]]:
    return {
        version: asdict(manifest)
        for version, manifest in kb_version_manifests().items()
    }


@router.post("/api/kb/query", response_model=V2QueryResponse)
def query_v2(
    request: V2QueryRequest,
    services: KbServices = Depends(get_kb_services),
) -> V2QueryResponse:
    started_at = perf_counter()
    logger.info(
        "KB typed query start version={} query={!r} top_k={}",
        request.kb_version,
        safe_text(request.query),
        request.top_k,
    )
    try:
        gemini = services.gemini
    except RuntimeError:
        gemini = None
    try:
        response = V2RetrievalService(services.v2_store, gemini).query(
            request.query,
            request.top_k,
        )
    except Exception as exc:
        logger.exception(
            "KB typed query error version={} error_type={} elapsed_ms={}",
            request.kb_version,
            exc.__class__.__name__,
            int((perf_counter() - started_at) * 1000),
        )
        raise
    logger.info(
        "KB typed query end version={} intent={} entities={} recommendations={} facts={} planner={} elapsed_ms={}",
        request.kb_version,
        response.answer_type,
        len(response.entities),
        len(response.recommendations),
        len(response.facts),
        response.trace[0].get("planner") if response.trace else "-",
        int((perf_counter() - started_at) * 1000),
    )
    return response


@router.post("/api/kb/search", response_model=KbSearchResponse)
def search(
    request: KbSearchRequest,
    services: KbServices = Depends(get_kb_services),
) -> KbSearchResponse:
    started_at = perf_counter()
    logger.info(
        "KB search start strategy={} query={!r} city={} entity_types={} top_k={}",
        request.strategy,
        safe_text(request.query),
        request.city or "-",
        request.entity_types or [],
        request.top_k,
    )
    try:
        response = _run_search(request, services)
        if _all_retrieval_sources_failed(response.trace):
            raise HTTPException(
                status_code=503,
                detail="Knowledge Base retrieval is temporarily unavailable.",
            )
    except Exception as exc:
        logger.exception(
            "KB search error strategy={} error_type={} elapsed_ms={}",
            request.strategy,
            exc.__class__.__name__,
            int((perf_counter() - started_at) * 1000),
        )
        raise
    result = KbSearchResponse(
        strategy=response.strategy,
        results=[_to_search_result(row) for row in response.results],
        trace=response.trace,
    )
    logger.info(
        "KB search end strategy={} result_count={} result_ids={} trace_steps={} elapsed_ms={}",
        result.strategy,
        len(result.results),
        [item.place_id for item in result.results],
        [event.get("step") for event in result.trace],
        int((perf_counter() - started_at) * 1000),
    )
    return result


@router.post("/api/kb/answer", response_model=KbAnswerResponse)
def answer(
    request: KbAnswerRequest,
    services: KbServices = Depends(get_kb_services),
) -> KbAnswerResponse:
    started_at = perf_counter()
    logger.info(
        "KB answer start strategy={} query={!r} city={} entity_types={} top_k={}",
        request.strategy,
        safe_text(request.query),
        request.city or "-",
        request.entity_types or [],
        request.top_k,
    )
    rag = TravelGraphRAG(services.store, services.gemini)
    text = rag.answer(
        question=request.query,
        city=request.city,
        entity_types=request.entity_types,
        top_k=request.top_k,
        strategy=request.strategy,
    )
    result = KbAnswerResponse(
        answer=text,
        strategy=request.strategy,
        trace=[{"step": "kb_answer", "status": "ok", "strategy": request.strategy}],
    )
    logger.info(
        "KB answer end strategy={} answer_len={} elapsed_ms={}",
        result.strategy,
        len(result.answer),
        int((perf_counter() - started_at) * 1000),
    )
    return result
