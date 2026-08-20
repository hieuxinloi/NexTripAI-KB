from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from nextrip_pipeline.schemas import BusinessStatus, EntityType

from .candidate import (
    CandidateDisposition,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
)


@dataclass(frozen=True, slots=True)
class ConservativeCityBounds:
    min_latitude: float
    max_latitude: float
    min_longitude: float
    max_longitude: float

    def __post_init__(self) -> None:
        if not -90 <= self.min_latitude < self.max_latitude <= 90:
            raise ValueError("invalid city latitude bounds")
        if not -180 <= self.min_longitude < self.max_longitude <= 180:
            raise ValueError("invalid city longitude bounds")

    def contains(self, latitude: float, longitude: float) -> bool:
        return (
            self.min_latitude <= latitude <= self.max_latitude
            and self.min_longitude <= longitude <= self.max_longitude
        )


def _default_city_bounds() -> dict[str, ConservativeCityBounds]:
    return {
        # Includes Ba Na Hills and the Da Nang urban/coastal area. Hoi An city
        # centre is south-east of this conservative envelope.
        "city_da_nang": ConservativeCityBounds(
            min_latitude=15.92,
            max_latitude=16.24,
            min_longitude=107.80,
            max_longitude=108.32,
        ),
        # Urban Quy Nhon, including Ghenh Rang and the northern urban wards.
        "city_quy_nhon": ConservativeCityBounds(
            min_latitude=13.70,
            max_latitude=13.86,
            min_longitude=109.12,
            max_longitude=109.30,
        ),
    }


def _default_incompatible_name_keywords() -> dict[EntityType, tuple[str, ...]]:
    return {
        EntityType.NIGHTLIFE: (
            "thu\u00ea loa",
            "d\u1ecbch v\u1ee5 loa",
            "cho thu\u00ea loa",
            "speaker rental",
            "karaoke equipment rental",
        )
    }


def _default_exact_categories() -> dict[EntityType, tuple[str, ...]]:
    return {
        EntityType.ATTRACTION: (
            "attraction",
            "tourist attraction",
            "museum",
            "beach",
            "park",
            "historical landmark",
            "scenic spot",
            "pagoda",
            "\u0111i\u1ec3m thu h\u00fat kh\u00e1ch du l\u1ecbch",
            "th\u1eafng c\u1ea3nh",
            "danh lam th\u1eafng c\u1ea3nh",
            "b\u1ea3o t\u00e0ng",
            "b\u00e3i bi\u1ec3n",
            "c\u00f4ng vi\u00ean",
            "\u0111\u1ecba danh l\u1ecbch s\u1eed",
            "ch\u00f9a",
            "\u0111\u1ec1n",
            "\u0111\u1ec1n th\u1edd",
            "th\u00e1c n\u01b0\u1edbc",
        ),
        EntityType.CAFE: (
            "cafe",
            "cafeteria",
            "coffee shop",
            "coffee store",
            "qu\u00e1n c\u00e0 ph\u00ea",
            "ti\u1ec7m c\u00e0 ph\u00ea",
        ),
        EntityType.HOTEL: (
            "hotel",
            "hostel",
            "lodging",
            "motel",
            "resort hotel",
            "homestay",
            "kh\u00e1ch s\u1ea1n",
            "kh\u00e1ch s\u1ea1n ngh\u1ec9 d\u01b0\u1ee1ng",
            "khu ngh\u1ec9 d\u01b0\u1ee1ng",
            "nh\u00e0 ngh\u1ec9",
        ),
        EntityType.NIGHTLIFE: (
            "bar",
            "cocktail bar",
            "karaoke bar",
            "lounge",
            "night club",
            "nightclub",
            "pub",
            "qu\u00e1n bar",
            "qu\u00e1n r\u01b0\u1ee3u",
            "qu\u00e1n r\u01b0\u1ee3u cocktail",
            "c\u00e2u l\u1ea1c b\u1ed9 \u0111\u00eam",
            "h\u1ed9p \u0111\u00eam",
            "qu\u00e1n karaoke",
        ),
        EntityType.RESTAURANT: (
            "restaurant",
            "food court",
            "seafood restaurant",
            "vietnamese restaurant",
            "nh\u00e0 h\u00e0ng",
            "qu\u00e1n \u0103n",
            "nh\u00e0 h\u00e0ng vi\u1ec7t nam",
            "nh\u00e0 h\u00e0ng h\u1ea3i s\u1ea3n",
            "khu \u0103n u\u1ed1ng",
        ),
    }


