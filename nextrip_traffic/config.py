from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, model_validator

from nextrip_pipeline.schemas import NexTripModel


def _default_kb_root() -> Path:
    configured = os.getenv("NEXTRIP_KB_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


class TrafficSettings(NexTripModel):
    """Runtime settings with conservative local defaults.

    Credentials are read at service-construction time, never while importing
    the FastAPI app. This keeps health checks and unit tests usable without a
    HERE account.
    """

    kb_root: Path = Field(default_factory=_default_kb_root)
    current_place_root: Path | None = None
    access_point_overrides_path: Path | None = None
    cache_path: Path | None = None
    valhalla_enabled: bool = True
    valhalla_base_url: str = "http://127.0.0.1:8002"
    here_enabled: bool = True
    here_api_key: str | None = None
    here_routes_url: str = "https://router.hereapi.com/v8/routes"
    provider_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    route_ttl_seconds: int = Field(default=600, ge=60, le=86400)
    matrix_ttl_seconds: int = Field(default=600, ge=60, le=86400)
    free_flow_ttl_seconds: int = Field(default=86400, ge=300, le=604800)
    departure_bucket_minutes: int = Field(default=5, ge=1, le=60)
    max_matrix_cells: int = Field(default=25, ge=1, le=100)
    stale_if_error_seconds: int = Field(default=3600, ge=0, le=86400)
    circuit_breaker_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_breaker_cooldown_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def fill_paths(self) -> TrafficSettings:
        root = self.kb_root.expanduser().resolve()
        object.__setattr__(self, "kb_root", root)
        if self.current_place_root is None:
            object.__setattr__(self, "current_place_root", root / "data/current/place")
        if self.access_point_overrides_path is None:
            object.__setattr__(
                self,
                "access_point_overrides_path",
                root / "config/traffic-access-points.json",
            )
        if self.cache_path is None:
            object.__setattr__(
                self,
                "cache_path",
                root / "data/current/traffic/cache.sqlite3",
            )
        return self

    @classmethod
    def from_env(cls) -> TrafficSettings:
        def integer(name: str, default: int) -> int:
            raw = os.getenv(name)
            return default if raw is None else int(raw)

        def boolean(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name} must be a boolean")

        return cls(
            kb_root=_default_kb_root(),
            valhalla_enabled=boolean("VALHALLA_ENABLED", True),
            valhalla_base_url=os.getenv("VALHALLA_URL", "http://127.0.0.1:8002"),
            here_enabled=boolean("HERE_ENABLED", True),
            here_api_key=os.getenv("HERE_API_KEY") or None,
            here_routes_url=os.getenv(
                "HERE_ROUTES_URL", "https://router.hereapi.com/v8/routes"
            ),
            provider_timeout_seconds=float(
                os.getenv("TRAFFIC_PROVIDER_TIMEOUT_SECONDS", "20")
            ),
            route_ttl_seconds=integer("TRAFFIC_ROUTE_TTL_SECONDS", 600),
            matrix_ttl_seconds=integer("TRAFFIC_MATRIX_TTL_SECONDS", 600),
            free_flow_ttl_seconds=integer("TRAFFIC_FREE_FLOW_TTL_SECONDS", 86400),
            departure_bucket_minutes=integer("TRAFFIC_DEPARTURE_BUCKET_MINUTES", 5),
            max_matrix_cells=integer("TRAFFIC_MAX_MATRIX_CELLS", 25),
            stale_if_error_seconds=integer("TRAFFIC_STALE_IF_ERROR_SECONDS", 3600),
            circuit_breaker_failure_threshold=integer(
                "TRAFFIC_CIRCUIT_FAILURE_THRESHOLD", 3
            ),
            circuit_breaker_cooldown_seconds=integer(
                "TRAFFIC_CIRCUIT_COOLDOWN_SECONDS", 60
            ),
        )
