from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from nextrip_current.api import create_app
from nextrip_current.config import CurrentDataSettings
from nextrip_current.models import (
    CurrentLookupStatus,
    HotelAvailabilitySearchRequest,
    HotelOfferSearchRequest,
)
from nextrip_current.repository import CurrentDataRepository
from nextrip_current.runtime import CurrentDataServiceFactory
from nextrip_current.service import CurrentDataService
from nextrip_pipeline.publishing import CurrentHotelAvailabilityWriter
from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.schemas import (
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    Occupancy,
    OfferAvailability,
    VerificationStatus,
)


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
REQUESTED = date(2026, 8, 24)


def _repository(tmp_path: Path) -> CurrentDataRepository:
    roots = {
        "place_root": tmp_path / "place",
        "hotel_price_root": tmp_path / "hotel_price",
        "hotel_availability_root": tmp_path / "hotel_availability",
        "trivago_mapping_root": tmp_path / "mapping",
    }
    for root in roots.values():
        root.mkdir(parents=True)
    return CurrentDataRepository(**roots)


def _price(
    root: Path,
    *,
    offset: int,
    nights: int = 2,
    observed_at: datetime = NOW,
    mapping_id: str | None = "mapping-1",
    external_id: str | None = "external-1",
) -> CurrentHotelPriceSnapshot:
    check_in = REQUESTED + timedelta(days=offset)
    observation = HotelPriceObservation(
        observation_id=f"price-{offset}",
        run_id=f"run-{offset}",
        hotel_id="hotel_qn_025",
        offer_key=f"offer-{offset}",
        source_record_id=f"raw-{offset}",
        source_id="trivago-mcp",
        mapping_id=mapping_id,
        external_id=external_id,
        seller="Booking.com",
        room_type="Deluxe",
        check_in=check_in,
        check_out=check_in + timedelta(days=nights),
        occupancy=Occupancy(),
        children_ages=[],
        currency="VND",
        amount=Decimal("1500000"),
        nightly_amount=Decimal("750000"),
        total_amount=Decimal("1500000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    snapshot = CurrentHotelPriceSnapshot(
        hotel_id=observation.hotel_id,
        observation_id=observation.observation_id,
        decision_id=f"decision-{offset}",
        observation=observation,
        updated_at=observed_at,
        stale_after=observed_at + timedelta(hours=5),
    )
    (root / f"price-{offset}.json").write_text(
        snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    return snapshot


def _availability(
    root: Path,
    *,
    offset: int,
    status: HotelAvailabilityStatus,
    reason: HotelAvailabilityReason,
    price_ids: list[str] | None = None,
    observed_at: datetime = NOW,
    nights: int = 2,
) -> None:
    check_in = REQUESTED + timedelta(days=offset)
    priced = price_ids or []
    confirmed = status is not HotelAvailabilityStatus.UNKNOWN
    observation = HotelAvailabilityObservation(
        observation_id=f"availability-{offset}-{status.value}",
        run_id=f"run-{offset}",
        hotel_id="hotel_qn_025",
        source_record_id=f"raw-{offset}",
        source_id="trivago-mcp",
        mapping_id="mapping-1" if confirmed else None,
        external_id="external-1" if confirmed else None,
        requested_check_in=REQUESTED,
        fallback_offset_days=offset,
        check_in=check_in,
        check_out=check_in + timedelta(days=nights),
        nights=nights,
        occupancy=Occupancy(),
        children_ages=[],
        currency="VND",
        status=status,
        reason=reason,
        offer_count=len(priced),
        price_observation_ids=priced,
        observed_at=observed_at,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    CurrentHotelAvailabilityWriter(root, clock=lambda: NOW).publish(observation)


def _request(*, refresh: bool = False) -> HotelAvailabilitySearchRequest:
    return HotelAvailabilitySearchRequest(
        hotel_ids=["hotel_qn_025"],
        check_in=REQUESTED,
        stay_nights=2,
        lookahead_days=1,
        currency="VND",
        refresh_if_missing=refresh,
    )


def test_lists_requested_unavailable_then_selects_next_available_window(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    # An old price for the requested stay must not leak after newer explicit
    # unavailability evidence replaces that context.
    _price(repository.hotel_price_root, offset=0, observed_at=NOW - timedelta(hours=1))
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.UNAVAILABLE,
        reason=HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
    )
    next_price = _price(repository.hotel_price_root, offset=1)
    _availability(
        repository.hotel_availability_root,
        offset=1,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        price_ids=[next_price.observation_id],
    )

    response = CurrentDataService(
        repository, clock=lambda: NOW
    ).search_hotel_availability(_request())
    result = response.results[0]

    assert response.stay_nights == 2
    assert response.stay_days == 3
    assert result.selected_window_index == 1
    assert [window.availability for window in result.windows] == [
        HotelAvailabilityStatus.UNAVAILABLE,
        HotelAvailabilityStatus.AVAILABLE,
    ]
    assert result.windows[0].offers == []
    assert result.windows[1].check_in == date(2026, 8, 25)
    assert result.windows[1].check_out == date(2026, 8, 27)
    assert result.windows[1].offers[0].total_amount == Decimal("1500000")


def test_unknown_evidence_never_triggers_a_fallback_date(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.UNKNOWN,
        reason=HotelAvailabilityReason.CRAWL_ERROR,
    )

    result = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(_request())
        .results[0]
    )

    assert len(result.windows) == 1
    assert result.windows[0].lookup_status is CurrentLookupStatus.AVAILABLE
    assert result.windows[0].availability is HotelAvailabilityStatus.UNKNOWN
    assert result.selected_window_index is None


def test_available_projection_without_its_price_evidence_is_incomplete(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        price_ids=["price-file-is-missing"],
    )

    window = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(_request())
        .results[0]
        .windows[0]
    )

    assert window.lookup_status is CurrentLookupStatus.MISSING
    assert window.availability is HotelAvailabilityStatus.UNKNOWN
    assert window.reason is HotelAvailabilityReason.NO_PRICE
    assert window.offers == []


def test_available_projection_rejects_price_from_another_captured_mapping(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    price = _price(
        repository.hotel_price_root,
        offset=0,
        external_id="different-external-id",
    )
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        price_ids=[price.observation_id],
    )

    window = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(_request())
        .results[0]
        .windows[0]
    )

    assert window.lookup_status is CurrentLookupStatus.MISSING
    assert window.availability is HotelAvailabilityStatus.UNKNOWN
    assert window.reason is HotelAvailabilityReason.NO_PRICE
    assert window.offers == []


