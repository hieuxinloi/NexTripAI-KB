from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from loguru import logger
from pydantic import BaseModel, Field

from ...config import Settings
from ...normalizer import slugify


CONCEPT_SELECTOR_INSTRUCTION = """You link a user meaning to an existing knowledge-graph concept.
Select exactly one candidate only when it preserves the meaning in the full user
query. Otherwise select null. Never create or alter a concept ID."""

RELATED_CONCEPT_SELECTOR_INSTRUCTION = """You expand a user preference into existing
knowledge-graph concepts. Select every candidate that directly preserves a useful
aspect of the preference in the full user query. Reject merely correlated concepts.
Return only candidate IDs and never create or alter an ID."""

ConceptLinkMethod = Literal[
    "exact",
    "semantic_embedding",
    "llm_candidate_selection",
    "unresolved",
]


class ConceptCandidate(BaseModel):
    concept_id: str
    canonical_name: str
    name: str
    concept_type: str
    domain: str
    score: float
    semantic_text: str = ""


class ConceptLink(BaseModel):
    input_term: str
    resolved_concept: str | None = None
    method: ConceptLinkMethod
    score: float = 0.0
    candidates: list[ConceptCandidate] = Field(default_factory=list)
    error_type: str | None = None
    selector_confidence: float | None = None


class ConceptLinkBatch(BaseModel):
    resolved: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    links: list[ConceptLink] = Field(default_factory=list)


class ConceptSelection(BaseModel):
    selected_concept_id: str | None = None
    confidence: float = Field(ge=0, le=1)


class RelatedConceptSelection(BaseModel):
    selected_concept_ids: list[str] = Field(default_factory=list)


class ConceptStore(Protocol):
    settings: Settings

    def semantic_concept_candidates(
        self,
        embedding: list[float],
        limit: int,
    ) -> Sequence[Mapping[str, object]]: ...


class ConceptAIClient(Protocol):
    def embed_query(self, query: str) -> list[float]: ...

    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[ConceptSelection],
    ) -> ConceptSelection: ...


