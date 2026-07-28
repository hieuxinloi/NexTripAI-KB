from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
import re


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
_VERSIONED_NEO4J_KEY = re.compile(
    r"^NEO4J_(V[1-9][0-9]*)_(URI|USER|PASSWORD|DATABASE)$"
)
_NEO4J_CONNECTION_FIELDS = ("URI", "USER", "PASSWORD", "DATABASE")


@dataclass(frozen=True)
class Neo4jConnectionSettings:
    uri: str
    user: str
    password: str
    database: str | None


def _version_sort_key(version: str) -> int:
    return int(version[1:])


def _configured_neo4j_versions() -> dict[str, Neo4jConnectionSettings]:
    version_names = {
        match.group(1).lower()
        for name in os.environ
        if (match := _VERSIONED_NEO4J_KEY.fullmatch(name))
    }
    connections: dict[str, Neo4jConnectionSettings] = {}
    for version in sorted(version_names, key=_version_sort_key):
        prefix = f"NEO4J_{version.upper()}_"
        values = {
            field_name: (os.getenv(f"{prefix}{field_name}") or "").strip()
            for field_name in _NEO4J_CONNECTION_FIELDS
        }
        if not all(values.values()):
            continue
        connections[version] = Neo4jConnectionSettings(
            uri=values["URI"],
            user=values["USER"],
            password=values["PASSWORD"],
            database=values["DATABASE"] or None,
        )

    legacy_values = {
        field_name: (os.getenv(f"NEO4J_{field_name}") or "").strip()
        for field_name in _NEO4J_CONNECTION_FIELDS
    }
    if "v1" not in connections and all(legacy_values.values()):
        connections["v1"] = Neo4jConnectionSettings(
            uri=legacy_values["URI"],
            user=legacy_values["USER"],
            password=legacy_values["PASSWORD"],
            database=legacy_values["DATABASE"] or None,
        )
    return dict(sorted(connections.items(), key=lambda item: _version_sort_key(item[0])))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    parsed = int(value)
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return parsed


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
    gemini_planner_model: str = ""
    gemini_thinking_level: str = "minimal"
    gemini_timeout_ms: int = 30000
    gemini_retry_attempts: int = 3
    structured_gemini_timeout_ms: int = 12000
    structured_gemini_retry_attempts: int = 1
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
    active_kb_version: str = "v8"
    neo4j_version_connections: dict[str, Neo4jConnectionSettings] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_env(cls) -> "Settings":
        planner_model = (os.getenv("GEMINI_PLANNER_MODEL") or "").strip()
        if not planner_model:
            raise RuntimeError("GEMINI_PLANNER_MODEL is required.")
        thinking_level = (
            os.getenv("GEMINI_THINKING_LEVEL", cls.gemini_thinking_level)
            .strip()
            .lower()
        )
        if thinking_level not in {"minimal", "low", "medium", "high"}:
            raise RuntimeError(
                "GEMINI_THINKING_LEVEL must be minimal, low, medium, or high."
            )
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
            gemini_planner_model=planner_model,
            gemini_thinking_level=thinking_level,
            gemini_timeout_ms=_env_int(
                "GEMINI_TIMEOUT_MS",
                cls.gemini_timeout_ms,
                minimum=1_000,
            ),
            gemini_retry_attempts=_env_int(
                "GEMINI_RETRY_ATTEMPTS",
                cls.gemini_retry_attempts,
            ),
            embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", cls.embedding_model),
            admin_api_key=os.getenv("KB_ADMIN_API_KEY") or None,
            active_kb_version=(
                os.getenv("ACTIVE_KB_VERSION", cls.active_kb_version).strip().lower()
            ),
            neo4j_version_connections=_configured_neo4j_versions(),
        )

    @property
    def configured_kb_versions(self) -> tuple[str, ...]:
        from .versions.registry import kb_version_manifests

        supported = kb_version_manifests()
        configured = [
            version
            for version in self.neo4j_version_connections
            if version in supported
        ]
        return tuple(configured)

    def for_version(self, version: str) -> "Settings":
        normalized = version.strip().lower()
        connection = self.neo4j_version_connections.get(normalized)
        if connection is not None:
            return replace(
                self,
                neo4j_uri=connection.uri,
                neo4j_user=connection.user,
                neo4j_password=connection.password,
                neo4j_database=connection.database,
            )
        if normalized == "v1":
            return self
        suffix = normalized.removeprefix("v")
        attribute_prefix = f"neo4j_v{suffix}_"
        if all(
            hasattr(self, f"{attribute_prefix}{name}")
            for name in ("uri", "user", "password", "database")
        ):
            return replace(
                self,
                neo4j_uri=getattr(self, f"{attribute_prefix}uri"),
                neo4j_user=getattr(self, f"{attribute_prefix}user"),
                neo4j_password=getattr(self, f"{attribute_prefix}password"),
                neo4j_database=getattr(self, f"{attribute_prefix}database"),
            )
        raise ValueError(f"Unsupported Knowledge Base version: {normalized}")

    def for_v2(self) -> "Settings":
        return self.for_version("v2")

    def for_v3(self) -> "Settings":
        return self.for_version("v3")

    def for_v4(self) -> "Settings":
        return self.for_version("v4")

    def for_v5(self) -> "Settings":
        return self.for_version("v5")

    def for_v8(self) -> "Settings":
        return self.for_version("v8")
