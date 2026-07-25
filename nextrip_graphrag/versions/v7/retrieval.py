from __future__ import annotations

from typing import Any

from ..v5.retrieval import V5RetrievalService
from ..v5.schemas import V5QueryPlan
from .entity_linker import SemanticEntityLinker
from .query_planner import plan_query
from .schemas import V7QueryResponse


class V7RetrievalService(V5RetrievalService):
    """LLM-native semantic planning with graph-grounded entity linking."""

    kb_version = "v7"
    graph_kb_version = "v5"
    manifest_version = "v7"
    response_model = V7QueryResponse

    def _ensure_ready(self) -> None:
        rows = self.store.run(
            """
            MATCH (catalog:TravelCatalog {kb_version: $kb_version})
            RETURN catalog.status AS status
            """,
            kb_version=self.graph_kb_version,
        )
        if not rows or rows[0]["status"] != "ready":
            raise RuntimeError("GraphRAG V5 snapshot is not ready")

    def _plan_query(
        self,
        query: str,
        catalog: dict[str, list[str]],
    ) -> tuple[V5QueryPlan, str, Any | None]:
        return plan_query(query, self.gemini, catalog)

    def _ground_plan(
        self,
        plan: V5QueryPlan,
        catalog: dict[str, list[str]],
        query: str,
    ) -> tuple[V5QueryPlan, list[str], bool, list[dict[str, Any]]]:
        result = SemanticEntityLinker(self.store, self.gemini).ground_plan(
            plan,
            catalog,
            user_query=query,
        )
        trace = {
            "step": "semantic_entity_grounding",
            "status": "blocked" if result.blocked else "ok",
            "links": [link.model_dump() for link in result.links],
            "concept_links": result.concept_links,
            "missing_fields": result.missing_fields,
        }
        return result.plan, result.missing_fields, result.blocked, [trace]


__all__ = ["V7RetrievalService", "V7QueryResponse"]
