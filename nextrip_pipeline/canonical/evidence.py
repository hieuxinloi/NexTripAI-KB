from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from itertools import combinations
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.schemas import (
    EntityType,
    GeoPoint,
    GoogleMapsPlaceObservation,
    NexTripModel,
)

from .models import stable_identifier, stable_sha256


class DuplicateEvidenceInputError(ValueError):
    """Raised when an offline evidence input cannot be audited safely."""


class DuplicateEvidenceStatus(StrEnum):
    CONFIRMED = "confirmed"
    REVIEW = "review"
    DISTINCT = "distinct"


class DuplicateEvidenceReason(StrEnum):
    SHARED_GOOGLE_PLACE_TOKEN = "shared_google_place_token"
    SHARED_GOOGLE_EXTERNAL_URL = "shared_google_external_url"
    OBSERVED_NAME_AND_LOCATION_MATCH = "observed_name_and_location_match"
    CONFLICTING_GOOGLE_PLACE_TOKEN = "conflicting_google_place_token"
    OBSERVED_LOCATIONS_FAR_APART = "observed_locations_far_apart"
    MASTER_NAME_CITY_MATCH_ONLY = "master_name_city_match_only"
    MISSING_GOOGLE_OBSERVATION = "missing_google_observation"
    INSUFFICIENT_STRONG_EVIDENCE = "insufficient_strong_evidence"
    CONFLICTING_STRONG_EVIDENCE = "conflicting_strong_evidence"


class DuplicateEvidencePolicy(NexTripModel):
    observed_match_distance_meters: float = Field(default=50.0, gt=0)
    observed_distinct_distance_meters: float = Field(default=500.0, gt=0)

    @model_validator(mode="after")
    def validate_distances(self) -> DuplicateEvidencePolicy:
        if (
            self.observed_distinct_distance_meters
            <= self.observed_match_distance_meters
        ):
            raise ValueError("distinct distance must exceed match distance")
        return self


class MasterIdentityProjection(NexTripModel):
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    name: str = Field(min_length=1)
    normalized_name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    normalized_city: str = Field(min_length=1)
    address: str | None = None
    location: GeoPoint | None = None
    source_path: str | None = None


class GoogleObservationIdentityEvidence(NexTripModel):
    place_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    source_url: str = Field(min_length=1)
    canonical_source_url: str = Field(min_length=1)
    google_place_token: str | None = None
    observed_name: str | None = None
    normalized_observed_name: str | None = None
    observed_address: str | None = None
    location: GeoPoint | None = None


class DuplicatePairEvidence(NexTripModel):
    left_place_id: str = Field(min_length=1)
    right_place_id: str = Field(min_length=1)
    status: DuplicateEvidenceStatus
    reason_codes: list[DuplicateEvidenceReason] = Field(min_length=1)
    same_master_name: bool
    same_master_city: bool
    same_observed_name: bool | None = None
    observed_distance_meters: float | None = Field(default=None, ge=0)
    left_google_place_token: str | None = None
    right_google_place_token: str | None = None
    shared_google_external_url: str | None = None


class DuplicateGroupEvidence(NexTripModel):
    group_id: str = Field(min_length=1)
    place_ids: list[str] = Field(min_length=2)
    status: DuplicateEvidenceStatus
    reason_codes: list[DuplicateEvidenceReason] = Field(min_length=1)
    master_identities: list[MasterIdentityProjection] = Field(min_length=2)
    google_observations: list[GoogleObservationIdentityEvidence] = Field(
        default_factory=list
    )
    missing_google_observation_place_ids: list[str] = Field(default_factory=list)
    pair_evidence: list[DuplicatePairEvidence] = Field(min_length=1)


class DuplicateEvidenceAudit(NexTripModel):
    schema_version: str = "1.0.0"
    audit_id: str = Field(min_length=1)
    audit_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_count: int = Field(ge=0)
    status_counts: dict[str, int]
    missing_google_observation_place_ids: list[str] = Field(default_factory=list)
    groups: list[DuplicateGroupEvidence]


def normalize_identity_text(value: str) -> str:
    """Return an accent-insensitive identity key without fuzzy matching."""

    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_like = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    ).replace("đ", "d")
    return " ".join(re.findall(r"[a-z0-9]+", ascii_like))


