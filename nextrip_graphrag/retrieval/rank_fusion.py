from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedRank:
    item_id: str
    score: float
    ranks: dict[str, int]


def fuse_ranked_ids(
    ranked_ids: Mapping[str, Sequence[str]],
    *,
    source_weights: Mapping[str, float] | None = None,
    k: int = DEFAULT_RRF_K,
) -> list[FusedRank]:
    """Fuse ID rankings without assuming comparable source score scales."""
    if k < 1:
        raise ValueError("RRF k must be positive")

    weights = {} if source_weights is None else source_weights
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for source, item_ids in ranked_ids.items():
        weight = weights.get(source, 1.0)
        if weight < 0:
            raise ValueError("RRF source weights cannot be negative")
        seen: set[str] = set()
        for rank, item_id in enumerate(item_ids, start=1):
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
            ranks.setdefault(item_id, {})[source] = rank

    return sorted(
        (
            FusedRank(item_id=item_id, score=score, ranks=ranks[item_id])
            for item_id, score in scores.items()
        ),
        key=lambda item: (
            -item.score,
            min(item.ranks.values()),
            item.item_id,
        ),
    )
