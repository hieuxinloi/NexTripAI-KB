from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nextrip_graphrag.__main__ import build_parser
from nextrip_graphrag.versions.v8.observation_publisher import (
    V8HotelPriceCleanupGate,
    V8ObservationInputError,
    V8ObservationKind,
    V8ObservationPublishError,
    V8ObservationPublishStatus,
    V8ObservationPublisher,
    build_v8_observation_plan_for_dataset,
    ensure_v8_observation_schema,
    load_latest_hotel_price_cleanup_gate,
    read_v8_observation_plan,
    write_v8_observation_plan,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.publishing.current_availability import (
    CurrentHotelAvailabilitySnapshot,
)
from nextrip_pipeline.publishing.current_menu import CurrentMenuMetadata
from nextrip_pipeline.publishing.current_price import CurrentHotelPriceSnapshot
from nextrip_pipeline.quality.opening_status_approval import (
    OpeningStatusReviewApprovalWriter,
    build_opening_status_review_approvals,
)
from nextrip_pipeline.schemas import (
    BusinessStatus,
    DailyOpeningStatus,
    EntityType,
    HotelAvailabilityObservation,
    HotelAvailabilityReason,
    HotelAvailabilityStatus,
    HotelPriceObservation,
    NormalizedMenu,
    NormalizedMenuItem,
    Occupancy,
    OfferAvailability,
    OpeningInterval,
    OpeningStatusObservation,
    VerificationStatus,
)
from tests.canonical_dataset_support import (
    CanonicalTestPlace,
    write_canonical_dataset,
)


NOW = datetime(2026, 8, 23, 4, tzinfo=timezone.utc)


def _complete_cleanup_gate() -> V8HotelPriceCleanupGate:
    return V8HotelPriceCleanupGate(
        passed=True,
        reason="batch_complete",
        summary_path="run=trivago-availability-test.json",
        summary_sha256="a" * 64,
        run_id="trivago-availability-test",
        finished_at=NOW,
        eligible_count=1,
        selected_count=1,
        completed_count=1,
        failed_count=0,
    )


def _write_hotel_batch_summary(
    root: Path,
    *,
    run_id: str,
    finished_at: datetime,
    selected: int,
    completed: int,
    failed: int,
    eligible: int | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    statuses = ["completed"] * completed + ["failed"] * failed
    payload = {
        "run_id": run_id,
        "started_at": (finished_at - timedelta(hours=1)).isoformat(),
        "finished_at": finished_at.isoformat(),
        "request_context": {
            "check_in": "2026-08-24",
            "check_out": "2026-08-25",
            "occupancy": {"adults": 2, "children": 0, "rooms": 1},
            "children_ages": [],
            "currency": "VND",
        },
        "lookahead_days": 1,
        "registry_count": selected,
        "eligible_count": selected if eligible is None else eligible,
        "selected_count": selected,
        "completed_count": completed,
        "failed_count": failed,
        "stop_reason_counts": {},
        "availability_counts": {},
        "items": [
            {
                "hotel_id": f"hotel_{index:03d}",
                "search_query": f"Hotel {index}",
                "status": status,
                "result_run_id": f"{run_id}-hotel-{index:03d}",
            }
            for index, status in enumerate(statuses, start=1)
        ],
    }
    path = root / f"run={run_id}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def data(self) -> list[dict[str, Any]]:
        return self.rows

    def consume(self) -> None:
        return None


class FakeTransaction:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any) -> FakeResult:
        self.calls.append((query, params))
        if "latest-hotel-price-observed-at" in query:
            if self.store.retention_failure == "latest":
                raise RuntimeError("retention anchor failed")
            return FakeResult(
                [
                    {
                        "latest_observed_at": (
                            self.store.latest_price_observed_at
                        )
                    }
                ]
            )
        if "prune-hotel-availability" in query:
            if self.store.retention_failure == "availability":
                raise RuntimeError("availability retention failed")
            return FakeResult(
                [{"deleted": self.store.deleted_availability_observations}]
            )
        if "prune-hotel-price" in query:
            if self.store.retention_failure == "price":
                raise RuntimeError("price retention failed")
            return FakeResult(
                [{"deleted": self.store.deleted_price_observations}]
            )
        if "MATCH (release:DatasetRelease" in query:
            return FakeResult([{"matches": self.store.release_matches}])
        if "OPTIONAL MATCH (place:Place" in query:
            return FakeResult(
                [
                    {
                        "place_id": place_id,
                        "matches": 0 if place_id in self.store.missing else 1,
                    }
                    for place_id in params["place_ids"]
                ]
            )
        if "availability_graph_id" in query:
            return FakeResult(
                [
                    {
                        "processed": sum(
                            len(row["price_graph_ids"]) for row in params["rows"]
                        )
                    }
                ]
            )
        if "RETURN count(" in query and " AS processed" in query:
            mismatched = sorted(
                {
                    row["graph_id"]
                    for row in params["rows"]
                    if row["graph_id"] in self.store.content_mismatches
                }
            )
            return FakeResult(
                [
                    {
                        "processed": len(params["rows"]) - len(mismatched),
                        "mismatched_ids": mismatched,
                    }
                ]
            )
        return FakeResult()


class FakeSession:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute_write(self, callback):
        self.store.execute_write_calls += 1
        try:
            result = callback(self.store.transaction)
        except Exception:
            self.store.rolled_back = True
            raise
        self.store.committed = True
        return result


class FakeDriver:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def session(self, **options: Any) -> FakeSession:
        self.store.session_options = options
        return FakeSession(self.store)


class FakeStore:
    def __init__(
        self,
        *,
        release_matches: int = 1,
        missing: set[str] | None = None,
        content_mismatches: set[str] | None = None,
        latest_price_observed_at: str | None = "2026-08-23T04:00:00Z",
        deleted_price_observations: int = 0,
        deleted_availability_observations: int = 0,
        retention_failure: str | None = None,
    ) -> None:
        self.release_matches = release_matches
        self.missing = missing or set()
        self.content_mismatches = content_mismatches or set()
        self.latest_price_observed_at = latest_price_observed_at
        self.deleted_price_observations = deleted_price_observations
        self.deleted_availability_observations = (
            deleted_availability_observations
        )
        self.retention_failure = retention_failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_write_calls = 0
        self.committed = False
        self.rolled_back = False
        self.session_options: dict[str, Any] | None = None
        self.settings = SimpleNamespace(neo4j_database="neo4j")
        self.transaction = FakeTransaction(self)
        self.driver = FakeDriver(self)

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


def test_plan_reads_current_artifacts_and_builds_deterministic_ids(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    dataset = _dataset(tmp_path / "canonical")

    first = build_v8_observation_plan_for_dataset(
        dataset,
        **roots,
        built_at=NOW,
    )
    second = build_v8_observation_plan_for_dataset(
        dataset,
        **roots,
        built_at=NOW + timedelta(minutes=5),
    )

    assert first.plan_id == second.plan_id
    assert first.plan_hash == second.plan_hash
    assert all(len(row.content_hash) == 64 for row in first.observations)
    assert all(len(row.content_hash) == 64 for row in first.menu_items)
    assert first.counts == {
        "hotel_price": 1,
        "hotel_availability": 1,
        "opening_status": 1,
        "menu_snapshot": 1,
        "menu_items": 1,
        "availability_price_relationships": 1,
        "total_observations": 4,
    }
    assert all(row.graph_id.startswith("v8obs:") for row in first.observations)
    availability = next(
        row
        for row in first.observations
        if row.kind is V8ObservationKind.HOTEL_AVAILABILITY
    )
    price = next(
        row for row in first.observations if row.kind is V8ObservationKind.HOTEL_PRICE
    )
    opening = next(
        row
        for row in first.observations
        if row.kind is V8ObservationKind.OPENING_STATUS
    )
    assert availability.referenced_observation_ids == [price.graph_id]
    assert datetime.fromisoformat(
        str(opening.properties["observed_at"]).replace("Z", "+00:00")
    ) == NOW
    assert datetime.fromisoformat(
        str(opening.properties["stale_after"]).replace("Z", "+00:00")
    ) == NOW + timedelta(days=1)
    assert {item.family for item in first.input_artifacts} == {
        "hotel_price",
        "hotel_availability",
        "current_menu",
        "current_menu_approval",
    }
    assert not any("traffic" in item.relative_path for item in first.input_artifacts)


def _append_stale_hotel_inputs(roots: dict[str, Path]) -> None:
    price_path = next(Path(roots["hotel_price_root"]).glob("*.json"))
    price = CurrentHotelPriceSnapshot.model_validate_json(price_path.read_bytes())
    old_price = price.model_copy(deep=True)
    old_price.observation_id = "source-price-old"
    old_price.observation.observation_id = "source-price-old"
    old_price.observation.offer_key = "listing|agoda|old"
    old_price.observation.source_record_id = "raw-price-old"
    old_price.observation.observed_at = NOW - timedelta(days=3)
    old_price.updated_at = NOW - timedelta(days=3)
    old_price.stale_after = NOW - timedelta(days=3) + timedelta(hours=5)
    _write(
        Path(roots["hotel_price_root"]) / "old-price.json",
        old_price.model_dump_json(indent=2),
    )

    availability_path = next(
        Path(roots["hotel_availability_root"]).glob("*.json")
    )
    availability = CurrentHotelAvailabilitySnapshot.model_validate_json(
        availability_path.read_bytes()
    )
    old_availability = availability.model_copy(deep=True)
    old_availability.observation_id = "source-availability-old"
    old_availability.observation.observation_id = "source-availability-old"
    old_availability.observation.source_record_id = "raw-price-old"
    old_availability.observation.price_observation_ids = ["source-price-old"]
    old_availability.observation.observed_at = NOW - timedelta(days=3)
    old_availability.updated_at = NOW - timedelta(days=3)
    old_availability.stale_after = NOW - timedelta(days=3) + timedelta(hours=5)
    _write(
        Path(roots["hotel_availability_root"]) / "old-availability.json",
        old_availability.model_dump_json(indent=2),
    )


def test_plan_excludes_hotel_inputs_older_than_retention_window(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    _append_stale_hotel_inputs(roots)

    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        hotel_price_cleanup_gate=_complete_cleanup_gate(),
        built_at=NOW,
    )

    assert plan.counts["hotel_price"] == 1
    assert plan.counts["hotel_availability"] == 1
    assert all(
        artifact.relative_path not in {"old-price.json", "old-availability.json"}
        for artifact in plan.input_artifacts
    )


def test_failed_cleanup_gate_does_not_prefilter_stale_hotel_inputs(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    _append_stale_hotel_inputs(roots)
    failed_gate = V8HotelPriceCleanupGate(
        reason="batch_incomplete",
        summary_path="run=incomplete.json",
        summary_sha256="b" * 64,
        run_id="incomplete",
        finished_at=NOW,
        selected_count=2,
        completed_count=1,
        failed_count=0,
    )

    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        hotel_price_cleanup_gate=failed_gate,
        built_at=NOW,
    )

    assert plan.counts["hotel_price"] == 2
    assert plan.counts["hotel_availability"] == 2
    assert {artifact.relative_path for artifact in plan.input_artifacts} >= {
        "old-price.json",
        "old-availability.json",
    }


def test_publish_rejects_gate_different_from_plan_prefilter_gate(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        hotel_price_cleanup_gate=_complete_cleanup_gate(),
        built_at=NOW,
    )
    store = FakeStore()

    with pytest.raises(
        V8ObservationPublishError,
        match="prefilter gate does not match publish gate",
    ):
        V8ObservationPublisher(
            store,
            hotel_price_cleanup_gate=V8HotelPriceCleanupGate(
                reason="batch_stale"
            ),
        ).publish(plan)

    assert store.calls == []


def test_plan_skips_pending_review_opening_without_blocking_verified_inputs(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    dataset = _dataset(
        tmp_path / "canonical",
        opening_verification_status=VerificationStatus.PENDING_REVIEW,
    )

    plan = build_v8_observation_plan_for_dataset(
        dataset,
        **roots,
        built_at=NOW,
    )

    assert plan.counts["opening_status"] == 0
    assert plan.counts["hotel_price"] == 1
    assert plan.counts["hotel_availability"] == 1
    assert plan.counts["menu_snapshot"] == 1
    assert plan.counts["total_observations"] == 3
    assert all(
        row.source_observation_id != "source-opening-1"
        for row in plan.observations
    )


def test_plan_skips_legacy_unknown_availability_as_audit_only(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    availability_path = next(
        Path(roots["hotel_availability_root"]).rglob("*.json")
    )
    snapshot = CurrentHotelAvailabilitySnapshot.model_validate_json(
        availability_path.read_bytes()
    )
    snapshot.observation = snapshot.observation.model_copy(
        update={
            "status": HotelAvailabilityStatus.UNKNOWN,
            "reason": HotelAvailabilityReason.NO_PRICE,
            "offer_count": 0,
            "price_observation_ids": [],
        }
    )
    availability_path.write_text(
        snapshot.model_dump_json(indent=2),
        encoding="utf-8",
    )

    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )

    assert plan.counts["hotel_availability"] == 0
    assert all(
        row.kind is not V8ObservationKind.HOTEL_AVAILABILITY
        for row in plan.observations
    )


def test_plan_publishes_exactly_approved_pending_opening_with_provenance(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    dataset = _dataset(
        tmp_path / "canonical",
        opening_verification_status=VerificationStatus.PENDING_REVIEW,
    )
    approval_root = tmp_path / "opening-approvals"
    approvals = build_opening_status_review_approvals(
        dataset,
        reviewer="oanh",
        approved_at=NOW + timedelta(hours=1),
    )
    OpeningStatusReviewApprovalWriter(approval_root).write_many(approvals)

    plan = build_v8_observation_plan_for_dataset(
        dataset,
        **roots,
        opening_approval_root=approval_root,
        built_at=NOW + timedelta(hours=1),
    )

    assert plan.counts["opening_status"] == 1
    opening = next(
        row
        for row in plan.observations
        if row.kind is V8ObservationKind.OPENING_STATUS
    )
    assert opening.properties["verification_status"] == "human_verified"
    assert opening.properties["source_verification_status"] == "pending_review"
    assert opening.properties["human_approval_id"] == approvals[0].approval_id
    assert opening.properties["human_approval_reviewer"] == "oanh"
    assert any(
        item.family == "opening_status_approval"
        for item in plan.input_artifacts
    )


def test_later_opening_approval_has_a_distinct_immutable_graph_id(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    dataset = _dataset(
        tmp_path / "canonical",
        opening_verification_status=VerificationStatus.PENDING_REVIEW,
    )

    graph_ids = []
    for hour in (1, 2):
        approval_root = tmp_path / f"opening-approvals-{hour}"
        approvals = build_opening_status_review_approvals(
            dataset,
            reviewer="oanh",
            approved_at=NOW + timedelta(hours=hour),
        )
        OpeningStatusReviewApprovalWriter(approval_root).write_many(approvals)
        plan = build_v8_observation_plan_for_dataset(
            dataset,
            **roots,
            opening_approval_root=approval_root,
            built_at=NOW + timedelta(hours=hour),
        )
        graph_ids.append(
            next(
                row.graph_id
                for row in plan.observations
                if row.kind is V8ObservationKind.OPENING_STATUS
            )
        )

    assert graph_ids[0] != graph_ids[1]


def test_cli_rejects_removed_current_place_input() -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        ["v8-publish-observations", "--canonical-dataset", "canonical.json"]
    )

    assert not hasattr(arguments, "place_root")
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "v8-publish-observations",
                "--canonical-dataset",
                "canonical.json",
                "--place-root",
                "data/current/place",
            ]
        )


def test_plan_rejects_unknown_canonical_place_and_mismatched_stay(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    price_path = next(Path(roots["hotel_price_root"]).rglob("*.json"))
    price = CurrentHotelPriceSnapshot.model_validate_json(price_path.read_bytes())
    price.observation.hotel_id = "hotel_missing"
    price.hotel_id = "hotel_missing"
    price_path.write_text(price.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(V8ObservationInputError, match="non-canonical place"):
        build_v8_observation_plan_for_dataset(
            _dataset(tmp_path / "canonical"), **roots, built_at=NOW
        )

    roots = _write_current_artifacts(tmp_path / "context")
    availability_path = next(Path(roots["hotel_availability_root"]).rglob("*.json"))
    availability = CurrentHotelAvailabilitySnapshot.model_validate_json(
        availability_path.read_bytes()
    )
    availability.observation.currency = "USD"
    availability_path.write_text(
        availability.model_dump_json(indent=2),
        encoding="utf-8",
    )
    with pytest.raises(V8ObservationInputError, match="different stay contexts"):
        build_v8_observation_plan_for_dataset(
            _dataset(tmp_path / "canonical"), **roots, built_at=NOW
        )


def test_menu_requires_current_human_approval_metadata(tmp_path: Path) -> None:
    roots = _write_current_artifacts(tmp_path)
    audit_path = next((Path(roots["current_menu_root"]) / "_audit").glob("*.json"))
    audit_path.unlink()

    with pytest.raises(V8ObservationInputError, match="human-approval metadata"):
        build_v8_observation_plan_for_dataset(
            _dataset(tmp_path / "canonical"), **roots, built_at=NOW
        )


def test_missing_optional_current_menu_root_is_an_empty_input(tmp_path: Path) -> None:
    roots = _write_current_artifacts(tmp_path)
    roots["current_menu_root"] = tmp_path / "not-created-menu-root"

    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )

    assert plan.counts["menu_snapshot"] == 0
    assert plan.counts["menu_items"] == 0


def test_dry_run_writes_manifest_without_touching_store(tmp_path: Path) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    store = FakeStore()
    manifest_path = tmp_path / "publish" / "manifest.json"

    manifest = V8ObservationPublisher(store, clock=lambda: NOW).publish(
        plan,
        dry_run=True,
        manifest_path=manifest_path,
    )

    assert manifest.status is V8ObservationPublishStatus.DRY_RUN
    assert manifest_path.is_file()
    assert store.calls == []
    assert all(count == 0 for count in manifest.processed_counts.values())
    assert manifest.hotel_price_retention.applied is False
    assert manifest.hotel_price_retention.previous_calendar_days == 1
    assert manifest.hotel_price_retention.skip_reason == "dry_run"


def test_cleanup_gate_uses_latest_finished_batch_without_hardcoded_count(
    tmp_path: Path,
) -> None:
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id="older-73-hotels",
        finished_at=NOW - timedelta(hours=5),
        selected=73,
        completed=73,
        failed=0,
    )
    latest = _write_hotel_batch_summary(
        summary_root,
        run_id="latest-4-hotels",
        finished_at=NOW,
        selected=4,
        completed=4,
        failed=0,
    )

    gate = load_latest_hotel_price_cleanup_gate(
        summary_root,
        evaluated_at=NOW,
    )

    assert gate.passed is True
    assert gate.reason == "batch_complete"
    assert gate.run_id == "latest-4-hotels"
    assert gate.summary_path == str(latest)
    assert gate.selected_count == 4
    assert gate.completed_count == 4
    assert gate.failed_count == 0
    assert gate.summary_sha256 == hashlib.sha256(latest.read_bytes()).hexdigest()


def test_cleanup_gate_rejects_empty_successful_batch(tmp_path: Path) -> None:
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id="empty-success",
        finished_at=NOW,
        selected=0,
        completed=0,
        failed=0,
    )

    gate = load_latest_hotel_price_cleanup_gate(
        summary_root,
        evaluated_at=NOW,
    )

    assert gate.passed is False
    assert gate.reason == "batch_empty"
    assert gate.selected_count == 0


def test_cleanup_gate_rejects_successful_partial_batch(tmp_path: Path) -> None:
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id="partial-retry-4-of-73",
        finished_at=NOW,
        eligible=73,
        selected=4,
        completed=4,
        failed=0,
    )

    gate = load_latest_hotel_price_cleanup_gate(
        summary_root,
        evaluated_at=NOW,
    )

    assert gate.passed is False
    assert gate.reason == "batch_partial"
    assert gate.eligible_count == 73
    assert gate.selected_count == 4


