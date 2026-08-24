from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from nextrip_pipeline.schemas import NexTripModel


def _default_kb_root() -> Path:
    configured = os.getenv("NEXTRIP_KB_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


class CurrentDataSettings(NexTripModel):
    """Canonical place, operational observation, and service settings."""

    kb_root: Path = Field(default_factory=_default_kb_root)
    canonical_dataset_path: Path | None = None
    current_hotel_price_root: Path | None = None
    current_hotel_availability_root: Path | None = None
    current_trivago_mapping_root: Path | None = None
    api_key: SecretStr | None = None
    trivago_refresh_enabled: bool = False
    traffic_api_base_url: str | None = None
    traffic_api_key: SecretStr | None = None
    traffic_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @model_validator(mode="after")
    def resolve_paths(self) -> CurrentDataSettings:
        root = self.kb_root.expanduser().resolve()
        object.__setattr__(self, "kb_root", root)
        if self.canonical_dataset_path is not None:
            object.__setattr__(
                self,
                "canonical_dataset_path",
                _resolve_path(root, self.canonical_dataset_path),
            )
        defaults = {
            "current_hotel_price_root": root / "data/current/hotel_price",
            "current_hotel_availability_root": (
                root / "data/current/hotel_availability"
            ),
            "current_trivago_mapping_root": root / "data/current/trivago_mappings",
        }
        for field_name, default in defaults.items():
            value = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                default if value is None else _resolve_path(root, value),
            )
        if self.traffic_api_base_url is not None:
            normalized = self.traffic_api_base_url.strip().rstrip("/")
            object.__setattr__(self, "traffic_api_base_url", normalized or None)
        return self

    @classmethod
    def from_env(cls) -> CurrentDataSettings:
        def boolean(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            normalized = raw.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name} must be a boolean")

        api_key = os.getenv("CURRENT_DATA_API_KEY")
        traffic_key = os.getenv("TRAFFIC_API_KEY")
        canonical_dataset = os.getenv("NEXTRIP_CANONICAL_DATASET")
        return cls(
            kb_root=_default_kb_root(),
            canonical_dataset_path=(
                Path(canonical_dataset) if canonical_dataset else None
            ),
            api_key=SecretStr(api_key) if api_key else None,
            trivago_refresh_enabled=boolean(
                "CURRENT_DATA_TRIVAGO_REFRESH_ENABLED", False
            ),
            traffic_api_base_url=os.getenv("CURRENT_DATA_TRAFFIC_API_URL") or None,
            traffic_api_key=SecretStr(traffic_key) if traffic_key else None,
            traffic_timeout_seconds=float(
                os.getenv("CURRENT_DATA_TRAFFIC_TIMEOUT_SECONDS", "20")
            ),
        )


def _resolve_path(root: Path, value: Path) -> Path:
    expanded = value.expanduser()
    return (expanded if expanded.is_absolute() else root / expanded).resolve()
