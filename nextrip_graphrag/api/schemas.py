from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import DEFAULT_SEARCH_TOP_K, DEFAULT_TYPED_QUERY_TOP_K, MAX_TOP_K, MIN_TOP_K
from ..versions.v6.schemas import ConversationContext


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "nextrip-kb"
    neo4j: str
    neo4j_v2: str | None = None
    neo4j_v3: str | None = None
    neo4j_v4: str | None = None
    neo4j_v5: str | None = None
    embedding_model: str
    retrieval_strategies: list[str] = Field(default_factory=list)


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    service: str = "nextrip-kb"
    ready_versions: list[str] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)


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
