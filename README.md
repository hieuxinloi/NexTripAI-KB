# NexTrip GraphRAG

This repo is the local Knowledge Graph and GraphRAG service for NexTripAI. It owns verified travel data, Neo4j graph loading, retrieval, and evidence returned to the backend.

## Workflow Documentation

Read this before implementation:

- Repo workflow: [docs/WORKFLOW.md](docs/WORKFLOW.md)
- GraphRAG V1 report: [docs/GRAPHRAG_V1_REPORT.md](docs/GRAPHRAG_V1_REPORT.md)
- System workflow: [../docs/WORKFLOW.md](../docs/WORKFLOW.md)
- Repo structure guide: [../docs/REPO_STRUCTURE.md](../docs/REPO_STRUCTURE.md)
- Step-by-step roadmap: [../docs/IMPLEMENTATION_STEPS.md](../docs/IMPLEMENTATION_STEPS.md)

## Data Source

Use only `travel_data_verified/` as the source of truth. Legacy raw dataset folders are intentionally removed to avoid mixing raw and verified datasets.

Current verified dataset:

- total: 519 places
- attraction: 118
- cafe: 106
- hotel: 73
- nightlife: 39
- restaurant: 183

Processed verified output lives in `processed_verified/`.

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
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Update `.env` with Neo4j and Gemini/Vertex AI settings.

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

## Run KB API

```powershell
uvicorn nextrip_graphrag.api.app:app --reload --port 8010
```

Endpoints:

- `GET /health`
- `POST /api/kb/search`
- `POST /api/kb/answer`

## Ask From CLI

```powershell
python -m nextrip_graphrag ask "Goi y 3 quan cafe o Quy Nhon" --city "Quy Nhon" --type cafe
python -m nextrip_graphrag ask "O Da Nang co nha hang hai san nao phu hop gia dinh?" --city "Da Nang" --type restaurant
```

## Notes

- Gemini SDK uses `google-genai`.
- Default embedding model: `gemini-embedding-001`.
- Neo4j uses fulltext and vector indexes for retrieval.
