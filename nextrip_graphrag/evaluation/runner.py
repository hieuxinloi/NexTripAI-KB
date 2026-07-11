from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import mean
from typing import Any

from ..config import Settings
from ..gemini_client import GeminiClient
from ..enrichment.embeddings import CachedBatchEmbedder
from ..neo4j_store import Neo4jGraphStore
from ..normalizer import CITY_DEFINITIONS, canonical_city
from ..retrieval import SearchRequest, get_strategy


DEFAULT_DATASET = Path(__file__).parent / "datasets" / "smoke_v1.json"


def _city_id(city: str | None) -> str | None:
    if not city:
        return None
    return CITY_DEFINITIONS[canonical_city(city)]["id"]


def _metrics(retrieved_ids: list[str], accepted_ids: list[str]) -> dict[str, float]:
    accepted = set(accepted_ids)
    hits = [place_id for place_id in retrieved_ids if place_id in accepted]
    first_rank = next(
        (rank for rank, place_id in enumerate(retrieved_ids, start=1) if place_id in accepted),
        None,
    )
    return {
        "hit_at_k": float(bool(hits)),
        "precision_at_k": len(hits) / len(retrieved_ids) if retrieved_ids else 0.0,
        "relevance_at_k": len(set(hits)) / min(len(retrieved_ids), len(accepted))
        if retrieved_ids and accepted
        else 0.0,
        "accepted_recall_at_k": len(set(hits)) / len(accepted) if accepted else 0.0,
        "reciprocal_rank": 1.0 / first_rank if first_rank else 0.0,
    }


def run_benchmark(
    settings: Settings,
    strategy_name: str,
    dataset_path: str | Path = DEFAULT_DATASET,
) -> dict[str, Any]:
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    strategy = get_strategy(strategy_name)
    store = Neo4jGraphStore(settings)
    embedder = CachedBatchEmbedder(
        GeminiClient(settings),
        Path("enrichment_workspace") / "cache" / "embeddings",
        model=settings.embedding_model,
        dimensions=settings.embedding_dim,
        delay=5.0,
        max_retries=4,
    )
    cases = []

    try:
        embedded_count = store.run(
            "MATCH (p:Place) WHERE p.embedding IS NOT NULL RETURN count(p) AS count"
        )[0]["count"]
        embedded_text_unit_count = store.run(
            "MATCH (t:TextUnit) WHERE t.embedding IS NOT NULL RETURN count(t) AS count"
        )[0]["count"]
        for case in dataset["cases"]:
            request = SearchRequest(
                query=case["query"],
                limit=case["top_k"],
                city_id=_city_id(case.get("city")),
                entity_types=case.get("entity_types"),
            )
            started = time.perf_counter()
            response = strategy.search(request, store, embedder)
            latency_ms = (time.perf_counter() - started) * 1000
            retrieved_ids = [
                str((row.get("place") or {}).get("id") or "") for row in response.results
            ]
            case_metrics = _metrics(retrieved_ids, case["accepted_place_ids"])
            cases.append(
                {
                    "id": case["id"],
                    "level": case["level"],
                    "query": case["query"],
                    "retrieved_place_ids": retrieved_ids,
                    "accepted_place_ids": case["accepted_place_ids"],
                    "metrics": case_metrics,
                    "latency_ms": round(latency_ms, 2),
                    "trace": response.trace,
                }
            )
    finally:
        store.close()

    metric_names = (
        "hit_at_k",
        "precision_at_k",
        "relevance_at_k",
        "accepted_recall_at_k",
        "reciprocal_rank",
    )
    aggregate = {
        name: round(mean(case["metrics"][name] for case in cases), 4)
        for name in metric_names
    }
    aggregate["mean_latency_ms"] = round(mean(case["latency_ms"] for case in cases), 2)
    return {
        "benchmark": dataset["name"],
        "dataset": dataset["dataset"],
        "strategy": strategy.name,
        "embedded_place_count": embedded_count,
        "embedded_text_unit_count": embedded_text_unit_count,
        "case_count": len(cases),
        "aggregate": aggregate,
        "cases": cases,
    }
