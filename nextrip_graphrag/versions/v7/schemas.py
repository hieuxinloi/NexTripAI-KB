from __future__ import annotations

from typing import Literal

from ..v5.schemas import V5QueryResponse


class V7QueryResponse(V5QueryResponse):
    kb_version: Literal["v7"] = "v7"
