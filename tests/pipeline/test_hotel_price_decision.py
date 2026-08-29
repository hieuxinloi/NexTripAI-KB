from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nextrip_pipeline.decision_gate import (
    HotelPriceDecisionGate,
    HotelPriceDecisionStatus,
)
from nextrip_pipeline.publishing import (
    CurrentHotelPriceSnapshot,
    CurrentHotelPriceWriter,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    HotelPriceObservation,
    MappingStatus,
    Occupancy,
    OfferAvailability,
    ValidationStatus,
)
from nextrip_pipeline.validators import HotelPriceValidatorOrchestrator


NOW = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)


def _mapping(status: MappingStatus = MappingStatus.CONFIRMED) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id="mapping-1",
        entity_id="hotel_qn_025",
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id="292003c34d4f",
        status=status,
        matched_at=NOW,
        verified_at=NOW if status is MappingStatus.CONFIRMED else None,
    )


def _observation(
    observation_id: str,
    amount: str,
    observed_at: datetime,
) -> HotelPriceObservation:
    return HotelPriceObservation(
        observation_id=observation_id,
        run_id=f"run-{observation_id}",
        hotel_id="hotel_qn_025",
        offer_key="292003c34d4f|booking-com|1r-2a-0c",
        source_record_id=f"source-{observation_id}",
        source_id="trivago-mcp",
        seller="Booking.com",
        room_type="unspecified",
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 21),
        occupancy=Occupancy(),
        currency="VND",
        amount=Decimal(amount),
        nightly_amount=Decimal(amount),
        total_amount=Decimal(amount),
        availability=OfferAvailability.AVAILABLE,
        observed_at=observed_at,
    )


def _decision(
    observation: HotelPriceObservation,
    *,
    validated_at: datetime,
    mapping: ExternalEntityMapping | None = None,
):
    validations = HotelPriceValidatorOrchestrator(clock=lambda: validated_at).validate(
        observation, mapping or _mapping()
    )
    decision = HotelPriceDecisionGate(clock=lambda: validated_at).decide(
        observation, validations
    )
    return validations, decision


def test_price_change_passes_and_replaces_current_immediately(tmp_path) -> None:
    first = _observation("price-1", "1000000", NOW)
    first_validations, first_decision = _decision(first, validated_at=NOW)
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    path = writer.publish(first, first_decision)

    changed = _observation("price-2", "9000000", NOW + timedelta(hours=4))
    changed_validations, changed_decision = _decision(
        changed,
        validated_at=NOW + timedelta(hours=4),
    )
    writer.publish(changed, changed_decision)
    current = CurrentHotelPriceSnapshot.model_validate_json(
        path.read_text(encoding="utf-8")
    )

    assert all(item.status is ValidationStatus.PASS for item in first_validations)
    assert all(item.status is ValidationStatus.PASS for item in changed_validations)
    assert first_decision.status is HotelPriceDecisionStatus.PASS
    assert changed_decision.status is HotelPriceDecisionStatus.PASS
    assert current.observation.nightly_amount == Decimal("9000000")


def test_stale_or_unconfirmed_price_is_quarantined_and_current_is_kept(
    tmp_path,
) -> None:
    current_observation = _observation("price-current", "1500000", NOW)
    _, current_decision = _decision(current_observation, validated_at=NOW)
    writer = CurrentHotelPriceWriter(tmp_path, clock=lambda: NOW)
    path = writer.publish(current_observation, current_decision)
    previous_content = path.read_text(encoding="utf-8")

    stale = _observation("price-stale", "700000", NOW)
    validations, decision = _decision(
        stale,
        validated_at=NOW + timedelta(hours=6),
        mapping=_mapping(MappingStatus.AUTO_MATCHED),
    )

    assert any(item.status is ValidationStatus.FAIL for item in validations)
    assert decision.status is HotelPriceDecisionStatus.QUARANTINE
    with pytest.raises(ValueError, match="only PASS"):
        writer.publish(stale, decision)
    assert path.read_text(encoding="utf-8") == previous_content
