from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ..v2.retrieval import _anchor_similarity, _fulltext_query
from .schemas import QueryTarget, TargetKind, TargetResult


@dataclass(frozen=True)
class _TargetSpec:
    label: str
    lookup_field: str = "name"


_TARGET_SPECS = {
    TargetKind.PLACE: _TargetSpec("Place"),
    TargetKind.CITY: _TargetSpec("City"),
    TargetKind.GEO_AREA: _TargetSpec("GeoArea"),
    TargetKind.DISH: _TargetSpec("Dish", "canonical_name"),
    TargetKind.ACTIVITY: _TargetSpec("Activity", "canonical_name"),
    TargetKind.CONCEPT: _TargetSpec("Concept", "canonical_name"),
}

_PLACE_TYPE_PREFIXES = {
    "attraction": ("địa điểm tham quan", "điểm tham quan", "attraction"),
    "cafe": ("quán cà phê", "quán cafe", "coffee shop", "cà phê", "cafe"),
    "hotel": ("khu nghỉ dưỡng", "khách sạn", "lưu trú", "resort", "hotel"),
    "nightlife": ("địa điểm nightlife", "quán bar", "nightlife", "night club", "bar", "pub", "club"),
    "restaurant": ("nhà hàng", "quán ăn", "restaurant"),
}
_EDGE_PUNCTUATION = re.compile(r"^[\s:;,.!?…|/\\\-–—]+|[\s:;,.!?…|/\\\-–—]+$")
_FUZZY_MATCH_THRESHOLD = 0.8


