from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.dataset import (
    materialize_canonical_active_dataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.cli import build_parser
from nextrip_pipeline.crawl import (
    TrivagoRegistryBuilder,
    TrivagoRegistryStatus,
    TrivagoSearchReviewEvidence,
    TrivagoSearchReviewOverride,
)
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def _canonical_dataset():  # type: ignore[no-untyped-def]
    specs = (
        ("hotel_dn_001", EntityType.HOTEL, "Canonical Hotel"),
        ("cafe_dn_001", EntityType.CAFE, "Canonical Cafe"),
    )
    slots = [
        LegacyPlaceSlot(
            legacy_place_id=place_id,
            city_id="city_da_nang",
            primary_type=entity_type,
        )
        for place_id, entity_type, _ in specs
    ]
    raw_records = {
        place_id: MasterRawRecord(
            place_id=place_id,
            source_filename=f"{entity_type.value}_final.json",
            record_index=index,
            raw_record={
                "id": place_id,
                "entity_type": entity_type.value,
                "name": name,
                "city": "Da Nang",
                "address": f"{index + 1} Test Street",
                "coordinates": {
                    "lat": 16.0544 + index / 1000,
                    "lng": 108.2022 + index / 1000,
                },
            },
        )
        for index, (place_id, entity_type, name) in enumerate(specs)
    }
    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id=raw_records,
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=NOW)
    return materialize_canonical_active_dataset(master, manifest)


def test_registry_uses_only_active_canonical_hotels() -> None:
    dataset = _canonical_dataset()

    registry, report = TrivagoRegistryBuilder(
        clock=lambda: NOW
    ).build_from_canonical_dataset(dataset)

    assert [entry.entity_id for entry in registry.entries] == ["hotel_dn_001"]
    assert registry.entries[0].master_name == "Canonical Hotel"
    assert registry.source_file == f"canonical-dataset:{dataset.dataset_id}"
    assert report.total_records == 1
    assert report.city_counts == {"Da Nang": 1}


def test_build_registry_cli_accepts_canonical_dataset_input() -> None:
    arguments = build_parser().parse_args(
        ["build-trivago-registry", "--canonical-dataset", "canonical.json"]
    )

    assert arguments.canonical_dataset.name == "canonical.json"
    assert not hasattr(arguments, "master_file")


def test_build_registry_cli_requires_canonical_dataset() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["build-trivago-registry"])


