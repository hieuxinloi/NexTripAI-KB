from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nextrip_pipeline import cli as cli_module
from nextrip_pipeline.crawl import compute_content_hash
from nextrip_pipeline.crawl.trivago_registry import (
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
    TrivagoRegistryStatus,
)
from nextrip_pipeline.jobs.trivago_mcp_batch import TrivagoPriceBatchContext
from nextrip_pipeline.jobs.trivago_stay_availability import (
    TrivagoStayAvailabilityResult,
    TrivagoStayStopReason,
    TrivagoStayWindowAttempt,
)
from nextrip_pipeline.jobs.trivago_stay_batch import (
    TrivagoStayBatchItem,
    TrivagoStayBatchItemStatus,
    TrivagoStayBatchSummary,
)
from nextrip_pipeline.quality import (
    TrivagoCandidateEvidence,
    TrivagoDiscoveryResolution,
    TrivagoDiscoveryStatus,
    compute_trivago_resolution_evidence_hash,
)
from nextrip_pipeline.review.trivago_mapping import (
    TrivagoReviewBatchBuilder,
    TrivagoReviewBatchError,
    TrivagoReviewBatchWriter,
    TrivagoReviewRecommendation,
)
from nextrip_pipeline.schemas import (
    EntityType,
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    Occupancy,
    RecordSubjectType,
    SourceRecord,
)


NOW = datetime(2026, 8, 22, 13, tzinfo=timezone.utc)
CHECK_IN = date(2026, 8, 23)
CHECK_OUT = date(2026, 8, 24)


def _entry(
    entity_id: str,
    name: str,
    *,
    status: TrivagoRegistryStatus = TrivagoRegistryStatus.UNRESOLVED,
    external_id: str | None = None,
    external_url: str | None = None,
) -> TrivagoHotelRegistryEntry:
    return TrivagoHotelRegistryEntry(
        entity_id=entity_id,
        master_name=name,
        city="Da Nang",
        address="1 Test Street, Da Nang",
        latitude=16.05,
        longitude=108.21,
        search_query=f"{name}, Da Nang, Vietnam",
        status=status,
        external_id=external_id,
        external_url=external_url,
        confidence=1.0 if status is TrivagoRegistryStatus.CONFIRMED else None,
        matched_at=NOW if status is TrivagoRegistryStatus.CONFIRMED else None,
        verified_at=NOW if status is TrivagoRegistryStatus.CONFIRMED else None,
    )


def _candidate(
    external_id: str,
    name: str,
    *,
    property_id: int,
    score: float = 0.97,
    distance_m: float | None = 20.0,
) -> TrivagoCandidateEvidence:
    return TrivagoCandidateEvidence(
        external_id=external_id,
        name=name,
        location_text="Da Nang, Vietnam",
        external_url=(
            f"https://www.trivago.vn/vi/lm/{external_id}"
            f"?currencyCode=VND&search=100-{property_id};dr-20260823-20260824"
        ),
        latitude=16.05,
        longitude=108.21,
        distance_from_master_m=distance_m,
        name_score=score,
        city_evidence="match",
    )


def _resolution(
    entity_id: str,
    source_record_id: str,
    candidates: list[TrivagoCandidateEvidence],
    *,
    selected_external_id: str | None,
) -> TrivagoDiscoveryResolution:
    selected = next(
        (
            candidate
            for candidate in candidates
            if candidate.external_id == selected_external_id
        ),
        None,
    )
    resolution = TrivagoDiscoveryResolution(
        entity_id=entity_id,
        source_record_id=source_record_id,
        status=TrivagoDiscoveryStatus.REVIEW,
        resolved_at=NOW,
        selected_external_id=selected.external_id if selected else None,
        selected_external_url=selected.external_url if selected else None,
        selected_name=selected.name if selected else None,
        confidence=selected.name_score if selected else 0.8,
        reason_codes=["candidate_requires_review"],
        returned_candidate_count=len(candidates),
        candidates=candidates,
        evidence_hash="0" * 64,
    )
    return resolution.model_copy(
        update={
            "evidence_hash": compute_trivago_resolution_evidence_hash(resolution)
        }
    )