@pytest.mark.parametrize(
    ("finished_at", "reason"),
    [
        (NOW - timedelta(hours=9), "batch_stale"),
        (NOW + timedelta(minutes=6), "batch_from_future"),
    ],
)
def test_cleanup_gate_rejects_stale_or_future_batch(
    tmp_path: Path,
    finished_at: datetime,
    reason: str,
) -> None:
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id=reason,
        finished_at=finished_at,
        selected=3,
        completed=3,
        failed=0,
    )

    gate = load_latest_hotel_price_cleanup_gate(
        summary_root,
        evaluated_at=NOW,
    )

    assert gate.passed is False
    assert gate.reason == reason


def test_cleanup_gate_rejects_duplicate_hotel_ids(tmp_path: Path) -> None:
    summary_root = tmp_path / "summaries"
    path = _write_hotel_batch_summary(
        summary_root,
        run_id="duplicate-hotels",
        finished_at=NOW,
        selected=2,
        completed=2,
        failed=0,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][1]["hotel_id"] = payload["items"][0]["hotel_id"]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    gate = load_latest_hotel_price_cleanup_gate(
        summary_root,
        evaluated_at=NOW,
    )

    assert gate.passed is False
    assert gate.reason == "batch_summary_inconsistent"


@pytest.mark.parametrize(
    ("completed", "failed", "reason"),
    [
        (2, 1, "batch_failed"),
        (2, 0, "batch_incomplete"),
    ],
)
def test_failed_or_incomplete_batch_skips_cleanup_but_publishes_observations(
    tmp_path: Path,
    completed: int,
    failed: int,
    reason: str,
) -> None:
    roots = _write_current_artifacts(tmp_path / "inputs")
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id=f"quality-{reason}",
        finished_at=NOW,
        selected=3,
        completed=completed,
        failed=failed,
    )
    store = FakeStore(
        deleted_price_observations=9,
        deleted_availability_observations=5,
    )

    manifest = V8ObservationPublisher(
        store,
        hotel_batch_summary_root=summary_root,
    ).publish(plan)

    assert manifest.status is V8ObservationPublishStatus.PUBLISHED
    assert manifest.processed_counts == plan.counts
    retention = manifest.hotel_price_retention
    assert retention.applied is False
    assert retention.skip_reason == reason
    assert retention.quality_gate.passed is False
    assert retention.quality_gate.selected_count == 3
    assert retention.quality_gate.completed_count == completed
    assert retention.quality_gate.failed_count == failed
    assert store.committed is True
    queries = "\n".join(query for query, _ in store.transaction.calls)
    assert "prune-hotel-availability" not in queries
    assert "prune-hotel-price" not in queries


