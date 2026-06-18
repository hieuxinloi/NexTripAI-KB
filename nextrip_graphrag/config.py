from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "change-me"
    neo4j_database: str | None = "neo4j"
    google_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 1536
    top_k: int = 8
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", cls.neo4j_uri),
            neo4j_user=os.getenv("NEO4J_USER", cls.neo4j_user),
            neo4j_password=os.getenv("NEO4J_PASSWORD", cls.neo4j_password),
            neo4j_database=os.getenv("NEO4J_DATABASE", cls.neo4j_database) or None,
            google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", cls.gemini_model),
            embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", cls.embedding_model),
            embedding_dim=int(os.getenv("GEMINI_EMBEDDING_DIM", str(cls.embedding_dim))),
            top_k=int(os.getenv("RAG_TOP_K", str(cls.top_k))),
            temperature=float(os.getenv("RAG_TEMPERATURE", str(cls.temperature))),
        )
