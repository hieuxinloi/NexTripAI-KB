from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from nextrip_pipeline.schemas import ExternalEntityMapping, MappingStatus

from .google_maps_mapping import (
    GoogleMapsMappingResolution,
    MappingResolutionStatus,
)


class OlderResolvedMappingError(ValueError):
    """Raised when stale mapping evidence attempts to replace current state."""


class GoogleMapsMappingResolutionWriter:
    """Persist immutable identity-resolution evidence for an audit run."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def write(
        self,
        resolution: GoogleMapsMappingResolution,
        *,
        run_id: str,
    ) -> Path:
        destination = (
            self.root_directory
            / f"run={quote(run_id, safe='-_.')}"
            / f"place={quote(resolution.place_id, safe='-_.')}.json"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as error:
            raise FileExistsError(
                f"Mapping resolution already exists: {destination}"
            ) from error
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(resolution.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return destination


class CurrentGoogleMapsMappingWriter:
    """Atomically retain the newest confirmed or rejected mapping state."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def publish(self, mapping: ExternalEntityMapping) -> Path:
        if mapping.source_id != "google-maps-web":
            raise ValueError("current mapping must belong to google-maps-web")
        if mapping.status not in {MappingStatus.CONFIRMED, MappingStatus.REJECTED}:
            raise ValueError("only confirmed or rejected mappings can become current")
        if mapping.status is MappingStatus.CONFIRMED and mapping.verified_at is None:
            raise ValueError("confirmed mapping requires verified_at")
        if mapping.last_checked_at is None:
            raise ValueError("current mapping requires last_checked_at")

        destination = self.path_for(mapping.entity_id)
        current = self.get(mapping.entity_id)
        if current is not None:
            if current.mapping_id != mapping.mapping_id:
                raise ValueError("mapping_id cannot change for a current place")
            if (
                current.last_checked_at is not None
                and current.last_checked_at > mapping.last_checked_at
            ):
                raise OlderResolvedMappingError(
                    "older confirmed mapping cannot replace current mapping"
                )
            if current.model_dump(mode="json") == mapping.model_dump(mode="json"):
                return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write(mapping.model_dump_json(indent=2) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination

    def get(self, entity_id: str) -> ExternalEntityMapping | None:
        path = self.path_for(entity_id)
        if not path.exists():
            return None
        return ExternalEntityMapping.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def all(self) -> list[ExternalEntityMapping]:
        if not self.root_directory.exists():
            return []
        return [
            ExternalEntityMapping.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root_directory.glob("*.json"))
        ]

    def path_for(self, entity_id: str) -> Path:
        return self.root_directory / f"{quote(entity_id, safe='-_.')}.json"


def apply_rejected_resolution(
    mapping: ExternalEntityMapping,
    resolution: GoogleMapsMappingResolution,
) -> ExternalEntityMapping:
    """Materialize a hard resolver conflict so future batches skip it."""

    if resolution.status is not MappingResolutionStatus.REJECT:
        raise ValueError("only REJECT resolutions can reject a mapping")
    if resolution.mapping_id != mapping.mapping_id:
        raise ValueError("resolution belongs to another mapping")
    return ExternalEntityMapping.model_validate(
        {
            **mapping.model_dump(mode="python"),
            "status": MappingStatus.REJECTED,
            "confidence": resolution.score,
            "verified_at": None,
            "last_checked_at": resolution.resolved_at,
            "source_record_ids": list(
                dict.fromkeys(
                    [*mapping.source_record_ids, resolution.source_record_id]
                )
            ),
            "attributes": {
                **mapping.attributes,
                "mapping_rejection_reason_codes": list(resolution.reason_codes),
                "mapping_evidence_hash": resolution.evidence_hash,
                "mapping_resolver_version": resolution.resolver_version,
            },
        }
    )
