from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from nextrip_graphrag.versions.v2.schemas import QueryIntent
from nextrip_graphrag.versions.v4.extraction import DescriptionExtractor
from nextrip_graphrag.versions.v4.ontology import deterministic_description_claims
from nextrip_graphrag.versions.v4.query_planner import deterministic_plan
from nextrip_graphrag.versions.v4.schemas import (
    ClaimPolarity,
    DynamicObservationInput,
    RetrievalMode,
)
from nextrip_graphrag.evaluation.v2_runner import _response_operation


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
        "props": {"name": "Korean BBQ", "description": "Chuyen thit nuong Han Quoc.", "category": "bbq"},
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
        concept for concept in extraction.concepts if concept.concept_type.value == "Audience"
    ]
    assert len(family_concepts) == 1
    assert family_concepts[0].canonical_name == "families"


def test_v4_plans_ontology_guided_recommendation() -> None:
    plan = deterministic_plan(
        "G\u1ee3i \u00fd nh\u00e0 h\u00e0ng h\u1ea3i s\u1ea3n cho gia \u0111\u00ecnh \u1edf Quy Nh\u01a1n"
    )

    assert plan.intent == QueryIntent.RECOMMENDATION
    assert plan.retrieval_mode == RetrievalMode.RECOMMENDATION
    assert plan.city == "Quy Nh\u01a1n"
    assert plan.entity_types == ["restaurant"]
    assert plan.required_concepts == ["seafood", "families"]


def test_v4_routes_live_weather_to_backend_tool() -> None:
    plan = deterministic_plan("Th\u1eddi ti\u1ebft Quy Nh\u01a1n h\u00f4m nay th\u1ebf n\u00e0o?")

    assert plan.retrieval_mode == RetrievalMode.DYNAMIC_SEARCH
    assert plan.intent == QueryIntent.UNSUPPORTED


def test_v4_plans_itinerary_candidates_without_building_timeline() -> None:
    plan = deterministic_plan("Quy Nh\u01a1n 1 ng\u00e0y n\u00ean \u0111i \u0111\u00e2u?")

    assert plan.retrieval_mode == RetrievalMode.PLANNING_CANDIDATES
    assert plan.intent == QueryIntent.RECOMMENDATION


def test_v4_query_plan_maps_to_benchmark_operation() -> None:
    plan = deterministic_plan("Quy Nh\u01a1n co bao nhieu nha hang?")

    assert _response_operation(plan) == "count"


def test_v4_multi_count_keeps_all_entity_types() -> None:
    plan = deterministic_plan(
        "Quy Nh\u01a1n c\u00f3 bao nhi\u00eau qu\u00e1n cafe, nh\u00e0 h\u00e0ng v\u00e0 kh\u00e1ch s\u1ea1n?"
    )

    assert plan.retrieval_mode == RetrievalMode.AGGREGATE
    assert plan.entity_types == ["cafe", "restaurant", "hotel"]


def test_v4_rain_question_routes_to_constrained_recommendation() -> None:
    plan = deterministic_plan(
        "Tr\u1eddi m\u01b0a th\u00ec n\u00ean \u0111i ch\u01a1i \u1edf \u0111\u00e2u \u1edf Quy Nh\u01a1n?"
    )

    assert plan.retrieval_mode == RetrievalMode.RECOMMENDATION
    assert plan.entity_types == ["attraction"]
    assert plan.constraints[0].field == "weather"
    assert plan.required_concepts == []


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
