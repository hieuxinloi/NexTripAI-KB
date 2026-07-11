from __future__ import annotations

from ...base import trace_error
from ...models import SearchResponse
from ...query_features import extract_graph_filters
from ..v1_hybrid.strategy import MIN_CANDIDATES, reciprocal_rank_fusion


class V1ProvenanceRetriever:
    """TextUnit-first retrieval that returns source-backed place evidence."""

    name = "v1_provenance"

    def search(self, request, store, embedder) -> SearchResponse:
        trace = []
        candidate_limit = max(request.limit * 4, MIN_CANDIDATES)

        try:
            if store.has_text_unit_embeddings():
                vector_rows = store.text_unit_vector_search(
                    embedding=embedder.embed_query(request.query),
                    limit=candidate_limit,
                    city_id=request.city_id,
                    entity_types=request.entity_types,
                )
                trace.append(
                    {"step": "text_unit_vector_search", "status": "ok", "count": len(vector_rows)}
                )
            else:
                vector_rows = []
                trace.append(
                    {
                        "step": "text_unit_vector_search",
                        "status": "skipped",
                        "reason": "graph_has_no_text_unit_embeddings",
                    }
                )
        except Exception as exc:
            vector_rows = []
            trace.append(trace_error("text_unit_vector_search", exc))

        try:
            keyword_rows = store.text_unit_keyword_search(
                query_text=request.query,
                limit=candidate_limit,
                city_id=request.city_id,
                entity_types=request.entity_types,
            )
            trace.append(
                {"step": "text_unit_keyword_search", "status": "ok", "count": len(keyword_rows)}
            )
        except Exception as exc:
            keyword_rows = []
            trace.append(trace_error("text_unit_keyword_search", exc))

        filters = extract_graph_filters(request.query)
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
                trace.append(
                    {
                        "step": "graph_filter_search",
                        "status": "ok",
                        "count": len(graph_rows),
                        "filters": {
                            "categories": filters.categories,
                            "terms": filters.terms,
                            "indoor_or_all_weather": filters.indoor_or_all_weather,
                        },
                    }
                )
            except Exception as exc:
                graph_rows = []
                trace.append(trace_error("graph_filter_search", exc))
        else:
            graph_rows = []
            trace.append({"step": "graph_filter_search", "status": "skipped"})

        results = reciprocal_rank_fusion(
            vector_rows,
            keyword_rows,
            request.limit,
            graph_rows=graph_rows,
        )
        trace.append(
            {
                "step": "text_unit_rrf_fusion",
                "status": "ok",
                "candidate_limit": candidate_limit,
                "count": len(results),
            }
        )
        return SearchResponse(strategy=self.name, results=results, trace=trace)
