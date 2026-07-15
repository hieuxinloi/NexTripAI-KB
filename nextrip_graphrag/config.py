from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import replace


DEFAULT_SEARCH_TOP_K = 8
DEFAULT_TYPED_QUERY_TOP_K = 5
MIN_TOP_K = 1
MAX_TOP_K = 30
DEFAULT_TEMPERATURE = 0.2
STRUCTURED_TEMPERATURE = 0.0
HEALTH_CHECK_TIMEOUT_SECONDS = 0.5
ENRICHMENT_HTTP_TIMEOUT_SECONDS = 20.0
SOURCE_CRAWL_DELAY_SECONDS = 0.75
NOMINATIM_DELAY_SECONDS = 1.1
V5_AREA_PROXIMITY_RADIUS_KM = 5.0
V5_AREA_PROXIMITY_CONFIDENCE = 0.70


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
    neo4j_v2_uri: str = "bolt://localhost:7688"
    neo4j_v2_user: str = "neo4j"
    neo4j_v2_password: str = "change-me"
    neo4j_v2_database: str | None = "neo4j"
    neo4j_v3_uri: str = "bolt://localhost:7689"
    neo4j_v3_user: str = "neo4j"
    neo4j_v3_password: str = "change-me"
    neo4j_v3_database: str | None = "neo4j"
    neo4j_v4_uri: str = "bolt://localhost:7690"
    neo4j_v4_user: str = "neo4j"
    neo4j_v4_password: str = "change-me"
    neo4j_v4_database: str | None = "neo4j"
    neo4j_v5_uri: str = "bolt://localhost:7691"
    neo4j_v5_user: str = "neo4j"
    neo4j_v5_password: str = "change-me"
    neo4j_v5_database: str | None = "neo4j"
    neo4j_connection_timeout: float = 3.0
    neo4j_max_transaction_retry_time: float = 3.0
    google_api_key: str | None = None
    google_genai_use_vertexai: bool = False
    google_application_credentials: str | None = None
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_ms: int = 30000
    gemini_retry_attempts: int = 3
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1536
    query_embedding_cache: str = "tmp/query_embedding_cache"
    v5_concept_link_min_score: float = 0.72
    v5_concept_link_min_margin: float = 0.03
    v5_concept_link_top_k: int = 5
    v5_concept_selection_min_confidence: float = 0.75
    top_k: int = DEFAULT_SEARCH_TOP_K
    temperature: float = DEFAULT_TEMPERATURE
    structured_temperature: float = STRUCTURED_TEMPERATURE
    log_level: str = "INFO"
    admin_api_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            neo4j_uri=os.getenv("NEO4J_URI", cls.neo4j_uri),
            neo4j_user=os.getenv("NEO4J_USER", cls.neo4j_user),
            neo4j_password=os.getenv("NEO4J_PASSWORD", cls.neo4j_password),
            neo4j_database=os.getenv("NEO4J_DATABASE", cls.neo4j_database) or None,
            neo4j_v2_uri=os.getenv("NEO4J_V2_URI", cls.neo4j_v2_uri),
            neo4j_v2_user=os.getenv("NEO4J_V2_USER", cls.neo4j_v2_user),
            neo4j_v2_password=os.getenv("NEO4J_V2_PASSWORD", cls.neo4j_v2_password),
            neo4j_v2_database=os.getenv("NEO4J_V2_DATABASE", cls.neo4j_v2_database) or None,
            neo4j_v3_uri=os.getenv("NEO4J_V3_URI", cls.neo4j_v3_uri),
            neo4j_v3_user=os.getenv("NEO4J_V3_USER", cls.neo4j_v3_user),
            neo4j_v3_password=os.getenv("NEO4J_V3_PASSWORD", cls.neo4j_v3_password),
            neo4j_v3_database=os.getenv("NEO4J_V3_DATABASE", cls.neo4j_v3_database) or None,
            neo4j_v4_uri=os.getenv("NEO4J_V4_URI", cls.neo4j_v4_uri),
            neo4j_v4_user=os.getenv("NEO4J_V4_USER", cls.neo4j_v4_user),
            neo4j_v4_password=os.getenv("NEO4J_V4_PASSWORD", cls.neo4j_v4_password),
            neo4j_v4_database=os.getenv("NEO4J_V4_DATABASE", cls.neo4j_v4_database) or None,
            neo4j_v5_uri=os.getenv("NEO4J_V5_URI", cls.neo4j_v5_uri),
            neo4j_v5_user=os.getenv("NEO4J_V5_USER", cls.neo4j_v5_user),
            neo4j_v5_password=os.getenv("NEO4J_V5_PASSWORD", cls.neo4j_v5_password),
            neo4j_v5_database=os.getenv("NEO4J_V5_DATABASE", cls.neo4j_v5_database) or None,
            google_api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
            google_genai_use_vertexai=_env_bool("GOOGLE_GENAI_USE_VERTEXAI"),
            google_application_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None,
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
            google_cloud_location=os.getenv("GOOGLE_CLOUD_LOCATION") or cls.google_cloud_location,
            gemini_model=os.getenv("GEMINI_MODEL", cls.gemini_model),
            embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", cls.embedding_model),
            admin_api_key=os.getenv("KB_ADMIN_API_KEY") or None,
        )

    def for_v2(self) -> "Settings":
        return replace(
            self,
            neo4j_uri=self.neo4j_v2_uri,
            neo4j_user=self.neo4j_v2_user,
            neo4j_password=self.neo4j_v2_password,
            neo4j_database=self.neo4j_v2_database,
        )

    def for_v3(self) -> "Settings":
        return replace(
            self,
            neo4j_uri=self.neo4j_v3_uri,
            neo4j_user=self.neo4j_v3_user,
            neo4j_password=self.neo4j_v3_password,
            neo4j_database=self.neo4j_v3_database,
        )

    def for_v4(self) -> "Settings":
        return replace(
            self,
            neo4j_uri=self.neo4j_v4_uri,
            neo4j_user=self.neo4j_v4_user,
            neo4j_password=self.neo4j_v4_password,
            neo4j_database=self.neo4j_v4_database,
        )

    def for_v5(self) -> "Settings":
        return replace(
            self,
            neo4j_uri=self.neo4j_v5_uri,
            neo4j_user=self.neo4j_v5_user,
            neo4j_password=self.neo4j_v5_password,
            neo4j_database=self.neo4j_v5_database,
        )
