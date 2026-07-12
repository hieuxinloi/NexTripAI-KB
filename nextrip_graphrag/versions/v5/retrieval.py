from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any

from ...config import DEFAULT_TYPED_QUERY_TOP_K
from ..registry import kb_version_manifests
from ..v2.retrieval import _elapsed_ms, _entity
from ..v2.schemas import EntityResult, FactResult, QueryIntent
from ..v4.policy import POLICY
from ..v4.retrieval import V4RetrievalService
from ..v4.schemas import ConstraintResult, MatchedPath, V4EvidenceResult
from .concept_linker import ConceptLinkBatch, ConceptLinker
from .graph_store import V5GraphStore
from .query_planner import PlannerFailure, plan_query
from .resolver import V5EntityResolver
from .schemas import QueryTarget, TargetKind, TargetResult, V5Intent, V5QueryPlan, V5QueryResponse


_ANSWER_TYPES = {
    V5Intent.LOOKUP: QueryIntent.ENTITY_DETAIL,
    V5Intent.PROFILE: QueryIntent.ENTITY_DETAIL,
    V5Intent.LIST: QueryIntent.ENTITY_LIST,
    V5Intent.RECOMMEND: QueryIntent.RECOMMENDATION,
    V5Intent.COMPARE: QueryIntent.ENTITY_LIST,
    V5Intent.SUMMARIZE: QueryIntent.ENTITY_LIST,
    V5Intent.AGGREGATE: QueryIntent.AGGREGATE_COUNT,
    V5Intent.PLAN_CANDIDATES: QueryIntent.RECOMMENDATION,
    V5Intent.TOOL_REQUIRED: QueryIntent.UNSUPPORTED,
    V5Intent.UNSUPPORTED: QueryIntent.UNSUPPORTED,
}


@dataclass
class _RetrievalOutcome:
    targets: list[TargetResult] = field(default_factory=list)
    entities: list[EntityResult] = field(default_factory=list)
    facts: list[FactResult] = field(default_factory=list)
    recommendations: list[EntityResult] = field(default_factory=list)
    matched_paths: list[MatchedPath] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    retrieval_strategy: str = "typed_graph"


