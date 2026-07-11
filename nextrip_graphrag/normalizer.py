from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


RAW_FILES = {
    "attraction": "attraction_final.json",
    "cafe": "cafe_final.json",
    "hotel": "hotel_final.json",
    "nightlife": "nightlife_final.json",
    "restaurant": "restaurant_final.json",
}

CITY_DEFINITIONS = {
    "Quy Nhơn": {
        "id": "city_quy_nhon",
        "slug": "quy-nhon",
        "name": "Quy Nhơn",
        "aliases": ["Quy Nhon", "Qui Nhơn", "Qui Nhon", "Bình Định", "Binh Dinh"],
        "country": "Việt Nam",
        "region": "Bình Định",
    },
    "Đà Nẵng": {
        "id": "city_da_nang",
        "slug": "da-nang",
        "name": "Đà Nẵng",
        "aliases": ["Da Nang", "Danang"],
        "country": "Việt Nam",
        "region": "Đà Nẵng",
    },
}

ENTITY_TYPE_LABELS = {
    "attraction": "Điểm tham quan",
    "cafe": "Quán cà phê",
    "hotel": "Lưu trú",
    "nightlife": "Giải trí đêm",
    "restaurant": "Nhà hàng",
}

CATEGORY_LABELS = {
    "apartment": "Căn hộ",
    "bar": "Bar",
    "bbq": "Đồ nướng",
    "beach": "Biển đảo",
    "cafe": "Cà phê",
    "culture": "Văn hóa",
    "entertainment": "Giải trí",
    "historical": "Lịch sử",
    "hostel": "Hostel",
    "hotel": "Khách sạn",
    "nature": "Thiên nhiên",
    "nightclub": "Club",
    "pub": "Pub",
    "resort": "Resort",
    "rooftop_bar": "Rooftop bar",
    "rooftop_cafe": "Cà phê rooftop",
    "seafood": "Hải sản",
    "specialty_coffee": "Specialty coffee",
    "tea_shop": "Trà và trà sữa",
    "vietnamese": "Món Việt",
    "villa": "Villa",
    "work_cafe": "Cà phê làm việc",
}

LIST_RELATION_FIELDS = {
    "ambience",
    "amenities",
    "cuisine",
    "dietary_options",
    "drink_specialties",
    "features",
    "highlights",
    "music_genres",
    "music_style",
    "payment_methods",
    "room_types",
    "serves",
    "signature_dishes",
    "suitable_for",
    "tags",
    "vibe",
    "weather_suitable",
}


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def slugify(value: str) -> str:
    value = strip_accents(value).lower()
    value = value.replace("đ", "d").replace("Đ", "d")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def canonical_city(value: str) -> str:
    plain = slugify(value)
    if "quy-nhon" in plain or "qui-nhon" in plain:
        return "Quy Nhơn"
    if "da-nang" in plain or "danang" in plain:
        return "Đà Nẵng"
    raise ValueError(f"Unsupported city: {value!r}")


def clean_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = [value]

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw_values:
        text = clean_string(item)
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(text)
    return cleaned


def is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def flatten_for_neo4j(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"nearby_attractions"}:
            continue
        prop_key = f"{prefix}_{key}" if prefix else key
        if item is None:
            continue
        if isinstance(item, dict):
            props.update(flatten_for_neo4j(item, prop_key))
        elif isinstance(item, list):
            if all(is_scalar(entry) for entry in item):
                cleaned = [entry for entry in item if entry is not None]
                if cleaned:
                    props[prop_key] = cleaned
        elif is_scalar(item):
            props[prop_key] = item
    return props


def relation_terms(raw: dict[str, Any]) -> dict[str, list[str]]:
    return {field: clean_list(raw.get(field)) for field in LIST_RELATION_FIELDS}


def price_summary(raw: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for field, label in [
        ("price_per_person", "Giá mỗi người"),
        ("price_per_night", "Giá mỗi đêm"),
        ("ticket_price", "Vé"),
        ("entry_fee", "Phí vào cửa"),
        ("drink_price", "Giá đồ uống"),
    ]:
        value = raw.get(field)
        if not isinstance(value, dict):
            continue
        min_price = value.get("min")
        if min_price is None:
            min_price = value.get("adult")
        max_price = value.get("max")
        currency = value.get("currency") or "VND"
        note = value.get("note")
        if min_price is not None or max_price is not None:
            if min_price is not None and max_price is not None:
                parts.append(f"{label}: {min_price}-{max_price} {currency}")
            elif min_price is not None:
                parts.append(f"{label}: từ {min_price} {currency}")
        if note:
            parts.append(f"{label} ghi chú: {note}")
    if raw.get("price_range"):
        parts.append(f"Khoảng giá: {raw['price_range']}")
    return parts


def build_search_text(raw: dict[str, Any], city_name: str, category_name: str) -> str:
    opening = raw.get("opening_hours") or {}
    transportation = raw.get("transportation_options") or {}
    text_parts = [
        f"Tên: {raw.get('name')}",
        f"Thành phố: {city_name}",
        f"Loại địa điểm: {ENTITY_TYPE_LABELS.get(raw.get('entity_type'), raw.get('entity_type'))}",
        f"Nhóm: {category_name}",
        f"Địa chỉ: {raw.get('address')}" if raw.get("address") else None,
        f"Quận/huyện: {raw.get('district')}" if raw.get("district") else None,
        f"Mô tả: {raw.get('description')}",
        f"Điểm nổi bật: {', '.join(clean_list(raw.get('highlights')))}" if raw.get("highlights") else None,
        f"Phù hợp: {', '.join(clean_list(raw.get('suitable_for')))}" if raw.get("suitable_for") else None,
        f"Tag: {', '.join(clean_list(raw.get('tags')))}" if raw.get("tags") else None,
        f"Giờ mở cửa: {opening.get('open')} - {opening.get('close')}" if opening else None,
        f"Ghi chú giờ mở cửa: {opening.get('note')}" if opening.get("note") else None,
        f"Cách trung tâm: {transportation.get('distance_from_center')}" if transportation.get("distance_from_center") else None,
        f"Thời gian từ trung tâm: {transportation.get('travel_time_from_center')}" if transportation.get("travel_time_from_center") else None,
    ]
    for field in sorted(LIST_RELATION_FIELDS):
        values = clean_list(raw.get(field))
        if values:
            text_parts.append(f"{field.replace('_', ' ')}: {', '.join(values)}")
    text_parts.extend(price_summary(raw))
    return "\n".join(part for part in text_parts if part)


def normalize_place(raw: dict[str, Any], source_file: str) -> dict[str, Any]:
    city_name = canonical_city(raw["city"])
    city = CITY_DEFINITIONS[city_name]
    entity_type = clean_string(raw.get("entity_type")) or source_file.removesuffix("_final.json")
    category = clean_string(raw.get("category")) or "unknown"
    category_name = CATEGORY_LABELS.get(category, category.replace("_", " ").title())
    coords = raw.get("coordinates") or {}

    props = flatten_for_neo4j(raw)
    props.update(
        {
            "id": raw["id"],
            "slug": f"{city['slug']}-{entity_type}-{slugify(raw['name'])}",
            "name": clean_string(raw["name"]),
            "entity_type": entity_type,
            "entity_label": ENTITY_TYPE_LABELS.get(entity_type, entity_type),
            "city": city_name,
            "city_id": city["id"],
            "category": category,
            "category_name": category_name,
            "lat": float(coords["lat"]),
            "lng": float(coords["lng"]),
            "source_file": source_file,
        }
    )

    source = raw.get("source") or {}
    props["source_name"] = source.get("source_name")
    props["source_url"] = source.get("url")
    props["source_crawled_at"] = source.get("crawled_at")

    search_text = build_search_text(raw, city_name, category_name)
    props["search_text"] = search_text
    props["embedding_text"] = raw.get("embedding_text") or search_text

    return {
        "id": raw["id"],
        "city_id": city["id"],
        "entity_type": entity_type,
        "category_id": f"category_{slugify(category)}",
        "category_name": category_name,
        "category_value": category,
        "place_type_id": f"type_{slugify(entity_type)}",
        "place_type_name": ENTITY_TYPE_LABELS.get(entity_type, entity_type),
        "props": {key: value for key, value in props.items() if value is not None},
        "terms": relation_terms(raw),
        "nearby_attractions": raw.get("nearby_attractions") or [],
    }


def load_raw_items(data_dir: Path) -> list[tuple[dict[str, Any], str]]:
    items: list[tuple[dict[str, Any], str]] = []
    for _, filename in RAW_FILES.items():
        path = data_dir / filename
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        for raw in payload.get("data", []):
            items.append((raw, filename))
    return items


def normalize_dataset(data_dir: str | Path) -> dict[str, Any]:
    data_path = Path(data_dir)
    raw_items = load_raw_items(data_path)
    places = [normalize_place(raw, source_file) for raw, source_file in raw_items]

    city_counts = Counter(place["city_id"] for place in places)
    type_counts = Counter(place["entity_type"] for place in places)
    cities = []
    for city in CITY_DEFINITIONS.values():
        cities.append(
            {
                **city,
                "is_main_node": True,
                "place_count": city_counts.get(city["id"], 0),
            }
        )

    manifest = {
        "total_places": len(places),
        "city_counts": dict(city_counts),
        "entity_type_counts": dict(type_counts),
        "raw_files": list(RAW_FILES.values()),
    }
    return {"cities": cities, "places": places, "manifest": manifest}


def write_processed(bundle: dict[str, Any], out_dir: str | Path) -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    with (out_path / "cities.json").open("w", encoding="utf-8") as handle:
        json.dump(bundle["cities"], handle, ensure_ascii=False, indent=2)

    with (out_path / "places.jsonl").open("w", encoding="utf-8") as handle:
        for place in bundle["places"]:
            handle.write(json.dumps(place, ensure_ascii=False) + "\n")

    with (out_path / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(bundle["manifest"], handle, ensure_ascii=False, indent=2)


def read_processed(processed_dir: str | Path) -> dict[str, Any]:
    path = Path(processed_dir)
    with (path / "cities.json").open("r", encoding="utf-8") as handle:
        cities = json.load(handle)
    places = []
    with (path / "places.jsonl").open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                places.append(json.loads(line))
    with (path / "manifest.json").open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    return {"cities": cities, "places": places, "manifest": manifest}
