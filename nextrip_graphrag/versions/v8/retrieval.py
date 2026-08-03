from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from functools import cached_property
from typing import Any

from neo4j import Record
from neo4j_graphrag.retrievers import HybridCypherRetriever
from neo4j_graphrag.retrievers.hybrid import HybridSearchRanker
from neo4j_graphrag.types import RetrieverResultItem

from ...config import DEFAULT_TYPED_QUERY_TOP_K
from ...normalizer import slugify
from ..v2.retrieval import _entity, _fulltext_query
from ..v2.schemas import EntityResult, FactResult
from ..v4.policy import POLICY
from ..v4.schemas import V4EvidenceResult
from ..v4.schemas import RankingCriterion
from ..v6.context import (
    ResolvedTurn,
    duration_days_from_query,
    is_itinerary_request,
)
from ..v6.retrieval import V6RetrievalService
from ..v6.schemas import ConversationContext
from ..v5.retrieval import _RetrievalOutcome
from ..v5.schemas import (
    GeoScope,
    QueryTarget,
    QueryTask,
    TargetKind,
    V5Intent,
    V5QueryPlan,
    V5QueryResponse,
)
from ..v7.entity_linker import SemanticEntityLinker
from ..v7.query_planner import _keep_material_clarification
from .query_planner import plan_query
from .schemas import RouteContext, RouteEndpoint, V8QueryPlan, V8QueryResponse


