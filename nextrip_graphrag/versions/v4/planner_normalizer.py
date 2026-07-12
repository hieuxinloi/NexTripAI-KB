from __future__ import annotations

from collections.abc import Iterable

from ...normalizer import canonical_city
from ..v2.schemas import QueryIntent
from .ontology import canonical_concept
from .planner_models import PlannerConstraintDraft, V4PlannerDraft
from .schemas import RetrievalMode, V4QueryPlan


MODE_INTENTS = {
    RetrievalMode.ENTITY_LOOKUP: QueryIntent.ENTITY_DETAIL,
    RetrievalMode.AGGREGATE: QueryIntent.AGGREGATE_COUNT,
    RetrievalMode.PATH_SEARCH: QueryIntent.ENTITY_LIST,
    RetrievalMode.RECOMMENDATION: QueryIntent.RECOMMENDATION,
    RetrievalMode.COMPARISON: QueryIntent.ENTITY_LIST,
    RetrievalMode.COMMUNITY_SEARCH: QueryIntent.ENTITY_LIST,
    RetrievalMode.DYNAMIC_SEARCH: QueryIntent.UNSUPPORTED,
    RetrievalMode.PLANNING_CANDIDATES: QueryIntent.RECOMMENDATION,
    RetrievalMode.UNSUPPORTED: QueryIntent.UNSUPPORTED,
}


def compile_plan(
    draft: V4PlannerDraft,
    concept_vocabulary: list[str],
) -> V4QueryPlan:
    allowed_concepts = set(concept_vocabulary)
    required_concepts = _validated_concepts(draft.required_concepts, allowed_concepts)
    preferred_concepts = [
        concept
        for concept in _validated_concepts(draft.preferred_concepts, allowed_concepts)
        if concept not in required_concepts
    ]
    return V4QueryPlan.model_validate({
        "intent": MODE_INTENTS[draft.retrieval_mode],
        "city": canonical_city(draft.city) if draft.city else None,
        "subjects": _unique_text(draft.subjects),
        "entity_types": _unique_lower(draft.entity_types),
        "predicates": _unique_lower(draft.predicates),
        "required_concepts": required_concepts,
        "preferred_concepts": preferred_concepts,
        "constraints": _unique_constraints(draft.constraints),
        "retrieval_mode": draft.retrieval_mode,
        "limit": draft.limit,
        "clarification_needed": draft.clarification_needed,
        "confidence": draft.confidence,
    })


def _validated_concepts(values: list[str], allowed: set[str]) -> list[str]:
    concepts = _unique(_resolve_concept(value, allowed) for value in values)
    unknown = set(concepts) - allowed
    if unknown:
        raise ValueError(f"Planner used concepts outside the graph vocabulary: {sorted(unknown)}")
    return concepts


def _resolve_concept(value: str, allowed: set[str]) -> str:
    canonical = canonical_concept(value)
    if canonical in allowed:
        return canonical
    _, separator, suffix = value.partition(":")
    if separator:
        canonical_suffix = canonical_concept(suffix)
        if canonical_suffix in allowed:
            return canonical_suffix
    return canonical


def _unique_lower(values: list[str]) -> list[str]:
    return _unique(value.casefold() for value in values)


def _unique_text(values: list[str]) -> list[str]:
    return _unique(values)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _unique_constraints(values: list[PlannerConstraintDraft]) -> list[dict]:
    unique = {
        (item.field, str(item.value), item.mode): item.model_dump()
        for item in values
    }
    return list(unique.values())
