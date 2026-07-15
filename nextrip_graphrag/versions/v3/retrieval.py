from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Any

from ...config import DEFAULT_TYPED_QUERY_TOP_K
from ..registry import kb_version_manifests
from ..v2.retrieval import V2RetrievalService, _elapsed_ms, _entity, _fact
from ..v2.schemas import EntityResult, FactResult, QueryOperation
from .graph_store import V3GraphStore
from .query_planner import plan_query
from .schemas import V3Filters, V3QueryResponse


class V3RetrievalService(V2RetrievalService):
    kb_version = "v3"
    fulltext_index = "v3_place_fulltext"
    vector_index = "v3_place_embedding"

    def __init__(self, store: V3GraphStore, gemini: Any | None = None):
        super().__init__(store, gemini)

    def query(self, query: str, top_k: int = DEFAULT_TYPED_QUERY_TOP_K) -> V3QueryResponse:
        self._ensure_ready()
        started = perf_counter()
        plan, planner, fallback_reason = plan_query(query, self.gemini)
        trace: list[dict[str, Any]] = [
            {
                "step": "query_planner",
                "status": "ok",
                "planner": planner,
                "fallback_reason": fallback_reason,
                "elapsed_ms": _elapsed_ms(started),
            }
        ]
        entities: list[EntityResult] = []
        facts: list[FactResult] = []
        recommendations: list[EntityResult] = []
        missing_fields: list[str] = []

        if plan.clarification_needed:
            missing_fields = ["city"] if plan.city is None else ["query_constraints"]
        else:
            for index, task in enumerate(plan.tasks, start=1):
                task_started = perf_counter()
                limit = min(task.limit, top_k)
                if task.operation == QueryOperation.COUNT:
                    facts.append(self._count_fact(plan.city, task.entity_types))
                elif task.operation == QueryOperation.LOOKUP:
                    entities, facts = self._lookup_v3(
                        plan.subjects[0],
                        task.predicates,
                        task.entity_types,
                        city=plan.city,
                    )
                    if not entities:
                        missing_fields.append(f"entity:{plan.subjects[0]}")
                    elif not facts:
                        missing_fields.extend(f"fact:{predicate}" for predicate in task.predicates)
                elif task.operation in {QueryOperation.FILTER, QueryOperation.RECOMMEND}:
                    candidate_limit = max(limit * 10, 50) if task.operation == QueryOperation.RECOMMEND else limit
                    results = self._filter_v3(
                        plan.city,
                        task.entity_types,
                        task.filters,
                        candidate_limit,
                    )
                    if task.operation == QueryOperation.RECOMMEND:
                        recommendations = self._semantic_rerank(query, results, limit)
                    else:
                        entities = results
                trace.append(
                    {
                        "step": task.operation.value,
                        "task_index": index,
                        "status": "ok",
                        "entity_types": task.entity_types,
                        "fact_count": len(facts),
                        "result_count": len(entities) + len(recommendations),
                        "retrieval_mode": "graph_first_typed_filters",
                        "elapsed_ms": _elapsed_ms(task_started),
                    }
                )

        evidence = self._evidence(facts)
        trace.append({"step": "total", "status": "ok", "elapsed_ms": _elapsed_ms(started)})
        return V3QueryResponse(
            answer_type=plan.intent,
            query_plan=plan,
            entities=entities,
            facts=facts,
            recommendations=recommendations,
            evidence=evidence,
            missing_fields=missing_fields,
            trace=trace,
            manifest=asdict(kb_version_manifests()["v3"]),
        )

    def _lookup_v3(
        self,
        subject: str,
        predicates: list[str],
        entity_types: list[str],
        *,
        city: str | None = None,
    ) -> tuple[list[EntityResult], list[FactResult]]:
        anchor = self._anchor(subject, entity_types=entity_types, city=city)
        if not anchor:
            return [], []
        rows = self.store.run(
            """
            MATCH (place:Place {id: $place_id, kb_version: $kb_version})-[:HAS_FACT]->(fact:Fact)
            WHERE fact.predicate IN $predicates
            OPTIONAL MATCH (fact)-[:SUPPORTED_BY]->(unit:TextUnit)
            RETURN fact, collect(DISTINCT unit.id) AS evidence_ids
            ORDER BY fact.predicate
            """,
            place_id=anchor["id"],
            predicates=predicates,
            kb_version=self.kb_version,
        )
        return [_entity(anchor)], [_fact(row) for row in rows]

    def _filter_v3(
        self,
        city: str | None,
        entity_types: list[str],
        filters: V3Filters,
        limit: int,
    ) -> list[EntityResult]:
        clauses = [
            "place.kb_version = $kb_version",
            "($city IS NULL OR place.city = $city)",
            "($entity_types = [] OR place.entity_type IN $entity_types)",
        ]
        params: dict[str, Any] = {
            "kb_version": self.kb_version,
            "city": city,
            "entity_types": entity_types,
            "limit": limit,
        }
        if filters.star_rating is not None:
            clauses.append("place.star_rating = $star_rating")
            params["star_rating"] = filters.star_rating
        if filters.category:
            clauses.append("place.category = $category")
            params["category"] = filters.category
        if filters.cuisine:
            clauses.append(
                "EXISTS { MATCH (place)-[:SERVES_CUISINE]->(facet:Cuisine) "
                "WHERE toLower(facet.name) CONTAINS toLower($cuisine) }"
            )
            params["cuisine"] = filters.cuisine
        if filters.dish:
            clauses.append(
                "EXISTS { MATCH (place)-[:HAS_DISH]->(facet:Dish) "
                "WHERE toLower(facet.name) CONTAINS toLower($dish) }"
            )
            params["dish"] = filters.dish
        if filters.amenity:
            clauses.append(
                "EXISTS { MATCH (place)-[:HAS_AMENITY]->(facet:Amenity) "
                "WHERE any(term IN $amenity_terms WHERE toLower(facet.name) CONTAINS term) }"
            )
            params["amenity_terms"] = _facet_terms(filters.amenity)
        if filters.venue_type:
            clauses.append(
                "EXISTS { MATCH (place)-[:HAS_VENUE_TYPE]->(facet:VenueType) "
                "WHERE toLower(facet.name) = toLower($venue_type) }"
            )
            params["venue_type"] = filters.venue_type
        if filters.open_24h:
            clauses.append(
                "((place.opening_hours_open IN ['00:00', '0:00'] "
                "AND place.opening_hours_close IN ['23:59', '24:00', '00:00']) "
                "OR EXISTS { MATCH (place)-[:HAS_FACT]->(fact:Fact) "
                "WHERE fact.predicate = 'opening_24h_claim' AND fact.value = true })"
            )
        if filters.indoor is True or filters.weather == "rain":
            clauses.append(
                "(place.is_indoor = true OR "
                "'all_weather' IN coalesce(place.weather_suitable, []))"
            )
        elif filters.indoor is False:
            clauses.append("place.is_indoor = false")
        if filters.weather and filters.weather != "rain":
            clauses.append(
                "($weather IN coalesce(place.weather_suitable, []) OR "
                "'all_weather' IN coalesce(place.weather_suitable, []))"
            )
            params["weather"] = filters.weather
        if filters.ambience == "sea_view" or filters.tag == "sea_view":
            clauses.append(
                "(place.hotel_style = 'beachfront' OR "
                "EXISTS { MATCH (place)-[:HAS_AMBIENCE|TAGGED_WITH|HAS_AMENITY]->(facet) "
                "WHERE toLower(facet.name) IN ['sea_view', 'giáp biển', 'beach_access'] })"
            )
        near_match = ""
        projection_extra = ""
        order_by = "coalesce(place.rating, 0) DESC, place.name"
        if filters.near_subject:
            anchor = self._anchor(filters.near_subject)
            if not anchor:
                return []
            near_match = "MATCH (place)-[near:NEAR]->(:Place {id: $near_id})"
            clauses.append("near.distance_km IS NOT NULL")
            params["near_id"] = anchor["id"]
            order_by = "near.distance_km ASC, coalesce(place.rating, 0) DESC"
            projection_extra = ", distance_km: near.distance_km"
        query = f"""
        MATCH (place:Place)
        {near_match}
        WHERE {' AND '.join(clauses)}
        RETURN place {{.*, score: coalesce(place.rating, 0){projection_extra}}} AS place
        ORDER BY {order_by}
        LIMIT $limit
        """
        return [_entity(row["place"]) for row in self.store.run(query, **params)]

    def _semantic_rerank(
        self,
        query: str,
        candidates: list[EntityResult],
        limit: int,
    ) -> list[EntityResult]:
        query_embedding = self._query_embedding(query)
        if query_embedding is None or not candidates:
            return candidates[:limit]
        candidate_ids = [candidate.place_id for candidate in candidates]
        rows = self.store.run(
            """
            CALL db.index.vector.queryNodes($index_name, $candidate_limit, $embedding)
            YIELD node AS place, score
            WHERE place.kb_version = $kb_version AND place.id IN $candidate_ids
            RETURN place.id AS place_id, score
            """,
            index_name=self.vector_index,
            candidate_limit=max(len(candidate_ids) * 10, 100),
            embedding=query_embedding,
            kb_version=self.kb_version,
            candidate_ids=candidate_ids,
        )
        vector_scores = {row["place_id"]: float(row["score"]) for row in rows}
        reranked = [
            candidate.model_copy(
                update={
                    "score": round(
                        0.85 * vector_scores.get(candidate.place_id, 0.0)
                        + 0.15 * min(float(candidate.score or 0.0) / 5.0, 1.0),
                        6,
                    )
                }
            )
            for candidate in candidates
        ]
        return sorted(reranked, key=lambda candidate: candidate.score or 0.0, reverse=True)[:limit]


def _facet_terms(value: str) -> list[str]:
    if value == "pool":
        return ["pool", "hồ bơi"]
    return [value.casefold()]
