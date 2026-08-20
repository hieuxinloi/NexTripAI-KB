from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest

from nextrip_pipeline.decision_gate import (
    HotelPriceDecision,
    HotelPriceDecisionStatus,
)
from nextrip_pipeline.publishing import (
    CurrentHotelPriceSnapshot,
    CurrentHotelPriceWriter,
    OlderPriceObservationError,
)
from nextrip_pipeline.schemas import (
    HotelPriceObservation,
    Occupancy,
    OfferAvailability,
)


NOW = datetime(2026, 8, 19, 10, tzinfo=timezone.utc)


def _observation(
    observation_id: str,
    *,
    observed_at: datetime = NOW,
    check_in: date = date(2026, 8, 21),
    check_out: date = date(2026, 8, 22),
    occupancy: Occupancy | None = None,
    children_ages: list[int] | None = None,
    seller: str = "Booking.com",
    offer_key: str = "hotel-123|standard-room",
    amount: str = "1000000",
    currency: str = "VND",
) -> HotelPriceObservation:
    return HotelPriceObservation(
        observation_id=observation_id,
        run_id=f"run-{observation_id}",
        hotel_id="hotel_dn_001",
        offer_key=offer_key,
        source_record_id=f"source-{observation_id}",
        source_id="trivago-mcp",
        seller=seller,
        room_type="standard",
        check_in=check_in,
        check_out=check_out,
        occupancy=occupancy or Occupancy(),
        children_ages=children_ages or [],
        currency=currency,
        nightly_amount=Decimal(amount),
        total_amount=Decimal(amount),
        availability=OfferAvailability.AVAILABLE,
        observed_at=observed_at,
    )


def _decision(observation: HotelPriceObservation) -> HotelPriceDecision:
    return HotelPriceDecision(
        decision_id=f"decision-{observation.observation_id}",
        run_id=observation.run_id,
        observation_id=observation.observation_id,
        hotel_id=observation.hotel_id,
        status=HotelPriceDecisionStatus.PASS,
        validation_ids=[f"validation-{observation.observation_id}"],
        decided_at=observation.observed_at,
    )


def test_observation_validates_and_canonicalizes_known_children_ages() -> None:
    occupancy = Occupancy(adults=2, children=2, rooms=1)

    observation = _observation(
        "canonical-ages",
        occupancy=occupancy,
        children_ages=[10, 6],
    )

    assert observation.children_ages == [6, 10]
    with pytest.raises(ValueError, match="must match"):
        _observation("missing-age", occupancy=occupancy, children_ages=[6])
    with pytest.raises(ValueError, match="between 0 and 17"):
        _observation("invalid-age", occupancy=occupancy, children_ages=[6, 18])


def test_path_partitions_exact_stay_occupancy_and_seller_offer(tmp_path) -> None:
    observation = _observation(
        "price-1",
        occupancy=Occupancy(adults=3, children=1, rooms=2),
        children_ages=[7],
    )
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)

    path = writer.publish(observation, _decision(observation))
    snapshot = CurrentHotelPriceSnapshot.model_validate_json(
        path.read_text(encoding="utf-8")
    )

    assert path.relative_to(tmp_path).parts[:4] == (
        "hotel=hotel_dn_001",
        "checkin=2026-08-21",
        "checkout=2026-08-22",
        "occupancy=3a-1c-2r",
    )
    assert path.relative_to(tmp_path).parts[4] == "children_ages=7"
    assert path.relative_to(tmp_path).parts[5] == "currency=VND"
    assert path.name.startswith("offer=booking-com--")
    assert snapshot.stale_after == observation.observed_at + timedelta(hours=5)
    assert not snapshot.is_stale(observation.observed_at + timedelta(hours=5))
    assert snapshot.is_stale(observation.observed_at + timedelta(hours=5, seconds=1))
    assert not list(path.parent.glob(".*.tmp"))


