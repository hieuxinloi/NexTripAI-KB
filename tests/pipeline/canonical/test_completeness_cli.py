from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.canonical.completeness import (
    CanonicalCompletenessAudit,
    CanonicalCompletenessWriter,
    CanonicalHotelStayContext,
    CanonicalSourcePolicy,
    CompletenessArtifactKind,
    CompletenessField,
    build_canonical_completeness_audit,
    digest_artifact_file,
)
from nextrip_pipeline.canonical.crawl_backlog import (
    CanonicalCrawlBacklogWriter,
    CanonicalCrawlJob,
    CanonicalCrawlRequiredField,
    build_canonical_crawl_backlog,
    read_canonical_crawl_backlog,
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
from nextrip_pipeline.canonical.models import (
    EntityCityQuota,
    stable_sha256,
)
from nextrip_pipeline.canonical.readiness import (
    CanonicalDatasetReadinessReport,
    _readiness_payload,
)
from nextrip_pipeline.cli import build_parser, main
from nextrip_pipeline.crawl import (
    GoogleMapsMappingRegistry,
    TrivagoHotelRegistry,
    TrivagoHotelRegistryEntry,
)
from nextrip_pipeline.jobs.google_maps_batch import load_google_maps_manifest
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 21, 8, 30, tzinfo=UTC)
MANIFEST_ID = "canonical-manifest-test"
MANIFEST_HASH = "1" * 64


def _canonical_record(
    place_id: str,
    entity_type: EntityType,
    *,
    name: str,
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
                source_filename=f"{entity_type.value}_final.json",
                record_index=0,
                source_record_hash="3" * 64,
            )
        ],
    )
    data = {
        "id": place_id,
        "entity_type": entity_type.value,
        "primary_type": entity_type.value,
        "place_types": [entity_type.value],
        "aliases": [],
        "name": name,
        "tags": [],
        "google_maps_category": entity_type.value,
        "description": f"Verified description for {name}",
        "cover_image_url": "https://example.com/cover.jpg",
        "opening_hours": {"monday": "08:00-22:00"},
        "price_range": "100000-200000 VND",
        "amenities": ["wifi"] if entity_type is EntityType.HOTEL else [],
    }
    values = {
        "place_id": place_id,
        "primary_type": entity_type,
        "secondary_types": [],
        "place_types": [entity_type],
        "name": name,
        "aliases": [],
        "tags": [],
        "city_id": "city_da_nang",
        "city": "Da Nang",
        "address": "1 Bach Dang, Da Nang",
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


def _canonical_dataset(
    *record_specs: tuple[str, EntityType, str],
    open_vacancy_count: int = 0,
) -> CanonicalActiveDataset:
    records = sorted(
        (
            _canonical_record(place_id, entity_type, name=name)
            for place_id, entity_type, name in record_specs
        ),
        key=lambda item: item.place_id,
    )
    counts: dict[EntityType, int] = {}
    for record in records:
        counts[record.primary_type] = counts.get(record.primary_type, 0) + 1
    entity_city_counts = [
        CanonicalEntityCityCount(
            city_id="city_da_nang",
            entity_type=entity_type,
            count=count,
        )
        for entity_type, count in sorted(counts.items(), key=lambda item: item[0].value)
    ]
    quotas = []
    for index, (entity_type, count) in enumerate(
        sorted(counts.items(), key=lambda item: item[0].value)
    ):
        vacancy_count = open_vacancy_count if index == 0 else 0
        quotas.append(
            EntityCityQuota(
                city_id="city_da_nang",
                entity_type=entity_type,
                target_count=count + vacancy_count,
                active_count=count,
                vacancy_count=vacancy_count,
            )
        )
    report_values = {
        "manifest_id": MANIFEST_ID,
        "manifest_hash": MANIFEST_HASH,
        "master_source_record_count": len(records) + open_vacancy_count,
        "approved_replacement_count": 0,
        "canonical_record_count": len(records),
        "master_materialized_count": len(records),
        "replacement_materialized_count": 0,
        "retired_duplicate_count": open_vacancy_count,
        "open_vacancy_count": open_vacancy_count,
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
        "records": records,
        "report": report,
    }
    dataset_hash = stable_sha256(_dataset_payload(**dataset_values))
    return CanonicalActiveDataset(
        dataset_id=f"canonical-active-{dataset_hash[:20]}",
        dataset_hash=dataset_hash,
        **dataset_values,
    )


def _ready_report(
    dataset: CanonicalActiveDataset,
    *,
    schema_version: str = "1.1.0",
) -> CanonicalDatasetReadinessReport:
    values = {
        "schema_version": schema_version,
        "dataset_id": dataset.dataset_id,
        "dataset_hash": dataset.dataset_hash,
        "audit_id": "duplicate-evidence-test",
        "audit_hash": "4" * 64,
        "manifest_id": dataset.manifest_id,
        "manifest_hash": dataset.manifest_hash,
        "group_count": 0,
        "resolved_merge": [],
        "resolved_distinct": [],
        "unresolved_groups": [],
        "publish_ready": dataset.report.open_vacancy_count == 0,
        "explicit_distinct_decisions": [],
    }
    if schema_version in {"1.2.0", "1.3.0"}:
        values["resolved_quarantined"] = []
    if schema_version == "1.3.0":
        values["open_vacancy_count"] = dataset.report.open_vacancy_count
    readiness_hash = stable_sha256(_readiness_payload(**values))
    return CanonicalDatasetReadinessReport(
        readiness_id=f"canonical-readiness-{readiness_hash[:20]}",
        readiness_hash=readiness_hash,
        **values,
    )


def _mapping(entity_id: str, entity_type: EntityType) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"google-maps-{entity_id}",
        entity_id=entity_id,
        entity_type=entity_type,
        source_id="google-maps-web",
        external_id=entity_id,
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.7,
        matched_at=NOW,
    )


