from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
import re
from time import perf_counter
from typing import Any

from ...config import DEFAULT_TYPED_QUERY_TOP_K
from ...normalizer import CITY_DEFINITIONS
from ..registry import kb_version_manifests
from .graph_store import V2GraphStore
from .query_planner import plan_query
from .schemas import (
    EntityResult,
    EvidenceResult,
    FactResult,
    QueryIntent,
    QueryOperation,
    V2QueryResponse,
)


class V2RetrievalService:
    kb_version = "v2"
    fulltext_index = "v2_place_fulltext"
    vector_index = "v2_place_embedding"

    def __init__(self, store: V2GraphStore, gemini: Any | None = None):
        self.store = store
        self.gemini = gemini

    def query(self, query: str, top_k: int = DEFAULT_TYPED_QUERY_TOP_K) -> V2QueryResponse:
        self._ensure_ready()
        trace: list[dict[str, Any]] = []
        started = perf_counter()
        plan, planner, fallback_reason = plan_query(query, self.gemini)
        planner_trace = {
            "step": "query_planner",
            "status": "ok",
            "planner": planner,
            "elapsed_ms": _elapsed_ms(started),
        }
        if fallback_reason:
            planner_trace["fallback_reason"] = fallback_reason
        trace.append(planner_trace)
        entities: list[EntityResult] = []
        facts: list[FactResult] = []
        recommendations: list[EntityResult] = []
        missing_fields: list[str] = []
        retrieval_mode = "graph"

        if plan.clarification_needed:
            missing_fields = _missing_fields(plan)
        elif plan.tasks:
            for task_index, task in enumerate(plan.tasks, start=1):
                task.limit = min(task.limit, top_k)
                retrieval_started = perf_counter()
                if task.operation == QueryOperation.COUNT:
                    facts.append(self._count_fact(plan.city, task.entity_types))
                elif task.operation == QueryOperation.LOOKUP:
                    entities, facts = self._lookup(plan.subjects[0], task.predicates)
                elif task.operation == QueryOperation.FILTER:
                    entities = self._filter(plan.city, task.entity_types, task.limit)
                elif task.operation == QueryOperation.RECOMMEND:
                    query_embedding = self._query_embedding(query)
                    if query_embedding is not None:
                        retrieval_mode = "hybrid_vector_graph"
                    recommendations = self._recommend(
                        plan.city,
                        task.entity_types,
                        task.limit,
                        rainy=task.hard_constraints.weather == "rain",
                        query_embedding=query_embedding,
                    )
                trace.append(
                    {
                        "step": task.operation.value,
                        "task_index": task_index,
                        "entity_types": task.entity_types,
                        "status": "ok",
                        "entity_count": len(entities) + len(recommendations),
                        "fact_count": len(facts),
                        "retrieval_mode": retrieval_mode,
                        "elapsed_ms": _elapsed_ms(retrieval_started),
                    }
                )

        evidence = self._evidence(facts)
        manifest = asdict(kb_version_manifests()["v2"])
        trace.append({"step": "total", "status": "ok", "elapsed_ms": _elapsed_ms(started)})
        return V2QueryResponse(
            answer_type=plan.intent,
            query_plan=plan,
            entities=entities,
            facts=facts,
            recommendations=recommendations,
            evidence=evidence,
            missing_fields=missing_fields,
            trace=trace,
            manifest=manifest,
        )

    def _count_fact(self, city: str | None, entity_types: list[str]) -> FactResult:
        city_id = _city_id(city)
        rows = self.store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(city:City {id: $city_id})
            WHERE place.entity_type IN $entity_types
            RETURN count(DISTINCT place) AS count
            """,
            city_id=city_id,
            entity_types=entity_types,
            kb_version=self.kb_version,
        )
        return FactResult(
            fact_id=f"aggregate:{city_id}:{'-'.join(entity_types)}",
            subject_id=city_id,
            predicate="count",
            entity_type=entity_types[0],
            value=int(rows[0]["count"]),
            value_type="number",
            confidence=1,
        )

    def _ensure_ready(self) -> None:
        rows = self.store.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            RETURN catalog.status AS status
            """,
            kb_version=self.kb_version,
        )
        if not rows or rows[0]["status"] != "ready":
            raise RuntimeError("GraphRAG V2 snapshot is not ready")

    def _lookup(
        self,
        subject: str,
        predicates: list[str],
    ) -> tuple[list[EntityResult], list[FactResult]]:
        anchor = self._anchor(subject)
        if not anchor:
            return [], []
        facts = self.store.run(
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
        return [_entity(anchor)], [_fact(row) for row in facts]

    def _anchor(
        self,
        subject: str,
        *,
        entity_types: list[str] | None = None,
        city: str | None = None,
    ) -> dict[str, Any] | None:
        allowed_types = entity_types or []
        exact = self.store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            WHERE ($entity_types = [] OR place.entity_type IN $entity_types)
              AND ($city IS NULL OR place.city = $city)
              AND (
                toLower(place.name) = toLower($subject)
                OR any(alias IN coalesce(place.aliases, []) WHERE toLower(alias) = toLower($subject))
              )
            RETURN place {.*, score: 1.0} AS place
            LIMIT 1
            """,
            subject=_fulltext_query(subject),
            entity_types=allowed_types,
            city=city,
            kb_version=self.kb_version,
        )
        if exact:
            return exact[0]["place"]
        rows = self.store.run(
            """
            CALL db.index.fulltext.queryNodes($index_name, $subject, {limit: 10})
            YIELD node, score
            WHERE node.kb_version = $kb_version
              AND ($entity_types = [] OR node.entity_type IN $entity_types)
              AND ($city IS NULL OR node.city = $city)
            RETURN node {.*, score: score} AS place
            ORDER BY score DESC
            LIMIT 10
            """,
            subject=subject,
            index_name=self.fulltext_index,
            entity_types=allowed_types,
            city=city,
            kb_version=self.kb_version,
        )
        for row in rows:
            place = row["place"]
            if _is_compatible_anchor(subject, place):
                return place
        candidates = self.store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            WHERE ($entity_types = [] OR place.entity_type IN $entity_types)
              AND ($city IS NULL OR place.city = $city)
            RETURN place {.*} AS place
            """,
            entity_types=allowed_types,
            city=city,
            kb_version=self.kb_version,
        )
        ranked = sorted(
            (
                (_anchor_similarity(subject, row["place"]), row["place"])
                for row in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        return ranked[0][1] if ranked and ranked[0][0] >= 0.82 else None

    def _filter(
        self,
        city: str | None,
        entity_types: list[str],
        limit: int,
    ) -> list[EntityResult]:
        rows = self.store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(:City {id: $city_id})
            WHERE place.entity_type IN $entity_types
            RETURN place {.*, score: coalesce(place.rating, 0)} AS place
            ORDER BY coalesce(place.rating, 0) DESC, place.name
            LIMIT $limit
            """,
            city_id=_city_id(city),
            entity_types=entity_types,
            limit=limit,
            kb_version=self.kb_version,
        )
        return [_entity(row["place"]) for row in rows]

    def _recommend(
        self,
        city: str | None,
        entity_types: list[str],
        limit: int,
        *,
        rainy: bool,
        query_embedding: list[float] | None,
    ) -> list[EntityResult]:
        if query_embedding is not None:
            rows = self.store.run(
                """
                CALL db.index.vector.queryNodes($index_name, $candidate_limit, $embedding)
                YIELD node AS place, score AS vectorScore
                WHERE place.kb_version = $kb_version
                  AND place.city = $city
                  AND ($entity_types = [] OR place.entity_type IN $entity_types)
                  AND (
                    NOT $rainy
                    OR place.is_indoor = true
                    OR 'all_weather' IN coalesce(place.weather_suitable, [])
                  )
                WITH place, (0.8 * vectorScore) + (0.2 * coalesce(place.rating, 0) / 5.0) AS score
                RETURN place {.*, score: score} AS place
                ORDER BY score DESC
                LIMIT $limit
                """,
                candidate_limit=max(limit * 10, 100),
                embedding=query_embedding,
                city=city,
                entity_types=entity_types,
                rainy=rainy,
                limit=limit,
                index_name=self.vector_index,
                kb_version=self.kb_version,
            )
            return [_entity(row["place"]) for row in rows]
        rows = self.store.run(
            """
            MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(:City {id: $city_id})
            WHERE ($entity_types = [] OR place.entity_type IN $entity_types)
              AND (
                NOT $rainy
                OR place.is_indoor = true
                OR 'all_weather' IN coalesce(place.weather_suitable, [])
              )
            WITH place, coalesce(place.rating, 0) AS score
            ORDER BY score DESC, place.name
            RETURN place {.*, score: score} AS place
            LIMIT $limit
            """,
            city_id=_city_id(city),
            entity_types=entity_types,
            rainy=rainy,
            limit=limit,
            kb_version=self.kb_version,
        )
        return [_entity(row["place"]) for row in rows]

    def _query_embedding(self, query: str) -> list[float] | None:
        if self.gemini is None:
            return None
        try:
            if not self.store.has_embeddings():
                return None
            return self.gemini.embed_query(query)
        except Exception:
            return None

    def _evidence(self, facts: list[FactResult]) -> list[EvidenceResult]:
        evidence_ids = sorted({item for fact in facts for item in fact.evidence_ids})
        if not evidence_ids:
            return []
        rows = self.store.run(
            """
            MATCH (unit:TextUnit)-[:PART_OF]->(document:Document)
            WHERE unit.id IN $ids
            RETURN unit, document
            """,
            ids=evidence_ids,
        )
        return [
            EvidenceResult(
                text_unit_id=row["unit"]["id"],
                document_id=row["document"]["id"],
                title=row["unit"].get("title") or row["document"].get("title") or "Evidence",
                text=row["unit"]["text"],
                url=row["document"].get("url"),
                source_name=row["document"].get("source_name"),
            )
            for row in rows
        ]


def _entity(place: dict[str, Any]) -> EntityResult:
    return EntityResult(
        place_id=place["id"],
        name=place["name"],
        city=place["city"],
        entity_type=place["entity_type"],
        category=place.get("category_name") or place.get("category"),
        score=place.get("score"),
        distance_km=place.get("distance_km"),
        attributes={
            key: value
            for key, value in {
                "is_indoor": place.get("is_indoor"),
                "weather_suitable": place.get("weather_suitable"),
            }.items()
            if value is not None and value != []
        },
    )


def _fact(row: dict[str, Any]) -> FactResult:
    fact = row["fact"]
    return FactResult(
        fact_id=fact["id"],
        subject_id=fact["id"].split(":", 2)[1],
        predicate=fact["predicate"],
        value=fact["value"],
        value_type=fact["value_type"],
        unit=fact.get("unit"),
        confidence=float(fact["confidence"]),
        evidence_ids=row["evidence_ids"],
    )


def _city_id(city: str | None) -> str:
    if city is None:
        raise ValueError("city is required for this operation")
    return CITY_DEFINITIONS[city]["id"]


def _missing_fields(plan: Any) -> list[str]:
    missing = []
    if plan.city is None and plan.intent in {
        QueryIntent.AGGREGATE_COUNT,
        QueryIntent.ENTITY_LIST,
        QueryIntent.RECOMMENDATION,
    }:
        missing.append("city")
    if plan.intent == QueryIntent.ENTITY_DETAIL and not plan.subjects:
        missing.append("subject")
    if plan.tasks and plan.intent in {QueryIntent.AGGREGATE_COUNT, QueryIntent.ENTITY_LIST}:
        if not plan.tasks[0].entity_types:
            missing.append("entity_types")
    return missing


def _elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


ANCHOR_STOP_WORDS = {
    "bai",
    "bien",
    "cafe",
    "coffee",
    "ca",
    "phe",
    "chua",
    "cau",
    "dia",
    "diem",
    "hotel",
    "khach",
    "nha",
    "quan",
    "restaurant",
    "bar",
    "san",
}


def _is_compatible_anchor(subject: str, place: dict[str, Any]) -> bool:
    return _anchor_similarity(subject, place) >= 0.6


def _fulltext_query(subject: str) -> str:
    sanitized = re.sub(r'[+\-!(){}\[\]^"~*?:\\/]|&&|\|\|', " ", subject)
    return " ".join(sanitized.split())


def _anchor_similarity(subject: str, place: dict[str, Any]) -> float:
    from ...normalizer import slugify

    subject_tokens = [
        token for token in slugify(subject).split("-") if token not in ANCHOR_STOP_WORDS
    ]
    candidate_text = " ".join([place.get("name") or "", *(place.get("aliases") or [])])
    candidate_tokens = slugify(candidate_text).split("-")
    if not subject_tokens:
        return 0.0
    matched = sum(
        any(
            token == candidate
            or SequenceMatcher(None, token, candidate).ratio() >= 0.86
            for candidate in candidate_tokens
        )
        for token in subject_tokens
    )
    token_match = matched / len(subject_tokens)
    compact_subject = "".join(subject_tokens)
    compact_candidate = "".join(candidate_tokens)
    compact_match = SequenceMatcher(None, compact_subject, compact_candidate).ratio()
    return max(token_match, compact_match if compact_match >= 0.72 else 0.0)
