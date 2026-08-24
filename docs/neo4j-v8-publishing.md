# Neo4j V8 canonical and observation publishing

V8 has two deliberately separate write paths:

1. A canonical release replaces the active static view only after the dataset,
   readiness report, and completeness audit agree and all blocking gates pass.
2. Scheduled observations append hotel price, availability, daily opening
   status, and human-approved menu data. Existing observation values are never
   overwritten. Traffic stays request-time data and is never written to Neo4j.

Neither command calls an LLM. Dry-run is the default and does not connect to
Neo4j. Always pin exact immutable artifact paths; do not select a release by a
hard-coded expected place count.

## Validate a canonical release offline

```powershell
$dataset = "data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json"
$readiness = "data/canonical/readiness/readiness=<readiness-id>/canonical-dataset-readiness.json"
$audit = "data/canonical/completeness/audit=<audit-id>/canonical-completeness-audit.json"

python -m nextrip_graphrag v8-import-canonical `
  --canonical-dataset $dataset `
  --readiness $readiness `
  --completeness-audit $audit
```

The command writes an immutable release manifest under
`data/neo4j/v8/releases/`. A successful dry-run has `status=validated`. The
typed/full-text static graph is ready immediately. Empty 1536-dimensional
vector indexes are created on apply, but `semantic_index_status` remains
`pending` until a separate, explicitly budgeted embedding job is run.

## Activate the release

Start the isolated V8 container described in `neo4j-v8-local.md`, then rerun the
same command with `--apply`:

```powershell
python -m nextrip_graphrag v8-import-canonical `
  --canonical-dataset $dataset `
  --readiness $readiness `
  --completeness-audit $audit `
  --apply
```

Only `NEO4J_V8_URI`, `NEO4J_V8_USER`, `NEO4J_V8_PASSWORD`, and
`NEO4J_V8_DATABASE` are used. Place IDs remain the canonical IDs. A new release
creates immutable `PlaceVersion` and `CanonicalSourceProvenance` nodes, then
atomically moves the active static view. Older versions and observations remain
available for audit.

Graph labels use business names such as `Place`, `CanonicalPlace`, `Fact`,
`Claim`, and `Observation`; they do not include a version prefix. The
`kb_version="v8"` property, release IDs, environment variable names, and CLI
names retain `v8` so the deployment remains version-isolated.

## Migrate an existing V8 graph to neutral labels

Fresh databases published by the current code already use neutral labels and
do not need this migration. For an existing database, pause canonical imports
and the observation publisher first. Keep an Aura snapshot as the rollback
point.

Inspect the connected graph without changing it:

```powershell
python -m nextrip_graphrag v8-migrate-labels
```

Apply the resumable migration after the read-only report is correct:

```powershell
python -m nextrip_graphrag v8-migrate-labels --batch-size 500 --apply
```

The command installs replacement labels and schema first, validates complete
coverage, then removes the legacy `V8*` labels and old `v8_*` schema objects.
It also verifies that the database is V8-isolated, the active release and
vector dimension are stable, neutral indexes are online, and node or
relationship counts do not change.

Verify that no legacy labels remain:

```cypher
MATCH (node)
UNWIND labels(node) AS label
WITH label, count(*) AS count
WHERE label STARTS WITH 'V8'
RETURN label, count;
```

## Validate and append operational observations

```powershell
python -m nextrip_graphrag v8-publish-observations `
  --canonical-dataset $dataset
```

This reads daily Google opening evidence directly from the pinned canonical
dataset and scans these append-only contextual/manual observation stores:

- `data/current/hotel_price`
- `data/current/hotel_availability`
- `data/current/menu`

Legacy place projections and the `current_place` artifact family are not
accepted. Place identity, business/opening facts and daily opening observations
come only from the exact canonical dataset passed to `--canonical-dataset`; an
invalid or missing canonical artifact fails the plan instead of falling back.

Add `--apply` only after the matching canonical release is active. Plans and
run manifests are written under `data/neo4j/v8/observation_runs/`. Repeating an
apply is idempotent; later crawl observations get new deterministic IDs and do
not replace older nodes.

## Airflow

`dags/nextrip_neo4j_v8.py` publishes observations only. Static canonical
activation remains a manual gated operation. Set the following in the Airflow
scheduler and worker environment after V8 is active:

```dotenv
NEXTRIP_NEO4J_V8_OBSERVATIONS_ENABLED=true
NEXTRIP_NEO4J_V8_OBSERVATIONS_SCHEDULE="*/15 * * * *"
NEXTRIP_CANONICAL_DATASET=data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json
```

The DAG is disabled by default and fails closed when the pinned dataset or V8
credentials are missing.
