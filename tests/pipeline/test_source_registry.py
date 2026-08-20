from __future__ import annotations

import json

import pytest

from nextrip_pipeline.crawl import (
    SourceNotFoundError,
    SourceRegistry,
    SourceRegistryError,
)
from nextrip_pipeline.schemas import EntityType


def _source(source_id: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "source_id": source_id,
        "display_name": source_id,
        "kind": "official_website",
        "crawl_method": "beautifulsoup",
        "entity_types": ["hotel", "restaurant"],
        "base_url": "https://example.com",
        "parser_version": "1.0.0",
        "enabled": enabled,
    }


def test_registry_loads_and_filters_sources(tmp_path) -> None:
    registry_path = tmp_path / "sources.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    _source("official-enabled"),
                    _source("official-disabled", enabled=False),
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = SourceRegistry.load(registry_path)

    assert registry.get("official-enabled").parser_version == "1.0.0"
    assert [source.source_id for source in registry.enabled_sources()] == [
        "official-enabled"
    ]
    assert [source.source_id for source in registry.for_entity(EntityType.HOTEL)] == [
        "official-enabled"
    ]
    assert len(registry.for_entity(EntityType.RESTAURANT, enabled_only=False)) == 2


def test_registry_rejects_duplicate_source_ids(tmp_path) -> None:
    registry_path = tmp_path / "sources.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [_source("duplicate"), _source("duplicate")],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SourceRegistryError, match="source_id values must be unique"):
        SourceRegistry.load(registry_path)


def test_registry_raises_for_unknown_source(tmp_path) -> None:
    registry_path = tmp_path / "sources.json"
    registry_path.write_text(
        json.dumps({"version": 1, "sources": []}),
        encoding="utf-8",
    )
    registry = SourceRegistry.load(registry_path)

    with pytest.raises(SourceNotFoundError):
        registry.get("missing")


def test_registry_rejects_unknown_fallback_source(tmp_path) -> None:
    source = _source("primary")
    source["fallback_policy"] = {"fallback_source_ids": ["not-registered"]}
    registry_path = tmp_path / "sources.json"
    registry_path.write_text(
        json.dumps({"version": 1, "sources": [source]}),
        encoding="utf-8",
    )

    with pytest.raises(SourceRegistryError, match="not registered"):
        SourceRegistry.load(registry_path)