def test_missing_batch_summary_skips_cleanup_with_audited_reason(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path / "inputs")
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    store = FakeStore()

    manifest = V8ObservationPublisher(
        store,
        hotel_batch_summary_root=tmp_path / "missing-summaries",
    ).publish(plan)

    retention = manifest.hotel_price_retention
    assert retention.applied is False
    assert retention.skip_reason == "batch_summary_missing"
    assert retention.quality_gate.reason == "batch_summary_missing"
    assert all(
        "prune-hotel" not in query for query, _ in store.transaction.calls
    )


def test_ensure_schema_creates_only_neutral_observation_schema() -> None:
    store = FakeStore()

    ensure_v8_observation_schema(store)

    queries = "\n".join(query for query, _ in store.calls)
    assert len(store.calls) == 4
    assert "CREATE CONSTRAINT observation_id" in queries
    assert "FOR (node:Observation)" in queries
    assert "CREATE CONSTRAINT menu_item_id" in queries
    assert "FOR (node:MenuItem)" in queries
    assert "CREATE RANGE INDEX observation_place" in queries
    assert "CREATE RANGE INDEX observation_observed_at" in queries
    assert ":V8" not in queries
    assert " v8_" not in queries


def test_plan_file_is_revalidated_and_cannot_be_replaced(tmp_path: Path) -> None:
    roots = _write_current_artifacts(tmp_path / "inputs")
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    path = tmp_path / "plans" / f"{plan.plan_id}.json"

    assert write_v8_observation_plan(path, plan) == path
    assert read_v8_observation_plan(path) == plan
    assert write_v8_observation_plan(path, plan) == path

    changed = plan.model_copy(
        update={
            "plan_hash": "c" * 64,
            "plan_id": "different-plan",
        }
    )
    with pytest.raises(FileExistsError, match="refusing to replace"):
        write_v8_observation_plan(path, changed)

    tampered = json.loads(path.read_text(encoding="utf-8"))
    price = next(
        row for row in tampered["observations"] if row["kind"] == "hotel_price"
    )
    price["properties"]["amount"] = 999999
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="content_hash"):
        read_v8_observation_plan(path)


