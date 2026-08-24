from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nextrip_graphrag.__main__ import build_parser
from nextrip_graphrag.versions.v8.observation_publisher import (
    V8ObservationInputError,
    V8ObservationKind,
    V8ObservationPublishError,
    V8ObservationPublishStatus,
    V8ObservationPublisher,
    build_v8_observation_plan_for_dataset,
    ensure_v8_observation_schema,
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
    ) -> None:
        self.release_matches = release_matches
        self.missing = missing or set()
        self.content_mismatches = content_mismatches or set()
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
    assert availability.referenced_observation_ids == [price.graph_id]
    assert {item.family for item in first.input_artifacts} == {
        "hotel_price",
        "hotel_availability",
        "current_menu",
        "current_menu_approval",
    }
    assert not any("traffic" in item.relative_path for item in first.input_artifacts)


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

    manifest = V8ObservationPublisher(store, clock=lambda: NOW).publish(plan)

    assert manifest.status is V8ObservationPublishStatus.PUBLISHED
    assert manifest.processed_counts == plan.counts
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


def _dataset(root: Path) -> CanonicalActiveDataset:
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
        verification_status=VerificationStatus.AUTO_VERIFIED,
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