def test_new_canonical_cli_defaults_are_safe() -> None:
    audit = build_parser().parse_args(
        [
            "audit-canonical-completeness",
            "--dataset",
            "dataset.json",
            "--readiness",
            "readiness.json",
        ]
    )
    backlog = build_parser().parse_args(
        [
            "build-canonical-crawl-backlog",
            "--completeness-report",
            "completeness.json",
        ]
    )
    registry = build_parser().parse_args(
        [
            "build-google-maps-registry",
            "--canonical-dataset",
            "canonical.json",
        ]
    )
    maps_batch = build_parser().parse_args(
        [
            "batch-google-maps",
            "--manifest",
            "maps-manifest.json",
            "--mode",
            "place",
        ]
    )
    trivago_batch = build_parser().parse_args(["batch-trivago-availability"])

    assert audit.as_of is None
    assert audit.check_in is None
    assert audit.check_out is None
    assert audit.adults == 2
    assert audit.children == 0
    assert audit.rooms == 1
    assert not hasattr(audit, "current_place_dir")
    assert audit.google_manifest == Path(
        "config/generated/canonical-google-maps-batch-manifest.json"
    )
    assert audit.output_dir == Path("data/canonical/completeness")
    assert backlog.sources == Path("config/sources.json")
    assert backlog.output_dir == Path("data/canonical/crawl-backlogs")
    assert registry.canonical_dataset == Path("canonical.json")
    assert not hasattr(registry, "master_dir")
    assert registry.base_manifest is None
    assert registry.batch_manifest_output is None
    assert registry.output == Path(
        "config/generated/canonical-google-maps-mapping-registry.json"
    )
    assert registry.report == Path(
        "config/generated/canonical-google-maps-registry-report.json"
    )
    assert maps_batch.backlog is None
    assert trivago_batch.backlog is None
    assert maps_batch.claim_dir == Path("data/runs/canonical_crawl_claims")
    assert trivago_batch.claim_dir == Path("data/runs/canonical_crawl_claims")
    assert maps_batch.current_menu_dir == Path("data/current/menu")


def test_google_maps_registry_cli_requires_canonical_dataset() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["build-google-maps-registry"])


def test_completeness_cli_rejects_removed_current_place_flag() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "audit-canonical-completeness",
                "--dataset",
                "dataset.json",
                "--readiness",
                "readiness.json",
                "--current-place-dir",
                "data/current/place",
            ]
        )


