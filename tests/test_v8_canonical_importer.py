from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nextrip_graphrag.versions.v8.canonical_importer import (
    CanonicalV8ArtifactError,
    CanonicalV8GateError,
    CanonicalV8GraphWriteError,
    CanonicalV8ReleaseManifestWriter,
    _CLEAN_CURRENT_COMPATIBILITY,
    apply_canonical_v8_import,
    dry_run_canonical_v8_import,
    ensure_canonical_v8_schema,
    prepare_canonical_v8_import,
    _place_properties,
    _record_fact_specs,
)
from nextrip_pipeline.canonical.completeness import (
    CanonicalHotelStayContext,
    build_canonical_completeness_audit,
)
from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDataset,
    CanonicalActiveDatasetReport,
    CanonicalActivePlaceRecord,
    CanonicalCoordinates,
    CanonicalEntityCityCount,
    CanonicalMasterRecordReference,
    CanonicalRecordProvenance,
    CanonicalRecordSource,
    _dataset_payload,
    _record_payload,
    _report_payload,
)
from nextrip_pipeline.canonical.models import EntityCityQuota, stable_sha256
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessReport,
    _readiness_payload,
)
from nextrip_pipeline.schemas import EntityType


UTC = timezone.utc
AS_OF = datetime(2026, 8, 23, 5, tzinfo=UTC)
MANIFEST_ID = "canonical-manifest-v8-import-test"
MANIFEST_HASH = "1" * 64


def _record(
    place_id: str,
    *,
    name: str,
    address: str | None = "1 Bach Dang, Da Nang",
) -> CanonicalActivePlaceRecord:
    coordinates = CanonicalCoordinates(
        lat=16.0544,
        lng=108.2022,
        source="verified-master",
    )
    provenance = CanonicalRecordProvenance(
        manifest_id=MANIFEST_ID,
        manifest_hash=MANIFEST_HASH,
        identity_hash="2" * 64,
        source_kind=CanonicalRecordSource.VERIFIED_MASTER,
        canonical_place_id=place_id,
        active_legacy_place_id=place_id,
        legacy_place_ids=[place_id],
        master_records=[
            CanonicalMasterRecordReference(
                legacy_place_id=place_id,
                source_filename="cafe_final.json",
                record_index=0,
                source_record_hash="3" * 64,
            )
        ],
    )
    data = {
        "id": place_id,
        "entity_type": "cafe",
        "primary_type": "cafe",
        "place_types": ["cafe"],
        "aliases": [],
        "name": name,
        "tags": ["pet friendly"],
        "google_maps_category": "Coffee shop",
        "description": f"Verified description for {name}",
        "cover_image_url": "https://example.com/cover.jpg",
        "opening_hours": {
            "open": "08:00",
            "close": "22:00",
            "closed_days": [],
            "note": "Daily",
        },
        "price_range": "30000-60000 VND",
        "price_per_person": {
            "min": 30000,
            "max": 60000,
            "currency": "VND",
        },
        "amenities": ["wifi"],
    }
    values = {
        "place_id": place_id,
        "primary_type": EntityType.CAFE,
        "secondary_types": [],
        "place_types": [EntityType.CAFE],
        "name": name,
        "aliases": [],
        "tags": ["pet friendly"],
        "city_id": "city_da_nang",
        "city": "Da Nang",
        "address": address,
        "coordinates": coordinates,
        "phone": None,
        "website_url": None,
        "external_identities": [],
        "data": data,
        "provenance": provenance,
    }
    return CanonicalActivePlaceRecord(
        record_hash=stable_sha256(_record_payload(**values)),
        **values,
    )


