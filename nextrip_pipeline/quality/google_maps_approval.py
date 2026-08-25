from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
)
from nextrip_pipeline.canonical.models import stable_sha256
from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.google_maps_identity import (
    google_maps_place_coordinates,
    google_maps_stable_external_ids,
    google_maps_stable_place_url,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    GoogleMapsPlaceObservation,
    MappingStatus,
)

from .google_maps_mapping import (
    GoogleMapsMappingResolution,
    ImmutableQualityModel,
    MappingResolutionStatus,
)
from .storage import CurrentGoogleMapsMappingWriter


_GOOGLE_SOURCE_ID = "google-maps-web"
_ACTIVE_MAPPING_STATUSES = {
    MappingStatus.CONFIRMED,
    MappingStatus.AUTO_MATCHED,
    MappingStatus.PENDING_REVIEW,
}
_MAPPING_STABLE_ID_ATTRIBUTE_KEYS = (
    "canonical_google_maps_url",
    "google_external_id",
    "google_place_id",
    "stable_external_id",
)


class GoogleMapsMappingApprovalError(ValueError):
    """Raised when reviewed Google evidence cannot safely bind a place."""


def _approval_payload(
    *,
    schema_version: str,
    canonical_dataset_id: str,
    canonical_dataset_hash: str,
    canonical_dataset_path: str,
    canonical_dataset_file_sha256: str,
    canonical_record_hash: str,
    place_id: str,
    entity_type: EntityType,
    city_id: str,
    city: str,
    mapping_id: str,
    external_id: str,
    google_detail_url: str,
    google_reference_name: str,
    google_reference_address: str | None,
    google_latitude: float,
    google_longitude: float,
    google_coordinate_source: str,
    google_coordinate_accuracy: str | None,
    observation_id: str,
    source_record_id: str,
    run_id: str,
    observed_at: datetime,
    observation_path: str,
    observation_file_sha256: str,
    resolver_version: str,
    resolution_status: MappingResolutionStatus,
    resolution_score: float,
    resolution_reason_codes: tuple[str, ...],
    resolution_evidence_hash: str,
    resolved_at: datetime,
    resolution_path: str,
    resolution_file_sha256: str,
    decision_id: str,
    decision_status: GoogleMapsDecisionStatus,
    decision_reason_codes: tuple[str, ...],
    decided_at: datetime,
    decision_path: str,
    decision_file_sha256: str,
    reviewer: str,
    approved_at: datetime,
    previous_external_id: str | None,
    previous_mapping_file_sha256: str | None,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "canonical_dataset_id": canonical_dataset_id,
        "canonical_dataset_hash": canonical_dataset_hash,
        "canonical_dataset_path": canonical_dataset_path,
        "canonical_dataset_file_sha256": canonical_dataset_file_sha256,
        "canonical_record_hash": canonical_record_hash,
        "place_id": place_id,
        "entity_type": entity_type.value,
        "city_id": city_id,
        "city": city,
        "mapping_id": mapping_id,
        "source_id": _GOOGLE_SOURCE_ID,
        "external_id": external_id,
        "google_detail_url": str(HttpUrl(google_detail_url)),
        "google_reference_name": google_reference_name,
        "google_reference_address": google_reference_address,
        "google_latitude": google_latitude,
        "google_longitude": google_longitude,
        "google_coordinate_source": google_coordinate_source,
        "google_coordinate_accuracy": google_coordinate_accuracy,
        "observation_id": observation_id,
        "source_record_id": source_record_id,
        "run_id": run_id,
        "observed_at": observed_at.isoformat(),
        "observation_path": observation_path,
        "observation_file_sha256": observation_file_sha256,
        "resolver_version": resolver_version,
        "resolution_status": resolution_status.value,
        "resolution_score": resolution_score,
        "resolution_reason_codes": list(resolution_reason_codes),
        "resolution_evidence_hash": resolution_evidence_hash,
        "resolved_at": resolved_at.isoformat(),
        "resolution_path": resolution_path,
        "resolution_file_sha256": resolution_file_sha256,
        "decision_id": decision_id,
        "decision_status": decision_status.value,
        "decision_reason_codes": list(decision_reason_codes),
        "decided_at": decided_at.isoformat(),
        "decision_path": decision_path,
        "decision_file_sha256": decision_file_sha256,
        "reviewer": reviewer,
        "approved_at": approved_at.isoformat(),
        "previous_external_id": previous_external_id,
        "previous_mapping_file_sha256": previous_mapping_file_sha256,
    }


