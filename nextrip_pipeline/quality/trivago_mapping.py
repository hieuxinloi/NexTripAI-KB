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
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
    SourceRecord,
)


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
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class TrivagoDiscoveryResolver:
    """Resolve Trivago IDs conservatively from name and city evidence."""

    version = "1.2.0"
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

        if (
            entry.status is TrivagoRegistryStatus.CONFIRMED
            and entry.external_id
            and raw_candidates
        ):
            possible_replacement = max(
                candidates,
                key=lambda candidate: candidate.name_score,
                default=None,
            )
            return self._resolution(
                entry,
                record,
                TrivagoDiscoveryStatus.REVIEW,
                possible_replacement,
                possible_replacement.name_score if possible_replacement else 0,
                [
                    "confirmed_external_id_not_returned",
                    "external_id_change_requires_review",
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
        evidence_hash = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
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

    def publish(self, mapping: ExternalEntityMapping) -> Path:
        if (
            mapping.source_id != "trivago-mcp"
            or mapping.status is not MappingStatus.CONFIRMED
            or mapping.verified_at is None
            or mapping.last_checked_at is None
        ):
            raise ValueError("current Trivago mapping must be confirmed and verified")
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
