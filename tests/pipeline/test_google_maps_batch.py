from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import nextrip_pipeline.cli as cli_module
from nextrip_pipeline.decision_gate import MenuDecisionStatus
from nextrip_pipeline.jobs import (
    GoogleMapsBatchItemStatus,
    GoogleMapsBatchMode,
    GoogleMapsBatchRunner,
    GoogleMapsBatchSummaryWriter,
    google_maps_batch_requires_retry,
    load_google_maps_manifest,
)
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping, MappingStatus
from nextrip_pipeline.publishing import GoogleMapsMenuSourceIndex


NOW = datetime(2026, 8, 18, 2, tzinfo=timezone.utc)


def _mapping(index: int, *, menu: bool = False) -> ExternalEntityMapping:
    attributes = (
        {"verified_menu_image_url": f"https://example.com/{index}.jpg"} if menu else {}
    )
    return ExternalEntityMapping(
        mapping_id=f"maps-{index}",
        entity_id=f"cafe-{index}",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id=f"Cafe {index}",
        status=MappingStatus.CONFIRMED,
        matched_at=NOW,
        verified_at=NOW,
        attributes=attributes,
    )


def test_google_maps_batch_isolates_failures_and_enforces_limit() -> None:
    calls = []

    def processor(mapping, run_id):
        calls.append((mapping.entity_id, run_id))
        if len(calls) == 1:
            raise RuntimeError("blocked")
        return SimpleNamespace(
            raw_path=Path(f"raw/{mapping.entity_id}.json"),
            normalized_path=Path(f"normalized/{mapping.entity_id}.json"),
            decision=SimpleNamespace(status=MenuDecisionStatus.REVIEW),
        )

    summary = GoogleMapsBatchRunner(
        processor,
        mode=GoogleMapsBatchMode.PLACE,
        max_requests=3,
        clock=lambda: NOW,
    ).run([_mapping(0), _mapping(1), _mapping(2), _mapping(3)], run_id="batch-1")

    assert len(calls) == 3
    assert summary.selected_count == 3
    assert summary.succeeded_count == 2
    assert summary.failed_count == 1
    assert any(item.error == "RuntimeError: blocked" for item in summary.items)


def test_trusted_batch_marks_isolated_error_no_update_and_guards_outages() -> None:
    def processor(mapping, run_id):
        if mapping.entity_id == "cafe-0":
            raise RuntimeError("place detail not resolved")
        return SimpleNamespace(decision=None)

    summary = GoogleMapsBatchRunner(
        processor,
        mode=GoogleMapsBatchMode.PLACE,
        max_requests=4,
        item_error_status=GoogleMapsBatchItemStatus.NO_UPDATE,
        clock=lambda: NOW,
    ).run([_mapping(index) for index in range(4)], run_id="scheduled-attempt")

    assert summary.succeeded_count == 3
    assert summary.no_update_count == 1
    assert summary.failed_count == 0
    assert summary.items[0].status is GoogleMapsBatchItemStatus.NO_UPDATE
    assert google_maps_batch_requires_retry(
        summary, max_no_update_ratio=0.20
    ) is False

    correlated = summary.model_copy(
        update={"succeeded_count": 2, "no_update_count": 2}
    )
    assert google_maps_batch_requires_retry(
        correlated, max_no_update_ratio=0.20
    ) is True


def test_menu_batch_only_selects_mappings_with_verified_menu_image() -> None:
    processed = []
    runner = GoogleMapsBatchRunner(
        lambda mapping, run_id: (
            processed.append(mapping.entity_id) or SimpleNamespace(decision=None)
        ),
        mode=GoogleMapsBatchMode.MENU,
        max_requests=16,
        clock=lambda: NOW,
    )

    summary = runner.run([_mapping(1), _mapping(2, menu=True)], run_id="menu-batch")

    assert processed == ["cafe-2"]
    assert summary.eligible_count == 1
    assert summary.succeeded_count == 1


