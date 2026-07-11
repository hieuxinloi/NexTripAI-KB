from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "nextrip-kb"
    neo4j: str
    neo4j_v2: str | None = None
    neo4j_v3: str | None = None
    embedding_model: str
    retrieval_strategies: list[str] = Field(default_factory=list)


class KbSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    city: str | None = None
    entity_types: list[str] | None = None
    top_k: int = Field(default=8, ge=1, le=30)
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


class V2QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    kb_version: Literal["v2", "v3"] = "v2"
    top_k: int = Field(default=5, ge=1, le=30)
