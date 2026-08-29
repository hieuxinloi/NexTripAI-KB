# KB V4

Ontology-Guided, Evidence-Aware, Query-Adaptive GraphRAG.

V4 builds seven connected domain subgraphs in an isolated Neo4j database:
geographic, offering/food, experience/activity, compatibility/constraint,
temporal/dynamic, provenance, and community/summary.

Descriptions are split into evidence TextUnits. Structured fields and an optional
Gemini extractor produce validated claims with polarity, confidence, and source
evidence. Only non-conflicting positive claims are promoted to traversal edges.

```powershell
docker compose -f docker-compose.v4.yml up -d
python -m nextrip_graphrag v4-build --processed-dir processed_verified --with-embeddings
python -m nextrip_graphrag v4-validate
python -m nextrip_graphrag v4-query "Goi y nha hang hai san cho gia dinh o Quy Nhon"
```

Add `--with-description-extraction` to `v4-build` to run Gemini extraction. Results
are cached under `tmp/v4_description_cache`; deterministic extraction remains
available for offline rebuilds.
