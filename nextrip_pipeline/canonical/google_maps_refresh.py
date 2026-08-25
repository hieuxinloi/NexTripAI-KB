from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import AwareDatetime, Field, HttpUrl, model_validator

from nextrip_pipeline.decision_gate.google_maps import (
    GoogleMapsDecision,
    GoogleMapsDecisionStatus,
)
from nextrip_pipeline.google_maps_identity import (
    google_maps_place_coordinates,
    google_maps_stable_external_id,
    google_maps_stable_place_url,
)
from nextrip_pipeline.google_maps_price import (
    google_maps_price_evidence_text,
    google_maps_price_level,
)
from nextrip_pipeline.publishing._file_lock import destination_file_lock
from nextrip_pipeline.schemas import (
    BusinessStatus,
    EntityType,
    GeoPoint,
    GoogleMapsPlaceObservation,
    NexTripModel,
    OpeningStatusObservation,
    VerificationStatus,
    WeeklyOpeningScheduleObservation,
)

from .dataset import (
    CanonicalActiveDataset,
    CanonicalActivePlaceRecord,
    CanonicalCoordinates,
)
from .models import stable_sha256

if TYPE_CHECKING:
    from nextrip_pipeline.quality.google_maps_approval import (
        GoogleMapsMappingApproval,
    )


GOOGLE_MAPS_CANONICAL_ENTITY_TYPES = (
    EntityType.ATTRACTION,
    EntityType.CAFE,
    EntityType.NIGHTLIFE,
    EntityType.RESTAURANT,
)
_PUBLISHABLE_VERIFICATION_STATUSES = {
    VerificationStatus.AUTO_VERIFIED,
    VerificationStatus.AGENT_VERIFIED,
    VerificationStatus.HUMAN_VERIFIED,
}
_MENU_REVIEW_ENTITY_TYPES = {EntityType.CAFE, EntityType.RESTAURANT}


class CanonicalGoogleMapsRefreshError(ValueError):
    """Raised when Google evidence cannot safely update a canonical dataset."""


class CanonicalGoogleMapsPatchAlreadyExistsError(FileExistsError):
    """Raised rather than replacing a different immutable patch artifact."""


class GoogleMapsPriceEvidenceStatus(StrEnum):
    OBSERVED = "observed"
    NOT_LISTED = "not_listed"


class GoogleMapsScheduleEvidenceStatus(StrEnum):
    OBSERVED = "observed"
    NOT_LISTED = "not_listed"


class GoogleMapsPatchDisposition(StrEnum):
    MISSING = "missing"
    NO_UPDATE = "no_update"
    REVIEW = "review"
    QUARANTINE = "quarantine"


