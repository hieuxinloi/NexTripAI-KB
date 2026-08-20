from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from nextrip_pipeline.crawl import RawJsonWriter, compute_content_hash
from nextrip_pipeline.crawl.adapters import TrivagoSearchStrategy
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.decision_gate import HotelPriceDecisionWriter
from nextrip_pipeline.jobs.trivago_mcp_batch import TrivagoPriceBatchContext
from nextrip_pipeline.jobs.trivago_stay_availability import (
    TrivagoStayAvailabilityRunner,
    TrivagoStayAvailabilityResultWriter,
    TrivagoStayStopReason,
)
from nextrip_pipeline.preprocessing import (
    NormalizedHotelPriceWriter,
    TrivagoMcpPriceNormalizer,
)
from nextrip_pipeline.publishing import (
    CurrentHotelAvailabilityWriter,
    CurrentHotelPriceWriter,
)
from nextrip_pipeline.quality import (
    CurrentTrivagoMappingWriter,
    TrivagoDiscoveryAuditWriter,
    TrivagoDiscoveryResolver,
)
from nextrip_pipeline.schemas import (
    EntityType,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    Occupancy,
    RecordSubjectType,
    SourceRecord,
)
from nextrip_pipeline.validators import ValidationResultWriter


NOW = datetime(2026, 8, 20, 5, tzinfo=timezone.utc)


def _entry(*, confirmed: bool = True) -> TrivagoHotelRegistryEntry:
    values = {
        "entity_id": "hotel_qn_001",
        "master_name": "Fleur De Lys Hotel Quy Nhon",
        "city": "Quy Nhơn",
        "address": "16 Nguyễn Huệ, Quy Nhơn",
        "search_query": "Fleur De Lys Hotel Quy Nhon, Quy Nhơn, Việt Nam",
    }
    if confirmed:
        values.update(
            {
                "status": TrivagoRegistryStatus.CONFIRMED,
                "external_id": "fleur-1",
                "verified_at": NOW,
            }
        )
    return TrivagoHotelRegistryEntry.model_validate(values)


def _priced_accommodation() -> dict[str, object]:
    return {
        "accommodation_id": "fleur-1",
        "accommodation_name": "Fleur De Lys Hotel Quy Nhon",
        "country_city": "Quy Nhơn, Việt Nam",
        "currency": "VND",
        "price_per_night": "1.200.000 đ",
        "price_per_stay": "3.600.000 đ",
        "advertisers": "Booking.com",
    }


