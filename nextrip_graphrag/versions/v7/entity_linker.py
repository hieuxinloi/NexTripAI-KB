from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from loguru import logger
from pydantic import BaseModel, Field

from ...config import Settings
from ...normalizer import slugify
from ...retrieval.rank_fusion import fuse_ranked_ids
from ..v2.retrieval import _fulltext_query
from ..v4.schemas import V4Constraint
from ..v5.concept_linker import ConceptLinkBatch, ConceptLinker
from ..v5.schemas import GeoScope, QueryTarget, TargetKind, V5Intent, V5QueryPlan


SELECTOR_INSTRUCTION = """Ground a raw user mention to the supplied graph candidates.
Select one candidate only when it denotes the same entity or graph value in the
full user query. Return null when candidates are ambiguous or unrelated.
Never create, modify, or return an ID outside the candidate list."""

LinkKind = Literal["city", "geo_area", "place", "category"]
LinkMethod = Literal["exact_graph_value", "llm_candidate_selection", "unresolved"]


class NodeCandidate(BaseModel):
    candidate_id: str
    canonical_value: str
    kind: LinkKind
    score: float = 0.0
    city: str | None = None
    entity_type: str | None = None
    retrieval: dict[str, float | int] = Field(default_factory=dict)


class NodeSelection(BaseModel):
    selected_candidate_id: str | None = None
    confidence: float = Field(ge=0, le=1)


class EntityLink(BaseModel):
    input_term: str
    kind: LinkKind
    resolved_value: str | None = None
    method: LinkMethod
    score: float = 0.0
    selector_confidence: float | None = None
    candidates: list[NodeCandidate] = Field(default_factory=list)
    error_type: str | None = None


class PlanGroundingResult(BaseModel):
    plan: V5QueryPlan
    links: list[EntityLink] = Field(default_factory=list)
    concept_links: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    blocked: bool = False


class EntityStore(Protocol):
    settings: Settings

    def run_versioned(
        self,
        query: str,
        **params: Any,
    ) -> Sequence[Mapping[str, object]]: ...

    def semantic_concept_candidates(
        self,
        embedding: list[float],
        limit: int,
    ) -> Sequence[Mapping[str, object]]: ...


class EntityAIClient(Protocol):
    def embed_query(self, query: str) -> list[float]: ...

    def generate_structured(
        self,
        system_instruction: str,
        prompt: str,
        response_schema: type[NodeSelection],
    ) -> NodeSelection: ...


class GraphCandidateProvider:
    """Retrieve a closed candidate set from graph indexes and catalog values."""

    def __init__(self, store: EntityStore, ai_client: EntityAIClient | None):
        self.store = store
        self.ai_client = ai_client
        self.limit = store.settings.v5_concept_link_top_k

    def candidates(
        self,
        term: str,
        kind: LinkKind,
        vocabulary: list[str],
    ) -> list[NodeCandidate]:
        if kind != "place":
            return [
                NodeCandidate(
                    candidate_id=f"{kind}:{index}",
                    canonical_value=value,
                    kind=kind,
                )
                for index, value in enumerate(vocabulary)
            ]
        return self._place_candidates(term)

    def _place_candidates(self, term: str) -> list[NodeCandidate]:
        sources: dict[str, list[NodeCandidate]] = {}
        query_text = _fulltext_query(term)
        if query_text:
            sources["fulltext"] = self._query_places(
                """
                CALL db.index.fulltext.queryNodes(
                  'v5_place_fulltext', $query_text, {limit: $limit}
                )
                YIELD node, score
                WHERE node.kb_version = $kb_version
                RETURN node.id AS candidate_id,
                       node.name AS canonical_value,
                       node.city AS city,
                       node.entity_type AS entity_type,
                       score
                ORDER BY score DESC
                """,
                query_text=query_text,
            )

        if self.ai_client is not None:
            sources["vector"] = self._query_places(
                """
                MATCH (node:Place)
                SEARCH node IN (
                  VECTOR INDEX v5_place_embedding
                  FOR $embedding
                  LIMIT $limit
                )
                SCORE AS score
                WHERE node.kb_version = $kb_version
                RETURN node.id AS candidate_id,
                       node.name AS canonical_value,
                       node.city AS city,
                       node.entity_type AS entity_type,
                       score
                ORDER BY score DESC
                """,
                embedding=self.ai_client.embed_query(term),
            )
        return _fuse_place_candidates(sources, self.limit)

    def _query_places(
        self,
        query: str,
        **params: object,
    ) -> list[NodeCandidate]:
        rows = self.store.run_versioned(query, limit=self.limit, **params)
        return [_place_candidate(row) for row in rows]


