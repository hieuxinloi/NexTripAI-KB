from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nextrip_graphrag.evaluation.v2_runner import _response_operation
from nextrip_graphrag.versions.v2.schemas import EntityResult, FactResult, QueryIntent
from nextrip_graphrag.versions.v4.extraction import DescriptionExtractor
from nextrip_graphrag.versions.v4.ontology import deterministic_description_claims
from nextrip_graphrag.versions.v4.query_planner import plan_query
from nextrip_graphrag.versions.v4.retrieval import V4RetrievalService, _balanced_results
from nextrip_graphrag.versions.v4.schemas import (
    ClaimPolarity,
    DynamicObservationInput,
    RetrievalMode,
    V4QueryPlan,
)


class FakeGemini:
    def __init__(self, payload: dict | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def generate_structured(self, instruction, prompt, schema):
        self.calls.append((instruction, prompt))
        if self.error is not None:
            raise self.error
        return schema.model_validate(self.payload)


def recommendation_plan(**overrides) -> dict:
    payload = {
        "intent": "recommendation",
        "city": "Quy Nhơn",
        "entity_types": ["restaurant"],
        "required_concepts": ["Hải sản"],
        "preferred_concepts": ["Gia đình"],
        "retrieval_mode": "recommendation",
        "limit": 5,
        "confidence": 0.96,
    }
    payload.update(overrides)
    return payload


CONCEPT_VOCABULARY = ["families", "seafood"]


def test_description_extraction_preserves_negative_amenity_claim() -> None:
    description = "Khach san khong lap dat he thong be boi."

    claims = deterministic_description_claims(description)

    assert len(claims) == 1
    assert claims[0].predicate == "HAS_AMENITY"
    assert claims[0].object_name == "ho boi"
    assert claims[0].polarity == ClaimPolarity.NEGATIVE
    assert claims[0].evidence_text in description


def test_description_extractor_combines_structured_and_text_claims() -> None:
    place = {
        "id": "rest_qn_test",
        "entity_type": "restaurant",
        "props": {
            "description": "Nha hang seafood phuc vu cocktail va co khong gian soi dong.",
            "cuisine": ["seafood"],
        },
        "terms": {"cuisine": ["seafood"]},
    }

    extraction = DescriptionExtractor().extract(place)

    predicates = {claim.predicate for claim in extraction.claims}
    assert {"SERVES_CUISINE", "HAS_DRINK_OFFERING", "HAS_AMBIENCE"} <= predicates
    cuisine = next(claim for claim in extraction.claims if claim.predicate == "SERVES_CUISINE")
    assert cuisine.subject_scope == "offering"


def test_v4_rejects_unsupported_structured_cuisine() -> None:
    place = {
        "id": "rest_qn_bad",
        "entity_type": "restaurant",
        "props": {
            "name": "Korean BBQ",
            "description": "Chuyen thit nuong Han Quoc.",
            "category": "bbq",
        },
        "terms": {"cuisine": ["seafood", "korean", "bbq"]},
    }

    extraction = DescriptionExtractor().extract(place)

    cuisines = {
        claim.object_name
        for claim in extraction.claims
        if claim.predicate == "SERVES_CUISINE"
    }
    assert cuisines == {"korean", "bbq"}


def test_v4_normalizes_description_and_structured_concept_aliases() -> None:
    place = {
        "id": "rest_qn_family",
        "entity_type": "restaurant",
        "props": {"description": "Khong gian phu hop cho gia dinh."},
        "terms": {"suitable_for": ["families"]},
    }

    extraction = DescriptionExtractor().extract(place)

    family_concepts = [
        concept for concept in extraction.concepts
        if concept.concept_type.value == "Audience"
    ]
    assert len(family_concepts) == 1
    assert family_concepts[0].canonical_name == "families"


def test_v4_uses_gemini_plan_and_canonicalizes_with_graph_ontology() -> None:
    gemini = FakeGemini(recommendation_plan(
        entity_types=["restaurant", "restaurant"],
        required_concepts=["Hải sản", "hai san"],
        preferred_concepts=["Gia đình", "Hải sản"],
    ))

    plan, planner, fallback_reason = plan_query(
        "Gợi ý nhà hàng hải sản cho gia đình ở Quy Nhơn",
        gemini,
        CONCEPT_VOCABULARY,
    )

    assert planner == "gemini"
    assert fallback_reason is None
    assert plan.entity_types == ["restaurant"]
    assert plan.required_concepts == ["seafood"]
    assert plan.preferred_concepts == ["families"]
    assert "<user_request>" in gemini.calls[0][1]


def test_v4_preserves_multi_type_agent_plan() -> None:
    gemini = FakeGemini(recommendation_plan(
        entity_types=["cafe", "hotel"],
        required_concepts=[],
        preferred_concepts=[],
    ))

    plan, planner, _ = plan_query(
        "Tôi muốn đi cafe và ở khách sạn tại Quy Nhơn",
        gemini,
        CONCEPT_VOCABULARY,
    )

    assert planner == "gemini"
    assert plan.entity_types == ["cafe", "hotel"]
    assert plan.subjects == []


def test_v4_moves_ranking_signals_out_of_graph_concepts() -> None:
    gemini = FakeGemini(recommendation_plan(
        required_concepts=[],
        preferred_concepts=["rating"],
        ranking_criteria=["popularity"],
        limit=6,
    ))

    plan, planner, fallback_reason = plan_query(
        "Cho toi top 6 nha hang o Da Nang",
        gemini,
        CONCEPT_VOCABULARY,
    )

    assert planner == "gemini"
    assert fallback_reason is None
    assert plan.preferred_concepts == []
    assert [criterion.value for criterion in plan.ranking_criteria] == [
        "popularity",
        "rating",
    ]
    assert plan.limit == 6


def test_v4_returns_safe_unsupported_plan_without_gemini() -> None:
    plan, planner, fallback_reason = plan_query("Một câu hỏi bất kỳ")

    assert planner == "planner_unavailable"
    assert fallback_reason == "GeminiUnavailable"
    assert plan.intent == QueryIntent.UNSUPPORTED
    assert plan.retrieval_mode == RetrievalMode.UNSUPPORTED


def test_v4_returns_safe_fallback_when_agent_output_is_invalid() -> None:
    gemini = FakeGemini(recommendation_plan(city="Huế"))

    plan, planner, fallback_reason = plan_query(
        "Gợi ý ở Huế",
        gemini,
        CONCEPT_VOCABULARY,
    )

    assert planner == "planner_fallback"
    assert fallback_reason == "ValueError"
    assert plan.retrieval_mode == RetrievalMode.UNSUPPORTED


def test_v4_returns_safe_fallback_when_gemini_fails() -> None:
    plan, planner, fallback_reason = plan_query(
        "Gợi ý ở Quy Nhơn",
        FakeGemini(error=TimeoutError()),
    )

    assert planner == "planner_fallback"
    assert fallback_reason == "TimeoutError"
    assert plan.retrieval_mode == RetrievalMode.UNSUPPORTED


def test_v4_plan_rejects_lookup_without_predicates() -> None:
    with pytest.raises(ValidationError, match="requires detail intent, subject and predicates"):
        V4QueryPlan.model_validate({
            "intent": "entity_detail",
            "subjects": ["Bãi biển Mỹ Khê"],
            "entity_types": ["attraction"],
            "retrieval_mode": "entity_lookup",
            "confidence": 0.9,
        })


def test_v4_plan_accepts_open_24h_as_typed_constraint() -> None:
    plan = V4QueryPlan.model_validate(recommendation_plan(
        entity_types=["cafe"],
        required_concepts=[],
        preferred_concepts=[],
        constraints=[{"field": "open_24h", "value": True, "mode": "hard"}],
    ))

    assert plan.constraints[0].field == "open_24h"


def test_v4_query_plan_maps_to_benchmark_operation() -> None:
    plan = V4QueryPlan.model_validate({
        "intent": "aggregate_count",
        "city": "Quy Nhơn",
        "entity_types": ["restaurant"],
        "retrieval_mode": "aggregate",
        "confidence": 0.99,
    })

    assert _response_operation(plan) == "count"


def test_v4_balances_explicit_multi_type_results() -> None:
    candidates = [
        EntityResult(
            place_id=f"cafe-{index}",
            name=f"Cafe {index}",
            city="Quy Nhơn",
            entity_type="cafe",
            score=1 - index / 10,
        )
        for index in range(4)
    ] + [
        EntityResult(
            place_id=f"hotel-{index}",
            name=f"Hotel {index}",
            city="Quy Nhơn",
            entity_type="hotel",
            score=0.5 - index / 10,
        )
        for index in range(2)
    ]

    results = _balanced_results(candidates, ["cafe", "hotel"], 4)

    assert [item.entity_type for item in results] == ["cafe", "hotel", "cafe", "hotel"]


def test_v4_multi_entity_lookup_returns_every_subject_in_order() -> None:
    class BatchLookupService(V4RetrievalService):
        def __init__(self):
            pass

        def _lookup_v4(self, subject, predicates, entity_types, required_concepts, *, city=None):
            entity = EntityResult(
                place_id=f"id-{subject}",
                name=subject,
                city=city or "Quy Nhơn",
                entity_type=entity_types[0],
            )
            fact = FactResult(
                fact_id=f"fact-{subject}",
                subject_id=entity.place_id,
                predicate=predicates[0],
                value=f"address-{subject}",
                value_type="string",
                confidence=1,
            )
            return [entity], [fact]

    results = BatchLookupService()._lookup_subjects(
        ["A", "B"],
        ["address"],
        ["restaurant"],
        [],
        city="Quy Nhơn",
    )

    assert [subject for subject, _, _ in results] == ["A", "B"]
    assert [entities[0].place_id for _, entities, _ in results] == ["id-A", "id-B"]
    assert [facts[0].subject_id for _, _, facts in results] == ["id-A", "id-B"]


def test_dynamic_observation_requires_valid_aware_time_window() -> None:
    observed_at = datetime.now(timezone.utc)
    observation = DynamicObservationInput(
        subject_id="attr_qn_001",
        observation_type="weather",
        value={"condition": "rain"},
        source="test",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(hours=1),
    )

    assert observation.expires_at > observation.observed_at

    with pytest.raises(ValidationError):
        DynamicObservationInput(
            subject_id="attr_qn_001",
            observation_type="weather",
            value={"condition": "rain"},
            source="test",
            observed_at=observed_at,
            expires_at=observed_at,
        )
