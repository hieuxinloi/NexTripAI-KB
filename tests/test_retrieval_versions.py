from nextrip_graphrag.retrieval import available_strategies, get_strategy
from nextrip_graphrag.retrieval.query_features import extract_graph_filters
from nextrip_graphrag.retrieval.versions.v1_hybrid.strategy import reciprocal_rank_fusion


def _row(place_id: str, score: float = 1.0) -> dict:
    return {"place": {"id": place_id}, "score": score, "facets": [], "nearby": []}


def test_version_registry_keeps_v1_experiments_available() -> None:
    assert available_strategies() == ["v1", "v1_hybrid", "v1_provenance"]
    assert get_strategy("v1").name == "v1"
    assert get_strategy("v1_hybrid").name == "v1_hybrid"


def test_query_features_extract_explicit_travel_constraints() -> None:
    filters = extract_graph_filters("Cafe rooftop trong nhà phù hợp khi trời mưa để làm việc")

    assert filters.categories == ["rooftop_cafe", "rooftop_bar"]
    assert filters.terms == ["work"]
    assert filters.indoor_or_all_weather is True


def test_v2_rrf_prioritizes_high_confidence_graph_candidates() -> None:
    results = reciprocal_rank_fusion(
        vector_rows=[],
        keyword_rows=[_row("lexical-noise"), _row("relevant")],
        graph_rows=[_row("relevant"), _row("graph-only")],
        limit=3,
    )

    assert [row["place"]["id"] for row in results] == [
        "relevant",
        "graph-only",
        "lexical-noise",
    ]
    assert results[0]["retrieval"]["keyword_rank"] == 2
    assert results[0]["retrieval"]["graph_rank"] == 1
