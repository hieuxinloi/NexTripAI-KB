# V8 Gemini semantic embeddings

The V8 embedding job is release-aware. It refuses to run unless the active
Neo4j `DatasetRelease` has the same dataset ID and hash as the pinned canonical
JSON file. It covers three semantic families:

- `Place` using the canonical semantic profile;
- `Concept` using its canonical name, type, and domain;
- `TextUnit` using the verified canonical evidence text.

Each target carries a deterministic `semantic_content_hash`. A vector is reused
only when its model, dimension, and content hash still match. Missing or changed
targets use the content-addressed disk cache before calling Gemini. Partial runs
remain resumable and keep `semantic_index_status=pending`; the job marks the
active release and catalog `ready` only after all three families reach 100%
coverage.

## Offline plan

Planning reads Neo4j and canonical JSON but does not call Gemini or write graph
data:

```powershell
python -m nextrip_graphrag.v8_embedding_cli `
  --canonical-dataset data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json
```

## Apply manually

Configure either `GOOGLE_API_KEY` or `GEMINI_API_KEY`, then run:

```powershell
python -m nextrip_graphrag.v8_embedding_cli `
  --canonical-dataset data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json `
  --model gemini-embedding-001 `
  --dimension 1536 `
  --cache-root data/cache/v8_embeddings `
  --output-root data/neo4j/v8/embedding_runs `
  --batch-size 16 `
  --request-delay 2 `
  --max-retries 5 `
  --apply
```

Every successful attempt writes an immutable summary beneath
`data/neo4j/v8/embedding_runs/run=<run-id>/summary.json`.

## Airflow rollout

After configuring the Gemini key, enable:

```text
NEXTRIP_V8_EMBEDDING_ENABLED=true
GEMINI_EMBEDDING_MODEL=gemini-embedding-001
NEXTRIP_V8_EMBEDDING_DIMENSION=1536
```

The canonical rollout then runs:

```text
activate release
  -> publish observations
  -> embed active canonical release
  -> verify 100% coverage
  -> semantic_index_status=ready
```

Canonical activation is not rolled back when Gemini is unavailable. Airflow
retries the embedding task with a new attempt/run ID, while completed vectors
are recovered from Neo4j or the content-addressed cache.
