from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from nextrip_pipeline.publishing.accepted_observations import (
    AcceptedObservationArtifact,
    AcceptedObservationConflictError,
    AcceptedObservationStore,
    AcceptedObservationType,
    UnverifiedObservationError,
    accepted_observation_hash,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    GoogleMapsPlaceObservation,
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    OfferAvailability,
    OpeningStatusObservation,
    VerificationStatus,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 4, 30, tzinfo=UTC)


def _price(
    *,
    observed_at: datetime = NOW,
    verification_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED,
) -> HotelPriceObservation:
    return HotelPriceObservation(
        observation_id=f"price-{observed_at.isoformat()}",
        run_id="hotel-price-run",
        hotel_id="hotel_qn_001",
        offer_key="deluxe|breakfast",
        source_record_id="trivago-record-1",
        source_id="trivago-web",
        mapping_id="trivago-mapping-1",
        external_id="trivago-hotel-1",
        seller="Booking.com",
        room_type="Deluxe",
        check_in=date(2026, 8, 25),
        check_out=date(2026, 8, 26),
        amount=Decimal("1250000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=observed_at,
        verification_status=verification_status,
    )


def _availability() -> HotelAvailabilityObservation:
    return HotelAvailabilityObservation(
        observation_id="availability-1",
        run_id="hotel-availability-run",
        hotel_id="hotel_qn_001",
        source_record_id="trivago-record-1",
        source_id="trivago-web",
        source_url="https://www.trivago.vn/",
        mapping_id="trivago-mapping-1",
        external_id="trivago-hotel-1",
        requested_check_in=date(2026, 8, 25),
        check_in=date(2026, 8, 25),
        check_out=date(2026, 8, 26),
        nights=1,
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        offer_count=1,
        price_observation_ids=[_price().observation_id],
        observed_at=NOW,
        verification_status=VerificationStatus.HUMAN_VERIFIED,
    )


def _opening(
    *,
    verification_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED,
) -> OpeningStatusObservation:
    return OpeningStatusObservation(
        observation_id="opening-1",
        run_id="google-run",
        place_id="cafe_dn_062",
        source_record_ids=["google-record-1"],
        local_date=date(2026, 8, 24),
        status=DailyOpeningStatus.OPEN_TODAY,
        is_24_hours=True,
        open_now=True,
        observed_at=NOW,
        verification_status=verification_status,
    )


def _google_place(
    *,
    verification_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED,
    opening_status: VerificationStatus = VerificationStatus.AUTO_VERIFIED,
) -> GoogleMapsPlaceObservation:
    return GoogleMapsPlaceObservation(
        observation_id="google-place-1",
        run_id="google-run",
        place_id="cafe_dn_062",
        source_record_id="google-record-1",
        source_id="google-maps-web",
        source_url="https://www.google.com/maps/place/xom-meo",
        name="Xóm Mèo Coffee & Petshop Đà Nẵng",
        business_status=BusinessStatus.ACTIVE,
        opening=_opening(verification_status=opening_status),
        observed_at=NOW,
        verification_status=verification_status,
    )


@pytest.mark.parametrize(
    ("observation", "expected_type"),
    [
        (_price(), AcceptedObservationType.HOTEL_PRICE),
        (_availability(), AcceptedObservationType.HOTEL_AVAILABILITY),
        (_opening(), AcceptedObservationType.GOOGLE_OPENING_STATUS),
        (_google_place(), AcceptedObservationType.GOOGLE_PLACE),
    ],
)
def test_writes_supported_observations_as_self_verifying_artifacts(
    tmp_path: Path,
    observation: object,
    expected_type: AcceptedObservationType,
) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")

    path = store.write(observation)  # type: ignore[arg-type]
    artifact = AcceptedObservationArtifact.model_validate_json(path.read_bytes())

    assert artifact.observation_type is expected_type
    assert artifact.content_hash == accepted_observation_hash(artifact.observation)
    assert f"type={expected_type.value}" in path.as_posix()
    assert "date=2026-08-24" in path.as_posix()
    assert path.name == f"observation={artifact.content_hash}.json"


def test_identical_write_is_idempotent_and_concurrency_safe(tmp_path: Path) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")
    observation = _price()

    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(store.write, [observation] * 8))

    assert len(set(paths)) == 1
    assert len(list((tmp_path / "observations").rglob("observation=*.json"))) == 1


def test_changed_content_appends_new_artifact_without_replacing_prior(
    tmp_path: Path,
) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")
    first = store.write(_price())
    first_bytes = first.read_bytes()

    second = store.write(_price(observed_at=NOW + timedelta(hours=5)))

    assert second != first
    assert first.read_bytes() == first_bytes
    assert first.exists() and second.exists()


@pytest.mark.parametrize(
    "status",
    [
        VerificationStatus.PENDING_REVIEW,
        VerificationStatus.QUARANTINED,
        VerificationStatus.REJECTED,
    ],
)
def test_rejects_unverified_top_level_observations(
    tmp_path: Path,
    status: VerificationStatus,
) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")

    with pytest.raises(UnverifiedObservationError, match=status.value):
        store.write(_price(verification_status=status))

    assert not list((tmp_path / "observations").rglob("observation=*.json"))


def test_rejects_google_place_with_unverified_nested_evidence(tmp_path: Path) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")

    with pytest.raises(UnverifiedObservationError, match="opening=pending_review"):
        store.write(
            _google_place(opening_status=VerificationStatus.PENDING_REVIEW)
        )


@pytest.mark.parametrize(
    "observation",
    [
        _availability().model_copy(
            update={
                "status": HotelAvailabilityStatus.UNKNOWN,
                "reason": HotelAvailabilityReason.NO_PRICE,
                "offer_count": 0,
                "price_observation_ids": [],
            }
        ),
        _opening().model_copy(update={"status": DailyOpeningStatus.UNKNOWN}),
        _google_place().model_copy(
            update={"business_status": BusinessStatus.UNKNOWN}
        ),
    ],
)
def test_rejects_semantically_unknown_observations(
    tmp_path: Path,
    observation: object,
) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")

    with pytest.raises(UnverifiedObservationError, match="unknown"):
        store.write(observation)  # type: ignore[arg-type]

    assert not list((tmp_path / "observations").rglob("observation=*.json"))


def test_existing_invalid_content_address_is_never_overwritten(tmp_path: Path) -> None:
    store = AcceptedObservationStore(tmp_path / "observations")
    observation = _opening()
    destination = store.destination_for(observation)
    destination.parent.mkdir(parents=True)
    destination.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        AcceptedObservationConflictError,
        match="immutable accepted observation is invalid",
    ):
        store.write(observation)

    assert destination.read_text(encoding="utf-8") == "{}\n"
