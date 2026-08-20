from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    ExistingCanonicalIdentity,
)
from nextrip_pipeline.canonical.detail import CandidateDetailStage
from nextrip_pipeline.canonical.discovery import _google_maps_identity
from nextrip_pipeline.canonical.master import CanonicalMasterLoad
from nextrip_pipeline.canonical.models import (
    CanonicalIdentityManifest,
    LegacyPlaceSlot,
    VacancyStatus,
    stable_sha256,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    GeoPoint,
    MappingStatus,
    NexTripModel,
    VerificationStatus,
)
from nextrip_pipeline.schemas.current_place import CurrentPlaceSnapshot

if TYPE_CHECKING:
    from nextrip_pipeline.canonical.review_correction import (
        CanonicalReviewCorrectionOverlay,
    )


_GOOGLE_SOURCE_ID = "google-maps-web"
_PLACE_ID_PATTERN = re.compile(
    r"^(?P<entity>attr|cafe|hotel|night|rest)_"
    r"(?P<city>dn|qn)_(?P<sequence>[0-9]{3,})$"
)
_ENTITY_PREFIX = {
    EntityType.ATTRACTION: "attr",
    EntityType.CAFE: "cafe",
    EntityType.HOTEL: "hotel",
    EntityType.NIGHTLIFE: "night",
    EntityType.RESTAURANT: "rest",
}
_CITY_PREFIX = {"city_da_nang": "dn", "city_quy_nhon": "qn"}
_CITY_NAME = {
    "city_da_nang": "Đà Nẵng",
    "city_quy_nhon": "Quy Nhơn",
}


class ExistingIdentityProjectionError(ValueError):
    """Raised when verified identity inputs cannot produce a safe projection."""


class ReplacementApprovalError(ValueError):
    """Raised when a candidate detail is not eligible for human approval."""


class ReplacementWriteConflictError(ValueError):
    """Raised when materialization would reuse an ID or external identity."""


class ReplacementApprovalMethod(StrEnum):
    HUMAN = "human"
    DETERMINISTIC = "deterministic"


class ProjectionInputKind(StrEnum):
    CURRENT_PLACE = "current_place"
    GOOGLE_MAPPING = "google_mapping"


class ProjectionIssueCode(StrEnum):
    INPUT_NOT_DIRECTORY = "input_not_directory"
    MALFORMED_INPUT = "malformed_input"
    DUPLICATE_RECORD = "duplicate_record"
    IDENTITY_MISMATCH = "identity_mismatch"


class ProjectionInputIssue(NexTripModel):
    """An optional current-state artifact excluded from identity projection."""

    input_kind: ProjectionInputKind
    relative_path: str = Field(min_length=1)
    code: ProjectionIssueCode
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    message: str = Field(min_length=1)
    quarantined: Literal[True] = True


class ProjectedExistingCanonicalIdentity(ExistingCanonicalIdentity):
    """Existing identity plus address evidence retained by this projection."""

    address: str | None = None


def _projection_payload(
    identities: list[ProjectedExistingCanonicalIdentity],
    issues: list[ProjectionInputIssue],
    *,
    review_correction_overlay_id: str | None = None,
    review_correction_overlay_hash: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "identities": [item.model_dump(mode="json") for item in identities],
        "quarantined_inputs": [item.model_dump(mode="json") for item in issues],
        "review_correction_overlay_id": review_correction_overlay_id,
        "review_correction_overlay_hash": review_correction_overlay_hash,
    }