class V5RetrievalService(V4RetrievalService):
    kb_version = "v5"
    fulltext_index = "v5_place_fulltext"
    vector_index = "v5_place_embedding"

    def __init__(self, store: V5GraphStore, gemini: Any | None = None):
        super().__init__(store, gemini)
        self.resolver = V5EntityResolver(store)

    def query(self, query: str, top_k: int = DEFAULT_TYPED_QUERY_TOP_K) -> V5QueryResponse:
        self._ensure_ready()
        started = perf_counter()
        catalog = self.store.planner_catalog()
        plan, planner, failure = plan_query(query, self.gemini, catalog)
        trace: list[dict[str, Any]] = [
            {
                "step": "query_planner",
                "status": "unavailable" if failure else "ok",
                "planner": planner,
                "failure_reason": failure.reason if failure else None,
                "failure_code": failure.code if failure else None,
                "elapsed_ms": _elapsed_ms(started),
            }
        ]
        missing_fields: list[str] = []
        outcome = _RetrievalOutcome()
        required_links = ConceptLinkBatch()

        if not failure and (plan.required_concepts or plan.preferred_concepts):
            plan, required_links, preferred_links = self._link_concepts(
                plan,
                catalog["concepts"],
                query,
            )
            missing_fields.extend(f"concept:{term}" for term in required_links.unresolved)
            trace.append(_concept_link_trace(required_links, preferred_links))

        if plan.clarification_needed:
            missing_fields.append("query_constraints")

        retrieval_blocked = (
            failure is not None
            or plan.clarification_needed
            or bool(required_links.unresolved)
            or plan.intent in {V5Intent.UNSUPPORTED, V5Intent.TOOL_REQUIRED}
        )
        if not retrieval_blocked:
            outcome = self._execute_plan(plan, query, top_k)
            missing_fields.extend(outcome.missing_fields)

        evidence = self._merge_evidence(
            self._target_evidence(outcome.targets),
            self._place_evidence(outcome.recommendations)
            if outcome.retrieval_strategy == "semantic_place_fallback"
            else [],
            self._claim_evidence(
                [*outcome.entities, *outcome.recommendations],
                [*plan.required_concepts, *plan.preferred_concepts],
            ),
        )
        trace.extend(
            [
                {
                    "step": "typed_retrieval",
                    "status": "skipped" if retrieval_blocked else "ok",
                    "target_count": len(outcome.targets),
                    "entity_count": len(outcome.entities),
                    "recommendation_count": len(outcome.recommendations),
                    "fact_count": len(outcome.facts),
                    "retrieval_strategy": outcome.retrieval_strategy,
                    "elapsed_ms": _elapsed_ms(started),
                },
                {"step": "total", "status": "ok", "elapsed_ms": _elapsed_ms(started)},
            ]
        )
        return V5QueryResponse(
            answer_type=_ANSWER_TYPES[plan.intent],
            intent=plan.intent,
            query_plan=plan,
            targets=outcome.targets,
            entities=outcome.entities,
            facts=outcome.facts,
            recommendations=outcome.recommendations,
            evidence=evidence,
            matched_paths=outcome.matched_paths,
            constraint_results=outcome.constraint_results,
            required_tools=plan.required_tools,
            missing_fields=missing_fields,
            error=_planner_error(failure),
            trace=trace,
            manifest=asdict(kb_version_manifests()["v5"]),
        )

    def _link_concepts(
        self,
        plan: V5QueryPlan,
        vocabulary: list[str],
        query: str,
    ) -> tuple[V5QueryPlan, ConceptLinkBatch, ConceptLinkBatch]:
        linker = ConceptLinker(self.store, self.gemini)
        required = linker.link(plan.required_concepts, vocabulary, user_query=query)
        preferred = linker.link(plan.preferred_concepts, vocabulary, user_query=query)
        linked_plan = plan.model_copy(
            update={
                "required_concepts": required.resolved,
                "preferred_concepts": preferred.resolved,
            }
        )
        return linked_plan, required, preferred

    def _execute_plan(
        self,
        plan: V5QueryPlan,
        query: str,
        top_k: int,
    ) -> _RetrievalOutcome:
        outcome = _RetrievalOutcome()
        if plan.intent in {V5Intent.LOOKUP, V5Intent.PROFILE, V5Intent.COMPARE}:
            for target in plan.targets:
                resolved = self.resolver.resolve(target, plan.limit, plan.geo_scope.cities)
                outcome.targets.extend(resolved)
                if not resolved:
                    outcome.missing_fields.append(
                        f"not_found:{target.kind.value}:{target.value}"
                    )
                    continue
                if target.kind == TargetKind.PLACE:
                    place_entities, place_facts = self._lookup_v4(
                        resolved[0].name,
                        plan.requested_fields,
                        target.entity_types,
                        plan.required_concepts,
                        city=_single_city(plan.geo_scope.cities),
                    )
                    outcome.entities.extend(place_entities)
                    outcome.facts.extend(place_facts)
        elif plan.intent == V5Intent.AGGREGATE:
            city = _single_city(plan.geo_scope.cities)
            for entity_type in _place_types(plan.targets):
                outcome.facts.append(self._count_fact(city, [entity_type]))
        elif plan.intent == V5Intent.SUMMARIZE:
            for target in plan.targets:
                resolved = self.resolver.resolve(target, plan.limit, plan.geo_scope.cities)
                outcome.targets.extend(resolved)
                if target.kind in {TargetKind.CITY, TargetKind.GEO_AREA} and resolved:
                    outcome.entities.extend(self._geo_places(resolved[0], plan.limit))
        elif any(target.kind != TargetKind.PLACE for target in plan.targets):
            for target in plan.targets:
                resolved = self.resolver.resolve(
                    target,
                    min(top_k, plan.limit),
                    plan.geo_scope.cities,
                )
                outcome.targets.extend(resolved)
                if target.kind in {TargetKind.CITY, TargetKind.GEO_AREA} and resolved:
                    outcome.recommendations.extend(
                        self._geo_places(resolved[0], min(top_k, plan.limit))
                    )
        else:
            limit = min(top_k, plan.limit)
            entity_types = _place_types(plan.targets)
            candidates, outcome.constraint_results = self._path_candidates(
                _single_city(plan.geo_scope.cities),
                entity_types,
                plan.required_concepts,
                plan.preferred_concepts,
                plan.constraints,
                max(limit * POLICY.candidate_multiplier, POLICY.minimum_candidate_pool),
            )
            query_embedding = (
                self._query_embedding(query)
                if not candidates and not plan.constraints
                else None
            )
            if query_embedding is not None:
                outcome.recommendations = self._semantic_place_candidates(
                    query_embedding,
                    _single_city(plan.geo_scope.cities),
                    entity_types,
                    limit,
                )
                outcome.retrieval_strategy = "semantic_place_fallback"
            else:
                outcome.recommendations = self._rank_candidates(
                    query,
                    candidates,
                    limit,
                    entity_types,
                    plan.ranking_criteria,
                )
            outcome.matched_paths = self._matched_paths(
                outcome.recommendations,
                [*plan.required_concepts, *plan.preferred_concepts],
            )
        return outcome

    def _geo_places(self, target: TargetResult, limit: int) -> list[EntityResult]:
        if target.kind == TargetKind.CITY:
            rows = self.store.run_versioned(
                """
                MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(scope:City {id: $target_id})
                RETURN place ORDER BY coalesce(place.rating, 0) DESC LIMIT $limit
                """,
                target_id=target.target_id,
                limit=limit,
            )
        else:
            rows = self.store.run_versioned(
                """
                MATCH (scope:GeoArea {id: $target_id, kb_version: $kb_version})
                CALL (scope) {
                  MATCH (place:Place {kb_version: $kb_version})-[:LOCATED_IN]->(scope)
                  RETURN place, 1.0 AS scope_score
                  UNION
                  MATCH (place:Place {kb_version: $kb_version})<-[:MENTIONS]-(unit:TextUnit)
                        -[:MENTIONS_GEO_AREA]->(scope)
                  RETURN place, 0.6 AS scope_score
                }
                WITH place, max(scope_score) AS score
                RETURN place {.*, score: score} AS place
                ORDER BY score DESC, coalesce(place.rating, 0) DESC
                LIMIT $limit
                """,
                target_id=target.target_id,
                limit=limit,
            )
        return [_entity(row["place"]) for row in rows]

    def _semantic_place_candidates(
        self,
        query_embedding: list[float],
        city: str | None,
        entity_types: list[str],
        limit: int,
    ) -> list[EntityResult]:
        rows = self.store.run_versioned(
            """
            CALL db.index.vector.queryNodes('v5_place_embedding', $candidate_limit, $embedding)
            YIELD node AS place, score AS vector_score
            WHERE place.kb_version = $kb_version
              AND ($city IS NULL OR place.city = $city)
              AND ($entity_types = [] OR place.entity_type IN $entity_types)
            WITH place, vector_score,
                 CASE
                   WHEN place.rating IS NULL THEN 0.0
                   WHEN place.rating > 5 THEN place.rating / 10.0
                   ELSE place.rating / 5.0
                 END AS rating_score
            RETURN place {.*, score: 0.85 * vector_score + 0.15 * rating_score} AS place
            ORDER BY place.score DESC
            LIMIT $limit
            """,
            candidate_limit=max(limit * POLICY.candidate_multiplier, POLICY.minimum_vector_pool),
            embedding=query_embedding,
            city=city,
            entity_types=entity_types,
            limit=limit,
        )
        return [_entity(row["place"]) for row in rows]

    def _target_evidence(self, targets: list[TargetResult]) -> list[V4EvidenceResult]:
        rows = [
            {"target_id": target.target_id, "evidence_id": evidence_id}
            for target in targets
            for evidence_id in target.evidence_ids
        ]
        if not rows:
            return []
        evidence = self.store.run_versioned(
            """
            UNWIND $rows AS row
            MATCH (unit:TextUnit {id: row.evidence_id, kb_version: $kb_version})
                  -[:PART_OF]->(document:Document)
            RETURN DISTINCT row.target_id AS subject_id,
                   unit.id AS text_unit_id,
                   document.id AS document_id,
                   coalesce(unit.title, document.title) AS title,
                   unit.text AS text,
                   document.url AS url,
                   document.source_name AS source_name
            """,
            rows=rows,
        )
        return [V4EvidenceResult.model_validate(row) for row in evidence]

    def _place_evidence(
        self,
        places: list[EntityResult],
    ) -> list[V4EvidenceResult]:
        if not places:
            return []
        rows = self.store.run_versioned(
            """
            UNWIND $place_ids AS place_id
            MATCH (unit:TextUnit {kb_version: $kb_version})-[:MENTIONS]->
                  (place:Place {id: place_id, kb_version: $kb_version})
            MATCH (unit)-[:PART_OF]->(document:Document)
            WITH place, unit, document
            ORDER BY CASE WHEN unit.evidence_origin = 'verified_place_record' THEN 0 ELSE 1 END,
                     unit.sequence
            WITH place, head(collect({unit: unit, document: document})) AS selected
            RETURN place.id AS subject_id,
                   selected.unit.id AS text_unit_id,
                   selected.document.id AS document_id,
                   coalesce(selected.unit.title, selected.document.title, place.name) AS title,
                   selected.unit.text AS text,
                   selected.document.url AS url,
                   selected.document.source_name AS source_name
            """,
            place_ids=[place.place_id for place in places],
        )
        return [V4EvidenceResult.model_validate(row) for row in rows]

    @staticmethod
    def _merge_evidence(
        *groups: list[V4EvidenceResult],
    ) -> list[V4EvidenceResult]:
        unique = {
            (item.subject_id, item.text_unit_id): item
            for group in groups
            for item in group
        }
        return list(unique.values())


def _single_city(cities: list[str]) -> str | None:
    return cities[0] if len(cities) == 1 else None


def _place_types(targets: list[QueryTarget]) -> list[str]:
    return list(dict.fromkeys(
        entity_type
        for target in targets
        if target.kind == TargetKind.PLACE
        for entity_type in target.entity_types
    ))


def _planner_error(failure: PlannerFailure | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    message = (
        "The V5 query plan did not satisfy the graph contract."
        if failure.code == "invalid_plan"
        else "The V5 query planner is temporarily unavailable."
    )
    return {
        "code": failure.code,
        "message": message,
        "retryable": failure.retryable,
        "reason": failure.reason,
    }


def _concept_link_trace(
    required: ConceptLinkBatch,
    preferred: ConceptLinkBatch,
) -> dict[str, Any]:
    links = [*required.links, *preferred.links]
    if required.unresolved:
        status = "blocked"
    elif preferred.unresolved:
        status = "partial"
    else:
        status = "ok"
    return {
        "step": "concept_linking",
        "status": status,
        "resolved": [*required.resolved, *preferred.resolved],
        "unresolved": [*required.unresolved, *preferred.unresolved],
        "links": [link.model_dump() for link in links],
    }
