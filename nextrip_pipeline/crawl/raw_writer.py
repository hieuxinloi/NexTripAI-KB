from __future__ import annotations

import hashlib
import json
import os
from datetime import timezone
from pathlib import Path
from urllib.parse import quote

from pydantic import JsonValue, TypeAdapter

from nextrip_pipeline.schemas import SourceRecord


_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class ContentHashMismatchError(ValueError):
    """Raised when raw_payload differs from the declared content_hash."""


class RawRecordAlreadyExistsError(FileExistsError):
    """Raised when an immutable raw record has already been written."""


def canonical_payload_bytes(payload: dict[str, JsonValue]) -> bytes:
    normalized_payload = _JSON_OBJECT_ADAPTER.validate_python(payload)
    return json.dumps(
        normalized_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_content_hash(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def _path_segment(prefix: str, value: str) -> str:
    return f"{prefix}={quote(value, safe='-_.')}"


class RawJsonWriter:
    """Writes SourceRecord envelopes once, partitioned by UTC crawl date."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def destination_for(self, record: SourceRecord) -> Path:
        crawl_date = record.crawled_at.astimezone(timezone.utc).date().isoformat()
        subject = (
            record.entity_type.value
            if record.subject_type.value == "place" and record.entity_type
            else record.subject_type.value
        )
        return (
            self.root_directory
            / f"entity={subject}"
            / _path_segment("source", record.source_id)
            / f"date={crawl_date}"
            / _path_segment("run", record.run_id)
            / f"{_path_segment('record', record.source_record_id)}.json"
        )

    def write(self, record: SourceRecord) -> Path:
        actual_hash = compute_content_hash(record.raw_payload)
        if actual_hash != record.content_hash.lower():
            raise ContentHashMismatchError(
                f"SourceRecord {record.source_record_id!r} declares "
                f"{record.content_hash.lower()}, but raw_payload hashes to {actual_hash}"
            )

        destination = self.destination_for(record)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized_record = (
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except FileExistsError as error:
            raise RawRecordAlreadyExistsError(
                f"Raw record is immutable and already exists: {destination}"
            ) from error

        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(serialized_record)
            file.flush()
            os.fsync(file.fileno())

        return destination
