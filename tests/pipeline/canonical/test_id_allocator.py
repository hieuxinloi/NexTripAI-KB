from __future__ import annotations

import pytest

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
)
from nextrip_pipeline.canonical.id_allocator import (
    MonotonicPlaceIdAllocator,
    PlaceIdAllocationError,
)
from nextrip_pipeline.schemas import EntityType


def _candidate(
    *,
    key: str = "candidate-1",
    entity_type: EntityType = EntityType.CAFE,
    city_id: str = "city_da_nang",
) -> CanonicalReplacementCandidate:
    return CanonicalReplacementCandidate(
        candidate_key=key,
        entity_type=entity_type,
        city_id=city_id,
        name="New place",
    )


def _pass(key: str = "candidate-1") -> CandidateValidationResult:
    return CandidateValidationResult(
        candidate_key=key,
        status=CandidateDisposition.PASS,
        reason_codes=[CandidateReasonCode.NO_DUPLICATE_SIGNAL],
    )


def test_allocator_uses_high_water_mark_in_entity_city_slot() -> None:
    allocator = MonotonicPlaceIdAllocator(
        existing_ids=["cafe_dn_001", "cafe_dn_063", "hotel_dn_090"],
        retired_ids=["cafe_dn_064"],
        reserved_ids=["cafe_dn_066"],
    )

    assert allocator.allocate(_candidate(), _pass()) == "cafe_dn_067"
    assert (
        allocator.allocate(_candidate(key="candidate-2"), _pass("candidate-2"))
        == "cafe_dn_068"
    )


def test_replacement_must_be_retired_and_preserve_slot() -> None:
    allocator = MonotonicPlaceIdAllocator(
        existing_ids=["cafe_dn_062"],
        retired_ids=["cafe_qn_036", "hotel_dn_073"],
    )

    with pytest.raises(PlaceIdAllocationError, match="entity/city slot"):
        allocator.allocate(
            _candidate(),
            _pass(),
            replacement_of="cafe_qn_036",
        )
    with pytest.raises(PlaceIdAllocationError, match="retired canonical ID"):
        allocator.allocate(
            _candidate(),
            _pass(),
            replacement_of="cafe_dn_062",
        )


def test_valid_replacement_gets_new_id_in_the_retired_slot() -> None:
    allocator = MonotonicPlaceIdAllocator(
        existing_ids=["cafe_dn_062"],
        retired_ids=["cafe_dn_064"],
    )

    allocated = allocator.allocate(
        _candidate(),
        _pass(),
        replacement_of="cafe_dn_064",
    )

    assert allocated == "cafe_dn_065"
    assert allocated not in {"cafe_dn_062", "cafe_dn_064"}


def test_valid_quarantined_replacement_uses_fresh_id_above_high_water() -> None:
    allocator = MonotonicPlaceIdAllocator(
        existing_ids=["cafe_dn_016"],
        quarantined_ids=["cafe_dn_017"],
    )

    allocated = allocator.allocate(
        _candidate(),
        _pass(),
        replacement_of="cafe_dn_017",
    )

    assert allocated == "cafe_dn_018"
    assert allocated not in {"cafe_dn_016", "cafe_dn_017"}
    assert (
        allocator.allocate(_candidate(key="candidate-2"), _pass("candidate-2"))
        == "cafe_dn_019"
    )


def test_allocator_never_allocates_non_pass_candidate() -> None:
    allocator = MonotonicPlaceIdAllocator(existing_ids=["cafe_dn_062"])
    review = CandidateValidationResult(
        candidate_key="candidate-1",
        status=CandidateDisposition.REVIEW,
        reason_codes=[CandidateReasonCode.PHONE_MATCH],
        matches=[],
    )

    with pytest.raises(PlaceIdAllocationError, match="distinct PASS"):
        allocator.allocate(_candidate(), review)


def test_allocator_rejects_validation_for_another_candidate() -> None:
    allocator = MonotonicPlaceIdAllocator(existing_ids=[])

    with pytest.raises(PlaceIdAllocationError, match="another candidate"):
        allocator.allocate(_candidate(), _pass("candidate-2"))


def test_allocator_preserves_prefix_beyond_three_digits() -> None:
    allocator = MonotonicPlaceIdAllocator(existing_ids=["rest_qn_999"])
    candidate = _candidate(
        entity_type=EntityType.RESTAURANT,
        city_id="city_quy_nhon",
    )

    assert allocator.allocate(candidate, _pass()) == "rest_qn_1000"
