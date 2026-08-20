from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from nextrip_current.models import HotelOfferSearchRequest
from nextrip_current.refresh import (
    TrivagoOnDemandPriceRefresher,
    TrivagoRefreshPaths,
)
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
    master_file = tmp_path / "travel_data_verified" / "hotel_final.json"
    master_file.parent.mkdir(parents=True)
    master_file.write_text(
        json.dumps(
            {
                "metadata": {"total_count": 1},
                "data": [
                    {
                        "id": "hotel_dn_001",
                        "entity_type": "hotel",
                        "name": "Old Master Hotel Name",
                        "city": "Da Nang",
                        "address": "1 Test Street",
                        "coordinates": {"lat": 16.0544, "lng": 108.2022},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return TrivagoRefreshPaths(
        master_file=master_file,
        checked_in_mapping_files=(),
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
