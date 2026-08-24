from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nextrip_pipeline.canonical.candidate import (
    CandidateDisposition,
    CandidateExternalIdentity,
    CandidateReasonCode,
    CandidateValidationResult,
    CanonicalReplacementCandidate,
    ExistingCanonicalIdentity,
)
from nextrip_pipeline.canonical.discovery import (
    GoogleMapsCandidateStage,
    GoogleMapsDiscoveryRun,
    StagedGoogleMapsCandidate,
    google_maps_candidate_stage_payload,
)
from nextrip_pipeline.canonical.distinct import DistinctCandidateValidator
from nextrip_pipeline.canonical.id_allocator import MonotonicPlaceIdAllocator
from nextrip_pipeline.canonical.models import (
    EntityCityVacancy,
    VacancyStatus,
    stable_identifier,
    stable_sha256,
)
from nextrip_pipeline.canonical.replacement_batch import (
    CanonicalReplacementProposalBatchRunner,
    CanonicalReplacementProposalBatchWriter,
    ReplacementCandidateFailureStatus,
    ReplacementProposalAlreadyExistsError,
    ReplacementVacancyStatus,
)
from nextrip_pipeline.schemas import EntityType, GeoPoint, RecordSubjectType, SourceRecord


NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def _vacancy(
    retired_place_id: str,
    *,
    entity_type: EntityType = EntityType.CAFE,
    city_id: str = "city_da_nang",
    status: VacancyStatus = VacancyStatus.VACANT,
) -> EntityCityVacancy:
    replacement = None
    if status is VacancyStatus.FILLED:
        replacement = (
            "cafe_dn_999"
            if entity_type is EntityType.CAFE
            else "hotel_dn_999"
        )
    return EntityCityVacancy(
        vacancy_id=stable_identifier(
            "vacancy",
            city_id,
            entity_type.value,
            retired_place_id,
        ),
        retired_place_id=retired_place_id,
        city_id=city_id,
        entity_type=entity_type,
        status=status,
        replacement_place_id=replacement,
    )


def _staged(position: int, token: str, name: str) -> StagedGoogleMapsCandidate:
    source_url = (
        f"https://www.google.com/maps/place/{name.replace(' ', '+')}"
        f"/@16.0{position},108.2,17z/data=!4m2!3m1!1s{token}"
    )
    return StagedGoogleMapsCandidate(
        candidate=CanonicalReplacementCandidate(
            candidate_key=stable_identifier("candidate", token),
            entity_type=EntityType.CAFE,
            city_id="city_da_nang",
            name=name,
            external_identities=[
                CandidateExternalIdentity(
                    source_id="google-maps-web",
                    external_id=token,
                    external_url=source_url,
                )
            ],
        ),
        source_url=source_url,
        result_position=position,
    )


def _discovery_run(
    vacancy: EntityCityVacancy,
    candidates: list[StagedGoogleMapsCandidate],
    *,
    run_id: str,
    root: Path,
    candidate_entity_type: EntityType | None = None,
) -> GoogleMapsDiscoveryRun:
    effective_entity_type = candidate_entity_type or vacancy.entity_type
    source_record_id = f"source-{vacancy.vacancy_id}"
    payload = {"search": vacancy.vacancy_id}
    source = SourceRecord(
        source_record_id=source_record_id,
        run_id=run_id,
        source_id="google-maps-web",
        entity_type=effective_entity_type,
        subject_type=RecordSubjectType.PLACE,
        subject_id=vacancy.vacancy_id,
        crawled_at=NOW,
        raw_payload=payload,
        content_hash=stable_sha256(payload),
        parser_version="test",
        source_url="https://www.google.com/maps/search/cafe",
    )
    stage_id = stable_identifier("candidate-stage", source_record_id)
    values = {
        "schema_version": "1.0.0",
        "stage_id": stage_id,
        "run_id": run_id,
        "source_record_id": source_record_id,
        "vacancy": vacancy,
        "query": f"{vacancy.entity_type.value} in {vacancy.city_id}",
        "result_limit": 10,
        "candidates": candidates,
        "candidate_entity_type": (
            effective_entity_type
            if effective_entity_type is not vacancy.entity_type
            else None
        ),
    }
    stage = GoogleMapsCandidateStage(
        stage_hash=stable_sha256(
            google_maps_candidate_stage_payload(**values)
        ),
        discovered_at=NOW,
        **values,
    )
    return GoogleMapsDiscoveryRun(
        source_record=source,
        raw_path=root / f"{source_record_id}-raw.json",
        stage=stage,
        stage_path=root / f"{stage_id}.json",
    )


