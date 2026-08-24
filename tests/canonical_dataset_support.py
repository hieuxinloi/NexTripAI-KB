from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nextrip_pipeline.canonical.dataset import (
    CanonicalActiveDatasetWriter,
    materialize_canonical_active_dataset,
    read_canonical_active_dataset,
)
from nextrip_pipeline.canonical.master import CanonicalMasterLoad, MasterRawRecord
from nextrip_pipeline.canonical.models import LegacyPlaceSlot
from nextrip_pipeline.canonical.resolver import build_canonical_identity_manifest
from nextrip_pipeline.schemas import EntityType


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_CANONICAL_DATASET = (
    REPOSITORY_ROOT
    / "data"
    / "canonical"
    / "datasets"
    / "dataset=canonical-active-76a727f96284f0604924"
    / "canonical-active-dataset.json"
)

_LEGACY_FILENAMES = {
    EntityType.ATTRACTION: "attraction_final.json",
    EntityType.CAFE: "cafe_final.json",
    EntityType.HOTEL: "hotel_final.json",
    EntityType.NIGHTLIFE: "nightlife_final.json",
    EntityType.RESTAURANT: "restaurant_final.json",
}


@dataclass(frozen=True, slots=True)
class CanonicalTestPlace:
    place_id: str
    name: str
    latitude: float
    longitude: float
    entity_type: EntityType = EntityType.CAFE
    city: str = "Da Nang"
    city_id: str = "city_da_nang"
    address: str | None = None
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ("canonical-test",)
    data: dict[str, Any] = field(default_factory=dict)


def write_canonical_dataset(
    root: Path,
    places: list[CanonicalTestPlace],
    *,
    generated_at: datetime | None = None,
) -> Path:
    """Materialize a valid immutable canonical dataset for serving tests."""

    timestamp = generated_at or datetime(2026, 8, 24, tzinfo=timezone.utc)
    raws: list[MasterRawRecord] = []
    slots: list[LegacyPlaceSlot] = []
    for index, place in enumerate(places):
        data = {
            "id": place.place_id,
            "entity_type": place.entity_type.value,
            "name": place.name,
            "aliases": list(place.aliases),
            "tags": list(place.tags),
            "city": place.city,
            "city_id": place.city_id,
            "address": place.address,
            "coordinates": {"lat": place.latitude, "lng": place.longitude},
            "last_updated": timestamp.isoformat(),
            "source": {
                "url": "https://example.test/canonical-source",
                "crawled_at": timestamp.isoformat(),
            },
            **place.data,
        }
        raws.append(
            MasterRawRecord(
                place_id=place.place_id,
                source_filename=f"{place.entity_type.value}_test.json",
                record_index=index,
                raw_record=data,
            )
        )
        slots.append(
            LegacyPlaceSlot(
                legacy_place_id=place.place_id,
                city_id=place.city_id,
                primary_type=place.entity_type,
                tags=list(place.tags),
            )
        )

    master = CanonicalMasterLoad(
        slots=slots,
        raw_records_by_id={record.place_id: record for record in raws},
        source_files=[],
        quota_counts=[],
        explicit_duplicate_decisions=[],
    )
    manifest = build_canonical_identity_manifest(slots, generated_at=timestamp)
    dataset = materialize_canonical_active_dataset(master, manifest)
    return CanonicalActiveDatasetWriter(root).write(dataset)


def write_legacy_projection_from_canonical(
    output_directory: Path,
    *,
    dataset_path: Path = ACTIVE_CANONICAL_DATASET,
) -> Path:
    """Build a temporary legacy fixture from canonical for historical tests.

    The repository deliberately does not ship a second place snapshot. Older
    V1-V7 normalization tests may still exercise their import format, so they
    derive it inside pytest's temporary directory from the one active
    canonical artifact.
    """

    dataset = read_canonical_active_dataset(dataset_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    for entity_type, filename in _LEGACY_FILENAMES.items():
        records = [
            record for record in dataset.records if record.primary_type is entity_type
        ]
        items: list[dict[str, Any]] = []
        for record in records:
            item = dict(record.data)
            item.update(
                {
                    "id": record.place_id,
                    "entity_type": record.primary_type.value,
                    "name": record.name,
                    "aliases": list(record.aliases),
                    "tags": list(record.tags),
                    "city_id": record.city_id,
                    "city": record.city,
                    "address": record.address,
                    "coordinates": {
                        "lat": record.coordinates.lat,
                        "lng": record.coordinates.lng,
                    },
                    "phone": record.phone,
                    "website_url": (
                        str(record.website_url)
                        if record.website_url is not None
                        else None
                    ),
                }
            )
            items.append(item)
        document = {
            "metadata": {
                "entity_type": entity_type.value,
                "total_count": len(items),
                "da_nang_count": sum(
                    item["city_id"] == "city_da_nang" for item in items
                ),
                "quy_nhon_count": sum(
                    item["city_id"] == "city_quy_nhon" for item in items
                ),
            },
            "data": items,
        }
        (output_directory / filename).write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return output_directory
