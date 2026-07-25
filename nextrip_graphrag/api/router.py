from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import socket
from dataclasses import asdict
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from loguru import logger

from ..config import HEALTH_CHECK_TIMEOUT_SECONDS
from ..logging import safe_text
from ..normalizer import CITY_DEFINITIONS, canonical_city
from ..rag import TravelGraphRAG
from ..retrieval import SearchRequest, available_strategies, get_strategy
from ..versions.registry import (
    kb_version_manifests,
    version_retrieval_service_class,
)
from ..versions.v4.schemas import DynamicObservationInput, V4QueryResponse
from .schemas import (
    GraphContext,
    HealthResponse,
    KbAnswerRequest,
    KbAnswerResponse,
    KbSearchRequest,
    KbSearchResponse,
    KbSearchResult,
    ReadinessResponse,
    SourceInfo,
    TypedQueryRequest,
)
from .dependencies import KbServices, get_kb_services, require_admin_api_key

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


def _store_health(store: Any) -> str:
    try:
        target = urlparse(store.settings.neo4j_uri)
        with socket.create_connection(
            (target.hostname or "localhost", target.port or 7687),
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        ):
            pass
        store.run("RETURN 1 AS ok")
        return "ready"
    except Exception as exc:
        return f"not_ready:{exc.__class__.__name__}"


def _version_health(services: KbServices) -> dict[str, str]:
    stores = {
        version: services.store_for(version)
        for version in services.settings.configured_kb_versions
    }
    if not stores:
        return {}
    with ThreadPoolExecutor(max_workers=len(stores), thread_name_prefix="kb-health") as pool:
        statuses = pool.map(_store_health, stores.values())
    return dict(zip(stores, statuses, strict=True))


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok", "service": "nextrip-kb"}


@router.get("/ready", response_model=ReadinessResponse)
def ready(
    response: Response,
    version: str | None = Query(default=None, pattern=r"^v[1-9][0-9]*$"),
    services: KbServices = Depends(get_kb_services),
) -> ReadinessResponse:
    statuses = _version_health(services)
    ready_versions = [name for name, value in statuses.items() if value == "ready"]
    is_ready = version in ready_versions if version else bool(ready_versions)
    if not is_ready:
        response.status_code = 503
    return ReadinessResponse(
        status="ready" if is_ready else "not_ready",
        ready_versions=ready_versions,
        versions=statuses,
    )


@router.get("/health", response_model=HealthResponse)
def health(services: KbServices = Depends(get_kb_services)) -> HealthResponse:
    statuses = _version_health(services)
    return HealthResponse(
        status="ok" if any(value == "ready" for value in statuses.values()) else "degraded",
        neo4j=statuses.get("v1", "not_configured"),
        neo4j_v2=statuses.get("v2"),
        neo4j_v3=statuses.get("v3"),
        neo4j_v4=statuses.get("v4"),
        neo4j_v5=statuses.get("v5"),
        embedding_model=services.settings.embedding_model,
        retrieval_strategies=available_strategies(),
    )


@router.get("/api/kb/versions")
def versions(
    services: KbServices = Depends(get_kb_services),
) -> dict[str, dict[str, Any]]:
    configured = set(services.settings.configured_kb_versions)
    return {
        version: asdict(manifest)
        for version, manifest in kb_version_manifests().items()
        if version in configured
    }


@router.get("/api/kb/v4/stats")
def v4_stats(services: KbServices = Depends(get_kb_services)) -> dict[str, Any]:
    try:
        store = services.store_for("v4")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "kb_version": "v4",
        "statistics": store.graph_statistics(),
        "subgraphs": store.domain_statistics(),
        "invariants": store.validate_invariants(),
    }


@router.get("/api/kb/v5/stats")
def v5_stats(services: KbServices = Depends(get_kb_services)) -> dict[str, Any]:
    try:
        store = services.store_for("v5")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "kb_version": "v5",
        "statistics": store.graph_statistics(),
        "subgraphs": store.domain_statistics(),
        "invariants": store.validate_invariants(),
    }


@router.post("/api/kb/v4/explain", response_model=V4QueryResponse)
def explain_v4(
    request: TypedQueryRequest,
    services: KbServices = Depends(get_kb_services),
) -> V4QueryResponse:
    try:
        gemini = services.gemini
    except RuntimeError:
        gemini = None
    try:
        store = services.store_for("v4")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    service_type = version_retrieval_service_class("v4")
    return service_type(store, gemini).query(request.query, request.top_k)


@router.post("/api/kb/v4/observations")
def upsert_v4_observation(
    observation: DynamicObservationInput,
    _: None = Depends(require_admin_api_key),
    services: KbServices = Depends(get_kb_services),
) -> dict[str, Any]:
    try:
        return services.store_for("v4").upsert_dynamic_observation(observation)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/kb/query")
def query_typed(
    request: TypedQueryRequest,
    services: KbServices = Depends(get_kb_services),
) -> Any:
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
    if request.kb_version not in services.settings.configured_kb_versions:
        raise HTTPException(
            status_code=404,
            detail=f"Knowledge Base {request.kb_version.upper()} is not configured.",
        )
    if request.kb_version == "v1":
        raise HTTPException(
            status_code=400,
            detail="Knowledge Base V1 does not support the typed query endpoint.",
        )
    try:
        service_type = version_retrieval_service_class(request.kb_version)
        store = services.store_for(request.kb_version)
        response = service_type(store, gemini).query(request.query, request.top_k)
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
