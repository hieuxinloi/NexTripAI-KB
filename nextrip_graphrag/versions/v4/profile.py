from __future__ import annotations


SHARED_PROFILE_PREDICATES = (
    "description",
    "address",
    "rating",
    "review_count",
    "opening_hours",
    "price",
    "price_min",
    "price_max",
    "highlights",
)

PROFILE_PREDICATES_BY_ENTITY_TYPE = {
    "attraction": (
        "activities",
        "amenities",
        "duration",
        "indoor",
        "weather",
        "altitude",
        "features",
    ),
    "cafe": (
        "cuisine",
        "signature_dishes",
        "ambience",
        "serves",
        "amenities",
        "suitable_for",
        "features",
    ),
    "hotel": (
        "amenities",
        "suitable_for",
        "features",
        "star_rating",
        "hotel_style",
        "check_in_time",
        "check_out_time",
        "distance_to_beach",
    ),
    "nightlife": (
        "venue_type",
        "music_genres",
        "age_restriction",
        "ambience",
        "features",
    ),
    "restaurant": (
        "cuisine",
        "signature_dishes",
        "ambience",
        "serves",
        "amenities",
        "suitable_for",
        "features",
    ),
}


def profile_predicates(entity_types: list[str]) -> list[str]:
    """Return a stable fact projection for one or more place types."""
    selected = list(SHARED_PROFILE_PREDICATES)
    types = entity_types or list(PROFILE_PREDICATES_BY_ENTITY_TYPE)
    for entity_type in types:
        selected.extend(PROFILE_PREDICATES_BY_ENTITY_TYPE.get(entity_type, ()))
    return list(dict.fromkeys(selected))
