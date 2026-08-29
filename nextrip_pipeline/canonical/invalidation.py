from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, field_validator, model_validator

from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import GeoPoint, GoogleMapsPlaceObservation, NexTripModel

from .eligibility import CandidateEntityEligibilityPolicy
from .models import CanonicalPlaceIdentity, stable_sha256

if TYPE_CHECKING:
    from .models import CanonicalIdentityManifest


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GOOGLE_SOURCE_ID = "google-maps-web"


class CanonicalInvalidationInputError(ValueError):
    """Raised when an invalidation is not backed by sufficient evidence."""


class CanonicalInvalidationWriteConflictError(ValueError):
    """Raised when an immutable invalidation record would be replaced."""


class CanonicalInvalidationReason(StrEnum):
    """Reviewed reasons that permanently remove an identity from active data."""

    ENTITY_TYPE_INELIGIBLE = "entity_type_ineligible"


class CanonicalInvalidationIdentityCorroboration(NexTripModel):
    """Content-pinned master facts that strongly identify the observed place."""

    reference_place_id: str = Field(min_length=1)
    reference_artifact_path: str = Field(min_length=1)
    reference_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    reference_record_index: int = Field(ge=0)
    reference_record_hash: str = Field(pattern=_SHA256_PATTERN)
    reference_name: str | None = None
    reference_address: str | None = None
    reference_phone: str | None = None
    reference_location: GeoPoint | None = None
    matched_signals: list[Literal["name", "address", "phone", "geo"]] = Field(
        min_length=2
    )

    @field_validator("matched_signals")
    @classmethod
    def validate_matched_signals(
        cls,
        values: list[Literal["name", "address", "phone", "geo"]],
    ) -> list[Literal["name", "address", "phone", "geo"]]:
        ordered = sorted(set(values))
        if len(ordered) != len(values):
            raise ValueError("identity corroboration signals must be unique")
        if not {"address", "phone", "geo"} & set(ordered):
            raise ValueError(
                "identity corroboration requires address, phone, or geo evidence"
            )
        return ordered


class CanonicalInvalidationEvidence(NexTripModel):
    """A content-pinned Google observation supporting one invalidation."""

    source_id: Literal["google-maps-web"] = "google-maps-web"
    source_record_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    source_url: HttpUrl
    observed_at: AwareDatetime
    observed_name: str | None = None
    observed_category: str = Field(min_length=1)
    observed_address: str | None = None
    observed_phone: str | None = None
    observed_website_url: HttpUrl | None = None
    observed_location: GeoPoint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    artifact_path: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_observation_hash: str = Field(pattern=_SHA256_PATTERN)
    identity_corroboration: CanonicalInvalidationIdentityCorroboration | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


def _approval_payload(
    *,
    schema_version: str,
    canonical_place_id: str,
    source_manifest_id: str,
    source_manifest_hash: str,
    identity_hash: str,
    reason: CanonicalInvalidationReason,
    reviewer: str,
    approved_at: datetime,
    evidence: list[CanonicalInvalidationEvidence],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "canonical_place_id": canonical_place_id,
        "source_manifest_id": source_manifest_id,
        "source_manifest_hash": source_manifest_hash,
        "identity_hash": identity_hash,
        "reason": reason.value,
        "reviewer": reviewer,
        "approved_at": approved_at.isoformat(),
        "evidence": [item.model_dump(mode="json") for item in evidence],
    }


