from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from ..v5.schemas import V5QueryPlan
from ..v6.schemas import V6QueryResponse


class MentionRole(StrEnum):
    TARGET = "target"
    SCOPE = "scope"
    NEAR_ANCHOR = "near_anchor"
    CATEGORY = "category"
    PREFERENCE = "preference"


class MentionKind(StrEnum):
    VENUE_NAME = "venue_name"
    BRAND_NAME = "brand_name"
    VENUE_OR_BRAND = "venue_or_brand"
    CITY = "city"
    GEO_AREA = "geo_area"
    CATEGORY = "category"
    CONCEPT = "concept"


class EntityMention(BaseModel):
    """A contiguous semantic span extracted before intent compilation."""

    surface: str = Field(min_length=1)
    role: MentionRole
    kind: MentionKind
    confidence: float = Field(default=0.0, ge=0, le=1)


class RouteTravelMode(StrEnum):
    CAR = "car"
    MOTORBIKE = "motorbike"
    BICYCLE = "bicycle"
    WALKING = "walking"


class RouteOptions(BaseModel):
    travel_mode: RouteTravelMode = RouteTravelMode.CAR
    speed_kmh: float | None = Field(default=None, gt=0, le=200)


class RouteEndpoint(BaseModel):
    place_id: str
    name: str
    latitude: float
    longitude: float


class RouteContext(BaseModel):
    endpoints: list[RouteEndpoint] = Field(default_factory=list, max_length=2)
    options: RouteOptions = Field(default_factory=RouteOptions)


class V8QueryPlan(V5QueryPlan):
    entity_mentions: list[EntityMention] = Field(default_factory=list)
    route_options: RouteOptions = Field(default_factory=RouteOptions)


class V8QueryResponse(V6QueryResponse):
    """V6's stateful response with the V8 manifest and retrieval trace."""

    kb_version: Literal["v8"] = "v8"
    query_plan: V8QueryPlan
    route_context: RouteContext | None = None
