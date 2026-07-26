from __future__ import annotations

from typing import Any

from ..v2.schemas import EntityResult
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


__all__ = ["V8RetrievalService", "V8QueryResponse"]