class ApprovedCanonicalInvalidation(NexTripModel):
    """Immutable human approval to quarantine one canonical identity."""

    schema_version: Literal["1.0.0", "1.1.0"] = "1.1.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=_SHA256_PATTERN)
    canonical_place_id: str = Field(min_length=1)
    source_manifest_id: str = Field(min_length=1)
    source_manifest_hash: str = Field(pattern=_SHA256_PATTERN)
    identity_hash: str = Field(pattern=_SHA256_PATTERN)
    reason: CanonicalInvalidationReason
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime
    evidence: list[CanonicalInvalidationEvidence] = Field(min_length=1)

    @field_validator("reviewer")
    @classmethod
    def normalize_reviewer(cls, value: str) -> str:
        reviewer = value.strip()
        if not reviewer:
            raise ValueError("invalidation reviewer cannot be blank")
        return reviewer

    @field_validator("evidence")
    @classmethod
    def sort_evidence(
        cls, values: list[CanonicalInvalidationEvidence]
    ) -> list[CanonicalInvalidationEvidence]:
        ordered = sorted(
            values,
            key=lambda item: (
                item.observed_at,
                item.observation_id,
                item.artifact_sha256,
            ),
        )
        observation_ids = [item.observation_id for item in ordered]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("invalidation evidence observations must be unique")
        return ordered

    @model_validator(mode="after")
    def validate_approval(self) -> ApprovedCanonicalInvalidation:
        policy = CandidateEntityEligibilityPolicy()
        for item in self.evidence:
            if not policy.is_recognized_ineligible(item.observed_category):
                raise ValueError(
                    "invalidation category is not a recognized ineligible category"
                )
        if self.schema_version == "1.0.0":
            if any(item.identity_corroboration is not None for item in self.evidence):
                raise ValueError(
                    "approval schema 1.0.0 cannot contain identity corroboration"
                )
        elif any(item.identity_corroboration is None for item in self.evidence):
            raise ValueError(
                "approval schema 1.1.0 requires strong identity corroboration"
            )
        payload = _approval_payload(
            schema_version=self.schema_version,
            canonical_place_id=self.canonical_place_id,
            source_manifest_id=self.source_manifest_id,
            source_manifest_hash=self.source_manifest_hash,
            identity_hash=self.identity_hash,
            reason=self.reason,
            reviewer=self.reviewer,
            approved_at=self.approved_at,
            evidence=self.evidence,
        )
        expected_hash = stable_sha256(payload)
        if self.approval_hash != expected_hash:
            raise ValueError("approval_hash does not match invalidation content")
        if self.approval_id != f"canonical-invalidation-{expected_hash[:20]}":
            raise ValueError("approval_id does not match approval_hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        identity: CanonicalPlaceIdentity,
        source_manifest_id: str,
        source_manifest_hash: str,
        reason: CanonicalInvalidationReason,
        reviewer: str,
        approved_at: datetime,
        evidence: list[CanonicalInvalidationEvidence],
    ) -> ApprovedCanonicalInvalidation:
        values: dict[str, object] = {
            "schema_version": "1.1.0",
            "canonical_place_id": identity.canonical_place_id,
            "source_manifest_id": source_manifest_id,
            "source_manifest_hash": source_manifest_hash,
            "identity_hash": identity.identity_hash,
            "reason": reason,
            "reviewer": reviewer.strip(),
            "approved_at": approved_at,
            "evidence": evidence,
        }
        approval_hash = stable_sha256(
            _approval_payload(**values)  # type: ignore[arg-type]
        )
        return cls(
            approval_id=f"canonical-invalidation-{approval_hash[:20]}",
            approval_hash=approval_hash,
            **values,
        )


