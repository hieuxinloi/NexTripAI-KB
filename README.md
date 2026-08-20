# NexTrip GraphRAG

This repo is the local Knowledge Graph and GraphRAG service for NexTripAI. It owns verified travel data, Neo4j graph loading, retrieval, and evidence returned to the backend.

## Workflow Documentation

Read this before implementation:

- Repo workflow: [docs/WORKFLOW.md](docs/WORKFLOW.md)
- GraphRAG V1 report: [docs/GRAPHRAG_V1_REPORT.md](docs/GRAPHRAG_V1_REPORT.md)
- Versioning V1-V5: [docs/GRAPHRAG_VERSIONING.md](docs/GRAPHRAG_VERSIONING.md)
- V1 retrieval experiment: [docs/GRAPHRAG_V1_RETRIEVAL_EXPERIMENT.md](docs/GRAPHRAG_V1_RETRIEVAL_EXPERIMENT.md)
- Data enrichment workflow: [docs/DATA_ENRICHMENT.md](docs/DATA_ENRICHMENT.md)
- Data enrichment run report: [docs/DATA_ENRICHMENT_RUN_REPORT.md](docs/DATA_ENRICHMENT_RUN_REPORT.md)
- Logging: [docs/LOGGING.md](docs/LOGGING.md)
- Hybrid traffic pipeline: [docs/traffic-pipeline.md](docs/traffic-pipeline.md)
- Current Data HTTP/MCP facade: [docs/current-data-mcp.md](docs/current-data-mcp.md)

## Data Source

Use only `travel_data_verified/` as the source of truth. Legacy raw dataset folders are intentionally removed to avoid mixing raw and verified datasets.

Current verified dataset:

- total: 692 places
- attraction: 118
- cafe: 106
- hotel: 73
- nightlife: 195
- restaurant: 200

Processed verified output lives in `processed_verified/`.

Build staged provenance and address candidates without changing verified JSON:

```powershell
python -m nextrip_graphrag build-source-artifacts
python -m nextrip_graphrag crawl-sources
python -m nextrip_graphrag enrich-addresses
```

## Graph Schema

```text
(:City)-[:HAS_PLACE]->(:Place)
(:Place)-[:IN_CITY]->(:City)
(:City)-[:HAS_PLACE_TYPE]->(:PlaceType)-[:CONTAINS_PLACE]->(:Place)
(:Place)-[:HAS_TYPE]->(:PlaceType)
(:Place)-[:HAS_CATEGORY]->(:Category)
(:Place)-[:TAGGED_WITH|HAS_AMENITY|HAS_FEATURE|HAS_CUISINE|SERVES|SUITABLE_FOR]->(:Term)
(:Place)-[:FROM_SOURCE]->(:Source)
(:Place)-[:NEAR {distance_km}]->(:Place)
(:Document)-[:HAS_TEXT_UNIT]->(:TextUnit)
(:Document)-[:SOURCE_FOR]->(:Place)
(:TextUnit)-[:MENTIONS {confidence, match_type}]->(:Place)
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Update `.env` with Neo4j and a Gemini Developer API key from Google AI Studio.

Each configured typed version requires its own complete `NEO4J_V{N}_*` block.
V8 does not inherit or reuse V5. Set `ACTIVE_KB_VERSION` to the deployment that
should serve new requests. The private deployment administration endpoints
require `KB_ADMIN_API_KEY`.

Run Neo4j locally:

```powershell
docker run --name nextrip-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/your-password neo4j:5
```

## Prepare Verified Data

Defaults already point to verified folders:

```powershell
python -m nextrip_graphrag prepare
```

Equivalent explicit command:

```powershell
python -m nextrip_graphrag prepare --data-dir travel_data_verified --out-dir processed_verified
```

Output:

- `processed_verified/cities.json`
- `processed_verified/places.jsonl`
- `processed_verified/manifest.json`

## Load Neo4j

Create constraints and indexes:

```powershell
python -m nextrip_graphrag schema
```

Fast load without embeddings:

```powershell
python -m nextrip_graphrag load
```

GraphRAG demo load with Gemini embeddings:

```powershell
python -m nextrip_graphrag load --with-embeddings
```

Build and load the provenance evidence graph:

```powershell
python -m nextrip_graphrag build-source-artifacts
python -m nextrip_graphrag crawl-sources
python -m nextrip_graphrag build-article-text-units
python -m nextrip_graphrag load-evidence --with-embeddings
```

## Run KB API

```powershell
uvicorn nextrip_graphrag.api.app:app --reload --port 8010
```

Endpoints:

- `GET /live` and `GET /ready?version=v3`
- `GET /health`
- `POST /api/kb/search`
- `POST /api/kb/answer`

The KB Cloud Run service is intended to be private. Grant `roles/run.invoker` only to the BE
runtime service account. Configure `KB_ADMIN_API_KEY` before using the mutating V4 observation API.

API requests that omit `strategy` use `v1_provenance`. Pass `v1` or `v1_hybrid` explicitly for
baseline experiments.

## Ask From CLI

```powershell
python -m nextrip_graphrag ask "Goi y 3 quan cafe o Quy Nhon" --city "Quy Nhon" --type cafe
python -m nextrip_graphrag ask "O Da Nang co nha hang hai san nao phu hop gia dinh?" --city "Da Nang" --type restaurant
```

Compare versioned retrieval strategies:

```powershell
python -m nextrip_graphrag search "dia diem trong nha khi troi mua" --city "Da Nang" --type attraction --strategy v1
python -m nextrip_graphrag search "dia diem trong nha khi troi mua" --city "Da Nang" --type attraction --strategy v1_hybrid
python -m nextrip_graphrag benchmark --strategy v1
python -m nextrip_graphrag benchmark --strategy v1_hybrid
python -m nextrip_graphrag search "dia diem ngam canh song ve dem" --city "Da Nang" --type attraction --strategy v1_provenance
python -m nextrip_graphrag benchmark --strategy v1_provenance
```

## Notes

- Gemini SDK uses `google-genai`.
- Default embedding model: `gemini-embedding-001`.
- Neo4j uses fulltext and vector indexes for retrieval.