def _default_keyword_categories() -> dict[EntityType, tuple[str, ...]]:
    return {
        EntityType.ATTRACTION: (
            "tourist attraction",
            "museum",
            "beach",
            "historical landmark",
            "pagoda",
            "scenic spot",
            "temple",
            "waterfall",
            "\u0111i\u1ec3m thu h\u00fat kh\u00e1ch du l\u1ecbch",
            "th\u1eafng c\u1ea3nh",
            "b\u1ea3o t\u00e0ng",
            "b\u00e3i bi\u1ec3n",
            "c\u00f4ng vi\u00ean",
            "\u0111\u1ecba danh l\u1ecbch s\u1eed",
            "ch\u00f9a",
            "th\u00e1c n\u01b0\u1edbc",
        ),
        EntityType.CAFE: (
            "cafe",
            "coffee shop",
            "coffeehouse",
            "qu\u00e1n c\u00e0 ph\u00ea",
            "ti\u1ec7m c\u00e0 ph\u00ea",
        ),
        EntityType.HOTEL: (
            "hotel",
            "hostel",
            "motel",
            "resort",
            "homestay",
            "kh\u00e1ch s\u1ea1n",
            "nh\u00e0 ngh\u1ec9",
            "khu ngh\u1ec9 d\u01b0\u1ee1ng",
        ),
        EntityType.NIGHTLIFE: (
            "bar",
            "cocktail bar",
            "karaoke",
            "lounge",
            "night club",
            "nightclub",
            "pub",
            "qu\u00e1n bar",
            "qu\u00e1n r\u01b0\u1ee3u",
            "c\u00e2u l\u1ea1c b\u1ed9 \u0111\u00eam",
            "h\u1ed9p \u0111\u00eam",
            "qu\u00e1n karaoke",
        ),
        EntityType.RESTAURANT: (
            "restaurant",
            "food court",
            "seafood restaurant",
            "nh\u00e0 h\u00e0ng",
            "qu\u00e1n \u0103n",
            "bistro",
            "eatery",
            "khu \u0103n u\u1ed1ng",
        ),
    }


@dataclass(frozen=True, slots=True)
class CandidateEntityEligibilityPolicy:
    """Conservative Google category vocabulary, replaceable by configuration."""

    exact_categories: Mapping[EntityType, tuple[str, ...]] = field(
        default_factory=_default_exact_categories
    )
    keyword_categories: Mapping[EntityType, tuple[str, ...]] = field(
        default_factory=_default_keyword_categories
    )
    ambiguous_categories: tuple[str, ...] = (
        "business",
        "establishment",
        "point of interest",
        "store",
        "bridge",
        "c\u1ea7u",
        "c\u1eeda h\u00e0ng",
        "doanh nghi\u1ec7p",
    )
    city_bounds: Mapping[str, ConservativeCityBounds] = field(
        default_factory=_default_city_bounds
    )
    incompatible_name_keywords: Mapping[EntityType, tuple[str, ...]] = field(
        default_factory=_default_incompatible_name_keywords
    )

    def __post_init__(self) -> None:
        for mapping in (self.exact_categories, self.keyword_categories):
            for entity_type, values in mapping.items():
                if not isinstance(entity_type, EntityType):
                    raise ValueError("category policy keys must be EntityType values")
                if any(not _category_key(value) for value in values):
                    raise ValueError("category policy terms must be non-empty")
        if any(not _category_key(value) for value in self.ambiguous_categories):
            raise ValueError("ambiguous category terms must be non-empty")
        if any(not city_id.strip() for city_id in self.city_bounds):
            raise ValueError("city boundary IDs must be non-empty")
        for entity_type, values in self.incompatible_name_keywords.items():
            if not isinstance(entity_type, EntityType):
                raise ValueError(
                    "incompatible-name policy keys must be EntityType values"
                )
            if any(not _category_key(value) for value in values):
                raise ValueError("incompatible-name keywords must be non-empty")

    def matching_entity_types(self, category: str) -> set[EntityType]:
        key = _category_key(category)
        padded = f" {key} "
        matches: set[EntityType] = set()
        for entity_type in EntityType:
            exact = {
                _category_key(value)
                for value in self.exact_categories.get(entity_type, ())
            }
            keywords = {
                _category_key(value)
                for value in self.keyword_categories.get(entity_type, ())
            }
            if key in exact or any(f" {term} " in padded for term in keywords):
                matches.add(entity_type)
        return matches

    def is_explicitly_ambiguous(self, category: str) -> bool:
        key = _category_key(category)
        return key in {_category_key(value) for value in self.ambiguous_categories}

    def has_incompatible_name_keyword(
        self,
        entity_type: EntityType,
        name: str,
    ) -> bool:
        name_key = _category_key(name)
        padded = f" {name_key} "
        return any(
            f" {_category_key(keyword)} " in padded
            for keyword in self.incompatible_name_keywords.get(entity_type, ())
        )


