# GraphRAG V5.1 Benchmark Report

Date: 2026-07-25

## Scope and scoring

- Source: `../docs/test_cases_benchmark_v3 (1).md`
- Dataset: 500 cases, including the common comparison set of 400 cases
- Graph: local Neo4j V5, 519 verified places
- Baseline: the V5 production fallback path with an offline fail-fast planner
- Final: V5.1 `deterministic-resilient-typed-router-v5.1`
- Scoring: deterministic response-contract checks by case family; no LLM judge
- Raw response, actual-output summary, status, and failure reason are retained for
  every case in the JSON result files.

The baseline result was rejudged after fixing an evaluator ambiguity: named
rating and price questions containing “bao nhiêu” are detail lookups, not
aggregate-count questions. Raw baseline responses were not changed.

## Result

| Scope | V5 baseline | V5.1 final | Improvement |
|---|---:|---:|---:|
| All 500 cases | 148 / 500 (29.6%) | 331 / 500 (66.2%) | +183 pass, +36.6 pp |
| Common 400 cases | 132 / 400 (33.0%) | 268 / 400 (67.0%) | +136 pass, +34.0 pp |

| Group | Baseline pass | V5.1 pass | Final fail | Pass delta |
|---|---:|---:|---:|---:|
| A. Information lookup | 105 | 116 | 4 | +11 |
| B. Filter and recommend | 19 | 91 | 9 | +72 |
| C. Relationship and reasoning | 0 | 75 | 5 | +75 |
| D. Itinerary planning | 0 | 0 | 80 | 0 |
| E. Multi-turn conversation | 0 | 0 | 60 | 0 |
| F. Hard, incomplete, regression cases | 24 | 49 | 11 | +25 |

## V5.1 changes

1. Added a deterministic high-confidence planner path for aggregate counts,
   named detail fields, typed recommendations, categories, constraints, nearby
   search, direct distances, dynamic-tool routing, and itinerary candidates.
2. Added hard category filtering and low-price ranking.
3. Added `NEAR`-based nearby retrieval with `distance_km`.
4. Added direct two-place distance facts and matched paths.
5. Added clarification and safe unsupported paths instead of returning a
   retryable planner error for requests that can be classified locally.
6. Added dish-list and noisy city/type aliases for Vietnamese travel queries.
7. Fixed Lucene full-text sanitization while preserving raw exact-name lookup.
   This fixes names containing reserved characters, including
   `Hilton Da Nang:`.
8. Added an executable 500-case benchmark command and deterministic rejudge
   support.

## Remaining difficulties

- D remains 0/80 because V5.1 retrieves grounded candidates but does not contain
  a day-by-day scheduler that enforces time, travel order, exclusions, and
  per-day load.
- E remains 0/60 because the KB query service is stateless. Thirty cases lose
  city context explicitly; the rest require references to prior choices or
  updates to an earlier result.
- Nine B cases have no grounded recommendations for view, relaxation,
  photography, or group-suitability constraints. The graph lacks consistent
  claims for these facets.
- Five C cases have no usable `NEAR` candidate from the named anchor and
  requested category.
- Four A cases request per-person restaurant prices that are absent from the
  verified facts.
- Remaining hard cases include unresolved aliases, unsupported live-price or
  booking checks, and ambiguous navigation references.

## Recommended V5.2 work

1. Add a grounded itinerary scheduler over V5.1 candidates with opening-hours,
   duration, distance, exclusions, and per-day capacity checks.
2. Move multi-turn context resolution into the benchmarked end-to-end service,
   including ordinal references such as “phương án thứ hai”.
3. Enrich and verify price, sea-view, relaxation, photography,
   group-suitability, and nearby-edge data.
4. Add alias clusters for major hotels, attractions, and noisy Vietnamese
   shorthand before using semantic fallback.