_PERSONALIZATION: ContextVar[dict[str, Any] | None] = ContextVar(
    "v8_personalization",
    default=None,
)
_EXPLICIT_CONCEPTS: ContextVar[frozenset[str]] = ContextVar(
    "v8_explicit_concepts",
    default=frozenset(),
)


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

    def query(
        self,
        query: str,
        top_k: int = DEFAULT_TYPED_QUERY_TOP_K,
        context: ConversationContext | None = None,
    ) -> V8QueryResponse:
        personalization_token = _PERSONALIZATION.set(
            dict((context or ConversationContext()).personalization)
        )
        concepts_token = _EXPLICIT_CONCEPTS.set(frozenset())
        try:
            response = super().query(query, top_k=top_k, context=context)
        finally:
            _PERSONALIZATION.reset(personalization_token)
            _EXPLICIT_CONCEPTS.reset(concepts_token)
        recovery = next(
            (
                item
                for item in reversed(response.trace)
                if item.get("step") == "v8_scope_recovery"
                and item.get("status") == "corrected"
            ),
            None,
        )
        if recovery is None:
            return self._attach_route_context(response)

        resolved_city = str(recovery["resolved_city"])
        source_cities = [str(city) for city in recovery["source_cities"]]
        warning = f"scope_corrected:{','.join(source_cities)}:{resolved_city}"
        payload = response.model_dump(mode="python")
        payload["query_plan"]["geo_scope"]["cities"] = [resolved_city]
        payload["conversation_context"].update(
            {
                "cities": [resolved_city],
                "city_source": "entity_grounding",
            }
        )
        payload["warnings"] = list(dict.fromkeys([*response.warnings, warning]))
        return self._attach_route_context(
            self.response_model.model_validate(payload)
        )

    def _attach_route_context(self, response: V8QueryResponse) -> V8QueryResponse:
        if "route" not in getattr(response, "required_tools", []):
            return response
        resolved = []
        seen_ids: set[str] = set()
        for target in response.query_plan.targets:
            if target.kind != TargetKind.PLACE or not target.value:
                continue
            matches = self.resolver.resolve(
                target,
                1,
                response.query_plan.geo_scope.cities,
            )
            if not matches or matches[0].target_id in seen_ids:
                continue
            seen_ids.add(matches[0].target_id)
            resolved.append(matches[0])
            if len(resolved) == 2:
                break
        if not resolved:
            return response
        rows = self.store.run_versioned(
            """
            UNWIND $place_ids AS place_id
            MATCH (place:Place {id: place_id, kb_version: $kb_version})
            WHERE place.location IS NOT NULL
            RETURN place.id AS place_id,
                   place.name AS name,
                   place.location.latitude AS latitude,
                   place.location.longitude AS longitude
            """,
            place_ids=[item.target_id for item in resolved],
        )
        by_id = {str(row["place_id"]): row for row in rows}
        endpoints = [
            RouteEndpoint(
                place_id=item.target_id,
                name=item.name,
                latitude=float(by_id[item.target_id]["latitude"]),
                longitude=float(by_id[item.target_id]["longitude"]),
            )
            for item in resolved
            if item.target_id in by_id
        ]
        if not endpoints:
            return response
        return response.model_copy(
            update={
                "route_context": RouteContext(
                    endpoints=endpoints,
                    options=response.query_plan.route_options,
                )
            }
        )

    def _place_distance(self, origin, destination):
        """V8 never exposes geodesic distance as a road-route fact."""
        del origin, destination
        return None, None

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
        if not (
            context.turn_count
            or context.cities
            or context.city_source
            or context.resolved_query
        ):
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
        return is_itinerary_request(resolved.query)

    def _final_itinerary_request(
        self,
        resolved: ResolvedTurn,
        response: V5QueryResponse,
        initially_itinerary: bool,
    ) -> bool:
        del resolved
        return initially_itinerary or response.intent == V5Intent.PLAN_CANDIDATES

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
        retrieval_query = self._personalized_retrieval_query(query)
        tasks = _effective_tasks(plan)
        if not tasks:
            outcome = super()._execute_plan(plan, retrieval_query, top_k)
            retry_plan = _scope_recovery_plan(plan, outcome)
            if retry_plan is None:
                return self._personalize_outcome(outcome)
            recovered = super()._execute_plan(retry_plan, retrieval_query, top_k)
            resolved_city = _resolved_city(recovered)
            if resolved_city is None or _has_not_found(recovered):
                return self._personalize_outcome(outcome)
            recovered.trace.append(
                {
                    "step": "v8_scope_recovery",
                    "status": "corrected",
                    "source_cities": plan.geo_scope.cities,
                    "resolved_city": resolved_city,
                }
            )
            recovered.retrieval_strategy = "v8_corrective_global_entity_lookup"
            return self._personalize_outcome(recovered)

        merged = _RetrievalOutcome()
        task_trace: list[dict[str, Any]] = []
        for task in tasks:
            task_scope = task.geo_scope
            if not (task_scope.cities or task_scope.areas or task_scope.near_entities):
                task_scope = plan.geo_scope
            task_plan = plan.model_copy(
                update={
                    "intent": task.intent,
                    "targets": task.targets,
                    "geo_scope": task_scope,
                    "required_concepts": (
                        task.required_concepts or plan.required_concepts
                    ),
                    "preferred_concepts": (
                        task.preferred_concepts or plan.preferred_concepts
                    ),
                    "ranking_criteria": (
                        task.ranking_criteria or plan.ranking_criteria
                    ),
                    "constraints": task.constraints or plan.constraints,
                    "limit": task.limit,
                    "tasks": [],
                }
            )
            outcome = super()._execute_plan(task_plan, retrieval_query, top_k)
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
        return self._personalize_outcome(merged)

    def _personalized_retrieval_query(self, query: str) -> str:
        personalization = _PERSONALIZATION.get() or {}
        explicit = _EXPLICIT_CONCEPTS.get()
        implicit_preferences = [
            str(value).strip()
            for value in personalization.get("preferred_concepts") or []
            if str(value).strip() and slugify(str(value)) not in explicit
        ]
        if not implicit_preferences:
            return query
        signals = ", ".join(dict.fromkeys(implicit_preferences))
        return f"{query}\nUser preference signals: {signals}"

    def _plan_query(
        self,
        query: str,
        catalog: dict[str, list[str]],
    ) -> tuple[V5QueryPlan, str, Any | None]:
        plan, planner, failure = plan_query(query, self.gemini, catalog)
        if failure is None:
            plan = self._personalize_plan(plan)
        if is_itinerary_request(query):
            if failure is not None:
                return (
                    _recover_itinerary_plan(query, catalog),
                    "deterministic_itinerary_recovery_v8",
                    None,
                )
            plan = _itinerary_candidate_plan(plan)
        return plan, planner, failure

    def _personalize_plan(self, plan: V5QueryPlan) -> V5QueryPlan:
        personalization = _PERSONALIZATION.get() or {}
        if not personalization:
            return plan
        _EXPLICIT_CONCEPTS.set(
            frozenset(
                slugify(value)
                for value in [*plan.required_concepts, *plan.preferred_concepts]
            )
        )
        preferred = [
            str(value).strip()
            for value in personalization.get("preferred_concepts") or []
            if str(value).strip()
        ]
        rankings = list(plan.ranking_criteria)
        budget = personalization.get("budget_level")
        if budget == "budget":
            rankings.append(RankingCriterion.PRICE_LOW)
        elif budget == "premium":
            rankings.append(RankingCriterion.RATING)
        return plan.model_copy(
            update={
                "preferred_concepts": list(
                    dict.fromkeys([*plan.preferred_concepts, *preferred])
                ),
                "ranking_criteria": list(dict.fromkeys(rankings)),
            }
        )

    def _personalize_outcome(
        self,
        outcome: _RetrievalOutcome,
    ) -> _RetrievalOutcome:
        personalization = _PERSONALIZATION.get() or {}
        excluded = {
            slugify(str(value))
            for value in personalization.get("excluded_concepts") or []
            if str(value).strip()
        }
        effective = sorted(excluded - _EXPLICIT_CONCEPTS.get())
        candidates = outcome.recommendations or outcome.entities
        if not effective or not candidates:
            if personalization:
                outcome.trace.append(
                    {
                        "step": "v8_personalization",
                        "status": "ok",
                        "profile_revision": personalization.get("profile_revision"),
                        "preferred_concepts": list(
                            personalization.get("preferred_concepts") or []
                        ),
                        "excluded_concepts": effective,
                    }
                )
            return outcome
        kept_ids = self._places_without_concepts(
            [item.place_id for item in candidates],
            effective,
        )
        outcome.recommendations = [
            item for item in outcome.recommendations if item.place_id in kept_ids
        ]
        outcome.entities = [
            item for item in outcome.entities if item.place_id in kept_ids
        ]
        outcome.trace.append(
            {
                "step": "v8_personalization",
                "status": "ok",
                "profile_revision": personalization.get("profile_revision"),
                "preferred_concepts": list(
                    personalization.get("preferred_concepts") or []
                ),
                "excluded_concepts": effective,
                "remaining_count": len(outcome.recommendations or outcome.entities),
            }
        )
        return outcome

    def _places_without_concepts(
        self,
        place_ids: list[str],
        excluded_concepts: list[str],
    ) -> set[str]:
        rows = self.store.run_versioned(
            """
            UNWIND $place_ids AS place_id
            MATCH (place:Place {id: place_id, kb_version: $kb_version})
            WHERE NOT EXISTS {
              MATCH (place)-[:HAS_OFFERING*0..1]->(subject)-[]->(concept:Concept)
              WHERE concept.kb_version = $kb_version
                AND any(
                  excluded IN $excluded_concepts
                  WHERE toLower(coalesce(concept.slug, '')) = excluded
                     OR toLower(coalesce(concept.canonical_name, '')) CONTAINS excluded
                     OR toLower(coalesce(concept.name, '')) CONTAINS excluded
                )
            }
            RETURN place.id AS place_id
            """,
            place_ids=place_ids,
            excluded_concepts=excluded_concepts,
        )
        return {str(row["place_id"]) for row in rows}

    def _ground_plan(
        self,
        plan: V5QueryPlan,
        catalog: dict[str, list[str]],
        query: str,
    ) -> tuple[V5QueryPlan, list[str], bool, list[dict[str, Any]]]:
        target_terms = {
            slugify(target.value)
            for target in plan.targets
            if target.kind == TargetKind.PLACE and target.value
        }
        near_terms = {slugify(value) for value in plan.geo_scope.near_entities if value}
        near_terms.update(
            slugify(str(constraint.value))
            for constraint in plan.constraints
            if constraint.field == "near_subject" and constraint.value
        )
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
                    "clarification_needed": _keep_material_clarification(grounded_plan),
                }
            )
        grounded_plan, semantic_missing, descriptive_recovery = (
            _recover_descriptive_venue_query(
                plan,
                grounded_plan,
                list(result.missing_fields),
            )
        )
        blocking_missing = [
            item for item in semantic_missing if not item.startswith("concept:")
        ]
        if _requires_city_scope(grounded_plan):
            blocking_missing.append("unresolved:city:query_scope")
        public_missing = _public_missing_fields(
            blocking_missing,
            target_terms=target_terms,
            near_terms=near_terms,
        )
        if any(item.startswith("not_found:entity:") for item in public_missing):
            plan_payload = grounded_plan.model_dump(mode="python")
            plan_payload["targets"] = [
                target.model_dump(mode="python")
                for target in _restore_unresolved_named_targets(
                    plan,
                    grounded_plan,
                )
            ]
            plan_payload["clarification_needed"] = False
            grounded_plan = type(grounded_plan).model_validate(plan_payload)
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
            "descriptive_query_recovery": descriptive_recovery,
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