class FakeDiscoveryService:
    def __init__(
        self,
        root: Path,
        candidates: list[StagedGoogleMapsCandidate],
        *,
        fail_types: set[EntityType] | None = None,
    ) -> None:
        self.root = root
        self.candidates = candidates
        self.fail_types = fail_types or set()
        self.calls: list[EntityCityVacancy] = []

    def run(
        self,
        vacancy: EntityCityVacancy,
        *,
        run_id: str,
        result_limit: int,
        search_term: str | None = None,
        candidate_entity_type: EntityType | None = None,
    ) -> GoogleMapsDiscoveryRun:
        self.calls.append(vacancy)
        if vacancy.entity_type in self.fail_types:
            raise RuntimeError(f"blocked {vacancy.entity_type.value} search")
        candidates = [
            item.model_copy(
                update={
                    "candidate": item.candidate.model_copy(
                        update={
                            "entity_type": (
                                candidate_entity_type or vacancy.entity_type
                            ),
                            "city_id": vacancy.city_id,
                        },
                        deep=True,
                    )
                },
                deep=True,
            )
            for item in self.candidates[:result_limit]
        ]
        return _discovery_run(
            vacancy,
            candidates,
            run_id=run_id,
            root=self.root,
            candidate_entity_type=candidate_entity_type,
        )


class FakeDetailService:
    def __init__(self, behavior: dict[str, str] | None = None) -> None:
        self.behavior = behavior or {}
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def run(
        self,
        staged: StagedGoogleMapsCandidate,
        vacancy: EntityCityVacancy,
        existing: tuple[ExistingCanonicalIdentity, ...],
        *,
        run_id: str,
    ) -> object:
        candidate_key = staged.candidate.candidate_key
        self.calls.append(
            (
                vacancy.vacancy_id,
                candidate_key,
                tuple(item.place_id for item in existing),
            )
        )
        behavior = self.behavior.get(candidate_key, "validate")
        if behavior == "error":
            raise RuntimeError("detail capture failed")
        candidate = staged.candidate.model_copy(
            update={
                "name": f"Google {staged.candidate.name}",
                "location": GeoPoint(
                    latitude=16.0 + staged.result_position / 100,
                    longitude=108.2,
                    source="google-maps-web",
                ),
            },
            deep=True,
        )
        if behavior == "review":
            validation = CandidateValidationResult(
                candidate_key=candidate_key,
                status=CandidateDisposition.REVIEW,
                reason_codes=[
                    CandidateReasonCode.GOOGLE_SOURCED_COORDINATES_MISSING
                ],
            )
        else:
            validation = DistinctCandidateValidator().validate(candidate, existing)
        detail_id = stable_identifier(
            "candidate-detail",
            vacancy.vacancy_id,
            candidate_key,
        )
        detail = SimpleNamespace(
            vacancy=vacancy,
            candidate=candidate,
            validation=validation,
            detail_id=detail_id,
            detail_hash="d" * 64,
            source_record_id=f"detail-source-{candidate_key}",
            observation_id=f"observation-{candidate_key}",
        )
        return SimpleNamespace(
            detail=detail,
            raw_path=Path(f"raw/{detail_id}.json"),
            detail_path=Path(f"detail/{detail_id}.json"),
        )


