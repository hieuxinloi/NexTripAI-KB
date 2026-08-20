from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.crawl import (
    GoogleMapsRegistryBuilder,
    GoogleMapsRegistryWriter,
    MasterDataValidationError,
)
from nextrip_pipeline.jobs import load_google_maps_manifest
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping, MappingStatus


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def _write_master_files(root: Path) -> None:
    for index, entity_type in enumerate(EntityType):
        item = {
            "id": f"{entity_type.value}_dn_001",
            "entity_type": entity_type.value,
            "name": f"Place {index}",
            "city": "Đà Nẵng",
            "address": f"{index} Test Street",
            "coordinates": {"lat": 16.0 + index / 100, "lng": 108.0},
        }
        (root / f"{entity_type.value}_final.json").write_text(
            json.dumps(
                {"metadata": {"total_count": 1}, "data": [item]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def test_registry_builder_covers_all_master_files_and_merges_override(
    tmp_path: Path,
) -> None:
    _write_master_files(tmp_path)
    override = ExternalEntityMapping(
        mapping_id="manual-cafe",
        entity_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Confirmed Cafe",
        external_url="https://www.google.com/maps/place/confirmed",
        status=MappingStatus.AUTO_MATCHED,
        confidence=0.9,
        matched_at=NOW,
        attributes={"verified_menu_image_url": "https://example.com/menu.jpg"},
    )

    registry, report = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(
        tmp_path,
        overrides=[override],
    )

    assert len(registry.mappings) == 5
    assert report.mapping_count == 5
    assert report.overrides_applied == 1
    cafe = next(item for item in registry.mappings if item.entity_id == "cafe_dn_001")
    assert str(cafe.external_url) == "https://www.google.com/maps/place/confirmed"
    assert cafe.external_id == "Place 1"
    assert cafe.attributes["verified_menu_image_url"] == "https://example.com/menu.jpg"
    assert cafe.attributes["master_latitude"] == 16.01


def test_registry_file_can_drive_batch_manifest(tmp_path: Path) -> None:
    _write_master_files(tmp_path)
    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)
    generated = tmp_path / "generated" / "registry.json"
    GoogleMapsRegistryWriter.write(registry, generated)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"registry_file": "generated/registry.json"}), encoding="utf-8"
    )

    mappings = load_google_maps_manifest(manifest)

    assert len(mappings) == 5
    assert {item.entity_type for item in mappings} == set(EntityType)


def test_manifest_overlays_persisted_confirmed_mapping(tmp_path: Path) -> None:
    _write_master_files(tmp_path)
    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)
    generated = tmp_path / "generated" / "registry.json"
    GoogleMapsRegistryWriter.write(registry, generated)
    resolved_directory = tmp_path / "current-mappings"
    resolved_directory.mkdir()
    base = next(
        item for item in registry.mappings if item.entity_id == "cafe_dn_001"
    )
    confirmed = ExternalEntityMapping.model_validate(
        {
            **base.model_dump(mode="python"),
            "external_id": "Google Cafe",
            "external_url": "https://www.google.com/maps/place/google-cafe",
            "status": MappingStatus.CONFIRMED,
            "confidence": 0.95,
            "verified_at": NOW,
        }
    )
    (resolved_directory / "cafe_dn_001.json").write_text(
        confirmed.model_dump_json(indent=2), encoding="utf-8"
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "registry_file": "generated/registry.json",
                "resolved_mapping_dir": "current-mappings",
            }
        ),
        encoding="utf-8",
    )

    mappings = load_google_maps_manifest(manifest)
    cafe = next(item for item in mappings if item.entity_id == "cafe_dn_001")

    assert cafe.status is MappingStatus.CONFIRMED
    assert cafe.external_id == "Google Cafe"


def test_registry_rejects_invalid_master_coordinates(tmp_path: Path) -> None:
    _write_master_files(tmp_path)
    path = tmp_path / "cafe_final.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["data"][0]["coordinates"] = None
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MasterDataValidationError, match="coordinates"):
        GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)
