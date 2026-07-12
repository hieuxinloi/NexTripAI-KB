from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ...neo4j_store import chunks
from ..v3.graph_store import FACET_SPECS
from ..v4.graph_store import V4GraphStore
from .geo import extract_geo_area_candidates


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

    def after_places_loaded(self, places: list[dict[str, Any]]) -> None:
        super().after_places_loaded(places)
        self._load_geo_areas(places)

    def _load_geo_areas(self, places: list[dict[str, Any]]) -> None:
        candidates = [
            asdict(candidate)
            for place in places
            for candidate in extract_geo_area_candidates(place)
        ]
        for batch in chunks(candidates, 500):
            self.run_versioned(
                """
                UNWIND $rows AS row
                MATCH (city:City {id: row.city_id, kb_version: $kb_version})
                MERGE (area:GeoArea {id: row.area_id})
                SET area.name = row.name,
                    area.normalized_name = toLower(row.name),
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

    def planner_catalog(self) -> dict[str, list[str]]:
        rows = self.run_versioned(
            """
            OPTIONAL MATCH (city:City {kb_version: $kb_version})
            WITH collect(DISTINCT city.name) AS cities
            OPTIONAL MATCH (area:GeoArea {kb_version: $kb_version})
            WITH cities, collect(DISTINCT area.name) AS areas
            OPTIONAL MATCH (concept:Concept {kb_version: $kb_version})
            RETURN cities, areas,
                   collect(DISTINCT concept.canonical_name) AS concepts
            """
        )
        row = rows[0]
        return {
            key: sorted(str(value) for value in row[key] if value)
            for key in ("cities", "areas", "concepts")
        }

    def graph_statistics(self) -> dict[str, int]:
        statistics = super().graph_statistics()
        row = self.run_versioned(
            """
            OPTIONAL MATCH (area:GeoArea {kb_version: $kb_version})
            WITH count(area) AS geo_areas
            OPTIONAL MATCH (:Place {kb_version: $kb_version})-[located:LOCATED_IN]->(:GeoArea)
            WITH geo_areas, count(located) AS location_edges
            OPTIONAL MATCH (:TextUnit {kb_version: $kb_version})-[mention:MENTIONS_GEO_AREA]->(:GeoArea)
            RETURN geo_areas, location_edges, count(mention) AS geo_mentions
            """
        )[0]
        statistics.update({key: int(value) for key, value in row.items()})
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
            RETURN areas, badParents, locations, badLocations, count(mention) AS mentions
            """
        )[0]
        checks = {
            "geo_areas_created": row["areas"] > 0,
            "geo_areas_have_single_city_parent": row["badParents"] == 0,
            "location_edges_are_grounded": row["badLocations"] == 0,
            "geo_mentions_created": row["mentions"] > 0,
        }
        report["checks"].update(checks)
        report["counts"].update({key: int(value) for key, value in row.items()})
        report["status"] = "pass" if all(report["checks"].values()) else "fail"
        return report
