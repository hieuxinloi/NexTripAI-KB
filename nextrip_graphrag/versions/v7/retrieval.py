from __future__ import annotations

from typing import Any

from ...retrieval.rank_fusion import fuse_ranked_ids
from ..v2.retrieval import _entity, _fulltext_query
from ..v2.schemas import EntityResult
from ..v4.policy import POLICY
from ..v5.retrieval import V5RetrievalService
from ..v5.schemas import V5QueryPlan
from .entity_linker import SemanticEntityLinker
from .query_planner import plan_query
from .schemas import V7QueryResponse


class V7RetrievalService(V5RetrievalService):
    """LLM-native semantic planning with graph-grounded entity linking."""

    kb_version = "v5"
    graph_kb_version = "v5"
    manifest_version = "v7"
    response_model = V7QueryResponse
    place_fallback_strategy = "hybrid_place_rrf_fallback"

    def _ensure_ready(self) -> None:
        rows = self.store.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            RETURN catalog.status AS status
            """,
            kb_version=self.graph_kb_version,
        )
        if not rows or rows[0]["status"] != "ready":
            raise RuntimeError("GraphRAG V5 snapshot is not ready")

    def _plan_query(
        self,
        query: str,
        catalog: dict[str, list[str]],
    ) -> tuple[V5QueryPlan, str, Any | None]:
        return plan_query(query, self.gemini, catalog)

    def _ground_plan(
        self,
        plan: V5QueryPlan,
        catalog: dict[str, list[str]],
        query: str,
    ) -> tuple[V5QueryPlan, list[str], bool, list[dict[str, Any]]]:
        result = SemanticEntityLinker(self.store, self.gemini).ground_plan(
            plan,
            catalog,
            user_query=query,
        )
        trace = {
            "step": "semantic_entity_grounding",
            "status": "blocked" if result.blocked else "ok",
            "links": [link.model_dump() for link in result.links],
            "concept_links": result.concept_links,
            "missing_fields": result.missing_fields,
        }
        return result.plan, result.missing_fields, result.blocked, [trace]

    def _place_fallback_candidates(
        self,
        query: str,
        query_embedding: list[float],
        city: str | None,
        entity_types: list[str],
        limit: int,
        place_ids: list[str] | None = None,
    ) -> list[EntityResult]:
        candidate_limit = max(
            limit * POLICY.candidate_multiplier,
            POLICY.minimum_vector_pool,
        )
        sources: dict[str, list[dict[str, Any]]] = {
            "vector": self._indexed_place_candidates(
                """
                CALL db.index.vector.queryNodes(
                  'v5_place_embedding', $candidate_limit, $embedding
                )
                YIELD node AS place, score
                WHERE place.kb_version = $kb_version
                  AND ($city IS NULL OR place.city = $city)
                  AND ($entity_types = [] OR place.entity_type IN $entity_types)
                  AND ($place_ids IS NULL OR place.id IN $place_ids)
                RETURN place {.*} AS place, score
                ORDER BY score DESC
                """,
                candidate_limit=candidate_limit,
                embedding=query_embedding,
                city=city,
                entity_types=entity_types,
                place_ids=place_ids,
            )
        }
        query_text = _fulltext_query(query)
        if query_text:
            sources["fulltext"] = self._indexed_place_candidates(
                """
                CALL db.index.fulltext.queryNodes(
                  'v5_place_fulltext', $query_text, {limit: $candidate_limit}
                )
                YIELD node AS place, score
                WHERE place.kb_version = $kb_version
                  AND ($city IS NULL OR place.city = $city)
                  AND ($entity_types = [] OR place.entity_type IN $entity_types)
                  AND ($place_ids IS NULL OR place.id IN $place_ids)
                RETURN place {.*} AS place, score
                ORDER BY score DESC
                """,
                candidate_limit=candidate_limit,
                query_text=query_text,
                city=city,
                entity_types=entity_types,
                place_ids=place_ids,
            )
        return _fuse_place_rows(sources, limit)

    def _indexed_place_candidates(
        self,
        cypher: str,
        **params: Any,
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.run_versioned(cypher, **params)]


def _fuse_place_rows(
    sources: dict[str, list[dict[str, Any]]],
    limit: int,
) -> list[EntityResult]:
    rows_by_id = {
        str(row["place"]["id"]): row
        for rows in sources.values()
        for row in rows
    }
    fused = fuse_ranked_ids(
        {
            source: [str(row["place"]["id"]) for row in rows]
            for source, rows in sources.items()
        }
    )
    results: list[EntityResult] = []
    for item in fused[:limit]:
        place = dict(rows_by_id[item.item_id]["place"])
        place["score"] = item.score
        results.append(_entity(place))
    return results


__all__ = ["V7RetrievalService", "V7QueryResponse"]
