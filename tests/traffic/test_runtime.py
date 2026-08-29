from __future__ import annotations

from pathlib import Path

import pytest

from nextrip_traffic.config import TrafficSettings
from nextrip_traffic.errors import TrafficConfigurationError
from nextrip_traffic.runtime import (
    TrafficRuntime,
    TrafficRuntimeFactory,
    build_traffic_service,
)
from tests.canonical_dataset_support import (
    CanonicalTestPlace,
    write_canonical_dataset,
)


class ClosableService:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_runtime_factory_is_lazy_singleton_and_closes() -> None:
    services: list[ClosableService] = []

    def builder() -> TrafficRuntime:
        service = ClosableService()
        services.append(service)
        return TrafficRuntime(service=service)  # type: ignore[arg-type]

    factory = TrafficRuntimeFactory(builder)

    assert factory.peek() is None
    first = factory.get()
    assert factory.get() is first
    assert len(services) == 1

    factory.reset()
    assert services[0].closed is True
    assert factory.peek() is None
    assert factory.get() is not first
    assert len(services) == 2
    factory.close()
    assert services[1].closed is True


def test_build_traffic_service_constructs_without_provider_network(
    tmp_path: Path,
) -> None:
    canonical_dataset = write_canonical_dataset(
        tmp_path / "canonical",
        [CanonicalTestPlace("one", "Place one", 16.0, 108.0)],
    )
    settings = TrafficSettings(
        kb_root=tmp_path,
        canonical_dataset_path=canonical_dataset,
        cache_path=tmp_path / "cache.sqlite3",
        valhalla_enabled=False,
        here_enabled=False,
    )

    service = build_traffic_service(settings)
    try:
        assert service.providers == {}
        assert len(service.registry) == 2
        assert service.registry.last_report.error_count == 0
    finally:
        service.close()


def test_build_traffic_service_requires_canonical_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEXTRIP_CANONICAL_DATASET", raising=False)

    with pytest.raises(
        TrafficConfigurationError,
        match="NEXTRIP_CANONICAL_DATASET",
    ):
        build_traffic_service()


def test_build_traffic_service_wraps_invalid_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALHALLA_ENABLED", "not-a-boolean")

    with pytest.raises(TrafficConfigurationError, match="initialization failed"):
        build_traffic_service()
