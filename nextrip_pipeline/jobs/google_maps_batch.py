from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import AwareDatetime, Field

from nextrip_pipeline.crawl import GoogleMapsMappingRegistry
from nextrip_pipeline.schemas import (
    EntityType,
    ExternalEntityMapping,
    MappingStatus,
    NexTripModel,
)


class GoogleMapsBatchMode(StrEnum):
    PLACE = "place"
    MENU = "menu"


class GoogleMapsBatchItemStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NO_UPDATE = "no_update"
    FAILED = "failed"


MENU_ENTITY_TYPES = {
    EntityType.CAFE,
    EntityType.NIGHTLIFE,
    EntityType.RESTAURANT,
}


class GoogleMapsBatchItem(NexTripModel):
    mapping_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    status: GoogleMapsBatchItemStatus
    decision_status: str | None = None
    artifact_paths: list[str] = Field(default_factory=list)
    error: str | None = None


class GoogleMapsBatchSummary(NexTripModel):
    run_id: str = Field(min_length=1)
    mode: GoogleMapsBatchMode
    started_at: AwareDatetime
    finished_at: AwareDatetime
    eligible_count: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    no_update_count: int = Field(default=0, ge=0)
    failed_count: int = Field(ge=0)
    items: list[GoogleMapsBatchItem] = Field(default_factory=list)