class ExistingIdentityProjection(NexTripModel):
    """Complete duplicate-check projection plus isolated optional input errors."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identities: list[ProjectedExistingCanonicalIdentity]
    quarantined_inputs: list[ProjectionInputIssue] = Field(default_factory=list)
    review_correction_overlay_id: str | None = Field(default=None, min_length=1)
    review_correction_overlay_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_projection(self) -> ExistingIdentityProjection:
        if (self.review_correction_overlay_id is None) != (
            self.review_correction_overlay_hash is None
        ):
            raise ValueError(
                "review correction overlay ID and hash must be provided together"
            )
        place_ids = [item.place_id for item in self.identities]
        if len(place_ids) != len(set(place_ids)):
            raise ValueError("projected place IDs must be unique")
        if self.identities != sorted(
            self.identities, key=lambda item: (item.place_id, item.retired)
        ):
            raise ValueError("projected identities must be sorted by place ID")
        if self.quarantined_inputs != sorted(
            self.quarantined_inputs,
            key=lambda item: (
                item.input_kind.value,
                item.relative_path,
                item.code.value,
                item.message,
            ),
        ):
            raise ValueError("quarantined inputs must be deterministically sorted")
        expected_hash = stable_sha256(
            _projection_payload(
                self.identities,
                self.quarantined_inputs,
                review_correction_overlay_id=self.review_correction_overlay_id,
                review_correction_overlay_hash=self.review_correction_overlay_hash,
            )
        )
        if self.projection_hash != expected_hash:
            raise ValueError("projection_hash does not match projection content")
        return self


@dataclass(frozen=True, slots=True)
class _LoadedArtifact:
    relative_path: str
    input_hash: str
    value: CurrentPlaceSnapshot | ExternalEntityMapping


def build_existing_identity_projection(
    master: CanonicalMasterLoad,
    manifest: CanonicalIdentityManifest,
    *,
    current_place_directory: str | Path | None = None,
    current_google_mapping_directory: str | Path | None = None,
    approved_replacements: Iterable[MaterializedReplacementRecord] = (),
) -> ExistingIdentityProjection:
    """Project every active identity and retired alias for duplicate checks.

    The verified master and canonical manifest are authoritative. Current-place
    and Google mapping directories are optional enrichment: a missing directory
    is equivalent to no enrichment, while malformed files are recorded as
    quarantined inputs and excluded without aborting the global projection.
    """

    overlays = _validate_projection_overlays(
        approved_replacements,
        manifest=manifest,
    )
    overlay_by_id = {item.id: item for item in overlays}
    issues: list[ProjectionInputIssue] = []
    current_artifacts = _load_optional_artifacts(
        current_place_directory,
        CurrentPlaceSnapshot,
        ProjectionInputKind.CURRENT_PLACE,
        issues,
    )
    mapping_artifacts = _load_optional_artifacts(
        current_google_mapping_directory,
        ExternalEntityMapping,
        ProjectionInputKind.GOOGLE_MAPPING,
        issues,
    )
    current_by_id = _index_current_places(current_artifacts, issues)
    mappings_by_id = _index_google_mappings(mapping_artifacts, issues)

    identities: list[ProjectedExistingCanonicalIdentity] = []
    retirement_by_id = {
        item.retired_place_id: item for item in manifest.retired_place_ids
    }
    expected_ids: set[str] = set()
    for canonical in manifest.identities:
        members = list(canonical.legacy_place_ids)
        expected_ids.add(canonical.canonical_place_id)
        expected_ids.update(
            item
            for item in members
            if item != canonical.active_legacy_place_id
        )
        overlay = overlay_by_id.get(canonical.canonical_place_id)
        if overlay is not None:
            external_identities = [_overlay_google_identity(overlay)]
            identities.append(_project_overlay_identity(overlay))
        else:
            external_identities = _confirmed_google_identities(
                members,
                mappings_by_id=mappings_by_id,
                current_by_id=current_by_id,
                expected_city_id=canonical.city_id,
                allowed_types=set(canonical.place_types),
                issues=issues,
            )
            identities.append(
                _project_one_identity(
                    master,
                    place_id=canonical.canonical_place_id,
                    master_place_id=canonical.active_legacy_place_id,
                    entity_type=canonical.primary_type,
                    city_id=canonical.city_id,
                    retired=False,
                    current_by_id=current_by_id,
                    external_identities=external_identities,
                    issues=issues,
                )
            )
        for retired_id in canonical.legacy_place_ids:
            if retired_id == canonical.active_legacy_place_id:
                continue
            retirement = retirement_by_id.get(retired_id)
            if retirement is None:
                raise ExistingIdentityProjectionError(
                    f"manifest is missing retirement metadata for {retired_id}"
                )
            identities.append(
                _project_one_identity(
                    master,
                    place_id=retired_id,
                    master_place_id=retired_id,
                    entity_type=retirement.entity_type,
                    city_id=retirement.city_id,
                    retired=True,
                    current_by_id=current_by_id,
                    external_identities=external_identities,
                    issues=issues,
                )
            )

    actual_ids = {item.place_id for item in identities}
    if actual_ids != expected_ids:
        raise ExistingIdentityProjectionError(
            "projection does not exactly cover active and retired manifest IDs"
        )
    _reject_overlay_token_conflicts(
        identities,
        overlays=overlays,
    )
    identities.sort(key=lambda item: (item.place_id, item.retired))
    issues = _unique_sorted_issues(issues)
    payload = _projection_payload(identities, issues)
    return ExistingIdentityProjection(
        projection_hash=stable_sha256(payload),
        identities=identities,
        quarantined_inputs=issues,
    )


def apply_review_corrections_to_identity_projection(
    projection: ExistingIdentityProjection,
    overlay: CanonicalReviewCorrectionOverlay,
) -> ExistingIdentityProjection:
    """Apply REVIEW field corrections to the replacement duplicate corpus.

    Identity state is deliberately unchanged. The corrected name/contact/geo
    facts only make replacement validation use the same source-backed values
    as canonical dataset materialization.
    """

    corrections = {
        correction.place_id: correction
        for group in overlay.groups
        for correction in group.corrections
    }
    projected_ids = {item.place_id for item in projection.identities}
    missing = sorted(set(corrections) - projected_ids)
    if missing:
        raise ExistingIdentityProjectionError(
            "review correction overlay references identities outside the "
            "projection: " + ", ".join(missing)
        )

    identities: list[ProjectedExistingCanonicalIdentity] = []
    for identity in projection.identities:
        correction = corrections.get(identity.place_id)
        if correction is None:
            identities.append(identity)
            continue
        updates: dict[str, object] = {}
        for field in ("name", "address", "phone", "website_url", "location"):
            if field in correction.corrected_fields:
                value = getattr(correction, field)
                if value is not None:
                    updates[field] = value
        identities.append(identity.model_copy(update=updates, deep=True))

    identities.sort(key=lambda item: (item.place_id, item.retired))
    values = {
        "identities": identities,
        "quarantined_inputs": projection.quarantined_inputs,
        "review_correction_overlay_id": overlay.overlay_id,
        "review_correction_overlay_hash": overlay.overlay_hash,
    }
    return ExistingIdentityProjection(
        projection_hash=stable_sha256(_projection_payload(**values)),
        **values,
    )


def _load_optional_artifacts(
    directory: str | Path | None,
    model: type[CurrentPlaceSnapshot] | type[ExternalEntityMapping],
    input_kind: ProjectionInputKind,
    issues: list[ProjectionInputIssue],
) -> list[_LoadedArtifact]:
    if directory is None:
        return []
    root = Path(directory)
    if not root.exists():
        return []
    if not root.is_dir():
        issues.append(
            ProjectionInputIssue(
                input_kind=input_kind,
                relative_path=root.name or str(root),
                code=ProjectionIssueCode.INPUT_NOT_DIRECTORY,
                message="optional current-state input is not a directory",
            )
        )
        return []

    loaded: list[_LoadedArtifact] = []
    for path in sorted(root.rglob("*.json"), key=lambda item: item.as_posix()):
        relative_path = path.relative_to(root).as_posix()
        try:
            content = path.read_bytes()
        except OSError as error:
            issues.append(
                ProjectionInputIssue(
                    input_kind=input_kind,
                    relative_path=relative_path,
                    code=ProjectionIssueCode.MALFORMED_INPUT,
                    message=f"cannot read input: {error}",
                )
            )
            continue
        input_hash = hashlib.sha256(content).hexdigest()
        try:
            value = model.model_validate_json(content)
        except (TypeError, ValueError) as error:
            issues.append(
                ProjectionInputIssue(
                    input_kind=input_kind,
                    relative_path=relative_path,
                    code=ProjectionIssueCode.MALFORMED_INPUT,
                    input_hash=input_hash,
                    message=f"schema validation failed: {error}",
                )
            )
            continue
        loaded.append(
            _LoadedArtifact(
                relative_path=relative_path,
                input_hash=input_hash,
                value=value,
            )
        )
    return loaded


def _index_current_places(
    artifacts: list[_LoadedArtifact],
    issues: list[ProjectionInputIssue],
) -> dict[str, _LoadedArtifact]:
    result: dict[str, _LoadedArtifact] = {}
    for artifact in artifacts:
        value = artifact.value
        assert isinstance(value, CurrentPlaceSnapshot)
        previous = result.get(value.place_id)
        if previous is None:
            result[value.place_id] = artifact
            continue
        issues.append(
            ProjectionInputIssue(
                input_kind=ProjectionInputKind.CURRENT_PLACE,
                relative_path=artifact.relative_path,
                code=ProjectionIssueCode.DUPLICATE_RECORD,
                input_hash=artifact.input_hash,
                message=f"duplicate current place_id {value.place_id!r} was excluded",
            )
        )
    return result


def _index_google_mappings(
    artifacts: list[_LoadedArtifact],
    issues: list[ProjectionInputIssue],
) -> dict[str, list[_LoadedArtifact]]:
    result: dict[str, list[_LoadedArtifact]] = {}
    seen_mapping_ids: set[str] = set()
    for artifact in artifacts:
        value = artifact.value
        assert isinstance(value, ExternalEntityMapping)
        if value.mapping_id in seen_mapping_ids:
            issues.append(
                ProjectionInputIssue(
                    input_kind=ProjectionInputKind.GOOGLE_MAPPING,
                    relative_path=artifact.relative_path,
                    code=ProjectionIssueCode.DUPLICATE_RECORD,
                    input_hash=artifact.input_hash,
                    message=f"duplicate mapping_id {value.mapping_id!r} was excluded",
                )
            )
            continue
        seen_mapping_ids.add(value.mapping_id)
        result.setdefault(value.entity_id, []).append(artifact)
    return result


def _project_one_identity(
    master: CanonicalMasterLoad,
    *,
    place_id: str,
    master_place_id: str,
    entity_type: EntityType,
    city_id: str,
    retired: bool,
    current_by_id: dict[str, _LoadedArtifact],
    external_identities: list[CandidateExternalIdentity],
    issues: list[ProjectionInputIssue],
) -> ProjectedExistingCanonicalIdentity:
    raw_source = master.raw_record(master_place_id)
    if raw_source is None:
        raise ExistingIdentityProjectionError(
            f"manifest identity has no verified master record: {master_place_id}"
        )
    raw = raw_source.raw_record
    name = _non_empty_text(raw.get("name"))
    if name is None:
        raise ExistingIdentityProjectionError(
            f"verified master record has no name: {master_place_id}"
        )
    phone = _master_phone(raw)
    website_url = _master_website(raw)
    location = _master_location(raw)
    address = _non_empty_text(raw.get("address"))

    current_artifact = current_by_id.get(master_place_id)
    if current_artifact is not None:
        current = current_artifact.value
        assert isinstance(current, CurrentPlaceSnapshot)
        city_matches = current.city_id in {None, city_id}
        if current.entity_type is not entity_type or not city_matches:
            issues.append(
                ProjectionInputIssue(
                    input_kind=ProjectionInputKind.CURRENT_PLACE,
                    relative_path=current_artifact.relative_path,
                    code=ProjectionIssueCode.IDENTITY_MISMATCH,
                    input_hash=current_artifact.input_hash,
                    message=(
                        f"current identity for {master_place_id!r} does not match "
                        "the verified entity/city slot"
                    ),
                )
            )
        else:
            name = current.name
            phone = current.phone or phone
            website_url = current.website_url or website_url
            location = current.location or location
            address = current.address or address

    return ProjectedExistingCanonicalIdentity(
        place_id=place_id,
        entity_type=entity_type,
        city_id=city_id,
        name=name,
        phone=phone,
        website_url=website_url,
        location=location,
        external_identities=external_identities,
        retired=retired,
        address=address,
    )


def _confirmed_google_identities(
    member_ids: Iterable[str],
    *,
    mappings_by_id: dict[str, list[_LoadedArtifact]],
    current_by_id: dict[str, _LoadedArtifact],
    expected_city_id: str,
    allowed_types: set[EntityType],
    issues: list[ProjectionInputIssue],
) -> list[CandidateExternalIdentity]:
    by_token: dict[str, CandidateExternalIdentity] = {}
    for member_id in member_ids:
        for artifact in mappings_by_id.get(member_id, []):
            mapping = artifact.value
            assert isinstance(mapping, ExternalEntityMapping)
            if (
                mapping.source_id != _GOOGLE_SOURCE_ID
                or mapping.status is not MappingStatus.CONFIRMED
            ):
                continue
            mapping_city = mapping.attributes.get("city_id")
            if (
                mapping.entity_type not in allowed_types
                or (
                    isinstance(mapping_city, str)
                    and mapping_city
                    and mapping_city != expected_city_id
                )
            ):
                issues.append(
                    ProjectionInputIssue(
                        input_kind=ProjectionInputKind.GOOGLE_MAPPING,
                        relative_path=artifact.relative_path,
                        code=ProjectionIssueCode.IDENTITY_MISMATCH,
                        input_hash=artifact.input_hash,
                        message=(
                            f"confirmed mapping for {member_id!r} does not match "
                            "the canonical entity/city identity"
                        ),
                    )
                )
                continue

            urls: list[str] = []
            if mapping.external_url is not None:
                urls.append(str(mapping.external_url))
            current_artifact = current_by_id.get(member_id)
            if current_artifact is not None:
                current = current_artifact.value
                assert isinstance(current, CurrentPlaceSnapshot)
                provenance = current.provenance
                if (
                    provenance.mapping_id == mapping.mapping_id
                    and provenance.source_id == _GOOGLE_SOURCE_ID
                    and provenance.source_url is not None
                ):
                    urls.append(str(provenance.source_url))

            for url in sorted(set(urls)):
                try:
                    _, token = _google_maps_identity(url)
                except ValueError:
                    continue
                if token is None:
                    continue
                token_key = token.casefold()
                proposed = CandidateExternalIdentity(
                    source_id=_GOOGLE_SOURCE_ID,
                    external_id=token,
                    external_url=url,
                )
                current_value = by_token.get(token_key)
                if current_value is None or str(proposed.external_url) < str(
                    current_value.external_url
                ):
                    by_token[token_key] = proposed
    return [by_token[key] for key in sorted(by_token)]


def _master_location(raw: dict[str, object]) -> GeoPoint | None:
    coordinates = raw.get("coordinates")
    if not isinstance(coordinates, dict):
        return None
    latitude = coordinates.get("lat")
    longitude = coordinates.get("lng")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return None
    try:
        return GeoPoint(
            latitude=float(latitude),
            longitude=float(longitude),
            accuracy="verified_master",
            source="verified-master-data",
        )
    except ValueError:
        return None


def _master_phone(raw: dict[str, object]) -> str | None:
    direct = _non_empty_text(raw.get("phone")) or _non_empty_text(
        raw.get("phone_number")
    )
    if direct:
        return direct
    contact = raw.get("contact")
    return _non_empty_text(contact.get("phone")) if isinstance(contact, dict) else None


def _master_website(raw: dict[str, object]) -> str | None:
    candidates = [raw.get("website_url"), raw.get("website")]
    contact = raw.get("contact")
    if isinstance(contact, dict):
        candidates.extend([contact.get("website_url"), contact.get("website")])
    for candidate in candidates:
        value = _non_empty_text(candidate)
        if value and value.casefold().startswith(("http://", "https://")):
            return value
    return None


def _non_empty_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _unique_sorted_issues(
    issues: Iterable[ProjectionInputIssue],
) -> list[ProjectionInputIssue]:
    unique = {
        stable_sha256(item.model_dump(mode="json")): item for item in issues
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item.input_kind.value,
            item.relative_path,
            item.code.value,
            item.message,
        ),
    )


def _approval_payload(
    *,
    allocated_place_id: str,
    replacement_of: str,
    candidate_detail: CandidateDetailStage,
    google_identity: CandidateExternalIdentity,
    approval_method: ReplacementApprovalMethod,
    reviewer: str,
    approved_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "allocated_place_id": allocated_place_id,
        "replacement_of": replacement_of,
        "candidate_detail": candidate_detail.model_dump(mode="json"),
        "google_identity": google_identity.model_dump(mode="json"),
        "approval_method": approval_method.value,
        "reviewer": reviewer,
        "approved_at": approved_at.isoformat(),
    }


class ApprovedReplacement(NexTripModel):
    """Human approval binding one PASS detail to one never-reused place ID."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocated_place_id: str = Field(min_length=1)
    replacement_of: str = Field(min_length=1)
    candidate_detail: CandidateDetailStage
    google_identity: CandidateExternalIdentity
    approval_method: ReplacementApprovalMethod = ReplacementApprovalMethod.HUMAN
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime

    @classmethod
    def from_candidate_detail(
        cls,
        detail: CandidateDetailStage,
        *,
        allocated_place_id: str,
        reviewer: str,
        approved_at: datetime,
        approval_method: ReplacementApprovalMethod = ReplacementApprovalMethod.HUMAN,
    ) -> ApprovedReplacement:
        validated_detail = CandidateDetailStage.model_validate(
            detail.model_dump(mode="python")
        )
        google_identity = _required_google_identity(validated_detail)
        replacement_of = validated_detail.vacancy.retired_place_id
        values = _approval_payload(
            allocated_place_id=allocated_place_id,
            replacement_of=replacement_of,
            candidate_detail=validated_detail,
            google_identity=google_identity,
            approval_method=approval_method,
            reviewer=reviewer,
            approved_at=approved_at,
        )
        approval_hash = stable_sha256(values)
        return cls(
            approval_id=f"replacement-approval-{approval_hash[:20]}",
            approval_hash=approval_hash,
            **values,
        )

    @model_validator(mode="after")
    def validate_approval(self) -> ApprovedReplacement:
        detail = self.candidate_detail
        candidate = detail.candidate
        if detail.validation.status is not CandidateDisposition.PASS:
            raise ValueError("only a PASS candidate detail can be approved")
        if self.replacement_of != detail.vacancy.retired_place_id:
            raise ValueError("replacement_of must match the candidate vacancy")
        if candidate.entity_type is not detail.vacancy.entity_type:
            raise ValueError("candidate type must match the vacant quota slot")
        if candidate.city_id != detail.vacancy.city_id:
            raise ValueError("candidate city must match the vacant quota slot")
        _validate_allocated_place_id(
            self.allocated_place_id,
            entity_type=detail.vacancy.entity_type,
            city_id=detail.vacancy.city_id,
        )
        if self.allocated_place_id == self.replacement_of:
            raise ValueError("a retired ID can never be reused for its replacement")
        required_google = _required_google_identity(detail)
        if self.google_identity != required_google:
            raise ValueError("approved Google identity does not match candidate detail")
        if self.approved_at < detail.observed_at:
            raise ValueError("approved_at cannot precede candidate observation")
        payload = _approval_payload(
            allocated_place_id=self.allocated_place_id,
            replacement_of=self.replacement_of,
            candidate_detail=detail,
            google_identity=self.google_identity,
            approval_method=self.approval_method,
            reviewer=self.reviewer,
            approved_at=self.approved_at,
        )
        expected_hash = stable_sha256(payload)
        if self.approval_hash != expected_hash:
            raise ValueError("approval_hash does not match approval content")
        if self.approval_id != f"replacement-approval-{expected_hash[:20]}":
            raise ValueError("approval_id does not match approval_hash")
        return self


