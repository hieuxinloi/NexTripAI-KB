from __future__ import annotations

from threading import Lock

from fastapi import Request

from ..config import Settings
from ..gemini_client import GeminiClient
from ..neo4j_store import Neo4jGraphStore


class KbServices:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Neo4jGraphStore(settings)
        self._gemini: GeminiClient | None = None
        self._gemini_lock = Lock()

    @property
    def gemini(self) -> GeminiClient:
        if self._gemini is None:
            with self._gemini_lock:
                if self._gemini is None:
                    self._gemini = GeminiClient(self.settings)
        return self._gemini

    def close(self) -> None:
        self.store.close()
        if self._gemini is not None:
            self._gemini.close()


def get_kb_services(request: Request) -> KbServices:
    return request.app.state.kb_services