class CanonicalGoogleMapsEvidence(NexTripModel):
    observation_relative_path: str = Field(min_length=1)
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_relative_path: str = Field(min_length=1)
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(min_length=1)
    decided_at: AwareDatetime
    mapping_approval_relative_path: str | None = Field(default=None, min_length=1)
    mapping_approval_file_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    mapping_approval_id: str | None = Field(default=None, min_length=1)
    mapping_approval_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    mapping_approval_reviewer: str | None = Field(default=None, min_length=1)
    mapping_approval_approved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_mapping_approval_provenance(
        self,
    ) -> CanonicalGoogleMapsEvidence:
        values = (
            self.mapping_approval_relative_path,
            self.mapping_approval_file_sha256,
            self.mapping_approval_id,
            self.mapping_approval_hash,
            self.mapping_approval_reviewer,
            self.mapping_approval_approved_at,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError(
                "mapping approval provenance fields must be supplied together"
            )
        return self


def _refresh_record_payload(
    *,
    place_id: str,
    entity_type: EntityType,
    source_record_id: str,
    observation_id: str,
    run_id: str,
    source_url: HttpUrl,
    observed_at: datetime,
    name: str,
    category: str | None,
    address: str | None,
    phone: str | None,
    website_url: HttpUrl | None,
    location: GeoPoint | None,
    business_status: BusinessStatus,
    opening: OpeningStatusObservation,
    weekly_opening: WeeklyOpeningScheduleObservation | None,
    schedule_status: GoogleMapsScheduleEvidenceStatus,
    price_level: int | None,
    raw_price_text: str | None,
    price_status: GoogleMapsPriceEvidenceStatus,
    evidence: CanonicalGoogleMapsEvidence,
) -> dict[str, object]:
    return {
        "place_id": place_id,
        "entity_type": entity_type.value,
        "source_id": "google-maps-web",
        "source_record_id": source_record_id,
        "observation_id": observation_id,
        "run_id": run_id,
        "source_url": str(source_url),
        "observed_at": observed_at.isoformat(),
        "name": name,
        "category": category,
        "address": address,
        "phone": phone,
        "website_url": str(website_url) if website_url is not None else None,
        "location": location.model_dump(mode="json") if location else None,
        "business_status": business_status.value,
        "opening": opening.model_dump(mode="json"),
        "weekly_opening": (
            weekly_opening.model_dump(mode="json") if weekly_opening else None
        ),
        "schedule_status": schedule_status.value,
        "price_level": price_level,
        "raw_price_text": raw_price_text,
        "price_status": price_status.value,
        "evidence": evidence.model_dump(mode="json"),
    }


class CanonicalGoogleMapsRefreshRecord(NexTripModel):
    """Source-pinned fields from one accepted Google Maps observation."""

    refresh_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    source_id: str = Field(default="google-maps-web", pattern=r"^google-maps-web$")
    source_record_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_url: HttpUrl
    observed_at: AwareDatetime
    name: str = Field(min_length=1)
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    website_url: HttpUrl | None = None
    location: GeoPoint | None = None
    business_status: BusinessStatus
    opening: OpeningStatusObservation
    weekly_opening: WeeklyOpeningScheduleObservation | None = None
    schedule_status: GoogleMapsScheduleEvidenceStatus
    price_level: int | None = Field(default=None, ge=1, le=4)
    raw_price_text: str | None = None
    price_status: GoogleMapsPriceEvidenceStatus
    evidence: CanonicalGoogleMapsEvidence

    @model_validator(mode="after")
    def validate_refresh(self) -> CanonicalGoogleMapsRefreshRecord:
        if self.entity_type not in GOOGLE_MAPS_CANONICAL_ENTITY_TYPES:
            raise ValueError("Google canonical refresh cannot contain hotels")
        if self.opening.place_id != self.place_id:
            raise ValueError("opening observation belongs to another place")
        if self.weekly_opening is not None and (
            self.weekly_opening.place_id != self.place_id
        ):
            raise ValueError("weekly opening observation belongs to another place")
        expected_schedule_status = (
            GoogleMapsScheduleEvidenceStatus.OBSERVED
            if self.weekly_opening is not None
            else GoogleMapsScheduleEvidenceStatus.NOT_LISTED
        )
        if self.schedule_status is not expected_schedule_status:
            raise ValueError("schedule_status does not match weekly opening evidence")
        expected_price_status = (
            GoogleMapsPriceEvidenceStatus.OBSERVED
            if self.price_level is not None or self.raw_price_text is not None
            else GoogleMapsPriceEvidenceStatus.NOT_LISTED
        )
        if self.price_status is not expected_price_status:
            raise ValueError("price_status does not match Google price evidence")
        payload = _refresh_record_payload(
            place_id=self.place_id,
            entity_type=self.entity_type,
            source_record_id=self.source_record_id,
            observation_id=self.observation_id,
            run_id=self.run_id,
            source_url=self.source_url,
            observed_at=self.observed_at,
            name=self.name,
            category=self.category,
            address=self.address,
            phone=self.phone,
            website_url=self.website_url,
            location=self.location,
            business_status=self.business_status,
            opening=self.opening,
            weekly_opening=self.weekly_opening,
            schedule_status=self.schedule_status,
            price_level=self.price_level,
            raw_price_text=self.raw_price_text,
            price_status=self.price_status,
            evidence=self.evidence,
        )
        if self.refresh_hash != stable_sha256(payload):
            raise ValueError("refresh_hash does not match Google refresh content")
        return self


class CanonicalGoogleMapsDeferredRecord(NexTripModel):
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    disposition: GoogleMapsPatchDisposition
    observation_id: str | None = Field(default=None, min_length=1)
    reason_codes: list[str] = Field(default_factory=list)


def _patch_payload(
    *,
    base_dataset_id: str,
    base_dataset_hash: str,
    generated_at: datetime,
    entity_types: Sequence[EntityType],
    source_run_ids: Sequence[str],
    records: Sequence[CanonicalGoogleMapsRefreshRecord],
    deferred: Sequence[CanonicalGoogleMapsDeferredRecord],
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "base_dataset_id": base_dataset_id,
        "base_dataset_hash": base_dataset_hash,
        "generated_at": generated_at.isoformat(),
        "entity_types": [item.value for item in entity_types],
        "source_run_ids": list(source_run_ids),
        "records": [item.model_dump(mode="json") for item in records],
        "deferred": [item.model_dump(mode="json") for item in deferred],
    }


class CanonicalGoogleMapsRefreshPatch(NexTripModel):
    """Immutable, content-addressed Google patch for one canonical snapshot."""

    schema_version: str = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    patch_id: str = Field(min_length=1)
    patch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_dataset_id: str = Field(min_length=1)
    base_dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    entity_types: list[EntityType] = Field(min_length=1)
    source_run_ids: list[str] = Field(default_factory=list)
    records: list[CanonicalGoogleMapsRefreshRecord] = Field(default_factory=list)
    deferred: list[CanonicalGoogleMapsDeferredRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_patch(self) -> CanonicalGoogleMapsRefreshPatch:
        if self.entity_types != sorted(set(self.entity_types), key=lambda x: x.value):
            raise ValueError("entity_types must be unique and sorted")
        if any(
            item not in GOOGLE_MAPS_CANONICAL_ENTITY_TYPES for item in self.entity_types
        ):
            raise ValueError("patch contains an unsupported entity type")
        if self.source_run_ids != sorted(set(self.source_run_ids)):
            raise ValueError("source_run_ids must be unique and sorted")
        if self.records != sorted(self.records, key=lambda item: item.place_id):
            raise ValueError("refresh records must be sorted")
        if self.deferred != sorted(self.deferred, key=lambda item: item.place_id):
            raise ValueError("deferred records must be sorted")
        record_ids = [item.place_id for item in self.records]
        deferred_ids = [item.place_id for item in self.deferred]
        if len(record_ids) != len(set(record_ids)) or len(deferred_ids) != len(
            set(deferred_ids)
        ):
            raise ValueError("patch place IDs must be unique")
        if set(record_ids) & set(deferred_ids):
            raise ValueError("a place cannot be both refreshed and deferred")
        payload = _patch_payload(
            base_dataset_id=self.base_dataset_id,
            base_dataset_hash=self.base_dataset_hash,
            generated_at=self.generated_at,
            entity_types=self.entity_types,
            source_run_ids=self.source_run_ids,
            records=self.records,
            deferred=self.deferred,
        )
        expected = stable_sha256(payload)
        if self.patch_hash != expected:
            raise ValueError("patch_hash does not match patch content")
        if self.patch_id != f"canonical-google-maps-{expected[:20]}":
            raise ValueError("patch_id does not match patch_hash")
        return self

    @property
    def target_count(self) -> int:
        return len(self.records) + len(self.deferred)

    @property
    def complete(self) -> bool:
        return not self.deferred

    @property
    def human_approved_count(self) -> int:
        return sum(
            item.evidence.mapping_approval_id is not None for item in self.records
        )


class _LoadedDecision(NexTripModel):
    decision: GoogleMapsDecision
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _LoadedObservation(NexTripModel):
    observation: GoogleMapsPlaceObservation
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _LoadedMappingApproval:
    approval: GoogleMapsMappingApproval
    path: Path
    sha256: str


def build_google_maps_canonical_refresh_patch(
    dataset: CanonicalActiveDataset,
    observation_root: str | Path,
    decision_root: str | Path,
    *,
    entity_types: Iterable[EntityType] = GOOGLE_MAPS_CANONICAL_ENTITY_TYPES,
    source_run_ids: Iterable[str] = (),
    mapping_approval_root: str | Path | None = None,
    generated_at: datetime | None = None,
) -> CanonicalGoogleMapsRefreshPatch:
    """Select the latest PASS or exactly approved evidence for each place."""

    selected_types = sorted(set(entity_types), key=lambda item: item.value)
    if not selected_types or any(
        item not in GOOGLE_MAPS_CANONICAL_ENTITY_TYPES for item in selected_types
    ):
        raise CanonicalGoogleMapsRefreshError(
            "select attraction, cafe, nightlife, or restaurant"
        )
    selected_run_ids = sorted(set(source_run_ids))
    records_by_id = {
        item.place_id: item
        for item in dataset.records
        if item.primary_type in set(selected_types)
    }
    decisions = _load_latest_decisions(decision_root, selected_run_ids)
    observations = _load_observations_by_place(
        observation_root,
        selected_run_ids,
        set(records_by_id),
    )
    mapping_approvals = _load_mapping_approvals(mapping_approval_root)
    refreshes: list[CanonicalGoogleMapsRefreshRecord] = []
    deferred: list[CanonicalGoogleMapsDeferredRecord] = []
    for place_id, canonical in sorted(records_by_id.items()):
        loaded_observations = observations.get(place_id)
        if not loaded_observations:
            deferred.append(
                CanonicalGoogleMapsDeferredRecord(
                    place_id=place_id,
                    entity_type=canonical.primary_type,
                    disposition=GoogleMapsPatchDisposition.MISSING,
                    reason_codes=["GOOGLE_OBSERVATION_MISSING"],
                )
            )
            continue

        accepted: tuple[
            _LoadedObservation,
            _LoadedDecision,
            _LoadedMappingApproval | None,
        ] | None = None
        for candidate in loaded_observations:
            candidate_observation = candidate.observation
            candidate_decision = decisions.get(candidate_observation.observation_id)
            if candidate_decision is None:
                continue
            candidate_approval = _matching_mapping_approval(
                mapping_approvals.get(candidate_observation.observation_id, ()),
                dataset=dataset,
                canonical=canonical,
                observation=candidate,
                decision=candidate_decision,
            )
            if candidate_decision.decision.status is GoogleMapsDecisionStatus.PASS:
                problem = _accepted_observation_problem(
                    candidate_observation,
                    candidate_decision.decision,
                    canonical,
                )
            elif candidate_approval is not None:
                problem = _approved_observation_problem(
                    candidate_observation,
                    candidate_decision.decision,
                    canonical,
                )
            else:
                continue
            if problem is None:
                accepted = (candidate, candidate_decision, candidate_approval)
                break

        accepted_approval: _LoadedMappingApproval | None = None
        if accepted is not None:
            loaded, loaded_decision, accepted_approval = accepted
            observation = loaded.observation
            decision = loaded_decision.decision
        else:
            loaded = loaded_observations[0]
            observation = loaded.observation
            loaded_decision = decisions.get(observation.observation_id)
            decision = loaded_decision.decision if loaded_decision else None

        if loaded_decision is None:
            deferred.append(
                CanonicalGoogleMapsDeferredRecord(
                    place_id=place_id,
                    entity_type=canonical.primary_type,
                    disposition=GoogleMapsPatchDisposition.REVIEW,
                    observation_id=observation.observation_id,
                    reason_codes=["GOOGLE_DECISION_MISSING"],
                )
            )
            continue
        if decision is None:
            raise AssertionError("loaded decision unexpectedly missing")
        if (
            decision.status is not GoogleMapsDecisionStatus.PASS
            and accepted_approval is None
        ):
            deferred.append(
                CanonicalGoogleMapsDeferredRecord(
                    place_id=place_id,
                    entity_type=canonical.primary_type,
                    disposition=(
                        GoogleMapsPatchDisposition.REVIEW
                        if decision.status is GoogleMapsDecisionStatus.REVIEW
                        else GoogleMapsPatchDisposition.QUARANTINE
                    ),
                    observation_id=observation.observation_id,
                    reason_codes=decision.reason_codes,
                )
            )
            continue
        evidence_problem = (
            _approved_observation_problem(
                observation,
                decision,
                canonical,
            )
            if accepted_approval is not None
            else _accepted_observation_problem(
                observation,
                decision,
                canonical,
            )
        )
        if evidence_problem is not None:
            disposition, reason_code = evidence_problem
            deferred.append(
                CanonicalGoogleMapsDeferredRecord(
                    place_id=place_id,
                    entity_type=canonical.primary_type,
                    disposition=disposition,
                    observation_id=observation.observation_id,
                    reason_codes=[reason_code],
                )
            )
            continue
        complete_weekly_opening = (
            observation.weekly_opening
            if observation.weekly_opening is not None
            and len(observation.weekly_opening.days) == 7
            else None
        )
        evidence = CanonicalGoogleMapsEvidence(
            observation_relative_path=_relative_artifact_path(
                loaded.path, Path(observation_root)
            ),
            observation_sha256=loaded.sha256,
            decision_relative_path=_relative_artifact_path(
                loaded_decision.path, Path(decision_root)
            ),
            decision_sha256=loaded_decision.sha256,
            decision_id=decision.decision_id,
            decided_at=decision.decided_at,
            **(
                {
                    "mapping_approval_relative_path": _relative_artifact_path(
                        accepted_approval.path,
                        Path(mapping_approval_root),
                    ),
                    "mapping_approval_file_sha256": accepted_approval.sha256,
                    "mapping_approval_id": accepted_approval.approval.approval_id,
                    "mapping_approval_hash": (
                        accepted_approval.approval.approval_hash
                    ),
                    "mapping_approval_reviewer": (
                        accepted_approval.approval.reviewer
                    ),
                    "mapping_approval_approved_at": (
                        accepted_approval.approval.approved_at
                    ),
                }
                if accepted_approval is not None
                else {}
            ),
        )
        raw_price_text = google_maps_price_evidence_text(observation.raw_price_text)
        price_level = google_maps_price_level(raw_price_text)
        values = {
            "place_id": place_id,
            "entity_type": canonical.primary_type,
            "source_record_id": observation.source_record_id,
            "observation_id": observation.observation_id,
            "run_id": observation.run_id,
            "source_url": observation.source_url,
            "observed_at": observation.observed_at,
            "name": observation.name.strip() if observation.name else "",
            "category": observation.category,
            "address": observation.address,
            "phone": observation.phone,
            "website_url": observation.website_url,
            "location": _independent_google_location(observation.location),
            "business_status": observation.business_status,
            "opening": observation.opening,
            "weekly_opening": complete_weekly_opening,
            "schedule_status": (
                GoogleMapsScheduleEvidenceStatus.OBSERVED
                if complete_weekly_opening is not None
                else GoogleMapsScheduleEvidenceStatus.NOT_LISTED
            ),
            "price_level": price_level,
            "raw_price_text": raw_price_text,
            "price_status": (
                GoogleMapsPriceEvidenceStatus.OBSERVED
                if price_level is not None or raw_price_text is not None
                else GoogleMapsPriceEvidenceStatus.NOT_LISTED
            ),
            "evidence": evidence,
        }
        refresh_hash = stable_sha256(
            _refresh_record_payload(**values)  # type: ignore[arg-type]
        )
        refreshes.append(
            CanonicalGoogleMapsRefreshRecord(
                refresh_hash=refresh_hash,
                **values,
            )
        )

    refreshes, deferred = _defer_external_identity_collisions(
        dataset,
        refreshes,
        deferred,
    )
    effective_time = generated_at or datetime.now(timezone.utc)
    if effective_time.tzinfo is None or effective_time.utcoffset() is None:
        raise CanonicalGoogleMapsRefreshError("generated_at must be timezone-aware")
    payload = _patch_payload(
        base_dataset_id=dataset.dataset_id,
        base_dataset_hash=dataset.dataset_hash,
        generated_at=effective_time,
        entity_types=selected_types,
        source_run_ids=selected_run_ids,
        records=refreshes,
        deferred=deferred,
    )
    patch_hash = stable_sha256(payload)
    return CanonicalGoogleMapsRefreshPatch(
        patch_id=f"canonical-google-maps-{patch_hash[:20]}",
        patch_hash=patch_hash,
        base_dataset_id=dataset.dataset_id,
        base_dataset_hash=dataset.dataset_hash,
        generated_at=effective_time,
        entity_types=selected_types,
        source_run_ids=selected_run_ids,
        records=refreshes,
        deferred=deferred,
    )


def _defer_external_identity_collisions(
    dataset: CanonicalActiveDataset,
    refreshes: Sequence[CanonicalGoogleMapsRefreshRecord],
    deferred: Sequence[CanonicalGoogleMapsDeferredRecord],
) -> tuple[
    list[CanonicalGoogleMapsRefreshRecord],
    list[CanonicalGoogleMapsDeferredRecord],
]:
    """Keep a scheduled identity collision from invalidating the whole release."""

    base_owner_by_external_id: dict[str, str] = {}
    base_ids_by_place: dict[str, set[str]] = {}
    for record in dataset.records:
        external_ids = _google_external_ids(record)
        base_ids_by_place[record.place_id] = external_ids
        for external_id in external_ids:
            existing_owner = base_owner_by_external_id.get(external_id)
            if existing_owner is not None and existing_owner != record.place_id:
                raise CanonicalGoogleMapsRefreshError(
                    "base canonical dataset has duplicate active Google stable "
                    f"external_id {external_id!r}: {existing_owner}, "
                    f"{record.place_id}"
                )
            base_owner_by_external_id[external_id] = record.place_id

    accepted = {item.place_id: item for item in refreshes}
    collision_ids_by_place: dict[str, set[str]] = {}
    while True:
        proposed_owners: dict[str, set[str]] = {}
        proposed_id_by_place: dict[str, str | None] = {}
        for record in dataset.records:
            refresh = accepted.get(record.place_id)
            proposed_id = (
                google_maps_stable_external_id(str(refresh.source_url))
                if refresh is not None
                else None
            )
            proposed_id_by_place[record.place_id] = proposed_id
            external_ids = (
                {proposed_id}
                if proposed_id is not None
                else base_ids_by_place[record.place_id]
            )
            for external_id in external_ids:
                proposed_owners.setdefault(external_id, set()).add(record.place_id)

        blocked: set[str] = set()
        for external_id, place_ids in proposed_owners.items():
            if len(place_ids) < 2:
                continue
            base_owner = base_owner_by_external_id.get(external_id)
            keep_owner = (
                base_owner
                if base_owner in place_ids
                and proposed_id_by_place.get(base_owner) == external_id
                else None
            )
            collision_refreshes = set(place_ids) & set(accepted)
            if keep_owner is not None:
                collision_refreshes.discard(keep_owner)
            if not collision_refreshes:
                raise CanonicalGoogleMapsRefreshError(
                    "cannot isolate duplicate active Google stable external_id "
                    f"{external_id!r}: {', '.join(sorted(place_ids))}"
                )
            for place_id in collision_refreshes:
                blocked.add(place_id)
                collision_ids_by_place.setdefault(place_id, set()).add(external_id)

        if not blocked:
            break
        for place_id in blocked:
            accepted.pop(place_id, None)

    deferred_records = list(deferred)
    refresh_by_id = {item.place_id: item for item in refreshes}
    for place_id in sorted(collision_ids_by_place):
        refresh = refresh_by_id[place_id]
        deferred_records.append(
            CanonicalGoogleMapsDeferredRecord(
                place_id=place_id,
                entity_type=refresh.entity_type,
                disposition=GoogleMapsPatchDisposition.NO_UPDATE,
                observation_id=refresh.observation_id,
                reason_codes=["GOOGLE_EXTERNAL_ID_COLLISION"],
            )
        )
    return (
        sorted(accepted.values(), key=lambda item: item.place_id),
        sorted(deferred_records, key=lambda item: item.place_id),
    )


def _google_external_ids(record: CanonicalActivePlaceRecord) -> set[str]:
    return {
        external_id
        for identity in record.external_identities
        if identity.get("source_id") == "google-maps-web"
        and isinstance((external_id := identity.get("external_id")), str)
        and external_id
    }


def apply_google_maps_canonical_refresh_patch(
    dataset: CanonicalActiveDataset,
    patch: CanonicalGoogleMapsRefreshPatch,
) -> CanonicalActiveDataset:
    """Create a new immutable dataset; the base dataset remains unchanged."""

    if (dataset.dataset_id, dataset.dataset_hash) != (
        patch.base_dataset_id,
        patch.base_dataset_hash,
    ):
        raise CanonicalGoogleMapsRefreshError(
            "Google Maps patch belongs to another canonical dataset"
        )
    refresh_by_id = {item.place_id: item for item in patch.records}
    records = [
        _apply_refresh(record, refresh_by_id[record.place_id])
        if record.place_id in refresh_by_id
        else CanonicalActivePlaceRecord.model_validate_json(record.model_dump_json())
        for record in dataset.records
    ]
    payload = {
        "schema_version": dataset.schema_version,
        "manifest_id": dataset.manifest_id,
        "manifest_hash": dataset.manifest_hash,
        "records": [item.model_dump(mode="json") for item in records],
        "report": dataset.report.model_dump(mode="json"),
    }
    dataset_hash = stable_sha256(payload)
    return CanonicalActiveDataset(
        schema_version=dataset.schema_version,
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        manifest_id=dataset.manifest_id,
        manifest_hash=dataset.manifest_hash,
        records=records,
        report=dataset.report,
    )


def _apply_refresh(
    record: CanonicalActivePlaceRecord,
    refresh: CanonicalGoogleMapsRefreshRecord,
) -> CanonicalActivePlaceRecord:
    if (record.place_id, record.primary_type) != (
        refresh.place_id,
        refresh.entity_type,
    ):
        raise CanonicalGoogleMapsRefreshError(
            f"Google refresh identity mismatch for {record.place_id}"
        )
    existing_refresh = record.data.get("google_maps_refresh")
    if isinstance(existing_refresh, dict):
        previous_observed_at = _parse_stored_datetime(
            existing_refresh.get("observed_at")
        )
        previous_refresh_hash = existing_refresh.get("refresh_hash")
        if previous_observed_at is not None:
            if previous_observed_at > refresh.observed_at:
                raise CanonicalGoogleMapsRefreshError(
                    f"Google refresh is stale for {record.place_id}: "
                    f"{refresh.observed_at.isoformat()} < "
                    f"{previous_observed_at.isoformat()}"
                )
            if previous_observed_at == refresh.observed_at:
                if previous_refresh_hash != refresh.refresh_hash:
                    raise CanonicalGoogleMapsRefreshError(
                        "conflicting Google evidence has the same observed_at for "
                        f"{record.place_id}"
                    )
                return CanonicalActivePlaceRecord.model_validate_json(
                    record.model_dump_json()
                )
    data = deepcopy(record.data)
    name = " ".join(unicodedata.normalize("NFKC", refresh.name).split())
    aliases = _canonical_aliases([*record.aliases, record.name], name)
    address = refresh.address or record.address
    phone = refresh.phone or record.phone
    website_url = refresh.website_url or record.website_url
    coordinates = record.coordinates
    if refresh.location is not None:
        coordinates = CanonicalCoordinates(
            lat=refresh.location.latitude,
            lng=refresh.location.longitude,
            source="google-maps-web",
        )

    # Apply accepted evidence as a patch.  A scheduled capture can legitimately
    # omit an optional panel (hours, price level, phone, and so on).  Absence in
    # one crawl must not erase a previously verified canonical value.
    data.update(
        {
            "name": name,
            "aliases": aliases,
            "address": address,
            "phone": phone,
            "website_url": str(website_url) if website_url else None,
            "coordinates": {
                "lat": coordinates.lat,
                "lng": coordinates.lng,
                "source": coordinates.source,
            },
            "google_maps_last_checked_at": refresh.observed_at.isoformat(),
            "last_updated": refresh.observed_at.isoformat(),
            "google_maps_refresh": {
                "refresh_hash": refresh.refresh_hash,
                "source_id": refresh.source_id,
                "source_record_id": refresh.source_record_id,
                "observation_id": refresh.observation_id,
                "run_id": refresh.run_id,
                "source_url": str(refresh.source_url),
                "observed_at": refresh.observed_at.isoformat(),
                "decision_id": refresh.evidence.decision_id,
                "observation_sha256": refresh.evidence.observation_sha256,
                "decision_sha256": refresh.evidence.decision_sha256,
                **(
                    {
                        "mapping_approval_id": (
                            refresh.evidence.mapping_approval_id
                        ),
                        "mapping_approval_hash": (
                            refresh.evidence.mapping_approval_hash
                        ),
                        "mapping_approval_reviewer": (
                            refresh.evidence.mapping_approval_reviewer
                        ),
                        "mapping_approval_approved_at": (
                            refresh.evidence.mapping_approval_approved_at.isoformat()
                            if refresh.evidence.mapping_approval_approved_at
                            else None
                        ),
                        "mapping_approval_file_sha256": (
                            refresh.evidence.mapping_approval_file_sha256
                        ),
                    }
                    if refresh.evidence.mapping_approval_id is not None
                    else {}
                ),
            },
        }
    )
    # ``city``/``city_id`` remain NexTrip service-area identity. Google can use
    # a newer province label, so keep its exact administrative text separately.
    if refresh.address is not None:
        data["google_maps_administrative_address"] = refresh.address
    if refresh.category is not None:
        data["google_maps_category"] = refresh.category
    if (
        refresh.business_status is not BusinessStatus.UNKNOWN
        or "business_status" not in data
    ):
        data["business_status"] = refresh.business_status.value
    if refresh.opening.status.value != "unknown" or "opening_status" not in data:
        data["opening_status"] = refresh.opening.model_dump(mode="json")
    if refresh.weekly_opening is not None:
        data["google_maps_weekly_opening"] = refresh.weekly_opening.model_dump(
            mode="json"
        )
        data["google_maps_schedule_status"] = refresh.schedule_status.value
        data["opening_hours"] = _canonical_opening_hours(refresh.weekly_opening)
    if (
        refresh.price_status is GoogleMapsPriceEvidenceStatus.OBSERVED
        or "google_maps_price" not in data
    ):
        data["google_maps_price"] = {
            "status": refresh.price_status.value,
            "level": refresh.price_level,
            "raw_text": refresh.raw_price_text,
            "observed_at": refresh.observed_at.isoformat(),
            "source": refresh.source_id,
        }
    if (
        record.primary_type in _MENU_REVIEW_ENTITY_TYPES
        and not _has_human_verified_menu(data)
    ):
        for field_name in ("menu_url", "menu_image_urls", "menu_items"):
            data.pop(field_name, None)
        data["menu"] = None

    external_identities = deepcopy(record.external_identities)
    stable_google_id = google_maps_stable_external_id(str(refresh.source_url))
    if stable_google_id is not None:
        external_identities = [
            item
            for item in external_identities
            if item.get("source_id") != "google-maps-web"
        ]
        external_identities.append(
            {
                "source_id": "google-maps-web",
                "external_id": stable_google_id,
                "external_url": str(refresh.source_url),
                "verified_at": refresh.observed_at.isoformat(),
            }
        )
        external_identities.sort(
            key=lambda item: (str(item.get("source_id")), str(item.get("external_id")))
        )

    values = record.model_dump(mode="json")
    values.update(
        {
            "name": name,
            "aliases": aliases,
            "address": address,
            "coordinates": coordinates.model_dump(mode="json"),
            "phone": phone,
            "website_url": str(website_url) if website_url is not None else None,
            "external_identities": external_identities,
            "data": data,
        }
    )
    values.pop("record_hash")
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(values),
        **values,
    )


def _canonical_opening_hours(
    weekly: WeeklyOpeningScheduleObservation,
) -> dict[str, object]:
    rendered: list[str] = []
    closed_days: list[str] = []
    simple_intervals: list[tuple[str, str]] = []
    for day in weekly.days:
        if day.closed:
            rendered.append(f"{day.day.value}: closed")
            closed_days.append(day.day.value)
            continue
        if day.open_24_hours:
            rendered.append(f"{day.day.value}: 24 hours")
            simple_intervals.append(("00:00", "23:59"))
            continue
        intervals = [
            f"{item.opens_at.strftime('%H:%M')}-{item.closes_at.strftime('%H:%M')}"
            for item in day.intervals
        ]
        rendered.append(f"{day.day.value}: {', '.join(intervals)}")
        if len(day.intervals) == 1:
            interval = day.intervals[0]
            simple_intervals.append(
                (
                    interval.opens_at.strftime("%H:%M"),
                    interval.closes_at.strftime("%H:%M"),
                )
            )
    unique_intervals = sorted(set(simple_intervals))
    opens_at = unique_intervals[0][0] if len(unique_intervals) == 1 else None
    closes_at = unique_intervals[0][1] if len(unique_intervals) == 1 else None
    return {
        "open": opens_at,
        "close": closes_at,
        "closed_days": closed_days,
        "note": "; ".join(rendered),
        "timezone": weekly.timezone,
        "observed_at": weekly.observed_at.isoformat(),
        "source": "google-maps-web",
    }


def _accepted_observation_problem(
    observation: GoogleMapsPlaceObservation,
    decision: GoogleMapsDecision,
    canonical: CanonicalActivePlaceRecord,
) -> tuple[GoogleMapsPatchDisposition, str] | None:
    if (
        observation.source_id != "google-maps-web"
        or observation.place_id != canonical.place_id
        or decision.place_id != canonical.place_id
        or decision.observation_id != observation.observation_id
        or decision.run_id != observation.run_id
    ):
        return (
            GoogleMapsPatchDisposition.QUARANTINE,
            "GOOGLE_EVIDENCE_IDENTITY_MISMATCH",
        )
    if observation.verification_status not in _PUBLISHABLE_VERIFICATION_STATUSES:
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_OBSERVATION_NOT_VERIFIED",
        )
    if not observation.name or not observation.name.strip():
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_NAME_MISSING",
        )
    return None


