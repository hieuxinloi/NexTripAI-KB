from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)
from nextrip_pipeline.canonical.discovery import (
    StagedGoogleMapsCandidate,
    _google_maps_identity,
    _validate_candidate_vacancy_type,
)
from nextrip_pipeline.canonical.distinct import DistinctCandidateValidator
from nextrip_pipeline.canonical.eligibility import (
    CandidateEntityEligibilityValidator,
)
from nextrip_pipeline.canonical.models import (
    EntityCityVacancy,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.crawl.raw_writer import RawJsonWriter
from nextrip_pipeline.preprocessing.google_maps import GoogleMapsPlaceNormalizer
from nextrip_pipeline.schemas import (
    ExternalEntityMapping,
    GoogleMapsPlaceObservation,
    MappingStatus,
    NexTripModel,
    SourceRecord,
)


class CandidateDetailError(ValueError):
    """Raised when a staged result cannot safely enter detail validation."""


class CandidateDetailAlreadyExistsError(FileExistsError):
    """Raised rather than overwrite an immutable candidate-detail result."""


class GoogleMapsDetailCapture(Protocol):
    source_id: str

    def fetch(
        self,
        mapping: ExternalEntityMapping,
        *,
        run_id: str,
    ) -> SourceRecord: ...


def candidate_detail_payload(
    *,
    schema_version: str,
    detail_id: str,
    run_id: str,
    source_record_id: str,
    observation_id: str,
    vacancy: EntityCityVacancy,
    candidate: CanonicalReplacementCandidate,
    validation: CandidateValidationResult,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "detail_id": detail_id,
        "run_id": run_id,
        "source_record_id": source_record_id,
        "observation_id": observation_id,
        "vacancy": vacancy.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
    }


class CandidateDetailStage(NexTripModel):
    """Immutable full candidate and its offline distinctness result."""

    schema_version: str = "1.0.0"
    detail_id: str = Field(min_length=1)
    detail_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    observed_at: AwareDatetime
    vacancy: EntityCityVacancy
    candidate: CanonicalReplacementCandidate
    validation: CandidateValidationResult

    @model_validator(mode="after")
    def validate_detail(self) -> CandidateDetailStage:
        if self.candidate.candidate_key != self.validation.candidate_key:
            raise ValueError("validation belongs to another candidate")
        _validate_candidate_vacancy_type(
            self.candidate.entity_type,
            self.vacancy,
        )
        if self.candidate.city_id != self.vacancy.city_id:
            raise ValueError("candidate city does not match vacancy")
        expected_hash = stable_sha256(
            candidate_detail_payload(
                schema_version=self.schema_version,
                detail_id=self.detail_id,
                run_id=self.run_id,
                source_record_id=self.source_record_id,
                observation_id=self.observation_id,
                vacancy=self.vacancy,
                candidate=self.candidate,
                validation=self.validation,
            )
        )
        if self.detail_hash != expected_hash:
            raise ValueError("detail_hash does not match candidate detail content")
        return self


class CandidateDetailStageWriter:
    """Write each detailed validation once under its vacancy and run."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, detail: CandidateDetailStage) -> Path:
        return (
            self.root_directory
            / _path_segment("city", detail.vacancy.city_id)
            / _path_segment("entity", detail.vacancy.entity_type.value)
            / _path_segment("vacancy", detail.vacancy.vacancy_id)
            / _path_segment("run", detail.run_id)
            / f"{_path_segment('detail', detail.detail_id)}.json"
        )

    def write(self, detail: CandidateDetailStage) -> Path:
        validated = CandidateDetailStage.model_validate(
            detail.model_dump(mode="python")
        )
        destination = self.destination_for(validated)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = (
            json.dumps(
                validated.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise CandidateDetailAlreadyExistsError(
                f"Candidate detail is immutable and already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        return destination


@dataclass(frozen=True, slots=True)
class CandidateDetailRun:
    mapping: ExternalEntityMapping
    source_record: SourceRecord
    raw_path: Path
    observation: GoogleMapsPlaceObservation
    detail: CandidateDetailStage
    detail_path: Path


class GoogleMapsCandidateDetail:
    """Resolve and validate one staged Maps result without allocating an ID."""

    def __init__(
        self,
        adapter: GoogleMapsDetailCapture,
        raw_writer: RawJsonWriter,
        detail_writer: CandidateDetailStageWriter,
        *,
        normalizer: GoogleMapsPlaceNormalizer | None = None,
        validator: DistinctCandidateValidator | None = None,
        eligibility_validator: CandidateEntityEligibilityValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.raw_writer = raw_writer
        self.detail_writer = detail_writer
        self.normalizer = normalizer or GoogleMapsPlaceNormalizer()
        self.validator = validator or DistinctCandidateValidator()
        self.eligibility_validator = (
            eligibility_validator or CandidateEntityEligibilityValidator()
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        staged: StagedGoogleMapsCandidate,
        vacancy: EntityCityVacancy,
        existing: Iterable[ExistingCanonicalIdentity],
        *,
        run_id: str,
    ) -> CandidateDetailRun:
        _validate_staged_vacancy(staged, vacancy)
        mapping = build_candidate_detail_mapping(
            staged,
            vacancy,
            matched_at=self.clock(),
        )
        source_record = self.adapter.fetch(mapping, run_id=run_id)
        # Preserve the exact capture before normalization or eligibility gates.
        raw_path = self.raw_writer.write(source_record)
        observation = self.normalizer.normalize(source_record, mapping)
        candidate = project_detailed_candidate(staged, vacancy, observation)
        validation = self.validator.validate(candidate, existing)
        validation = enforce_detail_eligibility(candidate, validation)
        validation = self.eligibility_validator.validate(
            candidate,
            validation,
            vacancy=vacancy,
            google_observation=observation,
        )
        detail_id = stable_identifier(
            "candidate-detail",
            source_record.source_record_id,
            candidate.candidate_key,
        )
        payload = candidate_detail_payload(
            schema_version="1.0.0",
            detail_id=detail_id,
            run_id=run_id,
            source_record_id=source_record.source_record_id,
            observation_id=observation.observation_id,
            vacancy=vacancy,
            candidate=candidate,
            validation=validation,
        )
        detail = CandidateDetailStage(
            schema_version="1.0.0",
            detail_id=detail_id,
            detail_hash=stable_sha256(payload),
            run_id=run_id,
            source_record_id=source_record.source_record_id,
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
            vacancy=vacancy,
            candidate=candidate,
            validation=validation,
        )
        detail_path = self.detail_writer.write(detail)
        return CandidateDetailRun(
            mapping=mapping,
            source_record=source_record,
            raw_path=raw_path,
            observation=observation,
            detail=detail,
            detail_path=detail_path,
        )


def build_candidate_detail_mapping(
    staged: StagedGoogleMapsCandidate,
    vacancy: EntityCityVacancy,
    *,
    matched_at: datetime,
) -> ExternalEntityMapping:
    _validate_staged_vacancy(staged, vacancy)
    stable_external_id = _stable_external_id(staged)
    return ExternalEntityMapping(
        mapping_id=stable_identifier(
            "candidate-mapping",
            staged.candidate.candidate_key,
        ),
        entity_id=staged.candidate.candidate_key,
        entity_type=staged.candidate.entity_type,
        source_id="google-maps-web",
        external_id=stable_external_id or staged.candidate.candidate_key,
        external_url=staged.source_url,
        status=MappingStatus.AUTO_MATCHED,
        matched_at=matched_at,
        attributes={
            "candidate_detail_capture": True,
            "canonical_vacancy_id": vacancy.vacancy_id,
            "candidate_entity_type": staged.candidate.entity_type.value,
            "target_vacancy_entity_type": vacancy.entity_type.value,
            "search_query": staged.candidate.name,
        },
    )


def project_detailed_candidate(
    staged: StagedGoogleMapsCandidate,
    vacancy: EntityCityVacancy,
    observation: GoogleMapsPlaceObservation,
) -> CanonicalReplacementCandidate:
    _validate_staged_vacancy(staged, vacancy)
    if observation.place_id != staged.candidate.candidate_key:
        raise CandidateDetailError("detail observation belongs to another candidate")
    source_url = str(observation.source_url)
    stable_external_id = _stable_external_id(staged, source_url)
    location = observation.location
    if location is not None and location.source != "google-maps-web":
        location = None
    if not observation.name:
        raise CandidateDetailError("Google Maps detail did not resolve a place name")
    return CanonicalReplacementCandidate(
        candidate_key=staged.candidate.candidate_key,
        entity_type=staged.candidate.entity_type,
        city_id=vacancy.city_id,
        name=observation.name,
        resolved_address=observation.address,
        provider_category=observation.category,
        business_status=observation.business_status,
        phone=observation.phone,
        website_url=observation.website_url,
        location=location,
        external_identities=[
            CandidateExternalIdentity(
                source_id="google-maps-web",
                external_id=stable_external_id,
                external_url=source_url,
            )
        ],
    )


def enforce_detail_eligibility(
    candidate: CanonicalReplacementCandidate,
    validation: CandidateValidationResult,
) -> CandidateValidationResult:
    if validation.candidate_key != candidate.candidate_key:
        raise CandidateDetailError("validation belongs to another candidate")
    missing: set[CandidateReasonCode] = set()
    has_stable_google_id = any(
        identity.source_id == "google-maps-web" and identity.external_id
        for identity in candidate.external_identities
    )
    if not has_stable_google_id:
        missing.add(CandidateReasonCode.GOOGLE_STABLE_EXTERNAL_ID_MISSING)
    if candidate.location is None or candidate.location.source != "google-maps-web":
        missing.add(CandidateReasonCode.GOOGLE_SOURCED_COORDINATES_MISSING)
    if not missing:
        return validation
    status = validation.status
    if status is CandidateDisposition.PASS:
        status = CandidateDisposition.REVIEW
    return CandidateValidationResult(
        candidate_key=validation.candidate_key,
        status=status,
        reason_codes=sorted(
            set(validation.reason_codes) | missing,
            key=lambda item: item.value,
        ),
        matches=validation.matches,
    )


def _validate_staged_vacancy(
    staged: StagedGoogleMapsCandidate,
    vacancy: EntityCityVacancy,
) -> None:
    try:
        _validate_candidate_vacancy_type(staged.candidate.entity_type, vacancy)
    except ValueError as error:
        raise CandidateDetailError(str(error)) from error
    if staged.candidate.city_id != vacancy.city_id:
        raise CandidateDetailError("staged candidate city does not match vacancy")
    identities = staged.candidate.external_identities
    if not any(
        identity.source_id == "google-maps-web"
        and identity.external_url is not None
        and str(identity.external_url) == str(staged.source_url)
        for identity in identities
    ):
        raise CandidateDetailError(
            "staged candidate lacks its exact Google Maps external URL"
        )


def _stable_external_id(
    staged: StagedGoogleMapsCandidate,
    *additional_urls: str,
) -> str | None:
    urls = [*additional_urls, str(staged.source_url)]
    urls.extend(
        str(identity.external_url)
        for identity in staged.candidate.external_identities
        if identity.external_url is not None
    )
    for url in dict.fromkeys(urls):
        try:
            _, external_id = _google_maps_identity(url)
        except ValueError:
            continue
        if external_id:
            return external_id
    return None


def _path_segment(prefix: str, value: str) -> str:
    return f"{prefix}={quote(value, safe='-_.')}"
