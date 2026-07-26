from __future__ import annotations

import json
from collections.abc import Callable
from functools import cached_property
from typing import Any

from neo4j import Record
from neo4j_graphrag.retrievers import HybridCypherRetriever
from neo4j_graphrag.retrievers.hybrid import HybridSearchRanker
from neo4j_graphrag.types import RetrieverResultItem

from ..v2.retrieval import _entity, _fulltext_query
from ..v2.schemas import EntityResult, FactResult
from ..v4.policy import POLICY
from ..v4.schemas import V4EvidenceResult
from ..v6.context import ResolvedTurn
from ..v6.retrieval import V6RetrievalService
from ..v6.schemas import ConversationContext
from ..v5.retrieval import _RetrievalOutcome
from ..v5.schemas import QueryTask, V5Intent, V5QueryPlan, V5QueryResponse
from ..v7.entity_linker import SemanticEntityLinker
from ..v7.query_planner import _keep_material_clarification
from .query_planner import plan_query
from .schemas import V8QueryResponse


class V8RetrievalService(V6RetrievalService):
    """V8 combines V7 semantic grounding with V6 stateful graph execution.

    Retrieval remains closed-world: Gemini proposes meaning, while Neo4j
    candidate indexes, typed edges and evidence TextUnits decide what is valid.
    """

    kb_version = "v8"
    graph_kb_version = "v8"
    api_kb_version = "v8"
    manifest_version = "v8"
    response_model = V8QueryResponse
    place_fallback_strategy = "neo4j_graphrag_hybrid_evidence_v8"
    fulltext_index = "v8_place_fulltext"
    vector_index = "v8_place_embedding"

    @cached_property
    def _place_hybrid_retriever(self) -> HybridCypherRetriever:
        return HybridCypherRetriever(
            driver=self.store.driver,
            vector_index_name=self.vector_index,
            fulltext_index_name=self.fulltext_index,
            retrieval_query="""
            WHERE node.kb_version = $kb_version
              AND ($city IS NULL OR node.city = $city)
              AND ($entity_types = [] OR node.entity_type IN $entity_types)
              AND ($place_ids IS NULL OR node.id IN $place_ids)
            RETURN node {.*} AS place, score
            ORDER BY score DESC
            LIMIT $result_limit
            """,
            result_formatter=_format_place_result,
            neo4j_database=self.store.settings.neo4j_database,
        )

    def _resolve_turn(
        self,
        query: str,
        context: ConversationContext,
        catalog: dict[str, list[str]],
    ) -> ResolvedTurn:
        del catalog
        if context.turn_count == 0:
            return ResolvedTurn(query=query, updates=[])
        planner_query = json.dumps(
            {
                "current_message": query,
                "conversation_context": context.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ResolvedTurn(
            query=query,
            planner_query=planner_query,
            updates=["semantic_context"],
        )

    def _planner_input(self, resolved: ResolvedTurn) -> str:
        if resolved.planner_query is not None:
            return resolved.planner_query
        return resolved.query

    def _initial_itinerary_request(self, resolved: ResolvedTurn) -> bool:
        del resolved
        return False

    def _final_itinerary_request(
        self,
        resolved: ResolvedTurn,
        response: V5QueryResponse,
        initially_itinerary: bool,
    ) -> bool:
        del resolved, initially_itinerary
        return response.intent == V5Intent.PLAN_CANDIDATES

    def _ensure_ready(self) -> None:
        rows = self.store.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            RETURN catalog.status AS status
            LIMIT 1
            """,
            kb_version=self.graph_kb_version,
        )
        if not rows or rows[0]["status"] != "ready":
            raise RuntimeError(
                "GraphRAG V8 projection is not ready; run the v8-project command."
            )

    def _count_fact(
        self,
        city: str | None,
        entity_types: list[str],
    ) -> FactResult:
        rows = self.store.run_versioned(
            """
            MATCH (place:Place)-[:IN_CITY]->(scope:City)
            WHERE place.kb_version = $kb_version
              AND scope.kb_version = $kb_version
              AND ($city IS NULL OR scope.name = $city)
              AND place.entity_type IN $entity_types
            RETURN count(DISTINCT place) AS count,
                   head(collect(DISTINCT scope.id)) AS city_id
            """,
            city=city,
            entity_types=entity_types,
        )
        city_id = rows[0]["city_id"]
        if city_id is None:
            city_id = city if city is not None else "all"
        return FactResult(
            fact_id=f"aggregate:{city_id}:{'-'.join(entity_types)}",
            subject_id=str(city_id),
            predicate="count",
            entity_type=entity_types[0],
            value=int(rows[0]["count"]),
            value_type="number",
            confidence=1,
        )

    def _execute_plan(
        self,
        plan: V5QueryPlan,
        query: str,
        top_k: int,
    ) -> _RetrievalOutcome:
        tasks = _effective_tasks(plan)
        if not tasks:
            return super()._execute_plan(plan, query, top_k)

        merged = _RetrievalOutcome()
        task_trace: list[dict[str, Any]] = []
        for task in tasks:
            task_plan = plan.model_copy(
                update={
                    "intent": task.intent,
                    "targets": task.targets,
                    "geo_scope": task.geo_scope,
                    "required_concepts": task.required_concepts,
                    "preferred_concepts": task.preferred_concepts,
                    "ranking_criteria": task.ranking_criteria,
                    "constraints": task.constraints,
                    "limit": task.limit,
                    "tasks": [],
                }
            )
            outcome = super()._execute_plan(task_plan, query, top_k)
            _merge_outcome(merged, outcome)
            task_trace.append(
                {
                    "step": "v8_query_task",
                    "status": "ok",
                    "task": task.name,
                    "recommendation_count": len(outcome.recommendations),
                    "constraint_count": len(outcome.constraint_results),
                }
            )
        merged.trace.extend(task_trace)
        merged.retrieval_strategy = "v8_multi_task_graph"
        return merged

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
        result = SemanticEntityLinker(
            self.store,
            self.gemini,
            place_fulltext_index=self.fulltext_index,
            place_vector_index=self.vector_index,
        ).ground_plan(
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
        if _requires_city_scope(grounded_plan):
            blocking_missing.append("unresolved:city:query_scope")
        public_missing = _public_missing_fields(blocking_missing)
        trace = {
            "step": "v8_semantic_grounding",
            "status": (
                "blocked"
                if blocking_missing
                else ("partial" if unresolved_concepts else "ok")
            ),
            "links": [link.model_dump() for link in result.links],
            "concept_links": result.concept_links,
            "relaxed_concepts": unresolved_concepts,
            "diagnostics": blocking_missing,
            "missing_fields": public_missing,
        }
        return (
            grounded_plan,
            public_missing,
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
        """Use Neo4j GraphRAG's validated hybrid retrieval implementation."""
        candidate_limit = max(
            limit * POLICY.candidate_multiplier,
            POLICY.minimum_vector_pool,
        )
        query_text = _fulltext_query(query)
        if not query_text:
            return []
        result = self._place_hybrid_retriever.search(
            query_text=query_text,
            query_vector=query_embedding,
            top_k=candidate_limit,
            ranker=HybridSearchRanker.NAIVE,
            query_params={
                "kb_version": self.graph_kb_version,
                "city": city,
                "entity_types": entity_types,
                "place_ids": place_ids,
                "result_limit": limit,
            },
        )
        return [_entity(_place_payload(item)) for item in result.items]


def _public_missing_fields(diagnostics: list[str]) -> list[str]:
    fields: list[str] = []
    for item in diagnostics:
        if item.startswith("unresolved:city:"):
            fields.append("city")
        elif item.startswith("unresolved:geo_area:"):
            fields.append("geo_area")
        elif item.startswith("unresolved:place:"):
            fields.append("near_reference")
        elif item.startswith("unresolved:"):
            _, kind, _ = item.split(":", 2)
            fields.append(kind)
        else:
            fields.append(item)
    return list(dict.fromkeys(fields))


def _requires_city_scope(plan: V5QueryPlan) -> bool:
    if plan.intent not in {
        V5Intent.LIST,
        V5Intent.RECOMMEND,
        V5Intent.PLAN_CANDIDATES,
    }:
        return False
    if (
        plan.geo_scope.cities
        or plan.geo_scope.areas
        or plan.geo_scope.near_entities
        or any(constraint.field == "near_subject" for constraint in plan.constraints)
    ):
        return False
    return any(
        target.kind.value == "place"
        and not target.value
        and bool(target.entity_types)
        for target in plan.targets
    )


def _effective_tasks(plan: V5QueryPlan) -> list[QueryTask]:
    if plan.tasks:
        return plan.tasks
    if plan.intent != V5Intent.PLAN_CANDIDATES:
        return []
    place_targets = [
        target
        for target in plan.targets
        if target.entity_types
    ]
    entity_types = list(
        dict.fromkeys(
            entity_type
            for target in place_targets
            for entity_type in target.entity_types
        )
    )
    if len(entity_types) <= 1:
        return []
    return [
        QueryTask(
            name=entity_type,
            targets=[
                target.model_copy(update={"entity_types": [entity_type]})
                for target in place_targets
                if entity_type in target.entity_types
            ],
            geo_scope=plan.geo_scope,
            required_concepts=plan.required_concepts,
            preferred_concepts=plan.preferred_concepts,
            ranking_criteria=plan.ranking_criteria,
            constraints=plan.constraints,
            limit=max(3, plan.limit),
        )
        for entity_type in entity_types
    ]


def _merge_outcome(destination: _RetrievalOutcome, source: _RetrievalOutcome) -> None:
    destination.targets = _unique_by(
        [*destination.targets, *source.targets],
        lambda item: item.target_id,
    )
    destination.entities = _unique_by(
        [*destination.entities, *source.entities],
        lambda item: item.place_id,
    )
    destination.facts = _unique_by(
        [*destination.facts, *source.facts],
        lambda item: item.fact_id,
    )
    destination.recommendations = _unique_by(
        [*destination.recommendations, *source.recommendations],
        lambda item: item.place_id,
    )
    destination.matched_paths = _unique_by(
        [*destination.matched_paths, *source.matched_paths],
        lambda item: (item.place_id, tuple(item.nodes), tuple(item.relationships)),
    )
    destination.constraint_results = _unique_by(
        [*destination.constraint_results, *source.constraint_results],
        lambda item: (item.place_id, item.field),
    )
    destination.missing_fields = list(
        dict.fromkeys([*destination.missing_fields, *source.missing_fields])
    )
    destination.trace.extend(source.trace)
    destination.place_evidence_required = (
        destination.place_evidence_required or source.place_evidence_required
    )


def _unique_by(items: list[Any], key: Callable[[Any], Any]) -> list[Any]:
    unique: dict[Any, Any] = {}
    for item in items:
        unique.setdefault(key(item), item)
    return list(unique.values())


def _format_place_result(record: Record) -> RetrieverResultItem:
    return RetrieverResultItem(
        content=dict(record["place"]),
        metadata={"score": float(record["score"])},
    )


def _place_payload(item: RetrieverResultItem) -> dict[str, Any]:
    place = dict(item.content)
    place["score"] = item.metadata["score"]
    return place


__all__ = ["V8RetrievalService", "V8QueryResponse"]
