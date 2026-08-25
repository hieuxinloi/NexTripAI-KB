from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from nextrip_current.models import HotelOfferSearchRequest
from nextrip_current.refresh import (
    HotelOfferRefreshError,
    TrivagoOnDemandPriceRefresher,
    TrivagoRefreshPaths,
)
from nextrip_pipeline.canonical.dataset import materialize_canonical_active_dataset
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.crawl.raw_writer import compute_content_hash
from nextrip_pipeline.crawl.adapters import TrivagoSearchStrategy
from nextrip_pipeline.jobs import TrivagoStayStopReason
from nextrip_pipeline.schemas import (
    EntityType,
    Occupancy,
    RecordSubjectType,
    SourceRecord,
)


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


class FakeTrivagoAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def search(
        self,
        target,
        request,
        *,
        run_id: str,
        strategy: TrivagoSearchStrategy = TrivagoSearchStrategy.NAME,
    ) -> SourceRecord:  # type: ignore[no-untyped-def]
        self.calls.append((target.entity_id, run_id))
        accommodation = {
            "accommodation_id": "trivago-hotel-1",
            "accommodation_name": "Old Master Hotel Name",
            "country_city": "Da Nang, Vietnam",
            "latitude": 16.0544,
            "longitude": 108.2022,
            "currency": request.currency,
            "price_per_night": "1.250.000 đ",
            "price_per_stay": "1.250.000 đ",
            "advertisers": "Booking.com",
            "accommodation_url": "https://www.trivago.vn/hotel-1",
        }
        raw_payload = {
            "request": {
                "tool": "trivago-accommodation-search",
                "search_strategy": strategy.value,
                "arguments": {
                    "query": target.search_query,
                    "arrival": request.check_in.isoformat(),
                    "departure": request.check_out.isoformat(),
                    "adults": request.occupancy.adults,
                    "children": request.occupancy.children,
                    "rooms": request.occupancy.rooms,
                    "currency": request.currency,
                },
                "entity_id": target.entity_id,
            },
            "response": {
                "result": {"structuredContent": {"accommodations": [accommodation]}}
            },
        }
        return SourceRecord(
            source_record_id="source-record-1",
            run_id=run_id,
            source_id="trivago-mcp",
            entity_type=EntityType.HOTEL,
            subject_type=RecordSubjectType.HOTEL_PRICE,
            subject_id=target.entity_id,
            crawled_at=NOW,
            raw_payload=raw_payload,
            content_hash=compute_content_hash(raw_payload),
            parser_version="test",
            source_url="https://mcp.trivago.com/mcp",
            http_status=200,
            content_type="application/json",
        )