class GoogleMapsBatchRunner:
    """Runs one bounded Maps batch while isolating failures per mapping."""

    def __init__(
        self,
        processor: Callable[[ExternalEntityMapping, str], Any],
        *,
        mode: GoogleMapsBatchMode,
        max_requests: int = 32,
        offset: int | None = None,
        item_error_status: GoogleMapsBatchItemStatus = (
            GoogleMapsBatchItemStatus.FAILED
        ),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.processor = processor
        self.mode = mode
        self.max_requests = max_requests
        if offset is not None and offset < 0:
            raise ValueError("offset cannot be negative")
        self.offset = offset
        if item_error_status is GoogleMapsBatchItemStatus.SUCCEEDED:
            raise ValueError("item_error_status cannot be succeeded")
        self.item_error_status = item_error_status
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        mappings: Sequence[ExternalEntityMapping],
        *,
        run_id: str,
    ) -> GoogleMapsBatchSummary:
        started_at = self.clock()
        eligible = sorted(
            (mapping for mapping in mappings if self._eligible(mapping)),
            key=lambda item: item.mapping_id,
        )
        selected = self._daily_rotation(eligible)
        items = []
        for mapping in selected:
            try:
                result = self.processor(mapping, run_id)
                decision = getattr(result, "decision", None)
                paths = [
                    str(value)
                    for field in (
                        "raw_path",
                        "raw_record_path",
                        "normalized_path",
                        "decision_path",
                        "resolution_path",
                        "current_mapping_path",
                        "current_place_path",
                        "review_task_path",
                    )
                    if (value := getattr(result, field, None)) is not None
                ]
                paths.extend(
                    str(value)
                    for value in getattr(result, "validation_paths", ())
                )
                paths.extend(
                    str(value)
                    for value in getattr(
                        result,
                        "accepted_observation_paths",
                        (),
                    )
                )
                items.append(
                    GoogleMapsBatchItem(
                        mapping_id=mapping.mapping_id,
                        entity_id=mapping.entity_id,
                        status=GoogleMapsBatchItemStatus.SUCCEEDED,
                        decision_status=(
                            decision.status.value if decision is not None else None
                        ),
                        artifact_paths=paths,
                    )
                )
            except Exception as error:
                items.append(
                    GoogleMapsBatchItem(
                        mapping_id=mapping.mapping_id,
                        entity_id=mapping.entity_id,
                        status=self.item_error_status,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
        succeeded = sum(
            item.status is GoogleMapsBatchItemStatus.SUCCEEDED for item in items
        )
        no_update = sum(
            item.status is GoogleMapsBatchItemStatus.NO_UPDATE for item in items
        )
        failed = sum(item.status is GoogleMapsBatchItemStatus.FAILED for item in items)
        return GoogleMapsBatchSummary(
            run_id=run_id,
            mode=self.mode,
            started_at=started_at,
            finished_at=self.clock(),
            eligible_count=len(eligible),
            selected_count=len(selected),
            succeeded_count=succeeded,
            no_update_count=no_update,
            failed_count=failed,
            items=items,
        )

    def _eligible(self, mapping: ExternalEntityMapping) -> bool:
        if mapping.source_id != "google-maps-web" or mapping.status not in {
            MappingStatus.CONFIRMED,
            MappingStatus.AUTO_MATCHED,
        }:
            return False
        if self.mode is GoogleMapsBatchMode.PLACE:
            return True
        return mapping.entity_type in MENU_ENTITY_TYPES and isinstance(
            mapping.attributes.get("verified_menu_image_url")
            or mapping.attributes.get("discovered_menu_image_url"),
            str,
        )

    def _daily_rotation(
        self, mappings: Sequence[ExternalEntityMapping]
    ) -> list[ExternalEntityMapping]:
        if len(mappings) <= self.max_requests:
            return list(mappings)
        if self.offset is None:
            day_number = self.clock().astimezone(timezone.utc).date().toordinal()
            start = (day_number * self.max_requests) % len(mappings)
        else:
            start = self.offset % len(mappings)
        rotated = list(mappings[start:]) + list(mappings[:start])
        return rotated[: self.max_requests]


def google_maps_batch_requires_retry(
    summary: GoogleMapsBatchSummary,
    *,
    max_no_update_ratio: float,
) -> bool:
    """Return whether an isolated batch should be retried as a system failure.

    A scheduled place crawl may fail to resolve a small number of individual
    listings. Those items are ``no_update`` and the canonical patch preserves
    their last accepted values. At least one such item is tolerated regardless
    of batch size; a larger correlated failure set trips the ratio guard so a
    parser outage or provider block cannot be mistaken for a healthy refresh.
    """

    if not 0 <= max_no_update_ratio <= 1:
        raise ValueError("max_no_update_ratio must be between 0 and 1")
    if summary.failed_count:
        return True
    if not summary.selected_count or not summary.no_update_count:
        return False
    allowed_no_updates = max(
        1,
        int(summary.selected_count * max_no_update_ratio),
    )
    return summary.no_update_count > allowed_no_updates


def load_google_maps_manifest(path: str | Path) -> list[ExternalEntityMapping]:
    manifest_path = Path(path)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = document.get("mapping_files", [])
    registry_file = document.get("registry_file")
    resolved_mapping_dir = document.get("resolved_mapping_dir")
    if not isinstance(files, list):
        raise ValueError("manifest.mapping_files must be a list")
    if registry_file is not None and not isinstance(registry_file, str):
        raise ValueError("manifest.registry_file must be a string")
    if resolved_mapping_dir is not None and not isinstance(
        resolved_mapping_dir, str
    ):
        raise ValueError("manifest.resolved_mapping_dir must be a string")
    if not files and not registry_file:
        raise ValueError("manifest requires registry_file or mapping_files")
    mappings = []
    seen = set()
    if registry_file:
        registry_path = Path(registry_file)
        if not registry_path.is_absolute():
            registry_path = manifest_path.parent / registry_path
        registry = GoogleMapsMappingRegistry.model_validate_json(
            registry_path.read_text(encoding="utf-8")
        )
        mappings.extend(registry.mappings)
        seen.update(mapping.mapping_id for mapping in registry.mappings)
    for value in files:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("manifest mapping file paths must be strings")
        mapping_path = Path(value)
        if not mapping_path.is_absolute():
            mapping_path = manifest_path.parent / mapping_path
        mapping = ExternalEntityMapping.model_validate_json(
            mapping_path.read_text(encoding="utf-8")
        )
        if mapping.mapping_id in seen:
            raise ValueError(f"duplicate mapping_id in manifest: {mapping.mapping_id}")
        seen.add(mapping.mapping_id)
        mappings.append(mapping)
    if resolved_mapping_dir:
        resolved_directory = Path(resolved_mapping_dir)
        if not resolved_directory.is_absolute():
            resolved_directory = manifest_path.parent / resolved_directory
        by_entity_id = {mapping.entity_id: mapping for mapping in mappings}
        if resolved_directory.exists():
            for resolved_path in sorted(resolved_directory.glob("*.json")):
                resolved = ExternalEntityMapping.model_validate_json(
                    resolved_path.read_text(encoding="utf-8")
                )
                base = by_entity_id.get(resolved.entity_id)
                if base is None:
                    raise ValueError(
                        "resolved mapping references unknown entity_id: "
                        f"{resolved.entity_id}"
                    )
                if (
                    resolved.mapping_id != base.mapping_id
                    or resolved.source_id != base.source_id
                    or resolved.entity_type != base.entity_type
                    or resolved.status
                    not in {MappingStatus.CONFIRMED, MappingStatus.REJECTED}
                ):
                    raise ValueError(
                        f"invalid resolved mapping overlay: {resolved.mapping_id}"
                    )
                by_entity_id[resolved.entity_id] = resolved
            mappings = [by_entity_id[mapping.entity_id] for mapping in mappings]
    # Resolved overlays and standalone mapping files are applied after the
    # registry document is parsed, so validate the final runnable set again.
    validated = GoogleMapsMappingRegistry(
        generated_at=datetime.now(timezone.utc),
        source_files=[str(manifest_path)],
        mappings=mappings,
    )
    return validated.mappings


class GoogleMapsBatchSummaryWriter:
    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(self, summary: GoogleMapsBatchSummary) -> Path:
        destination = (
            self.root_directory
            / f"mode={summary.mode.value}"
            / f"run={quote(summary.run_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Batch summary already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(summary.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination
