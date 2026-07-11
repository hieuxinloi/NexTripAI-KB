from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..normalizer import load_raw_items
from .io import stable_id, utc_now, write_json, write_jsonl


def load_verified_places(data_dir: str | Path) -> list[dict[str, Any]]:
    return [raw for raw, _ in load_raw_items(Path(data_dir))]


def build_source_catalog(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_names: dict[str, str | None] = {}
    for place in places:
        source = place.get("source") or {}
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        grouped[url].append({"place_id": place["id"], "place_name": place["name"]})
        source_names[url] = source.get("source_name")

    return [
        {
            "document_id": stable_id("doc", url),
            "url": url,
            "domain": urlparse(url).netloc.lower(),
            "source_name": source_names[url],
            "place_count": len(grouped[url]),
            "places": grouped[url],
        }
        for url in sorted(grouped)
    ]


def build_text_units(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for place in places:
        source = place.get("source") or {}
        url = str(source.get("url") or "").strip()
        text = str(place.get("embedding_text") or place.get("description") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "text_unit_id": stable_id("text", f"{place['id']}|{url}"),
                "document_id": stable_id("doc", url) if url else None,
                "place_id": place["id"],
                "place_name": place["name"],
                "city": place.get("city"),
                "entity_type": place.get("entity_type"),
                "text": text,
                "source_url": url or None,
                "evidence_origin": "verified_record",
            }
        )
    return rows


def build_source_artifacts(data_dir: str | Path, workspace: str | Path) -> dict[str, Any]:
    places = load_verified_places(data_dir)
    catalog = build_source_catalog(places)
    text_units = build_text_units(places)
    output_dir = Path(workspace) / "output"
    catalog_path = output_dir / "source_catalog.json"
    text_units_path = output_dir / "text_units.jsonl"
    write_json(catalog_path, catalog)
    write_jsonl(text_units_path, text_units)
    report = {
        "generated_at": utc_now(),
        "data_dir": str(Path(data_dir).resolve()),
        "place_count": len(places),
        "source_document_count": len(catalog),
        "text_unit_count": len(text_units),
        "catalog_path": str(catalog_path.resolve()),
        "text_units_path": str(text_units_path.resolve()),
    }
    write_json(output_dir / "source_artifacts_report.json", report)
    return report