def test_menu_batch_excludes_hotel_even_when_it_has_a_menu_image() -> None:
    hotel = _mapping(9, menu=True).model_copy(update={"entity_type": EntityType.HOTEL})
    summary = GoogleMapsBatchRunner(
        lambda mapping, run_id: SimpleNamespace(decision=None),
        mode=GoogleMapsBatchMode.MENU,
        clock=lambda: NOW,
    ).run([hotel], run_id="hotel-menu")

    assert summary.eligible_count == 0
    assert summary.selected_count == 0


def test_menu_batch_accepts_discovered_menu_image() -> None:
    mapping = _mapping(3).model_copy(
        update={
            "attributes": {
                "discovered_menu_image_url": "https://example.com/discovered.jpg"
            }
        }
    )
    summary = GoogleMapsBatchRunner(
        lambda mapping, run_id: SimpleNamespace(decision=None),
        mode=GoogleMapsBatchMode.MENU,
        clock=lambda: NOW,
    ).run([mapping], run_id="discovered-menu")

    assert summary.eligible_count == 1
    assert summary.succeeded_count == 1


def test_manual_offset_selects_next_batch_on_the_same_day() -> None:
    processed = []
    runner = GoogleMapsBatchRunner(
        lambda mapping, run_id: (
            processed.append(mapping.entity_id) or SimpleNamespace(decision=None)
        ),
        mode=GoogleMapsBatchMode.PLACE,
        max_requests=2,
        offset=2,
        clock=lambda: NOW,
    )

    runner.run([_mapping(index) for index in range(5)], run_id="offset-batch")

    assert processed == ["cafe-2", "cafe-3"]


def test_place_crawl_menu_source_index_keeps_latest_entry(tmp_path: Path) -> None:
    index = GoogleMapsMenuSourceIndex(tmp_path)
    observation = SimpleNamespace(
        place_id="cafe-1",
        source_record_id="source-1",
        observed_at=NOW,
        menu_source=SimpleNamespace(menu_image_urls=["https://example.com/menu.jpg"]),
    )

    path = index.publish(observation)
    entry = index.get("cafe-1")

    assert path is not None and path.exists()
    assert entry is not None
    assert str(entry.image_url) == "https://example.com/menu.jpg"


def test_manifest_uses_paths_relative_to_manifest_and_summary_is_written(
    tmp_path: Path,
) -> None:
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(_mapping(1).model_dump_json(indent=2), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"mapping_files": ["mapping.json"]}), encoding="utf-8"
    )

    mappings = load_google_maps_manifest(manifest)
    summary = GoogleMapsBatchRunner(
        lambda mapping, run_id: SimpleNamespace(decision=None),
        mode=GoogleMapsBatchMode.PLACE,
        clock=lambda: NOW,
    ).run(mappings, run_id="manifest-batch")
    path = GoogleMapsBatchSummaryWriter(tmp_path / "summaries").write(summary)

    assert mappings[0].mapping_id == "maps-1"
    assert json.loads(path.read_text(encoding="utf-8"))["succeeded_count"] == 1


