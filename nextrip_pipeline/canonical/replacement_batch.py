from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import AwareDatetime, Field, model_validator

from nextrip_pipeline.schemas import EntityType, NexTripModel
from nextrip_pipeline.crawl.browser import CrawlBlockedError

from .candidate import (
    CandidateDisposition,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)
from .detail import CandidateDetailRun
from .detail import CandidateDetailStage, candidate_detail_payload
from .discovery import (
    GoogleMapsDiscoveryRun,
    StagedGoogleMapsCandidate,
    _validate_candidate_vacancy_type,
)
from .distinct import DistinctCandidateValidator
from .id_allocator import MonotonicPlaceIdAllocator
from .models import EntityCityVacancy, VacancyStatus, stable_sha256


MAX_REPLACEMENT_BATCH_VACANCIES = 500
MAX_REPLACEMENT_SEARCH_RESULTS = 50
MAX_REPLACEMENT_DETAIL_CANDIDATES = 50
DEFAULT_SEARCH_TERM_SENTINEL = "<adapter-default>"


class ReplacementProposalAlreadyExistsError(FileExistsError):
    """Raised instead of replacing an immutable proposal-batch summary."""


class ReplacementVacancyStatus(StrEnum):
    PROPOSED = "proposed"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class ReplacementCandidateFailureStatus(StrEnum):
    REVIEW = "review"
    REJECT = "reject"
    ERROR = "error"


class ReplacementDiscoveryService(Protocol):
    def run(
        self,
        vacancy: EntityCityVacancy,
        *,
        run_id: str,
        result_limit: int,
        search_term: str | None = None,
    ) -> GoogleMapsDiscoveryRun: ...


class ReplacementDetailService(Protocol):
    def run(
        self,
        staged: StagedGoogleMapsCandidate,
        vacancy: EntityCityVacancy,
        existing: Iterable[ExistingCanonicalIdentity],
        *,
        run_id: str,
    ) -> CandidateDetailRun: ...


@dataclass(frozen=True, slots=True)
class _PooledCandidate:
    discovery: GoogleMapsDiscoveryRun
    staged: StagedGoogleMapsCandidate
    query_order: int


def canonical_replacement_proposal_payload(
    *,
    schema_version: str,
    proposal_id: str,
    target_vacancy: EntityCityVacancy,
    proposed_place_id: str,
    candidate: CanonicalReplacementCandidate,
    validation: CandidateValidationResult,
    result_position: int,
    discovery_vacancy_id: str,
    discovery_stage_id: str,
    discovery_stage_hash: str,
    discovery_source_record_id: str,
    discovery_raw_path: str,
    discovery_stage_path: str,
    detail_id: str,
    detail_hash: str,
    detail_source_record_id: str,
    observation_id: str,
    detail_raw_path: str,
    detail_path: str,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "proposal_id": proposal_id,
        "target_vacancy": target_vacancy.model_dump(mode="json"),
        "proposed_place_id": proposed_place_id,
        "candidate": candidate.model_dump(mode="json"),
        "validation": validation.model_dump(mode="json"),
        "result_position": result_position,
        "discovery_vacancy_id": discovery_vacancy_id,
        "discovery_stage_id": discovery_stage_id,
        "discovery_stage_hash": discovery_stage_hash,
        "discovery_source_record_id": discovery_source_record_id,
        "discovery_raw_path": discovery_raw_path,
        "discovery_stage_path": discovery_stage_path,
        "detail_id": detail_id,
        "detail_hash": detail_hash,
        "detail_source_record_id": detail_source_record_id,
        "observation_id": observation_id,
        "detail_raw_path": detail_raw_path,
        "detail_path": detail_path,
    }


