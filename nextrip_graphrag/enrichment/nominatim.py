from __future__ import annotations

import math
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from ..config import ENRICHMENT_HTTP_TIMEOUT_SECONDS, NOMINATIM_DELAY_SECONDS
from .catalog import load_verified_places
from .io import cache_path, read_json, utc_now, write_json, write_jsonl
from .source_crawler import DEFAULT_USER_AGENT


NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def score_candidate(place: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    coords = place.get("coordinates") or {}
    candidate_name = candidate.get("name") or candidate.get("display_name") or ""
    name_similarity = SequenceMatcher(None, normalize_text(place["name"]), normalize_text(candidate_name)).ratio()
    display_name = normalize_text(candidate.get("display_name") or "")
    city_match = normalize_text(place.get("city") or "") in display_name
    distance = haversine_km(
        float(coords["lat"]),
        float(coords["lng"]),
        float(candidate["lat"]),
        float(candidate["lon"]),
    )
    distance_score = max(0.0, 1.0 - min(distance, 20.0) / 20.0)
    confidence = 0.55 * name_similarity + 0.25 * distance_score + 0.20 * float(city_match)
    return {
        "display_name": candidate.get("display_name"),
        "lat": float(candidate["lat"]),
        "lng": float(candidate["lon"]),
        "osm_type": candidate.get("osm_type"),
        "osm_id": candidate.get("osm_id"),
        "category": candidate.get("category") or candidate.get("class"),
        "type": candidate.get("type"),
        "name_similarity": round(name_similarity, 4),
        "distance_km": round(distance, 3),
        "city_match": city_match,
        "confidence": round(confidence, 4),
    }


class NominatimClient:
    def __init__(
        self,
        workspace: str | Path,
        delay: float = NOMINATIM_DELAY_SECONDS,
        timeout: float = ENRICHMENT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.cache_dir = Path(workspace) / "cache" / "nominatim"
        self.delay = max(delay, 1.0)
        self.last_request_at = 0.0
        self.client = httpx.Client(
            base_url=NOMINATIM_BASE_URL,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "vi"},
            timeout=timeout,
        )

    def close(self) -> None:
        self.client.close()

    def search(self, query: str, refresh: bool = False) -> dict[str, Any]:
        path = cache_path(self.cache_dir, query)
        if path.exists() and not refresh:
            return read_json(path)
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        payload: dict[str, Any] = {"query": query, "fetched_at": utc_now(), "results": [], "error": None}
        try:
            response = self.client.get(
                "/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "namedetails": 1,
                    "limit": 3,
                    "countrycodes": "vn",
                },
            )
            self.last_request_at = time.monotonic()
            response.raise_for_status()
            payload["results"] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_request_at = time.monotonic()
            payload["error"] = f"{type(exc).__name__}: {exc}"
        write_json(path, payload)
        return payload

    def reverse(self, lat: float, lng: float, refresh: bool = False) -> dict[str, Any]:
        key = f"reverse|{lat:.7f}|{lng:.7f}"
        path = cache_path(self.cache_dir, key)
        if path.exists() and not refresh:
            cached = read_json(path)
            valid_results = [
                result
                for result in cached.get("results", [])
                if result.get("lat") is not None and result.get("lon") is not None
            ]
            if cached.get("results") and not valid_results and not cached.get("error"):
                cached["error"] = "provider_no_result: cached response has no coordinates"
            cached["results"] = valid_results
            return cached
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        payload: dict[str, Any] = {"query": key, "fetched_at": utc_now(), "results": [], "error": None}
        try:
            response = self.client.get(
                "/reverse",
                params={
                    "lat": lat,
                    "lon": lng,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "namedetails": 1,
                    "zoom": 18,
                },
            )
            self.last_request_at = time.monotonic()
            if response.status_code == 404:
                write_json(path, payload)
                return payload
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("lat") is not None and result.get("lon") is not None:
                payload["results"] = [result]
            else:
                provider_error = result.get("error") if isinstance(result, dict) else "invalid response"
                payload["error"] = f"provider_no_result: {provider_error}"
        except (httpx.HTTPError, ValueError) as exc:
            self.last_request_at = time.monotonic()
            payload["error"] = f"{type(exc).__name__}: {exc}"
        write_json(path, payload)
        return payload


def enrich_missing_addresses(
    data_dir: str | Path,
    workspace: str | Path,
    *,
    limit: int | None = None,
    refresh: bool = False,
    delay: float = 1.1,
) -> dict[str, Any]:
    places = [place for place in load_verified_places(data_dir) if not place.get("address")]
    if limit is not None:
        places = places[:limit]
    client = NominatimClient(workspace, delay=delay)
    candidates: list[dict[str, Any]] = []
    try:
        for place in places:
            query = f"{place['name']}, {place['city']}, Việt Nam"
            response = client.search(query, refresh=refresh)
            lookup_method = "name_search"
            raw_results = [
                result
                for result in response.get("results", [])
                if result.get("lat") is not None and result.get("lon") is not None
            ]
            if not raw_results and not response.get("error"):
                coords = place.get("coordinates") or {}
                response = client.reverse(float(coords["lat"]), float(coords["lng"]), refresh=refresh)
                raw_results = [
                    result
                    for result in response.get("results", [])
                    if result.get("lat") is not None and result.get("lon") is not None
                ]
                lookup_method = "coordinate_reverse"
            scored = sorted(
                (score_candidate(place, result) for result in raw_results),
                key=lambda row: row["confidence"],
                reverse=True,
            )
            best = scored[0] if scored else None
            if lookup_method == "coordinate_reverse" and best:
                status = "reverse_context_review"
            elif best and best["confidence"] >= 0.75 and best["distance_km"] <= 2 and best["city_match"]:
                status = "high_confidence_review"
            elif best:
                status = "manual_review"
            else:
                status = "no_match"
            candidates.append(
                {
                    "place_id": place["id"],
                    "name": place["name"],
                    "city": place.get("city"),
                    "entity_type": place.get("entity_type"),
                    "original_coordinates": place.get("coordinates"),
                    "query": query,
                    "lookup_method": lookup_method,
                    "status": status,
                    "recommended_index": 0 if scored else None,
                    "candidates": scored,
                    "provider": "OpenStreetMap Nominatim",
                    "attribution": "Data © OpenStreetMap contributors, ODbL 1.0",
                    "fetched_at": response.get("fetched_at"),
                    "error": response.get("error"),
                }
            )
    finally:
        client.close()

    output_dir = Path(workspace) / "output"
    candidates_path = output_dir / "address_candidates.jsonl"
    write_jsonl(candidates_path, candidates)
    report = {
        "generated_at": utc_now(),
        "requested": len(places),
        "high_confidence_review": sum(1 for row in candidates if row["status"] == "high_confidence_review"),
        "manual_review": sum(1 for row in candidates if row["status"] == "manual_review"),
        "reverse_context_review": sum(1 for row in candidates if row["status"] == "reverse_context_review"),
        "no_match": sum(1 for row in candidates if row["status"] == "no_match"),
        "errors": sum(1 for row in candidates if row.get("error")),
        "candidates_path": str(candidates_path.resolve()),
        "merge_performed": False,
    }
    write_json(output_dir / "address_report.json", report)
    return report
