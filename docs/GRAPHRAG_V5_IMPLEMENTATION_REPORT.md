# GraphRAG V5 Implementation Report

## Status

V5 is `experimental`. The first end-to-end foundation is implemented and uses
an isolated Neo4j database on HTTP `7478` and Bolt `7691`.

Implemented:

- typed query targets: Place, City, GeoArea, Dish, Activity and Concept;
- Gemini structured query plan with graph-vocabulary validation;
- label-guarded entity resolver;
- evidence-enriched Concept embeddings and semantic schema linking;
- LLM selection restricted to top semantic Concept IDs when vector candidates
  are ambiguous;
- city-scoped concept retrieval;
- conservative GeoArea extraction with separate `LOCATED_IN` and
  `MENTIONS_GEO_AREA` relationships;
- target-level TextUnit/Document citations;
- planner timeout, SDK retry policy and explicit error taxonomy;
- persistent query-embedding cache with an in-process stampede lock;
- KB API, BE bridge and FE V5 selector;
- V5 CLI build/query/validate commands and isolated Docker runtime.

## Live build result

Build source: `processed_verified`, derived only from `travel_data_verified`.

| Metric | Count |
|---|---:|
| Place | 519 |
| Fact | 8,269 |
| TextUnit | 2,871 |
| Concept | 282 |
| Claim | 2,420 |
| Offering | 401 |
| Ontology edges | 2,369 |
| GeoArea | 43 |
| Grounded `LOCATED_IN` | 3 |
| `MENTIONS_GEO_AREA` | 105 |
| Embedded Place | 519 |
| Embedded Concept | 282 |

All V5 graph invariants pass. Every location edge has confidence, extraction
method and evidence ID. Description mentions point to the matching description
TextUnit instead of the place-level aggregate TextUnit.

## Verified retrieval probes

- `Tuy Phước` resolves as `GeoArea`, not Place or City.
- The area summary returns `Chùa Bà Nước Mặn` through a grounded description
  mention and includes the supporting source TextUnit.
- Dish listing is scoped by city and only accepts Dish claims supported by
  restaurant entities; polluted cafe n-grams are excluded.
- Dynamic queries can return `tool_required` without querying Neo4j.
- `nghỉ ngơi` retrieves semantically related Concept candidates such as
  `yên tĩnh`, `ngắm cảnh`, `chill` and spa; ambiguous candidates require an LLM
  whitelist selection.
- When a typed graph path has no Place candidate, V5 may use Place semantic
  fallback only with city/type filters and no unsupported hard constraints.

## Current limits

1. Semantic linking quality still requires a labelled synonym/schema-linking
   benchmark. A successful mapping is not automatically a correct travel intent.
2. Gemini planner and candidate selection are provider-dependent. V5 applies
   timeout/retry and returns `planner_unavailable` when the provider is unavailable.
   The first complete embedding rebuild hit the Vertex online-prediction quota
   immediately afterward; runtime query vectors are now cached to reduce repeated
   quota usage, but project quota still limits uncached traffic.
3. Only three location edges are safe enough to promote from current verified
   fields. The other geographic occurrences remain mentions by design.
4. Communities are still the deterministic V4 taxonomy baseline. Learned
   multi-level communities, grounded reports, Global Search and DRIFT-style
   expansion are not implemented yet.
5. Source `signature_dishes` contains extraction noise. V5 filters obvious
   cross-type pollution during retrieval, but the upstream extraction dataset
   still requires a reviewed correction pipeline.
6. V5 has not yet been scored against the normalized KB-owned benchmark.

## Next gate

Before V5 can become a baseline:

1. create a labelled semantic-linking set with paraphrases and expected Concept IDs;
2. add a reviewable GeoArea audit artifact before promoting more locations;
3. normalize benchmark ownership, target type and data availability;
4. run V4/V5 on the same KB-owned cases;
5. implement multi-level community reports only after local typed retrieval is
   measurable and stable.