def _write_json(path: Path, model: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _write_identity_evidence(
    root: Path,
    *,
    batch_run_id: str,
    entity_id: str,
    identity_index: int,
    candidates: list[TrivagoCandidateEvidence],
    selected_external_id: str | None,
) -> tuple[Path, Path]:
    child_run_id = (
        f"{batch_run_id}-hotel-{entity_id}-offset-0-identity-{identity_index}"
    )
    source_record_id = f"source-{entity_id}-{identity_index}"
    payload = {
        "request": {
            "tool": "trivago-accommodation-search",
            "search_strategy": "name" if identity_index == 0 else "radius",
            "arguments": {
                "query": f"{entity_id} search {identity_index}",
                "arrival": CHECK_IN.isoformat(),
                "departure": CHECK_OUT.isoformat(),
            },
        },
        "response": {"result": {"structuredContent": {"accommodations": []}}},
    }
    record = SourceRecord(
        source_record_id=source_record_id,
        run_id=child_run_id,
        source_id="trivago-mcp",
        entity_type=EntityType.HOTEL,
        subject_type=RecordSubjectType.HOTEL_PRICE,
        subject_id=entity_id,
        crawled_at=NOW,
        raw_payload=payload,
        content_hash=compute_content_hash(payload),
        parser_version="test",
    )
    raw_path = _write_json(
        root
        / "data"
        / "raw"
        / "entity=hotel_price"
        / "source=trivago-mcp"
        / "date=2026-08-22"
        / f"run={child_run_id}"
        / f"record={source_record_id}.json",
        record,
    )
    resolution_path = _write_json(
        root
        / "data"
        / "quality"
        / "trivago_mapping"
        / f"run={child_run_id}"
        / f"hotel={entity_id}.json",
        _resolution(
            entity_id,
            source_record_id,
            candidates,
            selected_external_id=selected_external_id,
        ),
    )
    return raw_path, resolution_path


def _write_batch_inputs(
    root: Path,
    hotel_evidence: dict[
        str,
        list[tuple[list[TrivagoCandidateEvidence], str | None]],
    ],
    *,
    extra_registry_entries: list[TrivagoHotelRegistryEntry] | None = None,
) -> tuple[Path, Path, dict[str, list[tuple[Path, Path]]]]:
    batch_run_id = "review-source-batch"
    review_entries = [
        _entry(entity_id, f"Master {entity_id}")
        for entity_id in sorted(hotel_evidence)
    ]
    registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="hotel_final.json",
        entries=[*review_entries, *(extra_registry_entries or [])],
    )
    registry_path = _write_json(
        root / "config" / "generated" / "trivago-hotel-registry.json",
        registry,
    )

    evidence_paths: dict[str, list[tuple[Path, Path]]] = {}
    items: list[TrivagoStayBatchItem] = []
    for entity_id, identity_evidence in sorted(hotel_evidence.items()):
        pairs = [
            _write_identity_evidence(
                root,
                batch_run_id=batch_run_id,
                entity_id=entity_id,
                identity_index=index,
                candidates=candidates,
                selected_external_id=selected_external_id,
            )
            for index, (candidates, selected_external_id) in enumerate(
                identity_evidence
            )
        ]
        evidence_paths[entity_id] = pairs
        attempt_run_id = f"{batch_run_id}-hotel-{entity_id}-offset-0"
        availability = HotelAvailabilityObservation(
            observation_id=f"availability-{entity_id}",
            run_id=attempt_run_id,
            hotel_id=entity_id,
            source_record_id=f"source-{entity_id}-0",
            source_id="trivago-mcp",
            requested_check_in=CHECK_IN,
            fallback_offset_days=0,
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            nights=1,
            occupancy=Occupancy(),
            status=HotelAvailabilityStatus.UNKNOWN,
            reason=HotelAvailabilityReason.MAPPING_UNRESOLVED,
            raw_status_text="hotel identity was not confirmed",
            observed_at=NOW,
        )
        attempt = TrivagoStayWindowAttempt(
            attempt_run_id=attempt_run_id,
            fallback_offset_days=0,
            availability=availability,
            resolution_status=TrivagoDiscoveryStatus.REVIEW,
            artifact_paths=[
                _relative(path, root)
                for pair in pairs
                for path in pair
            ],
        )
        result_run_id = f"{batch_run_id}-hotel-{entity_id}"
        result = TrivagoStayAvailabilityResult(
            run_id=result_run_id,
            hotel_id=entity_id,
            requested_context=TrivagoPriceBatchContext(
                check_in=CHECK_IN,
                check_out=CHECK_OUT,
            ),
            lookahead_days=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
            stop_reason=TrivagoStayStopReason.UNKNOWN_RESULT,
            attempts=[attempt],
        )
        result_path = _write_json(
            root
            / "data"
            / "runs"
            / "trivago_stay"
            / f"run={result_run_id}.json",
            result,
        )
        entry = next(item for item in review_entries if item.entity_id == entity_id)
        items.append(
            TrivagoStayBatchItem(
                hotel_id=entity_id,
                search_query=entry.search_query,
                status=TrivagoStayBatchItemStatus.COMPLETED,
                result_run_id=result_run_id,
                stop_reason=TrivagoStayStopReason.UNKNOWN_RESULT,
                attempt_count=1,
                availability_counts={"unknown": 1},
                result_path=_relative(result_path, root),
            )
        )

    context = TrivagoPriceBatchContext(check_in=CHECK_IN, check_out=CHECK_OUT)
    summary = TrivagoStayBatchSummary(
        run_id=batch_run_id,
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=1),
        request_context=context,
        lookahead_days=0,
        registry_count=len(registry.entries),
        eligible_count=len(items),
        selected_count=len(items),
        completed_count=len(items),
        failed_count=0,
        stop_reason_counts={"unknown_result": len(items)},
        availability_counts={"unknown": len(items)},
        items=items,
    )
    summary_path = _write_json(
        root
        / "data"
        / "runs"
        / "trivago_availability_batch"
        / f"run={batch_run_id}.json",
        summary,
    )
    return summary_path, registry_path, evidence_paths