def _approved_observation_problem(
    observation: GoogleMapsPlaceObservation,
    decision: GoogleMapsDecision,
    canonical: CanonicalActivePlaceRecord,
) -> tuple[GoogleMapsPatchDisposition, str] | None:
    if (
        observation.source_id != "google-maps-web"
        or observation.place_id != canonical.place_id
        or decision.place_id != canonical.place_id
        or decision.observation_id != observation.observation_id
        or decision.run_id != observation.run_id
    ):
        return (
            GoogleMapsPatchDisposition.QUARANTINE,
            "GOOGLE_EVIDENCE_IDENTITY_MISMATCH",
        )
    if not observation.name or not observation.name.strip():
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_NAME_MISSING",
        )
    if google_maps_stable_place_url(str(observation.source_url)) is None:
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_DIRECT_DETAIL_URL_REQUIRED",
        )
    if _independent_google_location(observation.location) is None:
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_COORDINATE_EVIDENCE_REQUIRED",
        )
    url_coordinates = google_maps_place_coordinates(observation.source_url)
    if url_coordinates is not None and observation.location is not None and (
        abs(observation.location.latitude - url_coordinates[0]) > 1e-7
        or abs(observation.location.longitude - url_coordinates[1]) > 1e-7
    ):
        return (
            GoogleMapsPatchDisposition.REVIEW,
            "GOOGLE_PLACE_COORDINATE_MISMATCH",
        )
    return None


