from __future__ import annotations

from typing import Any

from ...retrieval.rank_fusion import fuse_ranked_ids
from ..v2.retrieval import _entity, _fulltext_query
from ..v2.schemas import EntityResult
from ..v4.policy import POLICY
from ..v4.schemas import V4EvidenceResult
from ..v6.retrieval import V6RetrievalService
from ..v5.schemas import V5QueryPlan
from ..v7.entity_linker import SemanticEntityLinker
from ..v7.query_planner import _keep_material_clarification
from .query_planner import plan_query
from .schemas import V8QueryResponse


class V8RetrievalService(V6RetrievalService):
    """V8 combines V7 semantic grounding with V6 stateful graph execution.

    Retrieval remains closed-world: Gemini proposes meaning, while Neo4j
    candidate indexes, typed edges and evidence TextUnits decide what is valid.
    """

    kb_version = "v5"
    graph_kb_version = "v5"
    api_kb_version = "v8"
    manifest_version = "v8"
    response_model = V8QueryResponse
    place_fallback_strategy = "hybrid_place_rrf_evidence_v8"

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
        unresolved_concepts = [
            item.removeprefix("concept:")
            for item in result.missing_fields
            if item.startswith("concept:")
        ]
        # An unknown paraphrase is not permission to invent a node. Treat it as
        # a soft semantic hint so hybrid retrieval can still find evidence chunks.
        grounded_plan = result.plan
        if unresolved_concepts:
            grounded_plan = grounded_plan.model_copy(
                update={
                    "preferred_concepts": list(
                        dict.fromkeys(
                            [
                                *grounded_plan.preferred_concepts,
                                *unresolved_concepts,
                            ]
                        )
                    ),
                    "required_concepts": [
                        value
                        for value in grounded_plan.required_concepts
                        if value not in unresolved_concepts
                    ],
                    "clarification_needed": _keep_material_clarification(
                        grounded_plan
                    ),
                }
            )
        blocking_missing = [
            item
            for item in result.missing_fields
            if not item.startswith("concept:")
        ]
        trace = {
            "step": "v8_semantic_grounding",
            "status": "partial" if unresolved_concepts else "ok",
            "links": [link.model_dump() for link in result.links],
            "concept_links": result.concept_links,
            "relaxed_concepts": unresolved_concepts,
            "missing_fields": blocking_missing,
        }
        return (
            grounded_plan,
            blocking_missing,
            bool(blocking_missing),
            [trace],
        )

    def _place_evidence(
        self,
        places: list[EntityResult],
    ) -> list[V4EvidenceResult]:
        """Return several evidence chunks linked through claim and mention edges."""
        if not places:
            return []
        rows = self.store.run_versioned(
            """
            UNWIND $place_ids AS place_id
            MATCH (place:Place {id: place_id, kb_version: $kb_version})
            CALL (place) {
              MATCH (unit:TextUnit {kb_version: $kb_version})-[:MENTIONS]->(place)
              RETURN unit, 0.0 AS claim_confidence
              UNION
              MATCH (place)-[:HAS_OFFERING*0..1]->(subject)
                    <-[:ABOUT]-(claim:Claim {kb_version: $kb_version})
                    -[:SUPPORTED_BY]->(unit:TextUnit {kb_version: $kb_version})
              RETURN unit, coalesce(claim.confidence, 0.0) AS claim_confidence
            }
            MATCH (unit)-[:PART_OF]->(document:Document)
            WITH place, unit, document, max(claim_confidence) AS claim_confidence
            ORDER BY place.id, claim_confidence DESC, unit.sequence
            WITH place, collect({
              unit: unit,
              document: document,
              claim_confidence: claim_confidence
            })[0..3] AS chunks
            UNWIND chunks AS chunk
            RETURN place.id AS subject_id,
                   chunk.unit.id AS text_unit_id,
                   chunk.document.id AS document_id,
                   coalesce(chunk.unit.title, chunk.document.title, place.name) AS title,
                   chunk.unit.text AS text,
                   chunk.document.url AS url,
                   chunk.document.source_name AS source_name,
                   chunk.unit.sequence AS sequence,
                   chunk.unit.evidence_origin AS evidence_origin,
                   chunk.claim_confidence AS confidence
            """,
            place_ids=[place.place_id for place in places],
        )
        return [V4EvidenceResult.model_validate(row) for row in rows]

    def _place_fallback_candidates(
        self,
        query: str,
        query_embedding: list[float],
        city: str | None,
        entity_types: list[str],
        limit: int,
        place_ids: list[str] | None = None,
    ) -> list[EntityResult]:
        """Use Neo4j's current SEARCH vector syntax plus full-text RRF."""
        candidate_limit = max(
            limit * POLICY.candidate_multiplier,
            POLICY.minimum_vector_pool,
        )
        vector_rows = self.store.run_versioned(
            f"""
            MATCH (place:Place)
            SEARCH place IN (
              VECTOR INDEX {self.vector_index}
              FOR $embedding
              LIMIT $candidate_limit
            )
            SCORE AS score
            WHERE place.kb_version = $kb_version
              AND ($city IS NULL OR place.city = $city)
              AND ($entity_types = [] OR place.entity_type IN $entity_types)
              AND ($place_ids IS NULL OR place.id IN $place_ids)
            RETURN place {{.*}} AS place, score
            """,
            embedding=query_embedding,
            city=city,
            entity_types=entity_types,
            place_ids=place_ids,
            candidate_limit=candidate_limit,
        )
        query_text = _fulltext_query(query)
        fulltext_rows = (
            self.store.run_versioned(
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
                query_text=query_text,
                city=city,
                entity_types=entity_types,
                place_ids=place_ids,
                candidate_limit=candidate_limit,
            )
            if query_text
            else []
        )
        rows_by_id = {
            str(row["place"]["id"]): row
            for row in [*vector_rows, *fulltext_rows]
        }
        fused = fuse_ranked_ids(
            {
                source: [
                    str(row["place"]["id"])
                    for row in rows
                ]
                for source, rows in {
                    "vector": vector_rows,
                    "fulltext": fulltext_rows,
                }.items()
            }
        )
        results: list[EntityResult] = []
        for item in fused[:limit]:
            place = dict(rows_by_id[item.item_id]["place"])
            place["score"] = item.score
            results.append(_entity(place))
        return results


__all__ = ["V8RetrievalService", "V8QueryResponse"]
