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
    "p.": "ward",
    "q.": "district",
    "tp.": "city_area",
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
_COUNTRY_NAMES = {"viet-nam", "vietnam"}
_PROVINCE_NAMES = {
    "binh-dinh",
    "da-nang",
    "dak-lak",
    "gia-lai",
    "quang-nam",
}
_CITY_ALIASES = {
    "da-nang",
    "quy-nhon",
    "qui-nhon",
    "thanh-pho-da-nang",
    "thanh-pho-quy-nhon",
    "thanh-pho-qui-nhon",
    "tp-da-nang",
    "tp-quy-nhon",
    "tp-qui-nhon",
}
_ADMIN_PREFIX = re.compile(
    r"^(?P<prefix>(?i:p\.|q\.|tp\.|phường|xã|quận|huyện|thị\s+xã|thành\s+phố))\s*"
)
_ADDRESS_NOISE = re.compile(r"[\ue000-\uf8ff]")
_POSTAL_CODE = re.compile(r"-\d{4,6}$")


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


def extract_geo_area_candidates(
    place: dict[str, Any],
    verified_address_areas: set[str] | None = None,
) -> list[GeoAreaCandidate]:
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
    address_area = _address_area(str(props.get("address") or ""))
    if (
        address_area is not None
        and verified_address_areas is not None
        and slugify(address_area.name) in verified_address_areas
    ):
        candidates.append(
            _candidate(
                address_area.name,
                address_area.area_type,
                place,
                relation="located_in",
                confidence=0.95,
                evidence_id=f"text-unit:verified:{place['id']}",
                method="verified_address_component",
            )
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


def verified_address_area_vocabulary(places: list[dict[str, Any]]) -> set[str]:
    """Learn high-confidence administrative components from verified addresses."""
    structured: set[str] = set()
    parsed = [
        _address_area(str(place["props"].get("address") or ""))
        for place in places
    ]
    for area in parsed:
        if area is None:
            continue
        if area.component_count >= 2 or area.has_admin_prefix:
            structured.add(slugify(area.name))
    return structured


@dataclass(frozen=True)
class _AddressArea:
    name: str
    area_type: str
    component_count: int
    has_admin_prefix: bool


def _address_area(address: str) -> _AddressArea | None:
    components = _address_components(address)
    while components and _parent_component(components[-1]):
        components.pop()
    if not components:
        return None
    raw_name = components[-1]
    prefix_match = _ADMIN_PREFIX.match(raw_name)
    prefix = prefix_match.group("prefix") if prefix_match else ""
    name = _ADMIN_PREFIX.sub("", raw_name).strip()
    if not name or _looks_like_street(name) or _parent_component(name):
        return None
    return _AddressArea(
        name=name,
        area_type=_AREA_TYPE.get(" ".join(prefix.casefold().split()), "locality"),
        component_count=len(components),
        has_admin_prefix=bool(prefix),
    )


def _address_components(address: str) -> list[str]:
    cleaned = _ADDRESS_NOISE.sub(" ", address).replace("\n", ",")
    return [
        normalized
        for component in cleaned.split(",")
        if (normalized := " ".join(component.strip().split()))
    ]


def _parent_component(value: str) -> bool:
    normalized = _POSTAL_CODE.sub("", slugify(value))
    return normalized in _COUNTRY_NAMES | _PROVINCE_NAMES | _CITY_ALIASES


def _looks_like_street(value: str) -> bool:
    normalized = slugify(value)
    return (
        any(character.isdigit() for character in value)
        or "+" in value
        or normalized in {"duong", "road", "unnamed", "unnamed-road"}
    )


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
