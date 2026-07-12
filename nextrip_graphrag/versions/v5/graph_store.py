from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ...config import (
    V5_AREA_PROXIMITY_CONFIDENCE,
    V5_AREA_PROXIMITY_RADIUS_KM,
)
from ...neo4j_store import chunks
from ..v3.graph_store import FACET_SPECS
from ..v4.graph_store import V4GraphStore
from .geo import extract_geo_area_candidates, verified_address_area_vocabulary


class V5GraphStore(V4GraphStore):
    kb_version = "v5"

    def ensure_v5_schema(self, embedding_dim: int) -> None:
        self.ensure_typed_schema(embedding_dim, index_prefix="v5")
        labels = {spec[0] for spec in FACET_SPECS.values()} | {
            "VenueType",
            "HotelStyle",
            "Concept",
            "Offering",
            "Claim",
            "DataConflict",
            "Community",
            "CommunityReport",
            "DynamicObservation",
            "GeoArea",
        }
        for label in labels:
            self.run(
                f"CREATE CONSTRAINT v5_{label.lower()}_id IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
        self.run(
            "CREATE FULLTEXT INDEX v5_concept_fulltext IF NOT EXISTS "
            "FOR (n:Concept) ON EACH [n.name, n.canonical_name]"
        )
        self.run(
            "CREATE FULLTEXT INDEX v5_geo_area_fulltext IF NOT EXISTS "
            "FOR (n:GeoArea) ON EACH [n.name, n.aliases]"
        )
        self.run(
            f"""
            CREATE VECTOR INDEX v5_concept_embedding IF NOT EXISTS
            FOR (n:Concept) ON (n.embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {embedding_dim},
              `vector.similarity_function`: 'cosine'
            }}}}
            """
        )

    def replace_graph(
        self,
        cities: list[dict[str, Any]],
        places: list[dict[str, Any]],
        embedder: Any | None = None,
        batch_size: int = 16,
    ) -> dict[str, int]:
        super().replace_graph(cities, places, embedder=embedder, batch_size=batch_size)
        if embedder is not None:
            self._embed_concepts(embedder, batch_size)
        return self.graph_statistics()

    def after_places_loaded(self, places: list[dict[str, Any]]) -> None:
        super().after_places_loaded(places)
        self._load_geo_areas(places)
        self._load_near_area_edges()

    def _load_geo_areas(self, places: list[dict[str, Any]]) -> None:
        self.run_versioned(
            """
            MATCH (area:GeoArea {kb_version: $kb_version})
            DETACH DELETE area
            """
        )
        verified_address_areas = verified_address_area_vocabulary(places)
        candidates = [
            asdict(candidate)
            for place in places
            for candidate in extract_geo_area_candidates(
                place,
                verified_address_areas,
            )
        ]
        for batch in chunks(candidates, 500):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (city:City {id: row.city_id, kb_version: $kb_version})
                MERGE (area:GeoArea {id: row.area_id})
                ON CREATE SET area.name = row.name,
                              area.normalized_name = toLower(row.name),
                              area.aliases = [row.name]
                SET area.aliases = CASE
                      WHEN row.name IN coalesce(area.aliases, []) THEN area.aliases
                      ELSE coalesce(area.aliases, []) + row.name
                    END,
                    area.area_type = row.area_type,
                    area.kb_version = $kb_version
                MERGE (city)-[:HAS_AREA]->(area)
                """,
                rows=batch,
            )

        located = [row for row in candidates if row["relation"] == "located_in"]
        for batch in chunks(located, 500):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (place:Place {id: row.place_id, kb_version: $kb_version})
                MATCH (area:GeoArea {id: row.area_id, kb_version: $kb_version})
                MERGE (place)-[located:LOCATED_IN]->(area)
                SET located.confidence = row.confidence,
                    located.evidence_id = row.evidence_id,
                    located.extraction_method = row.extraction_method
                """,
                rows=batch,
            )
        mentions = [row for row in candidates if row["relation"] == "mentions"]
        for batch in chunks(mentions, 500):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (unit:TextUnit {id: row.evidence_id, kb_version: $kb_version})
                MATCH (area:GeoArea {id: row.area_id, kb_version: $kb_version})
                MERGE (unit)-[mention:MENTIONS_GEO_AREA]->(area)
                SET mention.confidence = row.confidence,
                    mention.extraction_method = row.extraction_method
                """,
                rows=batch,
            )

    def _load_near_area_edges(self) -> None:
        self.run_versioned(
            """
            MATCH (:Place {kb_version: $kb_version})-[nearArea:NEAR_AREA]->
                  (:GeoArea {kb_version: $kb_version})
            DELETE nearArea
            """
        )
        self.run_versioned(
            """
            MATCH (anchor:Place {kb_version: $kb_version})-[:LOCATED_IN]->
                  (area:GeoArea {kb_version: $kb_version})
            MATCH (candidate:Place {kb_version: $kb_version})
            WHERE candidate <> anchor
              AND candidate.city = anchor.city
              AND candidate.location IS NOT NULL
              AND anchor.location IS NOT NULL
              AND NOT EXISTS { (candidate)-[:LOCATED_IN]->(area) }
            WITH candidate, area, anchor,
                 point.distance(candidate.location, anchor.location) / 1000.0 AS distanceKm
            WHERE distanceKm <= $radius_km
            WITH candidate, area,
                 min(distanceKm) AS minimumDistanceKm,
                 collect(DISTINCT anchor.id) AS anchorIds
            MERGE (candidate)-[nearArea:NEAR_AREA]->(area)
            SET nearArea.distance_km = round(minimumDistanceKm, 2),
                nearArea.anchor_place_ids = anchorIds,
                nearArea.confidence = $confidence,
                nearArea.derivation = 'point_distance_from_verified_area_anchor'
            """,
            radius_km=V5_AREA_PROXIMITY_RADIUS_KM,
            confidence=V5_AREA_PROXIMITY_CONFIDENCE,
        )

    def planner_catalog(self) -> dict[str, list[str]]:
        rows = self.run_versioned(
            """
            OPTIONAL MATCH (city:City {kb_version: $kb_version})
            WITH collect(DISTINCT city.name) AS cities
            OPTIONAL MATCH (area:GeoArea {kb_version: $kb_version})
            WITH cities, collect(DISTINCT area.name) AS areas
            OPTIONAL MATCH (concept:Concept {kb_version: $kb_version})
            WITH cities, areas,
                 collect(DISTINCT concept.canonical_name) AS concepts
            OPTIONAL MATCH (place:Place {kb_version: $kb_version})
            RETURN cities, areas, concepts,
                   collect(DISTINCT place.name) AS places
            """
        )
        row = rows[0]
        return {
            key: sorted(str(value) for value in row[key] if value)
            for key in ("cities", "areas", "concepts", "places")
        }

    def semantic_concept_candidates(
        self,
        embedding: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.run_versioned(
            """
            CALL db.index.vector.queryNodes('v5_concept_embedding', $limit, $embedding)
            YIELD node, score
            WHERE node.kb_version = $kb_version
            RETURN node.id AS concept_id,
                   node.name AS name,
                   node.canonical_name AS canonical_name,
                   node.concept_type AS concept_type,
                   node.domain AS domain,
                   score
            ORDER BY score DESC
            """,
            embedding=embedding,
            limit=limit,
        )

    def _embed_concepts(self, embedder: Any, batch_size: int) -> None:
        rows = self.run_versioned(
            """
            MATCH (concept:Concept {kb_version: $kb_version})
            OPTIONAL MATCH (claim:Claim {kb_version: $kb_version})-[:OBJECT]->(concept)
            WITH concept, collect(DISTINCT claim.evidence_text)[0..5] AS evidence_samples
            RETURN concept.id AS id,
                   concept.name AS name,
                   concept.canonical_name AS canonical_name,
                   concept.concept_type AS concept_type,
                   concept.domain AS domain,
                   evidence_samples
            ORDER BY concept.id
            """
        )
        for batch in chunks(rows, batch_size):
            texts = [_concept_semantic_text(row) for row in batch]
            embeddings = embedder.embed_documents(texts)
            updates = [
                {
                    "id": row["id"],
                    "semantic_text": text,
                    "embedding": embedding,
                }
                for row, text, embedding in zip(batch, texts, embeddings, strict=True)
            ]
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (concept:Concept {id: row.id, kb_version: $kb_version})
                SET concept.semantic_text = row.semantic_text,
                    concept.embedding = row.embedding
                """,
                rows=updates,
            )

    def graph_statistics(self) -> dict[str, int]:
        statistics = super().graph_statistics()
        row = self.run_versioned(
            """
            OPTIONAL MATCH (area:GeoArea {kb_version: $kb_version})
            WITH count(area) AS geo_areas
            OPTIONAL MATCH (:Place {kb_version: $kb_version})-[located:LOCATED_IN]->(:GeoArea)
            WITH geo_areas, count(located) AS location_edges
            OPTIONAL MATCH (:TextUnit {kb_version: $kb_version})-[mention:MENTIONS_GEO_AREA]->(:GeoArea)
            WITH geo_areas, location_edges, count(mention) AS geo_mentions
            OPTIONAL MATCH (:Place {kb_version: $kb_version})-[nearArea:NEAR_AREA]->(:GeoArea)
            RETURN geo_areas, location_edges, geo_mentions,
                   count(nearArea) AS near_area_edges
            """
        )[0]
        statistics.update({key: int(value) for key, value in row.items()})
        embedded = self.run_versioned(
            """
            MATCH (concept:Concept {kb_version: $kb_version})
            RETURN count(CASE WHEN concept.embedding IS NOT NULL THEN 1 END) AS embedded_concepts
            """
        )[0]
        statistics["embedded_concepts"] = int(embedded["embedded_concepts"])
        return statistics

    def validate_invariants(self, expected_places: int | None = None) -> dict[str, Any]:
        report = super().validate_invariants(expected_places)
        row = self.run_versioned(
            """
            OPTIONAL MATCH (area:GeoArea {kb_version: $kb_version})
            WITH count(area) AS areas,
                 count(CASE WHEN count { (:City)-[:HAS_AREA]->(area) } <> 1 THEN 1 END) AS badParents
            OPTIONAL MATCH (:Place {kb_version: $kb_version})-[located:LOCATED_IN]->(:GeoArea)
            WITH areas, badParents, count(located) AS locations,
                 count(CASE WHEN located.evidence_id IS NULL OR located.confidence IS NULL THEN 1 END) AS badLocations
            OPTIONAL MATCH (:TextUnit {kb_version: $kb_version})-[mention:MENTIONS_GEO_AREA]->(:GeoArea)
            WITH areas, badParents, locations, badLocations, count(mention) AS mentions
            OPTIONAL MATCH (:Place {kb_version: $kb_version})-[nearArea:NEAR_AREA]->(:GeoArea)
            WITH areas, badParents, locations, badLocations, mentions,
                 count(nearArea) AS nearAreas,
                 count(CASE WHEN nearArea.distance_km IS NULL
                                  OR nearArea.confidence IS NULL
                                  OR nearArea.anchor_place_ids IS NULL
                            THEN 1 END) AS badNearAreas
            OPTIONAL MATCH (concept:Concept {kb_version: $kb_version})
            RETURN areas, badParents, locations, badLocations, mentions,
                   nearAreas, badNearAreas,
                   count(concept) AS concepts,
                   count(CASE WHEN concept.embedding IS NOT NULL THEN 1 END) AS embeddedConcepts
            """
        )[0]
        checks = {
            "geo_areas_created": row["areas"] > 0,
            "geo_areas_have_single_city_parent": row["badParents"] == 0,
            "location_edges_are_grounded": row["badLocations"] == 0,
            "geo_mentions_created": row["mentions"] > 0,
            "near_area_edges_are_derived": (
                row["nearAreas"] > 0 and row["badNearAreas"] == 0
            ),
            "all_concepts_embedded": (
                row["concepts"] > 0 and row["embeddedConcepts"] == row["concepts"]
            ),
        }
        report["checks"].update(checks)
        report["counts"].update({key: int(value) for key, value in row.items()})
        report["status"] = "pass" if all(report["checks"].values()) else "fail"
        return report


def _concept_semantic_text(row: dict[str, Any]) -> str:
    values = (
        row["name"],
        row["canonical_name"],
        row["concept_type"],
        row["domain"],
        *row["evidence_samples"],
    )
    return " | ".join(str(value) for value in values if value)[:4000]