def _runner(
    tmp_path: Path,
    discovery: FakeDiscoveryService,
    detail: FakeDetailService,
    vacancies: list[EntityCityVacancy],
    *,
    max_detail_candidates: int = 5,
    max_vacancies: int = 100,
) -> CanonicalReplacementProposalBatchRunner:
    return CanonicalReplacementProposalBatchRunner(
        discovery,
        detail,  # type: ignore[arg-type]
        MonotonicPlaceIdAllocator(
            existing_ids=["cafe_dn_099", "hotel_dn_073"],
            retired_ids=[item.retired_place_id for item in vacancies],
        ),
        CanonicalReplacementProposalBatchWriter(tmp_path / "summaries"),
        result_limit=10,
        max_detail_candidates=max_detail_candidates,
        max_vacancies=max_vacancies,
        clock=lambda: NOW,
    )


def test_one_search_per_slot_reuses_pool_and_prevents_candidate_reuse(
    tmp_path: Path,
) -> None:
    # Feed order is intentionally reversed; selection must follow position.
    candidates = [
        _staged(2, "0x2:0x22", "Second Cafe"),
        _staged(1, "0x1:0x11", "First Cafe"),
    ]
    vacancies = [_vacancy("cafe_dn_008"), _vacancy("cafe_dn_009")]
    discovery = FakeDiscoveryService(tmp_path, candidates)
    detail = FakeDetailService()

    summary, summary_path = _runner(
        tmp_path,
        discovery,
        detail,
        vacancies,
    ).run(vacancies, [], run_id="replacement-run-1")

    assert summary_path.is_file()
    assert summary.unique_search_count == 1
    assert len(discovery.calls) == 1
    assert summary.proposed_count == 2
    assert [
        item.proposal.result_position  # type: ignore[union-attr]
        for item in summary.results
    ] == [1, 2]
    assert {
        item.proposal.proposed_place_id  # type: ignore[union-attr]
        for item in summary.results
    } == {"cafe_dn_100", "cafe_dn_101"}

    discovery_vacancy_id = discovery.calls[0].vacancy_id
    assert all(
        item.discovery_vacancy_id == discovery_vacancy_id
        for item in summary.results
    )
    assert all(
        item.proposal is not None
        and item.proposal.discovery_vacancy_id == discovery_vacancy_id
        and item.proposal.target_vacancy.vacancy_id == item.vacancy.vacancy_id
        and item.proposal.target_vacancy.status is VacancyStatus.VACANT
        and item.proposal.target_vacancy.replacement_place_id is None
        for item in summary.results
    )

    # Candidate position 1 is rejected before a duplicate second detail call.
    assert len(detail.calls) == 2
    later = max(summary.results, key=lambda item: item.proposal.result_position)  # type: ignore[union-attr]
    assert later.failures[0].status is ReplacementCandidateFailureStatus.REJECT
    assert later.failures[0].detail_id is None
    assert any(
        allocated_id in detail.calls[-1][2]
        for allocated_id in {"cafe_dn_100", "cafe_dn_101"}
    )


