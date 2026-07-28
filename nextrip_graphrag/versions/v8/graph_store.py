from __future__ import annotations

from neo4j_graphrag.indexes import create_fulltext_index, create_vector_index

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
    entity_label = "V8Entity"
    place_fulltext_index = "v8_place_fulltext"
    place_vector_index = "v8_place_embedding"
    concept_vector_index = "v8_concept_embedding"

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

        id_prefix = f"{self.kb_version}:"
        node_rows = self.run(
            """
            MATCH (source)
            WHERE source.kb_version = $source_kb_version
              AND source.id IS NOT NULL
            WITH source, labels(source) AS source_labels
            CREATE (copy:$(source_labels + [$entity_label]) {
              id: $id_prefix + toString(source.id)
            })
            FOREACH (
              key IN [key IN keys(source) WHERE key <> 'id'] |
              SET copy[key] = source[key]
            )
            SET copy.kb_version = $kb_version
            FOREACH (
              key IN [
                key IN keys(copy)
                WHERE key <> 'id'
                  AND key ENDS WITH '_id'
                  AND copy[key] IS :: STRING
              ] |
              SET copy[key] = $id_prefix + copy[key]
            )
            FOREACH (
              key IN [
                key IN keys(copy)
                WHERE key ENDS WITH '_ids'
                  AND copy[key] IS :: LIST<STRING>
              ] |
              SET copy[key] = [item IN copy[key] | $id_prefix + item]
            )
            FOREACH (
              ignored IN CASE
                WHEN 'TravelCatalog' IN source_labels THEN [1]
                ELSE []
              END |
              SET copy.status = 'building'
            )
            RETURN count(copy) AS nodes
            """,
            source_kb_version=self.source_kb_version,
            kb_version=self.kb_version,
            entity_label=self.entity_label,
            id_prefix=id_prefix,
        )
        projected_nodes = int(node_rows[0]["nodes"])

        self.run(
            """
            CREATE RANGE INDEX v8_entity_id IF NOT EXISTS
            FOR (node:V8Entity) ON (node.id)
            """
        )
        self.run("CALL db.awaitIndexes(60)")

        self._ensure_search_schema()

        relationship_rows = self.run(
            """
            MATCH (source)-[original]->(target)
            WHERE source.kb_version = $source_kb_version
              AND target.kb_version = $source_kb_version
              AND source.id IS NOT NULL
              AND target.id IS NOT NULL
            MATCH (source_copy:V8Entity {
              id: $id_prefix + toString(source.id),
              kb_version: $kb_version
            })
            MATCH (target_copy:V8Entity {
              id: $id_prefix + toString(target.id),
              kb_version: $kb_version
            })
            CREATE (source_copy)-[copy:$(type(original))]->(target_copy)
            SET copy = properties(original)
            FOREACH (
              key IN [
                key IN keys(copy)
                WHERE key ENDS WITH '_id'
                  AND copy[key] IS :: STRING
              ] |
              SET copy[key] = $id_prefix + copy[key]
            )
            FOREACH (
              key IN [
                key IN keys(copy)
                WHERE key ENDS WITH '_ids'
                  AND copy[key] IS :: LIST<STRING>
              ] |
              SET copy[key] = [item IN copy[key] | $id_prefix + item]
            )
            RETURN count(copy) AS relationships
            """,
            source_kb_version=self.source_kb_version,
            kb_version=self.kb_version,
            id_prefix=id_prefix,
        )
        projected_relationships = int(relationship_rows[0]["relationships"])

        statistics = self.projection_statistics()
        if statistics["nodes"] != projected_nodes:
            raise RuntimeError("V8 node projection count changed during materialization")
        if statistics["relationships"] != projected_relationships:
            raise RuntimeError(
                "V8 relationship projection count changed during materialization"
            )
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
        self.run(
            """
            MATCH (concept:Concept {kb_version: $kb_version})
            SET concept:V8Concept
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
        create_fulltext_index(
            self.driver,
            self.place_fulltext_index,
            label=self.place_label,
            node_properties=["name", "aliases", "entity_profile"],
            neo4j_database=self.settings.neo4j_database,
        )
        create_vector_index(
            self.driver,
            self.place_vector_index,
            label=self.place_label,
            embedding_property="embedding",
            dimensions=embedding_dimensions,
            similarity_fn="cosine",
            neo4j_database=self.settings.neo4j_database,
        )
        concept_dimensions = self.run(
            """
            MATCH (concept:V8Concept {kb_version: $kb_version})
            WHERE concept.embedding IS NOT NULL
            RETURN size(concept.embedding) AS dimensions
            LIMIT 1
            """,
            kb_version=self.kb_version,
        )
        if concept_dimensions:
            create_vector_index(
                self.driver,
                self.concept_vector_index,
                label="V8Concept",
                embedding_property="embedding",
                dimensions=int(concept_dimensions[0]["dimensions"]),
                similarity_fn="cosine",
                neo4j_database=self.settings.neo4j_database,
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
            "nodes": int(row["nodes"]) if rows else 0,
            "relationships": int(row["relationships"]) if rows else 0,
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

__all__ = ["V8GraphStore"]
