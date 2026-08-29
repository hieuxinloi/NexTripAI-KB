from __future__ import annotations

from .base import RetrievalStrategy
from .versions.v1.strategy import V1Retriever
from .versions.v1_hybrid.strategy import V1HybridRetriever
from .versions.v1_provenance.strategy import V1ProvenanceRetriever


_STRATEGIES: dict[str, RetrievalStrategy] = {
    "v1": V1Retriever(),
    "v1_hybrid": V1HybridRetriever(),
    "v1_provenance": V1ProvenanceRetriever(),
}


def available_strategies() -> list[str]:
    return sorted(_STRATEGIES)


def get_strategy(name: str) -> RetrievalStrategy:
    try:
        return _STRATEGIES[name.lower()]
    except KeyError as exc:
        choices = ", ".join(available_strategies())
        raise ValueError(f"Unknown retrieval strategy {name!r}. Available: {choices}") from exc
