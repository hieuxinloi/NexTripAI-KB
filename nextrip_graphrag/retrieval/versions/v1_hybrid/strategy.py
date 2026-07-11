from __future__ import annotations

from typing import Any

from ...base import trace_error
from ...models import SearchResponse
from ...query_features import extract_graph_filters


RRF_K = 60
CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATES = 20
SOURCE_WEIGHTS = {"vector": 1.0, "keyword": 1.0, "graph": 2.0}


def _place_id(row: dict[str, Any]) -> str:
    return str((row.get("place") or {}).get("id") or "")


def reciprocal_rank_fusion(
    vector_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    limit: int,
    graph_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    ranked_sources = (
        ("vector", vector_rows),
        ("keyword", keyword_rows),
        ("graph", graph_rows or []),
    )
    for source, rows in ranked_sources:
        for rank, row in enumerate(rows, start=1):
            place_id = _place_id(row)
            if not place_id:
                continue
            item = fused.setdefault(
                place_id,
                {"row": dict(row), "rrf_score": 0.0, "retrieval": {}},
            )
            item["rrf_score"] += SOURCE_WEIGHTS[source] / (RRF_K + rank)
            item["retrieval"][f"{source}_rank"] = rank
            item["retrieval"][f"{source}_score"] = row.get("score")

    ranked = sorted(
        fused.values(),
        key=lambda item: (
            item["rrf_score"],
            -min(
                item["retrieval"].get("vector_rank", 10_000),
                item["retrieval"].get("keyword_rank", 10_000),
                item["retrieval"].get("graph_rank", 10_000),
            ),
        ),
        reverse=True,
    )
    results = []
    for item in ranked[:limit]:
        row = item["row"]
        row["score"] = item["rrf_score"]
        row["retrieval"] = item["retrieval"]
        results.append(row)
    return results


class V1HybridRetriever:
    """Retrieval ablation on KB V1; this is not a new graph version."""

    name = "v1_hybrid"

    def search(self, request, store, embedder) -> SearchResponse:
        trace = []
        candidate_limit = max(request.limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)

        try:
            if store.has_embeddings():
                embedding = embedder.embed_query(request.query)
                vector_rows = store.vector_search(
                    embedding=embedding,
                    limit=candidate_limit,
                    city_id=request.city_id,
                    entity_types=request.entity_types,
                )
                trace.append(
                    {"step": "vector_search", "status": "ok", "count": len(vector_rows)}
                )
            else:
                vector_rows = []
                trace.append(
                    {
                        "step": "vector_search",
                        "status": "skipped",
                        "reason": "graph_has_no_embeddings",
                    }
                )
        except Exception as exc:
            vector_rows = []
            trace.append(trace_error("vector_search", exc))

        try:
            keyword_rows = store.keyword_search(
                query_text=request.query,
                limit=candidate_limit,
                city_id=request.city_id,
                entity_types=request.entity_types,
            )
            trace.append({"step": "keyword_search", "status": "ok", "count": len(keyword_rows)})
        except Exception as exc:
            keyword_rows = []
            trace.append(trace_error("keyword_search", exc))

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
                        "filters": {
                            "categories": filters.categories,
                            "terms": filters.terms,
                            "indoor_or_all_weather": filters.indoor_or_all_weather,
                        },
                        "count": len(graph_rows),
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
                "step": "rrf_fusion",
                "status": "ok",
                "rrf_k": RRF_K,
                "source_weights": SOURCE_WEIGHTS,
                "candidate_limit": candidate_limit,
                "count": len(results),
            }
        )
        return SearchResponse(strategy=self.name, results=results, trace=trace)
