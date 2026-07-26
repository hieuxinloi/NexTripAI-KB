from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..v5.schemas import V5QueryResponse


class ItinerarySlot(BaseModel):
    order: int = Field(ge=1)
    start_time: str
    end_time: str
    place_id: str
    name: str
    city: str
    entity_type: str
    rationale: str


class ItineraryDay(BaseModel):
    day: int = Field(ge=1)
    slots: list[ItinerarySlot] = Field(default_factory=list)


class ConversationContext(BaseModel):
    turn_count: int = Field(default=0, ge=0)
    cities: list[str] = Field(default_factory=list)
    duration_days: int | None = Field(default=None, ge=1, le=30)
    entity_types: list[str] = Field(default_factory=list)
    previous_intent: str | None = None
    previous_recommendations: list[str] = Field(default_factory=list)
    applied_updates: list[str] = Field(default_factory=list)
    resolved_query: str | None = None


class V6QueryResponse(V5QueryResponse):
    kb_version: Literal["v6"] = "v6"
    itinerary: list[ItineraryDay] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conversation_context: ConversationContext = Field(
        default_factory=ConversationContext
    )
