from __future__ import annotations

import re
from math import asin, cos, radians, sin, sqrt
from typing import Any

from ..v2.ontology import _fact, entity_profile as v2_entity_profile, extract_facts as v2_extract_facts


CITY_CENTERS = {
    "Đà Nẵng": (16.0544, 108.2022),
    "Quy Nhơn": (13.7820, 109.2190),
}


def extract_facts(place: dict[str, Any]) -> list[dict[str, Any]]:
    props = place["props"]
    place_id = place["id"]
    verified = bool(props.get("last_verified") or props.get("verification_status"))
    confidence = 0.95 if verified else 0.7
    facts = v2_extract_facts(place)
    candidates = [
        _fact(place_id, "check_in_time", props.get("check_in_time"), confidence=confidence),
        _fact(place_id, "check_out_time", props.get("check_out_time"), confidence=confidence),
        _fact(place_id, "distance_to_beach", _distance_km(props.get("distance_to_beach")), unit="km", confidence=confidence),
        _fact(place_id, "distance_to_center", _distance_km(props.get("distance_to_center")), unit="km", confidence=confidence),
        _fact(
            place_id,
            "distance_from_center",
            props.get("transportation_options_distance_from_center"),
            confidence=confidence,
        ),
        _fact(
            place_id,
            "distance_to_city_center_geo",
            _distance_to_city_center(props),
            unit="km",
            confidence=0.95,
        ),
        _fact(
            place_id,
            "travel_time_from_center",
            props.get("transportation_options_travel_time_from_center"),
            confidence=confidence,
        ),
        _fact(place_id, "hotel_style", props.get("hotel_style"), confidence=confidence),
        _fact(place_id, "cuisine", props.get("cuisine"), confidence=confidence),
        _fact(place_id, "signature_dishes", props.get("signature_dishes"), confidence=confidence),
        _fact(place_id, "serves", props.get("serves"), confidence=confidence),
        _fact(place_id, "ambience", props.get("ambience"), confidence=confidence),
        _fact(place_id, "features", props.get("features"), confidence=confidence),
        _fact(place_id, "suitable_for", props.get("suitable_for"), confidence=confidence),
        _fact(place_id, "venue_type", props.get("venue_type"), confidence=confidence),
        _fact(place_id, "music_genres", props.get("music_genres"), confidence=confidence),
        _fact(place_id, "highlights", props.get("highlights"), confidence=confidence),
        _fact(place_id, "tags", props.get("tags"), confidence=confidence),
        _fact(place_id, "age_restriction", props.get("age_restriction"), confidence=confidence),
        _fact(place_id, "construction_period", props.get("construction_period"), confidence=confidence),
        _fact(place_id, "unesco_status", props.get("unesco_status"), confidence=confidence),
        _fact(place_id, "booking_advice", props.get("booking_advice"), confidence=confidence),
        _fact(place_id, "cable_car", props.get("cable_car"), confidence=confidence),
        _fact(place_id, "branch_info", props.get("branch_info"), confidence=confidence),
        _fact(place_id, "price_min", _first_price(props, "min"), unit="VND", confidence=confidence),
        _fact(place_id, "price_max", _first_price(props, "max"), unit="VND", confidence=confidence),
        _fact(
            place_id,
            "opening_24h_claim",
            True if claims_open_24h(props) else None,
            confidence=confidence,
        ),
        _fact(
            place_id,
            "data_quality_warning",
            _opening_hours_warning(props),
            confidence=1.0,
        ),
    ]
    facts.extend(fact for fact in candidates if fact is not None)
    return facts


def entity_profile(place: dict[str, Any]) -> str:
    props = place["props"]
    extra = [
        _line("Check-in", props.get("check_in_time")),
        _line("Check-out", props.get("check_out_time")),
        _line("Khoảng cách đến biển", props.get("distance_to_beach")),
        _line("Khoảng cách đến trung tâm", props.get("distance_to_center")),
        _line("Phong cách khách sạn", props.get("hotel_style")),
        _line("Ẩm thực", props.get("cuisine")),
        _line("Món đặc trưng", props.get("signature_dishes")),
        _line("Phục vụ", props.get("serves")),
        _line("Không gian", props.get("ambience")),
        _line("Đặc điểm", props.get("features")),
        _line("Phù hợp", props.get("suitable_for")),
        _line("Loại hình nightlife", props.get("venue_type")),
        _line("Phong cách nhạc", props.get("music_genres")),
        _line("Điểm nổi bật", props.get("highlights")),
        _line("Tags", props.get("tags")),
    ]
    return "\n".join([v2_entity_profile(place), *(line for line in extra if line)])


def _line(label: str, value: Any) -> str | None:
    if value in (None, "", []):
        return None
    rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    return f"{label}: {rendered}"


def _first_price(props: dict[str, Any], bound: str) -> float | int | None:
    for prefix in (
        "price_per_night",
        "price_per_person",
        "drink_price",
        "entry_fee",
        "ticket_price",
    ):
        value = props.get(f"{prefix}_{bound}")
        if value is None and prefix == "ticket_price" and bound == "min":
            value = props.get("ticket_price_adult")
        if value is not None:
            return value
    return None


def _opening_hours_warning(props: dict[str, Any]) -> str | None:
    claims_always_open = claims_open_24h(props)
    structured_always_open = props.get("opening_hours_open") in {"00:00", "0:00"} and props.get(
        "opening_hours_close"
    ) in {"23:59", "24:00", "00:00"}
    if claims_always_open and not structured_always_open:
        return "opening_hours_conflict:text_claims_24h_but_structured_hours_differ"
    return None


def claims_open_24h(props: dict[str, Any]) -> bool:
    name = str(props.get("name") or "").casefold()
    if "24/7" in name or "24/24" in name:
        return True
    description = " ".join(str(props.get("description") or "").casefold().split())
    return bool(
        re.search(
            r"(?:qu[aá]n|c[aà] ph[eê]|cafe|coffee).{0,45}(?:m[oở]|ho[aạ]t [dđ][oộ]ng).{0,16}24/(?:7|24)",
            description,
        )
    )


def _distance_km(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.casefold().replace("km", "").strip())
        except ValueError:
            return None
    return None


def _distance_to_city_center(props: dict[str, Any]) -> float | None:
    center = CITY_CENTERS.get(str(props.get("city")))
    lat = props.get("lat")
    lng = props.get("lng")
    if center is None or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    lat1, lng1, lat2, lng2 = map(radians, (center[0], center[1], lat, lng))
    delta_lat = lat2 - lat1
    delta_lng = lng2 - lng1
    haversine = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lng / 2) ** 2
    return round(6371.0 * 2 * asin(sqrt(haversine)), 2)
