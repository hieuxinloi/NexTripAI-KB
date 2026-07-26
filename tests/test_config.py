from nextrip_graphrag.config import Settings


def _clear_versioned_neo4j_env(monkeypatch) -> None:
    for version in range(1, 10):
        for field in ("URI", "USER", "PASSWORD", "DATABASE"):
            monkeypatch.delenv(f"NEO4J_V{version}_{field}", raising=False)
    for field in ("URI", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"NEO4J_{field}", raising=False)


def test_from_env_reads_deployment_values_only(monkeypatch) -> None:
    monkeypatch.setenv("NEO4J_V5_URI", "bolt://graph-v5:7687")
    monkeypatch.setenv("GEMINI_PLANNER_MODEL", "deployment-planner-model")
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low")
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
    assert settings.gemini_planner_model == "deployment-planner-model"
    assert settings.gemini_thinking_level == "low"
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


def test_configured_versions_require_complete_supported_env_block(monkeypatch) -> None:
    _clear_versioned_neo4j_env(monkeypatch)
    monkeypatch.setenv("GEMINI_PLANNER_MODEL", "test-planner-model")
    monkeypatch.setenv("NEO4J_V5_URI", "bolt://graph-v5:7687")
    monkeypatch.setenv("NEO4J_V5_USER", "neo4j")
    monkeypatch.setenv("NEO4J_V5_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_V5_DATABASE", "neo4j")
    monkeypatch.setenv("NEO4J_V4_URI", "bolt://graph-v4:7687")
    monkeypatch.setenv("NEO4J_V4_USER", "neo4j")

    settings = Settings.from_env()

    assert settings.configured_kb_versions == ("v5",)
    assert settings.for_version("v5").neo4j_uri == "bolt://graph-v5:7687"


def test_unknown_version_env_is_not_advertised_without_implementation(monkeypatch) -> None:
    _clear_versioned_neo4j_env(monkeypatch)
    monkeypatch.setenv("GEMINI_PLANNER_MODEL", "test-planner-model")
    monkeypatch.setenv("NEO4J_V6_URI", "bolt://graph-v6:7687")
    monkeypatch.setenv("NEO4J_V6_USER", "neo4j")
    monkeypatch.setenv("NEO4J_V6_PASSWORD", "secret")
    monkeypatch.setenv("NEO4J_V6_DATABASE", "neo4j")

    settings = Settings.from_env()

    assert settings.configured_kb_versions == ()


def test_from_env_requires_explicit_planner_model(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_PLANNER_MODEL", raising=False)

    try:
        Settings.from_env()
    except RuntimeError as exc:
        assert "GEMINI_PLANNER_MODEL" in str(exc)
    else:
        raise AssertionError("Expected missing planner model to fail fast")
