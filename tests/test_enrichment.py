from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from nextrip_graphrag.enrichment.catalog import (
    build_source_artifacts,
    build_source_catalog,
    build_text_units,
    load_verified_places,
)
from nextrip_graphrag.enrichment.nominatim import haversine_km, score_candidate
from nextrip_graphrag.enrichment.source_crawler import (
    MIN_DOCUMENT_CHARS,
    extract_page_text,
)
from nextrip_graphrag.enrichment.text_units import chunk_text, find_mentions
from nextrip_graphrag.enrichment.embeddings import CachedBatchEmbedder
from nextrip_graphrag.normalizer import canonical_city, normalize_dataset, price_summary
from tests.canonical_dataset_support import write_legacy_projection_from_canonical


def _source_url_count(places: list[dict[str, object]]) -> int:
    return len(
        {
            str(source.get("url")).strip()
            for place in places
            if isinstance((source := place.get("source")), dict) and source.get("url")
        }
    )


def test_canonical_projection_source_catalog_has_expected_coverage(tmp_path) -> None:
    data_dir = write_legacy_projection_from_canonical(tmp_path / "legacy-projection")
    places = load_verified_places(data_dir)
    catalog = build_source_catalog(places)
    text_units = build_text_units(places)

    assert len(places) == 692
    assert len(catalog) == _source_url_count(places)
    assert sum(item["place_count"] for item in catalog) == 692
    assert len(text_units) == 692


def test_canonical_projection_normalizes_to_expected_manifest(tmp_path) -> None:
    data_dir = write_legacy_projection_from_canonical(tmp_path / "legacy-projection")
    bundle = normalize_dataset(data_dir)

    assert bundle["manifest"] == {
        "total_places": 692,
        "city_counts": {
            "city_quy_nhon": 306,
            "city_da_nang": 386,
        },
        "entity_type_counts": {
            "attraction": 118,
            "cafe": 118,
            "hotel": 73,
            "nightlife": 169,
            "restaurant": 214,
        },
        "raw_files": [
            "attraction_final.json",
            "cafe_final.json",
            "hotel_final.json",
            "nightlife_final.json",
            "restaurant_final.json",
        ],
    }

    places = {place["id"]: place for place in bundle["places"]}
    assert len(places) == len(bundle["places"])
    assert {
        place_id
        for place_id, place in places.items()
        if "lat" not in place["props"] or "lng" not in place["props"]
    } == set()


def test_canonical_projection_file_metadata_matches_records(tmp_path) -> None:
    data_dir = write_legacy_projection_from_canonical(tmp_path / "legacy-projection")
    for path in data_dir.glob("*_final.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        metadata = payload["metadata"]
        records = payload["data"]
        city_counts = Counter(canonical_city(record["city"]) for record in records)

        assert metadata["total_count"] == len(records), path.name
        assert metadata["quy_nhon_count"] == city_counts["Quy Nhơn"], path.name
        assert metadata["da_nang_count"] == city_counts["Đà Nẵng"], path.name


def test_source_artifacts_are_staged_outside_verified_data(tmp_path: Path) -> None:
    data_dir = write_legacy_projection_from_canonical(tmp_path / "legacy-projection")
    places = load_verified_places(data_dir)
    report = build_source_artifacts(data_dir, tmp_path / "workspace")

    assert report["place_count"] == 692
    assert report["source_document_count"] == _source_url_count(places)
    assert report["text_unit_count"] == 692
    assert (tmp_path / "workspace" / "output" / "source_catalog.json").exists()
    lines = (
        (tmp_path / "workspace" / "output" / "text_units.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 692
    assert json.loads(lines[0])["evidence_origin"] == "verified_record"


def test_article_parser_ignores_navigation_and_scripts() -> None:
    title, text = extract_page_text(
        """
        <html><head><title>Travel guide</title><script>bad()</script></head>
        <body><nav>Menu</nav><article><h1>Eo Gio</h1><p>Useful travel evidence.</p></article></body></html>
        """
    )

    assert title == "Travel guide"
    assert text == "Eo Gio\nUseful travel evidence."
    assert "Menu" not in text
    assert MIN_DOCUMENT_CHARS == 500


def test_candidate_score_rewards_nearby_city_match() -> None:
    place = {
        "name": "Eo Gió",
        "city": "Quy Nhơn",
        "coordinates": {"lat": 13.8863, "lng": 109.2926},
    }
    candidate = {
        "name": "Eo Gió",
        "display_name": "Eo Gió, Quy Nhơn, Bình Định, Việt Nam",
        "lat": "13.8864",
        "lon": "109.2927",
        "osm_type": "node",
        "osm_id": 1,
        "type": "attraction",
    }

    scored = score_candidate(place, candidate)

    assert haversine_km(13.8863, 109.2926, 13.8864, 109.2927) < 0.1
    assert scored["city_match"] is True
    assert scored["confidence"] > 0.95


def test_chunk_text_overlaps_and_preserves_offsets() -> None:
    text = " ".join(f"word-{index}" for index in range(400))
    chunks = chunk_text(text, max_chars=500, overlap_chars=60)

    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 500 for chunk in chunks)
    assert chunks[1]["char_start"] < chunks[0]["char_end"]
    assert chunks[-1]["char_end"] == len(text)


def test_find_mentions_only_matches_complete_normalized_name() -> None:
    places = [
        {"place_id": "p1", "place_name": "Cầu Rồng"},
        {"place_id": "p2", "place_name": "Bar"},
    ]

    mentions = find_mentions("Buổi tối tại Cau Rong rất đẹp.", places)

    assert [mention["place_id"] for mention in mentions] == ["p1"]


def test_embedding_cache_reuses_vectors(tmp_path: Path) -> None:
    class FakeEmbedder:
        calls = 0

        def embed_documents(self, texts):
            self.calls += 1
            return [[float(len(text))] for text in texts]

        def embed_query(self, text):
            self.calls += 1
            return [float(len(text))]

    fake = FakeEmbedder()
    cached = CachedBatchEmbedder(
        fake,
        tmp_path,
        model="test-model",
        dimensions=1,
        delay=0,
        max_retries=0,
    )

    assert cached.embed_documents(["abc", "abcd"]) == [[3.0], [4.0]]
    assert cached.embed_documents(["abc", "abcd"]) == [[3.0], [4.0]]
    assert fake.calls == 1
    assert cached.embed_query("question") == [8.0]
    assert cached.embed_query("question") == [8.0]
    assert fake.calls == 2


def test_price_summary_preserves_free_entry() -> None:
    summary = price_summary({"entry_fee": {"min": 0, "max": 0, "currency": "VND"}})

    assert any("0-0 VND" in item for item in summary)
