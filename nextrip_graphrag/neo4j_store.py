from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import Settings


ENTITY_LABELS = {
    "attraction": "Attraction",
    "cafe": "Cafe",
    "hotel": "Hotel",
    "nightlife": "Nightlife",
    "restaurant": "Restaurant",
}

TERM_RELATION_SPECS = {
    "ambience": ("Ambience", "HAS_AMBIENCE", "ambience"),
    "amenities": ("Amenity", "HAS_AMENITY", "amenity"),
    "cuisine": ("Cuisine", "HAS_CUISINE", "cuisine"),
    "dietary_options": ("DietaryOption", "HAS_DIETARY_OPTION", "dietary"),
    "drink_specialties": ("Drink", "HAS_DRINK_SPECIALTY", "drink"),
    "features": ("Feature", "HAS_FEATURE", "feature"),
    "highlights": ("Highlight", "HAS_HIGHLIGHT", "highlight"),
    "music_genres": ("MusicGenre", "HAS_MUSIC_GENRE", "music_genre"),
    "music_style": ("MusicStyle", "HAS_MUSIC_STYLE", "music_style"),
    "payment_methods": ("PaymentMethod", "ACCEPTS_PAYMENT", "payment"),
    "room_types": ("RoomType", "HAS_ROOM_TYPE", "room_type"),
    "serves": ("ServeTime", "SERVES", "serve"),
    "signature_dishes": ("Dish", "HAS_SIGNATURE_DISH", "dish"),
    "suitable_for": ("Audience", "SUITABLE_FOR", "audience"),
    "tags": ("Tag", "TAGGED_WITH", "tag"),
    "vibe": ("Vibe", "HAS_VIBE", "vibe"),
    "weather_suitable": ("Weather", "SUITABLE_WEATHER", "weather"),
}


