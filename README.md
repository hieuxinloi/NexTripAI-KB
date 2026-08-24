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
- Neo4j V8 local infrastructure: [docs/neo4j-v8-local.md](docs/neo4j-v8-local.md)
- Neo4j V8 canonical/observation publishing: [docs/neo4j-v8-publishing.md](docs/neo4j-v8-publishing.md)

## Data Source

The publish-ready immutable dataset selected by `NEXTRIP_CANONICAL_DATASET` is
the sole production authority for `Place` identity and static facts. Neo4j V8,
traffic access points and Current Data must all read that exact artifact and
fail closed when it is absent or invalid.

Raw crawl records, normalized observations, validation/decision outputs,
review backlogs and generated registries are evidence or derived artifacts.
They never become an alternative serving source and cannot bypass the
canonical readiness gate. Canonical cardinality is not fixed: an approved new
place may be added and a retired identity may be removed in a later immutable
version.

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

Run the isolated Neo4j V8 database without changing V1-V7:

```powershell
docker-compose --env-file .env -f docker-compose.v8.yml up -d
docker-compose --env-file .env -f docker-compose.v8.yml ps
```

V8 uses Browser `http://localhost:7479`, Bolt `bolt://localhost:7692`, and its
own named data/log volumes. See [docs/neo4j-v8-local.md](docs/neo4j-v8-local.md)
for validation, health, and shutdown commands.

The new V8 database is loaded from the gated canonical dataset, not by copying
V5. `v8-import-canonical` validates offline by default and requires an explicit
`--apply` to activate a release. Scheduled price, availability, opening, and
approved-menu evidence is appended with `v8-publish-observations`; traffic is
kept outside Neo4j. See [docs/neo4j-v8-publishing.md](docs/neo4j-v8-publishing.md).

## Historical V1-V7 Data Preparation

The repository no longer contains the historical verified source snapshot.
Only its processed artifacts and version history are retained for V1-V7
reproducibility; neither is a V8/current-pipeline source.

The historical verified seed snapshot contained 692 places: 118 attractions,
106 cafes, 73 hotels, 195 nightlife places and 200 restaurants. V1-V5 used a
separate processed bundle derived from that seed.

Do not run `prepare` without arguments: its legacy default source path has been
removed. Rebuilding a historical bundle requires a separately supplied snapshot
outside the repository and an explicit output directory:

```powershell
$legacyImport = "<legacy-import-dir>"
$legacyOutput = "<legacy-output-dir>"
python -m nextrip_graphrag prepare `
  --data-dir $legacyImport `
  --out-dir $legacyOutput
```

Output:

- `<legacy-output-dir>/cities.json`
- `<legacy-output-dir>/places.jsonl`
- `<legacy-output-dir>/manifest.json`

## Historical V1-V7 Neo4j Load

The commands in this section belong to the legacy version workflow. Use
`v8-import-canonical` for the active V8 graph.

Create constraints and indexes:

```powershell
python -m nextrip_graphrag schema
```

Fast load without embeddings:

```powershell
python -m nextrip_graphrag load --processed-dir processed_verified
```

GraphRAG demo load with Gemini embeddings:

```powershell
python -m nextrip_graphrag load `
  --processed-dir processed_verified `
  --with-embeddings
```

Build and load the provenance evidence graph:

```powershell
$legacyImport = "<legacy-import-dir>"
$legacyWorkspace = "<legacy-workspace-dir>"
python -m nextrip_graphrag build-source-artifacts `
  --data-dir $legacyImport `
  --workspace $legacyWorkspace
python -m nextrip_graphrag crawl-sources `
  --data-dir $legacyImport `
  --workspace $legacyWorkspace
python -m nextrip_graphrag build-article-text-units `
  --workspace $legacyWorkspace
python -m nextrip_graphrag load-evidence `
  --workspace $legacyWorkspace `
  --with-embeddings
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
