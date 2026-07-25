from pathlib import Path

from nextrip_graphrag.evaluation.v3_runner import (
    COMMON_CASE_LIMITS,
    evaluate_v3_case,
    read_v3_cases,
)


BENCHMARK = Path(__file__).parents[2] / "docs" / "test_cases_benchmark_v3 (1).md"


def test_v3_benchmark_has_expected_500_case_shape() -> None:
    cases = read_v3_cases(BENCHMARK)

    assert len(cases) == 500
    assert sum(case["comparison_case"] for case in cases) == 400
    assert {
        group: sum(case["group"] == group for case in cases)
        for group in COMMON_CASE_LIMITS
    } == {"A": 120, "B": 100, "C": 80, "D": 80, "E": 60, "F": 60}


def test_information_count_requires_aggregate_fact() -> None:
    case = {
        "group": "A",
        "query": "Ở Đà Nẵng có bao nhiêu khách sạn?",
    }
    failed, reason = evaluate_v3_case(
        case,
        [
            {
                "answer_type": "recommendation",
                "query_plan": {},
                "recommendations": [{"name": "Hotel A"}],
            }
        ],
    )

    assert failed is False
    assert reason == "count_not_aggregated"


def test_filtered_recommendation_requires_filter_in_plan() -> None:
    case = {
        "group": "B",
        "query": "Gợi ý khách sạn 5 sao ở Đà Nẵng",
    }
    passed, reason = evaluate_v3_case(
        case,
        [
            {
                "answer_type": "recommendation",
                "query_plan": {"constraints": []},
                "recommendations": [
                    {
                        "name": "Hotel A",
                        "city": "Đà Nẵng",
                        "entity_type": "hotel",
                    }
                ],
            }
        ],
    )

    assert passed is False
    assert reason == "filters_not_applied:star_rating:5"


def test_itinerary_requires_grounded_timed_slots() -> None:
    case = {
        "group": "D",
        "query": "Lên lịch trình 1 ngày ở Đà Nẵng.",
    }
    recommendations = [
        {
            "place_id": f"place-{index}",
            "name": f"Place {index}",
            "city": "Đà Nẵng",
            "entity_type": "attraction",
        }
        for index in range(1, 4)
    ]

    passed, reason = evaluate_v3_case(
        case,
        [
            {
                "intent": "plan_candidates",
                "query_plan": {
                    "duration_days": 1,
                    "geo_scope": {"cities": ["Đà Nẵng"]},
                },
                "recommendations": recommendations,
                "itinerary": [
                    {
                        "day": 1,
                        "slots": [
                            {
                                "place_id": item["place_id"],
                                "start_time": f"{8 + index:02d}:00",
                                "end_time": f"{9 + index:02d}:00",
                            }
                            for index, item in enumerate(recommendations)
                        ],
                    }
                ],
            }
        ],
    )

    assert passed is True
    assert reason == "grounded_itinerary_returned"


def test_multiturn_requires_explicit_context_updates() -> None:
    case = {
        "group": "E",
        "query": "Gợi ý khách sạn ở Đà Nẵng. → Ưu tiên hồ bơi.",
    }

    passed, reason = evaluate_v3_case(
        case,
        [
            {
                "query_plan": {"geo_scope": {"cities": ["Đà Nẵng"]}},
                "recommendations": [{"place_id": "hotel-1"}],
            },
            {
                "query_plan": {"geo_scope": {"cities": ["Đà Nẵng"]}},
                "recommendations": [{"place_id": "hotel-2"}],
                "conversation_context": {
                    "turn_count": 2,
                    "cities": ["Đà Nẵng"],
                    "applied_updates": ["inherited_city"],
                    "resolved_query": "Ưu tiên hồ bơi. ở Đà Nẵng. khách sạn",
                },
            },
        ],
    )

    assert passed is True
    assert reason == "multiturn_context_applied"
