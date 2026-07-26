from __future__ import annotations

from pathlib import Path
from typing import Any

from nextrip_graphrag.api.schemas import TypedQueryRequest
from nextrip_graphrag.config import Settings
from nextrip_graphrag.normalizer import CITY_DEFINITIONS
from nextrip_graphrag.versions.v5.schemas import (
    GeoScope,
    QueryTarget,
    TargetKind,
    V5Intent,
    V5QueryPlan,
)
from nextrip_graphrag.versions.v7.entity_linker import (
    GraphCandidateProvider,
    NodeSelection,
    SemanticEntityLinker,
)
from nextrip_graphrag.versions.v7.query_planner import (
    V7PlannerDraft,
    plan_query,
)
from nextrip_graphrag.versions.v7.retrieval import V7RetrievalService


CATALOG = {
    "cities": ["Đà Nẵng", "Quy Nhơn"],
    "areas": ["Hải Châu", "Nhơn Lý"],
    "categories": ["beach", "historical"],
    "concepts": ["families", "quiet", "sea view"],
    "places": ["Cầu Rồng", "Eo Gió"],
}


class PlannerAI:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def generate_structured(self, _instruction, _prompt, response_schema):
        return response_schema.model_validate(self.payload)


class SelectingAI:
    def __init__(self, selected_id: str | None, confidence: float = 0.9):
        self.selected_id = selected_id
        self.confidence = confidence

    def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2]

    def generate_structured(self, _instruction, _prompt, response_schema):
        assert response_schema is NodeSelection
        return NodeSelection(
            selected_candidate_id=self.selected_id,
            confidence=self.confidence,
        )


class CandidateStore:
    def __init__(self):
        self.settings = Settings(
            v5_concept_link_top_k=5,
            v5_concept_selection_min_confidence=0.75,
        )

    def run_versioned(self, query: str, **_params: Any) -> list[dict[str, Any]]:
        if "fulltext.queryNodes" in query:
            return [
                {
                    "candidate_id": "place:dragon-bridge",
                    "canonical_value": "Cầu Rồng",
                    "city": "Đà Nẵng",
                    "entity_type": "attraction",
                    "score": 0.81,
                }
            ]
        if "vector.queryNodes" in query:
            return [
                {
                    "candidate_id": "place:dragon-bridge",
                    "canonical_value": "Cầu Rồng",
                    "city": "Đà Nẵng",
                    "entity_type": "attraction",
                    "score": 0.93,
                },
                {
                    "candidate_id": "place:thi-nai-bridge",
                    "canonical_value": "Cầu Thị Nại",
                    "city": "Quy Nhơn",
                    "entity_type": "attraction",
                    "score": 0.72,
                },
            ]
        return []

    def semantic_concept_candidates(
        self,
        _embedding: list[float],
        _limit: int,
    ) -> list[dict[str, Any]]:
        return []


class PerturbedScoreStore(CandidateStore):
    def run_versioned(self, query: str, **_params: Any) -> list[dict[str, Any]]:
        if "fulltext.queryNodes" in query:
            return [
                {
                    "candidate_id": "place:dragon-bridge",
                    "canonical_value": "Cáº§u Rá»“ng",
                    "score": 0.1,
                }
            ]
        if "vector.queryNodes" in query:
            return [
                {
                    "candidate_id": "place:thi-nai-bridge",
                    "canonical_value": "Cáº§u Thá»‹ Náº¡i",
                    "score": 10_000.0,
                },
                {
                    "candidate_id": "place:dragon-bridge",
                    "canonical_value": "Cáº§u Rá»“ng",
                    "score": 0.01,
                },
            ]
        return []


class ReadyPlannerStore:
    def __init__(self):
        self.settings = Settings()

    def run(self, _query: str, **_params: Any) -> list[dict[str, Any]]:
        return [{"status": "ready"}]

    def planner_catalog(self) -> dict[str, list[str]]:
        return CATALOG


class HybridPlaceStore:
    settings = Settings()

    def run_versioned(self, query: str, **_params: Any) -> list[dict[str, Any]]:
        if "vector.queryNodes" in query:
            return [
                {
                    "place": {
                        "id": "place:semantic-only",
                        "name": "Semantic only",
                        "city": "Quy NhÆ¡n",
                        "entity_type": "hotel",
                    },
                    "score": 10_000.0,
                },
                {
                    "place": {
                        "id": "place:cross-source",
                        "name": "Cross source",
                        "city": "Quy NhÆ¡n",
                        "entity_type": "hotel",
                    },
                    "score": 0.01,
                },
            ]
        if "fulltext.queryNodes" in query:
            return [
                {
                    "place": {
                        "id": "place:cross-source",
                        "name": "Cross source",
                        "city": "Quy NhÆ¡n",
                        "entity_type": "hotel",
                    },
                    "score": 0.1,
                }
            ]
        return []