class SemanticEntityLinker:
    def __init__(self, store: EntityStore, ai_client: EntityAIClient | None):
        self.store = store
        self.ai_client = ai_client
        self.settings = store.settings
        self.candidate_provider = GraphCandidateProvider(store, ai_client)

    def ground_plan(
        self,
        plan: V5QueryPlan,
        catalog: dict[str, list[str]],
        *,
        user_query: str,
    ) -> PlanGroundingResult:
        links: list[EntityLink] = []
        missing: list[str] = []

        cities, city_links = self._ground_values(
            plan.geo_scope.cities,
            "city",
            catalog.get("cities", []),
            user_query,
        )
        links.extend(city_links)
        missing.extend(
            f"unresolved:city:{link.input_term}"
            for link in city_links
            if link.resolved_value is None
        )

        areas, area_links = self._ground_values(
            plan.geo_scope.areas,
            "geo_area",
            catalog.get("areas", []),
            user_query,
        )
        links.extend(area_links)
        missing.extend(
            f"unresolved:geo_area:{link.input_term}"
            for link in area_links
            if link.resolved_value is None
        )

        near_entities, near_links = self._ground_values(
            plan.geo_scope.near_entities,
            "place",
            catalog.get("places", []),
            user_query,
        )
        links.extend(near_links)
        missing.extend(
            f"unresolved:place:{link.input_term}"
            for link in near_links
            if link.resolved_value is None
        )

        targets: list[QueryTarget] = []
        concept_target_links: list[dict[str, Any]] = []
        for target in plan.targets:
            grounded, target_link, concept_link = self._ground_target(
                target,
                catalog,
                user_query,
            )
            targets.append(grounded)
            if target_link is not None:
                links.append(target_link)
                if target_link.resolved_value is None:
                    missing.append(
                        f"unresolved:{target.kind.value}:{target_link.input_term}"
                    )
            if concept_link is not None:
                concept_target_links.append(concept_link)
                if concept_link["resolved_concept"] is None:
                    missing.append(
                        f"unresolved:{target.kind.value}:{concept_link['input_term']}"
                    )

        constraints, constraint_links, constraint_missing = self._ground_constraints(
            plan.constraints,
            catalog,
            user_query,
        )
        links.extend(constraint_links)
        missing.extend(constraint_missing)
        existing_near_subjects = {
            str(constraint.value)
            for constraint in constraints
            if constraint.field == "near_subject"
        }
        constraints.extend(
            V4Constraint(field="near_subject", value=value)
            for value in near_entities
            if value not in existing_near_subjects
        )

        required = self._ground_concepts(
            plan.required_concepts,
            catalog.get("concepts", []),
            user_query,
        )
        preferred = self._ground_concepts(
            plan.preferred_concepts,
            catalog.get("concepts", []),
            user_query,
        )
        missing.extend(f"concept:{term}" for term in required.unresolved)

        grounded_plan = plan.model_copy(
            update={
                "targets": targets,
                "geo_scope": GeoScope(
                    cities=cities,
                    areas=areas,
                    near_entities=near_entities,
                ),
                "constraints": constraints,
                "required_concepts": required.resolved,
                "preferred_concepts": preferred.resolved,
            }
        )
        # An unresolved named target is a safe clarification state, not a
        # runtime validation failure. V5's plan validator explicitly allows
        # incomplete plans when clarification_needed is true.
        if grounded_plan.intent in {
            V5Intent.LOOKUP,
            V5Intent.PROFILE,
            V5Intent.COMPARE,
        }:
            minimum_named = 2 if grounded_plan.intent == V5Intent.COMPARE else 1
            named_count = sum(1 for target in targets if target.value)
            if named_count < minimum_named:
                grounded_plan = grounded_plan.model_copy(
                    update={"clarification_needed": True}
                )
        concept_links = [
            *concept_target_links,
            *(link.model_dump() for link in required.links),
            *(link.model_dump() for link in preferred.links),
        ]
        return PlanGroundingResult(
            plan=grounded_plan,
            links=links,
            concept_links=concept_links,
            missing_fields=list(dict.fromkeys(missing)),
            blocked=bool(missing),
        )

    def _ground_target(
        self,
        target: QueryTarget,
        catalog: dict[str, list[str]],
        user_query: str,
    ) -> tuple[QueryTarget, EntityLink | None, dict[str, Any] | None]:
        if target.value is None:
            return target, None, None
        if target.kind in {TargetKind.DISH, TargetKind.ACTIVITY, TargetKind.CONCEPT}:
            result = self._ground_concepts(
                [target.value],
                catalog.get("concepts", []),
                user_query,
            )
            resolved = result.resolved[0] if result.resolved else None
            link = result.links[0].model_dump() if result.links else {
                "input_term": target.value,
                "resolved_concept": None,
                "method": "unresolved",
            }
            return target.model_copy(update={"value": resolved}), None, link

        kind: LinkKind = {
            TargetKind.PLACE: "place",
            TargetKind.CITY: "city",
            TargetKind.GEO_AREA: "geo_area",
        }[target.kind]
        vocabulary = {
            "place": catalog.get("places", []),
            "city": catalog.get("cities", []),
            "geo_area": catalog.get("areas", []),
        }[kind]
        link = self.link(target.value, kind, vocabulary, user_query=user_query)
        return target.model_copy(update={"value": link.resolved_value}), link, None

    def _ground_constraints(
        self,
        constraints: list[V4Constraint],
        catalog: dict[str, list[str]],
        user_query: str,
    ) -> tuple[list[V4Constraint], list[EntityLink], list[str]]:
        grounded: list[V4Constraint] = []
        links: list[EntityLink] = []
        missing: list[str] = []
        for constraint in constraints:
            if constraint.field not in {"category", "near_subject"}:
                grounded.append(constraint)
                continue
            kind: LinkKind = (
                "category" if constraint.field == "category" else "place"
            )
            vocabulary = (
                catalog.get("categories", [])
                if kind == "category"
                else catalog.get("places", [])
            )
            link = self.link(
                str(constraint.value),
                kind,
                vocabulary,
                user_query=user_query,
            )
            links.append(link)
            if link.resolved_value is None:
                missing.append(f"unresolved:{kind}:{constraint.value}")
                continue
            grounded.append(
                constraint.model_copy(update={"value": link.resolved_value})
            )
        return grounded, links, missing

    def _ground_values(
        self,
        values: list[str],
        kind: LinkKind,
        vocabulary: list[str],
        user_query: str,
    ) -> tuple[list[str], list[EntityLink]]:
        links = [
            self.link(value, kind, vocabulary, user_query=user_query)
            for value in values
        ]
        resolved = list(
            dict.fromkeys(
                link.resolved_value
                for link in links
                if link.resolved_value is not None
            )
        )
        return resolved, links

    def _ground_concepts(
        self,
        values: list[str],
        vocabulary: list[str],
        user_query: str,
    ) -> ConceptLinkBatch:
        return ConceptLinker(self.store, self.ai_client).link(
            values,
            vocabulary,
            user_query=user_query,
        )

    def link(
        self,
        term: str,
        kind: LinkKind,
        vocabulary: list[str],
        *,
        user_query: str,
    ) -> EntityLink:
        exact = {slugify(value): value for value in vocabulary}.get(slugify(term))
        if exact is not None:
            return EntityLink(
                input_term=term,
                kind=kind,
                resolved_value=exact,
                method="exact_graph_value",
                score=1.0,
            )
        if self.ai_client is None:
            return EntityLink(input_term=term, kind=kind, method="unresolved")

        try:
            candidates = self._candidates(term, kind, vocabulary)
        except Exception as exc:
            logger.warning(
                "V7 entity candidates unavailable kind={} error_type={}",
                kind,
                exc.__class__.__name__,
            )
            return EntityLink(
                input_term=term,
                kind=kind,
                method="unresolved",
                error_type=exc.__class__.__name__,
            )
        if not candidates:
            return EntityLink(
                input_term=term,
                kind=kind,
                method="unresolved",
            )

        try:
            selection = self.ai_client.generate_structured(
                SELECTOR_INSTRUCTION,
                json.dumps(
                    {
                        "user_query": user_query,
                        "raw_mention": term,
                        "candidate_kind": kind,
                        "candidates": [
                            candidate.model_dump() for candidate in candidates
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                NodeSelection,
            )
        except Exception as exc:
            logger.warning(
                "V7 entity selector unavailable kind={} error_type={}",
                kind,
                exc.__class__.__name__,
            )
            return EntityLink(
                input_term=term,
                kind=kind,
                method="unresolved",
                candidates=candidates,
                error_type=exc.__class__.__name__,
            )

        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        selected = (
            by_id.get(selection.selected_candidate_id)
            if selection.selected_candidate_id is not None
            else None
        )
        if (
            selected is None
            or selection.confidence
            < self.settings.v5_concept_selection_min_confidence
        ):
            return EntityLink(
                input_term=term,
                kind=kind,
                method="unresolved",
                candidates=candidates,
                selector_confidence=selection.confidence,
            )
        return EntityLink(
            input_term=term,
            kind=kind,
            resolved_value=selected.canonical_value,
            method="llm_candidate_selection",
            score=selected.score,
            candidates=candidates,
            selector_confidence=selection.confidence,
        )

    def _candidates(
        self,
        term: str,
        kind: LinkKind,
        vocabulary: list[str],
    ) -> list[NodeCandidate]:
        return self.candidate_provider.candidates(term, kind, vocabulary)


def _place_candidate(row: Mapping[str, object]) -> NodeCandidate:
    return NodeCandidate(
        candidate_id=str(row["candidate_id"]),
        canonical_value=str(row["canonical_value"]),
        kind="place",
        score=float(row.get("score") or 0),
        city=str(row["city"]) if row.get("city") else None,
        entity_type=(
            str(row["entity_type"]) if row.get("entity_type") else None
        ),
    )


def _fuse_place_candidates(
    sources: Mapping[str, Sequence[NodeCandidate]],
    limit: int,
) -> list[NodeCandidate]:
    source_candidates = {
        source: {candidate.candidate_id: candidate for candidate in candidates}
        for source, candidates in sources.items()
    }
    by_id = {
        candidate_id: candidate
        for candidates in source_candidates.values()
        for candidate_id, candidate in candidates.items()
    }
    fused = fuse_ranked_ids(
        {
            source: [candidate.candidate_id for candidate in candidates]
            for source, candidates in sources.items()
        }
    )
    results: list[NodeCandidate] = []
    for item in fused[:limit]:
        candidate = by_id[item.item_id]
        retrieval: dict[str, float | int] = {}
        for source, rank in item.ranks.items():
            retrieval[f"{source}_rank"] = rank
            source_candidate = source_candidates[source][item.item_id]
            retrieval[f"{source}_score"] = source_candidate.score
        results.append(
            candidate.model_copy(
                update={"score": item.score, "retrieval": retrieval}
            )
        )
    return results