def approve_canonical_invalidation(
    manifest: CanonicalIdentityManifest,
    *,
    place_id: str,
    observation_path: str | Path,
    reason: CanonicalInvalidationReason,
    reviewer: str,
    identity_source_path: str | Path | None = None,
    approved_at: datetime | None = None,
    policy: CandidateEntityEligibilityPolicy | None = None,
) -> ApprovedCanonicalInvalidation:
    """Validate one source-pinned observation and create a human approval."""

    identity = next(
        (
            item
            for item in manifest.identities
            if place_id == item.canonical_place_id
            or place_id in item.legacy_place_ids
        ),
        None,
    )
    if identity is None:
        if any(
            place_id == item.identity.canonical_place_id
            or place_id in item.identity.legacy_place_ids
            for item in manifest.quarantined_identities
        ):
            raise CanonicalInvalidationInputError(
                f"canonical identity is already quarantined: {place_id}"
            )
        raise CanonicalInvalidationInputError(
            f"canonical identity is not active: {place_id}"
        )

    path = Path(observation_path)
    try:
        raw_bytes = path.read_bytes()
        observation = GoogleMapsPlaceObservation.model_validate_json(raw_bytes)
    except (OSError, TypeError, ValueError) as error:
        raise CanonicalInvalidationInputError(
            f"cannot read invalidation observation {path}: {error}"
        ) from error
    if observation.source_id != _GOOGLE_SOURCE_ID:
        raise CanonicalInvalidationInputError(
            "invalidation evidence must come from google-maps-web"
        )
    if observation.place_id not in identity.legacy_place_ids:
        raise CanonicalInvalidationInputError(
            "invalidation observation belongs to another canonical identity"
        )
    category = " ".join((observation.category or "").split())
    if not category:
        raise CanonicalInvalidationInputError(
            "invalidation requires a non-empty observed Google category"
        )
    effective_policy = policy or CandidateEntityEligibilityPolicy()
    if effective_policy.is_explicitly_ambiguous(category):
        raise CanonicalInvalidationInputError(
            f"ambiguous Google category cannot invalidate an identity: {category}"
        )
    matching_types = effective_policy.matching_entity_types(category)
    if matching_types & set(identity.place_types):
        raise CanonicalInvalidationInputError(
            "observed Google category is compatible with the canonical identity"
        )
    if matching_types:
        raise CanonicalInvalidationInputError(
            "a recognized NexTrip category requires reclassification, not quarantine"
        )
    if not effective_policy.is_recognized_ineligible(category):
        raise CanonicalInvalidationInputError(
            "unknown or mistyped Google category cannot invalidate an identity: "
            + category
        )
    if reason is not CanonicalInvalidationReason.ENTITY_TYPE_INELIGIBLE:
        raise CanonicalInvalidationInputError(
            f"unsupported canonical invalidation reason: {reason}"
        )

    decision_time = approved_at or datetime.now(timezone.utc)
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise CanonicalInvalidationInputError("approved_at must be timezone-aware")
    if identity_source_path is None:
        raise CanonicalInvalidationInputError(
            "invalidation requires a pinned identity source for physical-place "
            "corroboration"
        )
    corroboration = _build_identity_corroboration(
        identity,
        observation,
        identity_source_path,
    )
    evidence = CanonicalInvalidationEvidence(
        source_record_id=observation.source_record_id,
        observation_id=observation.observation_id,
        source_url=observation.source_url,
        observed_at=observation.observed_at,
        observed_name=observation.name,
        observed_category=category,
        observed_address=observation.address,
        observed_phone=observation.phone,
        observed_website_url=observation.website_url,
        observed_location=observation.location,
        artifact_path=path.as_posix(),
        artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        source_observation_hash=stable_sha256(
            observation.model_dump(mode="json")
        ),
        identity_corroboration=corroboration,
    )
    return ApprovedCanonicalInvalidation.create(
        identity=identity,
        source_manifest_id=manifest.manifest_id,
        source_manifest_hash=manifest.manifest_hash,
        reason=reason,
        reviewer=reviewer,
        approved_at=decision_time,
        evidence=[evidence],
    )


def _build_identity_corroboration(
    identity: CanonicalPlaceIdentity,
    observation: GoogleMapsPlaceObservation,
    identity_source_path: str | Path,
) -> CanonicalInvalidationIdentityCorroboration:
    path = Path(identity_source_path)
    try:
        raw_bytes = path.read_bytes()
        document = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanonicalInvalidationInputError(
            f"cannot read identity corroboration source {path}: {error}"
        ) from error
    records = document.get("data") if isinstance(document, dict) else None
    if not isinstance(records, list):
        raise CanonicalInvalidationInputError(
            "identity corroboration source must contain a data array"
        )
    matches = [
        (index, item)
        for index, item in enumerate(records)
        if isinstance(item, dict) and item.get("id") == observation.place_id
    ]
    if len(matches) != 1 or observation.place_id not in identity.legacy_place_ids:
        raise CanonicalInvalidationInputError(
            "identity corroboration source does not uniquely contain the observed "
            "canonical member"
        )
    record_index, record = matches[0]
    matched_signals = _identity_match_signals(record, observation)
    if len(matched_signals) < 2 or not (
        {"address", "phone", "geo"} & set(matched_signals)
    ):
        raise CanonicalInvalidationInputError(
            "observation is not strongly corroborated as the canonical physical "
            "place; require two matching signals including address, phone, or geo"
        )
    reference_location = _reference_location(record)
    return CanonicalInvalidationIdentityCorroboration(
        reference_place_id=observation.place_id,
        reference_artifact_path=path.as_posix(),
        reference_artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        reference_record_index=record_index,
        reference_record_hash=stable_sha256(record),
        reference_name=_optional_text(record.get("name")),
        reference_address=_optional_text(record.get("address")),
        reference_phone=_reference_phone(record),
        reference_location=reference_location,
        matched_signals=matched_signals,
    )