def test_apply_matches_existing_places_and_merges_append_only(tmp_path: Path) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    store = FakeStore()

    manifest = V8ObservationPublisher(
        store,
        clock=lambda: NOW,
        hotel_price_cleanup_gate=_complete_cleanup_gate(),
    ).publish(plan)

    assert manifest.status is V8ObservationPublishStatus.PUBLISHED
    assert manifest.processed_counts == plan.counts
    assert manifest.hotel_price_retention.applied is True
    queries = "\n".join(query for query, _ in [*store.calls, *store.transaction.calls])
    assert store.execute_write_calls == 1
    assert store.committed is True
    assert store.rolled_back is False
    assert store.session_options == {"database": "neo4j"}
    assert "MATCH (release:DatasetRelease" in queries
    assert "MATCH (place:Place" in queries
    assert "MERGE (place:Place" not in queries
    assert "MERGE (observation:Observation" in queries
    assert "MERGE (item:MenuItem" in queries
    assert ":HotelPriceObservation" in queries
    assert ":HotelAvailabilityObservation" in queries
    assert ":OpeningStatusObservation" in queries
    assert ":MenuSnapshot" in queries
    assert ":V8" not in queries
    assert "CREATE CONSTRAINT observation_id" in queries
    assert "CREATE CONSTRAINT menu_item_id" in queries
    assert "CREATE RANGE INDEX observation_place" in queries
    assert "CREATE RANGE INDEX observation_observed_at" in queries
    assert "v8_observation_id" not in queries
    assert "v8_menu_item_id" not in queries
    assert "v8_observation_place" not in queries
    assert "v8_observation_observed_at" not in queries
    assert "ON MATCH SET" not in queries
    assert "V8Traffic" not in queries


