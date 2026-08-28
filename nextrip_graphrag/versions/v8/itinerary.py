from __future__ import annotations

from typing import Any

from ..v6.itinerary import ItineraryBuilder


class V8ItineraryBuilder(ItineraryBuilder):
    """Build route hints from canonical points without persisted NEAR edges."""

    def _metadata(self, place_ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.store.run_versioned(
            """
            MATCH (place:Place {kb_version: $kb_version})
            WHERE place.id IN $place_ids
            OPTIONAL MATCH (other:Place {kb_version: $kb_version})
            WHERE other.id IN $place_ids
              AND other.id <> place.id
              AND place.location IS NOT NULL
              AND other.location IS NOT NULL
            WITH place, collect(
              CASE
                WHEN other IS NULL THEN null
                ELSE {
                  target_id: other.id,
                  distance_km: point.distance(place.location, other.location) / 1000.0
                }
              END
            ) AS raw_distances
            RETURN place.id AS place_id,
                   place.opening_hours_open AS opening_hours_open,
                   place.opening_hours_close AS opening_hours_close,
                   place.duration_recommendation AS duration_recommendation,
                   [item IN raw_distances WHERE item IS NOT NULL] AS distances
            """,
            place_ids=place_ids,
        )
        return {row["place_id"]: row for row in rows}