def _public_missing_fields(
    diagnostics: list[str],
    *,
    target_terms: set[str] | None = None,
    near_terms: set[str] | None = None,
) -> list[str]:
    targets = target_terms or set()
    anchors = near_terms or set()
    fields: list[str] = []
    for item in diagnostics:
        if item.startswith("unresolved:city:"):
            fields.append("city")
        elif item.startswith("unresolved:geo_area:"):
            fields.append("geo_area")
        elif item.startswith("unresolved:place:"):
            term = item.split(":", 2)[2]
            normalized = slugify(term)
            if (not targets and not anchors) or (
                normalized in anchors and normalized not in targets
            ):
                fields.append("near_reference")
            else:
                fields.append(f"not_found:entity:{term}")
        elif item.startswith("unresolved:"):
            _, kind, _ = item.split(":", 2)
            fields.append(kind)
        else:
            fields.append(item)
    return list(dict.fromkeys(fields))


def _scope_recovery_plan(
    plan: V5QueryPlan,
    outcome: _RetrievalOutcome,
) -> V5QueryPlan | None:
    if plan.intent not in {V5Intent.LOOKUP, V5Intent.PROFILE, V5Intent.COMPARE}:
        return None
    if not plan.geo_scope.cities or not _has_not_found(outcome):
        return None
    if not any(
        target.kind == TargetKind.PLACE and target.value for target in plan.targets
    ):
        return None
    plan_payload = plan.model_dump(mode="python")
    plan_payload["geo_scope"]["cities"] = []
    return type(plan).model_validate(plan_payload)