def _load_google_maps_dag_module():
    dag_path = Path(__file__).parents[2] / "dags" / "nextrip_google_maps.py"
    spec = importlib.util.spec_from_file_location("nextrip_google_maps_dag", dag_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_airflow_dag_module_is_safe_and_canonical_only_without_airflow() -> None:
    module = _load_google_maps_dag_module()

    registry_command = module._registry_command()
    batch_commands = {
        entity_type: module._place_batch_command(entity_type)
        for entity_type in ("attraction", "cafe", "nightlife", "restaurant")
    }
    apply_command = module._apply_canonical_refresh_command()

    assert "build-google-maps-registry" in registry_command
    assert "NEXTRIP_CANONICAL_DATASET_POINTER" in registry_command
    assert "resolve-active-canonical-dataset" in registry_command
    assert 'elif [ -n "${NEXTRIP_CANONICAL_DATASET:-}" ]' in registry_command
    assert '--canonical-dataset "$CANONICAL_DATASET"' in registry_command
    assert "canonical-google-maps-batch-manifest.json" in registry_command
    assert registry_command.endswith('printf "%s\\n" "$CANONICAL_DATASET"')
    assert "--base-manifest" not in registry_command
    assert "--master-dir" not in registry_command
    assert "travel_data_verified" not in registry_command

    for entity_type, batch_command in batch_commands.items():
        assert "batch-google-maps" in batch_command
        assert "--mode place" in batch_command
        assert "canonical-google-maps-batch-manifest.json" in batch_command
        assert "--current-place-dir" not in batch_command
        assert "data/current/place" not in batch_command
        assert "$MAPS_SCRATCH/current-place" not in batch_command
        assert f"--entity-type {entity_type}" in batch_command
        assert batch_command.count("--entity-type") == 1
        assert '--run-id "$SOURCE_RUN_ID"' in batch_command
        assert f"-{{{{ run_id }}}}-{entity_type}-try{{{{ ti.try_number }}}}-" in (
            batch_command
        )
        assert "from uuid import uuid4" in batch_command
        assert "$EXECUTION_ID" in batch_command
        assert "--max-no-update-ratio" in batch_command
        assert "NEXTRIP_MAPS_MAX_NO_UPDATE_RATIO:-0.20" in batch_command
        assert 'MAPS_SCRATCH="${NEXTRIP_MAPS_SCRATCH_ROOT' in batch_command
        assert '/$SOURCE_RUN_ID"' in batch_command
        assert 'test "$RETURNED_RUN_ID" = "$SOURCE_RUN_ID"' in batch_command
        assert batch_command.endswith('printf "%s\\n" "$RETURNED_RUN_ID"')
        assert "NEXTRIP_GOOGLE_MAPS_TRUSTED_SCHEDULED:-false" in batch_command
        assert "resolve-active-canonical-dataset" not in batch_command
        assert "ti.xcom_pull(task_ids='build_google_maps_registry')" in batch_command
        assert 'test -f "$CANONICAL_DATASET"' in batch_command
        assert "--trusted-scheduled-crawl" in batch_command
        assert "--disable-llm-review-queue" in batch_command
        assert "--accepted-observation-dir" in batch_command
        assert "NEXTRIP_ACCEPTED_OBSERVATION_ROOT:-data/observations" in batch_command
        assert "NEXTRIP_RAW_ROOT:-data/raw" in batch_command
        assert "NEXTRIP_VALIDATION_ROOT:-data/validation" in batch_command
        assert "NEXTRIP_MAPS_WEEKLY_LIMIT" in batch_command
        assert "NEXTRIP_MAPS_SUMMARY_ROOT:-data/runs/google_maps" in batch_command

    assert "apply-google-maps-canonical-refresh" in apply_command
    assert "resolve-active-canonical-dataset" not in apply_command
    assert "ti.xcom_pull(task_ids='build_google_maps_registry')" in apply_command
    assert 'test -f "$CANONICAL_DATASET"' in apply_command
    assert '--canonical-dataset "$CANONICAL_DATASET"' in apply_command
    assert "--skip-menu-backlog" not in apply_command
    assert apply_command.count("--run-id") == 4
    assert 'test "$UNIQUE_SOURCE_RUN_ID_COUNT" -eq 4' in apply_command
    for entity_type in ("attraction", "cafe", "nightlife", "restaurant"):
        shell_name = entity_type.upper()
        assert (
            f"ti.xcom_pull(task_ids='refresh_google_maps_{entity_type}')"
            in apply_command
        )
        assert f'--run-id "$SOURCE_RUN_ID_{shell_name}"' in apply_command
        assert f'test -n "$SOURCE_RUN_ID_{shell_name}"' in apply_command
    assert 'READINESS_PATH="$(printf "%s\\n" "$APPLY_OUTPUT"' in apply_command
    assert 'PATCH_PATH="$(printf "%s\\n" "$APPLY_OUTPUT"' in apply_command
    assert '"base_dataset":' in apply_command
    assert '"candidate_dataset":' in apply_command
    assert 'DATASET_PATH="$(realpath "$DATASET_PATH")"' in apply_command
    assert 'READINESS_PATH="$(realpath "$READINESS_PATH")"' in apply_command
    assert 'PATCH_PATH="$(realpath "$PATCH_PATH")"' in apply_command
    assert '"readiness":' in apply_command
    assert '"patch":' in apply_command


def test_airflow_google_maps_source_is_weekly_gated_and_has_no_legacy_sink() -> None:
    dag_path = Path(__file__).parents[2] / "dags" / "nextrip_google_maps.py"
    source = dag_path.read_text(encoding="utf-8")

    assert 'dag_id="nextrip_google_maps_weekly"' in source
    assert "NEXTRIP_GOOGLE_MAPS_AIRFLOW_ENABLED" in source
    assert 'NEXTRIP_GOOGLE_MAPS_SCHEDULE", "0 2 * * 1"' in source
    assert 'NEXTRIP_GOOGLE_MAPS_POOL", "google_maps_web"' in source
    assert "do_xcom_push=True" in source
    assert source.count("resolve-active-canonical-dataset") == 1
    assert "apply_google_maps_canonical_refresh" in source
    assert "trigger_canonical_release_rollout" in source
    assert "NEXTRIP_CANONICAL_ROLLOUT_AIRFLOW_ENABLED" in source
    assert 'task_id="refresh_google_maps_places"' not in source
    assert "for entity_type in CANONICAL_GOOGLE_MAPS_ENTITY_TYPES" in source
    assert "nextrip_google_maps_menu" not in source
    assert "refresh_google_maps_menus" not in source
    assert "--mode menu" not in source
    assert "schedule=None" not in source
    assert "travel_data_verified" not in source
    assert "data/current/place" not in source
    assert "--current-place-dir" not in source
    assert "--base-manifest" not in source
    assert "--skip-menu-backlog" not in source


def test_google_maps_airflow_gate_is_strict_and_disabled_by_default(
    monkeypatch,
) -> None:
    module = _load_google_maps_dag_module()

    monkeypatch.delenv("TEST_GOOGLE_AIRFLOW_FLAG", raising=False)
    assert module._env_flag("TEST_GOOGLE_AIRFLOW_FLAG") is False
    monkeypatch.setenv("TEST_GOOGLE_AIRFLOW_FLAG", "on")
    assert module._env_flag("TEST_GOOGLE_AIRFLOW_FLAG") is True


def test_batch_cli_accepts_entity_filters_and_exclusions() -> None:
    arguments = cli_module.build_parser().parse_args(
        [
            "batch-google-maps",
            "--manifest",
            "manifest.json",
            "--mode",
            "place",
            "--entity-type",
            "cafe",
            "--entity-type",
            "restaurant",
            "--exclude-entity-id",
            "cafe_dn_062",
            "--entity-id",
            "cafe_dn_001",
            "--entity-id",
            "rest_dn_001",
            "--force-search",
            "--search-query-mode",
            "name",
        ]
    )

    assert arguments.entity_type == ["cafe", "restaurant"]
    assert arguments.exclude_entity_id == ["cafe_dn_062"]
    assert arguments.entity_id == ["cafe_dn_001", "rest_dn_001"]
    assert arguments.force_search is True
    assert arguments.search_query_mode == "name"
    assert arguments.max_no_update_ratio == 0.20


def test_refresh_google_place_cli_accepts_force_search() -> None:
    arguments = cli_module.build_parser().parse_args(
        [
            "refresh-google-place",
            "--mapping",
            "mapping.json",
            "--force-search",
            "--search-query-mode",
            "name",
        ]
    )

    assert arguments.force_search is True
    assert arguments.search_query_mode == "name"


def test_google_maps_refresh_pipeline_forwards_force_search() -> None:
    arguments = cli_module.build_parser().parse_args(
        [
            "refresh-google-place",
            "--mapping",
            "mapping.json",
            "--force-search",
            "--search-query-mode",
            "name",
        ]
    )

    pipeline = cli_module._google_maps_refresh_pipeline(
        arguments,
        SimpleNamespace(),
    )

    assert pipeline.adapter.force_search is True
    assert pipeline.adapter.search_query_mode == "name"
