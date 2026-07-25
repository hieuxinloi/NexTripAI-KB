from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Protocol

from ..v2.schemas import EntityResult
from .schemas import ItineraryDay, ItinerarySlot


DEFAULT_DURATION_MINUTES = 90
DAY_START = "09:00"
DAY_END = "20:00"
MAX_ACTIVITIES_PER_DAY = 3


class ItineraryStore(Protocol):
    def run_versioned(self, query: str, **params: Any) -> list[dict[str, Any]]: ...


class ItineraryBuilder:
    """Create a deterministic schedule from graph-grounded recommendations."""

    def __init__(self, store: ItineraryStore):
        self.store = store

    def build(
        self,
        recommendations: list[EntityResult],
        duration_days: int,
    ) -> list[ItineraryDay]:
        candidates = _unique_places(recommendations)
        if not candidates:
            return []

        metadata = self._metadata([item.place_id for item in candidates])
        buckets = _distribute(candidates, duration_days)
        return [
            ItineraryDay(
                day=day_number,
                slots=self._schedule_day(items, metadata),
            )
            for day_number, items in enumerate(buckets, start=1)
        ]

    def _metadata(self, place_ids: list[str]) -> dict[str, dict[str, Any]]:
        rows = self.store.run_versioned(
            """
            MATCH (place:Place {kb_version: $kb_version})
            WHERE place.id IN $place_ids
            RETURN place.id AS place_id,
                   place.opening_hours_open AS opening_hours_open,
                   place.opening_hours_close AS opening_hours_close,
                   place.duration_recommendation AS duration_recommendation
            """,
            place_ids=place_ids,
        )
        return {row["place_id"]: row for row in rows}

    def _schedule_day(
        self,
        places: list[EntityResult],
        metadata: dict[str, dict[str, Any]],
    ) -> list[ItinerarySlot]:
        current = _clock(DAY_START)
        end_of_day = _clock(DAY_END)
        slots: list[ItinerarySlot] = []
        for place in _meal_aware_order(places):
            details = metadata.get(place.place_id, {})
            opening = _opening_window(
                details.get("opening_hours_open"),
                details.get("opening_hours_close"),
            )
            if opening is not None:
                current = max(current, opening[0])
            duration = _duration_minutes(details.get("duration_recommendation"))
            end = current + timedelta(minutes=duration)
            if end > end_of_day:
                break
            if opening is not None and end > opening[1]:
                continue
            slots.append(
                ItinerarySlot(
                    order=len(slots) + 1,
                    start_time=current.strftime("%H:%M"),
                    end_time=end.strftime("%H:%M"),
                    place_id=place.place_id,
                    name=place.name,
                    city=place.city,
                    entity_type=place.entity_type,
                    rationale=_rationale(place),
                )
            )
            current = end + timedelta(minutes=30)
        return slots


def _unique_places(items: list[EntityResult]) -> list[EntityResult]:
    unique: dict[str, EntityResult] = {}
    for item in items:
        unique.setdefault(item.place_id, item)
    return list(unique.values())


def _distribute(
    candidates: list[EntityResult],
    duration_days: int,
) -> list[list[EntityResult]]:
    day_count = max(1, duration_days)
    buckets = [[] for _ in range(day_count)]
    capacity = day_count * MAX_ACTIVITIES_PER_DAY
    for index, candidate in enumerate(candidates[:capacity]):
        buckets[index % day_count].append(candidate)
    return buckets


def _meal_aware_order(places: list[EntityResult]) -> list[EntityResult]:
    priority = {
        "attraction": 0,
        "cafe": 1,
        "restaurant": 2,
        "hotel": 3,
        "nightlife": 4,
    }
    return sorted(places, key=lambda item: priority.get(item.entity_type, 5))


def _opening_window(
    opens_at: Any,
    closes_at: Any,
) -> tuple[datetime, datetime] | None:
    if not isinstance(opens_at, str) or not isinstance(closes_at, str):
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}", opens_at):
        return None
    if not re.fullmatch(r"\d{1,2}:\d{2}", closes_at):
        return None
    end = "23:59" if closes_at in {"24:00", "00:00"} else closes_at
    start_time = _clock(opens_at)
    end_time = _clock(end)
    if end_time <= start_time:
        end_time = _clock("23:59")
    return start_time, end_time


def _duration_minutes(value: Any) -> int:
    if not isinstance(value, str):
        return DEFAULT_DURATION_MINUTES
    numbers = [int(item) for item in re.findall(r"\d+", value)]
    if not numbers:
        return DEFAULT_DURATION_MINUTES
    if "phút" in value.casefold():
        return max(30, min(numbers[0], 240))
    return max(30, min(numbers[0] * 60, 240))


def _clock(value: str) -> datetime:
    return datetime.strptime(value, "%H:%M")


def _rationale(place: EntityResult) -> str:
    category = f", nhóm {place.category}" if place.category else ""
    return f"Ứng viên được truy xuất từ graph cho {place.entity_type}{category}."