def _restore_unresolved_named_targets(
    original: V5QueryPlan,
    grounded: V5QueryPlan,
) -> list[QueryTarget]:
    return [
        QueryTarget(
            kind=linked.kind,
            value=raw.value,
            entity_types=linked.entity_types,
        )
        if linked.kind == TargetKind.PLACE and not linked.value and raw.value
        else linked
        for raw, linked in zip(original.targets, grounded.targets, strict=True)
    ]


def _recover_descriptive_venue_query(
    original: V5QueryPlan,
    grounded: V5QueryPlan,
    missing_fields: list[str],
) -> tuple[V5QueryPlan, list[str], bool]:
    """Recover a descriptive recommendation misclassified as a proper name.

    The semantic planner must already provide a venue type plus a preference,
    ranking criterion, or enforceable constraint. A plain unknown proper name
    therefore remains a closed-world not-found result.
    """

    if original.intent not in {V5Intent.LOOKUP, V5Intent.PROFILE}:
        return grounded, missing_fields, False
    named_places = [
        target
        for target in original.targets
        if target.kind == TargetKind.PLACE and target.value and target.entity_types
    ]
    if len(named_places) != 1 or original.requested_fields:
        return grounded, missing_fields, False
    has_preference_signal = bool(
        original.required_concepts
        or original.preferred_concepts
        or original.ranking_criteria
        or original.constraints
    )
    if not has_preference_signal:
        return grounded, missing_fields, False
    target = named_places[0]
    target_slug = slugify(target.value or "")
    unresolved_prefix = "unresolved:place:"
    unresolved_target = any(
        item.startswith(unresolved_prefix)
        and slugify(item.removeprefix(unresolved_prefix)) == target_slug
        for item in missing_fields
    )
    if not unresolved_target:
        return grounded, missing_fields, False
    recovered = grounded.model_copy(
        update={
            "intent": V5Intent.RECOMMEND,
            "targets": [
                QueryTarget(
                    kind=TargetKind.PLACE,
                    entity_types=target.entity_types,
                )
            ],
            "clarification_needed": False,
        }
    )
    remaining = [
        item
        for item in missing_fields
        if not (
            item.startswith(unresolved_prefix)
            and slugify(item.removeprefix(unresolved_prefix)) == target_slug
        )
    ]
    return recovered, remaining, True


