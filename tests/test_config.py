from nextrip_graphrag.config import Settings


def test_from_env_reads_deployment_values_only(monkeypatch) -> None:
    monkeypatch.setenv("NEO4J_V5_URI", "bolt://graph-v5:7687")
    monkeypatch.setenv("GEMINI_MODEL", "deployment-model")
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "deployment-embedding-model")

    policy_overrides = {
        "NEO4J_CONNECTION_TIMEOUT": "99",
        "NEO4J_MAX_TRANSACTION_RETRY_TIME": "99",
        "GEMINI_TIMEOUT_MS": "99",
        "GEMINI_RETRY_ATTEMPTS": "99",
        "GEMINI_EMBEDDING_DIM": "99",
        "QUERY_EMBEDDING_CACHE": "external-cache",
        "RAG_TOP_K": "99",
        "RAG_TEMPERATURE": "0.99",
        "V5_CONCEPT_LINK_MIN_SCORE": "0.99",
        "V5_CONCEPT_LINK_MIN_MARGIN": "0.99",
        "V5_CONCEPT_LINK_TOP_K": "99",
        "V5_CONCEPT_SELECTION_MIN_CONFIDENCE": "0.99",
        "LOG_LEVEL": "DEBUG",
    }
    for name, value in policy_overrides.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()
    defaults = Settings()

    assert settings.neo4j_v5_uri == "bolt://graph-v5:7687"
    assert settings.gemini_model == "deployment-model"
    assert settings.embedding_model == "deployment-embedding-model"
    assert settings.neo4j_connection_timeout == defaults.neo4j_connection_timeout
    assert settings.neo4j_max_transaction_retry_time == defaults.neo4j_max_transaction_retry_time
    assert settings.gemini_timeout_ms == defaults.gemini_timeout_ms
    assert settings.gemini_retry_attempts == defaults.gemini_retry_attempts
    assert settings.embedding_dim == defaults.embedding_dim
    assert settings.query_embedding_cache == defaults.query_embedding_cache
    assert settings.top_k == defaults.top_k
    assert settings.temperature == defaults.temperature
    assert settings.structured_temperature == defaults.structured_temperature
    assert settings.v5_concept_link_min_score == defaults.v5_concept_link_min_score
    assert settings.v5_concept_link_min_margin == defaults.v5_concept_link_min_margin
    assert settings.v5_concept_link_top_k == defaults.v5_concept_link_top_k
    assert (
        settings.v5_concept_selection_min_confidence
        == defaults.v5_concept_selection_min_confidence
    )
    assert settings.log_level == defaults.log_level