def _required_google_identity(
    detail: CandidateDetailStage,
) -> CandidateExternalIdentity:
    candidate = detail.candidate
    if candidate.location is None or candidate.location.source != _GOOGLE_SOURCE_ID:
        raise ReplacementApprovalError(
            "approved replacement requires Google Maps sourced coordinates"
        )
    valid_by_token: dict[str, CandidateExternalIdentity] = {}
    for identity in candidate.external_identities:
        if (
            identity.source_id != _GOOGLE_SOURCE_ID
            or identity.external_id is None
            or identity.external_url is None
        ):
            continue
        try:
            _, token = _google_maps_identity(str(identity.external_url))
        except ValueError:
            continue
        if token is None or token.casefold() != identity.external_id.casefold():
            continue
        valid_by_token.setdefault(token.casefold(), identity)
    if len(valid_by_token) != 1:
        raise ReplacementApprovalError(
            "approved replacement requires exactly one matching stable Google token"
        )
    return valid_by_token[next(iter(sorted(valid_by_token)))]


def _validate_allocated_place_id(
    place_id: str,
    *,
    entity_type: EntityType,
    city_id: str,
) -> None:
    match = _PLACE_ID_PATTERN.fullmatch(place_id)
    if match is None:
        raise ReplacementApprovalError(f"invalid allocated place ID: {place_id!r}")
    expected_city = _CITY_PREFIX.get(city_id)
    if expected_city is None:
        raise ReplacementApprovalError(f"unsupported candidate city: {city_id!r}")
    if (
        match.group("entity") != _ENTITY_PREFIX[entity_type]
        or match.group("city") != expected_city
    ):
        raise ReplacementApprovalError(
            "allocated place ID does not preserve vacancy entity/city slot"
        )


