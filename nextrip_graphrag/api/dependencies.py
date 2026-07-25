from __future__ import annotations

from threading import Lock
from secrets import compare_digest

from fastapi import Header, HTTPException, Request

from ..config import Settings
from ..gemini_client import GeminiClient
from ..neo4j_store import Neo4jGraphStore
from ..versions.registry import version_graph_store_class


class KbServices:
    def __init__(self, settings: Settings):
        self.settings = settings
        legacy_settings = (
            settings.for_version("v1")
            if "v1" in settings.configured_kb_versions
            else settings
        )
        self.store = Neo4jGraphStore(legacy_settings)
        self.version_stores = {
            version: version_graph_store_class(version)(
                settings.for_version(version)
            )
            for version in settings.configured_kb_versions
            if version != "v1"
        }
        self._gemini: GeminiClient | None = None
        self._gemini_lock = Lock()

    def store_for(self, version: str):
        normalized = version.strip().lower()
        if normalized == "v1" and normalized in self.settings.configured_kb_versions:
            return self.store
        try:
            return self.version_stores[normalized]
        except KeyError as exc:
            raise ValueError(
                f"Knowledge Base {normalized.upper()} is not configured."
            ) from exc

    @property
    def gemini(self) -> GeminiClient:
        if self._gemini is None:
            with self._gemini_lock:
                if self._gemini is None:
                    self._gemini = GeminiClient(self.settings)
        return self._gemini

    def close(self) -> None:
        self.store.close()
        for store in self.version_stores.values():
            store.close()
        if self._gemini is not None:
            self._gemini.close()


def get_kb_services(request: Request) -> KbServices:
    return request.app.state.kb_services


def require_admin_api_key(
    request: Request,
    x_kb_admin_key: str | None = Header(default=None),
) -> None:
    expected = request.app.state.kb_services.settings.admin_api_key
    if not expected:
        raise HTTPException(status_code=403, detail="KB administration API is disabled.")
    if not x_kb_admin_key or not compare_digest(x_kb_admin_key, expected):
        raise HTTPException(status_code=403, detail="Invalid KB administration credential.")