class CanonicalReplacementProposal(NexTripModel):
    """A PASS candidate proposed for approval; it does not fill the vacancy."""

    schema_version: str = "1.0.0"
    proposal_id: str = Field(min_length=1)
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_vacancy: EntityCityVacancy
    proposed_place_id: str = Field(min_length=1)
    candidate: CanonicalReplacementCandidate
    validation: CandidateValidationResult
    result_position: int = Field(ge=1)
    discovery_vacancy_id: str = Field(min_length=1)
    discovery_stage_id: str = Field(min_length=1)
    discovery_stage_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovery_source_record_id: str = Field(min_length=1)
    discovery_raw_path: str = Field(min_length=1)
    discovery_stage_path: str = Field(min_length=1)
    detail_id: str = Field(min_length=1)
    detail_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail_source_record_id: str = Field(min_length=1)
    observation_id: str = Field(min_length=1)
    detail_raw_path: str = Field(min_length=1)
    detail_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_proposal(self) -> CanonicalReplacementProposal:
        vacancy = self.target_vacancy
        if vacancy.status is not VacancyStatus.VACANT:
            raise ValueError("a proposal must retain a VACANT target vacancy")
        if vacancy.replacement_place_id is not None:
            raise ValueError("a proposal must not mutate replacement_place_id")
        if self.proposed_place_id == vacancy.retired_place_id:
            raise ValueError("a proposal cannot reuse the retired place ID")
        if self.candidate.city_id != vacancy.city_id:
            raise ValueError("proposal candidate does not match the vacancy city")
        _validate_candidate_vacancy_type(
            self.candidate.entity_type,
            vacancy,
        )
        if self.validation.candidate_key != self.candidate.candidate_key:
            raise ValueError("proposal validation belongs to another candidate")
        if self.validation.status is not CandidateDisposition.PASS:
            raise ValueError("only a PASS candidate may be proposed")
        expected_hash = stable_sha256(
            canonical_replacement_proposal_payload(
                schema_version=self.schema_version,
                proposal_id=self.proposal_id,
                target_vacancy=self.target_vacancy,
                proposed_place_id=self.proposed_place_id,
                candidate=self.candidate,
                validation=self.validation,
                result_position=self.result_position,
                discovery_vacancy_id=self.discovery_vacancy_id,
                discovery_stage_id=self.discovery_stage_id,
                discovery_stage_hash=self.discovery_stage_hash,
                discovery_source_record_id=self.discovery_source_record_id,
                discovery_raw_path=self.discovery_raw_path,
                discovery_stage_path=self.discovery_stage_path,
                detail_id=self.detail_id,
                detail_hash=self.detail_hash,
                detail_source_record_id=self.detail_source_record_id,
                observation_id=self.observation_id,
                detail_raw_path=self.detail_raw_path,
                detail_path=self.detail_path,
            )
        )
        if self.proposal_hash != expected_hash:
            raise ValueError("proposal_hash does not match proposal content")
        return self


class ReplacementCandidateFailure(NexTripModel):
    """One non-selected candidate, retained for review and audit."""

    candidate_key: str = Field(min_length=1)
    result_position: int = Field(ge=1)
    status: ReplacementCandidateFailureStatus
    validation: CandidateValidationResult | None = None
    discovery_stage_id: str = Field(min_length=1)
    detail_id: str | None = Field(default=None, min_length=1)
    detail_source_record_id: str | None = Field(default=None, min_length=1)
    detail_path: str | None = Field(default=None, min_length=1)
    error_stage: str | None = Field(default=None, min_length=1)
    error: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_failure(self) -> ReplacementCandidateFailure:
        if self.status is ReplacementCandidateFailureStatus.ERROR:
            if self.error_stage is None or self.error is None:
                raise ValueError("candidate ERROR requires error_stage and error")
            if self.validation is not None:
                raise ValueError("candidate ERROR cannot claim a validation result")
            return self
        if self.validation is None:
            raise ValueError("REVIEW/REJECT failure requires validation evidence")
        if self.validation.candidate_key != self.candidate_key:
            raise ValueError("failure validation belongs to another candidate")
        expected = ReplacementCandidateFailureStatus(
            self.validation.status.value
        )
        if self.status is not expected:
            raise ValueError("failure status does not match validation status")
        if self.error_stage is not None or self.error is not None:
            raise ValueError("validated failure cannot also contain an error")
        return self


class ReplacementVacancyResult(NexTripModel):
    """Independent outcome for one still-vacant historical quota slot."""

    vacancy: EntityCityVacancy
    status: ReplacementVacancyStatus
    discovery_vacancy_id: str | None = Field(default=None, min_length=1)
    discovery_stage_id: str | None = Field(default=None, min_length=1)
    discovery_source_record_id: str | None = Field(default=None, min_length=1)
    inspected_count: int = Field(ge=0)
    detail_request_count: int = Field(ge=0)
    proposal: CanonicalReplacementProposal | None = None
    failures: list[ReplacementCandidateFailure] = Field(default_factory=list)
    error_stage: str | None = Field(default=None, min_length=1)
    error: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_result(self) -> ReplacementVacancyResult:
        if self.vacancy.status is not VacancyStatus.VACANT:
            raise ValueError("replacement results may contain only VACANT slots")
        if self.detail_request_count > self.inspected_count:
            raise ValueError("detail requests cannot exceed inspected candidates")
        if self.status is ReplacementVacancyStatus.PROPOSED:
            if self.proposal is None:
                raise ValueError("PROPOSED result requires a proposal")
            if self.proposal.target_vacancy.vacancy_id != self.vacancy.vacancy_id:
                raise ValueError("proposal belongs to another vacancy")
        elif self.proposal is not None:
            raise ValueError("only PROPOSED results may contain a proposal")
        if self.status is ReplacementVacancyStatus.FAILED:
            if self.error_stage is None or self.error is None:
                raise ValueError("FAILED result requires error_stage and error")
        elif self.error_stage is not None or self.error is not None:
            raise ValueError("only FAILED results may contain a fatal error")
        return self