def _dataset(*records: CanonicalActivePlaceRecord) -> CanonicalActiveDataset:
    ordered = sorted(records, key=lambda item: item.place_id)
    entity_city_counts = [
        CanonicalEntityCityCount(
            city_id="city_da_nang",
            entity_type=EntityType.CAFE,
            count=len(ordered),
        )
    ]
    quotas = [
        EntityCityQuota(
            city_id="city_da_nang",
            entity_type=EntityType.CAFE,
            target_count=len(ordered),
            active_count=len(ordered),
            vacancy_count=0,
        )
    ]
    report_values = {
        "manifest_id": MANIFEST_ID,
        "manifest_hash": MANIFEST_HASH,
        "master_source_record_count": len(ordered),
        "approved_replacement_count": 0,
        "canonical_record_count": len(ordered),
        "master_materialized_count": len(ordered),
        "replacement_materialized_count": 0,
        "retired_duplicate_count": 0,
        "open_vacancy_count": 0,
        "filled_vacancy_count": 0,
        "entity_city_counts": entity_city_counts,
        "quotas": quotas,
    }
    report = CanonicalActiveDatasetReport(
        report_hash=stable_sha256(_report_payload(**report_values)),
        **report_values,
    )
    dataset_values = {
        "manifest_id": MANIFEST_ID,
        "manifest_hash": MANIFEST_HASH,
        "records": ordered,
        "report": report,
    }
    dataset_hash = stable_sha256(_dataset_payload(**dataset_values))
    return CanonicalActiveDataset(
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        **dataset_values,
    )


def _readiness(
    dataset: CanonicalActiveDataset,
) -> CanonicalDatasetReadinessReport:
    values = {
        "schema_version": "1.3.0",
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "audit_id": "duplicate-evidence-v8-import-test",
        "audit_hash": "4" * 64,
        "manifest_id": dataset.manifest_id,
        "manifest_hash": dataset.manifest_hash,
        "group_count": 0,
        "resolved_merge": [],
        "resolved_distinct": [],
        "resolved_quarantined": [],
        "unresolved_groups": [],
        "open_vacancy_count": 0,
        "publish_ready": True,
        "explicit_distinct_decisions": [],
    }
    readiness_hash = stable_sha256(_readiness_payload(**values))
    return CanonicalDatasetReadinessReport(
        readiness_id=f"canonical-readiness-{readiness_hash[:20]}",
        readiness_hash=readiness_hash,
        **values,
    )


