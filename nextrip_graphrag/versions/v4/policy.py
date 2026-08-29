from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RankingWeights:
    semantic: float = 0.35
    graph_coverage: float = 0.25
    type_match: float = 0.15
    rating: float = 0.15
    evidence: float = 0.10

    def __post_init__(self) -> None:
        total = self.semantic + self.graph_coverage + self.type_match + self.rating + self.evidence
        if abs(total - 1.0) > 1e-9:
            raise ValueError("V4 ranking weights must sum to 1.0")

    def score(
        self,
        *,
        semantic: float,
        graph_coverage: float,
        type_match: float,
        rating: float,
        evidence: float,
    ) -> float:
        return (
            self.semantic * semantic
            + self.graph_coverage * graph_coverage
            + self.type_match * type_match
            + self.rating * rating
            + self.evidence * evidence
        )


@dataclass(frozen=True)
class V4Policy:
    structured_claim_confidence: float = 0.90
    deterministic_claim_confidence: float = 0.78
    relationship_promotion_confidence: float = 0.75
    sentence_batch_size: int = 500
    concept_batch_size: int = 500
    claim_batch_size: int = 300
    candidate_multiplier: int = 10
    minimum_candidate_pool: int = 50
    minimum_vector_pool: int = 100
    maximum_evidence_results: int = 30
    community_sample_size: int = 8
    required_concept_weight: float = 0.4
    preferred_concept_weight: float = 0.6
    explicit_ranking_weight: float = 0.35
    ranking: RankingWeights = field(default_factory=RankingWeights)

    def __post_init__(self) -> None:
        if abs(self.required_concept_weight + self.preferred_concept_weight - 1.0) > 1e-9:
            raise ValueError("V4 concept weights must sum to 1.0")
        if not 0 <= self.explicit_ranking_weight <= 1:
            raise ValueError("V4 explicit ranking weight must be between 0 and 1")


POLICY = V4Policy()
