from __future__ import annotations

from itertools import chain, zip_longest
from typing import Any

from neo4j_graphrag.indexes import create_fulltext_index, create_vector_index

from ..v5.graph_store import V5GraphStore


class V8GraphStore(V5GraphStore):
    """Graph store for the isolated V8 database.

    New deployments are materialized from gated canonical artifacts by
    ``v8-import-canonical``. ``project_from_v5`` remains only as a legacy
    migration helper for databases that already contain a V5 projection.
    """

    kb_version = "v8"
    source_kb_version = "v5"
    place_label = "Place"
    entity_label = "Entity"
    place_fulltext_index = "place_fulltext"
    place_vector_index = "place_embedding"
    concept_vector_index = "concept_embedding"

    def runtime_readiness(self) -> dict[str, Any]:
        """Verify the active canonical release, not only Neo4j connectivity."""

        release_rows = self.run(
            """
            MATCH (catalog:CanonicalTravelCatalog:TravelCatalog {
              id: 'v8:canonical-travel-catalog',
              kb_version: $kb_version,
              status: 'ready'
            })-[:CURRENT_RELEASE]->(release:DatasetRelease {
              kb_version: $kb_version,
              status: 'active'
            })
            RETURN catalog.dataset_id AS catalog_dataset_id,
                   catalog.dataset_hash AS catalog_dataset_hash,
                   coalesce(catalog.place_count, release.place_count)
                     AS expected_place_count,
                   catalog.semantic_index_status AS semantic_index_status,
                   release.id AS release_id,
                   release.dataset_id AS release_dataset_id,
                   release.dataset_hash AS release_dataset_hash,
                   release.place_count AS release_place_count
            """,
            kb_version=self.kb_version,
        )
        issues: list[str] = []
        if len(release_rows) != 1:
            return {
                "ready": False,
                "semantic_index_status": "unknown",
                "issues": ["active_release_count"],
            }
        release = release_rows[0]
        if release.get("catalog_dataset_id") != release.get("release_dataset_id"):
            issues.append("dataset_id_mismatch")
        if release.get("catalog_dataset_hash") != release.get("release_dataset_hash"):
            issues.append("dataset_hash_mismatch")
        expected = int(release.get("expected_place_count") or -1)
        release_count = int(release.get("release_place_count") or -1)
        if expected < 1 or expected != release_count:
            issues.append("release_place_count_mismatch")

        count_rows = self.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            RETURN count(place) AS place_count
            """,
            kb_version=self.kb_version,
        )
        actual = int(count_rows[0].get("place_count", -1)) if count_rows else -1
        if actual != expected:
            issues.append("active_place_count_mismatch")
        semantic_status = str(
            release.get("semantic_index_status") or "pending"
        )
        if semantic_status not in {"pending", "ready"}:
            issues.append("semantic_index_status_invalid")
        return {
            "ready": not issues,
            "release_id": release.get("release_id"),
            "dataset_id": release.get("catalog_dataset_id"),
            "place_count": actual,
            "expected_place_count": expected,
            "release_place_count": release_count,
            "semantic_index_status": semantic_status,
            "semantic_ready": semantic_status == "ready",
            "issues": issues,
        }

    def personalized_candidates(
        self,
        *,
        seed_place_ids: list[str],
        preferred_concepts: list[str],
        excluded_concepts: list[str],
        excluded_place_ids: list[str],
        preferred_cities: list[str],
        limit: int,
        preferred_entity_types: list[str] | None = None,
        excluded_entity_types: list[str] | None = None,
        preferred_categories: list[str] | None = None,
        excluded_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.run(
            """
            OPTIONAL MATCH (seed:Place {kb_version: $kb_version})
            WHERE seed.id IN $seed_place_ids
            OPTIONAL MATCH (seed)-[:HAS_OFFERING*0..1]->(seedSubject)-[]->
                           (seedConcept:Concept {kb_version: $kb_version})
            WITH collect(DISTINCT toLower(coalesce(
                   seedConcept.canonical_name, seedConcept.name
                 ))) AS seedConcepts
            MATCH (candidate:Place {kb_version: $kb_version})
            WHERE NOT candidate.id IN $excluded_place_ids
              AND (
                size($preferred_cities) = 0
                OR candidate.city IN $preferred_cities
              )
            OPTIONAL MATCH (candidate)-[:HAS_OFFERING*0..1]->(subject)-[]->
                           (concept:Concept {kb_version: $kb_version})
            WITH candidate, seedConcepts,
                 collect(DISTINCT toLower(coalesce(
                   concept.canonical_name, concept.name
                 ))) AS candidateConcepts
            WITH candidate, seedConcepts,
                 reduce(
                   uniqueFeatures = [],
                   value IN candidateConcepts + [
                     toLower(coalesce(candidate.entity_type, '')),
                     toLower(coalesce(candidate.category, ''))
                   ] |
                   CASE
                     WHEN value = '' OR value IN uniqueFeatures THEN uniqueFeatures
                     ELSE uniqueFeatures + value
                   END
                 ) AS candidateFeatures,
                 [value IN $preferred_concepts
                    + $preferred_entity_types
                    + $preferred_categories | toLower(value)] AS preferredFeatures,
                 [value IN $excluded_concepts
                    + $excluded_entity_types
                    + $excluded_categories | toLower(value)] AS excludedFeatures
            WITH candidate, seedConcepts, candidateFeatures, preferredFeatures,
                 size([value IN candidateFeatures WHERE value IN seedConcepts]) AS sharedCount,
                 size([value IN candidateFeatures WHERE value IN preferredFeatures]) AS preferredCount,
                 excludedFeatures,
                 reduce(
                   uniqueFeatures = [],
                   value IN seedConcepts + preferredFeatures |
                   CASE
                     WHEN value IN uniqueFeatures THEN uniqueFeatures
                     ELSE uniqueFeatures + value
                   END
                 ) AS profileFeatures
            WHERE none(value IN candidateFeatures WHERE value IN excludedFeatures)
            WITH candidate, candidateFeatures, sharedCount, preferredCount,
                 CASE
                   WHEN sharedCount > 0 THEN 'similar_to_recent_place'
                   WHEN preferredCount > 0 THEN 'matches_preference'
                   WHEN size($preferred_cities) > 0 THEN 'preferred_city'
                   ELSE 'popular'
                 END AS reasonCode,
                 CASE
                   WHEN size(profileFeatures) > 0
                     THEN toFloat(size([
                       value IN candidateFeatures WHERE value IN profileFeatures
                     ])) / size(profileFeatures)
                   ELSE CASE
                     WHEN coalesce(toFloat(candidate.rating), 0.0) > 5.0
                       THEN coalesce(toFloat(candidate.rating), 0.0) / 10.0
                     ELSE coalesce(toFloat(candidate.rating), 0.0) / 5.0
                   END
                 END AS recommendationScore
            ORDER BY recommendationScore DESC,
                     coalesce(candidate.rating, 0) DESC,
                     coalesce(candidate.review_count, 0) DESC,
                     candidate.name ASC
            WITH candidate.entity_type AS entityGroup,
                 collect({
                   candidate: candidate,
                   score: recommendationScore,
                   reasonCode: reasonCode
                 })[0..$per_type_limit] AS rankedGroup
            UNWIND rankedGroup AS ranked
            WITH ranked, ranked.candidate AS rankedCandidate
            RETURN rankedCandidate.id AS place_id,
                   rankedCandidate.name AS name,
                   rankedCandidate.city AS city,
                   rankedCandidate.entity_type AS entity_type,
                   rankedCandidate.category AS category,
                   ranked.score AS score,
                   ranked.reasonCode AS reason_code,
                   CASE ranked.reasonCode
                     WHEN 'similar_to_recent_place'
                       THEN 'Tương tự những địa điểm bạn từng quan tâm'
                     WHEN 'matches_preference'
                       THEN 'Phù hợp với sở thích bạn đã chọn'
                     WHEN 'preferred_city'
                       THEN 'Phù hợp với thành phố bạn quan tâm'
                     ELSE 'Được đánh giá tốt trong dữ liệu NexTripAI'
                   END AS reason,
                   rankedCandidate {
                     .rating, .review_count, .address, .description,
                     .opening_hours_open, .opening_hours_close,
                     .opening_hours_note, .price_per_night_min,
                     .price_per_night_max, .price_per_person_min,
                     .price_per_person_max, .drink_price_min,
                     .drink_price_max, .entry_fee_min, .entry_fee_max,
                     .source_url
                   } AS attributes
            """,
            kb_version=self.kb_version,
            seed_place_ids=seed_place_ids,
            preferred_concepts=preferred_concepts,
            excluded_concepts=excluded_concepts,
            preferred_entity_types=preferred_entity_types or [],
            excluded_entity_types=excluded_entity_types or [],
            preferred_categories=preferred_categories or [],
            excluded_categories=excluded_categories or [],
            excluded_place_ids=excluded_place_ids,
            preferred_cities=preferred_cities,
            per_type_limit=max(limit, 3),
        )
        return _diversify_by_entity_type(rows, limit)

    def places_by_ids(self, place_ids: list[str]) -> list[dict[str, Any]]:
        return self.run(
            """
            UNWIND range(0, size($place_ids) - 1) AS position
            MATCH (place:Place {
              id: $place_ids[position],
              kb_version: $kb_version
            })
            RETURN place.id AS place_id,
                   place.name AS name,
                   place.city AS city,
                   place.entity_type AS entity_type,
                   place.category AS category,
                   CASE
                     WHEN coalesce(toFloat(place.rating), 0.0) > 5.0
                       THEN coalesce(toFloat(place.rating), 0.0) / 10.0
                     ELSE coalesce(toFloat(place.rating), 0.0) / 5.0
                   END AS score,
                   'popular' AS reason_code,
                   'Địa điểm bạn đã lưu' AS reason,
                   place {
                     .rating, .review_count, .address, .description,
                     .opening_hours_open, .opening_hours_close,
                     .opening_hours_note, .price_per_night_min,
                     .price_per_night_max, .price_per_person_min,
                     .price_per_person_max, .drink_price_min,
                     .drink_price_max, .entry_fee_min, .entry_fee_max,
                     .source_url
                   } AS attributes
            ORDER BY position
            """,
            kb_version=self.kb_version,
            place_ids=place_ids,
        )

    def project_from_v5(self, *, replace: bool = False) -> dict[str, int]:
        """Legacy in-database V5 projection; not used by canonical V8 releases."""
        self._require_legacy_projection_source()
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
            CREATE RANGE INDEX entity_id IF NOT EXISTS
            FOR (node:Entity) ON (node.id)
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
            MATCH (source_copy:Entity {
              id: $id_prefix + toString(source.id),
              kb_version: $kb_version
            })
            MATCH (target_copy:Entity {
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

    def _require_legacy_projection_source(self) -> None:
        canonical_rows = self.run(
            """
            MATCH (release:DatasetRelease {kb_version: $kb_version})
            RETURN count(release) AS releases
            """,
            kb_version=self.kb_version,
        )
        if canonical_rows and int(canonical_rows[0].get("releases", 0)):
            raise RuntimeError(
                "refusing legacy v8-project on a canonical-managed V8 database"
            )
        source_rows = self.run(
            """
            MATCH (catalog:TravelCatalog {
              kb_version: $source_kb_version,
              status: 'ready'
            })
            OPTIONAL MATCH (node {kb_version: $source_kb_version})
            RETURN count(DISTINCT catalog) AS catalogs,
                   count(DISTINCT node) AS nodes
            """,
            source_kb_version=self.source_kb_version,
        )
        row = source_rows[0] if source_rows else {}
        if int(row.get("catalogs", 0)) != 1 or int(row.get("nodes", 0)) == 0:
            raise RuntimeError(
                "legacy v8-project requires one ready V5 catalog with source nodes"
            )

    def _ensure_search_schema(self) -> None:
        """Ensure the isolated projection's business labels and search indexes."""
        self.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
            SET place:Place
            """,
            kb_version=self.kb_version,
        )
        self.run(
            """
            MATCH (concept:Concept {kb_version: $kb_version})
            SET concept:Concept
            """,
            kb_version=self.kb_version,
        )
        self.run(
            """
            CREATE RANGE INDEX place_city IF NOT EXISTS
            FOR (place:Place) ON (place.city)
            """
        )
        dimensions = self.run(
            """
            MATCH (place:Place {kb_version: $kb_version})
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
            MATCH (concept:Concept {kb_version: $kb_version})
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
                label="Concept",
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


def _diversify_by_entity_type(
    rows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    positive = [row for row in rows if float(row["score"]) > 0]
    fallback = [row for row in rows if float(row["score"]) <= 0]
    return [*_interleave_entity_types(positive), *_interleave_entity_types(fallback)][
        :limit
    ]


def _interleave_entity_types(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["entity_type"]), []).append(row)
    ranked_groups = sorted(
        groups.values(),
        key=lambda group: float(group[0]["score"]),
        reverse=True,
    )
    interleaved = chain.from_iterable(zip_longest(*ranked_groups))
    return [row for row in interleaved if row is not None]

__all__ = ["V8GraphStore"]