class SequenceAdapter:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[object, object, str, TrivagoSearchStrategy]] = []

    def search(self, target, request, *, run_id, strategy=TrivagoSearchStrategy.NAME):
        call_index = len(self.calls)
        self.calls.append((target, request, run_id, strategy))
        outcome = self.outcomes[call_index]
        if isinstance(outcome, Exception):
            raise outcome
        payload = {
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
                    "children_ages": "-".join(
                        str(age) for age in request.children_ages
                    ),
                },
            },
            "response": {"result": {"structuredContent": outcome}},
        }
        return SourceRecord(
            source_record_id=f"source-{call_index}",
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


def _runner(
    tmp_path,
    adapter: SequenceAdapter,
    *,
    normalizer: TrivagoMcpPriceNormalizer | None = None,
) -> TrivagoStayAvailabilityRunner:
    return TrivagoStayAvailabilityRunner(
        adapter,
        RawJsonWriter(tmp_path / "raw"),
        NormalizedHotelPriceWriter(tmp_path / "normalized"),
        TrivagoDiscoveryAuditWriter(tmp_path / "quality"),
        CurrentTrivagoMappingWriter(tmp_path / "mapping"),
        CurrentHotelAvailabilityWriter(
            tmp_path / "current" / "hotel_availability",
            clock=lambda: NOW,
        ),
        normalizer=normalizer,
        resolver=TrivagoDiscoveryResolver(clock=lambda: NOW),
        clock=lambda: NOW,
    )


def _context(*, nights: int = 3) -> TrivagoPriceBatchContext:
    return TrivagoPriceBatchContext(
        check_in=date(2026, 8, 21),
        check_out=date(2026, 8, 21 + nights),
        occupancy=Occupancy(adults=3, children=1, rooms=2),
        children_ages=[7],
    )


def test_full_multi_night_window_shifts_after_confirmed_unavailable(tmp_path) -> None:
    adapter = SequenceAdapter(
        [
            {"error": "No accommodations found"},
            {"accommodations": [_priced_accommodation()]},
        ]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(nights=3),
        run_id="stay-search",
        lookahead_days=1,
    )

    assert result.stop_reason is TrivagoStayStopReason.AVAILABLE_FOUND
    assert result.selected_available_offset_days == 1
    assert [item.availability.status for item in result.attempts] == [
        HotelAvailabilityStatus.UNAVAILABLE,
        HotelAvailabilityStatus.AVAILABLE,
    ]
    first, second = result.attempts
    assert first.availability.reason is (
        HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED
    )
    assert first.availability.requested_check_in == date(2026, 8, 21)
    assert second.availability.requested_check_in == date(2026, 8, 21)
    assert second.availability.check_in == date(2026, 8, 22)
    assert second.availability.check_out == date(2026, 8, 25)
    assert second.availability.nights == 3
    assert second.availability.occupancy == Occupancy(adults=3, children=1, rooms=2)
    assert second.availability.children_ages == [7]
    assert second.prices[0].check_in == date(2026, 8, 22)
    assert second.prices[0].check_out == date(2026, 8, 25)
    assert [(call[1].check_in, call[1].check_out) for call in adapter.calls] == [
        (date(2026, 8, 21), date(2026, 8, 24)),
        (date(2026, 8, 22), date(2026, 8, 25)),
    ]
    assert all(call[3] is TrivagoSearchStrategy.NAME for call in adapter.calls)
    assert len(list((tmp_path / "current" / "hotel_availability").rglob("*.json"))) == 2

    summary_path = TrivagoStayAvailabilityResultWriter(tmp_path / "runs").write(result)
    assert summary_path.name == "run=stay-search.json"
    assert '"selected_available_offset_days": 1' in summary_path.read_text(
        encoding="utf-8"
    )
    with pytest.raises(FileExistsError, match="already exists"):
        TrivagoStayAvailabilityResultWriter(tmp_path / "runs").write(result)


def test_exact_hotel_without_price_is_unknown_and_does_not_fallback(tmp_path) -> None:
    accommodation = _priced_accommodation()
    accommodation["price_per_stay"] = ""
    adapter = SequenceAdapter(
        [
            {"accommodations": [accommodation]},
            {"accommodations": [_priced_accommodation()]},
        ]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(),
        run_id="missing-price",
        lookahead_days=1,
    )

    assert len(adapter.calls) == 1
    assert result.stop_reason is TrivagoStayStopReason.UNKNOWN_RESULT
    assert result.attempts[0].availability.status is HotelAvailabilityStatus.UNKNOWN
    assert result.attempts[0].availability.reason is HotelAvailabilityReason.NO_PRICE
    assert not list((tmp_path / "normalized").rglob("*.json"))


def test_incomplete_duplicate_does_not_discard_complete_exact_offer(tmp_path) -> None:
    incomplete = _priced_accommodation()
    incomplete["price_per_stay"] = ""
    adapter = SequenceAdapter(
        [{"accommodations": [incomplete, _priced_accommodation()]}]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(),
        run_id="duplicate-offers",
        lookahead_days=0,
    )

    attempt = result.attempts[0]
    assert attempt.availability.status is HotelAvailabilityStatus.AVAILABLE
    assert len(attempt.prices) == 1


def test_explicit_sold_out_allows_next_full_stay_window(tmp_path) -> None:
    sold_out = _priced_accommodation()
    sold_out.update(
        {
            "availability": "sold_out",
            "price_per_night": "",
            "price_per_stay": "",
        }
    )
    adapter = SequenceAdapter(
        [
            {"accommodations": [sold_out]},
            {"accommodations": [_priced_accommodation()]},
        ]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(),
        run_id="sold-out",
        lookahead_days=1,
    )

    assert result.attempts[0].availability.status is (
        HotelAvailabilityStatus.UNAVAILABLE
    )
    assert result.attempts[0].availability.reason is (HotelAvailabilityReason.SOLD_OUT)
    assert result.attempts[1].availability.status is HotelAvailabilityStatus.AVAILABLE


def test_no_offers_without_confirmed_identity_is_unknown_and_stops(tmp_path) -> None:
    adapter = SequenceAdapter(
        [
            {"accommodations": []},
            {"accommodations": [_priced_accommodation()]},
        ]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(confirmed=False),
        _context(),
        run_id="unresolved",
        lookahead_days=1,
    )

    assert len(adapter.calls) == 1
    observation = result.attempts[0].availability
    assert observation.status is HotelAvailabilityStatus.UNKNOWN
    assert observation.reason is HotelAvailabilityReason.MAPPING_UNRESOLVED
    assert observation.mapping_id is None
    assert observation.external_id is None


def test_generic_empty_search_is_unknown_even_with_confirmed_mapping(tmp_path) -> None:
    adapter = SequenceAdapter(
        [
            {"accommodations": []},
            {"accommodations": [_priced_accommodation()]},
        ]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(),
        run_id="ambiguous-empty-search",
        lookahead_days=1,
    )

    assert len(adapter.calls) == 1
    observation = result.attempts[0].availability
    assert observation.status is HotelAvailabilityStatus.UNKNOWN
    assert observation.reason is HotelAvailabilityReason.PROVIDER_NOT_LISTED


def test_capture_error_is_persisted_as_unknown_and_never_triggers_fallback(
    tmp_path,
) -> None:
    adapter = SequenceAdapter(
        [RuntimeError("MCP timeout"), {"accommodations": [_priced_accommodation()]}]
    )

    result = _runner(tmp_path, adapter).run(
        _entry(),
        _context(),
        run_id="capture-error",
        lookahead_days=1,
    )

    assert len(adapter.calls) == 1
    attempt = result.attempts[0]
    assert attempt.error_stage == "capture"
    assert attempt.availability.status is HotelAvailabilityStatus.UNKNOWN
    assert attempt.availability.reason is HotelAvailabilityReason.CRAWL_ERROR
    assert len(list((tmp_path / "current" / "hotel_availability").rglob("*.json"))) == 1


def test_available_stay_can_run_existing_quality_and_current_price_gate(
    tmp_path,
) -> None:
    adapter = SequenceAdapter([{"accommodations": [_priced_accommodation()]}])
    runner = TrivagoStayAvailabilityRunner(
        adapter,
        RawJsonWriter(tmp_path / "raw"),
        NormalizedHotelPriceWriter(tmp_path / "normalized"),
        TrivagoDiscoveryAuditWriter(tmp_path / "quality"),
        CurrentTrivagoMappingWriter(tmp_path / "mapping"),
        CurrentHotelAvailabilityWriter(
            tmp_path / "current" / "hotel_availability",
            clock=lambda: NOW,
        ),
        validation_writer=ValidationResultWriter(tmp_path / "validation"),
        decision_writer=HotelPriceDecisionWriter(tmp_path / "decision"),
        current_price_writer=CurrentHotelPriceWriter(
            tmp_path / "current" / "hotel_price",
            clock=lambda: NOW,
        ),
        resolver=TrivagoDiscoveryResolver(clock=lambda: NOW),
        clock=lambda: NOW,
    )

    result = runner.run(
        _entry(),
        _context(),
        run_id="quality-gated",
        lookahead_days=0,
    )

    assert result.attempts[0].availability.status is HotelAvailabilityStatus.AVAILABLE
    assert len(list((tmp_path / "validation").rglob("*.json"))) == 4
    assert len(list((tmp_path / "decision").rglob("*.json"))) == 1
    assert len(list((tmp_path / "current" / "hotel_price").rglob("*.json"))) == 1


def test_runner_rejects_price_captured_under_a_different_mapping(tmp_path) -> None:
    class MismatchedNormalizer(TrivagoMcpPriceNormalizer):
        def normalize(self, record, mapping):
            observations = super().normalize(record, mapping)
            return [
                observation.model_copy(
                    update={
                        "mapping_id": "different-mapping",
                        "external_id": "different-external",
                    }
                )
                for observation in observations
            ]

    runner = _runner(
        tmp_path,
        SequenceAdapter([{"accommodations": [_priced_accommodation()]}]),
        normalizer=MismatchedNormalizer(),
    )

    with pytest.raises(ValueError, match="confirmed mapping identity"):
        runner.run(
            _entry(),
            _context(),
            run_id="mismatched-price-mapping",
            lookahead_days=0,
        )

    assert not list((tmp_path / "normalized").rglob("*.json"))
    assert not list((tmp_path / "current" / "hotel_availability").rglob("*.json"))


@pytest.mark.parametrize("lookahead_days", [-1, 15])
def test_lookahead_is_bounded(tmp_path, lookahead_days: int) -> None:
    runner = _runner(tmp_path, SequenceAdapter([]))

    with pytest.raises(ValueError, match="lookahead_days"):
        runner.run(
            _entry(),
            _context(),
            run_id="invalid-lookahead",
            lookahead_days=lookahead_days,
        )