def test_completeness_as_of_rejects_naive_datetime_and_normalizes_offset(
    capsys,
) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(
            [
                "audit-canonical-completeness",
                "--dataset",
                "dataset.json",
                "--readiness",
                "readiness.json",
                "--as-of",
                "2026-08-21T08:30:00",
            ]
        )

    assert error.value.code == 2
    assert "datetime must include a timezone" in capsys.readouterr().err

    arguments = build_parser().parse_args(
        [
            "audit-canonical-completeness",
            "--dataset",
            "dataset.json",
            "--readiness",
            "readiness.json",
            "--as-of",
            "2026-08-21T15:30:00+07:00",
        ]
    )
    assert arguments.as_of == NOW
    assert arguments.as_of.tzinfo is UTC


def test_completeness_cli_reports_open_vacancy_identity_gate(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
        open_vacancy_count=1,
    )
    readiness = _ready_report(dataset, schema_version="1.3.0")
    dataset_path = tmp_path / "dataset.json"
    readiness_path = tmp_path / "readiness.json"
    dataset_path.write_text(dataset.model_dump_json(), encoding="utf-8")
    readiness_path.write_text(readiness.model_dump_json(), encoding="utf-8")

    google_mapping = _mapping(
        "cafe_dn_active",
        EntityType.CAFE,
    ).model_copy(update={"attributes": {"canonical_dataset_id": dataset.dataset_id}})
    monkeypatch.setattr(
        "nextrip_pipeline.cli.load_google_maps_manifest",
        lambda _: [google_mapping],
    )
    google_registry_path = tmp_path / "google-registry.json"
    google_registry_path.write_text("{}", encoding="utf-8")
    google_manifest_path = tmp_path / "google-manifest.json"
    google_manifest_path.write_text(
        json.dumps({"registry_file": google_registry_path.name}),
        encoding="utf-8",
    )

    trivago_registry = TrivagoHotelRegistry(
        generated_at=NOW,
        source_file="test-fixture",
        entries=[
            TrivagoHotelRegistryEntry(
                entity_id="hotel_outside_dataset",
                master_name="Unused Hotel",
                city="Da Nang",
                search_query="Unused Hotel Da Nang",
            )
        ],
    )
    trivago_registry_path = tmp_path / "trivago-registry.json"
    trivago_registry_path.write_text(
        trivago_registry.model_dump_json(),
        encoding="utf-8",
    )
    output_root = tmp_path / "completeness"
    empty_root = tmp_path / "empty-artifacts"

    exit_code = main(
        [
            "audit-canonical-completeness",
            "--dataset",
            str(dataset_path),
            "--readiness",
            str(readiness_path),
            "--as-of",
            NOW.isoformat(),
            "--google-manifest",
            str(google_manifest_path),
            "--google-registry",
            str(google_registry_path),
            "--trivago-registry",
            str(trivago_registry_path),
            "--trivago-mapping-dir",
            str(empty_root),
            "--hotel-availability-dir",
            str(empty_root),
            "--hotel-price-dir",
            str(empty_root),
            "--current-menu-dir",
            str(empty_root),
            "--menu-source-dir",
            str(empty_root),
            "--output-dir",
            str(output_root),
        ]
    )

    assert exit_code == 0
    audit_path = next(output_root.rglob("canonical-completeness-audit.json"))
    audit = CanonicalCompletenessAudit.model_validate_json(audit_path.read_bytes())
    assert audit.schema_version == "1.1.0"
    assert audit.identity_review_place_ids == []
    assert audit.open_vacancy_count == 1
    assert audit.identity_publish_ready is False
    assert CompletenessArtifactKind.CURRENT_PLACE not in {
        item.kind for item in audit.input_digests
    }
    output = capsys.readouterr().out
    assert "identity_publish_ready=false identity_review_places=0 " in output
    assert "open_vacancies=1" in output


