# NexTripAI GraphRAG V1 Report

This report captures the current GraphRAG V1 baseline before deeper research iterations. Use it as the reference point for future variants, benchmarks, and improvement notes.

## 1. Current V1 Snapshot

Dataset:

- Raw tracked data: `travel_data/`, 524 places.
- Verified local data: `travel_data_verified/`, 519 places.
- Cities: Quy Nhon and Da Nang.
- Entity types: attraction, cafe, hotel, nightlife, restaurant.

Knowledge graph:

- Node labels: `City`, `Place`, `PlaceType`, `Category`, `Source`, `Term`.
- Extra place labels: `Attraction`, `Cafe`, `Hotel`, `Nightlife`, `Restaurant`.
- Core relationships:
  - `(:City)-[:HAS_PLACE]->(:Place)`
  - `(:Place)-[:IN_CITY]->(:City)`
  - `(:Place)-[:HAS_TYPE]->(:PlaceType)`
  - `(:Place)-[:HAS_CATEGORY]->(:Category)`
  - `(:Place)-[:FROM_SOURCE]->(:Source)`
  - `(:Place)-[:TAGGED_WITH|HAS_AMENITY|HAS_FEATURE|HAS_CUISINE|SERVES|SUITABLE_FOR]->(:Term)`
  - `(:Place)-[:NEAR]->(:Place)`

Retrieval:

- Vector search over `Place.embedding`.
- Keyword fallback over Neo4j fulltext index.
- Graph context expansion for facets and nearby places.
- Gemini embeddings and Gemini answer generation.

Interfaces:

- CLI: `prepare`, `schema`, `load`, `ask`.
- Local API scaffold: `GET /health`, `POST /api/kb/search`, `POST /api/kb/answer`.

## 2. Where V1 Works Well

V1 is a good baseline for:

- Factual retrieval when the user provides city and entity type.
- Simple recommendations such as cafes in Quy Nhon or seafood restaurants in Da Nang.
- Retrieval that benefits from structured facets:
  - cuisine
  - amenities
  - weather suitability
  - suitable audiences
  - tags/features
- Grounded answers from a compact set of retrieved places.
- Local demo because the pipeline is simple and inspectable.

## 3. Known Weaknesses

V1 is not yet an advanced GraphRAG system because:

- Retrieval is vector-first, then keyword fallback. It is not true hybrid score fusion.
- Graph traversal is shallow. The graph currently enriches retrieved nodes rather than driving retrieval.
- There is no graph-first Cypher retrieval for constraints like budget, weather, audience, area, price, rating, or open hours.
- There is no geospatial generated graph beyond available `nearby_attractions`.
- There are no community summaries or higher-level city/category summaries.
- There is no LightRAG-style dual-level retrieval across detailed entities and high-level themes.
- Errors in vector retrieval can be hidden by fallback behavior if not traced.
- There is no benchmark harness, no pass/fail report, and no systematic comparison across variants.
- There is no debug trace for why each place was selected.

## 4. Baseline Limits To Measure

The first benchmark should identify limits across five levels:

- L1 factual retrieval: address, opening hours, rating, ticket price.
- L2 recommendation: top places by city/type.
- L3 personalized recommendation: budget, family, quiet, seafood, indoor/outdoor.
- L4 itinerary support: choosing places for 1-3 day plans.
- L5 complex/multi-hop: weather-aware, nearby constraints, follow-up changes.

For each case, record:

- expected place ids or accepted categories.
- retrieved place ids.
- retrieval method.
- answer groundedness.
- missing or hallucinated facts.
- latency.
- notes for improvement.

## 5. Next GraphRAG Variants

Build these variants as explicit experiments:

1. `keyword_only`
   - Fulltext search only.
   - Useful as a lexical baseline.

2. `vector_only`
   - Semantic vector retrieval only.
   - Current strongest baseline when embeddings are loaded.

3. `hybrid_vector_keyword`
   - Combine vector and fulltext results with score fusion.
   - Expected to improve named-place and food/cuisine queries.

4. `graph_first_structured`
   - Extract filters such as city, type, cuisine, weather, budget, audience.
   - Use Cypher to narrow candidates before semantic rerank.

5. `geo_aware_graph`
   - Generate `NEAR` edges from coordinates.
   - Support area-aware and itinerary-friendly retrieval.

6. `community_summary`
   - Build summaries by city, type, category, and popular facets.
   - Support broad questions and comparison questions.

7. `dual_level_lightrag_style`
   - Retrieve both high-level summaries and low-level places.
   - Combine detailed candidates with thematic context.

## 6. Research Loop

Every improvement must follow this loop:

```txt
hypothesis
-> implement variant
-> run benchmark
-> compare against V1
-> inspect failure cases
-> improve graph/retrieval/prompt
-> rerun benchmark
```

Example hypotheses:

- Hybrid vector + keyword improves exact-place and cuisine queries.
- Graph-first retrieval improves constraint-heavy recommendations.
- Geo-aware edges improve itinerary candidate quality.
- Community summaries improve broad travel advice.

## 7. V1 Verdict

V1 is strong enough as a baseline and demo foundation, but not enough as the final research contribution. The next milestone is not just "make it answer"; it is to make retrieval variants measurable, comparable, and iteratively better for this travel dataset.