def _write_artifacts(
    tmp_path: Path,
    dataset: CanonicalActiveDataset,
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    readiness = _readiness(dataset)
    completeness = build_canonical_completeness_audit(
        dataset,
        readiness,
        as_of=AS_OF,
        stay_context=CanonicalHotelStayContext(
            check_in=date(2026, 8, 24),
            check_out=date(2026, 8, 25),
        ),
    )
    dataset_path = tmp_path / "dataset.json"
    readiness_path = tmp_path / "readiness.json"
    completeness_path = tmp_path / "completeness.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    readiness_path.write_text(
        readiness.model_dump_json(indent=2),
        encoding="utf-8",
    )
    completeness_path.write_text(
        completeness.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return dataset_path, readiness_path, completeness_path


def test_prepare_is_deterministic_and_never_assumes_692_places(
    tmp_path: Path,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(
            _record("cafe_dn_900", name="First Cafe"),
            _record("cafe_dn_901", name="Second Cafe"),
        ),
    )

    first = prepare_canonical_v8_import(*paths)
    second = prepare_canonical_v8_import(*paths)

    assert first.release == second.release
    assert first.release.place_count == 2
    assert len(first.places) == len(first.versions) == len(first.provenances) == 2
    assert len(first.documents) == len(first.text_units) == 2
    assert len(first.facts) == first.release.fact_count
    assert len(first.claims) == first.release.claim_count
    assert [item["id"] for item in first.places] == [
        "cafe_dn_900",
        "cafe_dn_901",
    ]
    assert first.places[0]["properties"]["kb_version"] == "v8"
    assert first.places[0]["properties"]["current_record_hash"]
    assert first.places[0]["properties"]["opening_hours_open"] == "08:00"
    assert first.places[0]["properties"]["opening_hours_close"] == "22:00"
    assert first.places[0]["properties"]["price_per_person_min"] == 30000
    assert first.places[0]["version_id"].startswith("v8:place-version:")
    assert first.categories[0]["properties"]["name"] == "Coffee shop"
    assert {item["concept_type"] for item in first.concepts} >= {
        "amenities",
        "tags",
    }
    assert first.documents[0]["properties"]["content_hash"]
    assert first.text_units[0]["properties"]["content_hash"]
    assert first.text_units[0]["properties"]["semantic_content_hash"]
    assert first.text_units[0]["properties"]["semantic_text"]
    assert first.places[0]["properties"]["semantic_content_hash"]
    assert first.places[0]["properties"]["semantic_text"]
    assert first.concepts[0]["properties"]["semantic_content_hash"]
    assert first.concepts[0]["properties"]["semantic_text"]
    text = first.text_units[0]["properties"]["text"]
    assert text.splitlines()[:7] == [
        "Tên: First Cafe",
        "Loại: cafe",
        "Thành phố: Da Nang",
        "Địa chỉ: 1 Bach Dang, Da Nang",
        "Danh mục: Coffee shop",
        "Tọa độ: 16.0544, 108.2022",
        "Mô tả: Verified description for First Cafe",
    ]
    assert all(
        marker not in text
        for marker in (
            "TÃªn:",
            "Loáº¡i:",
            "ThÃ nh phá»‘:",
            "Äá»‹a chá»‰:",
            "Danh má»¥c:",
            "Tá»a Ä‘á»™:",
            "MÃ´ táº£:",
        )
    )
    assert "fact:opening_hours=08:00-22:00" in text
    assert {item["properties"]["predicate"] for item in first.facts} >= {
        "address",
        "opening_hours",
        "price_min",
    }
    assert {item["properties"]["predicate"] for item in first.claims} >= {
        "HAS_AMENITY",
        "HAS_TAG",
    }


def test_dry_run_needs_no_graph_or_model_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    monkeypatch.delenv("GEMINI_PLANNER_MODEL", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)

    result = dry_run_canonical_v8_import(*paths)

    assert result.dry_run is True
    assert result.status == "validated"
    assert result.place_count == 1


def test_mismatched_artifacts_fail_closed(tmp_path: Path) -> None:
    first_paths = _write_artifacts(
        tmp_path / "first",
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    second_paths = _write_artifacts(
        tmp_path / "second",
        _dataset(_record("cafe_dn_901", name="Second Cafe")),
    )

    with pytest.raises(CanonicalV8ArtifactError, match="different canonical dataset"):
        prepare_canonical_v8_import(
            first_paths[0],
            second_paths[1],
            first_paths[2],
        )


def test_static_blocking_gap_rejects_release(tmp_path: Path) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe", address=None)),
    )

    with pytest.raises(CanonicalV8GateError, match="static_ingest_ready=false"):
        prepare_canonical_v8_import(*paths)


def test_release_manifest_writer_is_content_addressed(tmp_path: Path) -> None:
    paths = _write_artifacts(
        tmp_path / "inputs",
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    release = prepare_canonical_v8_import(*paths).release
    writer = CanonicalV8ReleaseManifestWriter(tmp_path / "releases")

    first = writer.write(release)
    second = writer.write(release)

    assert first == second
    assert first.read_text(encoding="utf-8").endswith("\n")


def test_schema_creates_empty_semantic_indexes_without_embedding_calls() -> None:
    queries: list[str] = []
    store = SimpleNamespace(run=lambda query: queries.append(query) or [])

    ensure_canonical_v8_schema(store, embedding_dimension=768)

    source = "\n".join(queries)
    assert "CREATE VECTOR INDEX place_embedding" in source
    assert "CREATE VECTOR INDEX concept_embedding" in source
    assert "CREATE VECTOR INDEX text_unit_embedding" in source
    assert "v8_place_embedding" not in source
    assert "v8_concept_embedding" not in source
    assert "`vector.dimensions`: 768" in source
    assert "GEMINI" not in source
    with pytest.raises(ValueError, match="embedding_dimension must be positive"):
        ensure_canonical_v8_schema(store, embedding_dimension=0)


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    def data(self) -> list[dict[str, Any]]:
        return self._rows

    def consume(self) -> None:
        return None


class _FakeTransaction:
    def __init__(
        self,
        plan,
        *,
        stage_status: str = "staging",
        immutable_conflict: str | None = None,
    ) -> None:
        self.plan = plan
        self.stage_status = stage_status
        self.immutable_conflict = immutable_conflict
        self.queries: list[str] = []
        self.transactions: list[list[str]] = []

    def run(self, query: str, **params: Any) -> _FakeResult:
        self.queries.append(query)
        release = self.plan.release
        place_ids = sorted(item["id"] for item in self.plan.places)
        if "v8:stage-release" in query:
            return _FakeResult(
                [
                    {
                        "id": release.release_id,
                        "release_hash": release.release_hash,
                        "manifest_json": params["properties"]["manifest_json"],
                        "kb_version": "v8",
                        "status": self.stage_status,
                    }
                ]
            )
        if "v8:check-immutable" in query:
            mismatch = (
                [f"v8:conflict:{self.immutable_conflict}"]
                if self.immutable_conflict and self.immutable_conflict in query
                else []
            )
            return _FakeResult([{"mismatched_ids": mismatch}])
        if "v8:check-staged-release" in query:
            return _FakeResult(
                [
                    {
                        "release_hash": release.release_hash,
                        "manifest_json": params.get("manifest_json")
                        or _manifest_json(release),
                        "place_ids": place_ids,
                        "version_count": len(place_ids),
                        "document_count": len(self.plan.documents),
                        "text_unit_count": len(self.plan.text_units),
                        "text_unit_document_count": len(self.plan.text_units),
                        "fact_count": len(self.plan.facts),
                        "fact_evidence_count": len(self.plan.facts),
                        "claim_count": len(self.plan.claims),
                        "claim_object_count": len(self.plan.claims),
                        "claim_evidence_count": len(self.plan.claims),
                    }
                ]
            )
        if "v8:check-staged-provenance" in query:
            return _FakeResult(
                [
                    {
                        "invalid_place_ids": [],
                        "version_count": len(place_ids),
                        "provenance_count": len(place_ids),
                    }
                ]
            )
        if "v8:activate-release" in query:
            return _FakeResult([{"release_id": release.release_id, "status": "active"}])
        if "v8:check-active-release" in query:
            return _FakeResult(
                [
                    {
                        "release_id": release.release_id,
                        "place_ids": place_ids,
                        "place_count": len(place_ids),
                        "current_version_count": len(place_ids),
                        "city_count": len(self.plan.cities),
                        "document_count": len(self.plan.documents),
                        "text_unit_count": len(self.plan.text_units),
                        "text_unit_document_count": len(self.plan.text_units),
                        "fact_count": len(self.plan.facts),
                        "fact_evidence_count": len(self.plan.facts),
                        "claim_count": len(self.plan.claims),
                        "claim_object_count": len(self.plan.claims),
                        "claim_evidence_count": len(self.plan.claims),
                        "semantic_index_status": "pending",
                    }
                ]
            )
        return _FakeResult()


def _manifest_json(release) -> str:
    import json

    return json.dumps(
        release.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class _FakeSession:
    def __init__(self, transaction: _FakeTransaction) -> None:
        self.transaction = transaction

    def __enter__(self):
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute_write(self, callback):
        start = len(self.transaction.queries)
        try:
            return callback(self.transaction)
        finally:
            self.transaction.transactions.append(self.transaction.queries[start:])


class _FakeDriver:
    def __init__(self, transaction: _FakeTransaction) -> None:
        self.transaction = transaction
        self.session_options: dict[str, Any] | None = None

    def session(self, **options: Any) -> _FakeSession:
        self.session_options = options
        return _FakeSession(self.transaction)


def test_apply_commits_staging_batches_then_atomically_activates(
    tmp_path: Path,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    plan = prepare_canonical_v8_import(*paths)
    transaction = _FakeTransaction(plan)
    store = SimpleNamespace(
        driver=_FakeDriver(transaction),
        settings=SimpleNamespace(neo4j_database="neo4j"),
    )

    result = apply_canonical_v8_import(store, plan, ensure_schema=False)

    source = "\n".join(transaction.queries)
    activation_transactions = [
        queries
        for queries in transaction.transactions
        if "v8:activate-release" in "\n".join(queries)
    ]
    assert len(transaction.transactions) > 2
    assert len(activation_transactions) == 1
    activation_source = "\n".join(activation_transactions[0])
    assert "v8:check-staged-release" in activation_source
    assert "v8:clean-current-outgoing" in activation_source
    assert "v8:activate-places" in activation_source
    assert "v8:activate-cities" in activation_source
    assert "v8:activate-concepts" in activation_source
    assert "v8:stage-versions" not in activation_source
    assert "v8:stage-facts" not in activation_source
    fact_stage_transactions = [
        "\n".join(queries)
        for queries in transaction.transactions
        if "v8:stage-facts" in "\n".join(queries)
    ]
    assert fact_stage_transactions
    assert all(
        "v8:check-immutable-facts" in queries for queries in fact_stage_transactions
    )
    assert "ON CREATE SET city = row.properties" in source
    assert "ON CREATE SET concept = row.properties" in source
    assert result.status == "active"
    assert result.place_count == 1
    assert "SET place:Place:Entity" in source
    assert "HAS_VERSION" in source
    assert "CURRENT_VERSION" in source
    assert "SUPPORTED_BY" in source
    assert "HAS_EVIDENCE_DOCUMENT" in source
    assert "HAS_EVIDENCE_TEXT_UNIT" in source
    assert "HAS_RECORDED_FACT" in source
    assert "HAS_RECORDED_CLAIM" in source
    assert "PART_OF_VERSION_DOCUMENT" in source
    assert "ABOUT_VERSION" in source
    assert "SUPPORTED_BY" not in _CLEAN_CURRENT_COMPATIBILITY
    assert "PART_OF" not in _CLEAN_CURRENT_COMPATIBILITY
    assert "OBJECT" not in _CLEAN_CURRENT_COMPATIBILITY
    assert "v8:reset-document-labels" in source
    assert "v8:reset-text-unit-labels" in source
    assert "v8:reset-fact-labels" in source
    assert "v8:reset-claim-labels" in source
    assert "REMOVE concept:Concept:Term:Dish:Activity" in source
    assert "SET concept:Dish" in source
    assert "SET concept:Activity" in source
    assert "HAS_TYPE" in source
    assert "HAS_CATEGORY" in source
    assert "HAS_CONCEPT" in source
    assert "TravelCatalog" in source
    assert "HAS_CITY" in source
    assert "REMOVE place:Place" in source
    assert "REMOVE concept:Concept:Term" in source
    assert ":V8" not in source
    assert "preserved_embedding" in source
    assert result.static_graph_ready is True
    assert result.semantic_index_ready is False
    assert result.semantic_index_status == "pending"
    assert result.document_count == 1
    assert result.text_unit_count == 1
    assert result.fact_count == len(plan.facts)
    assert result.claim_count == len(plan.claims)
    assert store.driver.session_options == {"database": "neo4j"}


def test_reapplying_active_release_is_a_noop_that_preserves_derived_data(
    tmp_path: Path,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    plan = prepare_canonical_v8_import(*paths)
    transaction = _FakeTransaction(plan, stage_status="active")
    store = SimpleNamespace(
        driver=_FakeDriver(transaction),
        settings=SimpleNamespace(neo4j_database="neo4j"),
    )

    result = apply_canonical_v8_import(store, plan, ensure_schema=False)

    source = "\n".join(transaction.queries)
    assert result.status == "active"
    assert "v8:stage-release" in source
    assert "v8:check-active-release" in source
    assert "v8:activate-places" not in source
    assert "v8:reset-concept-labels" not in source


def test_superseded_release_cannot_rollback_the_current_catalog(
    tmp_path: Path,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    plan = prepare_canonical_v8_import(*paths)
    transaction = _FakeTransaction(plan, stage_status="superseded")
    store = SimpleNamespace(
        driver=_FakeDriver(transaction),
        settings=SimpleNamespace(neo4j_database="neo4j"),
    )

    with pytest.raises(CanonicalV8GraphWriteError, match="cannot be resumed"):
        apply_canonical_v8_import(store, plan, ensure_schema=False)

    source = "\n".join(transaction.queries)
    assert "v8:stage-release" in source
    assert "v8:stage-versions" not in source
    assert "v8:clean-current-outgoing" not in source


def test_immutable_compatibility_content_conflict_fails_transaction(
    tmp_path: Path,
) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    plan = prepare_canonical_v8_import(*paths)
    transaction = _FakeTransaction(plan, immutable_conflict="documents")
    store = SimpleNamespace(
        driver=_FakeDriver(transaction),
        settings=SimpleNamespace(neo4j_database="neo4j"),
    )

    with pytest.raises(CanonicalV8GraphWriteError, match="documents conflict"):
        apply_canonical_v8_import(store, plan, ensure_schema=False)

    assert "v8:activate-release" not in "\n".join(transaction.queries)
    assert "v8:clean-current-outgoing" not in "\n".join(transaction.queries)
    document_stage = [
        "\n".join(queries)
        for queries in transaction.transactions
        if "v8:stage-documents" in "\n".join(queries)
    ]
    assert document_stage
    assert "v8:check-immutable-documents" in document_stage[-1]


def test_incomplete_staging_can_resume_idempotently(tmp_path: Path) -> None:
    paths = _write_artifacts(
        tmp_path,
        _dataset(_record("cafe_dn_900", name="First Cafe")),
    )
    plan = prepare_canonical_v8_import(*paths)
    transaction = _FakeTransaction(plan, immutable_conflict="documents")
    store = SimpleNamespace(
        driver=_FakeDriver(transaction),
        settings=SimpleNamespace(neo4j_database="neo4j"),
    )

    with pytest.raises(CanonicalV8GraphWriteError, match="documents conflict"):
        apply_canonical_v8_import(store, plan, ensure_schema=False)

    transaction.immutable_conflict = None
    result = apply_canonical_v8_import(store, plan, ensure_schema=False)

    assert result.status == "active"
    source = "\n".join(transaction.queries)
    assert source.count("v8:stage-documents") == 2
    assert source.count("v8:activate-release") == 1


def test_hotel_static_snapshot_does_not_publish_a_price_fact() -> None:
    cafe = _record("hotel_dn_900", name="Future Hotel")
    hotel_data = dict(cafe.data)
    hotel_data["price_per_night"] = {
        "min": 900000,
        "max": 1200000,
        "currency": "VND",
    }
    hotel = cafe.model_copy(
        update={
            "primary_type": EntityType.HOTEL,
            "place_types": [EntityType.HOTEL],
            "data": hotel_data,
        }
    )

    predicates = {item["predicate"] for item in _record_fact_specs(hotel)}
    place_properties = _place_properties(
        hotel,
        SimpleNamespace(release_id="release-test", dataset_id="dataset-test"),
    )

    assert predicates.isdisjoint({"price", "price_min", "price_max"})
    assert "price_per_night_min" not in place_properties
    assert "price_per_night_max" not in place_properties
    assert "price_range" not in place_properties