class GraphVersionStore:
    settings = Settings()

    def __init__(self):
        self.params: dict[str, Any] = {}

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.params = params
        if "RETURN count(DISTINCT place) AS count" in query:
            return [{"count": 42}]
        return [{"status": "ready"}]


def test_v7_requires_semantic_planner_instead_of_lexical_fallback() -> None:
    plan, planner, failure = plan_query(
        "bất kỳ cách diễn đạt nào",
        None,
        CATALOG,
    )

    assert planner == "planner_unavailable"
    assert failure is not None
    assert failure.code == "planner_unavailable"
    assert plan.intent == V5Intent.UNSUPPORTED
    assert plan.clarification_needed is True


def test_v7_service_exposes_planner_unavailable_without_fallback() -> None:
    response = V7RetrievalService(ReadyPlannerStore(), None).query(
        "Một câu chưa từng xuất hiện trong benchmark.",
    )

    assert response.kb_version == "v7"
    assert response.error is not None
    assert response.error["code"] == "planner_unavailable"
    assert response.error["message"].startswith("The V7 query planner")
    assert response.trace[0]["planner"] == "planner_unavailable"


def test_v7_queries_the_reused_v5_graph_version() -> None:
    store = GraphVersionStore()

    fact = V7RetrievalService(store, None)._count_fact(
        next(iter(CITY_DEFINITIONS)),
        ["attraction"],
    )

    assert fact.value == 42
    assert store.params["kb_version"] == "v5"
    assert V7RetrievalService.kb_version == "v5"
    assert V7RetrievalService.response_model.model_fields["kb_version"].default == "v7"


def test_v7_accepts_llm_semantics_without_phrase_rules() -> None:
    ai = PlannerAI(
        {
            "intent": "recommend",
            "targets": [
                {
                    "kind": "place",
                    "entity_types": ["hotel"],
                }
            ],
            "geo_scope": {"cities": ["thành phố biển miền Trung"]},
            "required_concepts": [
                "phù hợp với trẻ nhỏ",
                "có thể đi bộ ra bờ biển",
            ],
            "preferred_concepts": ["không gian ít ồn"],
            "ranking_criteria": ["rating"],
            "confidence": 0.88,
        }
    )

    plan, planner, failure = plan_query(
        "Kiếm giúp mình nơi ở để tụi nhỏ chạy nhảy, bước vài phút là tới biển.",
        ai,
        CATALOG,
    )

    assert planner == "gemini_semantic"
    assert failure is None
    assert plan.intent == V5Intent.RECOMMEND
    assert plan.targets[0].kind == TargetKind.PLACE
    assert plan.targets[0].entity_types == ["hotel"]
    assert plan.required_concepts == [
        "phù hợp với trẻ nhỏ",
        "có thể đi bộ ra bờ biển",
    ]


def test_v7_does_not_block_actionable_plan_when_llm_marks_clarification() -> None:
    ai = PlannerAI(
        {
            "intent": "recommend",
            "targets": [{"kind": "place", "entity_types": ["attraction"]}],
            "geo_scope": {"cities": ["Quy Nhơn", "Đà Nẵng"]},
            "preferred_concepts": ["cảnh đẹp", "trải nghiệm địa phương"],
            "clarification_needed": True,
            "confidence": 0.8,
        }
    )

    plan, _planner, failure = plan_query("Gợi ý chuyến đi", ai, CATALOG)

    assert failure is None
    assert plan.clarification_needed is False
    assert plan.geo_scope.cities == ["Quy Nhơn", "Đà Nẵng"]


def test_v7_place_grounding_selects_only_from_graph_candidates() -> None:
    linker = SemanticEntityLinker(
        CandidateStore(),
        SelectingAI("place:dragon-bridge"),
    )

    link = linker.link(
        "cây cầu phun lửa",
        "place",
        CATALOG["places"],
        user_query="Tối nay tôi muốn xem cây cầu phun lửa.",
    )

    assert link.resolved_value == "Cầu Rồng"
    assert link.method == "llm_candidate_selection"
    assert link.candidates[0].candidate_id == "place:dragon-bridge"
    assert link.candidates[0].retrieval == {
        "fulltext_rank": 1,
        "fulltext_score": 0.81,
        "vector_rank": 1,
        "vector_score": 0.93,
    }