def test_different_price_contexts_and_sellers_coexist(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    first = _observation("price-first")
    other_date = _observation(
        "price-other-date",
        check_in=date(2026, 8, 22),
        check_out=date(2026, 8, 23),
    )
    other_occupancy = _observation(
        "price-other-occupancy",
        occupancy=Occupancy(adults=1, children=0, rooms=1),
    )
    other_seller = _observation("price-agoda", seller="Agoda")

    paths = {
        writer.publish(item, _decision(item))
        for item in (first, other_date, other_occupancy, other_seller)
    }

    assert len(paths) == 4
    assert len(writer.list_snapshots(hotel_id="hotel_dn_001")) == 4
    assert (
        len(
            writer.list_snapshots(
                hotel_id="hotel_dn_001",
                check_in=date(2026, 8, 21),
                occupancy=Occupancy(),
            )
        )
        == 2
    )
    assert writer.get_latest("hotel_dn_001", seller="Agoda") is not None
    assert writer.get_for(other_seller) is not None


def test_different_currencies_have_distinct_current_contexts(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    vnd = _observation("price-vnd", currency="VND")
    usd = _observation("price-usd", currency="USD")

    vnd_path = writer.publish(vnd, _decision(vnd))
    usd_path = writer.publish(usd, _decision(usd))

    assert vnd_path != usd_path
    assert "currency=VND" in vnd_path.parts
    assert "currency=USD" in usd_path.parts
    assert len(writer.list_snapshots(hotel_id=vnd.hotel_id)) == 2
    assert [
        item.observation.currency
        for item in writer.list_snapshots(hotel_id=vnd.hotel_id, currency="USD")
    ] == ["USD"]


def test_exact_children_ages_are_distinct_and_filterable(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    occupancy = Occupancy(adults=2, children=1, rooms=1)
    younger = _observation(
        "price-child-3",
        occupancy=occupancy,
        children_ages=[3],
    )
    older = _observation(
        "price-child-12",
        occupancy=occupancy,
        children_ages=[12],
    )

    younger_path = writer.publish(younger, _decision(younger))
    older_path = writer.publish(older, _decision(older))

    assert younger_path != older_path
    assert "children_ages=3" in younger_path.parts
    assert "children_ages=12" in older_path.parts
    assert [
        item.observation_id
        for item in writer.list_snapshots(
            hotel_id=younger.hotel_id,
            occupancy=occupancy,
            children_ages=[3],
        )
    ] == ["price-child-3"]


def test_legacy_snapshot_without_children_ages_defaults_to_unknown(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    occupancy = Occupancy(adults=2, children=1, rooms=1)
    unknown_ages = _observation("legacy-child-age", occupancy=occupancy)
    contextual_path = writer.publish(unknown_ages, _decision(unknown_ages))
    payload = json.loads(contextual_path.read_text(encoding="utf-8"))
    del payload["observation"]["children_ages"]
    legacy_context_path = writer._legacy_context_path_for(unknown_ages)
    legacy_context_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_context_path.write_text(json.dumps(payload), encoding="utf-8")
    contextual_path.unlink()

    snapshot = writer.get_for(unknown_ages)

    assert snapshot is not None
    assert snapshot.observation.children_ages == []
    explicit_ages = _observation(
        "explicit-child-age",
        occupancy=occupancy,
        children_ages=[7],
    )
    assert writer.get_for(explicit_ages) is None


def test_exact_read_falls_back_to_pre_currency_context_without_migration(
    tmp_path,
) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    observation = _observation("pre-currency")
    currency_path = writer.publish(observation, _decision(observation))
    pre_currency_path = writer._pre_currency_context_path_for(observation)
    pre_currency_path.parent.mkdir(parents=True, exist_ok=True)
    pre_currency_path.write_text(
        currency_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    currency_path.unlink()

    snapshot = writer.get_for(observation)

    assert snapshot is not None
    assert snapshot.observation_id == "pre-currency"
    assert pre_currency_path.exists()
    assert not currency_path.exists()


def test_only_newer_observation_replaces_same_exact_context(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    current = _observation("price-current", amount="1200000")
    path = writer.publish(current, _decision(current))

    newer = _observation(
        "price-newer",
        observed_at=NOW + timedelta(hours=1),
        amount="900000",
    )
    assert writer.publish(newer, _decision(newer)) == path
    assert writer.get_for(newer).observation.nightly_amount == Decimal("900000")

    older = _observation(
        "price-older",
        observed_at=NOW - timedelta(minutes=1),
        amount="800000",
    )
    with pytest.raises(OlderPriceObservationError):
        writer.publish(older, _decision(older))
    assert writer.get_for(newer).observation_id == "price-newer"


def test_racing_price_publishers_cannot_replace_newer_with_older(tmp_path) -> None:
    replace_entered = Event()
    release_replace = Event()
    older_started = Event()

    class BlockingWriter(CurrentHotelPriceWriter):
        def _replace(self, destination, content) -> None:
            replace_entered.set()
            if not release_replace.wait(timeout=2):
                raise TimeoutError("test did not release the first publisher")
            super()._replace(destination, content)

    class SignalingWriter(CurrentHotelPriceWriter):
        def path_for(self, observation):
            destination = super().path_for(observation)
            older_started.set()
            return destination

    newer = _observation(
        "price-race-newer",
        observed_at=NOW + timedelta(hours=1),
        amount="900000",
    )
    older = _observation("price-race-older", amount="800000")
    blocking_writer = BlockingWriter(tmp_path, clock=lambda: NOW)
    signaling_writer = SignalingWriter(tmp_path, clock=lambda: NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        newer_future = executor.submit(
            blocking_writer.publish,
            newer,
            _decision(newer),
        )
        assert replace_entered.wait(timeout=2)
        older_future = executor.submit(
            signaling_writer.publish,
            older,
            _decision(older),
        )
        assert older_started.wait(timeout=2)
        try:
            with pytest.raises(FutureTimeoutError):
                older_future.result(timeout=0.15)
        finally:
            release_replace.set()

        path = newer_future.result(timeout=2)
        with pytest.raises(OlderPriceObservationError):
            older_future.result(timeout=2)

    assert blocking_writer.get_for(newer).observation_id == newer.observation_id
    assert path.with_name(f".{path.name}.lock").exists()


def test_queries_can_exclude_stale_snapshots(tmp_path) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    observation = _observation("price-1")
    writer.publish(observation, _decision(observation))

    assert (
        writer.get_latest(
            observation.hotel_id,
            include_stale=False,
            at=NOW + timedelta(hours=4),
        )
        is not None
    )
    assert (
        writer.get_latest(
            observation.hotel_id,
            include_stale=False,
            at=NOW + timedelta(hours=6),
        )
        is None
    )


def test_exact_read_falls_back_to_legacy_single_file_without_migration(
    tmp_path,
) -> None:
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    observation = _observation("legacy-price")
    contextual_path = writer.publish(observation, _decision(observation))
    legacy_path = writer.legacy_path_for(observation.hotel_id)
    legacy_path.write_text(
        contextual_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    contextual_path.unlink()

    snapshot = writer.get_for(observation)

    assert snapshot is not None
    assert snapshot.observation_id == observation.observation_id
    assert legacy_path.exists()
    assert not contextual_path.exists()

    older = _observation(
        "older-than-legacy",
        observed_at=NOW - timedelta(minutes=1),
    )
    with pytest.raises(OlderPriceObservationError):
        writer.publish(older, _decision(older))

    newer = _observation(
        "newer-than-legacy",
        observed_at=NOW + timedelta(minutes=1),
    )
    new_path = writer.publish(newer, _decision(newer))
    assert new_path != legacy_path
    assert legacy_path.exists()
    assert [item.observation_id for item in writer.list_snapshots()] == [
        "newer-than-legacy"
    ]


def test_invalid_ttl_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="TTL"):
        CurrentHotelPriceWriter(tmp_path, ttl=timedelta(0))
