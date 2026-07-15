from __future__ import annotations

from ...base import trace_error
from ...models import SearchResponse


class V1Retriever:
    """Original vector-first retrieval with keyword fallback."""

    name = "v1"

    def search(self, request, store, embedder) -> SearchResponse:
        trace = []
        try:
            embedding = embedder.embed_query(request.query)
            results = store.vector_search(
                embedding=embedding,
                limit=request.limit,
                city_id=request.city_id,
                entity_types=request.entity_types,
            )
            trace.append({"step": "vector_search", "status": "ok", "count": len(results)})
        except Exception as exc:
            results = []
            trace.append(trace_error("vector_search", exc))

        if results:
            for rank, row in enumerate(results, start=1):
                row["retrieval"] = {"vector_rank": rank, "vector_score": row.get("score")}
            return SearchResponse(strategy=self.name, results=results, trace=trace)

        try:
            results = store.keyword_search(
                query_text=request.query,
                limit=request.limit,
                city_id=request.city_id,
                entity_types=request.entity_types,
            )
            trace.append({"step": "keyword_search", "status": "ok", "count": len(results)})
            for rank, row in enumerate(results, start=1):
                row["retrieval"] = {"keyword_rank": rank, "keyword_score": row.get("score")}
        except Exception as exc:
            results = []
            trace.append(trace_error("keyword_search", exc))

        return SearchResponse(strategy=self.name, results=results, trace=trace)