def test_price_retention_keeps_current_and_previous_vietnam_days(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    # 2026-08-26T17:30Z is 2026-08-27 00:30 in Vietnam. Therefore
    # local days 27 and 26 are retained and the UTC cutoff is day 25 17:00.
    store = FakeStore(
        latest_price_observed_at="2026-08-26T17:30:00Z",
        deleted_price_observations=7,
        deleted_availability_observations=3,
    )
    summary_root = tmp_path / "summaries"
    _write_hotel_batch_summary(
        summary_root,
        run_id="retention-quality-pass",
        finished_at=NOW,
        selected=5,
        completed=5,
        failed=0,
    )

    manifest = V8ObservationPublisher(
        store,
        clock=lambda: NOW,
        hotel_batch_summary_root=summary_root,
    ).publish(plan)

    retention = manifest.hotel_price_retention
    assert retention.timezone == "Asia/Ho_Chi_Minh"
    assert retention.previous_calendar_days == 1
    assert retention.anchor_observed_at == datetime(
        2026, 8, 26, 17, 30, tzinfo=timezone.utc
    )
    assert retention.cutoff_observed_at == datetime(
        2026, 8, 25, 17, tzinfo=timezone.utc
    )
    assert retention.deleted_price_observations == 7
    assert retention.deleted_availability_observations == 3
    assert retention.quality_gate.passed is True
    assert retention.quality_gate.run_id == "retention-quality-pass"

    transaction_calls = store.transaction.calls
    prune_availability_index = next(
        index
        for index, (query, _) in enumerate(transaction_calls)
        if "prune-hotel-availability" in query
    )
    prune_price_index = next(
        index
        for index, (query, _) in enumerate(transaction_calls)
        if "prune-hotel-price" in query
    )
    final_release_index = max(
        index
        for index, (query, _) in enumerate(transaction_calls)
        if "validate-active-release" in query
    )
    assert prune_availability_index < prune_price_index < final_release_index
    for index in (prune_availability_index, prune_price_index):
        assert (
            transaction_calls[index][1]["cutoff_observed_at"]
            == "2026-08-25T17:00:00Z"
        )

    queries = "\n".join(query for query, _ in transaction_calls)
    assert "MATCH (price:HotelPriceObservation" in queries
    assert "MATCH (availability:HotelAvailabilityObservation" in queries
    assert "prune-hotel-opening" not in queries
    assert "prune-menu" not in queries


def test_price_retention_failure_rolls_back_publish_and_manifest(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    store = FakeStore(retention_failure="price")
    manifest_path = tmp_path / "must-not-exist.json"

    with pytest.raises(RuntimeError, match="price retention failed"):
        V8ObservationPublisher(
            store,
            hotel_price_cleanup_gate=_complete_cleanup_gate(),
        ).publish(
            plan,
            manifest_path=manifest_path,
        )

    assert store.execute_write_calls == 1
    assert store.rolled_back is True
    assert store.committed is False
    assert not manifest_path.exists()


def test_apply_fails_before_merge_for_wrong_release_or_missing_place(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )

    with pytest.raises(V8ObservationPublishError, match="exactly one active"):
        release_store = FakeStore(release_matches=0)
        V8ObservationPublisher(release_store).publish(plan)
    assert release_store.rolled_back is True

    store = FakeStore(missing={"hotel_dn_001"})
    with pytest.raises(V8ObservationPublishError, match="missing or ambiguous"):
        V8ObservationPublisher(store).publish(plan)
    queries = "\n".join(query for query, _ in store.transaction.calls)
    assert "MERGE (observation:Observation" not in queries
    assert store.execute_write_calls == 1
    assert store.rolled_back is True


def test_existing_observation_content_conflict_rolls_back_whole_apply(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    price = next(
        row for row in plan.observations if row.kind is V8ObservationKind.HOTEL_PRICE
    )
    store = FakeStore(content_mismatches={price.graph_id})
    manifest_path = tmp_path / "must-not-exist.json"

    with pytest.raises(V8ObservationPublishError, match="content conflict"):
        V8ObservationPublisher(store).publish(
            plan,
            manifest_path=manifest_path,
        )

    assert store.execute_write_calls == 1
    assert store.rolled_back is True
    assert store.committed is False
    assert not manifest_path.exists()


def test_late_menu_item_content_conflict_rolls_back_prior_observation_merges(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    plan = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    store = FakeStore(content_mismatches={plan.menu_items[0].graph_id})

    with pytest.raises(V8ObservationPublishError, match="menu items content conflict"):
        V8ObservationPublisher(store).publish(plan)

    queries = "\n".join(query for query, _ in store.transaction.calls)
    assert "merge-has-price" in queries
    assert "merge-menu-items" in queries
    assert store.execute_write_calls == 1
    assert store.rolled_back is True
    assert store.committed is False


def test_same_observation_identity_with_changed_price_has_new_content_hash(
    tmp_path: Path,
) -> None:
    roots = _write_current_artifacts(tmp_path)
    first = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )
    price_path = next(Path(roots["hotel_price_root"]).rglob("*.json"))
    snapshot = CurrentHotelPriceSnapshot.model_validate_json(price_path.read_bytes())
    snapshot.observation.amount = Decimal("510000")
    snapshot.observation.nightly_amount = Decimal("510000")
    snapshot.observation.total_amount = Decimal("510000")
    price_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    second = build_v8_observation_plan_for_dataset(
        _dataset(tmp_path / "canonical"),
        **roots,
        built_at=NOW,
    )

    first_price = next(
        row for row in first.observations if row.kind is V8ObservationKind.HOTEL_PRICE
    )
    second_price = next(
        row for row in second.observations if row.kind is V8ObservationKind.HOTEL_PRICE
    )
    assert first_price.graph_id == second_price.graph_id
    assert first_price.content_hash != second_price.content_hash
    assert first.plan_hash != second.plan_hash


def _dataset(
    root: Path,
    *,
    opening_verification_status: VerificationStatus = (
        VerificationStatus.AUTO_VERIFIED
    ),
) -> CanonicalActiveDataset:
    opening = OpeningStatusObservation(
        observation_id="source-opening-1",
        run_id="maps-run-1",
        place_id="cafe_dn_001",
        source_record_ids=["raw-maps-1"],
        local_date=date(2026, 8, 23),
        status=DailyOpeningStatus.OPEN_TODAY,
        opening_intervals=[
            OpeningInterval(
                opens_at="07:00:00",
                closes_at="22:00:00",
            )
        ],
        observed_at=NOW,
        verification_status=opening_verification_status,
    )
    dataset_path = write_canonical_dataset(
        root,
        [
            CanonicalTestPlace(
                place_id="cafe_dn_001",
                name="Cafe One",
                latitude=16.06,
                longitude=108.22,
                entity_type=EntityType.CAFE,
                data={
                    "business_status": BusinessStatus.ACTIVE.value,
                    "verification_status": VerificationStatus.AUTO_VERIFIED.value,
                    # A place-level verification timestamp can be older than its
                    # independently refreshed daily opening observation.
                    "last_verified": (NOW - timedelta(days=30)).isoformat(),
                    "opening_status": opening.model_dump(mode="json"),
                    "google_maps_refresh": {
                        "source_id": "google-maps-web",
                        "source_record_id": "raw-maps-1",
                        "observation_id": "source-place-1",
                        "decision_id": "maps-decision-1",
                        "run_id": "maps-run-1",
                        "source_url": "https://www.google.com/maps/place/test",
                    },
                },
            ),
            CanonicalTestPlace(
                place_id="hotel_dn_001",
                name="Hotel One",
                latitude=16.07,
                longitude=108.23,
                entity_type=EntityType.HOTEL,
            ),
        ],
        generated_at=NOW,
    )
    return read_canonical_active_dataset(dataset_path)


def _write_current_artifacts(tmp_path: Path) -> dict[str, Path]:
    price_root = tmp_path / "price"
    availability_root = tmp_path / "availability"
    menu_root = tmp_path / "menu"
    for root in (price_root, availability_root, menu_root):
        root.mkdir(parents=True, exist_ok=True)

    occupancy = Occupancy(adults=2, children=0, rooms=1)
    price_observation = HotelPriceObservation(
        observation_id="source-price-1",
        run_id="hotel-run-1",
        hotel_id="hotel_dn_001",
        offer_key="listing|agoda|2a-1r",
        source_record_id="raw-price-1",
        source_id="trivago-mcp",
        mapping_id="trivago-hotel-1",
        external_id="listing-1",
        seller="Agoda",
        room_type="double",
        check_in=date(2026, 8, 24),
        check_out=date(2026, 8, 25),
        occupancy=occupancy,
        currency="VND",
        amount=Decimal("500000"),
        nightly_amount=Decimal("500000"),
        total_amount=Decimal("500000"),
        availability=OfferAvailability.AVAILABLE,
        observed_at=NOW,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    price = CurrentHotelPriceSnapshot(
        hotel_id="hotel_dn_001",
        observation_id=price_observation.observation_id,
        decision_id="price-decision-1",
        observation=price_observation,
        updated_at=NOW,
        stale_after=NOW + timedelta(hours=5),
    )
    _write(price_root / "price.json", price.model_dump_json(indent=2))

    availability_observation = HotelAvailabilityObservation(
        observation_id="source-availability-1",
        run_id="hotel-run-1",
        hotel_id="hotel_dn_001",
        source_record_id="raw-price-1",
        source_id="trivago-mcp",
        mapping_id="trivago-hotel-1",
        external_id="listing-1",
        requested_check_in=date(2026, 8, 24),
        check_in=date(2026, 8, 24),
        check_out=date(2026, 8, 25),
        nights=1,
        occupancy=occupancy,
        currency="VND",
        status=HotelAvailabilityStatus.AVAILABLE,
        reason=HotelAvailabilityReason.OFFER_FOUND,
        offer_count=1,
        price_observation_ids=[price_observation.observation_id],
        observed_at=NOW,
        verification_status=VerificationStatus.AUTO_VERIFIED,
    )
    availability = CurrentHotelAvailabilitySnapshot(
        hotel_id="hotel_dn_001",
        observation_id=availability_observation.observation_id,
        observation=availability_observation,
        updated_at=NOW,
        stale_after=NOW + timedelta(hours=5),
    )
    _write(
        availability_root / "availability.json",
        availability.model_dump_json(indent=2),
    )

    menu = NormalizedMenu(
        place_id="cafe_dn_001",
        items=[
            NormalizedMenuItem(
                name="Cafe sua",
                section="Cafe",
                currency="VND",
                amount=22000,
            )
        ],
    )
    metadata = CurrentMenuMetadata(
        place_id="cafe_dn_001",
        review_id="menu-review-1",
        source_image_hash="b" * 64,
        reviewer="oanh",
        reviewed_at=NOW,
    )
    _write(menu_root / "cafe_dn_001.json", menu.model_dump_json(indent=2))
    _write(
        menu_root / "_audit" / "cafe_dn_001.json",
        metadata.model_dump_json(indent=2),
    )
    return {
        "hotel_price_root": price_root,
        "hotel_availability_root": availability_root,
        "current_menu_root": menu_root,
    }


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
