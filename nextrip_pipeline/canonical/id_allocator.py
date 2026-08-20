from __future__ import annotations

import re
from collections.abc import Iterable
from threading import Lock

from nextrip_pipeline.schemas import EntityType

from .candidate import (
    CandidateDisposition,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
)


_ID_PATTERN = re.compile(
    r"^(?P<entity>attr|cafe|hotel|night|rest)_"
    r"(?P<city>dn|qn)_(?P<sequence>[0-9]{3,})$"
)
_ENTITY_SLOTS = {
    EntityType.ATTRACTION: "attr",
    EntityType.CAFE: "cafe",
    EntityType.HOTEL: "hotel",
    EntityType.NIGHTLIFE: "night",
    EntityType.RESTAURANT: "rest",
}
_CITY_SLOTS = {
    "city_da_nang": "dn",
    "da_nang": "dn",
    "da nang": "dn",
    "đà nẵng": "dn",
    "dn": "dn",
    "city_quy_nhon": "qn",
    "quy_nhon": "qn",
    "quy nhon": "qn",
    "quy nhơn": "qn",
    "qn": "qn",
}


class PlaceIdAllocationError(ValueError):
    pass


class MonotonicPlaceIdAllocator:
    """Allocates only above a slot's existing/retired high-water mark."""

    def __init__(
        self,
        *,
        existing_ids: Iterable[str],
        retired_ids: Iterable[str] = (),
        reserved_ids: Iterable[str] = (),
    ) -> None:
        self._existing = {_canonical_id(value) for value in existing_ids}
        self._retired = {_canonical_id(value) for value in retired_ids}
        self._blocked = (
            self._existing
            | self._retired
            | {_canonical_id(value) for value in reserved_ids}
        )
        self._lock = Lock()

    def allocate(
        self,
        candidate: CanonicalReplacementCandidate,
        validation: CandidateValidationResult,
        *,
        replacement_of: str | None = None,
    ) -> str:
        if validation.candidate_key != candidate.candidate_key:
            raise PlaceIdAllocationError("validation belongs to another candidate")
        if validation.disposition is not CandidateDisposition.PASS:
            raise PlaceIdAllocationError(
                "only a distinct PASS candidate may receive an ID"
            )

        entity_slot = _ENTITY_SLOTS[candidate.entity_type]
        city_slot = _city_slot(candidate.city_id)
        if replacement_of is not None:
            replacement = _canonical_id(replacement_of)
            if replacement not in self._retired:
                raise PlaceIdAllocationError(
                    "replacement_of must identify a retired canonical ID"
                )
            match = _ID_PATTERN.fullmatch(replacement)
            assert match is not None
            replacement_slot = (match.group("entity"), match.group("city"))
            if replacement_slot != (entity_slot, city_slot):
                raise PlaceIdAllocationError(
                    "replacement candidate must preserve the retired entity/city slot"
                )

        with self._lock:
            high_water = max(
                (
                    int(match.group("sequence"))
                    for value in self._blocked
                    if (match := _ID_PATTERN.fullmatch(value)) is not None
                    and match.group("entity") == entity_slot
                    and match.group("city") == city_slot
                ),
                default=0,
            )
            sequence = high_water + 1
            while True:
                allocated = f"{entity_slot}_{city_slot}_{sequence:03d}"
                if allocated not in self._blocked:
                    self._blocked.add(allocated)
                    return allocated
                sequence += 1


def _canonical_id(value: str) -> str:
    candidate = value.strip().casefold()
    if _ID_PATTERN.fullmatch(candidate) is None:
        raise PlaceIdAllocationError(f"unsupported canonical place ID: {value!r}")
    return candidate


def _city_slot(value: str) -> str:
    normalized = " ".join(value.strip().casefold().replace("-", " ").split())
    try:
        return _CITY_SLOTS[normalized]
    except KeyError as error:
        raise PlaceIdAllocationError(
            f"unsupported canonical city: {value!r}"
        ) from error
