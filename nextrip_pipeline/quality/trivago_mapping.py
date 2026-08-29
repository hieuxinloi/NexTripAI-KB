from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
    SourceRecord,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock


class TrivagoDiscoveryStatus(StrEnum):
    CONFIRMED = "confirmed"
    REVIEW = "review"
    REJECTED = "rejected"
    MISSING = "missing"


class TrivagoCandidateEvidence(NexTripModel):
    external_id: str | None = None
    name: str | None = None
    location_text: str | None = None
    external_url: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    distance_from_master_m: float | None = Field(default=None, ge=0)
    name_score: float = Field(ge=0, le=1)
    city_evidence: str = Field(pattern=r"^(match|conflict|missing)$")


class TrivagoDiscoveryResolution(NexTripModel):
    entity_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    status: TrivagoDiscoveryStatus
    resolved_at: AwareDatetime
    selected_external_id: str | None = None
    selected_external_url: str | None = None
    # Optional so immutable resolution JSON written before canonical-name
    # propagation remains readable.
    selected_name: str | None = None
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(min_length=1)
    returned_candidate_count: int = Field(ge=0)
    candidates: list[TrivagoCandidateEvidence] = Field(default_factory=list)
    resolver_version: str = "1.2.0"
    review_target_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def compute_trivago_resolution_evidence_hash(
    resolution: TrivagoDiscoveryResolution,
) -> str:
    """Recompute the resolver-owned evidence digest from a stored resolution."""

    evidence: dict[str, object] = {
        "entity_id": resolution.entity_id,
        "source_record_id": resolution.source_record_id,
        "status": resolution.status.value,
        "selected_external_id": resolution.selected_external_id,
        "selected_name": resolution.selected_name,
        "reason_codes": resolution.reason_codes,
        "candidates": [
            candidate.model_dump(mode="json") for candidate in resolution.candidates
        ],
        "resolver_version": resolution.resolver_version,
    }
    # Resolver 1.3 introduced human approval of a selected provider URL. Bind
    # every decision field consumed by that approval to the resolver evidence
    # hash. Keep the legacy payload byte-for-byte compatible so immutable
    # resolutions and approvals produced by resolver <=1.2 remain readable.
    if _uses_decision_bound_evidence_hash(resolution.resolver_version):
        evidence.update(
            {
                "selected_external_url": resolution.selected_external_url,
                "confidence": resolution.confidence,
                "returned_candidate_count": resolution.returned_candidate_count,
            }
        )
    if resolution.review_target_hash is not None:
        evidence["review_target_hash"] = resolution.review_target_hash
    return _stable_sha256(evidence)


def _uses_decision_bound_evidence_hash(resolver_version: str) -> bool:
    """Return whether a resolver version uses the 1.3 evidence contract."""

    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", resolver_version)
    if match is None:
        # Unknown legacy/custom version strings predate the 1.3 contract.
        return False
    version = tuple(int(value or 0) for value in match.groups())
    return version >= (1, 3, 0)


