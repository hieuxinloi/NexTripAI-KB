# Canonical-only Google Maps Airflow pipeline

The scheduled Google Maps flow has one place-data source of truth: an immutable
canonical dataset selected through `NEXTRIP_CANONICAL_DATASET`. The verified raw
master files are not read by this DAG, and the legacy current-place directory is
not a serving or publication sink.

The scheduled scope is limited to four non-hotel entity types:

- `attraction`
- `cafe`
- `nightlife`
- `restaurant`

Hotels remain owned by the Trivago branch. Menu discovery, OCR, and approval are
not scheduled in this DAG.

## End-to-end flow

```text
NEXTRIP_CANONICAL_DATASET
          |
          v
build canonical Google Maps registry
          |
          +----------------+----------------+----------------+
          |                |                |                |
          v                v                v                v
   attraction batch    cafe batch     nightlife batch  restaurant batch
       run_id A          run_id B          run_id C         run_id D
          |                |                |                |
          +----------------+----------------+----------------+
                           | four exact run IDs via XCom
                           v
apply-google-maps-canonical-refresh
          |
          +--> immutable Google patch
          +--> immutable canonical dataset version
          +--> canonical readiness report
          +--> cafe/restaurant manual-menu backlog
```

Only evidence from the four immediately preceding entity batches can enter the
refresh patch. Every branch owns an attempt-specific ID with this shape:

```text
google-maps-place-<airflow-run-id>-<entity-type>-try<try-number>
```

The CLI receives it through `batch-google-maps --run-id`, verifies that the
returned ID matches, and emits the ID as the task's final stdout/XCom value. A
retry gets a new try number, so it cannot collide with the immutable summary or
evidence from an earlier attempt. The apply task waits for all four branches and
fails unless all four XCom values are non-empty and unique. It then passes them
as repeated `--run-id` arguments; older observations cannot be selected
accidentally.

The apply task emits the new content-addressed canonical dataset path as its final
stdout line. That path is the task's XCom return value. Promotion of this candidate
to the deployment's next `NEXTRIP_CANONICAL_DATASET` value and Neo4j publication
must be explicit; the DAG does not mutate an environment variable or overwrite an
older canonical artifact.

## Required environment

```text
NEXTRIP_CANONICAL_DATASET=/opt/airflow/nextrip/data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json
NEXTRIP_KB_ROOT=/opt/airflow/nextrip
```

Optional settings:

```text
NEXTRIP_MAPS_DAILY_LIMIT=10000
NEXTRIP_MAPS_NORMALIZED_ROOT=data/normalized
NEXTRIP_MAPS_DECISION_ROOT=data/decisions
NEXTRIP_MAPS_SCRATCH_ROOT=/tmp/nextrip-google-maps
```

The high default request ceiling applies independently to each entity branch,
selects every eligible canonical mapping, and does not encode a fixed expected
place count. Lower the limit only when a bounded partial refresh is intentional.

The normalized and decision roots must be persistent and shared by the crawl and
apply tasks. The scratch root is run-scoped and is used only to satisfy legacy
quality-pipeline projection interfaces. It is not read by the canonical refresh
and must not be exposed as application data.

## Derived canonical registry

The DAG regenerates the registry from the pinned canonical snapshot:

```bash
python -m nextrip_pipeline.cli build-google-maps-registry \
  --canonical-dataset "$NEXTRIP_CANONICAL_DATASET" \
  --output config/generated/canonical-google-maps-mapping-registry.json \
  --report config/generated/canonical-google-maps-registry-report.json \
  --batch-manifest-output config/generated/canonical-google-maps-batch-manifest.json
```

The generated manifest is the only manifest used by the scheduled place batch.
It contains active non-hotel identities from the selected canonical dataset and
carries the canonical dataset ID in each mapping. Production deliberately omits
`--base-manifest`: an older generated registry is a cache, not an identity
authority, and therefore cannot inject stale URLs or rejected mappings into a
new crawl cycle. The optional flag remains available for a controlled reuse of a
derived cache after its dataset lineage and identities have been audited.

## Manual canonical refresh

For a manual run, execute one independently named batch per entity type:

```powershell
$cycle = "manual-" + [guid]::NewGuid().ToString("N")
$runIds = @()

foreach ($entityType in @("attraction", "cafe", "nightlife", "restaurant")) {
  $sourceRunId = "google-maps-place-$cycle-$entityType"
  $scratch = "data/tmp/google-maps-manual/$sourceRunId"

  python -m nextrip_pipeline.cli batch-google-maps `
    --manifest config/generated/canonical-google-maps-batch-manifest.json `
    --mode place `
    --entity-type $entityType `
    --run-id $sourceRunId `
    --max-requests 10000 `
    --current-mapping-dir "$scratch/current-mappings" `
    --menu-source-dir "$scratch/menu-sources" `
    --current-menu-dir "$scratch/current-menu"

  if ($LASTEXITCODE -ne 0) { throw "Google Maps batch failed: $entityType" }
  $runIds += $sourceRunId
}
```

Then apply exactly those four runs in one operation:

```powershell
$applyArgs = @(
  "apply-google-maps-canonical-refresh",
  "--canonical-dataset", $env:NEXTRIP_CANONICAL_DATASET,
  "--observation-root", "data/normalized",
  "--decision-root", "data/decisions",
  "--entity-type", "attraction",
  "--entity-type", "cafe",
  "--entity-type", "nightlife",
  "--entity-type", "restaurant"
)
foreach ($sourceRunId in $runIds) {
  $applyArgs += @("--run-id", $sourceRunId)
}
python -m nextrip_pipeline.cli @applyArgs
```

The command preserves existing canonical values for missing, review, or
quarantined observations. Accepted records receive source-backed Google fields
and provenance; permanently closed places are retained with their status rather
than deleted.

## Menu policy

There is no `nextrip_google_maps_menu` DAG. The daily DAG does not run menu OCR or
publish menu snapshots.

During canonical refresh, cafe and restaurant menus remain empty unless they
already have human-verification provenance. Missing menus become manual collection
tasks. A human can later supply the source and normalized items through the review
flow; menu approval is independent of daily place freshness.

## Airflow behavior

`nextrip_google_maps_daily` runs at 02:00 in `Asia/Ho_Chi_Minh` with:

- `catchup=False`
- one active DAG run
- four parallel place-crawl tasks, one per supported entity type
- two retries with a ten-minute delay
- an eight-hour timeout per crawl branch
- a one-hour canonical-apply timeout

The executor needs at least four available task slots to run all crawl branches
concurrently. The apply task uses the default all-success trigger behavior and
does not materialize a partial canonical refresh when any branch fails.

Airflow should run in Linux containers. Scheduler and worker processes must mount
the same repository, generated registry files, canonical artifacts, and persistent
raw/normalized/validation/decision directories. Install browser support in the
worker image:

```bash
python -m pip install -e '.[crawl,orchestration]'
playwright install --with-deps chromium
```
