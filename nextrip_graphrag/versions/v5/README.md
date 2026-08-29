# KB V5

Typed-target, geo-aware and reliability-focused GraphRAG.

## V5.1 retrieval update

V5.1 keeps the V5 graph and API contract while versioning the retrieval layer as
`deterministic-resilient-typed-router-v5.1`. High-confidence count, detail,
category-filter, nearby, distance, dynamic-tool, and itinerary-candidate queries
now use a deterministic fast path. Gemini remains available for ambiguous
semantic plans, but provider failure no longer blocks queries covered by the
deterministic contract.

V5 reuses V4's verified Place, Fact, Claim and TextUnit ingestion, then adds
first-class geographic scopes and target-aware retrieval. V4 and V5 use
separate Neo4j databases and can be benchmarked independently.

Concept retrieval uses semantic schema linking: Gemini first creates a typed
plan, exact vocabulary matching runs next, and unknown semantic terms are mapped
through Concept evidence embeddings. Ambiguous vector candidates are passed to
Gemini as a strict ID whitelist; unresolved terms stop retrieval instead of
triggering a broad search.

```powershell
docker compose -f docker-compose.v5.yml up -d
python -m nextrip_graphrag v5-build --processed-dir processed_verified --with-embeddings
python -m nextrip_graphrag v5-validate
python -m nextrip_graphrag v5-query "Tuy Phuoc co gi dac biet?" --with-gemini-planner
```

An address-derived area may create `LOCATED_IN`. An area found only in prose
creates `MENTIONS_GEO_AREA`; it is never silently promoted to a location.
