from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nextrip_current.models import (
    CurrentHotelOffer,
    CurrentLookupStatus,
    HotelAvailabilityResult,
    HotelAvailabilitySearchRequest,
    HotelAvailabilitySearchResponse,
    HotelIdentity,
    HotelOfferProvenance,
    HotelOfferSearchRequest,
    HotelStayWindowResult,
)
from nextrip_pipeline.schemas import (
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    Occupancy,
    OfferAvailability,
    VerificationStatus,
)


CHECK_IN = date(2026, 8, 24)
NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def _request(**updates: object) -> HotelOfferSearchRequest:
    values: dict[str, object] = {
        "hotel_ids": ["hotel_qn_025"],
        "check_in": CHECK_IN,
    }
    values.update(updates)
    return HotelOfferSearchRequest.model_validate(values)


def test_existing_check_out_contract_is_preserved_and_duration_is_derived():
    request = _request(check_out=date(2026, 8, 27))

    assert request.check_out == date(2026, 8, 27)
    assert request.stay_nights == 3
    assert request.stay_days == 4
    assert request.lookahead_days == 1


def test_missing_checkout_and_duration_defaults_to_one_night():
    request = _request()

    assert request.check_out == date(2026, 8, 25)
    assert request.stay_nights == 1
    assert request.stay_days == 2


def test_availability_request_refreshes_missing_context_by_default() -> None:
    request = HotelAvailabilitySearchRequest(
        hotel_ids=["hotel_qn_025"],
        check_in=CHECK_IN,
    )

    assert request.refresh_if_missing is True
    assert request.lookahead_days == 1


@pytest.mark.parametrize(
    ("duration", "expected_check_out", "expected_nights", "expected_days"),
    [
        ({"stay_nights": 2}, date(2026, 8, 26), 2, 3),
        ({"stay_days": 3}, date(2026, 8, 26), 2, 3),
        (
            {
                "check_out": date(2026, 8, 26),
                "stay_nights": 2,
                "stay_days": 3,
            },
            date(2026, 8, 26),
            2,
            3,
        ),
    ],
)
def test_duration_inputs_resolve_to_one_canonical_stay(
    duration: dict[str, object],
    expected_check_out: date,
    expected_nights: int,
    expected_days: int,
):
    request = _request(**duration)

    assert request.check_out == expected_check_out
    assert request.stay_nights == expected_nights
    assert request.stay_days == expected_days


@pytest.mark.parametrize(
    "duration",
    [
        {"check_out": date(2026, 8, 26), "stay_nights": 3},
        {"check_out": date(2026, 8, 26), "stay_days": 4},
        {"stay_nights": 2, "stay_days": 4},
        {"check_out": date(2026, 8, 24)},
        {"check_out": date(2026, 8, 23)},
        {"stay_nights": 0},
        {"stay_days": 1},
        {"stay_nights": 31},
        {"stay_days": 32},
        {"check_out": date(2026, 9, 24)},
    ],
)
def test_invalid_or_conflicting_duration_is_rejected(duration: dict[str, object]):
    with pytest.raises(ValidationError):
        _request(**duration)


def test_lookahead_is_explicitly_bounded():
    assert _request(lookahead_days=0).lookahead_days == 0
    assert _request(lookahead_days=14).lookahead_days == 14

    with pytest.raises(ValidationError):
        _request(lookahead_days=15)


def test_checkout_is_optional_in_wire_schema_but_present_after_validation():
    schema = HotelOfferSearchRequest.model_json_schema()

    assert "check_out" not in schema["required"]
    assert _request().model_dump(mode="json")["check_out"] == "2026-08-25"