def test_v7_candidate_fusion_uses_ranks_not_incomparable_raw_scores() -> None:
    candidates = GraphCandidateProvider(
        CandidateStore(),
        SelectingAI(None),
    ).candidates("cÃ¢y cáº§u", "place", CATALOG["places"])

    assert [candidate.candidate_id for candidate in candidates] == [
        "place:dragon-bridge",
        "place:thi-nai-bridge",
    ]
    assert candidates[0].score < 0.1
    assert candidates[0].retrieval["fulltext_score"] == 0.81
    assert candidates[0].retrieval["vector_score"] == 0.93


def test_v7_candidate_fusion_is_stable_when_source_score_scales_change() -> None:
    candidates = GraphCandidateProvider(
        PerturbedScoreStore(),
        SelectingAI(None),
    ).candidates("cÃ¢y cáº§u", "place", CATALOG["places"])

    assert candidates[0].candidate_id == "place:dragon-bridge"
    assert candidates[0].retrieval["fulltext_rank"] == 1
    assert candidates[0].retrieval["vector_rank"] == 2
    assert candidates[1].retrieval["vector_score"] == 10_000.0


def test_v7_recommendation_fallback_uses_hybrid_rank_fusion() -> None:
    service = V7RetrievalService(HybridPlaceStore(), SelectingAI(None))

    candidates = service._place_fallback_candidates(
        "khÃ¡ch sáº¡n cÃ³ mÃ´ táº£ phÃ¹ há»£p",
        [0.1, 0.2],
        None,
        ["hotel"],
        5,
    )

    assert [candidate.place_id for candidate in candidates] == [
        "place:cross-source",
        "place:semantic-only",
    ]
    assert service.place_fallback_strategy == "hybrid_place_rrf_fallback"


def test_v7_rejects_selector_id_outside_candidate_whitelist() -> None:
    linker = SemanticEntityLinker(
        CandidateStore(),
        SelectingAI("place:invented"),
    )

    link = linker.link(
        "cây cầu phun lửa",
        "place",
        CATALOG["places"],
        user_query="Tôi muốn xem cây cầu phun lửa.",
    )

    assert link.resolved_value is None
    assert link.method == "unresolved"


def test_v7_converts_grounded_near_entity_to_typed_constraint() -> None:
    linker = SemanticEntityLinker(CandidateStore(), SelectingAI(None))
    plan = V5QueryPlan(
        intent=V5Intent.RECOMMEND,
        targets=[
            QueryTarget(kind=TargetKind.PLACE, entity_types=["restaurant"])
        ],
        geo_scope=GeoScope(
            cities=["Đà Nẵng"],
            near_entities=["Cầu Rồng"],
        ),
        confidence=0.9,
    )

    result = linker.ground_plan(
        plan,
        CATALOG,
        user_query="Tìm nhà hàng gần Cầu Rồng.",
    )

    assert result.blocked is False
    assert result.plan.geo_scope.near_entities == ["Cầu Rồng"]
    assert any(
        constraint.field == "near_subject"
        and constraint.value == "Cầu Rồng"
        for constraint in result.plan.constraints
    )


def test_v7_source_has_no_lexical_planner_tables_or_query_regex() -> None:
    version_dir = (
        Path(__file__).parents[1]
        / "nextrip_graphrag"
        / "versions"
        / "v7"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in version_dir.glob("*.py")
    )

    assert "planner_lexicon" not in source
    assert "_ALIASES" not in source
    assert "_SUBJECT_PATTERNS" not in source
    assert "\nimport re\n" not in source
    assert "\nfrom re import" not in source


def test_v7_is_exposed_by_typed_api_contract() -> None:
    request = TypedQueryRequest(
        query="Tìm một nơi yên tĩnh.",
        kb_version="v7",
    )

    assert request.kb_version == "v7"


def test_v7_structured_schema_rejects_unknown_entity_type() -> None:
    payload = {
        "intent": "recommend",
        "targets": [
            {
                "kind": "place",
                "entity_types": ["villa_that_does_not_exist"],
            }
        ],
        "confidence": 0.9,
    }

    try:
        V7PlannerDraft.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown entity types must be rejected by the schema")