def _matching_mapping_approval(
    loaded_approvals: Sequence[_LoadedMappingApproval],
    *,
    dataset: CanonicalActiveDataset,
    canonical: CanonicalActivePlaceRecord,
    observation: _LoadedObservation,
    decision: _LoadedDecision,
) -> _LoadedMappingApproval | None:
    observed = observation.observation
    decided = decision.decision
    location = observed.location
    detail_url = google_maps_stable_place_url(str(observed.source_url))
    stable_id = google_maps_stable_external_id(detail_url)
    if (
        _approved_observation_problem(observed, decided, canonical) is not None
        or location is None
        or detail_url is None
        or stable_id is None
    ):
        return None
    for loaded in loaded_approvals:
        approval = loaded.approval
        bindings = (
            approval.canonical_dataset_id == dataset.dataset_id,
            approval.canonical_dataset_hash == dataset.dataset_hash,
            approval.canonical_record_hash == canonical.record_hash,
            approval.place_id == canonical.place_id,
            approval.entity_type is canonical.primary_type,
            approval.city_id == canonical.city_id,
            approval.city == canonical.city,
            approval.mapping_id == f"google-maps-{canonical.place_id}",
            approval.external_id == stable_id,
            str(approval.google_detail_url) == str(observed.source_url),
            approval.google_reference_name == (observed.name or "").strip(),
            approval.google_reference_address == observed.address,
            approval.google_latitude == location.latitude,
            approval.google_longitude == location.longitude,
            approval.google_coordinate_source == location.source,
            approval.google_coordinate_accuracy == location.accuracy,
            approval.observation_id == observed.observation_id,
            approval.source_record_id == observed.source_record_id,
            approval.run_id == observed.run_id,
            approval.observed_at == observed.observed_at,
            approval.observation_file_sha256 == observation.sha256,
            approval.decision_id == decided.decision_id,
            approval.decision_status is decided.status,
            approval.decision_reason_codes == tuple(decided.reason_codes),
            approval.decided_at == decided.decided_at,
            approval.decision_file_sha256 == decision.sha256,
        )
        if all(bindings):
            return loaded
    return None