def test_registry_contains_all_master_hotels_without_fabricated_ids() -> None:
    mapping = ExternalEntityMapping.model_validate_json(
        open("config/trivago-mapping.json", encoding="utf-8").read()
    )
    dataset = read_canonical_active_dataset(
        Path("data/canonical/datasets")
        / "dataset=canonical-active-76a727f96284f0604924"
        / "canonical-active-dataset.json"
    )

    registry, report = TrivagoRegistryBuilder(
        clock=lambda: NOW
    ).build_from_canonical_dataset(
        dataset,
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
    assert sample.address not in sample.search_query
    assert any(sample.address in query for query in sample.search_queries[1:])
    assert sample.search_queries[0] == sample.search_query
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
    assert entry.search_aliases[0] == "Verified Master Hotel"
    assert entry.search_queries[0] == entry.search_query
    assert entry.address not in entry.search_queries[0]
    assert any(entry.address in query for query in entry.search_queries[1:])


def test_reviewed_alias_is_prioritized_and_pinned_evidence_is_validated(
    tmp_path,
) -> None:
    master = {
        "metadata": {"total_count": 1},
        "data": [
            {
                "id": "hotel-1",
                "entity_type": "hotel",
                "name": "Legacy Daisy Property",
                "city": "Da Nang",
                "address": "67 Che Lan Vien, Da Nang",
                "coordinates": {"lat": 16.0407, "lng": 108.2466},
            }
        ],
    }
    master_path = tmp_path / "hotel.json"
    master_path.write_text(json.dumps(master), encoding="utf-8")
    resolution = {
        "candidates": [
            {
                "external_id": "reviewed-daisy-id",
                "name": "Daisy Boutique Hotel",
                "external_url": (
                    "https://www.trivago.vn/vi/lm/daisy"
                    "?search=100-19017974;dr-20260823-20260824"
                ),
                "city_evidence": "match",
            }
        ]
    }
    evidence_path = tmp_path / "resolution.json"
    evidence_path.write_text(json.dumps(resolution), encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    override = TrivagoSearchReviewOverride(
        entity_id="hotel-1",
        aliases=["Daisy Boutique Hotel"],
        expected_external_id="reviewed-daisy-id",
        expected_property_id="19017974",
        expected_name="Daisy Boutique Hotel",
        reviewer="Oanhh-approved-agent-review",
        reviewed_at=NOW,
        reason="exact identity reviewed",
        evidence=[
            TrivagoSearchReviewEvidence(
                path="resolution.json", file_sha256=evidence_hash
            )
        ],
    )

    registry, report = TrivagoRegistryBuilder(clock=lambda: NOW).build(
        master_path,
        search_review_overrides=[override],
        evidence_root=tmp_path,
    )

    entry = registry.entries[0]
    assert entry.search_aliases[0] == "Daisy Boutique Hotel"
    assert "Daisy Boutique Hotel" in entry.search_queries[1]
    assert entry.address not in entry.search_queries[1]
    assert entry.review_target_hash is not None
    assert report.search_review_overrides_applied == 1

    tampered = override.model_copy(
        update={
            "evidence": [
                TrivagoSearchReviewEvidence(
                    path="resolution.json", file_sha256="0" * 64
                )
            ]
        }
    )
    try:
        TrivagoRegistryBuilder(clock=lambda: NOW).build(
            master_path,
            search_review_overrides=[tampered],
            evidence_root=tmp_path,
        )
    except ValueError as error:
        assert "evidence hash mismatch" in str(error)
    else:
        raise AssertionError("tampered review evidence should fail registry build")


def test_complete_reviewed_alias_does_not_duplicate_city_or_country(tmp_path) -> None:
    master = {
        "metadata": {"total_count": 1},
        "data": [
            {
                "id": "hotel_dn_043",
                "entity_type": "hotel",
                "name": "Voco Ma Belle Danang By Ihg",
                "city": "Đà Nẵng",
                "address": "168 Võ Nguyên Giáp, Đà Nẵng, Việt Nam",
                "coordinates": {"lat": 16.07414, "lng": 108.24464},
            }
        ],
    }
    master_path = tmp_path / "hotel.json"
    master_path.write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")
    exact_query = "Voco Ma Belle Danang By Ihg, Đà Nẵng, Vietnam"
    override = TrivagoSearchReviewOverride(
        entity_id="hotel_dn_043",
        aliases=[exact_query],
    )

    registry, _ = TrivagoRegistryBuilder(clock=lambda: NOW).build(
        master_path,
        search_review_overrides=[override],
    )

    assert registry.entries[0].search_queries[1] == exact_query


def test_terminal_provider_review_requires_entity_owned_immutable_evidence(
    tmp_path,
) -> None:
    master = {
        "metadata": {"total_count": 1},
        "data": [
            {
                "id": "hotel-1",
                "entity_type": "hotel",
                "name": "Hotel One",
                "city": "Da Nang",
                "address": "1 Beach Street",
            }
        ],
    }
    master_path = tmp_path / "hotel.json"
    master_path.write_text(json.dumps(master), encoding="utf-8")
    evidence_path = tmp_path / "resolution.json"
    evidence_path.write_text(
        json.dumps({"entity_id": "hotel-1", "candidates": []}),
        encoding="utf-8",
    )
    evidence = TrivagoSearchReviewEvidence(
        path=evidence_path.name,
        file_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    )
    override = TrivagoSearchReviewOverride(
        entity_id="hotel-1",
        final_status=TrivagoRegistryStatus.PROVIDER_NOT_LISTED,
        reviewer="Oanhh-approved-agent-review",
        reviewed_at=NOW,
        reason="reviewed searches returned no safe Trivago listing",
        evidence=[evidence],
    )

    registry, report = TrivagoRegistryBuilder(clock=lambda: NOW).build(
        master_path,
        search_review_overrides=[override],
        evidence_root=tmp_path,
    )

    entry = registry.entries[0]
    assert entry.status is TrivagoRegistryStatus.PROVIDER_NOT_LISTED
    assert report.status_counts == {"provider_not_listed": 1}
    assert entry.review_target_reviewer == "Oanhh-approved-agent-review"
    with pytest.raises(ValueError, match="only a confirmed"):
        entry.to_mapping()

    wrong_entity_path = tmp_path / "wrong-resolution.json"
    wrong_entity_path.write_text(
        json.dumps({"entity_id": "hotel-2", "candidates": []}),
        encoding="utf-8",
    )
    wrong = override.model_copy(
        update={
            "evidence": [
                TrivagoSearchReviewEvidence(
                    path=wrong_entity_path.name,
                    file_sha256=hashlib.sha256(
                        wrong_entity_path.read_bytes()
                    ).hexdigest(),
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="not entity-owned"):
        TrivagoRegistryBuilder(clock=lambda: NOW).build(
            master_path,
            search_review_overrides=[wrong],
            evidence_root=tmp_path,
        )


def test_review_aliases_are_deduplicated_by_case_and_diacritics() -> None:
    override = TrivagoSearchReviewOverride(
        entity_id="hotel-1",
        aliases=[
            "Daisy Boutique Hotel",
            "daisy boutique hotel",
            "Dáisy Boutique Hotel",
        ],
    )

    assert override.aliases == ["Daisy Boutique Hotel"]


def test_registry_rejects_confirmed_external_and_property_collisions(
    tmp_path,
) -> None:
    master = {
        "metadata": {"total_count": 2},
        "data": [
            {
                "id": "hotel-1",
                "entity_type": "hotel",
                "name": "Hotel One",
                "city": "Da Nang",
            },
            {
                "id": "hotel-2",
                "entity_type": "hotel",
                "name": "Hotel Two",
                "city": "Da Nang",
            },
        ],
    }
    master_path = tmp_path / "hotel.json"
    master_path.write_text(json.dumps(master), encoding="utf-8")

    def mapping(entity_id: str, external_id: str, property_id: str):
        return ExternalEntityMapping.model_validate(
            {
                "mapping_id": f"trivago-{entity_id}",
                "entity_id": entity_id,
                "entity_type": "hotel",
                "source_id": "trivago-mcp",
                "external_id": external_id,
                "external_url": (
                    "https://www.trivago.vn/vi/lm/hotel"
                    f"?search=100-{property_id};dr-20260823-20260824"
                ),
                "status": "confirmed",
                "matched_at": NOW,
                "verified_at": NOW,
                "last_checked_at": NOW,
            }
        )

    with pytest.raises(ValueError, match="external_id collision"):
        TrivagoRegistryBuilder(clock=lambda: NOW).build(
            master_path,
            overrides=[
                mapping("hotel-1", "shared-id", "1001"),
                mapping("hotel-2", "shared-id", "1002"),
            ],
        )
    with pytest.raises(ValueError, match="property_id collision"):
        TrivagoRegistryBuilder(clock=lambda: NOW).build(
            master_path,
            overrides=[
                mapping("hotel-1", "external-1", "1001"),
                mapping("hotel-2", "external-2", "1001"),
            ],
        )
