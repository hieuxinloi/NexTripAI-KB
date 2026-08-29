from __future__ import annotations

from nextrip_graphrag.api import dependencies


class _V8OnlySettings:
    configured_kb_versions = ("v8",)
    active_kb_version = "v8"

    @staticmethod
    def for_version(version: str) -> str:
        return f"settings:{version}"


class _FakeStore:
    def __init__(self, settings: str) -> None:
        self.settings = settings
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_v8_only_services_reuse_active_store_without_creating_v1(monkeypatch) -> None:
    def legacy_store(_settings):
        raise AssertionError("V8-only runtime must not construct a V1 store")

    monkeypatch.setattr(dependencies, "Neo4jGraphStore", legacy_store)
    monkeypatch.setattr(
        dependencies,
        "version_graph_store_class",
        lambda version: _FakeStore if version == "v8" else None,
    )

    services = dependencies.KbServices(_V8OnlySettings())

    assert services.active_version == "v8"
    assert services.store is services.store_for("v8")
    services.close()
    assert services.store.close_calls == 1