def canonical_replacement_batch_payload(
    *,
    schema_version: str,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    result_limit: int,
    max_detail_candidates: int,
    max_vacancies: int,
    candidate_entity_type: EntityType | None,
    identity_projection_hash: str | None,
    review_correction_overlay_id: str | None,
    review_correction_overlay_hash: str | None,
    search_plan: dict[str, list[str]],
    search_failures: list[str],
    input_vacancy_count: int,
    vacant_count: int,
    skipped_filled_count: int,
    unique_search_count: int,
    proposed_count: int,
    unresolved_count: int,
    failed_count: int,
    results: list[ReplacementVacancyResult],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "result_limit": result_limit,
        "max_detail_candidates": max_detail_candidates,
        "max_vacancies": max_vacancies,
        "identity_projection_hash": identity_projection_hash,
        "review_correction_overlay_id": review_correction_overlay_id,
        "review_correction_overlay_hash": review_correction_overlay_hash,
        "search_plan": search_plan,
        "search_failures": search_failures,
        "input_vacancy_count": input_vacancy_count,
        "vacant_count": vacant_count,
        "skipped_filled_count": skipped_filled_count,
        "unique_search_count": unique_search_count,
        "proposed_count": proposed_count,
        "unresolved_count": unresolved_count,
        "failed_count": failed_count,
        "results": [item.model_dump(mode="json") for item in results],
    }
    if candidate_entity_type is not None:
        payload["candidate_entity_type"] = candidate_entity_type.value
    return payload