def _offer(
    *,
    check_in: date,
    check_out: date,
    hotel_id: str = "hotel_qn_025",
) -> CurrentHotelOffer:
    return CurrentHotelOffer(
        hotel_id=hotel_id,
        offer_key=f"offer-{check_in.isoformat()}",
        seller="Booking.com",
        room_type="Deluxe",
        check_in=check_in,
        check_out=check_out,
        occupancy=Occupancy(),
        currency="VND",
        amount=Decimal("1499700"),
        total_amount=Decimal("1499700"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=NOW,
        stale_after=NOW + timedelta(hours=5),
        stale=False,
        provenance=HotelOfferProvenance(
            run_id="run-1",
            source_record_id="source-1",
            observation_id="price-1",
            decision_id="decision-1",
            verification_status=VerificationStatus.AUTO_VERIFIED,
        ),
    )


def _window(
    offset: int,
    *,
    status: HotelAvailabilityStatus,
    reason: HotelAvailabilityReason | None,
    lookup_status: CurrentLookupStatus = CurrentLookupStatus.AVAILABLE,
    stay_nights: int = 2,
) -> HotelStayWindowResult:
    check_in = CHECK_IN + timedelta(days=offset)
    check_out = check_in + timedelta(days=stay_nights)
    offers = (
        [_offer(check_in=check_in, check_out=check_out)]
        if status is HotelAvailabilityStatus.AVAILABLE
        else []
    )
    return HotelStayWindowResult(
        requested_check_in=CHECK_IN,
        fallback_offset_days=offset,
        check_in=check_in,
        check_out=check_out,
        stay_nights=stay_nights,
        lookup_status=lookup_status,
        availability=status,
        reason=reason,
        offers=offers,
        availability_observation_id=f"availability-{offset}",
    )


def test_response_lists_requested_unavailable_and_next_available_full_stay():
    requested = _window(
        0,
        status=HotelAvailabilityStatus.UNAVAILABLE,
        reason=HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
    )
    next_day = _window(
        1,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
    )
    result = HotelAvailabilityResult(
        hotel_id="hotel_qn_025",
        identity=HotelIdentity(hotel_id="hotel_qn_025", display_name="Fleur de Lys"),
        windows=[requested, next_day],
        selected_window_index=1,
    )
    response = HotelAvailabilitySearchResponse(
        check_in=CHECK_IN,
        check_out=CHECK_IN + timedelta(days=2),
        stay_nights=2,
        stay_days=3,
        lookahead_days=1,
        occupancy=Occupancy(),
        children_ages=[],
        currency="VND",
        include_stale=False,
        evaluated_at=NOW,
        results=[result],
    )

    assert [window.availability for window in response.results[0].windows] == [
        HotelAvailabilityStatus.UNAVAILABLE,
        HotelAvailabilityStatus.AVAILABLE,
    ]
    assert response.results[0].selected_window_index == 1
    assert response.results[0].windows[1].check_out == date(2026, 8, 27)


def test_missing_lookup_cannot_be_reported_as_confirmed_unavailable():
    with pytest.raises(ValidationError, match="missing lookup must keep"):
        _window(
            0,
            status=HotelAvailabilityStatus.UNAVAILABLE,
            reason=HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
            lookup_status=CurrentLookupStatus.MISSING,
        )

    missing = _window(
        0,
        status=HotelAvailabilityStatus.UNKNOWN,
        reason=HotelAvailabilityReason.MAPPING_UNRESOLVED,
        lookup_status=CurrentLookupStatus.MISSING,
    )
    assert missing.availability is HotelAvailabilityStatus.UNKNOWN


def test_technical_failure_reason_cannot_claim_hotel_is_unavailable():
    for reason in (
        HotelAvailabilityReason.NO_PRICE,
        HotelAvailabilityReason.CRAWL_ERROR,
    ):
        with pytest.raises(ValidationError, match="reason is incompatible"):
            _window(
                0,
                status=HotelAvailabilityStatus.UNAVAILABLE,
                reason=reason,
            )

    no_price = _window(
        0,
        status=HotelAvailabilityStatus.UNKNOWN,
        reason=HotelAvailabilityReason.NO_PRICE,
    )
    assert no_price.availability is HotelAvailabilityStatus.UNKNOWN


def test_result_must_select_earliest_fresh_available_window():
    windows = [
        _window(
            0,
            status=HotelAvailabilityStatus.UNAVAILABLE,
            reason=HotelAvailabilityReason.SOLD_OUT,
        ),
        _window(
            1,
            status=HotelAvailabilityStatus.AVAILABLE,
            reason=HotelAvailabilityReason.OFFER_FOUND,
        ),
    ]

    with pytest.raises(ValidationError, match="earliest fresh available"):
        HotelAvailabilityResult(
            hotel_id="hotel_qn_025",
            identity=HotelIdentity(hotel_id="hotel_qn_025"),
            windows=windows,
            selected_window_index=None,
        )


def test_response_rejects_more_windows_than_requested_lookahead():
    result = HotelAvailabilityResult(
        hotel_id="hotel_qn_025",
        identity=HotelIdentity(hotel_id="hotel_qn_025"),
        windows=[
            _window(
                0,
                status=HotelAvailabilityStatus.UNAVAILABLE,
                reason=HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED,
            ),
            _window(
                1,
                status=HotelAvailabilityStatus.UNKNOWN,
                reason=HotelAvailabilityReason.CRAWL_ERROR,
            ),
        ],
    )

    with pytest.raises(ValidationError, match="exceed"):
        HotelAvailabilitySearchResponse(
            check_in=CHECK_IN,
            check_out=CHECK_IN + timedelta(days=2),
            stay_nights=2,
            stay_days=3,
            lookahead_days=0,
            occupancy=Occupancy(),
            children_ages=[],
            include_stale=False,
            evaluated_at=NOW,
            results=[result],
        )
