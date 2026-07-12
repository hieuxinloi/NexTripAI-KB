from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from ...normalizer import slugify
from ..v4.ontology import split_description


GeoRelation = Literal["located_in", "mentions"]
_AREA_PATTERN = re.compile(
    r"\b(?P<type>(?i:xã|phường|huyện|quận|thị\s+xã|thành\s+phố))\s+"
    r"(?P<name>[A-ZÀ-ỸĐ][\wÀ-ỹĐđ]*(?:[ -][A-ZÀ-ỸĐ][\wÀ-ỹĐđ]*){0,3})",
)
_AREA_TYPE = {
    "xã": "commune",
    "phường": "ward",
    "huyện": "district",
    "quận": "district",
    "thị xã": "town",
    "thành phố": "city_area",
}
_TRAILING_WORDS = {
    "tỉnh",
    "thành phố",
    "việt nam",
}
_CONTEXT_BOUNDARY_WORDS = {
    "cách",
    "giờ",
    "thuộc",
    "tỉnh",
    "việt",
}


@dataclass(frozen=True)
class GeoAreaCandidate:
    area_id: str
    name: str
    area_type: str
    city_id: str
    place_id: str
    relation: GeoRelation
    confidence: float
    evidence_id: str
    extraction_method: str


def extract_geo_area_candidates(place: dict[str, Any]) -> list[GeoAreaCandidate]:
    """Extract conservative location and mention candidates from verified text."""
    props = place["props"]
    candidates = _extract_from_text(
        str(props.get("address") or ""),
        place,
        relation="located_in",
        confidence=0.92,
        evidence_id=f"text-unit:verified:{place['id']}",
        method="verified_address",
    )
    for sequence, unit in enumerate(split_description(str(props.get("description") or ""))):
        candidates.extend(
            _extract_from_text(
                unit["text"],
                place,
                relation="mentions",
                confidence=0.70,
                evidence_id=f"text-unit:description:{place['id']}:{sequence}",
                method="description_mention",
            )
        )
    district = str(props.get("district") or "").strip()
    if district and not any(character in district for character in "()[]"):
        district = _clean_name(district.title())
    else:
        district = ""
    if district:
        candidates.append(
            _candidate(
                district,
                "district",
                place,
                relation="located_in",
                confidence=0.98,
                evidence_id=f"text-unit:verified:{place['id']}",
                method="verified_field",
            )
        )
    unique = {
        (item.area_id, item.place_id, item.relation): item
        for item in candidates
        if slugify(item.name) not in {slugify(props.get("city") or "")}
    }
    return list(unique.values())


def _extract_from_text(
    text: str,
    place: dict[str, Any],
    *,
    relation: GeoRelation,
    confidence: float,
    evidence_id: str,
    method: str,
) -> list[GeoAreaCandidate]:
    candidates = []
    for match in _AREA_PATTERN.finditer(text):
        prefix = " ".join(match.group("type").casefold().split())
        if _AREA_TYPE[prefix] == "city_area":
            continue
        name = _clean_name(match.group("name"))
        if name:
            candidates.append(
                _candidate(
                    name,
                    _AREA_TYPE[prefix],
                    place,
                    relation=relation,
                    confidence=confidence,
                    evidence_id=evidence_id,
                    method=method,
                )
            )
    return candidates


def _clean_name(value: str) -> str:
    words = value.strip(" ,.;:()[]").split()
    for index, word in enumerate(words[1:], start=1):
        if word.casefold() in _CONTEXT_BOUNDARY_WORDS:
            words = words[:index]
            break
    while words and " ".join(words[-2:]).casefold() in _TRAILING_WORDS:
        words = words[:-2]
    while words and words[-1].casefold() in _TRAILING_WORDS:
        words.pop()
    if not words or any(not word[0].isupper() for word in words):
        return ""
    return " ".join(words)


def _candidate(
    name: str,
    area_type: str,
    place: dict[str, Any],
    *,
    relation: GeoRelation,
    confidence: float,
    evidence_id: str,
    method: str,
) -> GeoAreaCandidate:
    city_id = str(place["city_id"])
    city = str(place["props"].get("city") or "")
    if name != city and name.casefold().endswith(" " + city.casefold()):
        name = name[: -len(city)].strip()
    return GeoAreaCandidate(
        area_id=f"geo-area:{city_id}:{slugify(name)}",
        name=name,
        area_type=area_type,
        city_id=city_id,
        place_id=str(place["id"]),
        relation=relation,
        confidence=confidence,
        evidence_id=evidence_id,
        extraction_method=method,
    )