def test_newer_fresh_price_wins_over_older_unavailable_evidence(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.UNAVAILABLE,
        reason=HotelAvailabilityReason.SOLD_OUT,
        observed_at=NOW - timedelta(hours=1),
    )
    _price(repository.hotel_price_root, offset=0, observed_at=NOW)

    window = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(_request())
        .results[0]
        .windows[0]
    )

    assert window.lookup_status is CurrentLookupStatus.AVAILABLE
    assert window.availability is HotelAvailabilityStatus.AVAILABLE
    assert window.reason is HotelAvailabilityReason.OFFER_FOUND
    assert len(window.offers) == 1


def test_stale_unavailable_evidence_is_hidden_unless_explicitly_requested(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.UNAVAILABLE,
        reason=HotelAvailabilityReason.SOLD_OUT,
        observed_at=NOW - timedelta(hours=6),
    )

    hidden = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(_request())
        .results[0]
        .windows[0]
    )
    visible = (
        CurrentDataService(repository, clock=lambda: NOW)
        .search_hotel_availability(
            _request().model_copy(update={"include_stale": True})
        )
        .results[0]
        .windows[0]
    )

    assert hidden.lookup_status is CurrentLookupStatus.STALE
    assert hidden.availability is HotelAvailabilityStatus.UNKNOWN
    assert hidden.reason is None
    assert visible.lookup_status is CurrentLookupStatus.STALE
    assert visible.availability is HotelAvailabilityStatus.UNAVAILABLE
    assert visible.reason is HotelAvailabilityReason.SOLD_OUT


def test_on_demand_refresh_receives_full_duration_and_populates_both_dates(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    calls: list[tuple[str, int, int]] = []

    class Refresher:
        def refresh(self, hotel_id: str, request: HotelOfferSearchRequest) -> None:
            calls.append((hotel_id, request.stay_nights, request.lookahead_days))
            _availability(
                repository.hotel_availability_root,
                offset=0,
                status=HotelAvailabilityStatus.UNAVAILABLE,
                reason=HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
            )
            next_price = _price(repository.hotel_price_root, offset=1)
            _availability(
                repository.hotel_availability_root,
                offset=1,
                status=HotelAvailabilityStatus.AVAILABLE,
                reason=HotelAvailabilityReason.OFFER_FOUND,
                price_ids=[next_price.observation_id],
            )

    service = CurrentDataService(
        repository,
        hotel_refresher=Refresher(),
        clock=lambda: NOW,
    )
    result = service.search_hotel_availability(_request(refresh=True)).results[0]

    assert calls == [("hotel_qn_025", 2, 1)]
    assert result.selected_window_index == 1
    assert all(window.refresh_attempted for window in result.windows)


def test_http_availability_endpoint_exposes_duration_and_fallback_windows(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _availability(
        repository.hotel_availability_root,
        offset=0,
        status=HotelAvailabilityStatus.UNAVAILABLE,
        reason=HotelAvailabilityReason.SOLD_OUT,
    )
    next_price = _price(repository.hotel_price_root, offset=1)
    _availability(
        repository.hotel_availability_root,
        offset=1,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        price_ids=[next_price.observation_id],
    )
    service = CurrentDataService(repository, clock=lambda: NOW)
    app = create_app(
        settings=CurrentDataSettings(kb_root=tmp_path),
        service_factory=CurrentDataServiceFactory(lambda: service),
        internal_api_key="current-secret",
    )

    with TestClient(app) as client:
        unauthorized = client.post(
            "/api/current/hotel-availability/search",
            json={"hotel_ids": ["hotel_qn_025"], "check_in": "2026-08-24"},
        )
        response = client.post(
            "/api/current/hotel-availability/search",
            headers={"X-NexTrip-Current-Key": "current-secret"},
            json={
                "hotel_ids": ["hotel_qn_025"],
                "check_in": "2026-08-24",
                "stay_days": 3,
                "lookahead_days": 1,
                "currency": "VND",
                "refresh_if_missing": False,
            },
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["stay_nights"] == 2
    assert payload["stay_days"] == 3
    assert payload["results"][0]["selected_window_index"] == 1
    assert [window["availability"] for window in payload["results"][0]["windows"]] == [
        "unavailable",
        "available",
    ]