def test_canonical_registry_is_active_non_hotel_and_batch_manifest_is_loadable(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
        ("hotel_dn_active", EntityType.HOTEL, "Active Hotel"),
    )
    dataset_path = tmp_path / "canonical-dataset.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")

    base_registry = GoogleMapsMappingRegistry(
        generated_at=NOW,
        source_files=["test-fixture"],
        mappings=[
            _mapping("cafe_dn_active", EntityType.CAFE),
            _mapping("cafe_dn_retired", EntityType.CAFE),
            _mapping("hotel_dn_active", EntityType.HOTEL),
        ],
    )
    base_registry_path = tmp_path / "base-registry.json"
    base_registry_path.write_text(
        base_registry.model_dump_json(indent=2), encoding="utf-8"
    )
    base_manifest_path = tmp_path / "base-manifest.json"
    base_manifest_path.write_text(
        json.dumps({"registry_file": base_registry_path.name}),
        encoding="utf-8",
    )
    output_path = tmp_path / "generated" / "canonical-registry.json"
    report_path = tmp_path / "generated" / "canonical-report.json"
    batch_manifest_path = tmp_path / "generated" / "batch-manifest.json"

    exit_code = main(
        [
            "build-google-maps-registry",
            "--canonical-dataset",
            str(dataset_path),
            "--base-manifest",
            str(base_manifest_path),
            "--output",
            str(output_path),
            "--report",
            str(report_path),
            "--batch-manifest-output",
            str(batch_manifest_path),
        ]
    )

    assert exit_code == 0
    generated = GoogleMapsMappingRegistry.model_validate_json(output_path.read_bytes())
    assert [item.entity_id for item in generated.mappings] == ["cafe_dn_active"]
    assert generated.mappings[0].attributes["search_query"].endswith("Việt Nam")
    assert generated.source_files == [f"canonical-dataset:{dataset.dataset_id}"]
    assert [
        item.entity_id for item in load_google_maps_manifest(batch_manifest_path)
    ] == ["cafe_dn_active"]
    manifest_document = json.loads(batch_manifest_path.read_text(encoding="utf-8"))
    assert manifest_document == {"registry_file": "canonical-registry.json"}
    report_document = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_document["mapping_count"] == 1
    assert report_document["reused_mapping_count"] == 1
    output = capsys.readouterr().out
    assert f"batch_manifest={batch_manifest_path}" in output
    assert "mappings=1" in output


