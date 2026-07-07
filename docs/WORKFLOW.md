# NexTripAI-KB Repo Workflow

Repo nay la Knowledge Base cua NexTripAI. Nhiem vu chinh la bien du lieu du lich thanh Knowledge Graph, truy van GraphRAG va tra evidence cho backend.

Doc chung:

- System workflow: `../docs/WORKFLOW.md`
- Repo structure: `../docs/REPO_STRUCTURE.md`
- Roadmap chung: `../docs/IMPLEMENTATION_STEPS.md`
- GraphRAG V1 report: `docs/GRAPHRAG_V1_REPORT.md`

## 1. Role In System

`NexTripAI-KB` phu trach:

- Luu raw/verified travel data.
- Clean va normalize data.
- Tao processed files.
- Load Neo4j Knowledge Graph.
- Tao fulltext/vector indexes.
- Truy van candidates/evidence cho backend.
- Optional answer generation de smoke test GraphRAG doc lap.
- Research variants de tim GraphRAG tot nhat cho travel data.
- Benchmark va report limit/cai tien qua tung version.

`NexTripAI-KB` khong phu trach:

- Chat session cua user.
- Orchestrator Agent.
- Weather Agent.
- FE rendering.
- Booking/payment.

## 2. Current Assets

Hien tai repo co:

- `travel_data/`: raw tracked data, 524 places.
- `travel_data_verified/`: verified local data, 519 places neu co tren may.
- `processed/`: processed output tu raw data.
- `processed_verified/`: processed output tu verified data.
- `nextrip_graphrag/`: CLI va GraphRAG core.
- `.env.example`: config Neo4j + Gemini/Vertex AI.
- `docs/GRAPHRAG_V1_REPORT.md`: note V1 manh/yeu/limit va huong cai tien.

Core commands:

- `prepare`
- `schema`
- `load`
- `ask`

Local API:

- `GET /health`
- `POST /api/kb/search`
- `POST /api/kb/answer`

Run API:

```powershell
uvicorn nextrip_graphrag.api.app:app --reload --port 8010
```

## 2.1 Research Direction

Tu luc nay KB khong chi la data service. KB la GraphRAG research lab:

1. Giu V1 baseline chay duoc.
2. Tao benchmark L1-L5.
3. Thu nhieu retrieval variants:
   - keyword only.
   - vector only.
   - hybrid vector + keyword.
   - graph-first Cypher.
   - geo-aware graph.
   - community summary.
   - dual-level LightRAG-style.
4. Ghi report so sanh va failure cases.
5. Cai tien graph construction/retrieval/prompt theo evidence benchmark.

## 3. Step 1 - Check Data Source

Uu tien dung verified data cho demo:

```powershell
Test-Path .\travel_data_verified
```

Neu co:

```powershell
$DATA_DIR = "travel_data_verified"
$OUT_DIR = "processed_verified"
```

Neu khong:

```powershell
$DATA_DIR = "travel_data"
$OUT_DIR = "processed"
```

Expected counts for verified data:

- total: 519
- attraction: 118
- cafe: 106
- hotel: 73
- nightlife: 39
- restaurant: 183

Expected counts for raw tracked data:

- total: 524
- attraction: 118
- cafe: 106
- hotel: 73
- nightlife: 39
- restaurant: 188

## 4. Step 2 - Prepare Processed Data

Run:

```powershell
.\.venv\Scripts\Activate.ps1
python -m nextrip_graphrag prepare --data-dir travel_data_verified --out-dir processed_verified
```

Output bat buoc:

```txt
processed_verified/
  cities.json
  places.jsonl
  manifest.json
```

Kiem tra nhanh:

```powershell
Get-Content .\processed_verified\manifest.json
```

## 5. Step 3 - Run Neo4j Local

Dung Docker neu co:

```powershell
docker run --name nextrip-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/change-me neo4j:5
```

Kiem tra port:

```powershell
Test-NetConnection -ComputerName localhost -Port 7687
```

Update `.env`:

```dotenv
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=change-me
NEO4J_DATABASE=neo4j
```

## 6. Step 4 - Create Graph Schema

Run:

```powershell
python -m nextrip_graphrag schema
```

Schema creates:

- unique constraints for city/place/category/type/source/term.
- fulltext index for place text search.
- vector index for place embeddings.

## 7. Step 5 - Load Graph

Fast dev load, no embedding:

```powershell
python -m nextrip_graphrag load --processed-dir processed_verified
```

Demo GraphRAG load with embeddings:

```powershell
python -m nextrip_graphrag load --processed-dir processed_verified --with-embeddings
```

Use `--with-embeddings` only when Gemini/Vertex AI credentials are ready because it calls the model for many places.

## 8. Step 6 - Test Retrieval

Examples:

```powershell
python -m nextrip_graphrag ask "Goi y 3 quan cafe o Quy Nhon" --city "Quy Nhon" --type cafe
python -m nextrip_graphrag ask "O Da Nang co nha hang hai san nao phu hop gia dinh?" --city "Da Nang" --type restaurant
python -m nextrip_graphrag ask "Quy Nhon nen di bien nao vao buoi sang?" --city "Quy Nhon" --type attraction
```

Expected:

- Answer dung city/type.
- Co dia diem cu the.
- Khong invent dia diem ngoai graph.
- Neu thieu data thi noi ro thieu.

## 9. Step 7 - Add Local KB API

API local da duoc scaffold de BE goi qua HTTP. Tiep theo la lam no on dinh voi live Neo4j/Gemini va benchmark.

Target endpoints:

- `GET /health`
- `POST /api/kb/search`
- `POST /api/kb/answer`

Target folders:

```txt
nextrip_graphrag/
  api/
    app.py
    router.py
    schemas.py
```

`/api/kb/search` response item:

```json
{
  "place_id": "cafe_qn_001",
  "name": "Example Cafe",
  "city": "Quy Nhon",
  "entity_type": "cafe",
  "category": "work_cafe",
  "score": 0.88,
  "reason": "Phu hop nhu cau cafe lam viec",
  "source": {
    "name": "travel_data_verified",
    "url": null
  },
  "graph_context": {
    "facets": ["wifi"],
    "nearby": []
  }
}
```

## 10. Refactor Direction

Khong can doi het ngay. Khi code lon hon, tach dan:

```txt
nextrip_graphrag/
  ingestion/
  processing/
  graph/
  retrieval/
  api/
  evaluation/
```

LangGraph khong bat buoc trong KB V1 vi flow hien tai tuyen tinh: prepare -> schema -> load -> retrieve.

## 11. Tests

Can co:

- `prepare` tao du 3 output files.
- `manifest` counts dung.
- graph schema commands co the call duoc voi mock/live Neo4j.
- retrieval format stable.
- KB API app load duoc khi them API.

