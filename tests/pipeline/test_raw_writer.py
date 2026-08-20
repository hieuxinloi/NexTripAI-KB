from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from nextrip_pipeline.crawl import (
    ContentHashMismatchError,
    RawJsonWriter,
    RawRecordAlreadyExistsError,
    compute_content_hash,
)
from nextrip_pipeline.schemas import EntityType, SourceRecord


def _record(*, content_hash: str | None = None) -> SourceRecord:
    payload = {
        "name": "Khách sạn Biển Xanh",
        "offers": [{"amount": 1250000, "currency": "VND"}],
    }
    return SourceRecord(
        source_record_id="record/001",
        run_id="hotel-price-20260818T050000Z",
        source_id="hotel-partner-api",
        entity_type=EntityType.HOTEL,
        crawled_at=datetime(2026, 8, 18, 5, tzinfo=timezone.utc),
        raw_payload=payload,
        content_hash=content_hash or compute_content_hash(payload),
        parser_version="1.0.0",
        source_url="https://example.com/hotels/001",
        http_status=200,
        content_type="application/json",
    )


def test_writer_persists_source_record_in_partitioned_path(tmp_path) -> None:
    writer = RawJsonWriter(tmp_path / "raw")
    record = _record()

    destination = writer.write(record)

    assert destination.relative_to(tmp_path / "raw").as_posix() == (
        "entity=hotel/source=hotel-partner-api/date=2026-08-18/"
        "run=hotel-price-20260818T050000Z/record=record%2F001.json"
    )
    persisted = json.loads(destination.read_text(encoding="utf-8"))
    assert persisted["source_record_id"] == "record/001"
    assert persisted["raw_payload"] == record.raw_payload


def test_writer_never_overwrites_existing_record(tmp_path) -> None:
    writer = RawJsonWriter(tmp_path / "raw")
    record = _record()
    writer.write(record)

    with pytest.raises(RawRecordAlreadyExistsError, match="immutable"):
        writer.write(record)


def test_writer_rejects_payload_with_wrong_hash(tmp_path) -> None:
    writer = RawJsonWriter(tmp_path / "raw")
    record = _record(content_hash="0" * 64)

    with pytest.raises(ContentHashMismatchError, match="raw_payload hashes to"):
        writer.write(record)

    assert not writer.destination_for(record).exists()
