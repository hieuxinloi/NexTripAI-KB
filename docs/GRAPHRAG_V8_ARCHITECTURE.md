# NexTripAI GraphRAG V8 architecture proposal

## Decision

Use a schema-first hybrid GraphRAG. Keep the verified travel graph as the source
of truth, add evidence-grounded community projections for broad questions, and
route each query to a bounded retrieval mode. Do not replace the typed executor
with unrestricted LLM-generated Cypher.

V7.1 implements the first safe step: full-text and vector entity candidates are
fused by reciprocal rank rather than by incomparable raw scores.

## Why this design

Microsoft GraphRAG separates entity-focused local search, community-report global
search, and DRIFT search that combines both. Neo4j's supported retrievers similarly
combine vector or hybrid candidate search with bounded Cypher expansion. The
NexTripAI dataset is already structured and verified, so importing an
LLM-extracted graph wholesale would duplicate weaker facts and lose the existing
typed constraints.

Three options were evaluated:

1. **Recommended — typed graph plus local/global/DRIFT-style routing.** Best
   correctness and provenance; requires a new community index and evaluation.
2. **Neo4j GraphRAG package retrievers.** Faster integration, but adds a package
   contract and does not replace domain-specific constraint handling.
3. **Microsoft GraphRAG indexing end to end.** Strong global summaries, but costly
   and duplicative for 519 verified structured places.

## Knowledge model

### Source-of-truth nodes

- `City`, `GeoArea`, `Place`, `Offering`, and `Concept` model reusable domain
  entities.
- `Claim` models an assertion, including polarity, confidence, extraction method,
  validity interval, and source version.
- `TextUnit` and `Document` preserve the evidence used by each claim.

Scalar attributes such as rating, coordinates, opening hours, and prices stay as
properties. A value becomes a node only when it is reusable, independently
identifiable, or needs provenance.

### Derived index nodes

- `Community` stores a detected graph community at a hierarchy level.
- `CommunityReport` stores an evidence-grounded summary and the IDs of supporting
  claims/text units.

Community nodes are retrieval indexes, not truth sources. They must be rebuildable
from the typed graph.

### Edges

- Verified: `IN_CITY`, `LOCATED_IN`, `HAS_AREA`, `HAS_OFFERING`.
- Evidence: `ABOUT`, `OBJECT`, `SUPPORTED_BY`, `PART_OF`, `MENTIONS`.
- Derived: `NEAR`, `NEAR_AREA`, `IN_COMMUNITY`, `PARENT_COMMUNITY`,
  `HAS_REPORT`.

Every derived edge stores `derivation`, `algorithm_version`, `confidence`, and the
input/evidence IDs used to create it. Rebuilding an index version must not mutate
verified source edges.

## Index build

1. Validate and upsert verified entities and scalar properties.
2. Chunk descriptions into stable `TextUnit` IDs.
3. Extract claims into a closed ontology; link every claim to one subject, one
   concept, and at least one evidence unit.
4. Create semantic text and embeddings for `Place`, `Concept`, `TextUnit`, and
   `CommunityReport`.
5. Project a weighted graph for community detection:
   - place–concept weight = claim confidence multiplied by evidence support;
   - place–area and place–city weights come from verified geography;
   - derived proximity has a lower capped weight;
   - negative or conflicted claims do not create positive similarity edges.
6. Run hierarchical Leiden when Neo4j GDS is available. Otherwise fail the V8
   community build explicitly; do not silently substitute taxonomy buckets.
7. Generate reports from member entities, relationships, claims, and text units.
   Persist supporting IDs and report prompt/model/version.
8. Validate invariants, retrieval coverage, and report grounding before publishing
   the snapshot.

## Retrieval router

| Mode | Use | Retrieval |
| --- | --- | --- |
| Local | named place/detail/compare | graph-ground entity, expand 1–2 typed hops, fetch supporting text units |
| Path | recommendation/constraints | typed graph gate, hybrid candidates, path/evidence scoring |
| Global | city/theme overview | retrieve hierarchical community reports, map/reduce with citations |
| DRIFT | exploratory or ambiguous topic | seed with community reports, expand into entities and evidence |
| External | live price/weather/traffic | call a declared tool; never claim the snapshot is current |

Full-text, vector, graph-path, and community sources are fused by rank. Hard
constraints remain gates and are never traded away for semantic similarity.
Generated Cypher is not accepted; the planner emits a validated retrieval mode and
typed parameters consumed by reviewed Cypher templates.

## Evaluation gates

- Entity linking: recall@k, MRR, unresolved precision, and invented-ID rate.
- Retrieval: recall@k and nDCG by mode, hard-constraint violation rate, path
  validity, and evidence coverage.
- Global/DRIFT: claim precision, citation completeness, community coverage,
  comprehensiveness, diversity, latency, and token cost.
- Anti-hardcoding: frozen holdout paraphrases, unseen entities, synonym drift,
  adversarial prompt injection, and score-scale perturbation.
- Release: V8 is published only when it beats V7 on the frozen holdout without
  regressing constraint safety or provenance.

## Primary references

- Microsoft GraphRAG query modes:
  https://microsoft.github.io/graphrag/query/overview/
- Microsoft GraphRAG index and outputs:
  https://microsoft.github.io/graphrag/index/overview/
  https://microsoft.github.io/graphrag/index/outputs/
- Neo4j GraphRAG retrievers:
  https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html
- Edge et al., *From Local to Global: A Graph RAG Approach to Query-Focused
  Summarization*: https://arxiv.org/abs/2404.16130
- Cormack et al., *Reciprocal Rank Fusion Outperforms Condorcet and Individual
  Rank Learning Methods*:
  https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf
- Guo et al., *LightRAG: Simple and Fast Retrieval-Augmented Generation*:
  https://arxiv.org/abs/2410.05779
