from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
        rows = self.store.run_versioned(
            f"""
            MATCH (node:{spec.label} {{kb_version: $kb_version}})
            WHERE toLower(coalesce(node.{spec.lookup_field}, node.name)) = toLower($value)
               OR any(alias IN coalesce(node.aliases, []) WHERE toLower(alias) = toLower($value))
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
            value=target.value,
            cities=cities or [],
            entity_types=target.entity_types,
            limit=limit,
        )
        if not rows:
            rows = self.store.run_versioned(
                f"""
                MATCH (node:{spec.label} {{kb_version: $kb_version}})
                WHERE toLower(coalesce(node.{spec.lookup_field}, node.name)) CONTAINS toLower($value)
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
                value=target.value,
                cities=cities or [],
                entity_types=target.entity_types,
                limit=limit,
            )
        return [_target_result(row, target.kind) for row in rows]

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