def _identity_match_signals(
    record: Mapping[str, object],
    observation: GoogleMapsPlaceObservation,
) -> list[Literal["name", "address", "phone", "geo"]]:
    signals: list[Literal["name", "address", "phone", "geo"]] = []
    reference_name = _optional_text(record.get("name"))
    if reference_name and observation.name and (
        _identity_text_key(reference_name) == _identity_text_key(observation.name)
    ):
        signals.append("name")
    reference_address = _optional_text(record.get("address"))
    if reference_address and observation.address and (
        _identity_text_key(reference_address)
        == _identity_text_key(observation.address)
    ):
        signals.append("address")
    reference_phone = _reference_phone(record)
    if reference_phone and observation.phone and (
        _phone_key(reference_phone) == _phone_key(observation.phone)
    ):
        signals.append("phone")
    reference_location = _reference_location(record)
    if (
        reference_location is not None
        and observation.location is not None
        and _location_is_independently_observed(observation.location)
        and _distance_metres(reference_location, observation.location) <= 50
    ):
        signals.append("geo")
    return signals


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if isinstance(value, str) else ""
    return text or None


def _reference_phone(record: Mapping[str, object]) -> str | None:
    if phone := _optional_text(record.get("phone")):
        return phone
    contact = record.get("contact")
    return (
        _optional_text(contact.get("phone"))
        if isinstance(contact, Mapping)
        else None
    )


def _reference_location(record: Mapping[str, object]) -> GeoPoint | None:
    raw = record.get("coordinates") or record.get("location")
    if not isinstance(raw, Mapping):
        return None
    latitude = raw.get("lat", raw.get("latitude"))
    longitude = raw.get("lng", raw.get("longitude"))
    if not isinstance(latitude, (int, float)) or not isinstance(
        longitude, (int, float)
    ):
        return None
    return GeoPoint(latitude=float(latitude), longitude=float(longitude))


def _identity_text_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", unaccented))


def _phone_key(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    return digits[-9:]


def _location_is_independently_observed(location: GeoPoint) -> bool:
    provenance = " ".join(
        value.casefold()
        for value in (location.source, location.accuracy)
        if value
    )
    return "master" not in provenance and "fallback" not in provenance


def _distance_metres(left: GeoPoint, right: GeoPoint) -> float:
    latitude_1 = math.radians(left.latitude)
    latitude_2 = math.radians(right.latitude)
    latitude_delta = latitude_2 - latitude_1
    longitude_delta = math.radians(right.longitude - left.longitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_1)
        * math.cos(latitude_2)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


class CanonicalInvalidationWriter:
    """Write one immutable invalidation record per canonical identity."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)
        self.records_root = self.output_root / "records"

    def destination_for(self, approval: ApprovedCanonicalInvalidation) -> Path:
        return self.records_root / (
            f"place={quote(approval.canonical_place_id, safe='-_.')}.json"
        )

    def write(self, approval: ApprovedCanonicalInvalidation) -> Path:
        validated = ApprovedCanonicalInvalidation.model_validate_json(
            approval.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                try:
                    current = ApprovedCanonicalInvalidation.model_validate_json(
                        destination.read_bytes()
                    )
                except (OSError, TypeError, ValueError) as error:
                    raise CanonicalInvalidationWriteConflictError(
                        f"cannot validate existing invalidation {destination}: {error}"
                    ) from error
                if current == validated:
                    return destination
                raise CanonicalInvalidationWriteConflictError(
                    "canonical identity already has another invalidation approval: "
                    + validated.canonical_place_id
                )
            content = (
                json.dumps(
                    validated.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            temporary = destination.with_name(
                f".{destination.name}.{uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return destination


def load_approved_invalidations(
    output_root: str | Path,
) -> list[ApprovedCanonicalInvalidation]:
    records_root = Path(output_root) / "records"
    if not records_root.exists():
        return []
    by_place_id: dict[str, ApprovedCanonicalInvalidation] = {}
    for path in sorted(records_root.rglob("*.json"), key=lambda item: item.as_posix()):
        try:
            record = ApprovedCanonicalInvalidation.model_validate_json(
                path.read_bytes()
            )
        except (OSError, TypeError, ValueError) as error:
            raise CanonicalInvalidationWriteConflictError(
                f"cannot validate existing invalidation {path}: {error}"
            ) from error
        previous = by_place_id.get(record.canonical_place_id)
        if previous is not None and previous != record:
            raise CanonicalInvalidationWriteConflictError(
                "multiple invalidations exist for canonical identity: "
                + record.canonical_place_id
            )
        by_place_id[record.canonical_place_id] = record
    return sorted(by_place_id.values(), key=lambda item: item.canonical_place_id)
