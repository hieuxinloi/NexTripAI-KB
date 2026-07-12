from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Any

from ...normalizer import slugify
from ..registry import kb_version_manifests
from ..v2.retrieval import _elapsed_ms, _entity
from ..v2.schemas import EntityResult, FactResult, QueryIntent
from ..v3.retrieval import V3RetrievalService
from .graph_store import V4GraphStore
from .query_planner import plan_query
from .policy import POLICY
from .schemas import (
    ConstraintMode,
    ConstraintResult,
    MatchedPath,
    RetrievalMode,
    V4Constraint,
    V4EvidenceResult,
    V4QueryResponse,
)


class V4RetrievalService(V3RetrievalService):
    kb_version = "v4"
    fulltext_index = "v4_place_fulltext"
    vector_index = "v4_place_embedding"

    def __init__(self, store: V4GraphStore, gemini: Any | None = None):
        super().__init__(store, gemini)

    def query(self, query: str, top_k: int = 5) -> V4QueryResponse:
        self._ensure_ready()
        started = perf_counter()
        plan, planner, fallback_reason = plan_query(
            query,
            self.gemini,
            self.store.planner_vocabulary(),
        )
        trace: list[dict[str, Any]] = [{
            "step": "query_planner",
            "status": "ok",
            "planner": planner,
            "fallback_reason": fallback_reason,
            "retrieval_mode": plan.retrieval_mode.value,
            "elapsed_ms": _elapsed_ms(started),
        }]
        entities: list[EntityResult] = []
        facts: list[FactResult] = []
        recommendations: list[EntityResult] = []
        matched_paths: list[MatchedPath] = []
        constraint_results: list[ConstraintResult] = []
        required_tools: list[str] = []
        missing_fields: list[str] = []

        if plan.clarification_needed:
            missing_fields.append("city" if plan.city is None else "query_constraints")
        elif plan.retrieval_mode == RetrievalMode.DYNAMIC_SEARCH:
            required_tools = _required_tools(query)
        elif plan.retrieval_mode == RetrievalMode.AGGREGATE:
            for entity_type in plan.entity_types:
                facts.append(self._count_fact(plan.city, [entity_type]))
        elif plan.retrieval_mode == RetrievalMode.ENTITY_LOOKUP:
            entities, facts = self._lookup_v4(
                plan.subjects[0],
                plan.predicates,
                plan.entity_types,
                plan.required_concepts,
                city=plan.city,
            )
            if not entities:
                missing_fields.append(f"not_found:entity:{plan.subjects[0]}")
            elif not facts:
                missing_fields.extend(f"fact:{predicate}" for predicate in plan.predicates)
        elif plan.retrieval_mode == RetrievalMode.COMMUNITY_SEARCH:
            entities = self._community_candidates(plan.city, plan.entity_types, min(top_k, plan.limit))
        elif plan.retrieval_mode == RetrievalMode.COMPARISON:
            for subject in plan.subjects:
                anchor = self._anchor(subject, entity_types=plan.entity_types, city=plan.city)
                if anchor:
                    entities.append(_entity(anchor))
        elif plan.retrieval_mode in {
            RetrievalMode.PATH_SEARCH,
            RetrievalMode.RECOMMENDATION,
            RetrievalMode.PLANNING_CANDIDATES,
        }:
            limit = min(top_k, plan.limit)
            candidates, constraint_results = self._path_candidates(
                plan.city,
                plan.entity_types,
                plan.required_concepts,
                plan.preferred_concepts,
                plan.constraints,
                max(
                    limit * POLICY.candidate_multiplier,
                    POLICY.minimum_candidate_pool,
                ),
            )
            ranked = self._rank_candidates(query, candidates, limit, plan.entity_types)
            retrieval_concepts = [*plan.required_concepts, *plan.preferred_concepts]
            matched_paths = self._matched_paths(ranked, retrieval_concepts)
            if plan.retrieval_mode == RetrievalMode.PATH_SEARCH and plan.intent == QueryIntent.ENTITY_LIST:
                entities = ranked
            else:
                recommendations = ranked

        evidence = self._claim_evidence(
            [*entities, *recommendations],
            [*plan.required_concepts, *plan.preferred_concepts],
        )
        trace.append({
            "step": "retrieval",
            "status": "ok",
            "retrieval_mode": plan.retrieval_mode.value,
            "entity_count": len(entities),
            "recommendation_count": len(recommendations),
            "fact_count": len(facts),
            "matched_path_count": len(matched_paths),
            "constraint_checks": len(constraint_results),
            "required_tools": required_tools,
            "elapsed_ms": _elapsed_ms(started),
        })
        trace.append({"step": "total", "status": "ok", "elapsed_ms": _elapsed_ms(started)})
        return V4QueryResponse(
            answer_type=plan.intent,
            query_plan=plan,
            entities=entities,
            facts=facts,
            recommendations=recommendations,
            evidence=evidence,
            matched_paths=matched_paths,
            constraint_results=constraint_results,
            required_tools=required_tools,
            missing_fields=missing_fields,
            trace=trace,
            manifest=asdict(kb_version_manifests()["v4"]),
        )

    def _lookup_v4(
        self,
        subject: str,
        predicates: list[str],
        entity_types: list[str],
        required_concepts: list[str],
        *,
        city: str | None = None,
    ) -> tuple[list[EntityResult], list[FactResult]]:
        entities, facts = self._lookup_v3(
            subject,
            predicates,
            entity_types,
            city=city,
        )
        if not entities or not required_concepts:
            return entities, facts
        claim_rows = self.store.run(
            """
            MATCH (place:Place {id: $place_id, kb_version: $kb_version})-[:HAS_OFFERING*0..1]->(subject)
            MATCH (claim:Claim {kb_version: $kb_version})-[:ABOUT]->(subject)
            MATCH (claim)-[:OBJECT]->(concept:Concept)
            OPTIONAL MATCH (claim)-[:SUPPORTED_BY]->(unit:TextUnit)
            WHERE any(term IN $terms WHERE toLower(concept.canonical_name) CONTAINS term)
            RETURN claim.predicate AS predicate,
                   concept.canonical_name AS concept,
                   collect(DISTINCT claim.polarity) AS polarities,
                   max(claim.confidence) AS confidence,
                   collect(DISTINCT unit.id) AS evidence_ids
            """,
            place_id=entities[0].place_id,
            terms=[_plain(term) for term in required_concepts],
            kb_version=self.kb_version,
        )
        if not claim_rows:
            return entities, facts
        resolved: list[FactResult] = []
        conflict_predicates: set[str] = set()
        for row in claim_rows:
            polarities = set(row["polarities"])
            if {"positive", "negative"} <= polarities:
                conflict_predicates.add(row["predicate"])
                resolved.append(
                    FactResult(
                        fact_id=f"conflict:{entities[0].place_id}:{row['predicate']}:{row['concept']}",
                        subject_id=entities[0].place_id,
                        predicate="data_conflict",
                        value=f"{row['predicate']}:{row['concept']}",
                        value_type="string",
                        confidence=0,
                        evidence_ids=row["evidence_ids"],
                    )
                )
            elif polarities == {"negative"}:
                conflict_predicates.add(row["predicate"])
                resolved.append(
                    FactResult(
                        fact_id=f"claim:{entities[0].place_id}:{row['predicate']}:{row['concept']}",
                        subject_id=entities[0].place_id,
                        predicate=row["predicate"].casefold(),
                        value=False,
                        value_type="boolean",
                        confidence=float(row["confidence"]),
                        evidence_ids=row["evidence_ids"],
                    )
                )
        if conflict_predicates:
            facts = [fact for fact in facts if fact.predicate not in {"amenities", "features"}]
            facts.extend(resolved)
        return entities, facts

    def _path_candidates(
        self,
        city: str | None,
        entity_types: list[str],
        required_concepts: list[str],
        preferred_concepts: list[str],
        constraints: list[V4Constraint],
        limit: int,
    ) -> tuple[list[EntityResult], list[ConstraintResult]]:
        required_terms = [_plain(term) for term in required_concepts]
        preferred_terms = [_plain(term) for term in preferred_concepts]
        all_terms = [*required_terms, *preferred_terms]
        clauses = [
            "place.kb_version = $kb_version",
            "($city IS NULL OR place.city = $city)",
            "($entity_types = [] OR place.entity_type IN $entity_types)",
            "all(term IN $required_terms WHERE EXISTS { MATCH (place)-[:HAS_OFFERING*0..1]->(subject)-[]->(concept:Concept) WHERE concept.kb_version = $kb_version AND (toLower(concept.canonical_name) CONTAINS term OR toLower(concept.name) CONTAINS term) })",
        ]
        params: dict[str, Any] = {
            "kb_version": self.kb_version,
            "city": city,
            "entity_types": entity_types,
            "required_terms": required_terms,
            "preferred_terms": preferred_terms,
            "all_terms": all_terms,
            "required_weight": POLICY.required_concept_weight,
            "preferred_weight": POLICY.preferred_concept_weight,
            "limit": limit,
        }
        near_subject = _constraint(constraints, "near_subject")
        near_match = ""
        distance_projection = ""
        if near_subject:
            anchor = self._anchor(str(near_subject.value))
            if not anchor:
                return [], []
            near_match = "MATCH (place)-[near:NEAR]->(:Place {id: $near_id})"
            clauses.append("near.distance_km IS NOT NULL")
            params["near_id"] = anchor["id"]
            distance_projection = ", distance_km: near.distance_km"
        star = _constraint(constraints, "star_rating")
        if star:
            clauses.append("place.star_rating = $star_rating")
            params["star_rating"] = int(star.value)
        indoor = _constraint(constraints, "indoor")
        if indoor:
            clauses.append("place.is_indoor = $indoor")
            params["indoor"] = bool(indoor.value)
        open_24h = _constraint(constraints, "open_24h")
        if open_24h:
            clauses.append(
                "((place.opening_hours_open IN ['00:00', '0:00'] "
                "AND place.opening_hours_close IN ['23:59', '24:00', '00:00']) "
                "OR EXISTS { MATCH (place)-[:HAS_FACT]->(fact:Fact) "
                "WHERE fact.predicate = 'opening_24h_claim' AND fact.value = true })"
            )
        budget = _constraint(constraints, "budget_max")
        if budget:
            clauses.append("EXISTS { MATCH (place)-[:HAS_FACT]->(price:Fact) WHERE price.predicate IN ['price', 'price_min'] AND toFloat(price.value) <= $budget_max }")
            params["budget_max"] = float(budget.value)
        weather = _constraint(constraints, "weather")
        if weather:
            params["weather"] = _plain(str(weather.value))
            clauses.append(
                "(place.is_indoor = true OR EXISTS { "
                "MATCH (place)-[:HAS_OFFERING*0..1]->(subject)-[:SUITABLE_DURING]->(weather:Concept) "
                "WHERE toLower(weather.canonical_name) CONTAINS $weather "
                "OR toLower(weather.canonical_name) = 'all weather' })"
            )

        rows = self.store.run(
            f"""
            MATCH (place:Place)
            {near_match}
            WHERE {' AND '.join(clauses)}
            OPTIONAL MATCH (place)-[:HAS_OFFERING*0..1]->(subject)-[]->(matched:Concept)
            WHERE any(term IN $all_terms WHERE toLower(matched.canonical_name) CONTAINS term OR toLower(matched.name) CONTAINS term)
            WITH place,
                 count(DISTINCT CASE WHEN any(term IN $required_terms WHERE toLower(matched.canonical_name) CONTAINS term OR toLower(matched.name) CONTAINS term) THEN matched END) AS requiredMatches,
                 count(DISTINCT CASE WHEN any(term IN $preferred_terms WHERE toLower(matched.canonical_name) CONTAINS term OR toLower(matched.name) CONTAINS term) THEN matched END) AS preferredMatches{', near' if near_subject else ''}
            WITH place, requiredMatches, preferredMatches,
                 CASE WHEN size($required_terms) = 0 THEN 1.0 ELSE toFloat(requiredMatches) / size($required_terms) END AS requiredCoverage,
                 CASE WHEN size($preferred_terms) = 0 THEN 0.0 ELSE toFloat(preferredMatches) / size($preferred_terms) END AS preferredCoverage{', near' if near_subject else ''}
            RETURN place {{.*, score:
                CASE WHEN size($preferred_terms) = 0
                     THEN requiredCoverage
                     ELSE $required_weight * requiredCoverage + $preferred_weight * preferredCoverage
                END{distance_projection}}} AS place
            ORDER BY preferredMatches DESC, requiredMatches DESC, coalesce(place.rating, 0) DESC
            LIMIT $limit
            """,
            **params,
        )
        candidates = [_entity(row["place"]) for row in rows]
        checks = [
            ConstraintResult(
                place_id=candidate.place_id,
                field=constraint.field,
                expected=constraint.value,
                mode=constraint.mode,
                passed=True,
            )
            for candidate in candidates
            for constraint in constraints
            if constraint.mode == ConstraintMode.HARD
        ]
        return candidates, checks

    def _rank_candidates(
        self,
        query: str,
        candidates: list[EntityResult],
        limit: int,
        entity_types: list[str],
    ) -> list[EntityResult]:
        if not candidates:
            return []
        candidate_ids = [candidate.place_id for candidate in candidates]
        query_embedding = self._query_embedding(query)
        vector_scores: dict[str, float] = {}
        if query_embedding is not None:
            rows = self.store.run(
                """
                CALL db.index.vector.queryNodes($index_name, $candidate_limit, $embedding)
                YIELD node AS place, score
                WHERE place.kb_version = $kb_version AND place.id IN $candidate_ids
                RETURN place.id AS place_id, score
                """,
                index_name=self.vector_index,
                candidate_limit=max(
                    len(candidates) * POLICY.candidate_multiplier,
                    POLICY.minimum_vector_pool,
                ),
                embedding=query_embedding,
                kb_version=self.kb_version,
                candidate_ids=candidate_ids,
            )
            vector_scores = {row["place_id"]: float(row["score"]) for row in rows}
        metadata = self._candidate_metadata(candidate_ids)
        ranked = []
        for candidate in candidates:
            graph_score = float(candidate.score) if candidate.score is not None else 0.0
            candidate_metadata = metadata[candidate.place_id]
            rating_score = min(
                candidate_metadata["rating"] / candidate_metadata["rating_scale"],
                1.0,
            )
            semantic = vector_scores.get(candidate.place_id, 0)
            score = POLICY.ranking.score(
                semantic=semantic,
                graph_coverage=graph_score,
                type_match=1.0,
                rating=rating_score,
                evidence=candidate_metadata["evidence_confidence"],
            )
            ranked.append(candidate.model_copy(update={"score": round(score, 6)}))
        sorted_candidates = sorted(
            ranked,
            key=lambda item: item.score if item.score is not None else 0.0,
            reverse=True,
        )
        return _balanced_results(sorted_candidates, entity_types, limit)

    def _candidate_metadata(self, candidate_ids: list[str]) -> dict[str, dict[str, float]]:
        rows = self.store.run(
            """
            UNWIND $candidate_ids AS placeId
            MATCH (place:Place {id: placeId, kb_version: $kb_version})
            OPTIONAL MATCH (place)-[:HAS_OFFERING*0..1]->(subject)
                           <-[:ABOUT]-(claim:Claim {kb_version: $kb_version})
            RETURN place.id AS place_id,
                   coalesce(place.rating, 0) AS rating,
                   CASE WHEN coalesce(place.rating, 0) > 5 THEN 10.0 ELSE 5.0 END AS rating_scale,
                   coalesce(avg(claim.confidence), 0) AS evidence_confidence
            """,
            candidate_ids=candidate_ids,
            kb_version=self.kb_version,
        )
        return {
            row["place_id"]: {
                "rating": float(row["rating"]),
                "rating_scale": max(float(row["rating_scale"]), 1.0),
                "evidence_confidence": float(row["evidence_confidence"]),
            }
            for row in rows
        }

    def _matched_paths(self, candidates: list[EntityResult], concepts: list[str]) -> list[MatchedPath]:
        if not candidates or not concepts:
            return []
        rows = self.store.run(
            """
            UNWIND $place_ids AS placeId
            MATCH (place:Place {id: placeId, kb_version: $kb_version})
            MATCH path=(place)-[:HAS_OFFERING*0..1]->(subject)-[relationship]->(concept:Concept)
            WHERE $terms = [] OR any(term IN $terms WHERE toLower(concept.canonical_name) CONTAINS term OR toLower(concept.name) CONTAINS term)
            RETURN place.id AS place_id,
                   collect(DISTINCT concept.name) AS concepts,
                   collect(DISTINCT type(relationship)) AS relationships
            """,
            place_ids=[candidate.place_id for candidate in candidates],
            terms=[_plain(term) for term in concepts],
            kb_version=self.kb_version,
        )
        scores = {item.place_id: float(item.score or 0) for item in candidates}
        return [
            MatchedPath(
                place_id=row["place_id"],
                nodes=[row["place_id"], *row["concepts"]],
                relationships=row["relationships"],
                score=scores.get(row["place_id"], 0),
            )
            for row in rows
        ]

    def _claim_evidence(
        self,
        candidates: list[EntityResult],
        concepts: list[str],
    ) -> list[V4EvidenceResult]:
        if not candidates:
            return []
        rows = self.store.run(
            """
            UNWIND $place_ids AS placeId
            MATCH (place:Place {id: placeId, kb_version: $kb_version})-[:HAS_OFFERING*0..1]->(subject)
            MATCH (claim:Claim {kb_version: $kb_version})-[:ABOUT]->(subject)
            MATCH (claim)-[:OBJECT]->(concept:Concept)
            MATCH (claim)-[:SUPPORTED_BY]->(unit:TextUnit)-[:PART_OF]->(document:Document)
            WHERE $terms = [] OR any(term IN $terms WHERE toLower(concept.canonical_name) CONTAINS term OR toLower(concept.name) CONTAINS term)
            RETURN DISTINCT place.id AS subject_id,
                   unit.id AS text_unit_id, document.id AS document_id,
                   coalesce(unit.title, document.title, place.name) AS title,
                   unit.text AS text, document.url AS url, document.source_name AS source_name
            LIMIT $evidence_limit
            """,
            place_ids=[candidate.place_id for candidate in candidates],
            terms=[_plain(term) for term in concepts],
            evidence_limit=POLICY.maximum_evidence_results,
            kb_version=self.kb_version,
        )
        return [V4EvidenceResult.model_validate(row) for row in rows]

    def _community_candidates(self, city: str | None, entity_types: list[str], limit: int) -> list[EntityResult]:
        rows = self.store.run(
            """
            MATCH (community:Community {kb_version: $kb_version})<-[:IN_COMMUNITY]-(place:Place)
            WHERE ($city IS NULL OR community.city = $city)
              AND ($entity_types = [] OR community.entity_type IN $entity_types)
            WITH community, place ORDER BY coalesce(place.rating, 0) DESC
            WITH community, collect(place)[0..$limit] AS places
            UNWIND places AS place
            RETURN place {.*, score: coalesce(place.rating, 0) / 5.0} AS place
            LIMIT $limit
            """,
            city=city,
            entity_types=entity_types,
            limit=limit,
            kb_version=self.kb_version,
        )
        return [_entity(row["place"]) for row in rows]


def _balanced_results(
    candidates: list[EntityResult],
    requested_types: list[str],
    limit: int,
) -> list[EntityResult]:
    """Keep explicit multi-type requests represented without changing single-type ranking."""
    types = list(dict.fromkeys(requested_types))
    if len(types) < 2:
        return candidates[:limit]

    buckets = {
        entity_type: [item for item in candidates if item.entity_type == entity_type]
        for entity_type in types
    }
    selected: list[EntityResult] = []
    while len(selected) < limit and any(buckets.values()):
        for entity_type in types:
            bucket = buckets[entity_type]
            if bucket and len(selected) < limit:
                selected.append(bucket.pop(0))
    return selected


def _plain(value: str) -> str:
    return slugify(value).replace("-", " ")


def _constraint(constraints: list[V4Constraint], field: str) -> V4Constraint | None:
    return next((item for item in constraints if item.field == field and item.mode == ConstraintMode.HARD), None)


def _required_tools(query: str) -> list[str]:
    plain = _plain(query)
    tools = []
    if any(term in plain for term in ("thoi tiet", "mua", "nhiet do")):
        tools.append("weather")
    if any(term in plain for term in ("duong di", "giao thong", "mat bao lau")):
        tools.append("map")
    return tools or ["external_knowledge"]