def _paths(tmp_path: Path) -> TrivagoRefreshPaths:
    place_id = "hotel_dn_001"
    raw_record = {
        "id": place_id,
        "entity_type": "hotel",
        "name": "Old Master Hotel Name",
        "city": "Da Nang",
        "address": "1 Test Street",
        "coordinates": {"lat": 16.0544, "lng": 108.2022},
    }
    slot = LegacyPlaceSlot(
        legacy_place_id=place_id,
        city_id="city_da_nang",
        primary_type=EntityType.HOTEL,
    )
    master = CanonicalMasterLoad(
        slots=[slot],
        raw_records_by_id={
            place_id: MasterRawRecord(
                place_id=place_id,
                source_filename="hotel_final.json",
                record_index=0,
                raw_record=raw_record,
            )
        },
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest([slot], generated_at=NOW)
    dataset = materialize_canonical_active_dataset(master, manifest)
    canonical_dataset_file = tmp_path / "canonical-active-dataset.json"
    canonical_dataset_file.write_text(
        dataset.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return TrivagoRefreshPaths(
        canonical_dataset_file=canonical_dataset_file,
        evidence_root=tmp_path,
        checked_in_mapping_files=(),
        search_review_file=None,
        current_mapping_directory=tmp_path / "data/current/trivago_mappings",
        raw_directory=tmp_path / "data/raw",
        normalized_directory=tmp_path / "data/normalized",
        quality_directory=tmp_path / "data/quality/trivago_mapping",
        validation_directory=tmp_path / "data/validation",
        decision_directory=tmp_path / "data/decisions",
        current_price_directory=tmp_path / "data/current/hotel_price",
        current_availability_directory=(tmp_path / "data/current/hotel_availability"),
        summary_directory=tmp_path / "data/runs/trivago_mcp",
    )


def test_refresh_paths_resolve_canonical_dataset_from_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relative = Path("data/canonical/active.json")
    monkeypatch.setenv("NEXTRIP_CANONICAL_DATASET", str(relative))

    paths = TrivagoRefreshPaths.from_kb_root(tmp_path)

    assert paths.canonical_dataset_file == (tmp_path / relative).resolve()
    assert paths.evidence_root == tmp_path.resolve()


def test_refresh_paths_reject_missing_canonical_dataset_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXTRIP_CANONICAL_DATASET", raising=False)

    with pytest.raises(ValueError, match="NEXTRIP_CANONICAL_DATASET"):
        TrivagoRefreshPaths.from_kb_root(tmp_path)


def test_on_demand_refresh_runs_full_pipeline_for_one_exact_context(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    adapter = FakeTrivagoAdapter()
    refresher = TrivagoOnDemandPriceRefresher(
        paths,
        adapter=adapter,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    request = HotelOfferSearchRequest(
        hotel_ids=["hotel_dn_001"],
        check_in=date(2026, 8, 21),
        check_out=date(2026, 8, 22),
        occupancy=Occupancy(adults=2, children=0, rooms=1),
        currency="VND",
        refresh_if_missing=True,
    )

    result = refresher.refresh("hotel_dn_001", request)

    assert result.stop_reason is TrivagoStayStopReason.AVAILABLE_FOUND
    assert result.selected_available_offset_days == 0
    assert len(result.attempts) == 1
    assert len(result.attempts[0].prices) == 1
    assert len(adapter.calls) == 1

    mapping_payload = json.loads(
        (paths.current_mapping_directory / "hotel_dn_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert mapping_payload["entity_id"] == "hotel_dn_001"
    assert mapping_payload["attributes"]["master_name"] == "Old Master Hotel Name"
    assert mapping_payload["attributes"]["trivago_name"] == "Old Master Hotel Name"

    current_files = list(paths.current_price_directory.rglob("*.json"))
    assert len(current_files) == 1
    current_payload = json.loads(current_files[0].read_text(encoding="utf-8"))
    assert current_payload["hotel_id"] == "hotel_dn_001"
    assert current_payload["observation"]["nightly_amount"] == "1250000"
    assert current_payload["observation"]["check_in"] == "2026-08-21"
    assert current_payload["observation"]["occupancy"] == {
        "adults": 2,
        "children": 0,
        "rooms": 1,
    }
    availability_files = list(paths.current_availability_directory.rglob("*.json"))
    assert len(availability_files) == 1
    availability_payload = json.loads(availability_files[0].read_text(encoding="utf-8"))
    assert availability_payload["observation"]["status"] == "available"
    assert len(list(paths.summary_directory.glob("run=*.json"))) == 1
    assert len(
        list((tmp_path / "data" / "observations").rglob("observation=*.json"))
    ) == 2


def test_on_demand_refresh_respects_terminal_provider_review(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    evidence_path = tmp_path / "review-evidence.json"
    evidence_path.write_text(
        json.dumps({"entity_id": "hotel_dn_001", "candidates": []}),
        encoding="utf-8",
    )
    review_path = tmp_path / "config" / "trivago-search-review.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "overrides": [
                    {
                        "entity_id": "hotel_dn_001",
                        "final_status": "provider_not_listed",
                        "reviewer": "Oanhh",
                        "reviewed_at": NOW.isoformat(),
                        "reason": "reviewed provider listing is absent",
                        "evidence": [
                            {
                                "path": evidence_path.name,
                                "file_sha256": hashlib.sha256(
                                    evidence_path.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths = replace(paths, search_review_file=review_path)
    adapter = FakeTrivagoAdapter()
    refresher = TrivagoOnDemandPriceRefresher(
        paths,
        adapter=adapter,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    request = HotelOfferSearchRequest(
        hotel_ids=["hotel_dn_001"],
        check_in=date(2026, 8, 21),
        check_out=date(2026, 8, 22),
        refresh_if_missing=True,
    )

    with pytest.raises(HotelOfferRefreshError, match="provider_not_listed"):
        refresher.refresh("hotel_dn_001", request)

    assert adapter.calls == []
