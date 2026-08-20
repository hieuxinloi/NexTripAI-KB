from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.crawl import RawJsonWriter, compute_content_hash
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
)
from nextrip_pipeline.jobs.trivago_mcp_batch import (
    TrivagoBatchItemStatus,
    TrivagoBatchSummaryWriter,
    TrivagoMcpBatchRunner,
    TrivagoPriceBatchContext,
)
from nextrip_pipeline.decision_gate import HotelPriceDecisionWriter
from nextrip_pipeline.preprocessing import NormalizedHotelPriceWriter
from nextrip_pipeline.publishing import CurrentHotelPriceWriter
from nextrip_pipeline.quality import (
    CurrentTrivagoMappingWriter,
    TrivagoDiscoveryAuditWriter,
    TrivagoDiscoveryResolver,
)
from nextrip_pipeline.schemas import (
    EntityType,
    Occupancy,
    RecordSubjectType,
    SourceRecord,
)
from nextrip_pipeline.validators import ValidationResultWriter


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def _entry(number: int) -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id=f"hotel-{number}",
        master_name=f"Hotel Number {number}",
        city="Đà Nẵng",
        address=f"{number} Bạch Đằng",
        search_query=f"Hotel Number {number}, {number} Bạch Đằng, Đà Nẵng, Việt Nam",
    )


class FixtureAdapter:
    def __init__(self) -> None:
        self.requests = []

    def search(self, target, request, *, run_id):
        self.requests.append((target, request, run_id))
        if target.entity_id == "hotel-2":
            raise RuntimeError("temporary MCP failure")
        if target.entity_id == "hotel-3":
            structured = {"content": "no accommodations field"}
        else:
            structured = {
                "accommodations": [
                    {
                        "accommodation_id": "real-hotel-1",
                        "accommodation_name": "Hotel Number 1",
                        "city": "Đà Nẵng",
                        "currency": "VND",
                        "price_per_night": "1.200.000 đ",
                        "price_per_stay": "2.400.000 đ",
                        "advertisers": "Booking.com",
                    }
                ]
            }
        payload = {
            "request": {
                "entity_id": target.entity_id,
                "arguments": {
                    "arrival": request.check_in.isoformat(),
                    "departure": request.check_out.isoformat(),
                    "adults": request.occupancy.adults,
                    "children": request.occupancy.children,
                    "rooms": request.occupancy.rooms,
                    "currency": request.currency,
                },
            },
            "response": {"result": {"structuredContent": structured}},
        }
        return SourceRecord(
            source_record_id=f"source-{target.entity_id}",
            run_id=run_id,
            source_id="trivago-mcp",
            entity_type=EntityType.HOTEL,
            subject_type=RecordSubjectType.HOTEL_PRICE,
            subject_id=target.entity_id,
            crawled_at=NOW,
            raw_payload=payload,
            content_hash=compute_content_hash(payload),
            parser_version="test",
        )


def test_batch_isolates_failures_and_stops_before_price_publication(tmp_path) -> None:
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[_entry(3), _entry(1), _entry(2)],
    )
    adapter = FixtureAdapter()
    runner = TrivagoMcpBatchRunner(
        adapter,
        RawJsonWriter(tmp_path / "raw"),
        NormalizedHotelPriceWriter(tmp_path / "normalized"),
        TrivagoDiscoveryAuditWriter(tmp_path / "quality"),
        CurrentTrivagoMappingWriter(tmp_path / "mappings"),
        TrivagoBatchSummaryWriter(tmp_path / "runs"),
        resolver=TrivagoDiscoveryResolver(clock=lambda: NOW),
        clock=lambda: NOW,
    )
    context = TrivagoPriceBatchContext(
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 22),
        occupancy=Occupancy(adults=3, rooms=2),
    )

    summary, summary_path = runner.run(registry, context, run_id="batch-1")

    assert summary.selected_count == 3
    assert summary.succeeded_count == 1
    assert summary.unresolved_count == 1
    assert summary.failed_count == 1
    assert summary.normalized_observation_count == 1
    assert summary.request_context.occupancy.adults == 3
    by_id = {item.entity_id: item for item in summary.items}
    assert by_id["hotel-1"].status is TrivagoBatchItemStatus.SUCCEEDED
    assert by_id["hotel-2"].error_stage == "capture"
    assert by_id["hotel-3"].status is TrivagoBatchItemStatus.UNRESOLVED
    assert (tmp_path / "mappings" / "hotel-1.json").exists()
    assert not (tmp_path / "mappings" / "hotel-3.json").exists()
    assert summary_path.exists()
    assert not (tmp_path / "current" / "hotel_price").exists()
    assert [call[0].entity_id for call in adapter.requests] == [
        "hotel-1",
        "hotel-2",
        "hotel-3",
    ]