def _stable_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class TrivagoDiscoveryResolver:
    """Resolve Trivago IDs conservatively from name and city evidence."""

    version = "1.4.0"
    plausible_name_score = 0.72
    strong_name_score = 0.94

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def resolve(
        self,
        entry: TrivagoHotelRegistryEntry,
        record: SourceRecord,
    ) -> TrivagoDiscoveryResolution:
        if record.source_id != "trivago-mcp" or record.subject_id != entry.entity_id:
            raise ValueError(
                "Trivago discovery record provenance does not match target"
            )
        raw_candidates, accommodations_present = self._raw_candidates(record)
        candidates = [self._candidate(entry, value) for value in raw_candidates]

        known_match = None
        if entry.external_id:
            known_match = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.external_id == entry.external_id
                ),
                None,
            )
        if known_match is not None:
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.CONFIRMED,
                known_match,
                1.0,
                ["confirmed_external_id_returned"],
                candidates,
                len(raw_candidates),
            )

        reviewed_target = self._reviewed_target_match(entry, candidates)
        if reviewed_target is not None:
            if not self._review_target_request_is_current(entry, record):
                return self._resolution(
                    entry,
                    record,
                    TrivagoDiscoveryStatus.REVIEW,
                    reviewed_target,
                    0.99,
                    [
                        "reviewed_search_target_returned",
                        "review_target_request_hash_mismatch",
                        "fresh_review_target_evidence_required",
                    ],
                    candidates,
                    len(raw_candidates),
                )
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.CONFIRMED,
                reviewed_target,
                1.0,
                [
                    "reviewed_search_target_returned",
                    "reviewed_external_id_match",
                    "reviewed_property_id_match",
                    "reviewed_provider_name_match",
                    "city_match",
                ],
                candidates,
                len(raw_candidates),
            )

        if (
            entry.status is TrivagoRegistryStatus.CONFIRMED
            and entry.external_id
            and raw_candidates
        ):
            known_property_id = (
                _trivago_property_id(str(entry.external_url))
                if entry.external_url is not None
                else None
            )
            stable_property_matches = [
                candidate
                for candidate in candidates
                if known_property_id is not None
                and candidate.external_id is not None
                and candidate.external_url is not None
                and _trivago_property_id(candidate.external_url) == known_property_id
            ]
            replacement_external_ids = {
                candidate.external_id for candidate in stable_property_matches
            }
            if len(replacement_external_ids) == 1:
                possible_replacement = max(
                    stable_property_matches,
                    key=lambda candidate: candidate.name_score,
                )
                return self._resolution(
                    entry,
                    record,
                    TrivagoDiscoveryStatus.REVIEW,
                    possible_replacement,
                    possible_replacement.name_score,
                    [
                        "confirmed_external_id_not_returned",
                        "stable_property_id_supports_external_id_change",
                        "external_id_change_requires_review",
                    ],
                    candidates,
                    len(raw_candidates),
                )
            # Search ranking and stay-date inventory can omit a confirmed
            # listing while returning similarly named properties.  That is
            # not evidence that Trivago changed the hotel's external ID.  Keep
            # every candidate in the immutable audit, but never nominate one
            # as a replacement without an explicitly reviewed target.
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.MISSING,
                None,
                0,
                [
                    "confirmed_external_id_not_returned",
                    "returned_candidates_do_not_prove_identity_change",
                ],
                candidates,
                len(raw_candidates),
            )

        if not raw_candidates:
            reasons = [
                "confirmed_external_id_not_returned"
                if entry.external_id
                else (
                    "no_accommodations_returned"
                    if accommodations_present
                    else "accommodations_missing_from_response"
                )
            ]
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.MISSING,
                None,
                0,
                reasons,
                candidates,
                0,
            )

        viable = [
            candidate
            for candidate in candidates
            if candidate.external_id and candidate.name
        ]
        if not viable:
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                None,
                0,
                ["candidate_identity_fields_missing"],
                candidates,
                len(raw_candidates),
            )

        strong = [
            candidate
            for candidate in viable
            if self._is_exact_name_variant(entry.master_name, candidate.name)
            and candidate.city_evidence == "match"
        ]
        if len(strong) == 1:
            selected = strong[0]
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.CONFIRMED,
                selected,
                round(0.75 * selected.name_score + 0.25, 6),
                ["unique_strong_name", "city_match"],
                candidates,
                len(raw_candidates),
            )
        if len(strong) > 1:
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                None,
                max(candidate.name_score for candidate in strong),
                ["multiple_strong_candidates"],
                candidates,
                len(raw_candidates),
            )

        distinctive = [
            candidate
            for candidate in viable
            if candidate.city_evidence == "match"
            and self._has_distinctive_name_identity(entry, candidate)
            and candidate.distance_from_master_m is not None
            and candidate.distance_from_master_m <= 500
        ]
        if len(distinctive) == 1:
            selected = distinctive[0]
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.CONFIRMED,
                selected,
                round(0.8 * selected.name_score + 0.2, 6),
                ["unique_distinctive_name", "city_match"],
                candidates,
                len(raw_candidates),
            )
        if len(distinctive) > 1:
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                None,
                max(candidate.name_score for candidate in distinctive),
                ["multiple_distinctive_name_candidates"],
                candidates,
                len(raw_candidates),
            )

        # Coordinates alone are not a hotel identity in dense tourist areas.
        # A nearby property with a completely different name must remain in
        # review until another provider-owned identifier or human evidence
        # proves a rebrand.
        geo_candidates = self._geocoded_candidates(viable)
        if (
            geo_candidates
            and geo_candidates[0].distance_from_master_m is not None
            and geo_candidates[0].distance_from_master_m <= 50
            and not self._is_exact_name_variant(
                entry.master_name, geo_candidates[0].name
            )
        ):
            selected = geo_candidates[0]
            reasons = ["provider_name_differs", "geo_identity_requires_review"]
            if selected.city_evidence != "match":
                reasons.append("city_match_required_for_geo_confirmation")
            if len(geo_candidates) > 1:
                second_distance = geo_candidates[1].distance_from_master_m
                if second_distance is not None and second_distance <= 100:
                    reasons.append("nearby_geocoded_candidates_ambiguous")
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                selected,
                self._geo_confidence(selected),
                reasons,
                candidates,
                len(raw_candidates),
            )

        plausible = [
            candidate
            for candidate in viable
            if candidate.name_score >= self.plausible_name_score
        ]
        if plausible:
            selected = max(plausible, key=lambda candidate: candidate.name_score)
            reasons = ["candidate_requires_review"]
            if selected.city_evidence == "missing":
                reasons.append("city_evidence_missing")
            elif selected.city_evidence == "conflict":
                reasons.append("city_conflict")
            if not self._is_exact_name_variant(entry.master_name, selected.name):
                reasons.append("provider_name_differs")
            elif selected.name_score < self.strong_name_score:
                reasons.append("name_not_exact_enough")
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                selected,
                selected.name_score,
                reasons,
                candidates,
                len(raw_candidates),
            )

        different_name_candidates = [
            candidate
            for candidate in viable
            if not self._is_exact_name_variant(entry.master_name, candidate.name)
            and candidate.city_evidence != "conflict"
        ]
        if different_name_candidates:
            selected = self._review_candidate(different_name_candidates)
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                selected,
                selected.name_score,
                ["provider_name_differs", "strong_geo_identity_missing"],
                candidates,
                len(raw_candidates),
            )

        explicit_conflicts = [
            candidate for candidate in viable if candidate.city_evidence == "conflict"
        ]
        reasons = (
            ["no_plausible_name", "explicit_city_conflict"]
            if explicit_conflicts
            else ["no_plausible_name"]
        )
        return self._resolution(
            entry,
            record,
            TrivagoDiscoveryStatus.REJECTED,
            None,
            max(candidate.name_score for candidate in viable),
            reasons,
            candidates,
            len(raw_candidates),
        )

    @staticmethod
    def _raw_candidates(
        record: SourceRecord,
    ) -> tuple[list[dict[str, object]], bool]:
        response = record.raw_payload.get("response")
        if not isinstance(response, dict):
            return [], False
        result = response.get("result")
        if not isinstance(result, dict):
            return [], False
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            return [], False
        values = structured.get("accommodations")
        if not isinstance(values, list):
            return [], False
        return [value for value in values if isinstance(value, dict)], True

    def _candidate(
        self,
        entry: TrivagoHotelRegistryEntry,
        value: dict[str, object],
    ) -> TrivagoCandidateEvidence:
        external_id = self._text(value.get("accommodation_id"))
        name = self._text(value.get("accommodation_name") or value.get("name"))
        external_url = self._text(value.get("accommodation_url") or value.get("url"))
        latitude = self._coordinate(
            value.get("latitude")
            if value.get("latitude") is not None
            else value.get("lat"),
            -90,
            90,
        )
        longitude = self._coordinate(
            value.get("longitude")
            if value.get("longitude") is not None
            else value.get("lng"),
            -180,
            180,
        )
        distance_from_master_m = None
        if (
            entry.latitude is not None
            and entry.longitude is not None
            and latitude is not None
            and longitude is not None
        ):
            distance_from_master_m = round(
                self._distance_metres(
                    entry.latitude,
                    entry.longitude,
                    latitude,
                    longitude,
                ),
                3,
            )
        explicit_locations = [
            text
            for field in ("country_city", "city", "destination", "locality")
            if (text := self._text(value.get(field))) is not None
        ]
        address_locations = [
            text
            for field in ("location", "address")
            if (text := self._text(value.get(field))) is not None
        ]
        all_locations = [*explicit_locations, *address_locations]
        location_text = " | ".join(all_locations) or None
        expected_city = self._key(entry.city)
        if any(expected_city in self._key(location) for location in all_locations):
            city_evidence = "match"
        elif explicit_locations:
            city_evidence = "conflict"
        else:
            city_evidence = "missing"
        return TrivagoCandidateEvidence(
            external_id=external_id,
            name=name,
            location_text=location_text,
            external_url=external_url,
            latitude=latitude,
            longitude=longitude,
            distance_from_master_m=distance_from_master_m,
            name_score=self._name_score(entry.master_name, name),
            city_evidence=city_evidence,
        )

    @staticmethod
    def _coordinate(value: object, minimum: float, maximum: float) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            coordinate = float(value) if isinstance(value, (int, float, str)) else None
        except ValueError:
            return None
        if (
            coordinate is None
            or not math.isfinite(coordinate)
            or coordinate < minimum
            or coordinate > maximum
        ):
            return None
        return coordinate

    @staticmethod
    def _distance_metres(
        latitude_a: float,
        longitude_a: float,
        latitude_b: float,
        longitude_b: float,
    ) -> float:
        radius_m = 6_371_008.8
        latitude_a_rad = math.radians(latitude_a)
        latitude_b_rad = math.radians(latitude_b)
        latitude_delta = latitude_b_rad - latitude_a_rad
        longitude_delta = math.radians(longitude_b - longitude_a)
        haversine = (
            math.sin(latitude_delta / 2) ** 2
            + math.cos(latitude_a_rad)
            * math.cos(latitude_b_rad)
            * math.sin(longitude_delta / 2) ** 2
        )
        return 2 * radius_m * math.asin(min(1.0, math.sqrt(haversine)))

    @staticmethod
    def _geocoded_candidates(
        candidates: list[TrivagoCandidateEvidence],
    ) -> list[TrivagoCandidateEvidence]:
        # One accommodation can be repeated by offer/seller. Treat an external
        # accommodation ID as one identity when assessing nearby competitors.
        by_external_id: dict[str, TrivagoCandidateEvidence] = {}
        for candidate in candidates:
            if (
                candidate.external_id is None
                or candidate.distance_from_master_m is None
            ):
                continue
            current = by_external_id.get(candidate.external_id)
            if (
                current is None
                or current.distance_from_master_m is None
                or candidate.distance_from_master_m < current.distance_from_master_m
            ):
                by_external_id[candidate.external_id] = candidate
        return sorted(
            by_external_id.values(),
            key=lambda candidate: (
                candidate.distance_from_master_m
                if candidate.distance_from_master_m is not None
                else math.inf,
                candidate.external_id or "",
            ),
        )

    @staticmethod
    def _strong_geo_identity(
        candidates: list[TrivagoCandidateEvidence],
    ) -> TrivagoCandidateEvidence | None:
        if not candidates:
            return None
        closest = candidates[0]
        if (
            closest.distance_from_master_m is None
            or closest.distance_from_master_m > 50
            or closest.city_evidence != "match"
        ):
            return None
        if len(candidates) == 1:
            return closest
        second_distance = candidates[1].distance_from_master_m
        return (
            closest if second_distance is not None and second_distance > 100 else None
        )

    @staticmethod
    def _geo_confidence(candidate: TrivagoCandidateEvidence) -> float:
        distance = candidate.distance_from_master_m
        if distance is None:
            return 0
        return round(max(0.94, 0.99 - min(distance, 50) / 1000), 6)

    @staticmethod
    def _review_candidate(
        candidates: list[TrivagoCandidateEvidence],
    ) -> TrivagoCandidateEvidence:
        geocoded = [
            candidate
            for candidate in candidates
            if candidate.distance_from_master_m is not None
        ]
        if geocoded:
            return min(
                geocoded,
                key=lambda candidate: candidate.distance_from_master_m or 0,
            )
        return max(candidates, key=lambda candidate: candidate.name_score)

    def _name_score(self, master_name: str, candidate_name: str | None) -> float:
        if not candidate_name:
            return 0
        candidate_key = self._key(candidate_name)
        if not candidate_key:
            return 0
        variants = self._name_variants(master_name)
        return round(
            max(
                SequenceMatcher(None, variant, candidate_key).ratio()
                for variant in variants
            ),
            6,
        )

    def _name_variants(self, name: str) -> set[str]:
        values = {self._key(name)}
        for part in re.split(r"[()/|]", name):
            normalized = self._key(part)
            if len(normalized) >= 5:
                values.add(normalized)
        return values

    def _is_exact_name_variant(
        self, master_name: str, candidate_name: str | None
    ) -> bool:
        if not candidate_name:
            return False
        candidate_key = self._key(candidate_name)
        return bool(candidate_key) and candidate_key in self._name_variants(master_name)

    def _has_distinctive_name_identity(
        self,
        entry: TrivagoHotelRegistryEntry,
        candidate: TrivagoCandidateEvidence,
    ) -> bool:
        if candidate.name is None:
            return False
        master_tokens = self._distinctive_tokens(entry.master_name, entry.city)
        candidate_tokens = self._distinctive_tokens(candidate.name, entry.city)
        if not master_tokens or not candidate_tokens:
            return False
        shared = master_tokens & candidate_tokens
        if shared != master_tokens:
            return False
        # One long provider brand token (for example "furama") is sufficient;
        # otherwise require at least two distinctive tokens to avoid matching
        # generic nearby properties.
        return len(shared) >= 2 or any(len(token) >= 6 for token in shared)

    @classmethod
    def _distinctive_tokens(cls, value: str, city: str) -> set[str]:
        generic = {
            "accommodation",
            "apartments",
            "apartment",
            "beachfront",
            "boutique",
            "danang",
            "hotel",
            "hotels",
            "homestay",
            "hostel",
            "khach",
            "nang",
            "nhon",
            "resort",
            "san",
            "vietnam",
            "villa",
            "villas",
        }
        generic.update(cls._key(city).split())
        return {
            token
            for token in cls._key(value).split()
            if len(token) >= 3 and token not in generic
        }

    @staticmethod
    def _text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _reviewed_target_match(
        self,
        entry: TrivagoHotelRegistryEntry,
        candidates: list[TrivagoCandidateEvidence],
    ) -> TrivagoCandidateEvidence | None:
        if (
            entry.status is TrivagoRegistryStatus.CONFIRMED
            or not entry.review_target_external_id
            or not entry.review_target_property_id
            or not entry.review_target_name
            or not entry.review_target_reviewer
            or entry.review_target_reviewed_at is None
            or not entry.review_target_reason
            or not entry.review_target_hash
            or not entry.review_target_evidence
        ):
            return None
        matches = [
            candidate
            for candidate in candidates
            if candidate.external_id == entry.review_target_external_id
            and candidate.external_url is not None
            and _trivago_property_id(candidate.external_url)
            == entry.review_target_property_id
            and candidate.name is not None
            and self._key(candidate.name) == self._key(entry.review_target_name)
            and candidate.city_evidence == "match"
            and candidate.distance_from_master_m is not None
            and candidate.distance_from_master_m <= 500
        ]
        logical_matches: dict[tuple[str, str, str], TrivagoCandidateEvidence] = {}
        for candidate in matches:
            assert candidate.external_id is not None
            assert candidate.external_url is not None
            assert candidate.name is not None
            property_id = _trivago_property_id(candidate.external_url)
            assert property_id is not None
            key = (candidate.external_id, property_id, self._key(candidate.name))
            current = logical_matches.get(key)
            if (
                current is None
                or current.distance_from_master_m is None
                or (
                    candidate.distance_from_master_m is not None
                    and candidate.distance_from_master_m
                    < current.distance_from_master_m
                )
            ):
                logical_matches[key] = candidate
        return (
            next(iter(logical_matches.values())) if len(logical_matches) == 1 else None
        )

    @staticmethod
    def _review_target_request_is_current(
        entry: TrivagoHotelRegistryEntry,
        record: SourceRecord,
    ) -> bool:
        """Bind reviewed-target confirmation to the registry used at capture."""

        request = record.raw_payload.get("request")
        return (
            isinstance(request, dict)
            and entry.review_target_hash is not None
            and request.get("review_target_hash") == entry.review_target_hash
        )

    @staticmethod
    def _key(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        ascii_text = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()

    def _resolution(
        self,
        entry: TrivagoHotelRegistryEntry,
        record: SourceRecord,
        status: TrivagoDiscoveryStatus,
        selected: TrivagoCandidateEvidence | None,
        confidence: float,
        reasons: list[str],
        candidates: list[TrivagoCandidateEvidence],
        returned_count: int,
    ) -> TrivagoDiscoveryResolution:
        evidence = {
            "entity_id": entry.entity_id,
            "source_record_id": record.source_record_id,
            "status": status.value,
            "selected_external_id": selected.external_id if selected else None,
            "selected_name": selected.name if selected else None,
            "reason_codes": reasons,
            "candidates": [
                candidate.model_dump(mode="json") for candidate in candidates
            ],
            "resolver_version": self.version,
        }
        if _uses_decision_bound_evidence_hash(self.version):
            evidence.update(
                {
                    "selected_external_url": (
                        selected.external_url if selected else None
                    ),
                    "confidence": confidence,
                    "returned_candidate_count": returned_count,
                }
            )
        if entry.review_target_hash is not None:
            evidence["review_target_hash"] = entry.review_target_hash
        evidence_hash = _stable_sha256(evidence)
        return TrivagoDiscoveryResolution(
            entity_id=entry.entity_id,
            source_record_id=record.source_record_id,
            status=status,
            resolved_at=self.clock(),
            selected_external_id=selected.external_id if selected else None,
            selected_external_url=selected.external_url if selected else None,
            selected_name=selected.name if selected else None,
            confidence=confidence,
            reason_codes=reasons,
            returned_candidate_count=returned_count,
            candidates=candidates,
            resolver_version=self.version,
            review_target_hash=entry.review_target_hash,
            evidence_hash=evidence_hash,
        )


def apply_trivago_resolution(
    entry: TrivagoHotelRegistryEntry,
    resolution: TrivagoDiscoveryResolution,
) -> TrivagoHotelRegistryEntry:
    if resolution.entity_id != entry.entity_id:
        raise ValueError("resolution belongs to another hotel")
    source_records = list(
        dict.fromkeys([*entry.source_record_ids, resolution.source_record_id])
    )
    if resolution.status is TrivagoDiscoveryStatus.CONFIRMED:
        if not resolution.selected_external_id:
            raise ValueError("confirmed resolution requires selected_external_id")
        return TrivagoHotelRegistryEntry.model_validate(
            {
                **entry.model_dump(mode="python"),
                "status": TrivagoRegistryStatus.CONFIRMED,
                "external_id": resolution.selected_external_id,
                "external_url": resolution.selected_external_url or entry.external_url,
                "trivago_name": resolution.selected_name or entry.trivago_name,
                "confidence": resolution.confidence,
                "matched_at": entry.matched_at or resolution.resolved_at,
                "verified_at": resolution.resolved_at,
                "source_record_ids": source_records,
            }
        )
    if entry.status is TrivagoRegistryStatus.CONFIRMED:
        return TrivagoHotelRegistryEntry.model_validate(
            {
                **entry.model_dump(mode="python"),
                "source_record_ids": source_records,
            }
        )
    status = {
        TrivagoDiscoveryStatus.REVIEW: TrivagoRegistryStatus.REVIEW,
        TrivagoDiscoveryStatus.REJECTED: TrivagoRegistryStatus.REJECTED,
        TrivagoDiscoveryStatus.MISSING: TrivagoRegistryStatus.UNRESOLVED,
    }[resolution.status]
    return TrivagoHotelRegistryEntry.model_validate(
        {
            **entry.model_dump(mode="python"),
            "status": status,
            "source_record_ids": source_records,
        }
    )


class TrivagoDiscoveryAuditWriter:
    """Write immutable discovery evidence partitioned by run and hotel."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, resolution: TrivagoDiscoveryResolution, *, run_id: str) -> Path:
        destination = (
            self.root_directory
            / f"run={quote(run_id, safe='-_.')}"
            / f"hotel={quote(resolution.entity_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Trivago discovery audit already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(resolution.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination


class CurrentTrivagoMappingWriter:
    """Atomically materialize confirmed Trivago mappings (not hotel prices)."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)
        self._index_lock_destination = self.root_directory / "mapping-index"

    def publish(self, mapping: ExternalEntityMapping) -> Path:
        if (
            mapping.source_id != "trivago-mcp"
            or mapping.status is not MappingStatus.CONFIRMED
            or mapping.verified_at is None
            or mapping.last_checked_at is None
        ):
            raise ValueError("current Trivago mapping must be confirmed and verified")
        with destination_file_lock(self._index_lock_destination):
            return self._publish_locked(mapping)

    def _publish_locked(self, mapping: ExternalEntityMapping) -> Path:
        self._require_unique_external_id(mapping)
        destination = self.path_for(mapping.entity_id)
        current = self.get(mapping.entity_id)
        if current is not None:
            assert current.last_checked_at is not None
            if current.last_checked_at > mapping.last_checked_at:
                raise ValueError("older Trivago mapping cannot replace current mapping")
            if (
                current.last_checked_at == mapping.last_checked_at
                and current.external_id != mapping.external_id
            ):
                raise ValueError(
                    "equal-time Trivago mappings cannot change external_id"
                )
            if current.external_id == mapping.external_id:
                # A scheduled provider refresh may verify the same identity
                # again, but it must not erase human-approval/rebind history
                # that is intentionally absent from the derived registry
                # entry. Preserve current provenance while allowing fresh
                # provider-owned fields to win.
                mapping = ExternalEntityMapping.model_validate(
                    {
                        **mapping.model_dump(mode="python"),
                        "matched_at": current.matched_at,
                        "source_record_ids": list(
                            dict.fromkeys(
                                [
                                    *current.source_record_ids,
                                    *mapping.source_record_ids,
                                ]
                            )
                        ),
                        "attributes": {
                            **current.attributes,
                            **mapping.attributes,
                        },
                    }
                )
            if current.model_dump(mode="json") == mapping.model_dump(mode="json"):
                return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(mapping.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def _require_unique_external_id(self, incoming: ExternalEntityMapping) -> None:
        owner_by_external_id: dict[str, str] = {}
        owner_by_property_id: dict[str, str] = {}
        for mapping in self.all():
            owner = owner_by_external_id.setdefault(
                mapping.external_id, mapping.entity_id
            )
            if owner != mapping.entity_id:
                raise ValueError(
                    "duplicate current Trivago external_id "
                    f"{mapping.external_id}: {owner}, {mapping.entity_id}"
                )
            if mapping.external_url is not None:
                property_id = _trivago_property_id(str(mapping.external_url))
                if property_id is not None:
                    property_owner = owner_by_property_id.setdefault(
                        property_id, mapping.entity_id
                    )
                    if property_owner != mapping.entity_id:
                        raise ValueError(
                            "duplicate current Trivago property_id "
                            f"{property_id}: {property_owner}, {mapping.entity_id}"
                        )
        owner = owner_by_external_id.get(incoming.external_id)
        if owner is not None and owner != incoming.entity_id:
            raise ValueError(
                "Trivago external_id already belongs to another hotel: "
                f"{incoming.external_id} -> {owner}"
            )
        if incoming.external_url is not None:
            property_id = _trivago_property_id(str(incoming.external_url))
            property_owner = (
                owner_by_property_id.get(property_id)
                if property_id is not None
                else None
            )
            if property_owner is not None and property_owner != incoming.entity_id:
                raise ValueError(
                    "Trivago property_id already belongs to another hotel: "
                    f"{property_id} -> {property_owner}"
                )

    def all(self) -> list[ExternalEntityMapping]:
        if not self.root_directory.exists():
            return []
        return [
            ExternalEntityMapping.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root_directory.glob("*.json"))
        ]

    def get(self, entity_id: str) -> ExternalEntityMapping | None:
        path = self.path_for(entity_id)
        if not path.exists():
            return None
        return ExternalEntityMapping.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def path_for(self, entity_id: str) -> Path:
        return self.root_directory / f"{quote(entity_id, safe='-_.')}.json"


class TrivagoMappingApprovalError(ValueError):
    """Raised when REVIEW evidence cannot safely become a current mapping."""


def _approval_payload(
    *,
    schema_version: str,
    entity_id: str,
    external_id: str,
    external_url: str,
    trivago_name: str,
    reviewer: str,
    approved_at: datetime,
    source_record_id: str,
    resolved_at: datetime,
    resolver_version: str,
    confidence: float,
    reason_codes: list[str],
    resolution_path: str,
    resolution_file_sha256: str,
    resolution_evidence_hash: str,
    registry_path: str,
    registry_file_sha256: str,
    registry_entry_hash: str,
    previous_external_id: str | None,
    previous_mapping_file_sha256: str | None,
    external_id_change_approved: bool,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "entity_id": entity_id,
        "source_id": "trivago-mcp",
        "external_id": external_id,
        # Canonicalize Unicode path/query characters before hashing. Pydantic
        # stores ``HttpUrl`` in percent-encoded form, so hashing the raw MCP
        # string would otherwise make the freshly built approval fail its own
        # content-hash validation for Vietnamese Trivago URLs.
        "external_url": str(HttpUrl(external_url)),
        "trivago_name": trivago_name,
        "reviewer": reviewer,
        "approved_at": approved_at.isoformat(),
        "source_record_id": source_record_id,
        "resolved_at": resolved_at.isoformat(),
        "resolver_version": resolver_version,
        "confidence": confidence,
        "reason_codes": reason_codes,
        "resolution_path": resolution_path,
        "resolution_file_sha256": resolution_file_sha256,
        "resolution_evidence_hash": resolution_evidence_hash,
        "registry_path": registry_path,
        "registry_file_sha256": registry_file_sha256,
        "registry_entry_hash": registry_entry_hash,
        "previous_external_id": previous_external_id,
        "previous_mapping_file_sha256": previous_mapping_file_sha256,
        "external_id_change_approved": external_id_change_approved,
    }


class TrivagoMappingApproval(NexTripModel):
    """Content-addressed human approval pinned to REVIEW and registry bytes."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    entity_id: str = Field(min_length=1)
    source_id: Literal["trivago-mcp"] = "trivago-mcp"
    external_id: str = Field(min_length=1)
    external_url: HttpUrl
    trivago_name: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime
    source_record_id: str = Field(min_length=1)
    resolved_at: AwareDatetime
    resolver_version: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(min_length=1)
    resolution_path: str = Field(min_length=1)
    resolution_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolution_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_path: str = Field(min_length=1)
    registry_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registry_entry_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_external_id: str | None = None
    previous_mapping_file_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    external_id_change_approved: bool = False

    @model_validator(mode="after")
    def validate_approval(self) -> TrivagoMappingApproval:
        changed = (
            self.previous_external_id is not None
            and self.previous_external_id != self.external_id
        )
        if changed != self.external_id_change_approved:
            raise ValueError(
                "external_id_change_approved must exactly describe an ID change"
            )
        if (
            self.previous_mapping_file_sha256 is not None
            and self.previous_external_id is None
        ):
            raise ValueError("previous mapping hash requires a previous external_id")
        if self.approved_at < self.resolved_at:
            raise ValueError("approved_at cannot precede Trivago resolution")
        payload = _approval_payload(
            schema_version=self.schema_version,
            entity_id=self.entity_id,
            external_id=self.external_id,
            external_url=str(self.external_url),
            trivago_name=self.trivago_name,
            reviewer=self.reviewer,
            approved_at=self.approved_at,
            source_record_id=self.source_record_id,
            resolved_at=self.resolved_at,
            resolver_version=self.resolver_version,
            confidence=self.confidence,
            reason_codes=self.reason_codes,
            resolution_path=self.resolution_path,
            resolution_file_sha256=self.resolution_file_sha256,
            resolution_evidence_hash=self.resolution_evidence_hash,
            registry_path=self.registry_path,
            registry_file_sha256=self.registry_file_sha256,
            registry_entry_hash=self.registry_entry_hash,
            previous_external_id=self.previous_external_id,
            previous_mapping_file_sha256=self.previous_mapping_file_sha256,
            external_id_change_approved=self.external_id_change_approved,
        )
        expected_hash = _stable_sha256(payload)
        if self.approval_hash != expected_hash:
            raise ValueError("approval_hash does not match approval content")
        if self.approval_id != f"trivago-mapping-approval-{expected_hash[:20]}":
            raise ValueError("approval_id does not match approval_hash")
        return self


class TrivagoMappingApprovalWriter:
    """Write immutable, content-addressed Trivago mapping approvals."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, approval: TrivagoMappingApproval) -> Path:
        return (
            self.root_directory
            / f"hotel={quote(approval.entity_id, safe='-_.')}"
            / f"approval={quote(approval.approval_id, safe='-_.')}.json"
        )

    def write(self, approval: TrivagoMappingApproval) -> Path:
        validated = TrivagoMappingApproval.model_validate_json(
            approval.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                current = TrivagoMappingApproval.model_validate_json(
                    destination.read_bytes()
                )
                if current == validated:
                    return destination
                raise FileExistsError(
                    f"immutable Trivago approval already exists: {destination}"
                )
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(validated.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
        return destination


def approve_trivago_review(
    resolution_path: str | Path,
    registry_path: str | Path,
    *,
    reviewer: str,
    approval_writer: TrivagoMappingApprovalWriter,
    mapping_writer: CurrentTrivagoMappingWriter,
    allow_external_id_change: bool = False,
    approved_at: datetime | None = None,
) -> tuple[
    TrivagoMappingApproval,
    ExternalEntityMapping,
    Path,
    Path,
]:
    """Approve one exact REVIEW artifact and publish its confirmed mapping."""

    reviewed_at = approved_at or datetime.now(timezone.utc)
    resolution_file = Path(resolution_path)
    registry_file = Path(registry_path)
    resolution_bytes = resolution_file.read_bytes()
    registry_bytes = registry_file.read_bytes()
    resolution = TrivagoDiscoveryResolution.model_validate_json(resolution_bytes)
    registry = TrivagoHotelRegistry.model_validate_json(registry_bytes)

    if resolution.status is not TrivagoDiscoveryStatus.REVIEW:
        raise TrivagoMappingApprovalError(
            "only a Trivago REVIEW resolution can be human-approved"
        )
    expected_evidence_hash = compute_trivago_resolution_evidence_hash(resolution)
    if resolution.evidence_hash != expected_evidence_hash:
        raise TrivagoMappingApprovalError(
            "Trivago resolution evidence_hash does not match its evidence"
        )
    if not resolution.selected_external_id:
        raise TrivagoMappingApprovalError("Trivago REVIEW has no selected external_id")
    if not resolution.selected_name:
        raise TrivagoMappingApprovalError("Trivago REVIEW has no selected name")
    if not resolution.selected_external_url:
        raise TrivagoMappingApprovalError("Trivago REVIEW has no selected external URL")
    matching_candidates = [
        candidate
        for candidate in resolution.candidates
        if candidate.external_id == resolution.selected_external_id
        and candidate.name == resolution.selected_name
        and candidate.external_url == resolution.selected_external_url
    ]
    if not matching_candidates:
        raise TrivagoMappingApprovalError(
            "selected Trivago identity is not present in candidate evidence"
        )

    entries = [
        entry for entry in registry.entries if entry.entity_id == resolution.entity_id
    ]
    if len(entries) != 1:
        raise TrivagoMappingApprovalError(
            "registry must contain exactly one target hotel entry"
        )
    entry = entries[0]
    _require_unique_registry_external_ids(registry)
    registry_owner = next(
        (
            candidate.entity_id
            for candidate in registry.entries
            if candidate.external_id == resolution.selected_external_id
            and candidate.entity_id != resolution.entity_id
        ),
        None,
    )
    if registry_owner is not None:
        raise TrivagoMappingApprovalError(
            "selected Trivago external_id belongs to another registry hotel: "
            f"{registry_owner}"
        )
    selected_property_id = _trivago_property_id(resolution.selected_external_url)
    registry_property_owner = next(
        (
            candidate.entity_id
            for candidate in registry.entries
            if candidate.entity_id != resolution.entity_id
            and candidate.external_url is not None
            and _trivago_property_id(str(candidate.external_url))
            == selected_property_id
        ),
        None,
    )
    if selected_property_id is not None and registry_property_owner is not None:
        raise TrivagoMappingApprovalError(
            "selected Trivago property_id belongs to another registry hotel: "
            f"{registry_property_owner}"
        )

    current_mappings = mapping_writer.all()
    _require_unique_current_mappings(current_mappings)
    external_owner = next(
        (
            mapping.entity_id
            for mapping in current_mappings
            if mapping.external_id == resolution.selected_external_id
            and mapping.entity_id != resolution.entity_id
        ),
        None,
    )
    if external_owner is not None:
        raise TrivagoMappingApprovalError(
            "selected Trivago external_id already belongs to another hotel: "
            f"{external_owner}"
        )
    current_property_owner = next(
        (
            mapping.entity_id
            for mapping in current_mappings
            if mapping.entity_id != resolution.entity_id
            and mapping.external_url is not None
            and _trivago_property_id(str(mapping.external_url)) == selected_property_id
        ),
        None,
    )
    if selected_property_id is not None and current_property_owner is not None:
        raise TrivagoMappingApprovalError(
            "selected Trivago property_id already belongs to another hotel: "
            f"{current_property_owner}"
        )

    current = mapping_writer.get(resolution.entity_id)
    registry_previous_id = (
        entry.external_id if entry.status is TrivagoRegistryStatus.CONFIRMED else None
    )
    if (
        current is not None
        and registry_previous_id is not None
        and current.external_id != registry_previous_id
    ):
        raise TrivagoMappingApprovalError(
            "current mapping and registry disagree; rebuild the Trivago registry"
        )
    previous_external_id = (
        current.external_id if current is not None else registry_previous_id
    )
    external_id_changed = (
        previous_external_id is not None
        and previous_external_id != resolution.selected_external_id
    )
    if external_id_changed and not allow_external_id_change:
        raise TrivagoMappingApprovalError(
            "confirmed Trivago external_id would change; rerun with "
            "--allow-external-id-change after verifying provider identity"
        )
    if allow_external_id_change and not external_id_changed:
        raise TrivagoMappingApprovalError(
            "--allow-external-id-change was supplied but no ID change exists"
        )

    previous_mapping_hash = None
    if current is not None:
        previous_mapping_hash = hashlib.sha256(
            mapping_writer.path_for(current.entity_id).read_bytes()
        ).hexdigest()
    values = _approval_payload(
        schema_version="1.0.0",
        entity_id=resolution.entity_id,
        external_id=resolution.selected_external_id,
        external_url=resolution.selected_external_url,
        trivago_name=resolution.selected_name,
        reviewer=reviewer.strip(),
        approved_at=reviewed_at,
        source_record_id=resolution.source_record_id,
        resolved_at=resolution.resolved_at,
        resolver_version=resolution.resolver_version,
        confidence=resolution.confidence,
        reason_codes=resolution.reason_codes,
        resolution_path=str(resolution_file.resolve()),
        resolution_file_sha256=hashlib.sha256(resolution_bytes).hexdigest(),
        resolution_evidence_hash=resolution.evidence_hash,
        registry_path=str(registry_file.resolve()),
        registry_file_sha256=hashlib.sha256(registry_bytes).hexdigest(),
        registry_entry_hash=_stable_sha256(entry.model_dump(mode="json")),
        previous_external_id=previous_external_id,
        previous_mapping_file_sha256=previous_mapping_hash,
        external_id_change_approved=external_id_changed,
    )
    approval_hash = _stable_sha256(values)
    approval = TrivagoMappingApproval(
        approval_id=f"trivago-mapping-approval-{approval_hash[:20]}",
        approval_hash=approval_hash,
        **values,
    )
    mapping = _mapping_from_approval(entry, resolution, approval, current)

    # The audit is durable before a mapping can become current. A competing
    # writer can still make publication fail, but can never create an
    # unaudited confirmed mapping through this approval path.
    approval_path = approval_writer.write(approval)
    mapping_path = mapping_writer.publish(mapping)
    return approval, mapping, approval_path, mapping_path


def _mapping_from_approval(
    entry: TrivagoHotelRegistryEntry,
    resolution: TrivagoDiscoveryResolution,
    approval: TrivagoMappingApproval,
    current: ExternalEntityMapping | None,
) -> ExternalEntityMapping:
    attributes = dict(current.attributes) if current is not None else {}
    attributes.update(
        {
            "hotel_name": entry.master_name,
            "master_name": entry.master_name,
            "trivago_name": approval.trivago_name,
            "destination": entry.city,
            "master_address": entry.address,
            "master_latitude": entry.latitude,
            "master_longitude": entry.longitude,
            "search_query": entry.search_query,
            "human_mapping_approval_id": approval.approval_id,
            "human_mapping_approval_hash": approval.approval_hash,
            "human_mapping_reviewer": approval.reviewer,
            "human_mapping_approved_at": approval.approved_at.isoformat(),
            "resolution_evidence_hash": approval.resolution_evidence_hash,
            "resolution_file_sha256": approval.resolution_file_sha256,
            "registry_file_sha256": approval.registry_file_sha256,
            "external_id_change_approved": approval.external_id_change_approved,
            "previous_external_id": approval.previous_external_id,
        }
    )
    same_identity = current is not None and current.external_id == approval.external_id
    source_record_ids = list(
        dict.fromkeys(
            [
                *(current.source_record_ids if current is not None else []),
                *entry.source_record_ids,
                resolution.source_record_id,
            ]
        )
    )
    return ExternalEntityMapping(
        mapping_id=f"trivago-mcp-{entry.entity_id}",
        entity_id=entry.entity_id,
        entity_type=EntityType.HOTEL,
        source_id="trivago-mcp",
        external_id=approval.external_id,
        external_url=approval.external_url,
        status=MappingStatus.CONFIRMED,
        confidence=resolution.confidence,
        matched_at=(
            current.matched_at
            if same_identity and current is not None
            else approval.approved_at
        ),
        verified_at=approval.approved_at,
        last_checked_at=approval.approved_at,
        source_record_ids=source_record_ids,
        attributes=attributes,
    )


def _require_unique_registry_external_ids(registry: TrivagoHotelRegistry) -> None:
    owners: dict[str, str] = {}
    property_owners: dict[str, str] = {}
    for entry in registry.entries:
        if entry.external_id is None:
            continue
        owner = owners.setdefault(entry.external_id, entry.entity_id)
        if owner != entry.entity_id:
            raise TrivagoMappingApprovalError(
                "registry contains duplicate Trivago external_id "
                f"{entry.external_id}: {owner}, {entry.entity_id}"
            )
        if entry.external_url is not None:
            property_id = _trivago_property_id(str(entry.external_url))
            if property_id is not None:
                property_owner = property_owners.setdefault(
                    property_id, entry.entity_id
                )
                if property_owner != entry.entity_id:
                    raise TrivagoMappingApprovalError(
                        "registry contains duplicate Trivago property_id "
                        f"{property_id}: {property_owner}, {entry.entity_id}"
                    )


def _require_unique_current_mappings(
    mappings: list[ExternalEntityMapping],
) -> None:
    owners: dict[str, str] = {}
    property_owners: dict[str, str] = {}
    for mapping in mappings:
        owner = owners.setdefault(mapping.external_id, mapping.entity_id)
        if owner != mapping.entity_id:
            raise TrivagoMappingApprovalError(
                "current mappings contain duplicate Trivago external_id "
                f"{mapping.external_id}: {owner}, {mapping.entity_id}"
            )
        if mapping.external_url is None:
            continue
        property_id = _trivago_property_id(str(mapping.external_url))
        if property_id is None:
            continue
        property_owner = property_owners.setdefault(property_id, mapping.entity_id)
        if property_owner != mapping.entity_id:
            raise TrivagoMappingApprovalError(
                "current mappings contain duplicate Trivago property_id "
                f"{property_id}: {property_owner}, {mapping.entity_id}"
            )


def _trivago_property_id(value: str) -> str | None:
    parsed = urlparse(value)
    for search_value in parse_qs(parsed.query).get("search", []):
        match = re.search(r"(?:^|;)100-(\d+)(?:;|$)", search_value)
        if match:
            return match.group(1)
    match = re.search(r"(?:[?&;]|^)search=100-(\d+)", value)
    return match.group(1) if match else None
