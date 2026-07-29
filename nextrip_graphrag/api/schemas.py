from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from ..config import DEFAULT_SEARCH_TOP_K, DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ..versions.v6.schemas import ConversationContext


PlaceId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
PreferenceTerm = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]
CityTerm = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
]


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "nextrip-kb"
    neo4j: str
    neo4j_v2: str | None = None
    neo4j_v3: str | None = None
    neo4j_v4: str | None = None
    neo4j_v5: str | None = None
    neo4j_v8: str | None = None
    embedding_model: str
    retrieval_strategies: list[str] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str = "nextrip-kb"
    ready_versions: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    active_version: str | None = None
    previous_version: str | None = None


class KbSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    city: str | None = None
    entity_types: list[str] | None = None
    top_k: int = Field(default=DEFAULT_SEARCH_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    strategy: str = Field(default="v1_provenance", min_length=1)


class SourceInfo(BaseModel):
    name: str | None = None
    url: str | None = None


class GraphContext(BaseModel):
    facets: list[str] = Field(default_factory=list)
    nearby: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    text_unit_id: str | None = None
    text: str | None = None
    sequence: int | None = None
    evidence_origin: str | None = None
    confidence: float | None = None
    match_type: str | None = None
    title: str | None = None
    url: str | None = None
    score: float | None = None


class KbSearchResult(BaseModel):
    place_id: str
    name: str | None = None
    city: str | None = None
    entity_type: str | None = None
    category: str | None = None
    score: float | None = None
    source: SourceInfo = Field(default_factory=SourceInfo)
    graph_context: GraphContext = Field(default_factory=GraphContext)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class KbSearchResponse(BaseModel):
    strategy: str
    results: list[KbSearchResult]
    trace: list[dict[str, Any]] = Field(default_factory=list)


class KbAnswerRequest(KbSearchRequest):
    pass


class KbAnswerResponse(BaseModel):
    answer: str
    strategy: str
    trace: list[dict[str, Any]] = Field(default_factory=list)


class TypedQueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    kb_version: str = Field(default="v2", pattern=r"^v[1-9][0-9]*$")
    top_k: int = Field(default=DEFAULT_TYPED_QUERY_TOP_K, ge=MIN_TOP_K, le=MAX_TOP_K)
    conversation_context: ConversationContext | None = None


class PersonalizedRecommendationRequest(BaseModel):
    kb_version: Literal["v8"] = "v8"
    seed_place_ids: list[PlaceId] = Field(default_factory=list, max_length=20)
    preferred_concepts: list[PreferenceTerm] = Field(default_factory=list, max_length=30)
    excluded_concepts: list[PreferenceTerm] = Field(default_factory=list, max_length=30)
    excluded_place_ids: list[PlaceId] = Field(default_factory=list, max_length=100)
    preferred_cities: list[CityTerm] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=6, ge=MIN_TOP_K, le=12)


class PlaceBatchRequest(BaseModel):
    kb_version: Literal["v8"] = "v8"
    place_ids: list[PlaceId] = Field(min_length=1, max_length=100)


class PersonalizedPlace(BaseModel):
    place_id: str
    name: str
    city: str
    entity_type: str
    category: str | None = None
    score: float = Field(default=0, ge=0, le=1)
    reason_code: Literal[
        "similar_to_recent_place",
        "matches_preference",
        "preferred_city",
        "popular",
    ] = "popular"
    reason: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class PersonalizedRecommendationResponse(BaseModel):
    items: list[PersonalizedPlace] = Field(default_factory=list)
