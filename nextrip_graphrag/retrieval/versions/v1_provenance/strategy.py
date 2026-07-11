from __future__ import annotations

from time import perf_counter

from loguru import logger

from ....logging import safe_text
from ...base import trace_error
from ...models import SearchResponse
from ...query_features import extract_graph_filters
from ..v1_hybrid.strategy import MIN_CANDIDATES, reciprocal_rank_fusion


class V1ProvenanceRetriever:
    """TextUnit-first retrieval that returns source-backed place evidence."""

    name = "v1_provenance"

    def search(self, request, store, embedder) -> SearchResponse:
        started_at = perf_counter()
        trace = []
        candidate_limit = max(request.limit * 4, MIN_CANDIDATES)
        logger.info(
            "Retrieval start strategy={} query={!r} city_id={} entity_types={} limit={} candidate_limit={}",
            self.name,
            safe_text(request.query),
            request.city_id or "-",
            request.entity_types or [],
            request.limit,
            candidate_limit,
        )

        step_started_at = perf_counter()
        try:
            if store.has_text_unit_embeddings():
                vector_rows = store.text_unit_vector_search(
                    embedding=embedder.embed_query(request.query),
                    limit=candidate_limit,
                    city_id=request.city_id,
                    entity_types=request.entity_types,
                )
                vector_event = {
                    "step": "text_unit_vector_search",
                    "status": "ok",
                    "count": len(vector_rows),
                    "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
                }
                trace.append(vector_event)
                logger.info(
                    "Retrieval step end strategy={} step={} status={} count={} elapsed_ms={}",
                    self.name,
                    vector_event["step"],
                    vector_event["status"],
                    vector_event["count"],
                    vector_event["elapsed_ms"],
                )
            else:
                vector_rows = []
                vector_event = {
                    "step": "text_unit_vector_search",
                    "status": "skipped",
                    "reason": "graph_has_no_text_unit_embeddings",
                    "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
                }
                trace.append(vector_event)
                logger.info(
                    "Retrieval step end strategy={} step={} status={} reason={} elapsed_ms={}",
                    self.name,
                    vector_event["step"],
                    vector_event["status"],
                    vector_event["reason"],
                    vector_event["elapsed_ms"],
                )
        except Exception as exc:
            vector_rows = []
            vector_event = {
                **trace_error("text_unit_vector_search", exc),
                "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
            }
            trace.append(vector_event)
            logger.warning(
                "Retrieval step error strategy={} step={} error_type={} elapsed_ms={}",
                self.name,
                vector_event["step"],
                vector_event["error_type"],
                vector_event["elapsed_ms"],
            )

        step_started_at = perf_counter()
        try:
            keyword_rows = store.text_unit_keyword_search(
                query_text=request.query,
                limit=candidate_limit,
                city_id=request.city_id,
                entity_types=request.entity_types,
            )
            keyword_event = {
                "step": "text_unit_keyword_search",
                "status": "ok",
                "count": len(keyword_rows),
                "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
            }
            trace.append(keyword_event)
            logger.info(
                "Retrieval step end strategy={} step={} status={} count={} elapsed_ms={}",
                self.name,
                keyword_event["step"],
                keyword_event["status"],
                keyword_event["count"],
                keyword_event["elapsed_ms"],
            )
        except Exception as exc:
            keyword_rows = []
            keyword_event = {
                **trace_error("text_unit_keyword_search", exc),
                "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
            }
            trace.append(keyword_event)
            logger.warning(
                "Retrieval step error strategy={} step={} error_type={} elapsed_ms={}",
                self.name,
                keyword_event["step"],
                keyword_event["error_type"],
                keyword_event["elapsed_ms"],
            )

        filters = extract_graph_filters(request.query)
        step_started_at = perf_counter()
        if filters.active:
            try:
                graph_rows = store.graph_filter_search(
                    limit=candidate_limit,
                    city_id=request.city_id,
                    entity_types=request.entity_types,
                    categories=filters.categories,
                    terms=filters.terms,
                    indoor_or_all_weather=filters.indoor_or_all_weather,
                )
                graph_event = {
                    "step": "graph_filter_search",
                    "status": "ok",
                    "count": len(graph_rows),
                    "filters": {
                        "categories": filters.categories,
                        "terms": filters.terms,
                        "indoor_or_all_weather": filters.indoor_or_all_weather,
                    },
                    "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
                }
                trace.append(graph_event)
                logger.info(
                    "Retrieval step end strategy={} step={} status={} filters={} count={} elapsed_ms={}",
                    self.name,
                    graph_event["step"],
                    graph_event["status"],
                    graph_event["filters"],
                    graph_event["count"],
                    graph_event["elapsed_ms"],
                )
            except Exception as exc:
                graph_rows = []
                graph_event = {
                    **trace_error("graph_filter_search", exc),
                    "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
                }
                trace.append(graph_event)
                logger.warning(
                    "Retrieval step error strategy={} step={} error_type={} elapsed_ms={}",
                    self.name,
                    graph_event["step"],
                    graph_event["error_type"],
                    graph_event["elapsed_ms"],
                )
        else:
            graph_rows = []
            graph_event = {
                "step": "graph_filter_search",
                "status": "skipped",
                "reason": "no_structured_filters",
                "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
            }
            trace.append(graph_event)
            logger.info(
                "Retrieval step end strategy={} step={} status={} reason={} elapsed_ms={}",
                self.name,
                graph_event["step"],
                graph_event["status"],
                graph_event["reason"],
                graph_event["elapsed_ms"],
            )

        step_started_at = perf_counter()
        results = reciprocal_rank_fusion(
            vector_rows,
            keyword_rows,
            request.limit,
            graph_rows=graph_rows,
        )
        fusion_event = {
            "step": "text_unit_rrf_fusion",
            "status": "ok",
            "candidate_limit": candidate_limit,
            "count": len(results),
            "elapsed_ms": int((perf_counter() - step_started_at) * 1000),
        }
        trace.append(fusion_event)
        result_ids = [str((row.get("place") or {}).get("id") or "") for row in results]
        logger.info(
            "Retrieval end strategy={} result_count={} result_ids={} vector_candidates={} keyword_candidates={} graph_candidates={} fusion_ms={} elapsed_ms={}",
            self.name,
            len(results),
            result_ids,
            len(vector_rows),
            len(keyword_rows),
            len(graph_rows),
            fusion_event["elapsed_ms"],
            int((perf_counter() - started_at) * 1000),
        )
        return SearchResponse(strategy=self.name, results=results, trace=trace)
