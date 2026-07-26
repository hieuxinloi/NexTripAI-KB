from __future__ import annotations

from ..v5.graph_store import V5GraphStore


class V8GraphStore(V5GraphStore):
    """V8 reads the verified V5 snapshot until a dedicated projection exists."""

    kb_version = "v5"