def _has_not_found(outcome: _RetrievalOutcome) -> bool:
    return any(item.startswith("not_found:") for item in outcome.missing_fields)


def _resolved_city(outcome: _RetrievalOutcome) -> str | None:
    for item in [*outcome.entities, *outcome.recommendations]:
        if item.city:
            return item.city
    return None


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
    # Broad recommendation/list requests need an explicit user-selected scope
    # even when the semantic planner did not infer a venue type. Named-place
    # lookups use different intents and remain eligible for global grounding.
    return True


def _effective_tasks(plan: V5QueryPlan) -> list[QueryTask]:
    if plan.tasks:
        return plan.tasks
    if plan.intent != V5Intent.PLAN_CANDIDATES:
        return []
    place_targets = [target for target in plan.targets if target.entity_types]
    entity_types = list(
        dict.fromkeys(
            entity_type
            for target in place_targets
            for entity_type in target.entity_types
        )
    )
    if len(entity_types) <= 1:
        return []
    candidate_limits = {
        "attraction": 20,
        "restaurant": 6,
        "cafe": 6,
        "hotel": 6,
        "nightlife": 6,
    }
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
            limit=max(candidate_limits.get(entity_type, 6), plan.limit),
        )
        for entity_type in entity_types
    ]


def _itinerary_candidate_plan(plan: V5QueryPlan) -> V5QueryPlan:
    """Compile itinerary retrieval into balanced, city-scoped graph tasks.

    The LLM still extracts city, duration and preferences. This policy controls
    the ontology roles required by the downstream Planning Agent and prevents a
    natural-language phrase such as "lộ trình đi chơi" becoming an entity lookup.
    """
    payload = plan.model_dump(mode="python")
    payload.update(
        {
            "intent": V5Intent.PLAN_CANDIDATES,
            "targets": [
                QueryTarget(
                    kind=TargetKind.PLACE,
                    entity_types=[
                        "attraction",
                        "restaurant",
                        "cafe",
                        "hotel",
                    ],
                ).model_dump(mode="python")
            ],
            "tasks": [],
            "clarification_needed": False,
        }
    )
    return type(plan).model_validate(payload)


def _recover_itinerary_plan(
    query: str,
    catalog: dict[str, list[str]],
) -> V8QueryPlan:
    """Recover only structural fields when the semantic planner is invalid.

    Cities come from the live graph catalog and venue candidates still come
    from Neo4j. This is a reliability boundary, not an answer generator.
    """

    query_slug = f"-{slugify(query)}-"
    matching_cities = [
        city for city in catalog.get("cities", []) if f"-{slugify(city)}-" in query_slug
    ]
    city = max(matching_cities, key=lambda value: len(slugify(value)), default=None)
    return V8QueryPlan(
        intent=V5Intent.PLAN_CANDIDATES,
        targets=[
            QueryTarget(
                kind=TargetKind.PLACE,
                entity_types=["attraction", "restaurant", "cafe", "hotel"],
            )
        ],
        geo_scope=GeoScope(cities=[city] if city else []),
        duration_days=duration_days_from_query(query) or 1,
        clarification_needed=False,
        confidence=1,
    )


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