def test_canonical_registry_does_not_reuse_an_implicit_legacy_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
    )
    dataset_path = tmp_path / "canonical-dataset.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")

    legacy_registry = GoogleMapsMappingRegistry(
        generated_at=NOW,
        source_files=["stale-derived-cache"],
        mappings=[_mapping("cafe_dn_active", EntityType.CAFE)],
    )
    legacy_directory = tmp_path / "config"
    legacy_directory.mkdir()
    legacy_registry_path = legacy_directory / "legacy-registry.json"
    legacy_registry_path.write_text(
        legacy_registry.model_dump_json(indent=2), encoding="utf-8"
    )
    (legacy_directory / "google-maps-batch-manifest.json").write_text(
        json.dumps({"registry_file": legacy_registry_path.name}),
        encoding="utf-8",
    )

    output_path = tmp_path / "generated" / "canonical-registry.json"
    report_path = tmp_path / "generated" / "canonical-report.json"
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "build-google-maps-registry",
            "--canonical-dataset",
            str(dataset_path),
            "--output",
            str(output_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    generated = GoogleMapsMappingRegistry.model_validate_json(output_path.read_bytes())
    assert generated.mappings[0].external_id != "cafe_dn_active"
    assert generated.mappings[0].status is MappingStatus.AUTO_MATCHED
    assert (
        json.loads(report_path.read_text(encoding="utf-8"))["reused_mapping_count"] == 0
    )


def test_build_canonical_crawl_backlog_cli_from_small_completeness_fixture(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
    )
    audit = build_canonical_completeness_audit(
        dataset,
        _ready_report(dataset),
        as_of=NOW,
        stay_context=CanonicalHotelStayContext(
            check_in=date(2026, 8, 22),
            check_out=date(2026, 8, 23),
        ),
    )
    audit_path = CanonicalCompletenessWriter(tmp_path / "completeness").write(audit)
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "source_id": "google-maps-web",
                        "display_name": "Google Maps Web",
                        "kind": "aggregator",
                        "crawl_method": "playwright",
                        "entity_types": [
                            "attraction",
                            "cafe",
                            "nightlife",
                            "restaurant",
                        ],
                        "base_url": "https://www.google.com/maps",
                        "parser_version": "test-1.0.0",
                        "enabled": True,
                        "schedule_interval_minutes": 1440,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "backlogs"

    exit_code = main(
        [
            "build-canonical-crawl-backlog",
            "--completeness-report",
            str(audit_path),
            "--sources",
            str(sources_path),
            "--output-dir",
            str(output_root),
        ]
    )

    assert exit_code == 0
    backlog_path = next(output_root.rglob("canonical-crawl-backlog.json"))
    backlog = read_canonical_crawl_backlog(backlog_path)
    assert backlog.dataset_id == dataset.dataset_id
    assert backlog.task_count == 2
    assert backlog.automatic_count == 1
    assert backlog.blocked_count == 1
    assert backlog.manual_count == 1
    assert backlog.job_counts == {
        CanonicalCrawlJob.GOOGLE_MAPS_PLACE.value: 1,
        CanonicalCrawlJob.MENU_HUMAN_REVIEW.value: 1,
    }
    task = next(
        item
        for item in backlog.tasks
        if item.job is CanonicalCrawlJob.GOOGLE_MAPS_PLACE
    )
    assert task.place_id == "cafe_dn_active"
    assert task.job is CanonicalCrawlJob.GOOGLE_MAPS_PLACE
    assert CanonicalCrawlRequiredField.DAILY_OPENING in task.required_fields
    assert CanonicalCrawlRequiredField.MENU_SOURCE not in task.required_fields
    manual = next(
        item
        for item in backlog.tasks
        if item.job is CanonicalCrawlJob.MENU_HUMAN_REVIEW
    )
    assert manual.automatic is False
    assert manual.source_id == "human-review"
    output = capsys.readouterr().out
    assert f"backlog={backlog_path}" in output
    assert "tasks=2 automatic=1 blocked=1 manual=1" in output


def test_nightlife_does_not_create_a_menu_human_review_requirement() -> None:
    dataset = _canonical_dataset(
        ("nightlife_dn_active", EntityType.NIGHTLIFE, "Active Nightlife"),
    )

    audit = build_canonical_completeness_audit(
        dataset,
        _ready_report(dataset),
        as_of=NOW,
        stay_context=CanonicalHotelStayContext(
            check_in=date(2026, 8, 22),
            check_out=date(2026, 8, 23),
        ),
    )

    assert not any(gap.field is CompletenessField.VERIFIED_MENU for gap in audit.gaps)


def test_google_backlog_dispatch_pins_canonical_and_ignores_legacy_stores(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
    )
    dataset_path = tmp_path / "canonical-dataset.json"
    dataset_path.write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    mapping = _mapping("cafe_dn_active", EntityType.CAFE).model_copy(
        update={"attributes": {"canonical_dataset_id": dataset.dataset_id}}
    )
    registry = GoogleMapsMappingRegistry(
        generated_at=NOW,
        source_files=[f"canonical-dataset:{dataset.dataset_id}"],
        mappings=[mapping],
    )
    registry_path = tmp_path / "maps-registry.json"
    registry_path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    manifest_path = tmp_path / "maps-manifest.json"
    manifest_path.write_text(
        json.dumps({"registry_file": registry_path.name}),
        encoding="utf-8",
    )
    audit = build_canonical_completeness_audit(
        dataset,
        _ready_report(dataset),
        as_of=NOW,
        stay_context=CanonicalHotelStayContext(
            check_in=date(2026, 8, 22),
            check_out=date(2026, 8, 23),
        ),
        input_digests=[
            digest_artifact_file(
                registry_path,
                kind=CompletenessArtifactKind.GOOGLE_REGISTRY,
            )
        ],
    )
    backlog = build_canonical_crawl_backlog(
        audit,
        [
            CanonicalSourcePolicy(
                source_id="google-maps-web",
                enabled=True,
                schedule_interval_minutes=1440,
                parser_version="test-1.0.0",
            )
        ],
    )
    backlog_path = CanonicalCrawlBacklogWriter(tmp_path / "backlogs").write(backlog)
    poison_legacy_path = tmp_path / "legacy-store-is-not-a-directory"
    poison_legacy_path.write_text("legacy store must not be read", encoding="utf-8")

    class ReachedBrowser(RuntimeError):
        pass

    def stop_at_browser(*args, **kwargs):
        raise ReachedBrowser("canonical backlog verification completed")

    monkeypatch.setattr(
        "nextrip_pipeline.cli.PlaywrightBrowserClient",
        stop_at_browser,
    )

    arguments = [
        "batch-google-maps",
        "--manifest",
        str(manifest_path),
        "--canonical-dataset",
        str(dataset_path),
        "--backlog",
        str(backlog_path),
        "--mode",
        "place",
        "--current-menu-dir",
        str(poison_legacy_path),
        "--menu-source-dir",
        str(poison_legacy_path),
    ]
    exit_code = main(arguments)

    assert exit_code == 2
    assert "canonical backlog verification completed" in capsys.readouterr().err

    wrong_dataset = _canonical_dataset(
        ("cafe_dn_other", EntityType.CAFE, "Other Cafe"),
    )
    wrong_dataset_path = tmp_path / "wrong-canonical-dataset.json"
    wrong_dataset_path.write_text(
        wrong_dataset.model_dump_json(indent=2), encoding="utf-8"
    )
    arguments[arguments.index(str(dataset_path))] = str(wrong_dataset_path)

    assert main(arguments) == 2
    assert "does not match the crawl backlog" in capsys.readouterr().err


def test_google_maps_batch_with_only_blocked_backlog_tasks_is_safe_noop(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
    )
    audit = build_canonical_completeness_audit(
        dataset,
        _ready_report(dataset),
        as_of=NOW,
        stay_context=CanonicalHotelStayContext(
            check_in=date(2026, 8, 22),
            check_out=date(2026, 8, 23),
        ),
    )
    blocked_backlog = build_canonical_crawl_backlog(
        audit,
        [
            CanonicalSourcePolicy(
                source_id="google-maps-web",
                enabled=False,
                schedule_interval_minutes=1440,
                parser_version="test-1.0.0",
            )
        ],
    )
    assert blocked_backlog.task_count == 2
    assert blocked_backlog.automatic_count == 0
    backlog_path = CanonicalCrawlBacklogWriter(tmp_path / "backlogs").write(
        blocked_backlog
    )

    registry = GoogleMapsMappingRegistry(
        generated_at=NOW,
        source_files=["test-fixture"],
        mappings=[_mapping("cafe_dn_active", EntityType.CAFE)],
    )
    registry_path = tmp_path / "maps-registry.json"
    registry_path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    manifest_path = tmp_path / "maps-manifest.json"
    manifest_path.write_text(
        json.dumps({"registry_file": registry_path.name}),
        encoding="utf-8",
    )

    def unexpected_browser_initialization(*args, **kwargs):
        raise AssertionError("blocked backlog must not initialize Playwright")

    monkeypatch.setattr(
        "nextrip_pipeline.cli.PlaywrightBrowserClient",
        unexpected_browser_initialization,
    )
    summary_root = tmp_path / "summaries"

    exit_code = main(
        [
            "batch-google-maps",
            "--manifest",
            str(manifest_path),
            "--backlog",
            str(backlog_path),
            "--mode",
            "place",
            "--summary-dir",
            str(summary_root),
        ]
    )

    assert exit_code == 0
    assert not summary_root.exists()
    assert (
        "backlog_dispatch=skipped job=google_maps_place automatic_due=0"
        in capsys.readouterr().out
    )


def test_completeness_cli_rejects_a_manifest_registry_mismatch(
    tmp_path: Path,
    capsys,
) -> None:
    dataset = _canonical_dataset(
        ("cafe_dn_active", EntityType.CAFE, "Active Cafe"),
    )
    readiness = _ready_report(dataset)
    dataset_path = tmp_path / "dataset.json"
    readiness_path = tmp_path / "readiness.json"
    dataset_path.write_text(dataset.model_dump_json(), encoding="utf-8")
    readiness_path.write_text(readiness.model_dump_json(), encoding="utf-8")
    manifest_registry = tmp_path / "manifest-registry.json"
    explicit_registry = tmp_path / "different-registry.json"
    registry = GoogleMapsMappingRegistry(
        generated_at=NOW,
        source_files=[f"canonical-dataset:{dataset.dataset_id}"],
        mappings=[_mapping("cafe_dn_active", EntityType.CAFE)],
    )
    manifest_registry.write_text(registry.model_dump_json(), encoding="utf-8")
    explicit_registry.write_text(registry.model_dump_json(), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"registry_file": manifest_registry.name}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "audit-canonical-completeness",
            "--dataset",
            str(dataset_path),
            "--readiness",
            str(readiness_path),
            "--google-manifest",
            str(manifest_path),
            "--google-registry",
            str(explicit_registry),
        ]
    )

    assert exit_code == 2
    assert (
        "--google-manifest does not point at --google-registry"
        in capsys.readouterr().err
    )
