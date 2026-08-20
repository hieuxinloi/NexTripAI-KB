from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
)
from nextrip_pipeline.jobs import (
    TrivagoPriceBatchContext,
    TrivagoStayAvailabilityBatchRunner,
    TrivagoStayBatchItemStatus,
    TrivagoStayBatchSummaryWriter,
    TrivagoStayStopReason,
)
from nextrip_pipeline.schemas import HotelAvailabilityStatus, Occupancy


NOW = datetime(2026, 8, 20, 5, tzinfo=timezone.utc)


def _entry(number: int) -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id=f"hotel-{number}",
        master_name=f"Hotel {number}",
        city="Da Nang",
        search_query=f"Hotel {number}, Da Nang, Vietnam",
    )


class FixtureStayRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def run(self, entry, context, *, run_id, lookahead_days):
        self.calls.append((entry.entity_id, run_id, lookahead_days))
        if entry.entity_id == "hotel-2":
            raise RuntimeError("temporary hotel failure")
        if entry.entity_id == "hotel-3":
            statuses = [
                HotelAvailabilityStatus.UNAVAILABLE,
                HotelAvailabilityStatus.UNAVAILABLE,
            ]
            stop_reason = TrivagoStayStopReason.LOOKAHEAD_EXHAUSTED
            selected_offset = None
        else:
            statuses = [HotelAvailabilityStatus.AVAILABLE]
            stop_reason = TrivagoStayStopReason.AVAILABLE_FOUND
            selected_offset = 0
        return SimpleNamespace(
            run_id=run_id,
            stop_reason=stop_reason,
            selected_available_offset_days=selected_offset,
            attempts=[
                SimpleNamespace(availability=SimpleNamespace(status=status))
                for status in statuses
            ],
        )


class FixtureResultWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.results: list[object] = []

    def write(self, result) -> Path:
        self.results.append(result)
        return self.root / f"run={result.run_id}.json"


def test_stay_batch_is_bounded_and_isolates_each_hotel_failure(tmp_path) -> None:
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[_entry(3), _entry(1), _entry(2)],
    )
    stay_runner = FixtureStayRunner()
    result_writer = FixtureResultWriter(tmp_path / "stay")
    summary_writer = TrivagoStayBatchSummaryWriter(tmp_path / "batch")
    runner = TrivagoStayAvailabilityBatchRunner(
        stay_runner,
        result_writer,  # type: ignore[arg-type]
        summary_writer,
        clock=lambda: NOW,
    )
    context = TrivagoPriceBatchContext(
        check_in=date(2026, 8, 21),
        check_out=date(2026, 8, 24),
        occupancy=Occupancy(adults=3, children=1, rooms=2),
        children_ages=[8],
    )

    summary, summary_path = runner.run(
        registry,
        context,
        run_id="availability-batch-1",
        lookahead_days=1,
    )

    assert [call[0] for call in stay_runner.calls] == [
        "hotel-1",
        "hotel-2",
        "hotel-3",
    ]
    assert all(call[2] == 1 for call in stay_runner.calls)
    assert summary.request_context.check_out == date(2026, 8, 24)
    assert summary.selected_count == 3
    assert summary.completed_count == 2
    assert summary.failed_count == 1
    assert summary.stop_reason_counts == {
        "available_found": 1,
        "lookahead_exhausted": 1,
    }
    assert summary.availability_counts == {"available": 1, "unavailable": 2}
    by_id = {item.hotel_id: item for item in summary.items}
    assert by_id["hotel-1"].status is TrivagoStayBatchItemStatus.COMPLETED
    assert by_id["hotel-2"].status is TrivagoStayBatchItemStatus.FAILED
    assert by_id["hotel-2"].error_stage == "stay_availability"
    assert by_id["hotel-3"].attempt_count == 2
    assert len(result_writer.results) == 2
    assert summary_path.exists()

    with pytest.raises(FileExistsError, match="already exists"):
        summary_writer.write(summary)


def test_stay_batch_supports_entity_filter_offset_and_max_requests(tmp_path) -> None:
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[_entry(4), _entry(2), _entry(3), _entry(1)],
    )
    stay_runner = FixtureStayRunner()
    runner = TrivagoStayAvailabilityBatchRunner(
        stay_runner,
        FixtureResultWriter(tmp_path / "stay"),  # type: ignore[arg-type]
        TrivagoStayBatchSummaryWriter(tmp_path / "batch"),
        entity_ids=["hotel-1", "hotel-3", "hotel-4"],
        offset=1,
        max_requests=1,
        clock=lambda: NOW,
    )

    summary, _ = runner.run(
        registry,
        TrivagoPriceBatchContext(
            check_in=date(2026, 8, 21),
            check_out=date(2026, 8, 22),
        ),
        run_id="bounded-batch",
    )

    assert summary.eligible_count == 3
    assert [item.hotel_id for item in summary.items] == ["hotel-3"]


def test_availability_cli_defaults_and_full_stay_arguments() -> None:
    arguments = cli_module.build_parser().parse_args(["batch-trivago-availability"])

    assert arguments.check_in is None
    assert arguments.check_out is None
    assert arguments.check_in_offset_days == 1
    assert arguments.stay_nights == 1
    assert arguments.lookahead_days == 1
    assert (arguments.adults, arguments.children, arguments.rooms) == (2, 0, 1)
    assert arguments.current_price_dir == Path("data/current/hotel_price")
    assert arguments.current_availability_dir == Path("data/current/hotel_availability")

    check_in, check_out = cli_module._trivago_availability_dates(
        cli_module.build_parser().parse_args(
            [
                "batch-trivago-availability",
                "--check-in",
                "2026-08-21",
                "--stay-nights",
                "3",
            ]
        )
    )
    assert (check_in, check_out) == (
        date(2026, 8, 21),
        date(2026, 8, 24),
    )


def test_availability_cli_wires_exact_stay_and_lookahead(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    class FakeBatchRunner:
        def __init__(self, *args, **kwargs) -> None:
            captured["constructor_args"] = args
            captured["constructor_kwargs"] = kwargs

        def run(self, registry, context, *, run_id, lookahead_days):
            captured["registry"] = registry
            captured["context"] = context
            captured["run_id"] = run_id
            captured["lookahead_days"] = lookahead_days
            return (
                SimpleNamespace(
                    selected_count=1,
                    completed_count=1,
                    failed_count=0,
                    availability_counts={"available": 1},
                    items=[],
                ),
                Path("availability-summary.json"),
            )

    registry = object()
    monkeypatch.setattr(cli_module, "_load_trivago_registry", lambda path: registry)
    monkeypatch.setattr(
        cli_module, "TrivagoStayAvailabilityBatchRunner", FakeBatchRunner
    )
    monkeypatch.setattr(cli_module, "TrivagoMcpDiscoveryAdapter", lambda: object())

    exit_code = cli_module.main(
        [
            "batch-trivago-availability",
            "--check-in",
            "2026-08-21",
            "--check-out",
            "2026-08-24",
            "--lookahead-days",
            "2",
            "--children",
            "1",
            "--children-ages",
            "7",
        ]
    )

    assert exit_code == 0
    assert captured["registry"] is registry
    context = captured["context"]
    assert isinstance(context, TrivagoPriceBatchContext)
    assert (context.check_in, context.check_out) == (
        date(2026, 8, 21),
        date(2026, 8, 24),
    )
    assert context.children_ages == [7]
    assert captured["lookahead_days"] == 2
    assert "completed=1" in capsys.readouterr().out
