from __future__ import annotations

import json
from datetime import datetime, timezone

from nextrip_pipeline.crawl import (
    TrivagoRegistryBuilder,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.schemas import ExternalEntityMapping


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def test_registry_contains_all_master_hotels_without_fabricated_ids() -> None:
    mapping = ExternalEntityMapping.model_validate_json(
        open("config/trivago-mapping.json", encoding="utf-8").read()
    )

    registry, report = TrivagoRegistryBuilder(clock=lambda: NOW).build(
        "travel_data_verified/hotel_final.json",
        overrides=[mapping],
    )

    assert len(registry.entries) == 73
    assert report.total_records == 73
    assert report.overrides_applied == 1
    assert report.status_counts == {"confirmed": 1, "unresolved": 72}
    assert all(entry.latitude is not None for entry in registry.entries)
    assert all(entry.longitude is not None for entry in registry.entries)
    unresolved = [
        entry
        for entry in registry.entries
        if entry.status is TrivagoRegistryStatus.UNRESOLVED
    ]
    assert all(entry.external_id is None for entry in unresolved)
    confirmed = registry.confirmed_mappings()
    assert [(item.entity_id, item.external_id) for item in confirmed] == [
        ("hotel_qn_025", "292003c34d4f")
    ]
    sample = registry.entries[0]
    assert sample.master_name in sample.search_query
    assert sample.city in sample.search_query
    assert -90 <= sample.latitude <= 90
    assert -180 <= sample.longitude <= 180
    confirmed_mapping = confirmed[0]
    assert isinstance(confirmed_mapping.attributes["master_latitude"], float)
    assert isinstance(confirmed_mapping.attributes["master_longitude"], float)


def test_registry_rejects_master_count_mismatch(tmp_path) -> None:
    master = {
        "metadata": {"total_count": 2},
        "data": [
            {
                "id": "hotel-1",
                "entity_type": "hotel",
                "name": "Hotel One",
                "city": "Đà Nẵng",
                "address": None,
            }
        ],
    }
    path = tmp_path / "hotel.json"
    path.write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")

    try:
        TrivagoRegistryBuilder(clock=lambda: NOW).build(path)
    except ValueError as error:
        assert "metadata.total_count=2" in str(error)
    else:
        raise AssertionError("invalid master count should fail")


def test_registry_round_trips_confirmed_trivago_and_master_names(tmp_path) -> None:
    master = {
        "metadata": {"total_count": 1},
        "data": [
            {
                "id": "hotel-1",
                "entity_type": "hotel",
                "name": "Verified Master Hotel",
                "city": "ÄÃ  Náºµng",
                "address": "1 Báº¡ch Äáº±ng",
            }
        ],
    }
    master_path = tmp_path / "hotel.json"
    master_path.write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")
    override = ExternalEntityMapping.model_validate(
        {
            "mapping_id": "trivago-hotel-1",
            "entity_id": "hotel-1",
            "entity_type": "hotel",
            "source_id": "trivago-mcp",
            "external_id": "external-1",
            "status": "confirmed",
            "matched_at": NOW,
            "verified_at": NOW,
            "last_checked_at": NOW,
            "attributes": {
                "master_name": "Stale Master Name",
                "trivago_name": "Canonical Trivago Hotel",
            },
        }
    )

    registry, _ = TrivagoRegistryBuilder(clock=lambda: NOW).build(
        master_path, overrides=[override]
    )
    entry = registry.entries[0]
    mapping = entry.to_mapping()

    assert entry.entity_id == "hotel-1"
    assert entry.master_name == "Verified Master Hotel"
    assert entry.trivago_name == "Canonical Trivago Hotel"
    assert entry.search_name == "Canonical Trivago Hotel"
    assert "Canonical Trivago Hotel" in entry.search_query
    assert "Verified Master Hotel" not in entry.search_query
    assert mapping.attributes["master_name"] == "Verified Master Hotel"
    assert mapping.attributes["trivago_name"] == "Canonical Trivago Hotel"