class CandidateEntityEligibilityValidator:
    """Apply business status and requested-entity gates after distinctness."""

    def __init__(
        self,
        policy: CandidateEntityEligibilityPolicy | None = None,
    ) -> None:
        self.policy = policy or CandidateEntityEligibilityPolicy()

    def validate(
        self,
        candidate: CanonicalReplacementCandidate,
        validation: CandidateValidationResult,
    ) -> CandidateValidationResult:
        if validation.candidate_key != candidate.candidate_key:
            raise ValueError("validation belongs to another candidate")

        # A definite duplicate remains a duplicate regardless of category or
        # business status. Do not replace its stronger evidence classification.
        if validation.status is CandidateDisposition.REJECT:
            return validation

        reasons = set(validation.reason_codes)
        if candidate.business_status is BusinessStatus.PERMANENTLY_CLOSED:
            reasons.add(CandidateReasonCode.GOOGLE_PERMANENTLY_CLOSED)
            return _result(validation, CandidateDisposition.REJECT, reasons)

        location = candidate.location
        boundary_unavailable = False
        if location is not None and location.source == "google-maps-web":
            bounds = self.policy.city_bounds.get(candidate.city_id)
            if bounds is None:
                reasons.add(CandidateReasonCode.GOOGLE_CITY_BOUNDARY_UNAVAILABLE)
                boundary_unavailable = True
            elif not bounds.contains(location.latitude, location.longitude):
                reasons.add(CandidateReasonCode.GOOGLE_CITY_BOUNDARY_MISMATCH)
                return _result(validation, CandidateDisposition.REJECT, reasons)

        category = candidate.provider_category
        if category is None or not _category_key(category):
            category_reason = CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_MISSING
        else:
            matches = self.policy.matching_entity_types(category)
            if self.policy.is_explicitly_ambiguous(category) or len(matches) != 1:
                category_reason = (
                    CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_AMBIGUOUS
                )
            elif candidate.entity_type in matches:
                category_reason = (
                    CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE
                )
            else:
                category_reason = (
                    CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_INCOMPATIBLE
                )

        reasons.add(category_reason)
        status = validation.status
        if status is CandidateDisposition.PASS and boundary_unavailable:
            status = CandidateDisposition.REVIEW
        if (
            status is CandidateDisposition.PASS
            and category_reason
            is not CandidateReasonCode.GOOGLE_PROVIDER_CATEGORY_COMPATIBLE
        ):
            status = CandidateDisposition.REVIEW
        if self.policy.has_incompatible_name_keyword(
            candidate.entity_type,
            candidate.name,
        ):
            reasons.add(CandidateReasonCode.ENTITY_NAME_INCOMPATIBLE_KEYWORD)
            if status is CandidateDisposition.PASS:
                status = CandidateDisposition.REVIEW
        return _result(validation, status, reasons)


def _result(
    previous: CandidateValidationResult,
    status: CandidateDisposition,
    reasons: set[CandidateReasonCode],
) -> CandidateValidationResult:
    return CandidateValidationResult(
        candidate_key=previous.candidate_key,
        status=status,
        reason_codes=sorted(reasons, key=lambda item: item.value),
        matches=previous.matches,
    )


def _category_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold()).replace("\u0111", "d")
    without_marks = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", without_marks))
