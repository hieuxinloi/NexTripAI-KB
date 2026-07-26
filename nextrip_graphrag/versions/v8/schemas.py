from __future__ import annotations

from typing import Literal

from ..v6.schemas import V6QueryResponse


class V8QueryResponse(V6QueryResponse):
    """V6's stateful response with the V8 manifest and retrieval trace."""

    kb_version: Literal["v8"] = "v8"
