from nextrip_graphrag.api.router import _all_retrieval_sources_failed


def test_all_retrieval_sources_failed_when_every_search_errors() -> None:
    trace = [
        {"step": "text_unit_vector_search", "status": "error"},
        {"step": "text_unit_keyword_search", "status": "error"},
        {"step": "graph_filter_search", "status": "skipped"},
        {"step": "text_unit_rrf_fusion", "status": "ok"},
    ]

    assert _all_retrieval_sources_failed(trace) is True


def test_retrieval_is_available_when_keyword_fallback_succeeds() -> None:
    trace = [
        {"step": "vector_search", "status": "error"},
        {"step": "keyword_search", "status": "ok", "count": 0},
    ]

    assert _all_retrieval_sources_failed(trace) is False
