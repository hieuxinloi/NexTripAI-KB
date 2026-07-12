from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Any

from ..registry import kb_version_manifests
from ..v2.retrieval import _elapsed_ms, _entity
from ..v2.schemas import EntityResult, FactResult, QueryIntent
from ..v4.policy import POLICY
from ..v4.retrieval import V4RetrievalService
from ..v4.schemas import V4EvidenceResult
from .graph_store import V5GraphStore
from .query_planner import plan_query
from .resolver import V5EntityResolver
from .schemas import QueryTarget, TargetKind, TargetResult, V5Intent, V5QueryResponse


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


class V5RetrievalService(V4RetrievalService):
    kb_version = "v5"
    fulltext_index = "v5_place_fulltext"
    vector_index = "v5_place_embedding"

    def __init__(self, store: V5GraphStore, gemini: Any | None = None):
        super().__init__(store, gemini)
        self.resolver = V5EntityResolver(store)

    def query(self, query: str, top_k: int = 5) -> V5QueryResponse:
        self._ensure_ready()
        started = perf_counter()
        plan, planner, failure = plan_query(query, self.gemini, self.store.planner_catalog())
        trace: list[dict[str, Any]] = [{
            "step": "query_planner",
            "status": "unavailable" if failure else "ok",
            "planner": planner,
            "failure_reason": failure.reason if failure else None,
            "failure_code": failure.code if failure else None,
            "elapsed_ms": _elapsed_ms(started),
        }]
        targets: list[TargetResult] = []
        entities: list[EntityResult] = []
        facts: list[FactResult] = []
        recommendations: list[EntityResult] = []
        matched_paths = []
        constraint_results = []
        missing_fields: list[str] = []

        if plan.clarification_needed:
            missing_fields.append("query_constraints")
        elif plan.intent == V5Intent.UNSUPPORTED:
            pass
        elif plan.intent == V5Intent.TOOL_REQUIRED:
            pass
        elif plan.intent in {V5Intent.LOOKUP, V5Intent.PROFILE, V5Intent.COMPARE}:
            for target in plan.targets:
                resolved = self.resolver.resolve(target, plan.limit, plan.geo_scope.cities)
                targets.extend(resolved)
                if not resolved:
                    missing_fields.append(f"not_found:{target.kind.value}:{target.value}")
                    continue
                if target.kind == TargetKind.PLACE:
                    place_entities, place_facts = self._lookup_v4(
                        resolved[0].name,
                        plan.requested_fields,
                        target.entity_types,
                        plan.required_concepts,
                        city=_single_city(plan.geo_scope.cities),
                    )
                    entities.extend(place_entities)
                    facts.extend(place_facts)
        elif plan.intent == V5Intent.AGGREGATE:
            city = _single_city(plan.geo_scope.cities)
            for entity_type in _place_types(plan.targets):
                facts.append(self._count_fact(city, [entity_type]))
        elif plan.intent == V5Intent.SUMMARIZE:
            for target in plan.targets:
                resolved = self.resolver.resolve(target, plan.limit, plan.geo_scope.cities)
                targets.extend(resolved)
                if target.kind in {TargetKind.CITY, TargetKind.GEO_AREA} and resolved:
                    entities.extend(self._geo_places(resolved[0], plan.limit))
        elif any(target.kind != TargetKind.PLACE for target in plan.targets):
            for target in plan.targets:
                targets.extend(
                    self.resolver.resolve(
                        target,
                        min(top_k, plan.limit),
                        plan.geo_scope.cities,
                    )
                )
        else:
            limit = min(top_k, plan.limit)
            entity_types = _place_types(plan.targets)
            candidates, constraint_results = self._path_candidates(
                _single_city(plan.geo_scope.cities),
                entity_types,
                plan.required_concepts,
                plan.preferred_concepts,
                plan.constraints,
                max(limit * POLICY.candidate_multiplier, POLICY.minimum_candidate_pool),
            )
            recommendations = self._rank_candidates(
                query,
                candidates,
                limit,
                entity_types,
                plan.ranking_criteria,
            )
            matched_paths = self._matched_paths(
                recommendations,
                [*plan.required_concepts, *plan.preferred_concepts],
            )

        evidence = self._merge_evidence(
            self._target_evidence(targets),
            self._claim_evidence(
                [*entities, *recommendations],
                [*plan.required_concepts, *plan.preferred_concepts],
            ),
        )
        trace.extend([
            {
                "step": "typed_retrieval",
                "status": "skipped" if failure else "ok",
                "target_count": len(targets),
                "entity_count": len(entities),
                "recommendation_count": len(recommendations),
                "fact_count": len(facts),
                "elapsed_ms": _elapsed_ms(started),
            },
            {"step": "total", "status": "ok", "elapsed_ms": _elapsed_ms(started)},
        ])
        return V5QueryResponse(
            answer_type=_ANSWER_TYPES[plan.intent],
            intent=plan.intent,
            query_plan=plan,
            targets=targets,
            entities=entities,
            facts=facts,
            recommendations=recommendations,
            evidence=evidence,
            matched_paths=matched_paths,
            constraint_results=constraint_results,
            required_tools=plan.required_tools,
            missing_fields=missing_fields,
            error=(
                {
                    "code": failure.code,
                    "message": (
                        "The V5 query plan did not satisfy the graph contract."
                        if failure.code == "invalid_plan"
                        else "The V5 query planner is temporarily unavailable."
                    ),
                    "retryable": failure.retryable,
                    "reason": failure.reason,
                }
                if failure
                else None
            ),
            trace=trace,
            manifest=asdict(kb_version_manifests()["v5"]),
        )

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