class V5EntityResolver:
    def __init__(self, store: Any):
        self.store = store

    def resolve(
        self,
        target: QueryTarget,
        limit: int = 5,
        cities: list[str] | None = None,
    ) -> list[TargetResult]:
        if target.kind in {TargetKind.DISH, TargetKind.ACTIVITY, TargetKind.CONCEPT}:
            return self._resolve_concept_targets(target, limit, cities or [])
        if not target.value:
            return self.list_targets(target, limit, cities)
        spec = _TARGET_SPECS[target.kind]
        scope_clause = _scope_clause(target.kind)
        lookup_values = _lookup_values(target)
        rows = self.store.run_versioned(
            f"""
            MATCH (node:{spec.label} {{kb_version: $kb_version}})
            WHERE any(value IN $values WHERE
                toLower(coalesce(node.{spec.lookup_field}, node.name)) = toLower(value)
                OR any(alias IN coalesce(node.aliases, [])
                       WHERE toLower(alias) = toLower(value)))
            WITH node
            WHERE {scope_clause}
            OPTIONAL MATCH (claim:Claim {{kb_version: $kb_version}})-[:OBJECT]->(node)
            OPTIONAL MATCH (claim)-[:SUPPORTED_BY]->(claimUnit:TextUnit)
            OPTIONAL MATCH (directUnit:TextUnit)-[:MENTIONS|MENTIONS_GEO_AREA]->(node)
            RETURN node.id AS id,
                   coalesce(node.name, node.{spec.lookup_field}) AS name,
                   coalesce(node.description, node.summary) AS description,
                   [value IN collect(DISTINCT claimUnit.id) + collect(DISTINCT directUnit.id)
                    WHERE value IS NOT NULL] AS evidence_ids,
                   1.0 AS score
            LIMIT $limit
            """,
            values=lookup_values,
            cities=cities or [],
            entity_types=target.entity_types,
            limit=limit,
        )
        if not rows:
            rows = self.store.run_versioned(
                f"""
                MATCH (node:{spec.label} {{kb_version: $kb_version}})
                WHERE any(value IN $values WHERE
                    toLower(coalesce(node.{spec.lookup_field}, node.name)) CONTAINS toLower(value)
                    OR any(alias IN coalesce(node.aliases, [])
                           WHERE toLower(alias) CONTAINS toLower(value)))
                WITH node
                WHERE {scope_clause}
                OPTIONAL MATCH (claim:Claim {{kb_version: $kb_version}})-[:OBJECT]->(node)
                OPTIONAL MATCH (claim)-[:SUPPORTED_BY]->(claimUnit:TextUnit)
                OPTIONAL MATCH (directUnit:TextUnit)-[:MENTIONS|MENTIONS_GEO_AREA]->(node)
                RETURN node.id AS id,
                       coalesce(node.name, node.{spec.lookup_field}) AS name,
                       coalesce(node.description, node.summary) AS description,
                       [value IN collect(DISTINCT claimUnit.id) + collect(DISTINCT directUnit.id)
                        WHERE value IS NOT NULL] AS evidence_ids,
                       0.7 AS score
                ORDER BY size(coalesce(node.{spec.lookup_field}, node.name))
                LIMIT $limit
                """,
                values=lookup_values,
                cities=cities or [],
                entity_types=target.entity_types,
                limit=limit,
            )
        if not rows and target.kind == TargetKind.PLACE:
            rows = self._resolve_place_fulltext(
                lookup_values[0],
                target.entity_types,
                cities or [],
                limit,
            )
        return [_target_result(row, target.kind) for row in rows]

    def _resolve_place_fulltext(
        self,
        value: str,
        entity_types: list[str],
        cities: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        query_text = _fulltext_query(value)
        if not query_text:
            return []
        candidate_limit = max(limit * 10, 50)
        rows = self.store.run_versioned(
            """
            CALL db.index.fulltext.queryNodes(
                'v5_place_fulltext',
                $query_text,
                {limit: $candidate_limit}
            )
            YIELD node, score
            WHERE node.kb_version = $kb_version
              AND ($cities = [] OR node.city IN $cities)
              AND ($entity_types = [] OR node.entity_type IN $entity_types)
            OPTIONAL MATCH (claim:Claim {kb_version: $kb_version})-[:OBJECT]->(node)
            OPTIONAL MATCH (claim)-[:SUPPORTED_BY]->(claimUnit:TextUnit)
            OPTIONAL MATCH (directUnit:TextUnit)-[:MENTIONS]->(node)
            RETURN node.id AS id,
                   node.name AS name,
                   node.aliases AS aliases,
                   node.description AS description,
                   [item IN collect(DISTINCT claimUnit.id) + collect(DISTINCT directUnit.id)
                    WHERE item IS NOT NULL] AS evidence_ids,
                   score
            ORDER BY score DESC
            LIMIT $candidate_limit
            """,
            query_text=query_text,
            candidate_limit=candidate_limit,
            cities=cities,
            entity_types=entity_types,
        )
        compatible = [
            (
                _anchor_similarity(
                    value,
                    {
                        "name": row.get("name"),
                        "aliases": row.get("aliases") or [],
                    },
                ),
                row,
            )
            for row in rows
        ]
        return [
            row
            for similarity, row in sorted(
                compatible,
                key=lambda item: (item[0], float(item[1].get("score") or 0)),
                reverse=True,
            )
            if similarity >= _FUZZY_MATCH_THRESHOLD
        ][:limit]

    def _resolve_concept_targets(
        self,
        target: QueryTarget,
        limit: int,
        cities: list[str],
    ) -> list[TargetResult]:
        spec = _TARGET_SPECS[target.kind]
        rows = self.store.run_versioned(
            f"""
            MATCH (node:{spec.label} {{kb_version: $kb_version}})
            WHERE $value IS NULL
               OR toLower(coalesce(node.{spec.lookup_field}, node.name)) CONTAINS toLower($value)
            MATCH (claim:Claim {{kb_version: $kb_version}})-[:OBJECT]->(node)
            MATCH (claim)-[:ABOUT]->(subject)
            MATCH (place:Place {{kb_version: $kb_version}})-[:HAS_OFFERING*0..1]->(subject)
            WHERE ($cities = [] OR place.city IN $cities)
              AND ($target_kind <> 'dish' OR place.entity_type = 'restaurant')
            MATCH (claim)-[:SUPPORTED_BY]->(unit:TextUnit)
            WITH node, collect(DISTINCT unit.id) AS evidence_ids,
                 count(DISTINCT claim) AS support,
                 CASE WHEN $value IS NOT NULL
                           AND toLower(coalesce(node.{spec.lookup_field}, node.name)) = toLower($value)
                      THEN 1 ELSE 0 END AS exact
            RETURN node.id AS id,
                   coalesce(node.name, node.{spec.lookup_field}) AS name,
                   coalesce(node.description, node.summary) AS description,
                   evidence_ids,
                   toFloat(support) AS score
            ORDER BY exact DESC, support DESC, name
            LIMIT $limit
            """,
            value=target.value,
            cities=cities,
            target_kind=target.kind.value,
            limit=limit,
        )
        return [_target_result(row, target.kind) for row in rows]

    def list_targets(
        self,
        target: QueryTarget,
        limit: int,
        cities: list[str] | None = None,
    ) -> list[TargetResult]:
        spec = _TARGET_SPECS[target.kind]
        scope_clause = _scope_clause(target.kind)
        rows = self.store.run_versioned(
            f"""
            MATCH (node:{spec.label} {{kb_version: $kb_version}})
            WHERE {scope_clause}
            OPTIONAL MATCH (claim:Claim {{kb_version: $kb_version}})-[:OBJECT]->(node)
            OPTIONAL MATCH (claim)-[:SUPPORTED_BY]->(claimUnit:TextUnit)
            OPTIONAL MATCH (directUnit:TextUnit)-[:MENTIONS|MENTIONS_GEO_AREA]->(node)
            WITH node,
                 [value IN collect(DISTINCT claimUnit.id) + collect(DISTINCT directUnit.id)
                  WHERE value IS NOT NULL] AS evidence_ids,
                 count(DISTINCT claim) AS support
            RETURN node.id AS id,
                   coalesce(node.name, node.{spec.lookup_field}) AS name,
                   coalesce(node.description, node.summary) AS description,
                   evidence_ids,
                   toFloat(support) AS score
            ORDER BY support DESC, name
            LIMIT $limit
            """,
            limit=limit,
            cities=cities or [],
            entity_types=target.entity_types,
        )
        return [_target_result(row, target.kind) for row in rows]


def _target_result(row: dict[str, Any], kind: TargetKind) -> TargetResult:
    return TargetResult(
        target_id=str(row["id"]),
        kind=kind,
        name=str(row["name"]),
        description=row.get("description"),
        score=float(row.get("score") or 0),
        evidence_ids=[str(value) for value in row.get("evidence_ids") or [] if value],
    )


def _lookup_values(target: QueryTarget) -> list[str]:
    value = _clean_lookup_value(target.value or "")
    if target.kind != TargetKind.PLACE:
        return [value]
    normalized = _strip_place_type_prefix(value, target.entity_types)
    return list(dict.fromkeys(candidate for candidate in (normalized, value) if candidate))


def _clean_lookup_value(value: str) -> str:
    return _EDGE_PUNCTUATION.sub("", value.strip()).strip()


def _strip_place_type_prefix(value: str, entity_types: list[str]) -> str:
    prefixes = {
        prefix
        for entity_type in entity_types
        for prefix in _PLACE_TYPE_PREFIXES.get(entity_type, ())
    }
    if not prefixes:
        return value
    alternatives = "|".join(
        re.escape(prefix)
        for prefix in sorted(prefixes, key=len, reverse=True)
    )
    normalized = re.sub(
        rf"^(?:{alternatives})(?:\s*[:\-–—]\s*|\s+)",
        "",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    return _clean_lookup_value(normalized) or value


def _scope_clause(kind: TargetKind) -> str:
    if kind == TargetKind.PLACE:
        return (
            "($cities = [] OR node.city IN $cities) AND "
            "($entity_types = [] OR node.entity_type IN $entity_types)"
        )
    if kind == TargetKind.CITY:
        return "$cities = [] OR node.name IN $cities"
    if kind == TargetKind.GEO_AREA:
        return (
            "$cities = [] OR EXISTS { "
            "MATCH (city:City)-[:HAS_AREA]->(node) "
            "WHERE city.kb_version = $kb_version AND city.name IN $cities }"
        )
    return (
        "$cities = [] OR EXISTS { "
        "MATCH (claim:Claim {kb_version: $kb_version})-[:OBJECT]->(node) "
        "MATCH (claim)-[:ABOUT]->(subject) "
        "MATCH (place:Place {kb_version: $kb_version})-[:HAS_OFFERING*0..1]->(subject) "
        "WHERE place.city IN $cities }"
    )