def _build(root: Path, summary_path: Path, registry_path: Path):
    return TrivagoReviewBatchBuilder().build(
        summary_path,
        registry_path,
        current_mapping_directory=root / "data" / "current" / "trivago_mappings",
        workspace_root=root,
    )


def test_review_batch_aggregates_attempts_and_cli_writes_idempotently(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    primary = _candidate("hotel-a-id", "Master hotel-a", property_id=101)
    alternative = _candidate(
        "hotel-a-other",
        "Different Nearby Hotel",
        property_id=102,
        score=0.35,
        distance_m=800,
    )
    summary_path, registry_path, _ = _write_batch_inputs(
        tmp_path,
        {
            "hotel-a": [
                ([primary, alternative], "hotel-a-id"),
                ([primary], "hotel-a-id"),
            ]
        },
    )

    first = _build(tmp_path, summary_path, registry_path)
    rebuilt = _build(tmp_path, summary_path, registry_path)

    assert rebuilt == first
    assert first.task_count == 1
    assert first.candidate_count == 2
    task = first.tasks[0]
    assert task.selected_external_ids == ["hotel-a-id"]
    assert task.recommendation is TrivagoReviewRecommendation.APPROVAL_CANDIDATE
    assert task.recommended_external_id == "hotel-a-id"
    primary_review = next(
        candidate
        for candidate in task.candidates
        if candidate.external_id == "hotel-a-id"
    )
    assert len(primary_review.appearances) == 2
    assert len(primary_review.selected_review_resolution_paths) == 2
    assert primary_review.approval_ready is True

    writer = TrivagoReviewBatchWriter(tmp_path / "review")
    first_path = writer.write(first)
    original_bytes = first_path.read_bytes()
    assert writer.write(rebuilt) == first_path
    assert first_path.read_bytes() == original_bytes

    exit_code = cli_module.main(
        [
            "build-trivago-review-batch",
            "--batch-summary",
            str(summary_path),
            "--registry",
            str(registry_path),
            "--current-mapping-dir",
            str(tmp_path / "data" / "current" / "trivago_mappings"),
            "--output-dir",
            str(tmp_path / "review"),
            "--workspace-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"queue_id={first.queue_id}" in output
    assert "tasks=1 candidates=2" in output


def test_review_batch_detects_confirmed_and_cross_review_collisions(
    tmp_path: Path,
) -> None:
    shared_a = _candidate("shared-id", "Shared Hotel", property_id=999)
    shared_b = _candidate("shared-id", "Shared Hotel", property_id=999)
    confirmed = _entry(
        "hotel-confirmed",
        "Confirmed Hotel",
        status=TrivagoRegistryStatus.CONFIRMED,
        external_id="different-external-hash",
        external_url=(
            "https://www.trivago.vn/vi/lm/confirmed"
            "?currencyCode=VND&search=100-999;dr-20260823-20260824"
        ),
    )
    summary_path, registry_path, _ = _write_batch_inputs(
        tmp_path,
        {
            "hotel-a": [([shared_a], "shared-id")],
            "hotel-b": [([shared_b], "shared-id")],
        },
        extra_registry_entries=[confirmed],
    )

    batch = _build(tmp_path, summary_path, registry_path)

    assert batch.task_count == 2
    for task, other_id in zip(batch.tasks, ["hotel-b", "hotel-a"]):
        candidate = task.candidates[0]
        assert candidate.property_ids == ["999"]
        assert candidate.confirmed_collision_entity_ids == ["hotel-confirmed"]
        assert candidate.shared_review_entity_ids == [other_id]
        assert candidate.approval_ready is False
        assert task.recommended_external_id is None
    assert batch.recommendation_counts == {"reject_selected_candidate": 2}
    assert any("multiple review hotels" in warning for warning in batch.warnings)
    assert any("confirmed owner" in warning for warning in batch.warnings)


@pytest.mark.parametrize(
    ("artifact_kind", "expected_error"),
    [
        ("raw", "raw content_hash mismatch"),
        ("resolution", "resolution evidence_hash mismatch"),
    ],
)
def test_review_batch_rejects_tampered_evidence(
    tmp_path: Path,
    artifact_kind: str,
    expected_error: str,
) -> None:
    candidate = _candidate("hotel-a-id", "Master hotel-a", property_id=101)
    summary_path, registry_path, evidence_paths = _write_batch_inputs(
        tmp_path,
        {"hotel-a": [([candidate], "hotel-a-id")]},
    )
    raw_path, resolution_path = evidence_paths["hotel-a"][0]
    target = raw_path if artifact_kind == "raw" else resolution_path
    document = json.loads(target.read_text(encoding="utf-8"))
    if artifact_kind == "raw":
        document["raw_payload"]["request"]["arguments"]["query"] = "tampered"
    else:
        document["candidates"][0]["name"] = "Tampered Hotel"
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(TrivagoReviewBatchError, match=expected_error):
        _build(tmp_path, summary_path, registry_path)