class CanonicalReplacementProposalBatch(NexTripModel):
    """Content-hashed, approval-only result of one bounded replacement run."""

    schema_version: str = "1.0.0"
    batch_id: str = Field(min_length=1)
    batch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    result_limit: int = Field(ge=1, le=MAX_REPLACEMENT_SEARCH_RESULTS)
    max_detail_candidates: int = Field(
        ge=1,
        le=MAX_REPLACEMENT_DETAIL_CANDIDATES,
    )
    max_vacancies: int = Field(ge=1, le=MAX_REPLACEMENT_BATCH_VACANCIES)
    candidate_entity_type: EntityType | None = None
    identity_projection_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    review_correction_overlay_id: str | None = Field(default=None, min_length=1)
    review_correction_overlay_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    search_plan: dict[str, list[str]] = Field(default_factory=dict)
    search_failures: list[str] = Field(default_factory=list)
    input_vacancy_count: int = Field(ge=0)
    vacant_count: int = Field(ge=0)
    skipped_filled_count: int = Field(ge=0)
    unique_search_count: int = Field(ge=0)
    proposed_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    results: list[ReplacementVacancyResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_batch(self) -> CanonicalReplacementProposalBatch:
        if self.candidate_entity_type is not None:
            for result in self.results:
                _validate_candidate_vacancy_type(
                    self.candidate_entity_type,
                    result.vacancy,
                )
                if (
                    result.proposal is not None
                    and result.proposal.candidate.entity_type
                    is not self.candidate_entity_type
                ):
                    raise ValueError(
                        "proposal candidate type does not match batch candidate pool"
                    )
        if (self.review_correction_overlay_id is None) != (
            self.review_correction_overlay_hash is None
        ):
            raise ValueError(
                "review correction overlay ID and hash must be provided together"
            )
        if self.search_plan != {
            key: self.search_plan[key] for key in sorted(self.search_plan)
        }:
            raise ValueError("search_plan keys must be sorted")
        if any(
            not values or any(not value.strip() for value in values)
            for values in self.search_plan.values()
        ):
            raise ValueError("search_plan terms must be non-empty")
        if self.input_vacancy_count != (
            self.vacant_count + self.skipped_filled_count
        ):
            raise ValueError("input vacancy counts are inconsistent")
        if self.vacant_count != len(self.results):
            raise ValueError("vacant_count must equal per-vacancy result count")
        observed_counts = {
            status: sum(item.status is status for item in self.results)
            for status in ReplacementVacancyStatus
        }
        if self.proposed_count != observed_counts[ReplacementVacancyStatus.PROPOSED]:
            raise ValueError("proposed_count does not match results")
        if self.unresolved_count != observed_counts[
            ReplacementVacancyStatus.UNRESOLVED
        ]:
            raise ValueError("unresolved_count does not match results")
        if self.failed_count != observed_counts[ReplacementVacancyStatus.FAILED]:
            raise ValueError("failed_count does not match results")
        vacancy_ids = [item.vacancy.vacancy_id for item in self.results]
        if len(vacancy_ids) != len(set(vacancy_ids)):
            raise ValueError("batch results contain duplicate vacancy IDs")
        expected_hash = stable_sha256(
            canonical_replacement_batch_payload(
                schema_version=self.schema_version,
                run_id=self.run_id,
                started_at=self.started_at,
                finished_at=self.finished_at,
                result_limit=self.result_limit,
                max_detail_candidates=self.max_detail_candidates,
                max_vacancies=self.max_vacancies,
                candidate_entity_type=self.candidate_entity_type,
                identity_projection_hash=self.identity_projection_hash,
                review_correction_overlay_id=self.review_correction_overlay_id,
                review_correction_overlay_hash=self.review_correction_overlay_hash,
                search_plan=self.search_plan,
                search_failures=self.search_failures,
                input_vacancy_count=self.input_vacancy_count,
                vacant_count=self.vacant_count,
                skipped_filled_count=self.skipped_filled_count,
                unique_search_count=self.unique_search_count,
                proposed_count=self.proposed_count,
                unresolved_count=self.unresolved_count,
                failed_count=self.failed_count,
                results=self.results,
            )
        )
        if self.batch_hash != expected_hash:
            raise ValueError("batch_hash does not match batch content")
        if self.batch_id != f"canonical_replacement_{expected_hash[:20]}":
            raise ValueError("batch_id does not match batch_hash")
        return self


class CanonicalReplacementProposalBatchWriter:
    """Persist a proposal summary exactly once; approval remains separate."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, summary: CanonicalReplacementProposalBatch) -> Path:
        return self.root_directory / f"run={quote(summary.run_id, safe='-_.')}.json"

    def write(self, summary: CanonicalReplacementProposalBatch) -> Path:
        validated = CanonicalReplacementProposalBatch.model_validate(
            summary.model_dump(mode="python")
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
            raise ReplacementProposalAlreadyExistsError(
                f"Replacement proposal summary already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        return destination


class CanonicalReplacementProposalBatchRunner:
    """Find distinct replacements without changing canonical/master state."""

    def __init__(
        self,
        discovery_service: ReplacementDiscoveryService,
        detail_service: ReplacementDetailService,
        allocator: MonotonicPlaceIdAllocator,
        summary_writer: CanonicalReplacementProposalBatchWriter,
        *,
        result_limit: int = 20,
        max_detail_candidates: int = 5,
        max_vacancies: int = 100,
        search_terms: Mapping[tuple[str, EntityType], Sequence[str]] | None = None,
        candidate_entity_type: EntityType | None = None,
        pre_detail_validator: DistinctCandidateValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_search_terms = _normalize_search_terms(search_terms or {})
        if candidate_entity_type is not None and not isinstance(
            candidate_entity_type,
            EntityType,
        ):
            raise ValueError("candidate_entity_type must be an EntityType")
        _validate_bound(
            "result_limit",
            result_limit,
            maximum=MAX_REPLACEMENT_SEARCH_RESULTS,
        )
        _validate_bound(
            "max_detail_candidates",
            max_detail_candidates,
            maximum=MAX_REPLACEMENT_DETAIL_CANDIDATES,
        )
        _validate_bound(
            "max_vacancies",
            max_vacancies,
            maximum=MAX_REPLACEMENT_BATCH_VACANCIES,
        )
        maximum_term_count = max(
            (len(terms) for terms in normalized_search_terms.values()),
            default=1,
        )
        available_candidate_count = result_limit * maximum_term_count
        if max_detail_candidates > available_candidate_count:
            if maximum_term_count == 1:
                raise ValueError(
                    "max_detail_candidates cannot exceed result_limit"
                )
            raise ValueError(
                "max_detail_candidates cannot exceed result_limit multiplied "
                "by the maximum search-term count"
            )
        self.discovery_service = discovery_service
        self.detail_service = detail_service
        self.allocator = allocator
        self.summary_writer = summary_writer
        self.result_limit = result_limit
        self.max_detail_candidates = max_detail_candidates
        self.max_vacancies = max_vacancies
        self.search_terms = normalized_search_terms
        self.candidate_entity_type = candidate_entity_type
        self.pre_detail_validator = pre_detail_validator or DistinctCandidateValidator()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        vacancies: Sequence[EntityCityVacancy],
        existing: Iterable[ExistingCanonicalIdentity],
        *,
        run_id: str,
        identity_projection_hash: str | None = None,
        review_correction_overlay_id: str | None = None,
        review_correction_overlay_hash: str | None = None,
    ) -> tuple[CanonicalReplacementProposalBatch, Path]:
        if not run_id.strip():
            raise ValueError("run_id must not be blank")
        vacancy_ids = [item.vacancy_id for item in vacancies]
        if len(vacancy_ids) != len(set(vacancy_ids)):
            raise ValueError("input contains duplicate vacancy IDs")

        started_at = self.clock()
        vacant = sorted(
            (item for item in vacancies if item.status is VacancyStatus.VACANT),
            key=_vacancy_sort_key,
        )
        if len(vacant) > self.max_vacancies:
            raise ValueError(
                "open vacancy count exceeds "
                f"max_vacancies={self.max_vacancies}"
            )
        for vacancy in vacant:
            _validate_candidate_vacancy_type(
                self.candidate_entity_type or vacancy.entity_type,
                vacancy,
            )
        working_existing = list(existing)
        if len({item.place_id for item in working_existing}) != len(working_existing):
            raise ValueError("existing identities contain duplicate place IDs")

        if (review_correction_overlay_id is None) != (
            review_correction_overlay_hash is None
        ):
            raise ValueError(
                "review correction overlay ID and hash must be provided together"
            )

        by_slot: dict[tuple[str, EntityType], list[EntityCityVacancy]] = {}
        for vacancy in vacant:
            by_slot.setdefault(
                (vacancy.city_id, vacancy.entity_type), []
            ).append(vacancy)

        search_plan = {
            _search_plan_key(slot, self._candidate_type_for_slot(slot)):
            list(self._terms_for_slot(slot))
            for slot in sorted(by_slot, key=_slot_sort_key)
        }
        unique_search_count = 0
        search_failures: list[str] = []
        results: list[ReplacementVacancyResult] = []
        blocked_error: str | None = None
        for slot in sorted(by_slot, key=_slot_sort_key):
            slot_vacancies = by_slot[slot]
            representative = slot_vacancies[0]
            discoveries: list[GoogleMapsDiscoveryRun] = []
            slot_errors: list[str] = []
            for term in self._terms_for_slot(slot):
                unique_search_count += 1
                try:
                    discovery_kwargs = {
                        "run_id": _child_run_id(
                            run_id,
                            "discovery",
                            representative.vacancy_id,
                            term,
                        ),
                        "result_limit": self.result_limit,
                    }
                    if term != DEFAULT_SEARCH_TERM_SENTINEL:
                        discovery_kwargs["search_term"] = term
                    if self.candidate_entity_type is not None:
                        discovery_kwargs["candidate_entity_type"] = (
                            self.candidate_entity_type
                        )
                    discovery = self.discovery_service.run(
                        representative,
                        **discovery_kwargs,
                    )
                except CrawlBlockedError as error:
                    blocked_error = _error_text(error)
                    message = f"{_slot_key(slot)} | {term} | {blocked_error}"
                    search_failures.append(message)
                    slot_errors.append(message)
                    break
                except Exception as error:
                    message = (
                        f"{_slot_key(slot)} | {term} | {_error_text(error)}"
                    )
                    search_failures.append(message)
                    slot_errors.append(message)
                    continue
                discoveries.append(discovery)

            if blocked_error is not None:
                break
            if not discoveries:
                error = "; ".join(slot_errors) or "no discovery search completed"
                results.extend(
                    _failed_results(
                        slot_vacancies,
                        error_stage="discovery",
                        error=error,
                    )
                )
                continue
            slot_results, blocked_error = self._process_slot(
                    slot_vacancies,
                    discoveries,
                    working_existing,
                    batch_run_id=run_id,
                )
            results.extend(slot_results)
            if blocked_error is not None:
                break

        if blocked_error is not None:
            completed_ids = {item.vacancy.vacancy_id for item in results}
            remaining = [
                vacancy
                for vacancy in vacant
                if vacancy.vacancy_id not in completed_ids
            ]
            results.extend(
                _failed_results(
                    remaining,
                    error_stage="crawl_blocked_circuit_breaker",
                    error=blocked_error,
                )
            )

        results.sort(key=lambda item: _vacancy_sort_key(item.vacancy))

        finished_at = self.clock()
        counts = {
            status: sum(item.status is status for item in results)
            for status in ReplacementVacancyStatus
        }
        values: dict[str, object] = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "result_limit": self.result_limit,
            "max_detail_candidates": self.max_detail_candidates,
            "max_vacancies": self.max_vacancies,
            "candidate_entity_type": self.candidate_entity_type,
            "identity_projection_hash": identity_projection_hash,
            "review_correction_overlay_id": review_correction_overlay_id,
            "review_correction_overlay_hash": review_correction_overlay_hash,
            "search_plan": search_plan,
            "search_failures": search_failures,
            "input_vacancy_count": len(vacancies),
            "vacant_count": len(vacant),
            "skipped_filled_count": len(vacancies) - len(vacant),
            "unique_search_count": unique_search_count,
            "proposed_count": counts[ReplacementVacancyStatus.PROPOSED],
            "unresolved_count": counts[ReplacementVacancyStatus.UNRESOLVED],
            "failed_count": counts[ReplacementVacancyStatus.FAILED],
            "results": results,
        }
        batch_hash = stable_sha256(
            canonical_replacement_batch_payload(
                **values,  # type: ignore[arg-type]
            )
        )
        summary = CanonicalReplacementProposalBatch(
            batch_id=f"canonical_replacement_{batch_hash[:20]}",
            batch_hash=batch_hash,
            **values,
        )
        return summary, self.summary_writer.write(summary)

    def _process_slot(
        self,
        vacancies: Sequence[EntityCityVacancy],
        discoveries: Sequence[GoogleMapsDiscoveryRun],
        working_existing: list[ExistingCanonicalIdentity],
        *,
        batch_run_id: str,
    ) -> tuple[list[ReplacementVacancyResult], str | None]:
        slot = (vacancies[0].city_id, vacancies[0].entity_type)
        expected_candidate_type = self._candidate_type_for_slot(slot)
        for discovery in discoveries:
            discovered_slot = (
                discovery.stage.vacancy.city_id,
                discovery.stage.vacancy.entity_type,
            )
            if discovered_slot != slot:
                return (
                    _failed_results(
                        vacancies,
                        error_stage="discovery_rebind",
                        error=(
                            "discovery stage belongs to another entity/city slot"
                        ),
                    ),
                    None,
                )
            discovered_candidate_type = (
                discovery.stage.candidate_entity_type
                or discovery.stage.vacancy.entity_type
            )
            if discovered_candidate_type is not expected_candidate_type:
                return (
                    _failed_results(
                        vacancies,
                        error_stage="discovery_rebind",
                        error="discovery stage belongs to another candidate pool",
                    ),
                    None,
                )

        pool = _candidate_pool(discoveries)[: self.max_detail_candidates]
        used_candidate_keys: set[str] = set()
        detail_cache: dict[str, CandidateDetailRun | str] = {}
        results: list[ReplacementVacancyResult] = []
        for vacancy in vacancies:
            failures: list[ReplacementCandidateFailure] = []
            inspected_count = 0
            detail_request_count = 0
            for pooled in pool:
                inspected_count += 1
                discovery = pooled.discovery
                discovery_vacancy = discovery.stage.vacancy
                rebound = _rebind_staged_candidate(
                    pooled.staged,
                    discovery_vacancy=discovery_vacancy,
                    target_vacancy=vacancy,
                    candidate_entity_type=expected_candidate_type,
                )
                pre_validation = self.pre_detail_validator.validate(
                    rebound.candidate,
                    working_existing,
                )
                if pre_validation.status is CandidateDisposition.REJECT:
                    failures.append(
                        _validated_failure(
                            rebound,
                            discovery_stage_id=discovery.stage.stage_id,
                            validation=pre_validation,
                        )
                    )
                    continue

                cached_detail = detail_cache.get(rebound.candidate.candidate_key)
                try:
                    if isinstance(cached_detail, CandidateDetailRun):
                        detail_run = _rebind_detail_run(cached_detail, vacancy)
                    elif isinstance(cached_detail, str):
                        raise RuntimeError(cached_detail)
                    else:
                        detail_request_count += 1
                        detail_run = self.detail_service.run(
                            rebound,
                            vacancy,
                            tuple(working_existing),
                            run_id=_child_run_id(
                                batch_run_id,
                                "detail",
                                vacancy.vacancy_id,
                                rebound.candidate.candidate_key,
                            ),
                        )
                        _validate_detail_run(detail_run, rebound, vacancy)
                        detail_cache[rebound.candidate.candidate_key] = detail_run
                except CrawlBlockedError as error:
                    message = _error_text(error)
                    results.append(
                        ReplacementVacancyResult(
                            vacancy=vacancy,
                            status=ReplacementVacancyStatus.FAILED,
                            discovery_vacancy_id=discovery_vacancy.vacancy_id,
                            discovery_stage_id=discovery.stage.stage_id,
                            discovery_source_record_id=(
                                discovery.stage.source_record_id
                            ),
                            inspected_count=inspected_count,
                            detail_request_count=detail_request_count,
                            failures=failures,
                            error_stage="detail_crawl_blocked",
                            error=message,
                        )
                    )
                    return results, message
                except Exception as error:
                    if not isinstance(cached_detail, CandidateDetailRun):
                        detail_cache.setdefault(
                            rebound.candidate.candidate_key, _error_text(error)
                        )
                    failures.append(
                        ReplacementCandidateFailure(
                            candidate_key=rebound.candidate.candidate_key,
                            result_position=rebound.result_position,
                            status=ReplacementCandidateFailureStatus.ERROR,
                            discovery_stage_id=discovery.stage.stage_id,
                            error_stage="detail",
                            error=_error_text(error),
                        )
                    )
                    continue

                validation = detail_run.detail.validation
                if validation.status is not CandidateDisposition.PASS:
                    failures.append(
                        _validated_failure(
                            rebound,
                            discovery_stage_id=discovery.stage.stage_id,
                            validation=validation,
                            detail_run=detail_run,
                        )
                    )
                    continue

                try:
                    proposed_place_id = self.allocator.allocate(
                        detail_run.detail.candidate,
                        validation,
                        replacement_of=(
                            vacancy.retired_place_id
                            if detail_run.detail.candidate.entity_type
                            is vacancy.entity_type
                            else None
                        ),
                    )
                    if any(
                        item.place_id == proposed_place_id
                        for item in working_existing
                    ):
                        raise ValueError("allocator returned an existing place ID")
                    proposal = _build_proposal(
                        vacancy=vacancy,
                        discovery=discovery,
                        staged=rebound,
                        detail_run=detail_run,
                        proposed_place_id=proposed_place_id,
                    )
                except Exception as error:
                    results.append(
                        ReplacementVacancyResult(
                            vacancy=vacancy,
                            status=ReplacementVacancyStatus.FAILED,
                            discovery_vacancy_id=discovery_vacancy.vacancy_id,
                            discovery_stage_id=discovery.stage.stage_id,
                            discovery_source_record_id=(
                                discovery.stage.source_record_id
                            ),
                            inspected_count=inspected_count,
                            detail_request_count=detail_request_count,
                            failures=failures,
                            error_stage="allocation",
                            error=_error_text(error),
                        )
                    )
                    break

                working_existing.append(_existing_from_proposal(proposal))
                used_candidate_keys.add(rebound.candidate.candidate_key)
                results.append(
                    ReplacementVacancyResult(
                        vacancy=vacancy,
                        status=ReplacementVacancyStatus.PROPOSED,
                        discovery_vacancy_id=discovery_vacancy.vacancy_id,
                        discovery_stage_id=discovery.stage.stage_id,
                        discovery_source_record_id=discovery.stage.source_record_id,
                        inspected_count=inspected_count,
                        detail_request_count=detail_request_count,
                        proposal=proposal,
                        failures=failures,
                    )
                )
                break
            else:
                reference = discoveries[0]
                results.append(
                    ReplacementVacancyResult(
                        vacancy=vacancy,
                        status=ReplacementVacancyStatus.UNRESOLVED,
                        discovery_vacancy_id=reference.stage.vacancy.vacancy_id,
                        discovery_stage_id=reference.stage.stage_id,
                        discovery_source_record_id=reference.stage.source_record_id,
                        inspected_count=inspected_count,
                        detail_request_count=detail_request_count,
                        failures=failures,
                    )
                )
        return results, None

    def _terms_for_slot(self, slot: tuple[str, EntityType]) -> tuple[str, ...]:
        return self.search_terms.get(slot, (DEFAULT_SEARCH_TERM_SENTINEL,))

    def _candidate_type_for_slot(
        self,
        slot: tuple[str, EntityType],
    ) -> EntityType:
        return self.candidate_entity_type or slot[1]


def _rebind_staged_candidate(
    staged: StagedGoogleMapsCandidate,
    *,
    discovery_vacancy: EntityCityVacancy,
    target_vacancy: EntityCityVacancy,
    candidate_entity_type: EntityType,
) -> StagedGoogleMapsCandidate:
    """Clone only candidate context; shared discovery evidence stays unchanged."""

    if (discovery_vacancy.city_id, discovery_vacancy.entity_type) != (
        target_vacancy.city_id,
        target_vacancy.entity_type,
    ):
        raise ValueError("a discovery candidate cannot cross entity/city slots")
    if staged.candidate.entity_type is not candidate_entity_type:
        raise ValueError("discovery candidate belongs to another candidate pool")
    _validate_candidate_vacancy_type(candidate_entity_type, target_vacancy)
    candidate = staged.candidate.model_copy(
        update={
            "city_id": target_vacancy.city_id,
            "entity_type": candidate_entity_type,
        },
        deep=True,
    )
    return staged.model_copy(update={"candidate": candidate}, deep=True)


def _validated_failure(
    staged: StagedGoogleMapsCandidate,
    *,
    discovery_stage_id: str,
    validation: CandidateValidationResult,
    detail_run: CandidateDetailRun | None = None,
) -> ReplacementCandidateFailure:
    return ReplacementCandidateFailure(
        candidate_key=staged.candidate.candidate_key,
        result_position=staged.result_position,
        status=ReplacementCandidateFailureStatus(validation.status.value),
        validation=validation,
        discovery_stage_id=discovery_stage_id,
        detail_id=(detail_run.detail.detail_id if detail_run else None),
        detail_source_record_id=(
            detail_run.detail.source_record_id if detail_run else None
        ),
        detail_path=(str(detail_run.detail_path) if detail_run else None),
    )


def _build_proposal(
    *,
    vacancy: EntityCityVacancy,
    discovery: GoogleMapsDiscoveryRun,
    staged: StagedGoogleMapsCandidate,
    detail_run: CandidateDetailRun,
    proposed_place_id: str,
) -> CanonicalReplacementProposal:
    proposal_id = (
        f"replacement_{vacancy.vacancy_id}_{detail_run.detail.candidate.candidate_key}"
    )
    values: dict[str, object] = {
        "schema_version": "1.0.0",
        "proposal_id": proposal_id,
        "target_vacancy": vacancy,
        "proposed_place_id": proposed_place_id,
        "candidate": detail_run.detail.candidate,
        "validation": detail_run.detail.validation,
        "result_position": staged.result_position,
        "discovery_vacancy_id": discovery.stage.vacancy.vacancy_id,
        "discovery_stage_id": discovery.stage.stage_id,
        "discovery_stage_hash": discovery.stage.stage_hash,
        "discovery_source_record_id": discovery.stage.source_record_id,
        "discovery_raw_path": str(discovery.raw_path),
        "discovery_stage_path": str(discovery.stage_path),
        "detail_id": detail_run.detail.detail_id,
        "detail_hash": detail_run.detail.detail_hash,
        "detail_source_record_id": detail_run.detail.source_record_id,
        "observation_id": detail_run.detail.observation_id,
        "detail_raw_path": str(detail_run.raw_path),
        "detail_path": str(detail_run.detail_path),
    }
    proposal_hash = stable_sha256(
        canonical_replacement_proposal_payload(
            **values,  # type: ignore[arg-type]
        )
    )
    return CanonicalReplacementProposal(
        proposal_hash=proposal_hash,
        **values,
    )


def _existing_from_proposal(
    proposal: CanonicalReplacementProposal,
) -> ExistingCanonicalIdentity:
    candidate = proposal.candidate
    return ExistingCanonicalIdentity(
        place_id=proposal.proposed_place_id,
        entity_type=candidate.entity_type,
        city_id=candidate.city_id,
        name=candidate.name,
        phone=candidate.phone,
        website_url=candidate.website_url,
        location=candidate.location,
        external_identities=candidate.external_identities,
    )


def _validate_detail_run(
    detail_run: CandidateDetailRun,
    staged: StagedGoogleMapsCandidate,
    vacancy: EntityCityVacancy,
) -> None:
    detail = detail_run.detail
    if detail.vacancy.vacancy_id != vacancy.vacancy_id:
        raise ValueError("detail result belongs to another vacancy")
    if detail.candidate.candidate_key != staged.candidate.candidate_key:
        raise ValueError("detail result belongs to another staged candidate")
    if detail.validation.candidate_key != staged.candidate.candidate_key:
        raise ValueError("detail validation belongs to another staged candidate")


def _rebind_detail_run(
    detail_run: CandidateDetailRun,
    vacancy: EntityCityVacancy,
) -> CandidateDetailRun:
    """Reuse one immutable Google detail capture for another slot vacancy."""
    detail = detail_run.detail
    if detail.vacancy.vacancy_id == vacancy.vacancy_id:
        return detail_run
    payload = detail.model_dump(mode="python")
    payload["vacancy"] = vacancy
    payload["detail_hash"] = stable_sha256(
        candidate_detail_payload(
            schema_version=detail.schema_version,
            detail_id=detail.detail_id,
            run_id=detail.run_id,
            source_record_id=detail.source_record_id,
            observation_id=detail.observation_id,
            vacancy=vacancy,
            candidate=detail.candidate,
            validation=detail.validation,
        )
    )
    rebound = CandidateDetailStage.model_validate(payload)
    return CandidateDetailRun(
        mapping=detail_run.mapping,
        source_record=detail_run.source_record,
        raw_path=detail_run.raw_path,
        observation=detail_run.observation,
        detail=rebound,
        detail_path=detail_run.detail_path,
    )


def _vacancy_sort_key(vacancy: EntityCityVacancy) -> tuple[str, str, str]:
    return (
        vacancy.entity_type.value,
        vacancy.city_id,
        vacancy.vacancy_id,
    )


def _child_run_id(run_id: str, stage: str, *parts: str) -> str:
    suffix = stable_sha256([stage, *parts])[:16]
    return f"{run_id}-{stage}-{suffix}"


def _validate_bound(name: str, value: int, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _error_text(error: Exception) -> str:
    # Pydantic's shared model config strips leading/trailing whitespace from
    # strings. Normalize the error before computing the content hash so a
    # multiline Playwright/HTTP error cannot make the persisted batch differ
    # from the payload that was hashed.
    return f"{type(error).__name__}: {error}".strip()


def _normalize_search_terms(
    values: Mapping[tuple[str, EntityType], Sequence[str]],
) -> dict[tuple[str, EntityType], tuple[str, ...]]:
    normalized: dict[tuple[str, EntityType], tuple[str, ...]] = {}
    for slot, terms in values.items():
        if not isinstance(slot, tuple) or len(slot) != 2:
            raise ValueError("search_terms keys must be (city_id, entity_type)")
        city_id, entity_type = slot
        if not isinstance(city_id, str) or not city_id.strip():
            raise ValueError("search_terms city_id must not be blank")
        if not isinstance(entity_type, EntityType):
            raise ValueError("search_terms entity_type must be EntityType")
        if isinstance(terms, str):
            terms = (terms,)
        cleaned = tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))
        if not cleaned:
            raise ValueError(f"search terms for {city_id}/{entity_type.value} are empty")
        normalized[(city_id.strip(), entity_type)] = cleaned
    return normalized


def _slot_key(slot: tuple[str, EntityType]) -> str:
    return f"{slot[0]}/{slot[1].value}"


def _search_plan_key(
    target_slot: tuple[str, EntityType],
    candidate_entity_type: EntityType,
) -> str:
    target = _slot_key(target_slot)
    if candidate_entity_type is target_slot[1]:
        return target
    return f"{target}->{candidate_entity_type.value}"


def _slot_sort_key(slot: tuple[str, EntityType]) -> tuple[str, str]:
    return (slot[0], slot[1].value)


def _candidate_pool(
    discoveries: Sequence[GoogleMapsDiscoveryRun],
) -> list[_PooledCandidate]:
    """Union discovery results, keeping the first occurrence of each identity."""
    selected: dict[str, _PooledCandidate] = {}
    for query_order, discovery in enumerate(discoveries):
        for staged in discovery.stage.candidates:
            identities = staged.candidate.external_identities
            identity_key = (
                identities[0].external_id
                if identities and identities[0].external_id
                else staged.candidate.candidate_key
            )
            key = str(identity_key)
            pooled = _PooledCandidate(discovery, staged, query_order)
            previous = selected.get(key)
            if previous is None or (query_order, staged.result_position) < (
                previous.query_order,
                previous.staged.result_position,
            ):
                selected[key] = pooled
    return sorted(
        selected.values(),
        key=lambda item: (
            item.query_order,
            item.staged.result_position,
            item.staged.candidate.candidate_key,
        ),
    )


def _failed_results(
    vacancies: Sequence[EntityCityVacancy],
    *,
    error_stage: str,
    error: str,
) -> list[ReplacementVacancyResult]:
    return [
        ReplacementVacancyResult(
            vacancy=vacancy,
            status=ReplacementVacancyStatus.FAILED,
            inspected_count=0,
            detail_request_count=0,
            error_stage=error_stage,
            error=error,
        )
        for vacancy in vacancies
    ]