def test_quarantined_vacancy_proposes_fresh_monotonic_place_id(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy("cafe_dn_017")
    discovery = FakeDiscoveryService(
        tmp_path,
        [_staged(1, "0x314219:0x17", "IKIGAI garden cafe")],
    )
    runner = CanonicalReplacementProposalBatchRunner(
        discovery,
        FakeDetailService(),  # type: ignore[arg-type]
        MonotonicPlaceIdAllocator(
            existing_ids=["cafe_dn_016"],
            quarantined_ids=["cafe_dn_017"],
        ),
        CanonicalReplacementProposalBatchWriter(tmp_path / "summaries"),
        result_limit=10,
        max_detail_candidates=5,
        clock=lambda: NOW,
    )

    summary, _ = runner.run(
        [vacancy],
        [],
        run_id="quarantined-vacancy-run",
    )

    assert summary.proposed_count == 1
    assert summary.failed_count == 0
    proposal = summary.results[0].proposal
    assert proposal is not None
    assert proposal.target_vacancy.retired_place_id == "cafe_dn_017"
    assert proposal.proposed_place_id == "cafe_dn_018"


def test_cross_type_pool_allocates_actual_type_and_preserves_vacancy_link(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy(
        "night_qn_001",
        entity_type=EntityType.NIGHTLIFE,
        city_id="city_quy_nhon",
    )
    discovery = FakeDiscoveryService(
        tmp_path,
        [_staged(1, "0xcafe:0x101", "New Quy Nhon Cafe")],
    )
    detail = FakeDetailService()
    runner = CanonicalReplacementProposalBatchRunner(
        discovery,
        detail,  # type: ignore[arg-type]
        MonotonicPlaceIdAllocator(
            existing_ids=["cafe_qn_099", "night_qn_100"],
            retired_ids=[vacancy.retired_place_id],
        ),
        CanonicalReplacementProposalBatchWriter(tmp_path / "summaries"),
        result_limit=10,
        max_detail_candidates=5,
        candidate_entity_type=EntityType.CAFE,
        clock=lambda: NOW,
    )

    summary, _ = runner.run([vacancy], [], run_id="cross-type-batch-run")

    assert summary.candidate_entity_type is EntityType.CAFE
    assert summary.search_plan == {
        "city_quy_nhon/nightlife->cafe": ["<adapter-default>"]
    }
    proposal = summary.results[0].proposal
    assert proposal is not None
    assert proposal.target_vacancy.retired_place_id == "night_qn_001"
    assert proposal.candidate.entity_type is EntityType.CAFE
    assert proposal.proposed_place_id == "cafe_qn_100"


def test_detail_bound_retains_review_and_error_without_reaching_later_pass(
    tmp_path: Path,
) -> None:
    candidates = [
        _staged(3, "0x3:0x33", "Would Pass"),
        _staged(1, "0x1:0x11", "Needs Review"),
        _staged(2, "0x2:0x22", "Broken Detail"),
    ]
    vacancy = _vacancy("cafe_dn_008")
    discovery = FakeDiscoveryService(tmp_path, candidates)
    behavior = {
        candidates[1].candidate.candidate_key: "review",
        candidates[2].candidate.candidate_key: "error",
    }
    detail = FakeDetailService(behavior)

    summary, _ = _runner(
        tmp_path,
        discovery,
        detail,
        [vacancy],
        max_detail_candidates=2,
    ).run([vacancy], [], run_id="bounded-run")

    result = summary.results[0]
    assert result.status is ReplacementVacancyStatus.UNRESOLVED
    assert result.inspected_count == 2
    assert result.detail_request_count == 2
    assert [item.status for item in result.failures] == [
        ReplacementCandidateFailureStatus.REVIEW,
        ReplacementCandidateFailureStatus.ERROR,
    ]
    assert all(call[1] != candidates[0].candidate.candidate_key for call in detail.calls)


def test_multi_query_detail_bound_can_cover_five_ten_result_pools(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy("cafe_dn_008")
    terms = tuple(f"query-{index}" for index in range(1, 6))
    candidates_by_term = {
        term: [
            _staged(
                position,
                f"0x{query_index:x}:0x{position:x}",
                f"Cafe {query_index}-{position}",
            )
            for position in range(1, 11)
        ]
        for query_index, term in enumerate(terms, start=1)
    }

    class MultiQueryDiscovery(FakeDiscoveryService):
        def __init__(self) -> None:
            super().__init__(tmp_path, [])
            self.search_calls: list[str] = []

        def run(
            self,
            vacancy: EntityCityVacancy,
            *,
            run_id: str,
            result_limit: int,
            search_term: str | None = None,
        ) -> GoogleMapsDiscoveryRun:
            assert search_term is not None
            self.calls.append(vacancy)
            self.search_calls.append(search_term)
            return _discovery_run(
                vacancy,
                candidates_by_term[search_term][:result_limit],
                run_id=run_id,
                root=tmp_path,
            )

    discovery = MultiQueryDiscovery()
    last_candidate = candidates_by_term[terms[-1]][-1]
    review_behavior = {
        candidate.candidate.candidate_key: "review"
        for term in terms
        for candidate in candidates_by_term[term]
        if candidate.candidate.candidate_key
        != last_candidate.candidate.candidate_key
    }
    detail = FakeDetailService(review_behavior)
    runner = CanonicalReplacementProposalBatchRunner(
        discovery,
        detail,  # type: ignore[arg-type]
        MonotonicPlaceIdAllocator(
            existing_ids=["cafe_dn_099"],
            retired_ids=[vacancy.retired_place_id],
        ),
        CanonicalReplacementProposalBatchWriter(tmp_path / "summaries"),
        result_limit=10,
        max_detail_candidates=50,
        search_terms={(vacancy.city_id, vacancy.entity_type): terms},
        clock=lambda: NOW,
    )

    summary, _ = runner.run([vacancy], [], run_id="five-query-run")

    assert summary.unique_search_count == 5
    assert summary.search_plan == {"city_da_nang/cafe": list(terms)}
    assert discovery.search_calls == list(terms)
    assert len(detail.calls) == 50
    assert summary.proposed_count == 1
    assert summary.results[0].inspected_count == 50
    assert summary.results[0].detail_request_count == 50
    assert summary.results[0].proposal is not None
    assert (
        summary.results[0].proposal.candidate.candidate_key
        == last_candidate.candidate.candidate_key
    )


def test_multi_query_detail_bound_counts_normalized_unique_terms(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy("cafe_dn_008")
    discovery = FakeDiscoveryService(tmp_path, [])
    detail = FakeDetailService()
    common = {
        "discovery_service": discovery,
        "detail_service": detail,
        "allocator": MonotonicPlaceIdAllocator(
            existing_ids=[],
            retired_ids=[vacancy.retired_place_id],
        ),
        "summary_writer": CanonicalReplacementProposalBatchWriter(
            tmp_path / "summaries"
        ),
        "result_limit": 10,
        "search_terms": {
            (vacancy.city_id, vacancy.entity_type): (
                " first ",
                "first",
                "",
                "second",
            )
        },
    }

    runner = CanonicalReplacementProposalBatchRunner(
        **common,  # type: ignore[arg-type]
        max_detail_candidates=20,
    )

    assert runner.search_terms == {
        (vacancy.city_id, vacancy.entity_type): ("first", "second")
    }
    with pytest.raises(
        ValueError,
        match="maximum search-term count",
    ):
        CanonicalReplacementProposalBatchRunner(
            **common,  # type: ignore[arg-type]
            max_detail_candidates=21,
        )


def test_discovery_error_is_isolated_and_cached_per_slot(tmp_path: Path) -> None:
    cafes = [_vacancy("cafe_dn_008"), _vacancy("cafe_dn_009")]
    hotel = _vacancy(
        "hotel_dn_001",
        entity_type=EntityType.HOTEL,
    )
    filled = _vacancy("cafe_dn_010", status=VacancyStatus.FILLED)
    vacancies = [*cafes, hotel, filled]
    discovery = FakeDiscoveryService(
        tmp_path,
        [_staged(1, "0x1:0x11", "New Place")],
        fail_types={EntityType.CAFE},
    )
    detail = FakeDetailService()

    summary, _ = _runner(
        tmp_path,
        discovery,
        detail,
        vacancies,
    ).run(vacancies, [], run_id="isolated-run")

    assert summary.input_vacancy_count == 4
    assert summary.vacant_count == 3
    assert summary.skipped_filled_count == 1
    assert summary.unique_search_count == 2
    assert len(discovery.calls) == 2
    assert summary.failed_count == 2
    assert summary.proposed_count == 1
    assert [
        item.error_stage
        for item in summary.results
        if item.status is ReplacementVacancyStatus.FAILED
    ] == ["discovery", "discovery"]


def test_multiline_discovery_error_is_normalized_before_batch_hash(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy("cafe_dn_008")

    class MultilineFailureDiscovery(FakeDiscoveryService):
        def run(
            self,
            vacancy: EntityCityVacancy,
            *,
            run_id: str,
            result_limit: int,
        ) -> GoogleMapsDiscoveryRun:
            self.calls.append(vacancy)
            raise RuntimeError("page timeout\n\n")

    discovery = MultilineFailureDiscovery(tmp_path, [])
    detail = FakeDetailService()

    summary, path = _runner(
        tmp_path,
        discovery,
        detail,
        [vacancy],
    ).run([vacancy], [], run_id="multiline-error-run")

    assert path.is_file()
    assert summary.search_failures == [
        "city_da_nang/cafe | <adapter-default> | RuntimeError: page timeout"
    ]
    assert summary.failed_count == 1


def test_max_vacancies_bounds_only_open_work_and_skips_filled_slots(
    tmp_path: Path,
) -> None:
    vacant = _vacancy("cafe_dn_008")
    filled = [
        _vacancy(f"cafe_dn_{index:03d}", status=VacancyStatus.FILLED)
        for index in range(9, 13)
    ]
    vacancies = [vacant, *filled]
    discovery = FakeDiscoveryService(
        tmp_path,
        [_staged(1, "0x1:0x11", "New Cafe")],
    )
    detail = FakeDetailService()

    summary, _ = _runner(
        tmp_path,
        discovery,
        detail,
        vacancies,
        max_vacancies=1,
    ).run(vacancies, [], run_id="one-open-many-filled")

    assert summary.input_vacancy_count == 5
    assert summary.vacant_count == 1
    assert summary.skipped_filled_count == 4
    assert summary.proposed_count == 1
    assert len(discovery.calls) == 1


def test_summary_is_immutable_and_configuration_is_hard_bounded(
    tmp_path: Path,
) -> None:
    vacancy = _vacancy("cafe_dn_008")
    discovery = FakeDiscoveryService(
        tmp_path,
        [_staged(1, "0x1:0x11", "New Cafe")],
    )
    detail = FakeDetailService()
    runner = _runner(tmp_path, discovery, detail, [vacancy])
    summary, _ = runner.run([vacancy], [], run_id="immutable-run")

    with pytest.raises(ReplacementProposalAlreadyExistsError, match="already exists"):
        CanonicalReplacementProposalBatchWriter(tmp_path / "summaries").write(summary)

    with pytest.raises(ValueError, match="max_detail_candidates"):
        CanonicalReplacementProposalBatchRunner(
            discovery,
            detail,  # type: ignore[arg-type]
            MonotonicPlaceIdAllocator(
                existing_ids=[],
                retired_ids=[vacancy.retired_place_id],
            ),
            CanonicalReplacementProposalBatchWriter(tmp_path / "other"),
            result_limit=2,
            max_detail_candidates=3,
        )
    with pytest.raises(ValueError, match="exceeds max_vacancies"):
        CanonicalReplacementProposalBatchRunner(
            discovery,
            detail,  # type: ignore[arg-type]
            MonotonicPlaceIdAllocator(
                existing_ids=[],
                retired_ids=[vacancy.retired_place_id],
            ),
            CanonicalReplacementProposalBatchWriter(tmp_path / "other"),
            max_vacancies=1,
        ).run(
            [vacancy, _vacancy("cafe_dn_009")],
            [],
            run_id="too-many",
        )
