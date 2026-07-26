from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..v5.graph_store import V5GraphStore


class V8GraphStore(V5GraphStore):
    """Graph store for the isolated V8 projection.

    V8 is materialized as a versioned projection in the same Neo4j database.
    Node ids are namespaced so V5 remains untouched, while labels, properties,
    relationship types and provenance edges are preserved for graph retrieval.
    """

    kb_version = "v8"
    source_kb_version = "v5"
    place_label = "V8Place"
    place_fulltext_index = "v8_place_fulltext"
    place_vector_index = "v8_place_embedding"

    def project_from_v5(self, *, replace: bool = False) -> dict[str, int]:
        if not replace and self._projection_ready():
            self._ensure_search_schema()
            return self.projection_statistics()
        # A non-ready projection is incomplete by definition. Rebuild only
        # that version so a previous timeout cannot be mistaken for readiness.
        self.run(
            "MATCH (node {kb_version: $kb_version}) DETACH DELETE node",
            kb_version=self.kb_version,
        )

        nodes = self.run(
            """
            MATCH (node)
            WHERE node.kb_version = $source_kb_version AND node.id IS NOT NULL
            RETURN labels(node) AS labels, properties(node) AS properties
            """,
            source_kb_version=self.source_kb_version,
        )
        grouped_nodes: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in nodes:
            labels = tuple(
                sorted(
                    label
                    for label in row["labels"]
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label)
                )
            )
            if not labels:
                continue
            properties = dict(row["properties"])
            source_id = properties.get("id")
            if not source_id:
                continue
            properties = self._namespace_reference_properties(properties)
            properties["id"] = self._namespaced_id(str(source_id))
            properties["kb_version"] = self.kb_version
            if "TravelCatalog" in labels:
                properties["status"] = "building"
            grouped_nodes[labels].append({"properties": properties})

        for labels, rows in grouped_nodes.items():
            label_expression = ":" + ":".join(labels)
            self.run(
                f"""
                UNWIND $rows AS row
                CREATE (node{label_expression})
                SET node = row.properties
                """,
                rows=rows,
            )

        self._ensure_search_schema()

        relationships = self.run(
            """
            MATCH (source)-[relationship]->(target)
            WHERE source.kb_version = $source_kb_version
              AND target.kb_version = $source_kb_version
              AND source.id IS NOT NULL
              AND target.id IS NOT NULL
            RETURN source.id AS source_id,
                   target.id AS target_id,
                   labels(source) AS source_labels,
                   labels(target) AS target_labels,
                   type(relationship) AS relationship_type,
                   properties(relationship) AS properties
            """,
            source_kb_version=self.source_kb_version,
        )
        grouped_relationships: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in relationships:
            relationship_type = str(row["relationship_type"])
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", relationship_type):
                continue
            source_label = _primary_label(row["source_labels"])
            target_label = _primary_label(row["target_labels"])
            if not source_label or not target_label:
                continue
            grouped_relationships[(source_label, target_label)].append(
                {
                    "source_id": self._namespaced_id(str(row["source_id"])),
                    "target_id": self._namespaced_id(str(row["target_id"])),
                    "relationship_type": relationship_type,
                    "properties": self._namespace_reference_properties(
                        dict(row["properties"])
                    ),
                }
            )

        for (source_label, target_label), rows in grouped_relationships.items():
            for start in range(0, len(rows), 1000):
                self.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (source:{source_label} {{id: row.source_id, kb_version: $kb_version}})
                    MATCH (target:{target_label} {{id: row.target_id, kb_version: $kb_version}})
                    CREATE (source)-[relationship:$(row.relationship_type)]->(target)
                    SET relationship = row.properties
                    """,
                    rows=rows[start : start + 1000],
                    kb_version=self.kb_version,
                )

        statistics = self.projection_statistics()
        self.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            SET catalog.status = 'ready',
                catalog.projection_source = $source_kb_version,
                catalog.projected_nodes = $nodes,
                catalog.projected_relationships = $relationships
            """,
            kb_version=self.kb_version,
            source_kb_version=self.source_kb_version,
            nodes=statistics["nodes"],
            relationships=statistics["relationships"],
        )
        return statistics

    def _ensure_search_schema(self) -> None:
        """Give the projection its own labels and semantic indexes.

        A version-specific label keeps V8 retrieval independent from the V5
        indexes even though both projections intentionally share one database.
        """
        self.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            SET place:V8Place
            """,
            kb_version=self.kb_version,
        )
        dimensions = self.run(
            """
            MATCH (place:V8Place {kb_version: $kb_version})
            WHERE place.embedding IS NOT NULL
            RETURN size(place.embedding) AS dimensions
            LIMIT 1
            """,
            kb_version=self.kb_version,
        )
        if not dimensions:
            raise RuntimeError("V8 projection has no place embeddings")
        embedding_dimensions = int(dimensions[0]["dimensions"])
        self.run(
            """
            CREATE FULLTEXT INDEX v8_place_fulltext IF NOT EXISTS
            FOR (place:V8Place)
            ON EACH [place.name, place.aliases, place.entity_profile]
            """
        )
        self.run(
            f"""
            CREATE VECTOR INDEX v8_place_embedding IF NOT EXISTS
            FOR (place:V8Place) ON (place.embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {embedding_dimensions},
              `vector.similarity_function`: 'cosine'
            }}}}
            """
        )
        self.run(
            """
            CALL db.awaitIndexes(60)
            """
        )

    def projection_statistics(self) -> dict[str, int]:
        rows = self.run(
            """
            OPTIONAL MATCH (node {kb_version: $kb_version})
            WITH count(node) AS nodes
            OPTIONAL MATCH (source {kb_version: $kb_version})-[relationship]->(target)
            RETURN nodes, count(relationship) AS relationships
            """,
            kb_version=self.kb_version,
        )
        row = rows[0] if rows else {}
        return {
            "kb_version": self.kb_version,
            "nodes": int(row.get("nodes", 0)),
            "relationships": int(row.get("relationships", 0)),
        }

    def _projection_ready(self) -> bool:
        rows = self.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            RETURN catalog.status AS status
            LIMIT 1
            """,
            kb_version=self.kb_version,
        )
        return bool(rows and rows[0]["status"] == "ready")

    @staticmethod
    def _namespaced_id(source_id: str) -> str:
        return f"v8:{source_id}"

    @classmethod
    def _namespace_reference_properties(
        cls,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        updated = dict(properties)
        for key, value in properties.items():
            if key == "id":
                continue
            if key.endswith("_id") and isinstance(value, str):
                updated[key] = cls._namespaced_id(value)
            elif key.endswith("_ids") and isinstance(value, list):
                updated[key] = [
                    cls._namespaced_id(str(item))
                    for item in value
                ]
        return updated


def _primary_label(labels: list[str]) -> str | None:
    preferred = (
        "Place",
        "City",
        "GeoArea",
        "Offering",
        "Concept",
        "Claim",
        "TextUnit",
        "Document",
        "Fact",
        "TravelCatalog",
        "Community",
        "CommunityReport",
        "Source",
    )
    for label in preferred:
        if label in labels:
            return label
    for label in labels:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            return label
    return None


__all__ = ["V8GraphStore"]
