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

PLACE_OUTGOING_RELATIONSHIPS = [
    "IN_CITY",
    "HAS_TYPE",
    "HAS_CATEGORY",
    "FROM_SOURCE",
    "NEAR",
    *(spec[1] for spec in TERM_RELATION_SPECS.values()),
]
PLACE_INCOMING_RELATIONSHIPS = ["HAS_PLACE", "CONTAINS_PLACE", "NEAR"]


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
            connection_timeout=settings.neo4j_connection_timeout,
            max_transaction_retry_time=settings.neo4j_max_transaction_retry_time,
        )

    def close(self) -> None:
        self.driver.close()

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        options: dict[str, Any] = {"parameters_": params}
        if self.settings.neo4j_database:
            options["database_"] = self.settings.neo4j_database
        result = self.driver.execute_query(query, **options)
        return [record.data() for record in result.records]

    def has_embeddings(self) -> bool:
        rows = self.run(
            "MATCH (p:Place) WHERE p.embedding IS NOT NULL RETURN count(p) > 0 AS available"
        )
        return bool(rows and rows[0]["available"])

    def has_text_unit_embeddings(self) -> bool:
        rows = self.run(
            "MATCH (t:TextUnit) WHERE t.embedding IS NOT NULL RETURN count(t) > 0 AS available"
        )
        return bool(rows and rows[0]["available"])

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
            "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT text_unit_id IF NOT EXISTS FOR (t:TextUnit) REQUIRE t.id IS UNIQUE",
            "CREATE FULLTEXT INDEX place_fulltext IF NOT EXISTS FOR (p:Place) ON EACH [p.name, p.description, p.search_text, p.category_name, p.city]",
            "CREATE FULLTEXT INDEX text_unit_fulltext IF NOT EXISTS FOR (t:TextUnit) ON EACH [t.title, t.text]",
            f"""
            CREATE VECTOR INDEX place_embedding IF NOT EXISTS
            FOR (p:Place) ON (p.embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {embedding_dim},
              `vector.similarity_function`: 'cosine'
            }}}}
            """,
            f"""
            CREATE VECTOR INDEX text_unit_embedding IF NOT EXISTS
            FOR (t:TextUnit) ON (t.embedding)
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

        self.sync_place_ids([place["id"] for place in places])
        self.load_nearby_relationships(places)

    def sync_place_ids(self, place_ids: list[str]) -> None:
        if not place_ids:
            raise ValueError("Refusing to synchronize an empty place snapshot")
        self.run(
            "MATCH (p:Place) WHERE NOT p.id IN $place_ids DETACH DELETE p",
            place_ids=place_ids,
        )

    def upsert_place(self, place: dict[str, Any], embedding: list[float] | None) -> None:
        entity_label = ENTITY_LABELS.get(place["entity_type"], "TravelPlace")
        props = dict(place["props"])
        lat = props.get("lat")
        lng = props.get("lng")

        self._clear_managed_place_relationships(place["id"])

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

    def _clear_managed_place_relationships(self, place_id: str) -> None:
        self.run(
            """
            MATCH (p:Place {id: $place_id})-[relationship]->()
            WHERE type(relationship) IN $relationship_types
            DELETE relationship
            """,
            place_id=place_id,
            relationship_types=PLACE_OUTGOING_RELATIONSHIPS,
        )
        self.run(
            """
            MATCH ()-[relationship]->(p:Place {id: $place_id})
            WHERE type(relationship) IN $relationship_types
            DELETE relationship
            """,
            place_id=place_id,
            relationship_types=PLACE_INCOMING_RELATIONSHIPS,
        )

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
            SET source.name = $source_name
            REMOVE source.url
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

    def clear_evidence_graph(self) -> None:
        self.run(
            "MATCH (t:TextUnit) WHERE t.evidence_origin IN ['crawled_article', 'verified_record'] DETACH DELETE t"
        )
        self.run("MATCH (d:Document {managed_by: 'nextrip_enrichment'}) DETACH DELETE d")

    def load_evidence_graph(
        self,
        documents: list[dict[str, Any]],
        text_units: list[dict[str, Any]],
        embedder: Any | None = None,
        batch_size: int = 16,
        replace: bool = True,
    ) -> None:
        if replace:
            self.clear_evidence_graph()

        document_rows = []
        for document in documents:
            props = {key: value for key, value in document.items() if key != "place_ids"}
            props["id"] = props.pop("document_id")
            document_rows.append({"props": props, "place_ids": document.get("place_ids", [])})
        self.run(
            """
            UNWIND $documents AS document
            MERGE (d:Document {id: document.props.id})
            SET d += document.props
            WITH d, document
            UNWIND document.place_ids AS place_id
            MATCH (p:Place {id: place_id})
            MERGE (d)-[r:SOURCE_FOR]->(p)
            SET r.evidence_origin = 'verified_dataset_source_link',
                r.confidence = 0.6
            """,
            documents=document_rows,
        )

        for batch in chunks(text_units, batch_size):
            embeddings = (
                embedder.embed_documents([unit["text"] for unit in batch])
                if embedder
                else [None] * len(batch)
            )
            rows = []
            for unit, embedding in zip(batch, embeddings, strict=True):
                props = {
                    key: value
                    for key, value in unit.items()
                    if key not in {"mentions", "document_id", "text_unit_id"}
                }
                props["id"] = unit["text_unit_id"]
                rows.append(
                    {
                        "props": props,
                        "document_id": unit["document_id"],
                        "mentions": unit.get("mentions", []),
                        "embedding": embedding,
                    }
                )
            self._upsert_text_unit_batch(rows)

    def _upsert_text_unit_batch(self, rows: list[dict[str, Any]]) -> None:
        self.run(
            """
            UNWIND $rows AS row
            MERGE (t:TextUnit {id: row.props.id})
            SET t += row.props
            WITH t, row
            MATCH (d:Document {id: row.document_id})
            MERGE (d)-[:HAS_TEXT_UNIT]->(t)
            """,
            rows=rows,
        )
        vector_rows = [row for row in rows if row["embedding"] is not None]
        if vector_rows:
            self.run(
                """
                UNWIND $rows AS row
                MATCH (t:TextUnit {id: row.props.id})
                CALL db.create.setNodeVectorProperty(t, 'embedding', row.embedding)
                """,
                rows=vector_rows,
            )
        mention_rows = [row for row in rows if row["mentions"]]
        if mention_rows:
            self.run(
                """
                UNWIND $rows AS row
                MATCH (t:TextUnit {id: row.props.id})
                UNWIND row.mentions AS mention
                MATCH (p:Place {id: mention.place_id})
                MERGE (t)-[r:MENTIONS]->(p)
                SET r.match_type = mention.match_type,
                    r.confidence = mention.confidence
                """,
                rows=mention_rows,
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
            OPTIONAL MATCH (node)-[]->(facet:Term)
            OPTIONAL MATCH (node)-[:NEAR]-(near:Place)
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

    def text_unit_vector_search(
        self,
        embedding: list[float],
        limit: int,
        city_id: str | None = None,
        entity_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._text_unit_search(
            """
            CALL db.index.vector.queryNodes('text_unit_embedding', $candidate_k, $embedding)
            YIELD node, score
            """,
            limit=limit,
            city_id=city_id,
            entity_types=entity_types,
            embedding=embedding,
            candidate_k=max(limit * 20, 100),
            raw_query=None,
        )

    def text_unit_keyword_search(
        self,
        query_text: str,
        limit: int,
        city_id: str | None = None,
        entity_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._text_unit_search(
            """
            CALL db.index.fulltext.queryNodes('text_unit_fulltext', $query_text, {limit: $candidate_k})
            YIELD node, score
            """,
            limit=limit,
            city_id=city_id,
            entity_types=entity_types,
            query_text=query_text,
            candidate_k=max(limit * 20, 100),
            raw_query=query_text,
        )

    def _text_unit_search(
        self,
        search_clause: str,
        *,
        limit: int,
        city_id: str | None,
        entity_types: list[str] | None,
        **search_params: Any,
    ) -> list[dict[str, Any]]:
        query = search_clause + """
            MATCH (document:Document)-[:HAS_TEXT_UNIT]->(node)-[mention:MENTIONS]->(place:Place)
            MATCH (place)-[:IN_CITY]->(city:City)
            WHERE ($city_id IS NULL OR city.id = $city_id)
              AND (size($entity_types) = 0 OR place.entity_type IN $entity_types)
            WITH node, document, place, city, mention,
                 score + CASE
                   WHEN $raw_query IS NOT NULL AND toLower(place.name) = toLower($raw_query) THEN 10.0
                   WHEN $raw_query IS NOT NULL AND toLower(place.name) CONTAINS toLower($raw_query) THEN 3.0
                   ELSE 0.0
                 END AS score
            ORDER BY mention.confidence DESC, score DESC
            WITH place, city, max(score) AS score,
                 collect({
                    text_unit_id: node.id,
                    text: node.text,
                    sequence: node.sequence,
                    evidence_origin: node.evidence_origin,
                    confidence: mention.confidence,
                    match_type: mention.match_type,
                    title: document.title,
                    url: document.url,
                    score: score
                 })[0..3] AS evidence
            OPTIONAL MATCH (place)-[:HAS_CATEGORY]->(category:Category)
            OPTIONAL MATCH (place)-[]->(facet:Term)
            OPTIONAL MATCH (place)-[:NEAR]-(near:Place)
            WITH place, city, score, evidence, category,
                 collect(DISTINCT facet.name)[0..12] AS facets,
                 collect(DISTINCT near {
                    .id, .name, .entity_type, .entity_label, .category_name, .lat, .lng
                 })[0..5] AS nearby
            RETURN place {.*, embedding: null} AS place,
                   score,
                   city { .id, .name } AS city,
                   category { .id, .name } AS category,
                   facets,
                   nearby,
                   evidence
            ORDER BY score DESC
            LIMIT $limit
        """
        return self.run(
            query,
            city_id=city_id,
            entity_types=entity_types or [],
            limit=limit,
            **search_params,
        )

    def graph_filter_search(
        self,
        *,
        limit: int,
        city_id: str | None = None,
        entity_types: list[str] | None = None,
        categories: list[str] | None = None,
        terms: list[str] | None = None,
        indoor_or_all_weather: bool = False,
    ) -> list[dict[str, Any]]:
        return self.run(
            """
            MATCH (node:Place)-[:IN_CITY]->(city:City)
            WHERE ($city_id IS NULL OR city.id = $city_id)
              AND (size($entity_types) = 0 OR node.entity_type IN $entity_types)
            OPTIONAL MATCH (node)-[:HAS_CATEGORY]->(category:Category)
            OPTIONAL MATCH (node)-[facet_rel]->(facet:Term)
            WITH node, city, category,
                 collect(DISTINCT facet.name) AS facets,
                 collect(DISTINCT facet.id) AS facet_ids,
                 collect(DISTINCT toLower(facet.name)) AS facet_names
            WHERE (size($categories) = 0 OR node.category IN $categories)
              AND (size($terms) = 0 OR any(term IN $terms WHERE term IN facet_names))
              AND (
                NOT $indoor_or_all_weather
                OR node.is_indoor = true
                OR 'weather_all-weather' IN facet_ids
                OR 'weather_all_weather' IN facet_ids
                OR 'weather_rainy' IN facet_ids
              )
            OPTIONAL MATCH (node)-[:NEAR]-(near:Place)
            WITH node, city, category, facets,
                 collect(DISTINCT near {
                    .id, .name, .entity_type, .entity_label, .category_name, .lat, .lng
                 })[0..5] AS nearby
            OPTIONAL MATCH (document:Document)-[:HAS_TEXT_UNIT]->(evidence_node:TextUnit)-[mention:MENTIONS]->(node)
            WITH node, city, category, facets, nearby, document, evidence_node, mention
            ORDER BY mention.confidence DESC, evidence_node.sequence ASC
            WITH node, city, category, facets, nearby,
                 collect(DISTINCT {
                    text_unit_id: evidence_node.id,
                    text: evidence_node.text,
                    sequence: evidence_node.sequence,
                    evidence_origin: evidence_node.evidence_origin,
                    confidence: mention.confidence,
                    match_type: mention.match_type,
                    title: document.title,
                    url: document.url,
                    score: 1.0
                 })[0..3] AS evidence
            RETURN node {.*, embedding: null} AS place,
                   1.0 AS score,
                   city { .id, .name } AS city,
                   category { .id, .name } AS category,
                   facets[0..12] AS facets,
                   nearby,
                   evidence
            ORDER BY coalesce(node.rating, 0) DESC,
                     coalesce(node.review_count, 0) DESC,
                     node.name ASC
            LIMIT $limit
            """,
            city_id=city_id,
            entity_types=entity_types or [],
            categories=categories or [],
            terms=terms or [],
            indoor_or_all_weather=indoor_or_all_weather,
            limit=limit,
        )