def slugify_term(value: str) -> str:
    from .normalizer import slugify

    return slugify(value)


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Neo4jGraphStore:
    def __init__(self, settings: Settings):
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError(
                "Missing neo4j driver. Install dependencies with: pip install -r requirements.txt"
            ) from exc

        self.settings = settings
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    def close(self) -> None:
        self.driver.close()

    def _session(self):
        if self.settings.neo4j_database:
            return self.driver.session(database=self.settings.neo4j_database)
        return self.driver.session()

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        with self._session() as session:
            return session.run(query, **params).data()

    def ensure_schema(self, embedding_dim: int) -> None:
        if embedding_dim <= 0:
            raise ValueError("embedding_dim must be positive")

        statements = [
            "CREATE CONSTRAINT city_id IF NOT EXISTS FOR (c:City) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT place_id IF NOT EXISTS FOR (p:Place) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT category_id IF NOT EXISTS FOR (c:Category) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT place_type_id IF NOT EXISTS FOR (t:PlaceType) REQUIRE t.id IS UNIQUE",
            "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (s:Source) REQUIRE s.id IS UNIQUE",
            "CREATE CONSTRAINT term_id IF NOT EXISTS FOR (t:Term) REQUIRE t.id IS UNIQUE",
            "CREATE FULLTEXT INDEX place_fulltext IF NOT EXISTS FOR (p:Place) ON EACH [p.name, p.description, p.search_text, p.category_name, p.city]",
            f"""
            CREATE VECTOR INDEX place_embedding IF NOT EXISTS
            FOR (p:Place) ON (p.embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {embedding_dim},
              `vector.similarity_function`: 'cosine'
            }}}}
            """,
        ]
        for statement in statements:
            self.run(statement)

    def load_cities(self, cities: list[dict[str, Any]]) -> None:
        self.run(
            """
            UNWIND $cities AS city
            MERGE (c:City {id: city.id})
            SET c += city
            """,
            cities=cities,
        )

    def load_places(
        self,
        places: list[dict[str, Any]],
        embedder: Any | None = None,
        batch_size: int = 16,
    ) -> None:
        for batch in chunks(places, batch_size):
            embeddings: list[list[float] | None]
            if embedder:
                embeddings = embedder.embed_documents(
                    [place["props"]["search_text"] for place in batch]
                )
            else:
                embeddings = [None] * len(batch)

            for place, embedding in zip(batch, embeddings, strict=True):
                self.upsert_place(place, embedding)

        self.load_nearby_relationships(places)

    def upsert_place(self, place: dict[str, Any], embedding: list[float] | None) -> None:
        entity_label = ENTITY_LABELS.get(place["entity_type"], "TravelPlace")
        props = dict(place["props"])
        lat = props.get("lat")
        lng = props.get("lng")

        query = f"""
        MERGE (p:Place {{id: $id}})
        SET p:{entity_label}
        SET p += $props
        WITH p
        WHERE $lat IS NOT NULL AND $lng IS NOT NULL
        SET p.location = point({{latitude: $lat, longitude: $lng}})
        """
        self.run(query, id=place["id"], props=props, lat=lat, lng=lng)

        if embedding is not None:
            self.run(
                """
                MATCH (p:Place {id: $id})
                CALL db.create.setNodeVectorProperty(p, 'embedding', $embedding)
                """,
                id=place["id"],
                embedding=embedding,
            )

        self.upsert_core_relationships(place)
        self.upsert_term_relationships(place)

    def upsert_core_relationships(self, place: dict[str, Any]) -> None:
        props = place["props"]
        source_id = f"source_{slugify_term(props.get('source_name') or 'unknown')}"

        self.run(
            """
            MATCH (p:Place {id: $place_id})
            MATCH (city:City {id: $city_id})
            MERGE (city)-[:HAS_PLACE]->(p)
            MERGE (p)-[:IN_CITY]->(city)

            MERGE (type:PlaceType {id: $type_id})
            SET type.name = $type_name
            MERGE (city)-[:HAS_PLACE_TYPE]->(type)
            MERGE (type)-[:CONTAINS_PLACE]->(p)
            MERGE (p)-[:HAS_TYPE]->(type)

            MERGE (category:Category {id: $category_id})
            SET category.name = $category_name,
                category.value = $category_value
            MERGE (p)-[:HAS_CATEGORY]->(category)

            MERGE (source:Source {id: $source_id})
            SET source.name = $source_name,
                source.url = $source_url
            MERGE (p)-[:FROM_SOURCE]->(source)
            """,
            place_id=place["id"],
            city_id=place["city_id"],
            type_id=place["place_type_id"],
            type_name=place["place_type_name"],
            category_id=place["category_id"],
            category_name=place["category_name"],
            category_value=place["category_value"],
            source_id=source_id,
            source_name=props.get("source_name") or "unknown",
            source_url=props.get("source_url"),
        )

    def upsert_term_relationships(self, place: dict[str, Any]) -> None:
        for field, values in place.get("terms", {}).items():
            if not values:
                continue
            spec = TERM_RELATION_SPECS.get(field)
            if not spec:
                continue
            label, relationship, prefix = spec
            terms = [
                {
                    "id": f"{prefix}_{slugify_term(value)}",
                    "name": value,
                }
                for value in values
            ]
            query = f"""
            MATCH (p:Place {{id: $place_id}})
            UNWIND $terms AS term
            MERGE (t:Term:{label} {{id: term.id}})
            SET t.name = term.name
            MERGE (p)-[:{relationship}]->(t)
            """
            self.run(query, place_id=place["id"], terms=terms)

    def load_nearby_relationships(self, places: list[dict[str, Any]]) -> None:
        relationships = []
        for place in places:
            for nearby in place.get("nearby_attractions", []):
                if not nearby.get("id"):
                    continue
                relationships.append(
                    {
                        "from_id": place["id"],
                        "to_id": nearby["id"],
                        "distance_km": nearby.get("distance_km"),
                    }
                )
        if not relationships:
            return
        self.run(
            """
            UNWIND $relationships AS rel
            MATCH (a:Place {id: rel.from_id})
            MATCH (b:Place {id: rel.to_id})
            MERGE (a)-[r:NEAR]->(b)
            SET r.distance_km = rel.distance_km
            """,
            relationships=relationships,
        )

    def vector_search(
        self,
        embedding: list[float],
        limit: int,
        city_id: str | None = None,
        entity_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        candidate_k = max(limit * 8, 40)
        return self.run(
            """
            CALL db.index.vector.queryNodes('place_embedding', $candidate_k, $embedding)
            YIELD node, score
            MATCH (node)-[:IN_CITY]->(city:City)
            WHERE ($city_id IS NULL OR city.id = $city_id)
              AND (size($entity_types) = 0 OR node.entity_type IN $entity_types)
            OPTIONAL MATCH (node)-[:HAS_CATEGORY]->(category:Category)
            OPTIONAL MATCH (node)-[:TAGGED_WITH|HAS_AMENITY|HAS_FEATURE|HAS_CUISINE|SERVES|SUITABLE_FOR|HAS_SIGNATURE_DISH|HAS_MUSIC_STYLE|HAS_VIBE|SUITABLE_WEATHER|HAS_HIGHLIGHT|HAS_AMBIENCE|HAS_DIETARY_OPTION|HAS_DRINK_SPECIALTY|HAS_MUSIC_GENRE|ACCEPTS_PAYMENT|HAS_ROOM_TYPE]->(facet:Term)
            OPTIONAL MATCH (node)-[near_rel:NEAR]-(near:Place)
            WITH node, score, city, category,
                 collect(DISTINCT facet.name)[0..12] AS facets,
                 collect(DISTINCT near {
                    .id, .name, .entity_type, .entity_label, .category_name, .lat, .lng
                 })[0..5] AS nearby
            RETURN node {.*, embedding: null} AS place,
                   score,
                   city { .id, .name } AS city,
                   category { .id, .name } AS category,
                   facets,
                   nearby
            ORDER BY score DESC
            LIMIT $limit
            """,
            embedding=embedding,
            candidate_k=candidate_k,
            city_id=city_id,
            entity_types=entity_types or [],
            limit=limit,
        )

    def keyword_search(
        self,
        query_text: str,
        limit: int,
        city_id: str | None = None,
        entity_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.run(
            """
            CALL db.index.fulltext.queryNodes('place_fulltext', $query_text, {limit: $candidate_k})
            YIELD node, score
            MATCH (node)-[:IN_CITY]->(city:City)
            WHERE ($city_id IS NULL OR city.id = $city_id)
              AND (size($entity_types) = 0 OR node.entity_type IN $entity_types)
            OPTIONAL MATCH (node)-[:HAS_CATEGORY]->(category:Category)
            RETURN node {.*, embedding: null} AS place,
                   score,
                   city { .id, .name } AS city,
                   category { .id, .name } AS category,
                   [] AS facets,
                   [] AS nearby
            ORDER BY score DESC
            LIMIT $limit
            """,
            query_text=query_text,
            candidate_k=max(limit * 4, 20),
            city_id=city_id,
            entity_types=entity_types or [],
            limit=limit,
        )
