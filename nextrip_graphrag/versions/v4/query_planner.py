from __future__ import annotations

from typing import Protocol, TypeVar

from loguru import logger
from pydantic import BaseModel

from ..v2.schemas import QueryIntent
from .planner_models import V4PlannerDraft
from .planner_normalizer import compile_plan
from .planner_prompt import SYSTEM_INSTRUCTION, user_prompt
from .schemas import RetrievalMode, V4QueryPlan


StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class StructuredPlanner(Protocol):
    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[StructuredResult],
    ) -> StructuredResult: ...


def plan_query(
    query: str,
    gemini: StructuredPlanner | None = None,
    concept_vocabulary: list[str] | None = None,
) -> tuple[V4QueryPlan, str, str | None]:
    """Build a validated V4 plan with Gemini, without heuristic intent guessing."""
    if gemini is None:
        return _unavailable_plan(), "planner_unavailable", "GeminiUnavailable"

    try:
        generated = gemini.generate_structured(
            SYSTEM_INSTRUCTION,
            user_prompt(query, concept_vocabulary or []),
            V4PlannerDraft,
        )
        compiled = compile_plan(generated, concept_vocabulary or [])
        logger.info(
            "V4 planner LLM output draft={} compiled_plan={}",
            generated.model_dump_json(),
            compiled.model_dump_json(),
        )
        return compiled, "gemini", None
    except Exception as exc:
        logger.warning(
            "V4 query planner fallback error_type={} query_length={}",
            exc.__class__.__name__,
            len(query),
        )
        return _unavailable_plan(), "planner_fallback", exc.__class__.__name__


def _unavailable_plan() -> V4QueryPlan:
    return V4QueryPlan(
        intent=QueryIntent.UNSUPPORTED,
        retrieval_mode=RetrievalMode.UNSUPPORTED,
        confidence=0,
    )
