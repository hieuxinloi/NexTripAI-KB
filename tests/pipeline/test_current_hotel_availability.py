from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime, timedelta, timezone
from threading import Event

import pytest
from pydantic import ValidationError

from nextrip_pipeline.publishing import (
    CurrentHotelAvailabilitySnapshot,
    CurrentHotelAvailabilityWriter,
    OlderAvailabilityObservationError,
)
from nextrip_pipeline.schemas import (
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    Occupancy,
    VerificationStatus,
)


NOW = datetime(2026, 8, 20, 4, tzinfo=timezone.utc)


def _observation(
    observation_id: str,
    *,
    status: HotelAvailabilityStatus = HotelAvailabilityStatus.UNAVAILABLE,
    reason: HotelAvailabilityReason = (
        HotelAvailabilityReason.NO_BOOKABLE_OFFER_RETURNED
    ),
    observed_at: datetime = NOW,
    requested_check_in: date = date(2026, 8, 21),
    fallback_offset_days: int = 0,
    nights: int = 1,
    occupancy: Occupancy | None = None,
    children_ages: list[int] | None = None,
    offer_count: int = 0,
    price_observation_ids: list[str] | None = None,
    mapping_id: str | None = "mapping-hotel-1",
    external_id: str | None = "trivago-hotel-1",
    verification_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED,
) -> HotelAvailabilityObservation:
    check_in = requested_check_in + timedelta(days=fallback_offset_days)
    return HotelAvailabilityObservation(
        observation_id=observation_id,
        run_id=f"run-{observation_id}",
        hotel_id="hotel_qn_001",
        source_record_id=f"source-record-{observation_id}",
        source_id="trivago-mcp",
        mapping_id=mapping_id,
        external_id=external_id,
        requested_check_in=requested_check_in,
        fallback_offset_days=fallback_offset_days,
        check_in=check_in,
        check_out=check_in + timedelta(days=nights),
        nights=nights,
        occupancy=occupancy or Occupancy(),
        children_ages=children_ages or [],
        currency="vnd",
        status=status,
        reason=reason,
        offer_count=offer_count,
        price_observation_ids=price_observation_ids or [],
        observed_at=observed_at,
        verification_status=verification_status,
    )


def test_unavailable_and_later_available_dates_preserve_search_annotations() -> None:
    unavailable = _observation("unavailable")
    available = _observation(
        "available-next-day",
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        fallback_offset_days=1,
        offer_count=1,
        price_observation_ids=["price-next-day"],
    )

    assert unavailable.check_in == date(2026, 8, 21)
    assert unavailable.status is HotelAvailabilityStatus.UNAVAILABLE
    assert available.requested_check_in == unavailable.requested_check_in
    assert available.check_in == date(2026, 8, 22)
    assert available.fallback_offset_days == 1
    assert available.price_observation_ids == ["price-next-day"]
    assert available.currency == "VND"


def test_multi_night_context_and_children_ages_are_exact() -> None:
    observation = _observation(
        "three-night-stay",
        nights=3,
        occupancy=Occupancy(adults=2, children=2, rooms=1),
        children_ages=[12, 5],
    )

    assert observation.check_out == date(2026, 8, 24)
    assert observation.nights == 3
    assert observation.children_ages == [5, 12]

    payload = observation.model_dump()
    payload["nights"] = 2
    with pytest.raises(ValidationError, match="nights must equal"):
        HotelAvailabilityObservation.model_validate(payload)

    payload = observation.model_dump()
    payload["children_ages"] = []
    with pytest.raises(ValidationError, match="children_ages must match"):
        HotelAvailabilityObservation.model_validate(payload)


def test_outcomes_cannot_overstate_provider_evidence() -> None:
    with pytest.raises(ValidationError, match="reason is incompatible"):
        _observation(
            "technical-failure",
            status=HotelAvailabilityStatus.UNAVAILABLE,
            reason=HotelAvailabilityReason.CRAWL_ERROR,
        )

    with pytest.raises(ValidationError, match="confirmed mapping"):
        _observation(
            "unresolved-as-unavailable",
            mapping_id=None,
            external_id=None,
        )

    unknown = _observation(
        "unresolved",
        status=HotelAvailabilityStatus.UNKNOWN,
        reason=HotelAvailabilityReason.MAPPING_UNRESOLVED,
        mapping_id=None,
        external_id=None,
    )
    assert unknown.status is HotelAvailabilityStatus.UNKNOWN

    with pytest.raises(ValidationError, match="priced offer"):
        _observation(
            "available-without-price",
            status=HotelAvailabilityStatus.AVAILABLE,
            reason=HotelAvailabilityReason.OFFER_FOUND,
        )


