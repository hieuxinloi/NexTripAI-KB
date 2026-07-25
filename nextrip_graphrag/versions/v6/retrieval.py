from __future__ import annotations

import re
from dataclasses import asdict
from typing import Iterable

from ...config import DEFAULT_TYPED_QUERY_TOP_K
from ..registry import kb_version_manifests
from ..v2.schemas import EntityResult
from ..v5.retrieval import V5RetrievalService
from ..v5.schemas import V5Intent, V5QueryResponse
from .context import (
    ResolvedTurn,
    is_itinerary_request,
    resolve_turn,
    update_context,
)
from .itinerary import ItineraryBuilder
from .schemas import ConversationContext, V6QueryResponse


class V6RetrievalService(V5RetrievalService):
    """V6 adds explicit conversation state and grounded itinerary scheduling."""

    kb_version = "v5"
    graph_kb_version = "v5"

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

    def query(
        self,
        query: str,
        top_k: int = DEFAULT_TYPED_QUERY_TOP_K,
        context: ConversationContext | None = None,
    ) -> V6QueryResponse:
        previous = context or ConversationContext()
        catalog = self.store.planner_catalog()
        resolved = resolve_turn(query, previous, catalog)
        itinerary_requested = is_itinerary_request(resolved.query)
        response = super().query(_planner_query(resolved.query), max(top_k, 10))
        if itinerary_requested and response.intent != V5Intent.PLAN_CANDIDATES:
            response = self._broad_itinerary_response(
                response,
                previous,
                top_k,
            )
        if (
            response.intent == V5Intent.PLAN_CANDIDATES
            and not itinerary_requested
        ):
            response = _as_recommendation(response)
        response = _inherit_city_scope(response, previous)

        if response.intent == V5Intent.PLAN_CANDIDATES:
            response = self._ensure_itinerary_candidates(response, resolved, top_k)

        itinerary = []
        if response.intent == V5Intent.PLAN_CANDIDATES:
            duration = response.query_plan.duration_days or previous.duration_days or 1
            itinerary = ItineraryBuilder(self.store).build(
                response.recommendations,
                duration,
            )

        current = update_context(
            previous,
            resolved,
            response.query_plan,
            response.recommendations,
        )
        payload = response.model_dump(mode="json")
        payload.update(
            {
                "kb_version": "v6",
                "itinerary": [day.model_dump(mode="json") for day in itinerary],
                "conversation_context": current.model_dump(mode="json"),
                "manifest": asdict(kb_version_manifests()["v6"]),
            }
        )
        payload["trace"].append(
            {
                "step": "v6_post_processing",
                "status": "ok",
                "context_updates": resolved.updates,
                "itinerary_days": len(itinerary),
            }
        )
        return V6QueryResponse.model_validate(payload)

    def query_conversation(
        self,
        turns: Iterable[str],
        top_k: int = DEFAULT_TYPED_QUERY_TOP_K,
    ) -> list[V6QueryResponse]:
        context = ConversationContext()
        responses: list[V6QueryResponse] = []
        for turn in turns:
            response = self.query(turn, top_k=top_k, context=context)
            responses.append(response)
            context = response.conversation_context
        return responses

    def _ensure_itinerary_candidates(
        self,
        response: V5QueryResponse,
        resolved: ResolvedTurn,
        top_k: int,
    ) -> V5QueryResponse:
        duration = response.query_plan.duration_days or 1
        minimum = min(3, duration)
        if len(response.recommendations) >= minimum:
            return response
        cities = response.query_plan.geo_scope.cities
        if not cities:
            return response

        broad_query = f"Lịch trình {duration} ngày ở {cities[0]}"
        broad = super().query(broad_query, max(top_k, 10))
        merged = _merge_recommendations(
            response.recommendations,
            broad.recommendations,
        )
        if len(merged) <= len(response.recommendations):
            return response
        return response.model_copy(
            update={
                "recommendations": merged,
                "missing_fields": [
                    *response.missing_fields,
                    "relaxed_itinerary_preferences",
                ],
                "trace": [
                    *response.trace,
                    {
                        "step": "itinerary_candidate_relaxation",
                        "status": "partial",
                        "source_query": resolved.query,
                        "fallback_query": broad_query,
                    },
                ],
            }
        )

    def _broad_itinerary_response(
        self,
        response: V5QueryResponse,
        context: ConversationContext,
        top_k: int,
    ) -> V5QueryResponse:
        cities = response.query_plan.geo_scope.cities or context.cities
        if not cities:
            return response
        duration = (
            response.query_plan.duration_days
            or context.duration_days
            or 1
        )
        return super().query(
            f"Lịch trình {duration} ngày ở {cities[0]}",
            max(top_k, 10),
        )


def _merge_recommendations(
    primary: list[EntityResult],
    fallback: list[EntityResult],
) -> list[EntityResult]:
    merged: dict[str, EntityResult] = {}
    for item in [*primary, *fallback]:
        merged.setdefault(item.place_id, item)
    return list(merged.values())


def _as_recommendation(response: V5QueryResponse) -> V5QueryResponse:
    plan = response.query_plan.model_copy(update={"intent": V5Intent.RECOMMEND})
    return response.model_copy(
        update={
            "intent": V5Intent.RECOMMEND,
            "query_plan": plan,
        }
    )


def _inherit_city_scope(
    response: V5QueryResponse,
    context: ConversationContext,
) -> V5QueryResponse:
    if response.query_plan.geo_scope.cities or not context.cities:
        return response
    scope = response.query_plan.geo_scope.model_copy(
        update={"cities": context.cities}
    )
    plan = response.query_plan.model_copy(update={"geo_scope": scope})
    return response.model_copy(update={"query_plan": plan})


def _planner_query(query: str) -> str:
    plain = query.casefold()
    exclusion = re.search(
        r"\b(?:không muốn|khong muon|tránh|tranh|loại bỏ|loai bo|bỏ|bo)\b",
        plain,
    )
    if exclusion is None:
        return query
    return re.sub(
        r"\b(?:bar|club|pub|nightlife)\b",
        "",
        query,
        flags=re.IGNORECASE,
    )