def extract_google_place_token(source_url: str) -> str | None:
    """Extract a stable Maps feature identifier from common place URL forms."""

    decoded = unquote(source_url)
    patterns = (
        r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)",
        r"!16s(/g/[A-Za-z0-9_-]+)",
        r"(?:query_place_id|place_id|ftid)=([^&#]+)",
        r"(?:[?&]q=place_id:)([^&#]+)",
        r"(?:[?&]cid=)([0-9]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1)).strip().casefold()
    return None


def canonical_google_place_url(source_url: str) -> str:
    """Remove presentation/tracking parameters while preserving place identity."""

    parsed = urlsplit(source_url.strip())
    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", unquote(parsed.path or "/")).rstrip("/")
    path = (path or "/").casefold()
    ignored = {"authuser", "entry", "g_ep", "hl", "utm_campaign", "utm_source"}
    query = sorted(
        (key.casefold(), unquote(value).casefold())
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in ignored
        and not key.casefold().startswith("utm_")
    )
    query_text = "&".join(f"{key}={value}" for key, value in query)
    canonical = host + path
    return canonical + (f"?{query_text}" if query_text else "")


def load_duplicate_candidate_groups(
    source: str | Path | Sequence[Sequence[str]],
) -> list[list[str]]:
    """Load registry-report groups or normalize an injected group collection."""

    if isinstance(source, (str, Path)):
        document = _read_json(Path(source))
        if isinstance(document, Mapping):
            raw_groups = document.get("duplicate_identity_candidates")
            if raw_groups is None:
                raw_groups = document.get("groups")
        else:
            raw_groups = document
    else:
        raw_groups = source
    if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
        raise DuplicateEvidenceInputError("duplicate candidate groups must be a list")

    groups: list[list[str]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for index, raw_group in enumerate(raw_groups):
        if isinstance(raw_group, Mapping):
            raw_group = raw_group.get("place_ids")
        if not isinstance(raw_group, Sequence) or isinstance(
            raw_group, (str, bytes)
        ):
            raise DuplicateEvidenceInputError(f"candidate group {index} must be a list")
        group = sorted({str(value).strip() for value in raw_group if str(value).strip()})
        if len(group) < 2:
            raise DuplicateEvidenceInputError(
                f"candidate group {index} must contain at least two unique IDs"
            )
        key = tuple(group)
        if key in seen_groups:
            raise DuplicateEvidenceInputError(f"duplicate candidate group: {group}")
        seen_groups.add(key)
        groups.append(group)
    return sorted(groups, key=lambda item: tuple(item))


def load_master_identity_projections(
    source: str | Path | Iterable[str | Path],
) -> list[MasterIdentityProjection]:
    """Read identity-only projections from verified master JSON documents."""

    paths = _resolve_json_paths(source, recursive=False)
    projections: list[MasterIdentityProjection] = []
    seen_ids: set[str] = set()
    for path in paths:
        document = _read_json(path)
        records = document.get("data") if isinstance(document, Mapping) else None
        if not isinstance(records, list):
            raise DuplicateEvidenceInputError(f"{path}: data must be a list")
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise DuplicateEvidenceInputError(f"{path}[{index}] must be an object")
            projection = _master_projection(record, source_path=path)
            if projection.place_id in seen_ids:
                raise DuplicateEvidenceInputError(
                    f"duplicate master place ID: {projection.place_id}"
                )
            seen_ids.add(projection.place_id)
            projections.append(projection)
    return sorted(projections, key=lambda item: item.place_id)


def load_latest_google_maps_observations(
    source: str | Path | Iterable[str | Path],
) -> list[GoogleMapsPlaceObservation]:
    """Read only Maps observations and retain the newest one for every place."""

    paths = _resolve_json_paths(source, recursive=True)
    latest_payloads: dict[str, tuple[datetime, str, Mapping[str, Any], Path, int]] = {}
    for path in paths:
        document = _read_json(path)
        documents = document if isinstance(document, list) else [document]
        for index, item in enumerate(documents):
            if not isinstance(item, Mapping) or item.get("source_id") != "google-maps-web":
                continue
            place_id = str(item.get("place_id") or "").strip()
            observation_id = str(item.get("observation_id") or "").strip()
            try:
                observed_at = datetime.fromisoformat(
                    str(item.get("observed_at") or "").replace("Z", "+00:00")
                )
            except ValueError as error:
                raise DuplicateEvidenceInputError(
                    f"invalid Google observation identity {path}[{index}]: {error}"
                ) from error
            if not place_id or not observation_id or observed_at.tzinfo is None:
                raise DuplicateEvidenceInputError(
                    f"invalid Google observation identity {path}[{index}]"
                )
            candidate = (observed_at, observation_id, item, path, index)
            current = latest_payloads.get(place_id)
            if current is None or candidate[:2] > current[:2]:
                latest_payloads[place_id] = candidate

    latest: list[GoogleMapsPlaceObservation] = []
    for place_id in sorted(latest_payloads):
        _, _, item, path, index = latest_payloads[place_id]
        payload = dict(item)
        # Early normalized captures stored a menu URL directly in this field.
        # Menu is irrelevant to identity evidence; adapting the historical shape
        # lets the auditor read the immutable record without rewriting it.
        if isinstance(payload.get("menu_source"), str):
            payload["menu_source"] = None
        try:
            latest.append(GoogleMapsPlaceObservation.model_validate(payload))
        except ValueError as error:
            raise DuplicateEvidenceInputError(
                f"invalid latest Google observation {path}[{index}]: {error}"
            ) from error
    return latest


class DuplicateEvidenceAuditor:
    """Classify name-based candidates using immutable, offline identity evidence."""

    def __init__(self, policy: DuplicateEvidencePolicy | None = None) -> None:
        self.policy = policy or DuplicateEvidencePolicy()

    def audit(
        self,
        candidate_groups: Sequence[Sequence[str]],
        master_identities: Iterable[MasterIdentityProjection],
        google_observations: Iterable[GoogleMapsPlaceObservation],
    ) -> DuplicateEvidenceAudit:
        groups = load_duplicate_candidate_groups(candidate_groups)
        master_by_id = _unique_by_place_id(master_identities, "master identity")
        latest_google = _latest_by_place_id(google_observations)
        unknown = sorted(
            {place_id for group in groups for place_id in group} - set(master_by_id)
        )
        if unknown:
            raise DuplicateEvidenceInputError(
                "candidate groups reference missing master IDs: " + ", ".join(unknown)
            )

        results = [
            self._audit_group(group, master_by_id, latest_google) for group in groups
        ]
        results.sort(key=lambda item: item.group_id)
        missing = sorted(
            {
                place_id
                for group in results
                for place_id in group.missing_google_observation_place_ids
            }
        )
        counts = Counter(item.status.value for item in results)
        status_counts = {
            status.value: counts[status.value] for status in DuplicateEvidenceStatus
        }
        payload = {
            "schema_version": "1.0.0",
            "groups": [item.model_dump(mode="json") for item in results],
        }
        audit_hash = stable_sha256(payload)
        return DuplicateEvidenceAudit(
            audit_id=f"duplicate_evidence_{audit_hash[:20]}",
            audit_hash=audit_hash,
            group_count=len(results),
            status_counts=status_counts,
            missing_google_observation_place_ids=missing,
            groups=results,
        )

    def audit_paths(
        self,
        *,
        candidate_groups_path: str | Path,
        master_source: str | Path | Iterable[str | Path],
        google_observation_source: str | Path | Iterable[str | Path],
    ) -> DuplicateEvidenceAudit:
        """Convenience path adapter; still performs no network or writes."""

        return self.audit(
            load_duplicate_candidate_groups(candidate_groups_path),
            load_master_identity_projections(master_source),
            load_latest_google_maps_observations(google_observation_source),
        )

    def _audit_group(
        self,
        place_ids: Sequence[str],
        master_by_id: Mapping[str, MasterIdentityProjection],
        latest_google: Mapping[str, GoogleMapsPlaceObservation],
    ) -> DuplicateGroupEvidence:
        ordered_ids = sorted(place_ids)
        masters = [master_by_id[place_id] for place_id in ordered_ids]
        google_evidence = [
            _observation_evidence(latest_google[place_id])
            for place_id in ordered_ids
            if place_id in latest_google
        ]
        evidence_by_id = {item.place_id: item for item in google_evidence}
        pairs = [
            self._audit_pair(
                master_by_id[left],
                master_by_id[right],
                evidence_by_id.get(left),
                evidence_by_id.get(right),
            )
            for left, right in combinations(ordered_ids, 2)
        ]
        pair_statuses = {item.status for item in pairs}
        if pair_statuses == {DuplicateEvidenceStatus.CONFIRMED}:
            status = DuplicateEvidenceStatus.CONFIRMED
        elif pair_statuses == {DuplicateEvidenceStatus.DISTINCT}:
            status = DuplicateEvidenceStatus.DISTINCT
        else:
            status = DuplicateEvidenceStatus.REVIEW

        missing = sorted(set(ordered_ids) - set(evidence_by_id))
        reasons = {reason for pair in pairs for reason in pair.reason_codes}
        if missing:
            reasons.add(DuplicateEvidenceReason.MISSING_GOOGLE_OBSERVATION)
        return DuplicateGroupEvidence(
            group_id=stable_identifier("duplicate_group", *ordered_ids),
            place_ids=ordered_ids,
            status=status,
            reason_codes=sorted(reasons, key=lambda item: item.value),
            master_identities=masters,
            google_observations=google_evidence,
            missing_google_observation_place_ids=missing,
            pair_evidence=pairs,
        )

    def _audit_pair(
        self,
        left_master: MasterIdentityProjection,
        right_master: MasterIdentityProjection,
        left_google: GoogleObservationIdentityEvidence | None,
        right_google: GoogleObservationIdentityEvidence | None,
    ) -> DuplicatePairEvidence:
        same_master_name = (
            left_master.normalized_name == right_master.normalized_name
        )
        same_master_city = left_master.normalized_city == right_master.normalized_city
        reasons: set[DuplicateEvidenceReason] = set()
        confirm_signals: set[DuplicateEvidenceReason] = set()
        conflict_signals: set[DuplicateEvidenceReason] = set()
        distance: float | None = None
        same_observed_name: bool | None = None
        shared_url: str | None = None

        if left_google is not None and right_google is not None:
            left_token = left_google.google_place_token
            right_token = right_google.google_place_token
            if left_token and right_token:
                if left_token == right_token:
                    confirm_signals.add(
                        DuplicateEvidenceReason.SHARED_GOOGLE_PLACE_TOKEN
                    )
                else:
                    conflict_signals.add(
                        DuplicateEvidenceReason.CONFLICTING_GOOGLE_PLACE_TOKEN
                    )
            if (
                _is_strong_google_place_url(left_google.source_url)
                and _is_strong_google_place_url(right_google.source_url)
                and left_google.canonical_source_url
                == right_google.canonical_source_url
            ):
                shared_url = left_google.canonical_source_url
                confirm_signals.add(
                    DuplicateEvidenceReason.SHARED_GOOGLE_EXTERNAL_URL
                )

            left_name = left_google.normalized_observed_name
            right_name = right_google.normalized_observed_name
            same_observed_name = bool(
                left_name and right_name and left_name == right_name
            )
            distance = _distance_meters(left_google.location, right_google.location)
            if (
                same_master_city
                and same_observed_name
                and distance is not None
                and distance <= self.policy.observed_match_distance_meters
            ):
                confirm_signals.add(
                    DuplicateEvidenceReason.OBSERVED_NAME_AND_LOCATION_MATCH
                )
            if (
                distance is not None
                and distance >= self.policy.observed_distinct_distance_meters
            ):
                conflict_signals.add(
                    DuplicateEvidenceReason.OBSERVED_LOCATIONS_FAR_APART
                )

        reasons.update(confirm_signals)
        reasons.update(conflict_signals)
        if confirm_signals and conflict_signals:
            status = DuplicateEvidenceStatus.REVIEW
            reasons.add(DuplicateEvidenceReason.CONFLICTING_STRONG_EVIDENCE)
        elif confirm_signals:
            status = DuplicateEvidenceStatus.CONFIRMED
        elif conflict_signals:
            status = DuplicateEvidenceStatus.DISTINCT
        else:
            status = DuplicateEvidenceStatus.REVIEW
            if same_master_name and same_master_city:
                reasons.add(DuplicateEvidenceReason.MASTER_NAME_CITY_MATCH_ONLY)
            else:
                reasons.add(DuplicateEvidenceReason.INSUFFICIENT_STRONG_EVIDENCE)

        return DuplicatePairEvidence(
            left_place_id=left_master.place_id,
            right_place_id=right_master.place_id,
            status=status,
            reason_codes=sorted(reasons, key=lambda item: item.value),
            same_master_name=same_master_name,
            same_master_city=same_master_city,
            same_observed_name=same_observed_name,
            observed_distance_meters=(round(distance, 2) if distance is not None else None),
            left_google_place_token=(
                left_google.google_place_token if left_google else None
            ),
            right_google_place_token=(
                right_google.google_place_token if right_google else None
            ),
            shared_google_external_url=shared_url,
        )


def _master_projection(
    record: Mapping[str, Any], *, source_path: Path
) -> MasterIdentityProjection:
    required = ("id", "entity_type", "name", "city")
    missing = [field for field in required if not str(record.get(field) or "").strip()]
    if missing:
        raise DuplicateEvidenceInputError(
            f"{source_path}: master identity missing {', '.join(missing)}"
        )
    coordinates = record.get("coordinates")
    location = None
    if isinstance(coordinates, Mapping):
        latitude = coordinates.get("lat")
        longitude = coordinates.get("lng")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            location = GeoPoint(latitude=latitude, longitude=longitude)
    name = str(record["name"]).strip()
    city = str(record["city"]).strip()
    try:
        entity_type = EntityType(str(record["entity_type"]).strip())
    except ValueError as error:
        raise DuplicateEvidenceInputError(
            f"{source_path}: unsupported entity_type {record['entity_type']}"
        ) from error
    address = str(record.get("address") or "").strip() or None
    return MasterIdentityProjection(
        place_id=str(record["id"]).strip(),
        entity_type=entity_type,
        name=name,
        normalized_name=normalize_identity_text(name),
        city=city,
        normalized_city=normalize_identity_text(city),
        address=address,
        location=location,
        source_path=str(source_path),
    )


def _observation_evidence(
    observation: GoogleMapsPlaceObservation,
) -> GoogleObservationIdentityEvidence:
    source_url = str(observation.source_url)
    name = observation.name.strip() if observation.name else None
    location = observation.location
    if location is not None and (
        (location.source or "").casefold() == "verified-master-data"
        or "fallback" in (location.accuracy or "").casefold()
    ):
        # A normalizer may retain master coordinates when Maps exposes none.
        # That is useful operationally but is not independent duplicate evidence.
        location = None
    return GoogleObservationIdentityEvidence(
        place_id=observation.place_id,
        observation_id=observation.observation_id,
        observed_at=observation.observed_at,
        source_url=source_url,
        canonical_source_url=canonical_google_place_url(source_url),
        google_place_token=extract_google_place_token(source_url),
        observed_name=name,
        normalized_observed_name=(normalize_identity_text(name) if name else None),
        observed_address=observation.address,
        location=location,
    )


def _unique_by_place_id(
    records: Iterable[MasterIdentityProjection], label: str
) -> dict[str, MasterIdentityProjection]:
    result: dict[str, MasterIdentityProjection] = {}
    for record in records:
        if record.place_id in result:
            raise DuplicateEvidenceInputError(
                f"duplicate {label} place ID: {record.place_id}"
            )
        result[record.place_id] = record
    return result


def _latest_by_place_id(
    observations: Iterable[GoogleMapsPlaceObservation],
) -> dict[str, GoogleMapsPlaceObservation]:
    result: dict[str, GoogleMapsPlaceObservation] = {}
    for observation in observations:
        current = result.get(observation.place_id)
        if current is None or (
            observation.observed_at,
            observation.observation_id,
        ) > (current.observed_at, current.observation_id):
            result[observation.place_id] = observation
    return result


def _resolve_json_paths(
    source: str | Path | Iterable[str | Path], *, recursive: bool
) -> list[Path]:
    raw_paths = [source] if isinstance(source, (str, Path)) else list(source)
    resolved: list[Path] = []
    for raw_path in raw_paths:
        path = Path(raw_path)
        if path.is_dir():
            pattern = "**/*.json" if recursive else "*_final.json"
            resolved.extend(path.glob(pattern))
        elif path.is_file():
            resolved.append(path)
        else:
            raise FileNotFoundError(path)
    return sorted(set(resolved))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DuplicateEvidenceInputError(f"cannot read {path}: {error}") from error


def _distance_meters(left: GeoPoint | None, right: GeoPoint | None) -> float | None:
    if left is None or right is None:
        return None
    latitude_1 = math.radians(left.latitude)
    latitude_2 = math.radians(right.latitude)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = math.radians(right.longitude - left.longitude)
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _is_strong_google_place_url(source_url: str) -> bool:
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").casefold()
    path = unquote(parsed.path).casefold()
    if host == "maps.app.goo.gl":
        return True
    # A search URL is only query provenance. Even if two records used the same
    # query, it does not prove Google resolved them to the same physical place.
    return "/maps/place/" in path