class ConceptLinker:
    def __init__(self, store: ConceptStore, ai_client: ConceptAIClient | None):
        self.store = store
        self.ai_client = ai_client
        self.settings = store.settings

    def link(
        self,
        terms: list[str],
        vocabulary: list[str],
        *,
        user_query: str = "",
    ) -> ConceptLinkBatch:
        by_slug = {slugify(value): value for value in vocabulary}
        links = [self._link_term(term, by_slug, user_query) for term in terms]
        resolved = list(
            dict.fromkeys(
                link.resolved_concept
                for link in links
                if link.resolved_concept is not None
            )
        )
        unresolved = [link.input_term for link in links if link.resolved_concept is None]
        return ConceptLinkBatch(resolved=resolved, unresolved=unresolved, links=links)

    def link_related(
        self,
        terms: list[str],
        vocabulary: list[str],
        *,
        user_query: str = "",
    ) -> ConceptLinkBatch:
        """Link preference terms, allowing one preference to span related concepts."""
        by_slug = {slugify(value): value for value in vocabulary}
        batch = ConceptLinkBatch(
            links=[
                self._link_term(
                    term,
                    by_slug,
                    user_query,
                    select_ambiguous=False,
                )
                for term in terms
            ]
        )
        links: list[ConceptLink] = []
        for link in batch.links:
            if link.resolved_concept is not None or not link.candidates:
                links.append(link)
                continue
            selected = self._select_related_candidates(
                link.input_term,
                user_query,
                link.candidates,
            )
            if not selected:
                links.append(link)
                continue
            links.extend(
                ConceptLink(
                    input_term=link.input_term,
                    resolved_concept=candidate.canonical_name,
                    method="llm_candidate_selection",
                    score=candidate.score,
                    candidates=link.candidates,
                )
                for candidate in selected
            )
        resolved = list(
            dict.fromkeys(
                link.resolved_concept
                for link in links
                if link.resolved_concept is not None
            )
        )
        unresolved = list(
            dict.fromkeys(
                link.input_term for link in links if link.resolved_concept is None
            )
        )
        return ConceptLinkBatch(resolved=resolved, unresolved=unresolved, links=links)

    def _link_term(
        self,
        term: str,
        by_slug: dict[str, str],
        user_query: str,
        *,
        select_ambiguous: bool = True,
    ) -> ConceptLink:
        exact = by_slug.get(slugify(term))
        if exact is not None:
            return ConceptLink(
                input_term=term,
                resolved_concept=exact,
                method="exact",
                score=1.0,
            )
        if self.ai_client is None:
            return ConceptLink(input_term=term, method="unresolved")
        try:
            embedding = self.ai_client.embed_query(term)
            rows = self.store.semantic_concept_candidates(
                embedding,
                self.settings.v5_concept_link_top_k,
            )
        except Exception as exc:
            logger.warning(
                "V5 concept linker unavailable term_length={} error_type={}",
                len(term),
                exc.__class__.__name__,
            )
            return ConceptLink(
                input_term=term,
                method="unresolved",
                error_type=exc.__class__.__name__,
            )
        candidates = [ConceptCandidate.model_validate(row) for row in rows]
        if not candidates or candidates[0].score < self.settings.v5_concept_link_min_score:
            return ConceptLink(
                input_term=term,
                method="unresolved",
                candidates=candidates,
            )
        best = _clear_vector_match(
            candidates,
            self.settings.v5_concept_link_min_margin,
        )
        if best is not None:
            return ConceptLink(
                input_term=term,
                resolved_concept=best.canonical_name,
                method="semantic_embedding",
                score=best.score,
                candidates=candidates,
            )
        if not select_ambiguous:
            return ConceptLink(
                input_term=term,
                method="unresolved",
                candidates=candidates,
            )
        selected = self._select_candidate(term, user_query, candidates)
        if selected is None:
            return ConceptLink(
                input_term=term,
                method="unresolved",
                candidates=candidates,
            )
        best, selector_confidence = selected
        return ConceptLink(
            input_term=term,
            resolved_concept=best.canonical_name,
            method="llm_candidate_selection",
            score=best.score,
            candidates=candidates,
            selector_confidence=selector_confidence,
        )

    def _select_candidate(
        self,
        term: str,
        user_query: str,
        candidates: list[ConceptCandidate],
    ) -> tuple[ConceptCandidate, float] | None:
        try:
            selection = self.ai_client.generate_structured(
                CONCEPT_SELECTOR_INSTRUCTION,
                json.dumps(
                    {
                        "user_query": user_query,
                        "semantic_term": term,
                        "candidates": [candidate.model_dump() for candidate in candidates],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                ConceptSelection,
            )
        except Exception as exc:
            logger.warning(
                "V5 concept selector unavailable term_length={} error_type={}",
                len(term),
                exc.__class__.__name__,
            )
            return None
        if (
            selection.selected_concept_id is None
            or selection.confidence < self.settings.v5_concept_selection_min_confidence
        ):
            return None
        by_id = {candidate.concept_id: candidate for candidate in candidates}
        candidate = by_id.get(selection.selected_concept_id)
        if candidate is None:
            logger.warning("V5 concept selector returned an ID outside the candidate whitelist")
            return None
        return candidate, selection.confidence

    def _select_related_candidates(
        self,
        term: str,
        user_query: str,
        candidates: list[ConceptCandidate],
    ) -> list[ConceptCandidate]:
        if self.ai_client is None:
            return []
        try:
            selection = self.ai_client.generate_structured(
                RELATED_CONCEPT_SELECTOR_INSTRUCTION,
                json.dumps(
                    {
                        "user_query": user_query,
                        "preference": term,
                        "candidates": [candidate.model_dump() for candidate in candidates],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                RelatedConceptSelection,
            )
        except Exception as exc:
            logger.warning(
                "V5 related concept selector unavailable term_length={} error_type={}",
                len(term),
                exc.__class__.__name__,
            )
            return []
        by_id = {candidate.concept_id: candidate for candidate in candidates}
        return [
            by_id[concept_id]
            for concept_id in dict.fromkeys(selection.selected_concept_ids)
            if concept_id in by_id
        ]


def _clear_vector_match(
    candidates: list[ConceptCandidate],
    minimum_margin: float,
) -> ConceptCandidate | None:
    if len(candidates) == 1:
        return candidates[0]
    if candidates[0].score - candidates[1].score >= minimum_margin:
        return candidates[0]
    return None
