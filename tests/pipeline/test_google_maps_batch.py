from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from nextrip_pipeline.decision_gate import MenuDecisionStatus
from nextrip_pipeline.cli import build_parser
from nextrip_pipeline.jobs import (
    GoogleMapsBatchMode,
    GoogleMapsBatchRunner,
    GoogleMapsBatchSummaryWriter,
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


def test_airflow_dag_module_is_safe_without_airflow_installed() -> None:
    dag_path = Path(__file__).parents[2] / "dags" / "nextrip_google_maps.py"
    spec = importlib.util.spec_from_file_location("nextrip_google_maps_dag", dag_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "batch-google-maps" in module._batch_command("place", 32)
    assert "--mode menu" in module._batch_command("menu", 16)
    assert "build-google-maps-registry" in module._registry_command()


def test_batch_cli_accepts_entity_filters_and_exclusions() -> None:
    arguments = build_parser().parse_args(
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
        ]
    )

    assert arguments.entity_type == ["cafe", "restaurant"]
    assert arguments.exclude_entity_id == ["cafe_dn_062"]
    assert arguments.entity_id == ["cafe_dn_001", "rest_dn_001"]
