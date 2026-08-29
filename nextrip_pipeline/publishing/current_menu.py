from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field

from nextrip_pipeline.review import MenuReviewResolution, MenuReviewStatus
from nextrip_pipeline.schemas import NexTripModel


class OlderMenuReviewError(ValueError):
    """Raised when an older approval attempts to replace the current menu."""


class CurrentMenuMetadata(NexTripModel):
    place_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    source_image_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    reviewer: str = Field(min_length=1)
    reviewed_at: AwareDatetime


class CurrentMenuWriter:
    """Publishes only human-approved menu payloads as the current KB projection."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def publish(self, resolution: MenuReviewResolution) -> Path:
        if (
            resolution.status is not MenuReviewStatus.APPROVED
            or resolution.approved_menu is None
        ):
            raise ValueError("only an approved human review can become current")
        place_segment = quote(resolution.place_id, safe="-_.")
        destination = self.root_directory / f"{place_segment}.json"
        metadata_path = self.root_directory / "_audit" / f"{place_segment}.json"
        if metadata_path.exists():
            current = CurrentMenuMetadata.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
            if current.review_id == resolution.review_id:
                return destination
            if current.reviewed_at >= resolution.reviewed_at:
                raise OlderMenuReviewError(
                    "older menu approval cannot replace the current menu"
                )

        metadata = CurrentMenuMetadata(
            place_id=resolution.place_id,
            review_id=resolution.review_id,
            source_image_hash=resolution.source_image_hash,
            reviewer=resolution.reviewer,
            reviewed_at=resolution.reviewed_at,
        )
        self._replace(
            destination,
            resolution.approved_menu.model_dump_json(indent=2) + "\n",
        )
        self._replace(metadata_path, metadata.model_dump_json(indent=2) + "\n")
        return destination

    @staticmethod
    def _replace(destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