def _load_latest_decisions(
    root: str | Path,
    run_ids: Sequence[str],
) -> dict[str, _LoadedDecision]:
    directory = Path(root)
    result: dict[str, _LoadedDecision] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.rglob("*.json")):
        try:
            content = path.read_bytes()
            decision = GoogleMapsDecision.model_validate_json(content)
        except (OSError, TypeError, ValueError):
            continue
        if run_ids and decision.run_id not in run_ids:
            continue
        loaded = _LoadedDecision(
            decision=decision,
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        current = result.get(decision.observation_id)
        if current is None or (
            decision.decided_at,
            decision.decision_id,
        ) > (
            current.decision.decided_at,
            current.decision.decision_id,
        ):
            result[decision.observation_id] = loaded
    return result


def _load_mapping_approvals(
    root: str | Path | None,
) -> dict[str, list[_LoadedMappingApproval]]:
    if root is None:
        return {}
    from nextrip_pipeline.quality.google_maps_approval import (
        GoogleMapsMappingApproval,
    )

    directory = Path(root)
    result: dict[str, list[_LoadedMappingApproval]] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.rglob("*.json")):
        try:
            content = path.read_bytes()
            approval = GoogleMapsMappingApproval.model_validate_json(content)
            canonical_bytes = (approval.model_dump_json(indent=2) + "\n").encode(
                "utf-8"
            )
        except (OSError, TypeError, ValueError):
            continue
        if content != canonical_bytes:
            continue
        result.setdefault(approval.observation_id, []).append(
            _LoadedMappingApproval(
                approval=approval,
                path=path,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    for loaded_approvals in result.values():
        loaded_approvals.sort(
            key=lambda item: (
                item.approval.approved_at,
                item.approval.approval_id,
            ),
            reverse=True,
        )
    return result


def _load_observations_by_place(
    root: str | Path,
    run_ids: Sequence[str],
    place_ids: set[str],
) -> dict[str, list[_LoadedObservation]]:
    directory = Path(root)
    result: dict[str, list[_LoadedObservation]] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.rglob("*.json")):
        try:
            content = path.read_bytes()
            observation = GoogleMapsPlaceObservation.model_validate_json(content)
        except (OSError, TypeError, ValueError):
            continue
        if (
            observation.source_id != "google-maps-web"
            or observation.place_id not in place_ids
            or (run_ids and observation.run_id not in run_ids)
        ):
            continue
        loaded = _LoadedObservation(
            observation=observation,
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        result.setdefault(observation.place_id, []).append(loaded)
    for loaded_observations in result.values():
        loaded_observations.sort(
            key=lambda item: (
                item.observation.observed_at,
                item.observation.observation_id,
            ),
            reverse=True,
        )
    return result


def _independent_google_location(location: GeoPoint | None) -> GeoPoint | None:
    if location is None:
        return None
    source = (location.source or "").casefold()
    accuracy = (location.accuracy or "").casefold()
    if source != "google-maps-web" or "fallback" in accuracy:
        return None
    return location


def _relative_artifact_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _canonical_aliases(values: Iterable[str], name: str) -> list[str]:
    name_key = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    selected: dict[str, str] = {}
    for value in values:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        key = " ".join(cleaned.casefold().split())
        if cleaned and key != name_key:
            selected.setdefault(key, cleaned)
    return [selected[key] for key in sorted(selected)]


def _parse_stored_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def _has_human_verified_menu(data: dict[str, object]) -> bool:
    status = data.get("menu_verification_status")
    if isinstance(status, str) and status.casefold() in {
        "approved",
        "human_verified",
        "verified",
    }:
        return True
    menu = data.get("menu")
    return bool(
        isinstance(menu, dict)
        and isinstance(menu.get("verification_status"), str)
        and str(menu["verification_status"]).casefold()
        in {"approved", "human_verified", "verified"}
    )


class CanonicalGoogleMapsPatchWriter:
    """Write one immutable Google refresh patch under a content-addressed path."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def destination_for(self, patch: CanonicalGoogleMapsRefreshPatch) -> Path:
        return (
            self.output_root
            / f"patch={quote(patch.patch_id, safe='-_.')}"
            / "canonical-google-maps-refresh.json"
        )

    def write(self, patch: CanonicalGoogleMapsRefreshPatch) -> Path:
        validated = CanonicalGoogleMapsRefreshPatch.model_validate_json(
            patch.model_dump_json()
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                existing = CanonicalGoogleMapsRefreshPatch.model_validate_json(
                    destination.read_bytes()
                )
                if existing.patch_hash == validated.patch_hash:
                    return destination
                raise CanonicalGoogleMapsPatchAlreadyExistsError(
                    f"immutable Google patch already exists: {destination}"
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
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        return destination


class MenuCollectionStatus(StrEnum):
    PENDING_HUMAN_SOURCE = "pending_human_source"


class CanonicalMenuCollectionTask(NexTripModel):
    place_id: str = Field(min_length=1)
    entity_type: EntityType
    name: str = Field(min_length=1)
    city: str = Field(min_length=1)
    status: MenuCollectionStatus = MenuCollectionStatus.PENDING_HUMAN_SOURCE


class CanonicalMenuCollectionBacklog(NexTripModel):
    schema_version: str = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    backlog_id: str = Field(min_length=1)
    backlog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: AwareDatetime
    tasks: list[CanonicalMenuCollectionTask]

    @model_validator(mode="after")
    def validate_backlog(self) -> CanonicalMenuCollectionBacklog:
        if self.tasks != sorted(self.tasks, key=lambda item: item.place_id):
            raise ValueError("menu collection tasks must be sorted")
        if len({item.place_id for item in self.tasks}) != len(self.tasks):
            raise ValueError("menu collection place IDs must be unique")
        if any(
            item.entity_type not in _MENU_REVIEW_ENTITY_TYPES for item in self.tasks
        ):
            raise ValueError("menu collection only supports cafe and restaurant")
        payload = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "generated_at": self.generated_at.isoformat(),
            "tasks": [item.model_dump(mode="json") for item in self.tasks],
        }
        expected = stable_sha256(payload)
        if self.backlog_hash != expected:
            raise ValueError("backlog_hash does not match menu backlog content")
        if self.backlog_id != f"canonical-menu-{expected[:20]}":
            raise ValueError("backlog_id does not match backlog_hash")
        return self


def build_canonical_menu_collection_backlog(
    dataset: CanonicalActiveDataset,
    *,
    generated_at: datetime | None = None,
) -> CanonicalMenuCollectionBacklog:
    effective_time = generated_at or datetime.now(timezone.utc)
    tasks = [
        CanonicalMenuCollectionTask(
            place_id=record.place_id,
            entity_type=record.primary_type,
            name=record.name,
            city=record.city,
        )
        for record in dataset.records
        if record.primary_type in _MENU_REVIEW_ENTITY_TYPES
        and not _has_human_verified_menu(record.data)
    ]
    payload = {
        "schema_version": "1.0.0",
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "generated_at": effective_time.isoformat(),
        "tasks": [item.model_dump(mode="json") for item in tasks],
    }
    backlog_hash = stable_sha256(payload)
    return CanonicalMenuCollectionBacklog(
        backlog_id=f"canonical-menu-{backlog_hash[:20]}",
        backlog_hash=backlog_hash,
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        generated_at=effective_time,
        tasks=tasks,
    )


class CanonicalMenuCollectionBacklogWriter:
    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)

    def write(self, backlog: CanonicalMenuCollectionBacklog) -> Path:
        destination = (
            self.output_root
            / f"backlog={quote(backlog.backlog_id, safe='-_.')}"
            / "menu-human-review-backlog.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination_file_lock(destination):
            if destination.exists():
                existing = CanonicalMenuCollectionBacklog.model_validate_json(
                    destination.read_bytes()
                )
                if existing.backlog_hash == backlog.backlog_hash:
                    return destination
                raise FileExistsError(f"menu backlog already exists: {destination}")
            content = (
                json.dumps(
                    backlog.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        return destination


__all__ = [
    "CanonicalGoogleMapsPatchWriter",
    "CanonicalGoogleMapsRefreshError",
    "CanonicalGoogleMapsRefreshPatch",
    "CanonicalGoogleMapsRefreshRecord",
    "CanonicalMenuCollectionBacklog",
    "CanonicalMenuCollectionBacklogWriter",
    "GOOGLE_MAPS_CANONICAL_ENTITY_TYPES",
    "apply_google_maps_canonical_refresh_patch",
    "build_canonical_menu_collection_backlog",
    "build_google_maps_canonical_refresh_patch",
]