class ReplacementCoordinates(NexTripModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class ReplacementSource(NexTripModel):
    source_name: Literal["google-maps-web"] = "google-maps-web"
    url: HttpUrl
    crawled_at: AwareDatetime


class ReplacementProvenance(NexTripModel):
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_detail_id: str = Field(min_length=1)
    candidate_detail_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_key: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    source_record_ids: list[str] = Field(min_length=1)
    observation_ids: list[str] = Field(min_length=1)
    vacancy_id: str = Field(min_length=1)
    replacement_of: str = Field(min_length=1)
    google_external_id: str = Field(min_length=1)
    google_external_url: HttpUrl
    approval_method: ReplacementApprovalMethod
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime


class MaterializedReplacementRecord(NexTripModel):
    """A new master-shaped record; original verified files remain untouched."""

    id: str = Field(min_length=1)
    entity_type: EntityType
    primary_type: EntityType
    place_types: list[EntityType] = Field(min_length=1)
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    city_id: str = Field(min_length=1)
    address: str | None = None
    coordinates: ReplacementCoordinates
    phone: str | None = None
    website_url: HttpUrl | None = None
    tags: list[str] = Field(default_factory=list)
    source: ReplacementSource
    embedding_text: str = Field(min_length=1)
    verification_status: VerificationStatus
    last_updated: AwareDatetime
    provenance: ReplacementProvenance

    @model_validator(mode="after")
    def validate_materialized_record(self) -> MaterializedReplacementRecord:
        if self.primary_type is not self.entity_type:
            raise ValueError("primary_type must match entity_type")
        if self.place_types != [self.primary_type]:
            raise ValueError("a replacement starts with exactly one primary place type")
        expected_verification = (
            VerificationStatus.HUMAN_VERIFIED
            if self.provenance.approval_method is ReplacementApprovalMethod.HUMAN
            else VerificationStatus.AUTO_VERIFIED
        )
        if self.verification_status is not expected_verification:
            raise ValueError(
                "verification_status must match the replacement approval method"
            )
        if self.id == self.provenance.replacement_of:
            raise ValueError("materialized replacement cannot reuse a retired ID")
        return self

    def to_legacy_place_slot(self) -> LegacyPlaceSlot:
        """Project this overlay into the input contract used by the manifest."""

        return LegacyPlaceSlot(
            legacy_place_id=self.id,
            city_id=self.city_id,
            primary_type=self.primary_type,
            secondary_types=[
                item for item in self.place_types if item is not self.primary_type
            ],
            tags=self.tags,
        )


def _validate_projection_overlays(
    replacements: Iterable[MaterializedReplacementRecord],
    *,
    manifest: CanonicalIdentityManifest,
) -> list[MaterializedReplacementRecord]:
    records = [
        MaterializedReplacementRecord.model_validate_json(item.model_dump_json())
        for item in replacements
    ]
    try:
        _reject_incoming_duplicates(records)
    except ReplacementWriteConflictError as error:
        raise ExistingIdentityProjectionError(
            f"invalid approved replacement overlay: {error}"
        ) from error

    identity_by_id = {
        item.canonical_place_id: item for item in manifest.identities
    }
    filled_by_replacement = {
        item.replacement_place_id: item
        for item in manifest.vacancies
        if item.status is VacancyStatus.FILLED
        and item.replacement_place_id is not None
    }
    for record in records:
        identity = identity_by_id.get(record.id)
        if (
            identity is None
            or identity.active_legacy_place_id != record.id
            or identity.legacy_place_ids != [record.id]
            or identity.primary_type is not record.primary_type
            or identity.city_id != record.city_id
        ):
            raise ExistingIdentityProjectionError(
                "approved replacement overlay is not an active matching manifest "
                f"identity: {record.id}"
            )
        vacancy = filled_by_replacement.get(record.id)
        if (
            vacancy is None
            or vacancy.retired_place_id != record.provenance.replacement_of
            or vacancy.vacancy_id != record.provenance.vacancy_id
            or vacancy.entity_type is not record.entity_type
            or vacancy.city_id != record.city_id
        ):
            raise ExistingIdentityProjectionError(
                "approved replacement overlay does not match its filled manifest "
                f"vacancy: {record.id}"
            )
        _overlay_google_identity(record)
    return sorted(records, key=lambda item: item.id)


def _overlay_google_identity(
    record: MaterializedReplacementRecord,
) -> CandidateExternalIdentity:
    provenance = record.provenance
    external_url = str(provenance.google_external_url)
    if str(record.source.url) != external_url:
        raise ExistingIdentityProjectionError(
            f"replacement source URL does not match provenance: {record.id}"
        )
    try:
        _, token = _google_maps_identity(external_url)
    except ValueError as error:
        raise ExistingIdentityProjectionError(
            f"replacement has an invalid Google Maps URL: {record.id}"
        ) from error
    if (
        token is None
        or token.casefold() != provenance.google_external_id.casefold()
    ):
        raise ExistingIdentityProjectionError(
            f"replacement Google token does not match its URL: {record.id}"
        )
    return CandidateExternalIdentity(
        source_id=_GOOGLE_SOURCE_ID,
        external_id=provenance.google_external_id,
        external_url=provenance.google_external_url,
    )


def _project_overlay_identity(
    record: MaterializedReplacementRecord,
) -> ProjectedExistingCanonicalIdentity:
    return ProjectedExistingCanonicalIdentity(
        place_id=record.id,
        entity_type=record.primary_type,
        city_id=record.city_id,
        name=record.name,
        phone=record.phone,
        website_url=record.website_url,
        location=GeoPoint(
            latitude=record.coordinates.lat,
            longitude=record.coordinates.lng,
            accuracy="approved_replacement",
            source=_GOOGLE_SOURCE_ID,
            verified_at=record.last_updated,
        ),
        external_identities=[_overlay_google_identity(record)],
        retired=False,
        address=record.address,
    )


def _reject_overlay_token_conflicts(
    identities: Iterable[ProjectedExistingCanonicalIdentity],
    *,
    overlays: Iterable[MaterializedReplacementRecord],
) -> None:
    overlay_token_owner = {
        item.provenance.google_external_id.casefold(): item.id for item in overlays
    }
    if not overlay_token_owner:
        return
    for identity in identities:
        for external in identity.external_identities:
            if external.source_id != _GOOGLE_SOURCE_ID or external.external_id is None:
                continue
            owner = overlay_token_owner.get(external.external_id.casefold())
            if owner is not None and owner != identity.place_id:
                raise ExistingIdentityProjectionError(
                    "approved replacement Google token already belongs to existing "
                    f"identity {identity.place_id}: {external.external_id}"
                )


def materialize_master_record(
    approval: ApprovedReplacement,
) -> MaterializedReplacementRecord:
    """Return a new record and never mutate the detail or original master input."""

    approved = ApprovedReplacement.model_validate_json(
        approval.model_dump_json()
    )
    detail = approved.candidate_detail
    candidate = detail.candidate
    location = candidate.location
    assert location is not None  # Enforced by ApprovedReplacement validation.
    external_id = approved.google_identity.external_id
    external_url = approved.google_identity.external_url
    assert external_id is not None and external_url is not None
    city = _CITY_NAME[detail.vacancy.city_id]
    return MaterializedReplacementRecord(
        id=approved.allocated_place_id,
        entity_type=candidate.entity_type,
        primary_type=candidate.entity_type,
        place_types=[candidate.entity_type],
        name=candidate.name,
        city=city,
        city_id=candidate.city_id,
        address=None,
        coordinates=ReplacementCoordinates(
            lat=location.latitude,
            lng=location.longitude,
        ),
        phone=candidate.phone,
        website_url=candidate.website_url,
        tags=["canonical_replacement"],
        source=ReplacementSource(
            url=external_url,
            crawled_at=detail.observed_at,
        ),
        embedding_text=(
            f"{candidate.name} | {city} | {candidate.entity_type.value}"
        ),
        verification_status=(
            VerificationStatus.HUMAN_VERIFIED
            if approved.approval_method is ReplacementApprovalMethod.HUMAN
            else VerificationStatus.AUTO_VERIFIED
        ),
        last_updated=approved.approved_at,
        provenance=ReplacementProvenance(
            approval_id=approved.approval_id,
            approval_hash=approved.approval_hash,
            candidate_detail_id=detail.detail_id,
            candidate_detail_hash=detail.detail_hash,
            candidate_key=candidate.candidate_key,
            source_ids=[_GOOGLE_SOURCE_ID],
            source_record_ids=[detail.source_record_id],
            observation_ids=[detail.observation_id],
            vacancy_id=detail.vacancy.vacancy_id,
            replacement_of=approved.replacement_of,
            google_external_id=external_id,
            google_external_url=external_url,
            approval_method=approved.approval_method,
            reviewer=approved.reviewer,
            approved_at=approved.approved_at,
        ),
    )


class ApprovedReplacementWriter:
    """Deterministically publish approved records with global uniqueness gates."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)
        self.records_root = self.output_root / "records"
        self._lock_destination = self.output_root / "approved-replacements"

    def destination_for(self, record: MaterializedReplacementRecord) -> Path:
        return (
            self.records_root
            / f"entity={quote(record.entity_type.value, safe='-_.')}"
            / f"city={quote(record.city_id, safe='-_.')}"
            / f"place={quote(record.id, safe='-_.')}.json"
        )

    def write(self, approval: ApprovedReplacement) -> Path:
        return self.write_many([approval])[0]

    def write_many(
        self,
        approvals: Iterable[ApprovedReplacement],
    ) -> list[Path]:
        records = [materialize_master_record(item) for item in approvals]
        _reject_incoming_duplicates(records)
        if not records:
            return []

        with destination_file_lock(self._lock_destination):
            existing = self._read_existing()
            existing_by_id = {item.id: item for item in existing}
            existing_by_token = {
                item.provenance.google_external_id.casefold(): item for item in existing
            }
            for record in records:
                same_id = existing_by_id.get(record.id)
                if same_id is not None and same_id != record:
                    raise ReplacementWriteConflictError(
                        f"allocated place ID already exists: {record.id}"
                    )
                token_key = record.provenance.google_external_id.casefold()
                same_token = existing_by_token.get(token_key)
                if same_token is not None and same_token.id != record.id:
                    raise ReplacementWriteConflictError(
                        "Google external token already belongs to "
                        f"{same_token.id}: {record.provenance.google_external_id}"
                    )

            destinations: list[Path] = []
            for record in records:
                destination = self.destination_for(record)
                destinations.append(destination)
                if existing_by_id.get(record.id) == record:
                    continue
                self._atomic_write(destination, record)
                existing_by_id[record.id] = record
                existing_by_token[
                    record.provenance.google_external_id.casefold()
                ] = record
            return destinations

    def _read_existing(self) -> list[MaterializedReplacementRecord]:
        if not self.records_root.exists():
            return []
        records: list[MaterializedReplacementRecord] = []
        ids: set[str] = set()
        tokens: set[str] = set()
        for path in sorted(
            self.records_root.rglob("*.json"), key=lambda item: item.as_posix()
        ):
            try:
                record = MaterializedReplacementRecord.model_validate_json(
                    path.read_bytes()
                )
            except (OSError, TypeError, ValueError) as error:
                raise ReplacementWriteConflictError(
                    f"cannot validate existing replacement {path}: {error}"
                ) from error
            token = record.provenance.google_external_id.casefold()
            if record.id in ids:
                raise ReplacementWriteConflictError(
                    f"duplicate existing allocated place ID: {record.id}"
                )
            if token in tokens:
                raise ReplacementWriteConflictError(
                    "duplicate existing Google external token: "
                    f"{record.provenance.google_external_id}"
                )
            ids.add(record.id)
            tokens.add(token)
            records.append(record)
        return sorted(records, key=lambda item: item.id)

    @staticmethod
    def _atomic_write(
        destination: Path,
        record: MaterializedReplacementRecord,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = (
            json.dumps(
                record.model_dump(mode="json"),
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


def _reject_incoming_duplicates(
    records: Iterable[MaterializedReplacementRecord],
) -> None:
    place_ids: set[str] = set()
    tokens: set[str] = set()
    for record in records:
        if record.id in place_ids:
            raise ReplacementWriteConflictError(
                f"duplicate allocated place ID in batch: {record.id}"
            )
        place_ids.add(record.id)
        token = record.provenance.google_external_id.casefold()
        if token in tokens:
            raise ReplacementWriteConflictError(
                "duplicate Google external token in batch: "
                f"{record.provenance.google_external_id}"
            )
        tokens.add(token)


def load_approved_replacements(
    output_root: str | Path,
) -> list[MaterializedReplacementRecord]:
    """Reload and globally revalidate a deterministic replacement overlay."""

    return ApprovedReplacementWriter(output_root)._read_existing()


def project_approved_replacement_slots(
    replacements: Iterable[MaterializedReplacementRecord],
) -> list[LegacyPlaceSlot]:
    """Return sorted manifest-input slots without rewriting verified master."""

    validated = [
        MaterializedReplacementRecord.model_validate_json(item.model_dump_json())
        for item in replacements
    ]
    _reject_incoming_duplicates(validated)
    return sorted(
        (item.to_legacy_place_slot() for item in validated),
        key=lambda item: item.legacy_place_id,
    )


def load_approved_replacement_slots(
    output_root: str | Path,
) -> list[LegacyPlaceSlot]:
    """Reload an overlay and project it directly into canonical manifest slots."""

    return project_approved_replacement_slots(
        load_approved_replacements(output_root)
    )
