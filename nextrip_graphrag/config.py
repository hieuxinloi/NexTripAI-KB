from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me"
    neo4j_database: str | None = "neo4j"
    google_api_key: str | None = None
    google_genai_use_vertexai: bool = False
    google_application_credentials: str | None = None
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1536
    top_k: int = 8
    temperature: float = 0.2
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", cls.neo4j_uri),
            neo4j_user=os.getenv("NEO4J_USER", cls.neo4j_user),
            neo4j_password=os.getenv("NEO4J_PASSWORD", cls.neo4j_password),
            neo4j_database=os.getenv("NEO4J_DATABASE", cls.neo4j_database) or None,
            google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            google_genai_use_vertexai=_env_bool("GOOGLE_GENAI_USE_VERTEXAI"),
            google_application_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None,
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION") or cls.google_cloud_location,
            gemini_model=os.getenv("GEMINI_MODEL", cls.gemini_model),
            embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", cls.embedding_model),
            embedding_dim=int(os.getenv("GEMINI_EMBEDDING_DIM", str(cls.embedding_dim))),
            top_k=int(os.getenv("RAG_TOP_K", str(cls.top_k))),
            temperature=float(os.getenv("RAG_TEMPERATURE", str(cls.temperature))),
            log_level=os.getenv("LOG_LEVEL", cls.log_level),
        )
