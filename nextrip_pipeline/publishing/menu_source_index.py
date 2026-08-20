from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from pydantic import AwareDatetime, Field, HttpUrl

from nextrip_pipeline.schemas import GoogleMapsPlaceObservation, NexTripModel


class GoogleMapsMenuSourceEntry(NexTripModel):
    place_id: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    image_url: HttpUrl
    observed_at: AwareDatetime


class GoogleMapsMenuSourceIndex:
    """Current per-place pointer to a menu image discovered by the place crawl."""

    def __init__(self, root_directory: str | Path) -> None:
        self.root_directory = Path(root_directory)

    def publish(self, observation: GoogleMapsPlaceObservation) -> Path | None:
        source = observation.menu_source
        if source is None or not source.menu_image_urls:
            return None
        entry = GoogleMapsMenuSourceEntry(
            place_id=observation.place_id,
            source_record_id=observation.source_record_id,
            image_url=source.menu_image_urls[0],
            observed_at=observation.observed_at,
        )
        destination = self.path_for(observation.place_id)
        if destination.exists():
            current = GoogleMapsMenuSourceEntry.model_validate_json(
                destination.read_text(encoding="utf-8")
            )
            if current.observed_at >= entry.observed_at:
                return destination
        self._replace(destination, entry.model_dump_json(indent=2) + "\n")
        return destination

    def get(self, place_id: str) -> GoogleMapsMenuSourceEntry | None:
        path = self.path_for(place_id)
        if not path.exists():
            return None
        return GoogleMapsMenuSourceEntry.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def path_for(self, place_id: str) -> Path:
        return self.root_directory / f"{quote(place_id, safe='-_.')}.json"

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
