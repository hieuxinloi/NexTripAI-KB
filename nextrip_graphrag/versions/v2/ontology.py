from __future__ import annotations

import json
from typing import Any


def _fact(
    place_id: str,
    predicate: str,
    value: Any,
    *,
    unit: str | None = None,
    confidence: float,
) -> dict[str, Any] | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        value_type = "boolean"
    elif isinstance(value, (int, float)):
        value_type = "number"
    elif isinstance(value, list):
        value_type = "string_list"
    else:
        value_type = "string"
    return {
        "id": f"fact:{place_id}:{predicate}",
        "predicate": predicate,
        "value": value,
        "value_type": value_type,
        "unit": unit,
        "confidence": confidence,
    }


def extract_facts(place: dict[str, Any]) -> list[dict[str, Any]]:
    props = place["props"]
    place_id = place["id"]
    verified = bool(props.get("last_verified") or props.get("verification_status"))
    confidence = 0.95 if verified else 0.7
    location = None
    if props.get("lat") is not None and props.get("lng") is not None:
        location = json.dumps(
            {"lat": props["lat"], "lng": props["lng"]},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    candidates = [
        _fact(place_id, "city", props.get("city"), confidence=confidence),
        _fact(place_id, "address", props.get("address"), confidence=confidence),
        _fact(place_id, "location", location, confidence=confidence),
        _fact(place_id, "latitude", props.get("lat"), confidence=confidence),
        _fact(place_id, "longitude", props.get("lng"), confidence=confidence),
        _fact(place_id, "description", props.get("description"), confidence=confidence),
        _fact(place_id, "rating", props.get("rating"), unit="stars", confidence=confidence),
        _fact(place_id, "review_count", props.get("review_count"), confidence=confidence),
        _fact(place_id, "star_rating", props.get("star_rating"), unit="stars", confidence=confidence),
        _fact(place_id, "phone", props.get("phone"), confidence=confidence),
        _fact(place_id, "altitude", props.get("altitude_m"), unit="m", confidence=confidence),
        _fact(
            place_id,
            "duration",
            props.get("duration_recommendation"),
            confidence=confidence,
        ),
        _fact(place_id, "amenities", props.get("amenities"), confidence=confidence),
        _fact(place_id, "indoor", props.get("is_indoor"), confidence=confidence),
        _fact(place_id, "weather", props.get("weather_suitable"), confidence=confidence),
        _fact(
            place_id,
            "opening_hours",
            _opening_hours(props),
            confidence=confidence,
        ),
        _fact(place_id, "price", _price(props), unit="VND", confidence=confidence),
    ]
    return [fact for fact in candidates if fact is not None]


def _opening_hours(props: dict[str, Any]) -> str | None:
    opening = props.get("opening_hours_open")
    closing = props.get("opening_hours_close")
    if opening and closing:
        return f"{opening}-{closing}"
    return opening or closing or props.get("opening_hours_note")


def _price(props: dict[str, Any]) -> str | float | None:
    for key in (
        "price_per_person_min",
        "price_per_night_min",
        "ticket_price_adult",
        "entry_fee_adult",
        "drink_price_min",
        "price_range",
    ):
        value = props.get(key)
        if value is not None:
            return value
    return None


def entity_profile(place: dict[str, Any]) -> str:
    props = place["props"]
    aliases = ", ".join(props.get("aliases") or [])
    parts = [
        f"Tên: {props.get('name')}",
        f"Tên khác: {aliases}" if aliases else None,
        f"Thành phố: {props.get('city')}",
        f"Loại: {place.get('place_type_name')}",
        f"Danh mục: {place.get('category_name')}",
        f"Địa chỉ: {props.get('address')}" if props.get("address") else None,
        f"Mô tả: {props.get('description')}" if props.get("description") else None,
        f"Tọa độ: {props.get('lat')}, {props.get('lng')}",
        f"Đánh giá: {props.get('rating')} sao" if props.get("rating") is not None else None,
        f"Số lượt đánh giá: {props.get('review_count')}"
        if props.get("review_count") is not None
        else None,
        f"Độ cao: {props.get('altitude_m')} m" if props.get("altitude_m") is not None else None,
        f"Giờ mở cửa: {_opening_hours(props)}" if _opening_hours(props) else None,
        f"Điện thoại: {props.get('phone')}" if props.get("phone") else None,
        f"Thời lượng gợi ý: {props.get('duration_recommendation')}"
        if props.get("duration_recommendation")
        else None,
        f"Trong nhà: {props.get('is_indoor')}" if props.get("is_indoor") is not None else None,
        f"Thời tiết phù hợp: {', '.join(props.get('weather_suitable') or [])}"
        if props.get("weather_suitable")
        else None,
        f"Tiện ích: {', '.join(props.get('amenities') or [])}"
        if props.get("amenities")
        else None,
        f"Giá: {_price(props)} VND" if _price(props) is not None else None,
    ]
    return "\n".join(part for part in parts if part)
