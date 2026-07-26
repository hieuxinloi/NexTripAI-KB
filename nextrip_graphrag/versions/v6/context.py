from __future__ import annotations

import re
from dataclasses import dataclass

from ...normalizer import slugify
from ..v2.schemas import EntityResult
from ..v5.schemas import V5QueryPlan
from .schemas import ConversationContext


ENTITY_LABELS = {
    "attraction": "điểm tham quan",
    "cafe": "cafe",
    "hotel": "khách sạn",
    "nightlife": "nightlife",
    "restaurant": "nhà hàng",
}


@dataclass(frozen=True)
class ResolvedTurn:
    query: str
    updates: list[str]
    planner_query: str | None = None


def resolve_turn(
    query: str,
    context: ConversationContext,
    catalog: dict[str, list[str]],
) -> ResolvedTurn:
    """Resolve omitted thread state into a standalone, auditable query."""
    if context.turn_count == 0:
        return ResolvedTurn(query=query, updates=[])

    plain = _plain(query)
    parts = [query.strip()]
    updates: list[str] = []

    cities = _catalog_mentions(query, catalog["cities"])
    if not cities and context.cities:
        parts.append(f"ở {context.cities[0]}")
        updates.append("inherited_city")

    duration = _duration_days(plain)
    if _adds_one_day(plain) and context.duration_days is not None:
        duration = context.duration_days + 1
        updates.append("incremented_duration")
    elif duration is None and context.duration_days is not None:
        duration = context.duration_days
        updates.append("inherited_duration")
    if duration is not None and _duration_days(_plain(" ".join(parts))) is None:
        parts.append(f"{duration} ngày")

    if not _has_entity_type(plain) and context.entity_types:
        labels = [
            ENTITY_LABELS[entity_type]
            for entity_type in context.entity_types
            if entity_type in ENTITY_LABELS
        ]
        if labels:
            parts.append(" ".join(labels))
            updates.append("inherited_entity_types")

    if (
        context.previous_intent == "plan_candidates"
        and not _has_itinerary_signal(plain)
    ):
        parts.append("cập nhật lịch trình")
        updates.append("continued_itinerary")

    ordinal = _ordinal_reference(plain)
    if ordinal is not None and len(context.previous_recommendations) >= ordinal:
        parts.append(context.previous_recommendations[ordinal - 1])
        updates.append("resolved_prior_recommendation")

    if _mentions_rain(plain) and "trong nha" not in plain:
        parts.append("ưu tiên trong nhà")
        updates.append("applied_rain_constraint")

    return ResolvedTurn(query=". ".join(parts), updates=updates)


def is_itinerary_request(query: str) -> bool:
    plain = _plain(query)
    if _has_itinerary_signal(plain):
        return True
    if "xen ke" in plain:
        return True
    return _duration_days(plain) is not None and not _has_entity_type(plain)


def duration_days_from_query(query: str) -> int | None:
    """Extract the trip duration using the shared conversation normalizer."""

    return _duration_days(_plain(query))


def update_context(
    previous: ConversationContext,
    resolved: ResolvedTurn,
    plan: V5QueryPlan,
    recommendations: list[EntityResult],
) -> ConversationContext:
    cities = plan.geo_scope.cities or previous.cities
    duration = plan.duration_days
    if duration is None:
        duration = previous.duration_days
    entity_types = [
        entity_type
        for target in plan.targets
        for entity_type in target.entity_types
    ] or previous.entity_types
    recommendation_names = [item.name for item in recommendations]
    return ConversationContext(
        turn_count=previous.turn_count + 1,
        cities=_unique(cities),
        duration_days=duration,
        entity_types=_unique(entity_types),
        previous_intent=plan.intent.value,
        previous_recommendations=(
            recommendation_names or previous.previous_recommendations
        ),
        applied_updates=_unique([*previous.applied_updates, *resolved.updates]),
        resolved_query=resolved.query,
        city_source=previous.city_source,
    )


def _catalog_mentions(query: str, values: list[str]) -> list[str]:
    query_slug = f"-{slugify(query)}-"
    return [
        value
        for value in values
        if f"-{slugify(value)}-" in query_slug
    ]


def _plain(value: str) -> str:
    return slugify(value).replace("-", " ")


def _duration_days(plain: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\s*(?:ngay|n)\b", plain)
    return int(match.group(1)) if match else None


def _adds_one_day(plain: str) -> bool:
    return "them mot ngay" in plain or "them 1 ngay" in plain


def _has_entity_type(plain: str) -> bool:
    signals = (
        "diem tham quan",
        "cafe",
        "ca phe",
        "khach san",
        "hotel",
        "nightlife",
        "nha hang",
        "quan an",
    )
    return any(signal in plain for signal in signals)


def _has_itinerary_signal(plain: str) -> bool:
    return any(
        signal in plain
        for signal in ("lich trinh", "lo trinh", "ke hoach", "sap xep", "xep lich")
    )


def _ordinal_reference(plain: str) -> int | None:
    match = re.search(r"\b(?:phuong an|cho|noi|dia diem)\s+thu\s+(\d+)\b", plain)
    if match:
        return int(match.group(1))
    words = {"nhat": 1, "hai": 2, "ba": 3}
    for word, value in words.items():
        if f" thu {word}" in f" {plain}":
            return value
    return None


def _mentions_rain(plain: str) -> bool:
    return "troi mua" in plain or "ngay mua" in plain


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