def test_writer_partitions_exact_context_and_has_five_hour_ttl(tmp_path) -> None:
    writer = CurrentHotelAvailabilityWriter(tmp_path, clock=lambda: NOW)
    observation = _observation(
        "availability-1",
        nights=3,
        occupancy=Occupancy(adults=3, children=1, rooms=2),
        children_ages=[7],
    )

    path = writer.publish(observation)
    snapshot = CurrentHotelAvailabilitySnapshot.model_validate_json(
        path.read_text(encoding="utf-8")
    )

    assert path.relative_to(tmp_path).parts == (
        "hotel=hotel_qn_001",
        "checkin=2026-08-21",
        "checkout=2026-08-24",
        "nights=3",
        "occupancy=3a-1c-2r",
        "children_ages=7",
        "currency=VND",
        "source=trivago-mcp.json",
    )
    assert snapshot.stale_after == NOW + timedelta(hours=5)
    assert not snapshot.is_stale(NOW + timedelta(hours=5))
    assert snapshot.is_stale(NOW + timedelta(hours=5, seconds=1))
    assert not list(path.parent.glob(".*.tmp"))


def test_latest_search_result_replaces_previous_status_for_same_context(
    tmp_path,
) -> None:
    writer = CurrentHotelAvailabilityWriter(tmp_path, clock=lambda: NOW)
    available = _observation(
        "available-old",
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        offer_count=1,
        price_observation_ids=["price-old"],
    )
    path = writer.publish(available)
    unavailable = _observation(
        "unavailable-new",
        observed_at=NOW + timedelta(hours=1),
    )

    assert writer.publish(unavailable) == path
    current = writer.get_for(unavailable)
    assert current is not None
    assert current.observation_id == "unavailable-new"
    assert current.observation.status is HotelAvailabilityStatus.UNAVAILABLE
    assert len(writer.list_snapshots()) == 1

    older = _observation(
        "unknown-older",
        status=HotelAvailabilityStatus.UNKNOWN,
        reason=HotelAvailabilityReason.CRAWL_ERROR,
        observed_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(OlderAvailabilityObservationError):
        writer.publish(older)


def test_racing_availability_publishers_preserve_newest_evidence(tmp_path) -> None:
    replace_entered = Event()
    release_replace = Event()
    older_started = Event()

    class BlockingWriter(CurrentHotelAvailabilityWriter):
        def _replace(self, destination, content) -> None:
            replace_entered.set()
            if not release_replace.wait(timeout=2):
                raise TimeoutError("test did not release the first publisher")
            super()._replace(destination, content)

    class SignalingWriter(CurrentHotelAvailabilityWriter):
        def path_for(self, observation):
            destination = super().path_for(observation)
            older_started.set()
            return destination

    newer = _observation(
        "availability-race-newer",
        observed_at=NOW + timedelta(hours=1),
    )
    older = _observation("availability-race-older")
    blocking_writer = BlockingWriter(tmp_path, clock=lambda: NOW)
    signaling_writer = SignalingWriter(tmp_path, clock=lambda: NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        newer_future = executor.submit(blocking_writer.publish, newer)
        assert replace_entered.wait(timeout=2)
        older_future = executor.submit(signaling_writer.publish, older)
        assert older_started.wait(timeout=2)
        try:
            with pytest.raises(FutureTimeoutError):
                older_future.result(timeout=0.15)
        finally:
            release_replace.set()

        path = newer_future.result(timeout=2)
        with pytest.raises(OlderAvailabilityObservationError):
            older_future.result(timeout=2)

    current = blocking_writer.get_for(newer)
    assert current is not None
    assert current.observation_id == newer.observation_id
    assert path.with_name(f".{path.name}.lock").exists()


def test_writer_filters_staleness_status_and_exact_guest_context(tmp_path) -> None:
    writer = CurrentHotelAvailabilityWriter(tmp_path, clock=lambda: NOW)
    default_context = _observation("default-context")
    child_context = _observation(
        "child-context",
        occupancy=Occupancy(adults=2, children=1, rooms=1),
        children_ages=[8],
    )
    next_day = _observation("next-day", fallback_offset_days=1)
    for item in (default_context, child_context, next_day):
        writer.publish(item)

    assert len(writer.list_snapshots(hotel_id="hotel_qn_001")) == 3
    assert [
        item.observation_id
        for item in writer.list_snapshots(
            occupancy=Occupancy(adults=2, children=1, rooms=1),
            children_ages=[8],
        )
    ] == ["child-context"]
    assert len(writer.list_snapshots(status=HotelAvailabilityStatus.UNAVAILABLE)) == 3
    assert (
        writer.get_latest(
            "hotel_qn_001",
            check_in=date(2026, 8, 21),
            include_stale=False,
            at=NOW + timedelta(hours=6),
        )
        is None
    )


def test_writer_rejects_pending_evidence_and_invalid_ttl(tmp_path) -> None:
    writer = CurrentHotelAvailabilityWriter(tmp_path)
    pending = _observation(
        "pending",
        verification_status=VerificationStatus.PENDING_REVIEW,
    )

    with pytest.raises(ValueError, match="only verified"):
        writer.publish(pending)
    with pytest.raises(ValueError, match="TTL"):
        CurrentHotelAvailabilityWriter(tmp_path, ttl=timedelta(0))
