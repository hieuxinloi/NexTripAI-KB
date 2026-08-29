from __future__ import annotations

from typing import Any

from ...normalizer import slugify
from ..v2.graph_store import V2GraphStore
from .ontology import entity_profile, extract_facts


FACET_SPECS = {
    "amenities": ("Amenity", "HAS_AMENITY"),
    "cuisine": ("Cuisine", "SERVES_CUISINE"),
    "signature_dishes": ("Dish", "HAS_DISH"),
    "features": ("Feature", "HAS_FEATURE"),
    "suitable_for": ("Audience", "SUITABLE_FOR"),
    "ambience": ("Ambience", "HAS_AMBIENCE"),
    "serves": ("ServeTime", "SERVES_AT"),
    "tags": ("Tag", "TAGGED_WITH"),
    "music_genres": ("MusicGenre", "HAS_MUSIC_GENRE"),
    "weather_suitable": ("Weather", "SUITABLE_WEATHER"),
}


class V3GraphStore(V2GraphStore):
    kb_version = "v3"

    def ensure_v3_schema(self, embedding_dim: int) -> None:
        self.ensure_typed_schema(embedding_dim, index_prefix="v3")
        for label in {spec[0] for spec in FACET_SPECS.values()} | {"VenueType", "HotelStyle"}:
            self.run(
                f"CREATE CONSTRAINT v3_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )

    def facts_for_place(self, place: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_facts(place)

    def profile_for_place(self, place: dict[str, Any]) -> str:
        return entity_profile(place)

    def after_places_loaded(self, places: list[dict[str, Any]]) -> None:
        self._load_facets(places)
        self._load_single_facets(places)
        self._load_geo_near_edges()

    def _load_facets(self, places: list[dict[str, Any]]) -> None:
        for field, (label, relationship) in FACET_SPECS.items():
            rows = []
            for place in places:
                values = place.get("terms", {}).get(field) or place["props"].get(field) or []
                for value in values:
                    if field in {"cuisine", "signature_dishes"} and not _facet_is_supported(
                        place, field, str(value)
                    ):
                        continue
                    rows.append(
                        {
                            "place_id": place["id"],
                            "facet_id": f"{label.lower()}:{slugify(str(value))}",
                            "name": str(value),
                        }
                    )
            if not rows:
                continue
            self.run_versioned(
                f"""
                UNWIND $rows AS row
                MATCH (place:Place {{id: row.place_id, kb_version: $kb_version}})
                MERGE (facet:{label} {{id: row.facet_id}})
                SET facet.name = row.name, facet.kb_version = $kb_version
                MERGE (place)-[:{relationship}]->(facet)
                """,
                rows=rows,
            )

    def _load_single_facets(self, places: list[dict[str, Any]]) -> None:
        specs = (
            ("venue_type", "VenueType", "HAS_VENUE_TYPE"),
            ("hotel_style", "HotelStyle", "HAS_HOTEL_STYLE"),
        )
        for field, label, relationship in specs:
            rows = [
                {
                    "place_id": place["id"],
                    "facet_id": f"{label.lower()}:{slugify(str(place['props'][field]))}",
                    "name": str(place["props"][field]),
                }
                for place in places
                if place["props"].get(field)
            ]
            if rows:
                self.run_versioned(
                    f"""
                    UNWIND $rows AS row
                    MATCH (place:Place {{id: row.place_id, kb_version: $kb_version}})
                    MERGE (facet:{label} {{id: row.facet_id}})
                    SET facet.name = row.name, facet.kb_version = $kb_version
                    MERGE (place)-[:{relationship}]->(facet)
                    """,
                    rows=rows,
                )

    def _load_geo_near_edges(self) -> None:
        self.run_versioned(
            """
            MATCH (source:Place {kb_version: $kb_version})
            WHERE source.entity_type IN ['hotel', 'cafe', 'restaurant', 'nightlife']
            CALL (source) {
              MATCH (target:Place {kb_version: $kb_version, entity_type: 'attraction'})
              WHERE source.city = target.city AND source.location IS NOT NULL AND target.location IS NOT NULL
              WITH target, point.distance(source.location, target.location) / 1000.0 AS distanceKm
              ORDER BY distanceKm
              LIMIT 5
              RETURN target, distanceKm
            }
            MERGE (source)-[near:NEAR]->(target)
            SET near.distance_km = round(distanceKm, 2), near.origin = 'geo_v3'
            """
        )

    def graph_statistics(self) -> dict[str, int]:
        statistics = super().graph_statistics()
        facets = self.run_versioned(
            """
            MATCH (place:Place {kb_version: $kb_version})-[relationship]->(facet)
            WHERE type(relationship) IN $relationships
            RETURN count(relationship) AS facet_edges
            """,
            relationships=[spec[1] for spec in FACET_SPECS.values()]
            + ["HAS_VENUE_TYPE", "HAS_HOTEL_STYLE"],
        )[0]
        statistics["facet_edges"] = int(facets["facet_edges"])
        return statistics


_CUISINE_EVIDENCE_TERMS = {
    "seafood": ("seafood", "hai-san"),
    "vegetarian": ("vegetarian", "vegan", "chay"),
    "vietnamese": ("vietnamese", "mon-viet", "am-thuc-viet"),
    "korean": ("korean", "han-quoc"),
    "japanese": ("japanese", "nhat-ban", "sushi"),
    "chinese": ("chinese", "trung-hoa"),
    "western": ("western", "mon-au"),
    "bbq": ("bbq", "nuong"),
}


def _facet_is_supported(place: dict[str, Any], field: str, value: str) -> bool:
    props = place["props"]
    evidence = slugify(
        " ".join(
            str(part)
            for part in (props.get("name"), props.get("description"), props.get("category"))
            if part
        )
    )
    claim = slugify(value)
    if field == "cuisine":
        terms = _CUISINE_EVIDENCE_TERMS.get(claim, (claim,))
        return any(term in evidence for term in terms)
    return claim in evidence
