from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nextrip_pipeline.crawl import (
    GoogleMapsMappingRegistry,
    GoogleMapsRegistryBuilder,
    GoogleMapsRegistryWriter,
    MasterDataValidationError,
)
from nextrip_pipeline.jobs import load_google_maps_manifest
from nextrip_pipeline.google_maps_identity import (
    google_maps_stable_external_id,
    google_maps_stable_external_ids,
)
from nextrip_pipeline.schemas import EntityType, ExternalEntityMapping, MappingStatus


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
GOOGLE_TOKEN = "0x31421b00723d9291:0x46d9f1c4fa5c9f78"
GOOGLE_URL = (
    "https://www.google.com/maps/place/Confirmed+Cafe/"
    f"data=!4m7!3m6!1s{GOOGLE_TOKEN}!8m2"
)


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
    assert cafe.external_url is None
    assert cafe.external_id == "search:cafe_dn_001"
    assert cafe.status is MappingStatus.AUTO_MATCHED
    assert cafe.attributes["verified_menu_image_url"] == "https://example.com/menu.jpg"
    assert cafe.attributes["master_latitude"] == 16.01
    assert cafe.attributes["search_query"].startswith("Place 1, 1 Test Street")


def test_registry_search_placeholders_are_unique_and_not_display_names(
    tmp_path: Path,
) -> None:
    _write_master_files(tmp_path)

    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)

    external_ids = [item.external_id for item in registry.mappings]
    assert len(external_ids) == len(set(external_ids))
    assert external_ids == [
        f"search:{item.entity_id}" for item in registry.mappings
    ]


def test_confirmed_name_only_override_is_downgraded_to_search(
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
        status=MappingStatus.CONFIRMED,
        confidence=0.95,
        matched_at=NOW,
        verified_at=NOW,
    )

    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(
        tmp_path,
        overrides=[override],
    )

    cafe = next(item for item in registry.mappings if item.entity_id == "cafe_dn_001")
    assert cafe.external_id == "search:cafe_dn_001"
    assert cafe.external_url is None
    assert cafe.status is MappingStatus.PENDING_REVIEW
    assert cafe.verified_at is None


def test_confirmed_override_uses_stable_token_from_google_url(
    tmp_path: Path,
) -> None:
    _write_master_files(tmp_path)
    override = ExternalEntityMapping(
        mapping_id="manual-cafe",
        entity_id="cafe_dn_001",
        entity_type=EntityType.CAFE,
        source_id="google-maps-web",
        external_id="Confirmed Cafe",
        external_url=GOOGLE_URL,
        status=MappingStatus.CONFIRMED,
        confidence=0.95,
        matched_at=NOW,
        verified_at=NOW,
    )

    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(
        tmp_path,
        overrides=[override],
    )

    cafe = next(item for item in registry.mappings if item.entity_id == "cafe_dn_001")
    assert cafe.external_id == GOOGLE_TOKEN
    assert str(cafe.external_url) == GOOGLE_URL
    assert cafe.status is MappingStatus.CONFIRMED


def test_query_place_id_must_be_a_stable_google_identifier() -> None:
    assert (
        google_maps_stable_external_id(
            "https://www.google.com/maps/search/?query_place_id=Coffee+Shop"
        )
        is None
    )
    assert google_maps_stable_external_id(
        "https://www.google.com/maps/search/?query_place_id=ChIJabc_123"
    ) == "ChIJabc_123"


def test_opaque_google_place_ids_are_compared_case_sensitively() -> None:
    assert google_maps_stable_external_ids(
        "ChIJabc_123",
        "ChIJAbc_123",
        "ChIJabc_123",
    ) == ("ChIJAbc_123", "ChIJabc_123")


def _confirmed_mapping(
    entity_id: str,
    entity_type: EntityType,
    token: str = GOOGLE_TOKEN,
) -> ExternalEntityMapping:
    return ExternalEntityMapping(
        mapping_id=f"google-maps-{entity_id}",
        entity_id=entity_id,
        entity_type=entity_type,
        source_id="google-maps-web",
        external_id=token,
        external_url=(
            "https://www.google.com/maps/place/Test/"
            f"data=!4m7!3m6!1s{token}!8m2"
        ),
        status=MappingStatus.CONFIRMED,
        confidence=1.0,
        matched_at=NOW,
        verified_at=NOW,
    )


def test_registry_model_rejects_duplicate_active_stable_external_id() -> None:
    with pytest.raises(
        ValueError,
        match="duplicate active Google stable external_id.*cafe_dn_001.*nightlife_dn_001",
    ):
        GoogleMapsMappingRegistry(
            generated_at=NOW,
            source_files=["test.json"],
            mappings=[
                _confirmed_mapping("cafe_dn_001", EntityType.CAFE),
                _confirmed_mapping("nightlife_dn_001", EntityType.NIGHTLIFE),
            ],
        )


def test_registry_builder_reports_duplicate_active_stable_external_id(
    tmp_path: Path,
) -> None:
    _write_master_files(tmp_path)

    with pytest.raises(
        MasterDataValidationError,
        match="duplicate active Google stable external_id.*cafe_dn_001.*nightlife_dn_001",
    ):
        GoogleMapsRegistryBuilder(clock=lambda: NOW).build(
            tmp_path,
            overrides=[
                _confirmed_mapping("cafe_dn_001", EntityType.CAFE),
                _confirmed_mapping("nightlife_dn_001", EntityType.NIGHTLIFE),
            ],
        )


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


def test_manifest_rejects_duplicate_stable_ids_from_resolved_overlays(
    tmp_path: Path,
) -> None:
    _write_master_files(tmp_path)
    registry, _ = GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)
    generated = tmp_path / "generated" / "registry.json"
    GoogleMapsRegistryWriter.write(registry, generated)
    resolved_directory = tmp_path / "current-mappings"
    resolved_directory.mkdir()
    by_entity_id = {item.entity_id: item for item in registry.mappings}
    for entity_id, entity_type in (
        ("cafe_dn_001", EntityType.CAFE),
        ("nightlife_dn_001", EntityType.NIGHTLIFE),
    ):
        resolved = _confirmed_mapping(entity_id, entity_type).model_copy(
            update={"mapping_id": by_entity_id[entity_id].mapping_id}
        )
        (resolved_directory / f"{entity_id}.json").write_text(
            resolved.model_dump_json(indent=2), encoding="utf-8"
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

    with pytest.raises(
        ValueError,
        match="duplicate active Google stable external_id.*cafe_dn_001.*nightlife_dn_001",
    ):
        load_google_maps_manifest(manifest)


def test_registry_rejects_invalid_master_coordinates(tmp_path: Path) -> None:
    _write_master_files(tmp_path)
    path = tmp_path / "cafe_final.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["data"][0]["coordinates"] = None
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MasterDataValidationError, match="coordinates"):
        GoogleMapsRegistryBuilder(clock=lambda: NOW).build(tmp_path)