class GoogleMapsMappingApproval(ImmutableQualityModel):
    """Content-addressed approval pinned to all input artifact bytes."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    approval_id: str = Field(min_length=1)
    approval_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_dataset_id: str = Field(min_length=1)
    canonical_dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_dataset_path: str = Field(min_length=1)
    canonical_dataset_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_record_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    city_id: str = Field(min_length=1)
    city: str = Field(min_length=1)
    mapping_id: str = Field(min_length=1)
    source_id: Literal["google-maps-web"] = "google-maps-web"
    external_id: str = Field(min_length=1)
    google_detail_url: HttpUrl
    google_reference_name: str = Field(min_length=1)
    google_reference_address: str | None = None
    google_latitude: float = Field(ge=-90, le=90)
    google_longitude: float = Field(ge=-180, le=180)
    google_coordinate_source: Literal["google-maps-web"] = "google-maps-web"
    google_coordinate_accuracy: str | None = None
    observation_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    observation_path: str = Field(min_length=1)
    observation_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolver_version: str = Field(min_length=1)
    resolution_status: MappingResolutionStatus
    resolution_score: float = Field(ge=0, le=1)
    resolution_reason_codes: tuple[str, ...]
    resolution_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    resolved_at: AwareDatetime
    resolution_path: str = Field(min_length=1)
    resolution_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision_id: str = Field(min_length=1)
    decision_status: GoogleMapsDecisionStatus
    decision_reason_codes: tuple[str, ...]
    decided_at: AwareDatetime
    decision_path: str = Field(min_length=1)
    decision_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1)
    approved_at: AwareDatetime
    previous_external_id: str | None = None
    previous_mapping_file_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )

    @model_validator(mode="after")
    def validate_content_address(self) -> GoogleMapsMappingApproval:
        if self.mapping_id != f"google-maps-{self.place_id}":
            raise ValueError("mapping_id does not match approved place_id")
        if self.resolution_status is MappingResolutionStatus.AUTO_CONFIRM:
            raise ValueError("AUTO_CONFIRM mapping does not require human approval")
        if self.decision_status is GoogleMapsDecisionStatus.PASS:
            raise ValueError("PASS decision does not require human approval")
        if self.approved_at < max(
            self.observed_at,
            self.resolved_at,
            self.decided_at,
        ):
            raise ValueError("approved_at cannot precede reviewed evidence")
        if (
            self.previous_mapping_file_sha256 is not None
            and self.previous_external_id is None
        ):
            raise ValueError("previous mapping hash requires previous external_id")
        detail_url = google_maps_stable_place_url(str(self.google_detail_url))
        stable_ids = google_maps_stable_external_ids(detail_url)
        if detail_url is None or stable_ids != (self.external_id,):
            raise ValueError("approval requires one matching stable Google detail ID")
        accuracy = (self.google_coordinate_accuracy or "").casefold()
        if "fallback" in accuracy or "verified_master" in accuracy:
            raise ValueError("approval cannot contain fallback coordinates")

        payload = _approval_payload(
            schema_version=self.schema_version,
            canonical_dataset_id=self.canonical_dataset_id,
            canonical_dataset_hash=self.canonical_dataset_hash,
            canonical_dataset_path=self.canonical_dataset_path,
            canonical_dataset_file_sha256=self.canonical_dataset_file_sha256,
            canonical_record_hash=self.canonical_record_hash,
            place_id=self.place_id,
            entity_type=self.entity_type,
            city_id=self.city_id,
            city=self.city,
            mapping_id=self.mapping_id,
            external_id=self.external_id,
            google_detail_url=str(self.google_detail_url),
            google_reference_name=self.google_reference_name,
            google_reference_address=self.google_reference_address,
            google_latitude=self.google_latitude,
            google_longitude=self.google_longitude,
            google_coordinate_source=self.google_coordinate_source,
            google_coordinate_accuracy=self.google_coordinate_accuracy,
            observation_id=self.observation_id,
            source_record_id=self.source_record_id,
            run_id=self.run_id,
            observed_at=self.observed_at,
            observation_path=self.observation_path,
            observation_file_sha256=self.observation_file_sha256,
            resolver_version=self.resolver_version,
            resolution_status=self.resolution_status,
            resolution_score=self.resolution_score,
            resolution_reason_codes=self.resolution_reason_codes,
            resolution_evidence_hash=self.resolution_evidence_hash,
            resolved_at=self.resolved_at,
            resolution_path=self.resolution_path,
            resolution_file_sha256=self.resolution_file_sha256,
            decision_id=self.decision_id,
            decision_status=self.decision_status,
            decision_reason_codes=self.decision_reason_codes,
            decided_at=self.decided_at,
            decision_path=self.decision_path,
            decision_file_sha256=self.decision_file_sha256,
            reviewer=self.reviewer,
            approved_at=self.approved_at,
            previous_external_id=self.previous_external_id,
            previous_mapping_file_sha256=self.previous_mapping_file_sha256,
        )
        expected_hash = stable_sha256(payload)
        if self.approval_hash != expected_hash:
            raise ValueError("approval_hash does not match approval content")
        if self.approval_id != f"google-maps-mapping-approval-{expected_hash[:20]}":
            raise ValueError("approval_id does not match approval_hash")
        return self


class GoogleMapsMappingApprovalWriter:
    """Write immutable content-addressed Google mapping approvals."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, approval: GoogleMapsMappingApproval) -> Path:
        return (
            self.root_directory
            / f"place={quote(approval.place_id, safe='-_.')}"
            / f"approval={quote(approval.approval_id, safe='-_.')}.json"
        )

    def write(self, approval: GoogleMapsMappingApproval) -> Path:
        validated = GoogleMapsMappingApproval.model_validate_json(
            approval.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                current = GoogleMapsMappingApproval.model_validate_json(
                    destination.read_bytes()
                )
                if current == validated:
                    return destination
                raise FileExistsError(
                    f"immutable Google Maps approval already exists: {destination}"
                )
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(validated.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
        return destination


def approve_google_maps_mapping(
    canonical_dataset_path: str | Path,
    observation_path: str | Path,
    resolution_path: str | Path,
    decision_path: str | Path,
    *,
    reviewer: str,
    approval_writer: GoogleMapsMappingApprovalWriter,
    mapping_writer: CurrentGoogleMapsMappingWriter,
    active_mappings: Iterable[ExternalEntityMapping] = (),
    approved_at: datetime | None = None,
) -> tuple[
    GoogleMapsMappingApproval,
    ExternalEntityMapping,
    Path,
    Path,
]:
    """Approve exact reviewed evidence and publish one confirmed mapping.

    Canonical city identity is intentionally retained. Google is authoritative
    only for the provider reference name, detail token, address and coordinate.
    """

    dataset_file = Path(canonical_dataset_path)
    observation_file = Path(observation_path)
    resolution_file = Path(resolution_path)
    decision_file = Path(decision_path)
    dataset_bytes = dataset_file.read_bytes()
    observation_bytes = observation_file.read_bytes()
    resolution_bytes = resolution_file.read_bytes()
    decision_bytes = decision_file.read_bytes()
    dataset = CanonicalActiveDataset.model_validate_json(dataset_bytes)
    observation = GoogleMapsPlaceObservation.model_validate_json(observation_bytes)
    resolution = GoogleMapsMappingResolution.model_validate_json(resolution_bytes)
    decision = GoogleMapsDecision.model_validate_json(decision_bytes)
    reviewed_at = approved_at or datetime.now(timezone.utc)

    record = _require_place_binding(dataset, observation, resolution, decision)
    detail_url = google_maps_stable_place_url(str(observation.source_url))
    stable_ids = google_maps_stable_external_ids(detail_url)
    if detail_url is None or len(stable_ids) != 1:
        raise GoogleMapsMappingApprovalError(
            "approval requires a Google /maps/place/ detail URL with one stable ID"
        )
    external_id = stable_ids[0]
    name = (observation.name or "").strip()
    if not name:
        raise GoogleMapsMappingApprovalError(
            "approval requires the Google reference name"
        )
    location = observation.location
    if location is None or location.source != _GOOGLE_SOURCE_ID:
        raise GoogleMapsMappingApprovalError(
            "approval requires coordinates sourced from google-maps-web"
        )
    accuracy = (location.accuracy or "").casefold()
    if "fallback" in accuracy or "verified_master" in accuracy:
        raise GoogleMapsMappingApprovalError(
            "fallback coordinates cannot be approved as Google coordinates"
        )
    url_coordinates = google_maps_place_coordinates(detail_url)
    if url_coordinates is not None and (
        abs(location.latitude - url_coordinates[0]) > 1e-7
        or abs(location.longitude - url_coordinates[1]) > 1e-7
    ):
        raise GoogleMapsMappingApprovalError(
            "observation coordinates do not match Google place coordinates"
        )

    current_mappings = [*mapping_writer.all(), *active_mappings]
    _reject_cross_owner_token(dataset, current_mappings, record.place_id, external_id)
    current = mapping_writer.get(record.place_id)
    if current is not None:
        if current.source_id != _GOOGLE_SOURCE_ID:
            raise GoogleMapsMappingApprovalError(
                "current target mapping is not owned by google-maps-web"
            )
        if current.mapping_id != resolution.mapping_id:
            raise GoogleMapsMappingApprovalError(
                "current mapping_id does not match reviewed resolution"
            )

    previous_mapping_sha256 = None
    if current is not None:
        previous_mapping_sha256 = hashlib.sha256(
            mapping_writer.path_for(record.place_id).read_bytes()
        ).hexdigest()
    payload = _approval_payload(
        schema_version="1.0.0",
        canonical_dataset_id=dataset.dataset_id,
        canonical_dataset_hash=dataset.dataset_hash,
        canonical_dataset_path=str(dataset_file.resolve()),
        canonical_dataset_file_sha256=_sha256(dataset_bytes),
        canonical_record_hash=record.record_hash,
        place_id=record.place_id,
        entity_type=record.primary_type,
        city_id=record.city_id,
        city=record.city,
        mapping_id=resolution.mapping_id,
        external_id=external_id,
        google_detail_url=detail_url,
        google_reference_name=name,
        google_reference_address=observation.address,
        google_latitude=location.latitude,
        google_longitude=location.longitude,
        google_coordinate_source=location.source,
        google_coordinate_accuracy=location.accuracy,
        observation_id=observation.observation_id,
        source_record_id=observation.source_record_id,
        run_id=observation.run_id,
        observed_at=observation.observed_at,
        observation_path=str(observation_file.resolve()),
        observation_file_sha256=_sha256(observation_bytes),
        resolver_version=resolution.resolver_version,
        resolution_status=resolution.status,
        resolution_score=resolution.score,
        resolution_reason_codes=resolution.reason_codes,
        resolution_evidence_hash=resolution.evidence_hash,
        resolved_at=resolution.resolved_at,
        resolution_path=str(resolution_file.resolve()),
        resolution_file_sha256=_sha256(resolution_bytes),
        decision_id=decision.decision_id,
        decision_status=decision.status,
        decision_reason_codes=tuple(decision.reason_codes),
        decided_at=decision.decided_at,
        decision_path=str(decision_file.resolve()),
        decision_file_sha256=_sha256(decision_bytes),
        reviewer=reviewer.strip(),
        approved_at=reviewed_at,
        previous_external_id=current.external_id if current is not None else None,
        previous_mapping_file_sha256=previous_mapping_sha256,
    )
    approval_hash = stable_sha256(payload)
    approval = GoogleMapsMappingApproval(
        approval_id=f"google-maps-mapping-approval-{approval_hash[:20]}",
        approval_hash=approval_hash,
        **payload,
    )
    mapping = apply_google_maps_mapping_approval(
        approval,
        current_mapping=current,
    )

    approval_path = approval_writer.write(approval)
    mapping_path = mapping_writer.publish(mapping)
    return approval, mapping, approval_path, mapping_path


def apply_google_maps_mapping_approval(
    approval: GoogleMapsMappingApproval,
    *,
    current_mapping: ExternalEntityMapping | None = None,
) -> ExternalEntityMapping:
    """Materialize a confirmed mapping while retaining canonical service region."""

    validated = GoogleMapsMappingApproval.model_validate_json(
        approval.model_dump_json()
    )
    if current_mapping is not None:
        if current_mapping.entity_id != validated.place_id:
            raise GoogleMapsMappingApprovalError(
                "current mapping belongs to another canonical place"
            )
        if current_mapping.mapping_id != validated.mapping_id:
            raise GoogleMapsMappingApprovalError("current mapping_id cannot change")
        if current_mapping.source_id != _GOOGLE_SOURCE_ID:
            raise GoogleMapsMappingApprovalError(
                "current mapping is not owned by google-maps-web"
            )

    same_identity = (
        current_mapping is not None
        and current_mapping.external_id == validated.external_id
    )
    attributes = dict(current_mapping.attributes) if current_mapping else {}
    attributes.update(
        {
            "canonical_dataset_id": validated.canonical_dataset_id,
            "canonical_dataset_hash": validated.canonical_dataset_hash,
            "canonical_record_hash": validated.canonical_record_hash,
            "master_name": attributes.get("master_name")
            or validated.google_reference_name,
            "city_id": validated.city_id,
            "master_city": validated.city,
            "service_region_city_id": validated.city_id,
            "service_region_city": validated.city,
            "google_place_name": validated.google_reference_name,
            "google_reference_address": validated.google_reference_address,
            "google_latitude": validated.google_latitude,
            "google_longitude": validated.google_longitude,
            "google_coordinate_source": validated.google_coordinate_source,
            "google_coordinate_accuracy": validated.google_coordinate_accuracy,
            "canonical_google_maps_url": str(validated.google_detail_url),
            "human_mapping_approval_id": validated.approval_id,
            "human_mapping_approval_hash": validated.approval_hash,
            "human_mapping_reviewer": validated.reviewer,
            "human_mapping_approved_at": validated.approved_at.isoformat(),
            "approval_dataset_file_sha256": (
                validated.canonical_dataset_file_sha256
            ),
            "approval_observation_file_sha256": (
                validated.observation_file_sha256
            ),
            "approval_resolution_file_sha256": validated.resolution_file_sha256,
            "approval_decision_file_sha256": validated.decision_file_sha256,
            "mapping_evidence_hash": validated.resolution_evidence_hash,
            "mapping_resolver_version": validated.resolver_version,
            "previous_external_id": validated.previous_external_id,
        }
    )
    source_record_ids = list(
        dict.fromkeys(
            [
                *(current_mapping.source_record_ids if current_mapping else []),
                validated.source_record_id,
            ]
        )
    )
    return ExternalEntityMapping(
        mapping_id=validated.mapping_id,
        entity_id=validated.place_id,
        entity_type=validated.entity_type,
        source_id=_GOOGLE_SOURCE_ID,
        external_id=validated.external_id,
        external_url=validated.google_detail_url,
        status=MappingStatus.CONFIRMED,
        confidence=validated.resolution_score,
        matched_at=(
            current_mapping.matched_at
            if same_identity and current_mapping is not None
            else validated.approved_at
        ),
        verified_at=validated.approved_at,
        last_checked_at=validated.approved_at,
        source_record_ids=source_record_ids,
        attributes=attributes,
    )


def _require_place_binding(
    dataset: CanonicalActiveDataset,
    observation: GoogleMapsPlaceObservation,
    resolution: GoogleMapsMappingResolution,
    decision: GoogleMapsDecision,
) -> CanonicalActivePlaceRecord:
    records = [item for item in dataset.records if item.place_id == observation.place_id]
    if len(records) != 1:
        raise GoogleMapsMappingApprovalError(
            "observation place_id is not one active canonical record"
        )
    record = records[0]
    expected_mapping_id = f"google-maps-{record.place_id}"
    bindings = (
        resolution.place_id == record.place_id,
        resolution.mapping_id == expected_mapping_id,
        resolution.observation_id == observation.observation_id,
        resolution.source_record_id == observation.source_record_id,
        decision.place_id == record.place_id,
        decision.observation_id == observation.observation_id,
        decision.run_id == observation.run_id,
        observation.opening.place_id == record.place_id,
        observation.source_id == _GOOGLE_SOURCE_ID,
    )
    if not all(bindings):
        raise GoogleMapsMappingApprovalError(
            "canonical, observation, mapping resolution, and decision place binding "
            "do not match"
        )
    if observation.weekly_opening is not None and (
        observation.weekly_opening.place_id != record.place_id
    ):
        raise GoogleMapsMappingApprovalError(
            "weekly opening evidence belongs to another place"
        )
    if resolution.status is MappingResolutionStatus.AUTO_CONFIRM:
        raise GoogleMapsMappingApprovalError(
            "AUTO_CONFIRM mapping does not require human approval"
        )
    if decision.status is GoogleMapsDecisionStatus.PASS:
        raise GoogleMapsMappingApprovalError(
            "PASS decision does not require human approval"
        )
    return record


def _reject_cross_owner_token(
    dataset: CanonicalActiveDataset,
    mappings: Iterable[ExternalEntityMapping],
    target_place_id: str,
    external_id: str,
) -> None:
    for record in dataset.records:
        for identity in record.external_identities:
            if identity.get("source_id") != _GOOGLE_SOURCE_ID:
                continue
            tokens = google_maps_stable_external_ids(
                identity.get("external_id"),
                identity.get("external_url"),
            )
            if len(tokens) > 1:
                raise GoogleMapsMappingApprovalError(
                    "canonical record contains conflicting stable Google IDs: "
                    f"{record.place_id}"
                )
            if tokens == (external_id,) and record.place_id != target_place_id:
                raise GoogleMapsMappingApprovalError(
                    "stable Google token belongs to another active canonical record: "
                    f"{record.place_id}"
                )

    for mapping in mappings:
        if (
            mapping.source_id != _GOOGLE_SOURCE_ID
            or mapping.status not in _ACTIVE_MAPPING_STATUSES
        ):
            continue
        tokens = google_maps_stable_external_ids(
            mapping.external_id,
            mapping.external_url,
            *(mapping.attributes.get(key) for key in _MAPPING_STABLE_ID_ATTRIBUTE_KEYS),
        )
        if len(tokens) > 1:
            raise GoogleMapsMappingApprovalError(
                "active mapping contains conflicting stable Google IDs: "
                f"{mapping.entity_id}"
            )
        if tokens == (external_id,) and mapping.entity_id != target_place_id:
            raise GoogleMapsMappingApprovalError(
                "stable Google token belongs to another active mapping: "
                f"{mapping.entity_id}"
            )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