def test_batch_can_be_bounded_with_deterministic_offset(tmp_path) -> None:
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[_entry(3), _entry(1), _entry(2)],
    )
    adapter = FixtureAdapter()
    runner = TrivagoMcpBatchRunner(
        adapter,
        RawJsonWriter(tmp_path / "raw"),
        NormalizedHotelPriceWriter(tmp_path / "normalized"),
        TrivagoDiscoveryAuditWriter(tmp_path / "quality"),
        CurrentTrivagoMappingWriter(tmp_path / "mappings"),
        TrivagoBatchSummaryWriter(tmp_path / "runs"),
        max_requests=1,
        offset=2,
        resolver=TrivagoDiscoveryResolver(clock=lambda: NOW),
        clock=lambda: NOW,
    )

    summary, _ = runner.run(
        registry,
        TrivagoPriceBatchContext(
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 21),
        ),
        run_id="batch-offset",
    )

    assert [item.entity_id for item in summary.items] == ["hotel-3"]


def test_full_batch_validates_decides_and_materializes_contextual_current(
    tmp_path,
) -> None:
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[_entry(1)],
    )
    runner = TrivagoMcpBatchRunner(
        FixtureAdapter(),
        RawJsonWriter(tmp_path / "raw"),
        NormalizedHotelPriceWriter(tmp_path / "normalized"),
        TrivagoDiscoveryAuditWriter(tmp_path / "quality"),
        CurrentTrivagoMappingWriter(tmp_path / "mappings"),
        TrivagoBatchSummaryWriter(tmp_path / "runs"),
        validation_writer=ValidationResultWriter(tmp_path / "validation"),
        decision_writer=HotelPriceDecisionWriter(tmp_path / "decisions"),
        current_writer=CurrentHotelPriceWriter(
            tmp_path / "current" / "hotel_price",
            clock=lambda: NOW,
        ),
        resolver=TrivagoDiscoveryResolver(clock=lambda: NOW),
        clock=lambda: NOW,
    )

    summary, _ = runner.run(
        registry,
        TrivagoPriceBatchContext(
            check_in=date(2026, 8, 20),
            check_out=date(2026, 8, 22),
        ),
        run_id="batch-full",
    )

    assert summary.succeeded_count == 1
    assert summary.normalized_observation_count == 1
    assert summary.validation_count == 4
    assert summary.decision_counts == {"pass": 1}
    assert summary.published_current_count == 1
    item = summary.items[0]
    assert item.validation_count == 4
    assert item.decision_counts == {"pass": 1}
    assert item.published_current_count == 1
    current_files = list((tmp_path / "current" / "hotel_price").rglob("*.json"))
    assert len(current_files) == 1
    assert "checkin=2026-08-20" in current_files[0].as_posix()


def test_batch_cli_defaults_to_tomorrow_one_night_and_two_adults_one_room() -> None:
    arguments = cli_module.build_parser().parse_args(["batch-trivago-prices"])

    assert arguments.check_in is None
    assert arguments.check_out is None
    assert arguments.check_in_offset_days == 1
    assert arguments.stay_nights == 1
    assert (arguments.adults, arguments.children, arguments.rooms) == (2, 0, 1)
    assert arguments.children_ages == []
    assert not hasattr(arguments, "headed")
    assert not hasattr(arguments, "artifact_dir")


def test_batch_cli_connects_mcp_to_quality_and_contextual_current(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, *args, **kwargs) -> None:
            captured["constructor_args"] = args
            captured["constructor_kwargs"] = kwargs

        def run(self, registry, context, *, run_id):
            captured["registry"] = registry
            captured["context"] = context
            captured["run_id"] = run_id
            return (
                SimpleNamespace(
                    selected_count=1,
                    succeeded_count=1,
                    unresolved_count=0,
                    failed_count=0,
                    normalized_observation_count=1,
                    validation_count=4,
                    decision_counts={"pass": 1},
                    published_current_count=1,
                    items=[],
                ),
                Path("summary.json"),
            )

    registry = object()
    monkeypatch.setattr(cli_module, "_load_trivago_registry", lambda path: registry)
    monkeypatch.setattr(cli_module, "TrivagoMcpBatchRunner", FakeRunner)
    monkeypatch.setattr(
        cli_module,
        "PlaywrightBrowserClient",
        lambda *args, **kwargs: pytest.fail("Trivago MCP batch must not use Playwright"),
    )

    exit_code = cli_module.main(
        [
            "batch-trivago-prices",
            "--check-in",
            "2026-08-20",
            "--check-out",
            "2026-08-22",
        ]
    )

    assert exit_code == 0
    context = captured["context"]
    assert isinstance(context, TrivagoPriceBatchContext)
    assert (context.check_in, context.check_out) == (
        date(2026, 8, 20),
        date(2026, 8, 22),
    )
    assert context.occupancy == Occupancy(adults=2, children=0, rooms=1)
    constructor_kwargs = captured["constructor_kwargs"]
    assert isinstance(constructor_kwargs, dict)
    assert constructor_kwargs["validation_writer"] is not None
    assert constructor_kwargs["decision_writer"] is not None
    assert constructor_kwargs["current_writer"] is not None
    assert "current=1" in capsys.readouterr().out
