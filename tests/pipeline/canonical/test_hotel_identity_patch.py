from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nextrip_pipeline.canonical.dataset import read_canonical_active_dataset
from nextrip_pipeline.canonical.hotel_identity_patch import (
    CanonicalHotelIdentityPatchError,
    apply_canonical_hotel_identity_patch,
    build_canonical_hotel_identity_patch,
)
from nextrip_pipeline.schemas import EntityType
from tests.canonical_dataset_support import CanonicalTestPlace, write_canonical_dataset


NOW = datetime(2026, 8, 26, 4, 30, tzinfo=timezone.utc)


def _dataset(tmp_path):
    path = write_canonical_dataset(
        tmp_path / "datasets",
        [
            CanonicalTestPlace(
                place_id="hotel_dn_001",
                name="Old Hotel",
                latitude=16.07,
                longitude=108.23,
                entity_type=EntityType.HOTEL,
                address="Old address",
                data={
                    "description": "Old identity description",
                    "amenities": ["old amenity"],
                    "booking_links": {"booking": "https://old.example"},
                },
            )
        ],
    )
    return read_canonical_active_dataset(path)


def _evidence(tmp_path, source_id: str, source_record_id: str):
    path = tmp_path / "evidence" / f"{source_record_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"verified":true}\n', encoding="utf-8")
    return {
        "source_id": source_id,
        "source_record_id": source_record_id,
        "path": path.relative_to(tmp_path).as_posix(),
    }


def test_replacement_clears_stale_identity_and_preserves_id(tmp_path):
    dataset = _dataset(tmp_path)
    corrections = {
        "records": [
            {
                "place_id": "hotel_dn_001",
                "mode": "replace",
                "previous_name": "Old Hotel",
                "name": "Provider Hotel",
                "address": "2 New Street, Da Nang",
                "latitude": 16.05,
                "longitude": 108.25,
                "phone": "+84 236 000 0000",
                "website_url": "https://provider.example",
                "trivago_external_id": "provider-1",
                "trivago_property_id": "12345",
                "trivago_url": "https://www.trivago.vn/provider-hotel",
                "star_rating": 4,
                "review_rating": 9.1,
                "review_count": 100,
                "evidence": [
                    _evidence(tmp_path, "trivago-mcp", "trivago-1"),
                    _evidence(tmp_path, "google-maps-web", "google-1"),
                ],
                "reason": "Reviewed same-ID physical replacement",
                "reviewer": "reviewer",
                "reviewed_at": NOW.isoformat(),
            }
        ]
    }
    patch = build_canonical_hotel_identity_patch(
        dataset, corrections, evidence_root=tmp_path, generated_at=NOW
    )
    candidate = apply_canonical_hotel_identity_patch(dataset, patch)
    record = candidate.records[0]

    assert record.place_id == "hotel_dn_001"
    assert record.name == "Provider Hotel"
    assert record.address == "2 New Street, Da Nang"
    assert record.coordinates.lat == 16.05
    assert record.data["amenities"] == []
    assert record.data["booking_links"] == {
        "trivago": "https://www.trivago.vn/provider-hotel"
    }
    assert record.data["hotel_identity_patch"]["mode"] == "replace"
    assert candidate.dataset_id != dataset.dataset_id


def test_patch_rejects_stale_previous_name(tmp_path):
    dataset = _dataset(tmp_path)
    corrections = {
        "records": [
            {
                "place_id": "hotel_dn_001",
                "mode": "rename",
                "previous_name": "Wrong old name",
                "name": "Provider Hotel",
                "trivago_external_id": "provider-1",
                "trivago_property_id": "12345",
                "trivago_url": "https://www.trivago.vn/provider-hotel",
                "evidence": [_evidence(tmp_path, "trivago-mcp", "trivago-1")],
                "reason": "Provider rename",
                "reviewer": "reviewer",
                "reviewed_at": NOW.isoformat(),
            }
        ]
    }

    with pytest.raises(CanonicalHotelIdentityPatchError, match="stale previous_name"):
        build_canonical_hotel_identity_patch(
            dataset, corrections, evidence_root=tmp_path, generated_at=NOW
        )
