from __future__ import annotations

from hashlib import sha256
from typing import Any

from ...neo4j_store import Neo4jGraphStore, chunks
from .ontology import entity_profile, extract_facts


class V2GraphStore(Neo4jGraphStore):
    kb_version = "v2"

    def ensure_v2_schema(self, embedding_dim: int) -> None:
        self.ensure_typed_schema(embedding_dim, index_prefix="v2")

    def ensure_typed_schema(self, embedding_dim: int, *, index_prefix: str) -> None:
        statements = [
            f"CREATE CONSTRAINT {index_prefix}_catalog_id IF NOT EXISTS FOR (n:TravelCatalog) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_city_id IF NOT EXISTS FOR (n:City) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_type_id IF NOT EXISTS FOR (n:PlaceType) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_place_id IF NOT EXISTS FOR (n:Place) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_category_id IF NOT EXISTS FOR (n:Category) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_fact_id IF NOT EXISTS FOR (n:Fact) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_document_id IF NOT EXISTS FOR (n:Document) REQUIRE n.id IS UNIQUE",
            f"CREATE CONSTRAINT {index_prefix}_text_unit_id IF NOT EXISTS FOR (n:TextUnit) REQUIRE n.id IS UNIQUE",
            f"CREATE FULLTEXT INDEX {index_prefix}_place_fulltext IF NOT EXISTS FOR (n:Place) ON EACH [n.name, n.aliases, n.entity_profile]",
            f"""
            CREATE VECTOR INDEX {index_prefix}_place_embedding IF NOT EXISTS
            FOR (n:Place) ON (n.embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {embedding_dim},
              `vector.similarity_function`: 'cosine'
            }}}}
            """,
        ]
        for statement in statements:
            self.run(statement)

    def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return self.run(query, kb_version=self.kb_version, **params)

    def facts_for_place(self, place: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_facts(place)

    def profile_for_place(self, place: dict[str, Any]) -> str:
        return entity_profile(place)

    def replace_graph(
        self,
        cities: list[dict[str, Any]],
        places: list[dict[str, Any]],
        embedder: Any | None = None,
        batch_size: int = 16,
    ) -> dict[str, int]:
        self.run_versioned("MATCH (n {kb_version: $kb_version}) DETACH DELETE n")
        self.run_versioned(
            """
            CREATE (catalog:TravelCatalog {
              id: 'nextrip-travel-catalog',
              name: 'NexTripAI Travel Catalog',
              status: 'building',
              kb_version: $kb_version
            })
            WITH catalog
            UNWIND $cities AS city
            CREATE (c:City)
            SET c = city, c.kb_version = $kb_version
            CREATE (catalog)-[:HAS_CITY {place_count_snapshot: 0}]->(c)
            """,
            cities=cities,
        )

        for batch in chunks(places, batch_size):
            profiles = [self.profile_for_place(place) for place in batch]
            embeddings = embedder.embed_documents(profiles) if embedder else [None] * len(batch)
            for place, profile, embedding in zip(batch, profiles, embeddings, strict=True):
                self._upsert_place(place, profile, embedding)

        self._load_nearby(places)
        self.after_places_loaded(places)
        self.refresh_count_snapshots()
        self.run_versioned(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            SET catalog.status = 'ready', catalog.built_at = datetime()
            """
        )
        return self.graph_statistics()

    def after_places_loaded(self, places: list[dict[str, Any]]) -> None:
        return None

    def _upsert_place(
        self,
        place: dict[str, Any],
        profile: str,
        embedding: list[float] | None,
    ) -> None:
        props = dict(place["props"])
        props.update(
            {
                "kb_version": self.kb_version,
                "entity_profile": profile,
                "category_name": place.get("category_name"),
            }
        )
        facts = self.facts_for_place(place)
        document, text_unit = _evidence_nodes(place, profile, self.kb_version)
        self.run_versioned(
            """
            MATCH (city:City {id: $city_id, kb_version: $kb_version})
            MERGE (type:PlaceType {id: $type_id})
            SET type.name = $type_name, type.kb_version = $kb_version
            MERGE (category:Category {id: $category_id})
            SET category.name = $category_name,
                category.value = $category_value,
                category.kb_version = $kb_version
            CREATE (place:Place)
            SET place = $props
            MERGE (city)-[:HAS_PLACE_TYPE {place_count_snapshot: 0}]->(type)
            CREATE (type)-[:CONTAINS_PLACE]->(place)
            CREATE (place)-[:IN_CITY]->(city)
            CREATE (place)-[:HAS_CATEGORY]->(category)
            MERGE (document:Document {id: $document.id})
            SET document += $document
            CREATE (textUnit:TextUnit)
            SET textUnit = $text_unit
            CREATE (textUnit)-[:PART_OF]->(document)
            CREATE (textUnit)-[:MENTIONS]->(place)
            WITH place, textUnit
            UNWIND $facts AS factData
            CREATE (fact:Fact)
            SET fact = factData, fact.kb_version = $kb_version
            CREATE (place)-[:HAS_FACT]->(fact)
            CREATE (fact)-[:SUPPORTED_BY]->(textUnit)
            """,
            city_id=place["city_id"],
            type_id=place["place_type_id"],
            type_name=place["place_type_name"],
            category_id=place["category_id"],
            category_name=place["category_name"],
            category_value=place["category_value"],
            props=props,
            document=document,
            text_unit=text_unit,
            facts=facts,
        )
        if props.get("lat") is not None and props.get("lng") is not None:
            self.run_versioned(
                """
                MATCH (place:Place {id: $place_id, kb_version: $kb_version})
                SET place.location = point({latitude: $lat, longitude: $lng})
                """,
                place_id=place["id"],
                lat=props["lat"],
                lng=props["lng"],
            )
        if embedding is not None:
            self.run_versioned(
                """
                MATCH (place:Place {id: $place_id, kb_version: $kb_version})
                CALL db.create.setNodeVectorProperty(place, 'embedding', $embedding)
                """,
                place_id=place["id"],
                embedding=embedding,
            )

    def _load_nearby(self, places: list[dict[str, Any]]) -> None:
        relationships = [
            {
                "from_id": place["id"],
                "to_id": nearby["id"],
                "distance_km": nearby.get("distance_km"),
            }
            for place in places
            for nearby in place.get("nearby_attractions", [])
            if nearby.get("id")
        ]
        if not relationships:
            return
        self.run_versioned(
            """
            UNWIND $relationships AS item
            MATCH (source:Place {id: item.from_id, kb_version: $kb_version})
            MATCH (target:Place {id: item.to_id, kb_version: $kb_version})
            MERGE (source)-[near:NEAR]->(target)
            SET near.distance_km = item.distance_km
            """,
            relationships=relationships,
        )

    def refresh_count_snapshots(self) -> None:
        self.run_versioned(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})-[rel:HAS_CITY]->(city:City)
            OPTIONAL MATCH (place:Place {kb_version: $kb_version})-[:IN_CITY]->(city)
            WITH rel, count(DISTINCT place) AS placeCount
            SET rel.place_count_snapshot = placeCount
            """
        )
        self.run_versioned(
            """
            MATCH (city:City {kb_version: $kb_version})-[rel:HAS_PLACE_TYPE]->(type:PlaceType)
            OPTIONAL MATCH (type)-[:CONTAINS_PLACE]->(place:Place)-[:IN_CITY]->(city)
            WITH rel, count(DISTINCT place) AS placeCount
            SET rel.place_count_snapshot = placeCount
            """
        )

    def graph_statistics(self) -> dict[str, int]:
        row = self.run_versioned(
            """
            MATCH (place:Place {kb_version: $kb_version})
            WITH count(place) AS places
            MATCH (fact:Fact {kb_version: $kb_version})
            WITH places, count(fact) AS facts
            MATCH (textUnit:TextUnit {kb_version: $kb_version})
            RETURN places, facts, count(textUnit) AS text_units
            """
        )[0]
        return {key: int(value) for key, value in row.items()}

    def validate_invariants(self, expected_places: int = 519) -> dict[str, Any]:
        counts = self.run_versioned(
            """
            MATCH (city:City {kb_version: $kb_version})
            WITH count(city) AS cities
            MATCH (place:Place {kb_version: $kb_version})
            WITH cities, count(place) AS places,
                 count(CASE WHEN place.embedding IS NOT NULL THEN 1 END) AS embedded_places
            OPTIONAL MATCH (orphanFact:Fact {kb_version: $kb_version})
            WHERE NOT (orphanFact)-[:SUPPORTED_BY]->(:TextUnit)
            WITH cities, places, embedded_places, count(orphanFact) AS facts_without_evidence
            OPTIONAL MATCH (badPlace:Place {kb_version: $kb_version})
            WITH cities, places, embedded_places, facts_without_evidence, badPlace,
                 count { (badPlace)-[:IN_CITY]->(:City) } AS cityEdges,
                 count { (:PlaceType)-[:CONTAINS_PLACE]->(badPlace) } AS typeEdges
            WITH cities, places, embedded_places, facts_without_evidence,
                 sum(CASE WHEN cityEdges <> 1 OR typeEdges <> 1 THEN 1 ELSE 0 END) AS invalid_places
            RETURN cities, places, embedded_places, facts_without_evidence, invalid_places
            """
        )[0]
        snapshots = self.run_versioned(
            """
            MATCH (city:City {kb_version: $kb_version})-[rel:HAS_PLACE_TYPE]->(type:PlaceType)
            OPTIONAL MATCH (type)-[:CONTAINS_PLACE]->(place:Place)-[:IN_CITY]->(city)
            WITH rel, count(DISTINCT place) AS liveCount
            RETURN sum(CASE WHEN rel.place_count_snapshot = liveCount THEN 0 ELSE 1 END) AS mismatches
            """
        )[0]["mismatches"]
        checks = {
            "two_cities": counts["cities"] == 2,
            "expected_places": counts["places"] == expected_places,
            "single_city_and_type_per_place": counts["invalid_places"] == 0,
            "all_facts_have_evidence": counts["facts_without_evidence"] == 0,
            "count_snapshots_match": snapshots == 0,
        }
        return {
            "status": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "counts": counts,
            "snapshot_mismatches": snapshots,
        }


def _evidence_nodes(
    place: dict[str, Any],
    profile: str,
    kb_version: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    props = place["props"]
    verified_source = next(iter(place.get("verified_sources") or []), {})
    url = verified_source.get("url") or props.get("source_url")
    source_name = verified_source.get("name") or props.get("source_name")
    source_key = url or f"verified-record:{place['id']}"
    document_id = f"doc:{sha256(source_key.encode('utf-8')).hexdigest()[:20]}"
    text_unit_id = f"text-unit:verified:{place['id']}"
    return (
        {
            "id": document_id,
            "title": source_name or "Verified travel record",
            "url": url,
            "source_name": source_name,
            "kb_version": kb_version,
        },
        {
            "id": text_unit_id,
            "title": props.get("name"),
            "text": profile,
            "evidence_origin": "verified_record",
            "confidence": 0.95
            if props.get("last_verified") or props.get("verification_status")
            else 0.7,
            "kb_version": kb_version,
        },
    )
