from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ...enrichment.nominatim import haversine_km
from ...normalizer import slugify


MAX_DUPLICATE_DISTANCE_KM = 0.1
MIN_SHARED_NAME_TOKENS = 3
MIN_NAME_JACCARD = 0.8


@dataclass(frozen=True)
class EntityResolutionReport:
    input_places: int
    canonical_places: int
    merged_places: int
    duplicate_groups: int
    id_redirects: dict[str, str]


def resolve_duplicate_places(
    places: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], EntityResolutionReport]:
    """Merge high-confidence duplicate venues before building the V8 graph.

    A match requires the same city and entity type, near-identical venue-name
    tokens, and coordinates within 100 metres.  The conservative conjunction
    prevents name-only matching from collapsing separate branches.
    """
    rows = deepcopy(places)
    parents = {str(place["id"]): str(place["id"]) for place in rows}

    def find(identifier: str) -> str:
        while parents[identifier] != identifier:
            parents[identifier] = parents[parents[identifier]]
            identifier = parents[identifier]
        return identifier

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    partitions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for place in rows:
        key = (str(place.get("city_id") or ""), str(place.get("entity_type") or ""))
        partitions.setdefault(key, []).append(place)

    for candidates in partitions.values():
        for index, left in enumerate(candidates):
            for right in candidates[index + 1 :]:
                if _duplicate_match(left, right):
                    union(str(left["id"]), str(right["id"]))

    groups: dict[str, list[dict[str, Any]]] = {}
    for place in rows:
        groups.setdefault(find(str(place["id"])), []).append(place)

    canonical_rows: list[dict[str, Any]] = []
    redirects: dict[str, str] = {}
    for members in groups.values():
        canonical = max(members, key=_canonical_score)
        canonical_id = str(canonical["id"])
        for member in members:
            redirects[str(member["id"])] = canonical_id
        canonical_rows.append(_merge_group(canonical, members))

    for place in canonical_rows:
        place["nearby_attractions"] = _redirect_nearby(
            place,
            redirects,
        )

    canonical_rows.sort(key=lambda place: str(place["id"]))
    merged_count = len(rows) - len(canonical_rows)
    return (
        canonical_rows,
        EntityResolutionReport(
            input_places=len(rows),
            canonical_places=len(canonical_rows),
            merged_places=merged_count,
            duplicate_groups=sum(len(members) > 1 for members in groups.values()),
            id_redirects={
                source: target
                for source, target in redirects.items()
                if source != target
            },
        ),
    )


def _duplicate_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_coordinates = _coordinates(left)
    right_coordinates = _coordinates(right)
    if left_coordinates is None or right_coordinates is None:
        return False
    distance_km = haversine_km(*left_coordinates, *right_coordinates)
    if distance_km > MAX_DUPLICATE_DISTANCE_KM:
        return False

    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return len(left_tokens) >= 2
    shared = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return (
        len(shared) >= MIN_SHARED_NAME_TOKENS
        and len(shared) / len(union) >= MIN_NAME_JACCARD
    )


def _coordinates(place: dict[str, Any]) -> tuple[float, float] | None:
    props = place.get("props") or {}
    latitude = props.get("lat", props.get("coordinates_lat"))
    longitude = props.get("lng", props.get("coordinates_lng"))
    if latitude is None or longitude is None:
        return None
    return float(latitude), float(longitude)


def _name_tokens(place: dict[str, Any]) -> set[str]:
    props = place.get("props") or {}
    name_tokens = set(slugify(str(props.get("name") or "")).split("-"))
    city_tokens = set(slugify(str(props.get("city") or "")).split("-"))
    return name_tokens - city_tokens - {"unknown"}


def _canonical_score(place: dict[str, Any]) -> tuple[int, int, int]:
    props = place.get("props") or {}
    verified = int(
        bool(props.get("last_verified") or props.get("verification_status"))
    )
    populated = sum(
        value not in (None, "", [], {})
        for value in props.values()
    )
    description_length = len(str(props.get("description") or ""))
    return verified, description_length, populated


def _merge_group(
    canonical: dict[str, Any],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(canonical)
    props = result["props"]
    canonical_name = str(props.get("name") or "")
    aliases = [
        *props.get("aliases", []),
        *(
            str(member["props"].get("name") or "")
            for member in members
            if member is not canonical
        ),
    ]
    props["aliases"] = _unique_strings(
        value for value in aliases if value and value.casefold() != canonical_name.casefold()
    )

    secondary_evidence: list[dict[str, Any]] = []
    for member in members:
        if member is canonical:
            continue
        member_props = member["props"]
        secondary_evidence.append(
            {
                "source_place_id": str(member["id"]),
                "title": str(member_props.get("name") or canonical_name),
                "source_name": member_props.get("source_name"),
                "url": member_props.get("source_url"),
                "text": (
                    member_props.get("search_text")
                    or member_props.get("embedding_text")
                    or member_props.get("description")
                    or canonical_name
                ),
            }
        )
        _fill_missing_props(props, member_props)

    if secondary_evidence:
        result["merged_evidence"] = secondary_evidence
        props["merged_source_ids"] = [
            item["source_place_id"] for item in secondary_evidence
        ]
        props["entity_resolution_method"] = "name_city_type_geo_v8"

    result["terms"] = _merge_terms(members)
    result["nearby_attractions"] = [
        deepcopy(nearby)
        for member in members
        for nearby in member.get("nearby_attractions", [])
    ]
    return result


def _fill_missing_props(
    destination: dict[str, Any],
    source: dict[str, Any],
) -> None:
    for key, value in source.items():
        if destination.get(key) in (None, "", [], {}) and value not in (
            None,
            "",
            [],
            {},
        ):
            destination[key] = deepcopy(value)


def _merge_terms(members: list[dict[str, Any]]) -> dict[str, list[Any]]:
    fields = {
        field
        for member in members
        for field in (member.get("terms") or {})
    }
    return {
        field: _unique_values(
            value
            for member in members
            for value in (member.get("terms") or {}).get(field, [])
        )
        for field in sorted(fields)
    }


def _redirect_nearby(
    place: dict[str, Any],
    redirects: dict[str, str],
) -> list[dict[str, Any]]:
    place_id = str(place["id"])
    nearest_by_id: dict[str, dict[str, Any]] = {}
    for nearby in place.get("nearby_attractions", []):
        target_id = redirects.get(str(nearby.get("id") or ""))
        if not target_id or target_id == place_id:
            continue
        candidate = {**nearby, "id": target_id}
        current = nearest_by_id.get(target_id)
        if current is None or _distance_value(candidate) < _distance_value(current):
            nearest_by_id[target_id] = candidate
    return sorted(nearest_by_id.values(), key=_distance_value)


def _distance_value(item: dict[str, Any]) -> float:
    value = item.get("distance_km")
    return float(value) if value is not None else float("inf")


def _unique_strings(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(str(value))
    return result


def _unique_values(values: Any) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        marker = repr(value)
        if marker not in seen:
            seen.add(marker)
            result.append(deepcopy(value))
    return result


__all__ = ["EntityResolutionReport", "resolve_duplicate_places"]
