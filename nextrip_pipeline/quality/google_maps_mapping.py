from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from nextrip_pipeline.google_maps_identity import google_maps_stable_external_ids
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    GoogleMapsPlaceObservation,
    MappingStatus,
    NexTripModel,
)


class ImmutableQualityModel(NexTripModel):
    """Strict, immutable contract used for decisions and queued requests."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        frozen=True,
    )


class MappingResolutionStatus(StrEnum):
    AUTO_CONFIRM = "auto_confirm"
    REVIEW = "review"
    REJECT = "reject"


class PlaceIdentitySnapshot(ImmutableQualityModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    category: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class PlaceIdentityEvidence(ImmutableQualityModel):
    master: PlaceIdentitySnapshot
    observed: PlaceIdentitySnapshot
    name_score: float | None = Field(default=None, ge=0, le=1)
    address_score: float | None = Field(default=None, ge=0, le=1)
    city_score: float | None = Field(default=None, ge=0, le=1)
    category_score: float | None = Field(default=None, ge=0, le=1)
    coordinate_score: float | None = Field(default=None, ge=0, le=1)
    coordinate_distance_m: float | None = Field(default=None, ge=0)


class GoogleMapsMappingResolution(ImmutableQualityModel):
    resolver_version: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    status: MappingResolutionStatus
    score: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...]
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: PlaceIdentityEvidence
    resolved_at: AwareDatetime


class ResolverPolicy(ImmutableQualityModel):
    auto_confirm_max_distance_m: float = Field(default=500, gt=0)
    reject_min_distance_m: float = Field(default=3000, gt=0)
    auto_confirm_min_name_score: float = Field(default=0.72, ge=0, le=1)
    auto_confirm_min_score: float = Field(default=0.78, ge=0, le=1)
    secondary_evidence_min_score: float = Field(default=0.45, ge=0, le=1)


class GoogleMapsMappingResolver:
    """Resolve Maps identity without an LLM or mutable external state.

    Auto-confirmation deliberately requires both a city match and a close
    coordinate match. Missing safety evidence always produces REVIEW. A known
    city conflict, a large coordinate conflict, or a broken mapping binding is
    rejected without semantic review.
    """

    resolver_version = "1.2.0"
    _weights = {
        "name": 0.40,
        "address": 0.20,
        "city": 0.15,
        "category": 0.10,
        "coordinate": 0.15,
    }
    _category_terms = {
        EntityType.ATTRACTION: {
            "amusement park",
            "art gallery",
            "attraction",
            "bảo tàng",
            "bãi biển",
            "beach",
            "chợ truyền thống",
            "chùa phật giáo",
            "công viên giải trí",
            "công viên nước",
            "cultural landmark",
            "danh lam thắng cảnh",
            "đảo",
            "địa danh lịch sử",
            "địa điểm hành hương",
            "điểm tắm suối khoáng nóng kiểu nhật",
            "điểm mốc lịch sử",
            "điểm thu hút khách du lịch",
            "đỉnh núi",
            "di tích lịch sử",
            "hồ",
            "historical landmark",
            "khu bảo tồn thiên nhiên",
            "museum",
            "pagoda",
            "park",
            "phòng trưng bày nghệ thuật",
            "quần đảo",
            "sân chơi",
            "scenic spot",
            "temple",
            "thắng cảnh",
            "tourist attraction",
            "trung tâm vui chơi giải trí",
            "vườn bách thú",
        },
        EntityType.CAFE: {
            "bakery",
            "bubble tea",
            "cafe",
            "coffee",
            "coffee shop",
            "cửa hàng cà phê",
            "dessert",
            "quán cà phê",
            "quán trà",
            "quán trà sữa",
            "quan ca phe",
            "tea house",
            "tiệm cà phê",
            "tiệm trà",
            "trà trân châu",
        },
        EntityType.HOTEL: {
            "apartment",
            "căn hộ dịch vụ",
            "guest house",
            "homestay",
            "hostel",
            "hotel",
            "khách sạn",
            "khu nghỉ dưỡng",
            "lodging",
            "motel",
            "nhà khách",
            "nhà nghỉ",
            "resort",
            "villa",
        },
        EntityType.NIGHTLIFE: {
            "bar",
            "câu lạc bộ đêm",
            "câu lạc bộ bãi biển",
            "cocktail bar",
            "club",
            "hộp đêm",
            "karaoke",
            "lounge",
            "night club",
            "nightclub",
            "pub",
            "quán bar cocktail",
            "quán karaoke",
            "quán rượu",
            "vũ trường",
        },
        EntityType.RESTAURANT: {
            "bistro",
            "breakfast restaurant",
            "buffet",
            "cửa hàng bán đồ ăn nấu sẵn",
            "eatery",
            "food",
            "grill",
            "khu ẩm thực",
            "nhà hàng",
            "nha hang",
            "noodle",
            "pizza",
            "quán ăn",
            "restaurant",
            "seafood",
            "sushi",
            "tiệm ăn",
            "vegan restaurant",
        },
    }

    def __init__(
        self,
        *,
        policy: ResolverPolicy | None = None,
        known_cities: Sequence[str] = ("Đà Nẵng", "Quy Nhơn"),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or ResolverPolicy()
        self.known_cities = tuple(
            dict.fromkeys(self._normalize_text(city) for city in known_cities)
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if (
            self.policy.auto_confirm_max_distance_m
            >= self.policy.reject_min_distance_m
        ):
            raise ValueError(
                "auto-confirm distance must be below the reject distance"
            )

    def resolve(
        self,
        mapping: ExternalEntityMapping,
        observation: GoogleMapsPlaceObservation,
        *,
        canonical_url: str | None = None,
    ) -> GoogleMapsMappingResolution:
        master = self._master_snapshot(mapping)
        observed = self._observed_snapshot(observation, master.city)
        evidence = self._score_evidence(mapping, master, observed)
        score = self._weighted_score(evidence)
        hard_conflicts = self._hard_conflicts(mapping, observation, evidence)
        if hard_conflicts:
            status = MappingResolutionStatus.REJECT
            reasons = hard_conflicts
        elif self._can_auto_confirm(evidence, score):
            stable_ids = self._stable_external_ids(
                mapping,
                observation,
                canonical_url=canonical_url,
            )
            if len(stable_ids) == 1:
                status = MappingResolutionStatus.AUTO_CONFIRM
                reasons = ("STRONG_IDENTITY_MATCH",)
            else:
                status = MappingResolutionStatus.REVIEW
                reasons = (
                    "STABLE_EXTERNAL_ID_CONFLICT"
                    if stable_ids
                    else "STABLE_EXTERNAL_ID_MISSING",
                )
        else:
            status = MappingResolutionStatus.REVIEW
            reasons = self._review_reasons(evidence, score)
        evidence_hash = self._evidence_hash(mapping, observation, evidence)
        return GoogleMapsMappingResolution(
            resolver_version=self.resolver_version,
            mapping_id=mapping.mapping_id,
            place_id=mapping.entity_id,
            observation_id=observation.observation_id,
            source_record_id=observation.source_record_id,
            status=status,
            score=score,
            reason_codes=reasons,
            evidence_hash=evidence_hash,
            evidence=evidence,
            resolved_at=self.clock(),
        )

    def resolve_and_update(
        self,
        mapping: ExternalEntityMapping,
        observation: GoogleMapsPlaceObservation,
        *,
        canonical_url: str | None = None,
    ) -> tuple[GoogleMapsMappingResolution, ExternalEntityMapping]:
        """Resolve once and safely materialize an AUTO_CONFIRM mapping.

        REVIEW and REJECT results return the original mapping unchanged, which
        makes this method convenient for refresh and historical reprocessing.
        """

        resolution = self.resolve(
            mapping,
            observation,
            canonical_url=canonical_url,
        )
        if resolution.status is not MappingResolutionStatus.AUTO_CONFIRM:
            return resolution, mapping
        return resolution, apply_auto_confirmation(
            mapping,
            observation,
            resolution,
            canonical_url=canonical_url,
        )

    def _master_snapshot(
        self, mapping: ExternalEntityMapping
    ) -> PlaceIdentitySnapshot:
        latitude = mapping.attributes.get("master_latitude")
        longitude = mapping.attributes.get("master_longitude")
        return PlaceIdentitySnapshot(
            name=self._optional_text(
                mapping.attributes.get("master_name") or mapping.external_id
            ),
            address=self._optional_text(mapping.attributes.get("master_address")),
            city=self._optional_text(mapping.attributes.get("master_city")),
            category=mapping.entity_type.value if mapping.entity_type else None,
            latitude=float(latitude) if isinstance(latitude, (int, float)) else None,
            longitude=float(longitude)
            if isinstance(longitude, (int, float))
            else None,
        )

    @staticmethod
    def _stable_external_ids(
        mapping: ExternalEntityMapping,
        observation: GoogleMapsPlaceObservation,
        *,
        canonical_url: str | None,
    ) -> tuple[str, ...]:
        return google_maps_stable_external_ids(
            canonical_url,
            observation.source_url,
            mapping.external_id,
            mapping.external_url,
            mapping.attributes.get("canonical_google_maps_url"),
            mapping.attributes.get("google_external_id"),
            mapping.attributes.get("google_place_id"),
        )

    def _observed_snapshot(
        self,
        observation: GoogleMapsPlaceObservation,
        expected_city: str | None,
    ) -> PlaceIdentitySnapshot:
        google_location = (
            observation.location
            if observation.location is not None
            and observation.location.source == observation.source_id
            and observation.location.accuracy != "verified_master_fallback"
            else None
        )
        latitude = google_location.latitude if google_location else None
        longitude = google_location.longitude if google_location else None
        return PlaceIdentitySnapshot(
            name=self._optional_text(observation.name),
            address=self._optional_text(observation.address),
            city=self._detect_city(observation.address, expected_city),
            category=self._optional_text(observation.category),
            latitude=latitude,
            longitude=longitude,
        )

    def _score_evidence(
        self,
        mapping: ExternalEntityMapping,
        master: PlaceIdentitySnapshot,
        observed: PlaceIdentitySnapshot,
    ) -> PlaceIdentityEvidence:
        distance = self._coordinate_distance(master, observed)
        return PlaceIdentityEvidence(
            master=master,
            observed=observed,
            name_score=self._text_similarity(master.name, observed.name),
            address_score=self._address_similarity(master.address, observed.address),
            city_score=self._city_score(master.city, observed.city),
            category_score=self._category_score(mapping.entity_type, observed.category),
            coordinate_score=self._coordinate_score(distance),
            coordinate_distance_m=distance,
        )

    def _hard_conflicts(
        self,
        mapping: ExternalEntityMapping,
        observation: GoogleMapsPlaceObservation,
        evidence: PlaceIdentityEvidence,
    ) -> tuple[str, ...]:
        reasons = []
        if mapping.entity_id != observation.place_id:
            reasons.append("MAPPING_ENTITY_CONFLICT")
        if mapping.source_id != observation.source_id:
            reasons.append("MAPPING_SOURCE_CONFLICT")
        if evidence.city_score == 0:
            reasons.append("CITY_CONFLICT")
        if (
            evidence.coordinate_distance_m is not None
            and evidence.coordinate_distance_m > self.policy.reject_min_distance_m
        ):
            reasons.append("COORDINATE_CONFLICT")
        if (
            evidence.name_score is not None
            and evidence.name_score < 0.12
            and evidence.address_score is not None
            and evidence.address_score < 0.15
            and evidence.category_score == 0
        ):
            reasons.append("MULTI_FIELD_IDENTITY_CONFLICT")
        return tuple(sorted(set(reasons)))

    def _can_auto_confirm(
        self, evidence: PlaceIdentityEvidence, score: float
    ) -> bool:
        distance = evidence.coordinate_distance_m
        secondary = max(
            value
            for value in (
                evidence.address_score or 0.0,
                evidence.category_score or 0.0,
            )
        )
        return bool(
            evidence.city_score == 1
            and distance is not None
            and distance <= self.policy.auto_confirm_max_distance_m
            and evidence.name_score is not None
            and evidence.name_score >= self.policy.auto_confirm_min_name_score
            and evidence.category_score == 1.0
            and secondary >= self.policy.secondary_evidence_min_score
            and score >= self.policy.auto_confirm_min_score
        )

    def _review_reasons(
        self, evidence: PlaceIdentityEvidence, score: float
    ) -> tuple[str, ...]:
        reasons = []
        if evidence.name_score is None:
            reasons.append("NAME_MISSING")
        elif evidence.name_score < self.policy.auto_confirm_min_name_score:
            reasons.append("NAME_AMBIGUOUS")
        if evidence.address_score is None:
            reasons.append("ADDRESS_EVIDENCE_MISSING")
        if evidence.city_score is None:
            reasons.append("CITY_EVIDENCE_MISSING")
        if evidence.category_score is None:
            reasons.append("CATEGORY_EVIDENCE_MISSING")
        elif evidence.category_score == 0:
            reasons.append("CATEGORY_CONFLICT_REVIEW")
        if evidence.coordinate_distance_m is None:
            reasons.append("COORDINATE_EVIDENCE_MISSING")
        elif (
            evidence.coordinate_distance_m
            > self.policy.auto_confirm_max_distance_m
        ):
            reasons.append("COORDINATE_AMBIGUOUS")
        if score < self.policy.auto_confirm_min_score:
            reasons.append("SCORE_BELOW_AUTO_CONFIRM")
        if not reasons:
            reasons.append("SECONDARY_EVIDENCE_WEAK")
        return tuple(sorted(set(reasons)))

    def _weighted_score(self, evidence: PlaceIdentityEvidence) -> float:
        values = {
            "name": evidence.name_score,
            "address": evidence.address_score,
            "city": evidence.city_score,
            "category": evidence.category_score,
            "coordinate": evidence.coordinate_score,
        }
        score = sum(self._weights[key] * (values[key] or 0.0) for key in self._weights)
        return round(score, 6)

    def _evidence_hash(
        self,
        mapping: ExternalEntityMapping,
        observation: GoogleMapsPlaceObservation,
        evidence: PlaceIdentityEvidence,
    ) -> str:
        content = {
            "resolver_version": self.resolver_version,
            "policy": self.policy.model_dump(mode="json"),
            "mapping": {
                "entity_id": mapping.entity_id,
                "entity_type": mapping.entity_type.value
                if mapping.entity_type
                else None,
                "source_id": mapping.source_id,
            },
            "observation_source_id": observation.source_id,
            "evidence": evidence.model_dump(mode="json"),
        }
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _detect_city(self, address: str | None, expected_city: str | None) -> str | None:
        normalized_address = self._normalize_text(address or "")
        if not normalized_address:
            return None
        candidates = list(self.known_cities)
        expected = self._normalize_text(expected_city or "")
        if expected and expected not in candidates:
            candidates.append(expected)
        matches = [city for city in candidates if self._phrase_in_text(city, normalized_address)]
        if expected in matches:
            return expected_city
        return matches[0] if matches else None

    @classmethod
    def _category_score(
        cls, expected: EntityType | None, observed_category: str | None
    ) -> float | None:
        if expected is None or not observed_category:
            return None
        observed = cls._normalize_text(observed_category)
        matched_types = {
            entity_type
            for entity_type, terms in cls._category_terms.items()
            if any(
                cls._phrase_in_text(cls._normalize_text(term), observed)
                for term in terms
            )
        }
        if expected in matched_types:
            return 1.0
        return 0.0 if matched_types else None

    @classmethod
    def _city_score(cls, expected: str | None, observed: str | None) -> float | None:
        if not expected or not observed:
            return None
        return 1.0 if cls._normalize_text(expected) == cls._normalize_text(observed) else 0.0

    @classmethod
    def _text_similarity(cls, left: str | None, right: str | None) -> float | None:
        if not left or not right:
            return None
        normalized_left = cls._normalize_text(left)
        normalized_right = cls._normalize_text(right)
        if normalized_left == normalized_right:
            return 1.0
        left_tokens = set(normalized_left.split())
        right_tokens = set(normalized_right.split())
        if not left_tokens or not right_tokens:
            return None
        dice = 2 * len(left_tokens & right_tokens) / (len(left_tokens) + len(right_tokens))
        sequence = SequenceMatcher(None, normalized_left, normalized_right).ratio()
        containment = 0.0
        if min(len(normalized_left), len(normalized_right)) >= 4 and (
            normalized_left in normalized_right or normalized_right in normalized_left
        ):
            containment = 0.9
        return round(max(dice, sequence, containment), 6)

    @classmethod
    def _address_similarity(
        cls, expected: str | None, observed: str | None
    ) -> float | None:
        if not expected or not observed:
            return None
        stop_words = {"viet", "nam", "vietnam"}
        left = set(cls._normalize_text(expected).split()) - stop_words
        right = set(cls._normalize_text(observed).split()) - stop_words
        if not left or not right:
            return None
        return round(2 * len(left & right) / (len(left) + len(right)), 6)

    def _coordinate_score(self, distance_m: float | None) -> float | None:
        if distance_m is None:
            return None
        auto_max = self.policy.auto_confirm_max_distance_m
        reject_min = self.policy.reject_min_distance_m
        if distance_m <= 100:
            return 1.0
        if distance_m <= auto_max:
            return round(1.0 - 0.15 * (distance_m - 100) / max(auto_max - 100, 1), 6)
        if distance_m <= reject_min:
            return round(0.7 * (reject_min - distance_m) / (reject_min - auto_max), 6)
        return 0.0

    @staticmethod
    def _coordinate_distance(
        master: PlaceIdentitySnapshot, observed: PlaceIdentitySnapshot
    ) -> float | None:
        if None in (
            master.latitude,
            master.longitude,
            observed.latitude,
            observed.longitude,
        ):
            return None
        lat1 = math.radians(float(master.latitude))
        lat2 = math.radians(float(observed.latitude))
        delta_lat = math.radians(float(observed.latitude) - float(master.latitude))
        delta_lon = math.radians(float(observed.longitude) - float(master.longitude))
        value = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        )
        return round(6371008.8 * 2 * math.asin(math.sqrt(value)), 3)

    @staticmethod
    def _normalize_text(value: str) -> str:
        value = value.casefold().replace("đ", "d")
        decomposed = unicodedata.normalize("NFKD", value)
        ascii_text = "".join(
            character for character in decomposed if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()

    @staticmethod
    def _phrase_in_text(phrase: str, text: str) -> bool:
        return bool(re.search(rf"(?:^|\s){re.escape(phrase)}(?:$|\s)", text))

    @staticmethod
    def _optional_text(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None


def apply_auto_confirmation(
    mapping: ExternalEntityMapping,
    observation: GoogleMapsPlaceObservation,
    resolution: GoogleMapsMappingResolution,
    *,
    canonical_url: str | None = None,
) -> ExternalEntityMapping:
    """Return a confirmed mapping only from a matching AUTO_CONFIRM result."""

    if resolution.status is not MappingResolutionStatus.AUTO_CONFIRM:
        raise ValueError("only AUTO_CONFIRM resolutions can confirm a mapping")
    if resolution.mapping_id != mapping.mapping_id:
        raise ValueError("resolution belongs to another mapping")
    if (
        resolution.observation_id != observation.observation_id
        or resolution.source_record_id != observation.source_record_id
        or resolution.place_id != observation.place_id
    ):
        raise ValueError("resolution belongs to another observation")
    if (
        mapping.entity_id != observation.place_id
        or mapping.source_id != observation.source_id
    ):
        raise ValueError("mapping and observation identity do not match")
    observed_name = (observation.name or "").strip()
    if not observed_name:
        raise ValueError("auto-confirmation requires an observed place name")
    stable_ids = GoogleMapsMappingResolver._stable_external_ids(
        mapping,
        observation,
        canonical_url=canonical_url,
    )
    if len(stable_ids) != 1:
        raise ValueError(
            "auto-confirmation requires exactly one stable Google external ID"
        )
    external_id = stable_ids[0]
    url_candidates = (canonical_url, observation.source_url, mapping.external_url)
    observed_url = next(
        (
            str(value)
            for value in url_candidates
            if google_maps_stable_external_ids(value) == (external_id,)
        ),
        None,
    )
    source_record_ids = list(
        dict.fromkeys([*mapping.source_record_ids, observation.source_record_id])
    )
    return ExternalEntityMapping.model_validate(
        {
            **mapping.model_dump(mode="python"),
            "external_id": external_id,
            "external_url": observed_url,
            "status": MappingStatus.CONFIRMED,
            "confidence": resolution.score,
            "verified_at": resolution.resolved_at,
            "last_checked_at": resolution.resolved_at,
            "source_record_ids": source_record_ids,
            "attributes": {
                **mapping.attributes,
                "google_place_name": observed_name,
                "canonical_google_maps_url": observed_url,
                "last_observed_address": observation.address,
                "last_observed_category": observation.category,
                "mapping_evidence_hash": resolution.evidence_hash,
                "mapping_resolver_version": resolution.resolver_version,
            },
        }
    )


class LLMReviewVerdict(StrEnum):
    SAME_PLACE = "same_place"
    DIFFERENT_PLACE = "different_place"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class LLMMappingReviewInput(ImmutableQualityModel):
    deterministic_score: float = Field(ge=0, le=1)
    deterministic_reason_codes: tuple[str, ...]
    evidence: PlaceIdentityEvidence


class LLMReviewResponseContract(ImmutableQualityModel):
    schema_version: Literal["1.0"] = "1.0"
    allowed_verdicts: tuple[
        Literal["same_place"],
        Literal["different_place"],
        Literal["insufficient_evidence"],
    ] = ("same_place", "different_place", "insufficient_evidence")
    rationale_max_characters: int = 1000


class LLMMappingReviewRequest(ImmutableQualityModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolver_version: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    place_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    instructions: tuple[str, ...]
    input: LLMMappingReviewInput
    response_contract: LLMReviewResponseContract = Field(
        default_factory=LLMReviewResponseContract
    )
    estimated_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    created_at: AwareDatetime


class LLMMappingReviewResponse(ImmutableQualityModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    verdict: LLMReviewVerdict
    confidence: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=1000)


class LLMReviewQueueConfig(ImmutableQualityModel):
    root_directory: Path
    max_requests_per_run: int = Field(default=25, gt=0)
    max_tokens_per_run: int = Field(default=25_000, gt=0)
    max_input_tokens_per_request: int = Field(default=1600, gt=0)
    max_output_tokens_per_request: int = Field(default=300, gt=0)


class LLMReviewQueueDisposition(StrEnum):
    QUEUED = "queued"
    CACHED = "cached"
    NOT_REVIEWABLE = "not_reviewable"
    REQUEST_TOKEN_LIMIT_EXCEEDED = "request_token_limit_exceeded"
    RUN_REQUEST_CAP_REACHED = "run_request_cap_reached"
    RUN_TOKEN_BUDGET_EXCEEDED = "run_token_budget_exceeded"


class LLMReviewQueueReceipt(ImmutableQualityModel):
    disposition: LLMReviewQueueDisposition
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str | None = None
    request_path: str | None = None
    estimated_input_tokens: int = Field(default=0, ge=0)
    reserved_total_tokens: int = Field(default=0, ge=0)
    run_request_count: int = Field(default=0, ge=0)
    run_reserved_tokens: int = Field(default=0, ge=0)


class LLMReviewRequestWriter:
    """Write content-addressed review requests without invoking an LLM."""

    _instructions = (
        "Decide only whether the master place and observed place are the same place.",
        "Use only the structured evidence in this request.",
        "Return insufficient_evidence when the evidence cannot support a decision.",
        "Do not override deterministic city or coordinate safety conflicts.",
        "Return a response that exactly follows the declared response contract.",
    )

    def __init__(
        self,
        config: LLMReviewQueueConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def enqueue(
        self,
        resolution: GoogleMapsMappingResolution,
        *,
        run_id: str,
    ) -> LLMReviewQueueReceipt:
        request_id = f"place-mapping-{resolution.evidence_hash}"
        path = self.request_path(resolution.evidence_hash)
        if resolution.status is not MappingResolutionStatus.REVIEW:
            return self._receipt(
                LLMReviewQueueDisposition.NOT_REVIEWABLE,
                resolution,
                request_id=None,
                path=None,
            )
        if path.exists():
            existing = self._read_request(path)
            if existing.evidence_hash != resolution.evidence_hash:
                raise ValueError(f"Cached request hash does not match its path: {path}")
            count, tokens = self._run_usage(run_id)
            return self._receipt(
                LLMReviewQueueDisposition.CACHED,
                resolution,
                request_id=existing.request_id,
                path=path,
                estimated_input_tokens=existing.estimated_input_tokens,
                reserved_total_tokens=(
                    existing.estimated_input_tokens + existing.max_output_tokens
                ),
                run_request_count=count,
                run_reserved_tokens=tokens,
            )
        input_value = LLMMappingReviewInput(
            deterministic_score=resolution.score,
            deterministic_reason_codes=resolution.reason_codes,
            evidence=resolution.evidence,
        )
        estimated_input_tokens = self._estimate_input_tokens(input_value)
        reserved = (
            estimated_input_tokens + self.config.max_output_tokens_per_request
        )
        count, tokens = self._run_usage(run_id)
        common = {
            "request_id": request_id,
            "path": path,
            "estimated_input_tokens": estimated_input_tokens,
            "reserved_total_tokens": reserved,
            "run_request_count": count,
            "run_reserved_tokens": tokens,
        }
        if estimated_input_tokens > self.config.max_input_tokens_per_request:
            return self._receipt(
                LLMReviewQueueDisposition.REQUEST_TOKEN_LIMIT_EXCEEDED,
                resolution,
                **common,
            )
        if count >= self.config.max_requests_per_run:
            return self._receipt(
                LLMReviewQueueDisposition.RUN_REQUEST_CAP_REACHED,
                resolution,
                **common,
            )
        if tokens + reserved > self.config.max_tokens_per_run:
            return self._receipt(
                LLMReviewQueueDisposition.RUN_TOKEN_BUDGET_EXCEEDED,
                resolution,
                **common,
            )
        request = LLMMappingReviewRequest(
            request_id=request_id,
            run_id=run_id,
            evidence_hash=resolution.evidence_hash,
            resolver_version=resolution.resolver_version,
            mapping_id=resolution.mapping_id,
            place_id=resolution.place_id,
            observation_id=resolution.observation_id,
            source_record_id=resolution.source_record_id,
            instructions=self._instructions,
            input=input_value,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=self.config.max_output_tokens_per_request,
            created_at=self.clock(),
        )
        try:
            self._write_once(path, request.model_dump_json(indent=2) + "\n")
            disposition = LLMReviewQueueDisposition.QUEUED
        except FileExistsError:
            existing = self._read_request(path)
            if existing.evidence_hash != resolution.evidence_hash:
                raise ValueError(f"Cached request hash does not match its path: {path}")
            disposition = LLMReviewQueueDisposition.CACHED
        new_count, new_tokens = self._run_usage(run_id)
        return self._receipt(
            disposition,
            resolution,
            request_id=request_id,
            path=path,
            estimated_input_tokens=estimated_input_tokens,
            reserved_total_tokens=reserved,
            run_request_count=new_count,
            run_reserved_tokens=new_tokens,
        )

    def request_path(self, evidence_hash: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", evidence_hash):
            raise ValueError("evidence_hash must be a lowercase SHA-256 value")
        return self.config.root_directory / "requests" / f"evidence={evidence_hash}.json"

    def _estimate_input_tokens(self, input_value: LLMMappingReviewInput) -> int:
        payload = {
            "instructions": self._instructions,
            "input": input_value.model_dump(mode="json"),
            "response_contract": LLMReviewResponseContract().model_dump(mode="json"),
        }
        characters = len(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        return max(1, math.ceil(characters / 4))

    def _run_usage(self, run_id: str) -> tuple[int, int]:
        count = 0
        tokens = 0
        directory = self.config.root_directory / "requests"
        if not directory.exists():
            return count, tokens
        for path in directory.glob("evidence=*.json"):
            request = self._read_request(path)
            if request.run_id == run_id:
                count += 1
                tokens += request.estimated_input_tokens + request.max_output_tokens
        return count, tokens

    @staticmethod
    def _read_request(path: Path) -> LLMMappingReviewRequest:
        return LLMMappingReviewRequest.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    @staticmethod
    def _write_once(destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Immutable LLM review request already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

    @staticmethod
    def _receipt(
        disposition: LLMReviewQueueDisposition,
        resolution: GoogleMapsMappingResolution,
        *,
        request_id: str | None,
        path: Path | None,
        estimated_input_tokens: int = 0,
        reserved_total_tokens: int = 0,
        run_request_count: int = 0,
        run_reserved_tokens: int = 0,
    ) -> LLMReviewQueueReceipt:
        return LLMReviewQueueReceipt(
            disposition=disposition,
            evidence_hash=resolution.evidence_hash,
            request_id=request_id,
            request_path=str(path) if path else None,
            estimated_input_tokens=estimated_input_tokens,
            reserved_total_tokens=reserved_total_tokens,
            run_request_count=run_request_count,
            run_reserved_tokens=run_reserved_tokens,
        )
