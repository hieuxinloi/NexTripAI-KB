# Canonical-only Google Maps Airflow pipeline

The scheduled Google Maps flow has one place-data source of truth: an immutable
canonical dataset selected through `NEXTRIP_CANONICAL_DATASET_POINTER`. The
direct `NEXTRIP_CANONICAL_DATASET` path is only a migration fallback. Verified
raw master files are not read by this DAG, and the legacy current-place
directory is not a serving or publication sink.

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
          +--> raw JSON -> normalize -> deterministic validators
          |                |                |                |
          +--> trusted decision gate: PASS or QUARANTINE (no review queue)
          |                |                |                |
          +--> PASS: append-only accepted JSON in data/observations
          +--> QUARANTINE: raw/normalized/validation only; no current update
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
google-maps-place-<airflow-run-id>-<entity-type>-try<try-number>-<execution-id>
```

The CLI receives it through `batch-google-maps --run-id`, verifies that the
returned ID matches, and emits the ID as the task's final stdout/XCom value. A
retry or manually cleared task gets a fresh random execution ID, so it cannot
collide with immutable summary or evidence from an earlier attempt even when
Airflow reuses the same try number. The apply task waits for all four branches and
fails unless all four XCom values are non-empty and unique. It then passes them
as repeated `--run-id` arguments; older observations cannot be selected
accidentally.

The apply task emits the new content-addressed canonical dataset path as its
final stdout line. That path is the task's XCom return value. The old dataset is
never overwritten. Promote the new dataset's readiness-paired snapshot through
the atomic pointer only after its matching Neo4j canonical release is active.

## Required environment

```text
NEXTRIP_KB_ROOT=/opt/airflow/nextrip
NEXTRIP_CANONICAL_DATASET_POINTER=data/canonical/active-dataset-pointer.json
NEXTRIP_GOOGLE_MAPS_AIRFLOW_ENABLED=true
NEXTRIP_GOOGLE_MAPS_TRUSTED_SCHEDULED=true
```

If no pointer has been promoted yet, set the legacy
`NEXTRIP_CANONICAL_DATASET` to one exact immutable dataset during migration.
For Docker Compose, `NEXTRIP_DATA_ROOT` selects the host directory mounted at
`/opt/airflow/nextrip/data`; see [Runtime data root](runtime-data-root.md).

Optional settings:

```text
NEXTRIP_GOOGLE_MAPS_SCHEDULE=0 2 * * 1
NEXTRIP_MAPS_WEEKLY_LIMIT=10000
NEXTRIP_MAPS_MAX_NO_UPDATE_RATIO=0.20
NEXTRIP_GOOGLE_MAPS_POOL=google_maps_web
NEXTRIP_ACCEPTED_OBSERVATION_ROOT=data/observations
NEXTRIP_RAW_ROOT=data/raw
NEXTRIP_MAPS_NORMALIZED_ROOT=data/normalized
NEXTRIP_VALIDATION_ROOT=data/validation
NEXTRIP_MAPS_DECISION_ROOT=data/decisions
NEXTRIP_MAPS_SCRATCH_ROOT=/tmp/nextrip-google-maps
```

The high default request ceiling applies independently to each entity branch,
selects every eligible canonical mapping, and does not encode a fixed expected
place count. Lower the limit only when a bounded partial refresh is intentional.

In trusted scheduled mode, a place-level exception is recorded as `NO_UPDATE`.
The patch therefore leaves that place's last canonical value unchanged instead
of failing the other successful places. At least one isolated `NO_UPDATE` is
tolerated. If their share exceeds `NEXTRIP_MAPS_MAX_NO_UPDATE_RATIO`, the task
fails and Airflow retries because a correlated provider or parser failure is
more likely than an isolated listing issue.

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
    --trusted-scheduled-crawl `
    --disable-llm-review-queue `
    --accepted-observation-dir data/observations `
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

An `opening.status=unknown` result means that the page did not contain enough
provider evidence to assert open or closed. The opening-status validator emits
`OPENING_STATUS_UNAVAILABLE`; trusted scheduled execution quarantines that item
without failing the entity batch. The crawl evidence remains auditable, no
accepted observation is written, and the last verified canonical value remains
active. Airflow retries are reserved for technical failures rather than this
expected data gap. `unknown` is never converted to open, closed today, or
permanently closed.

## Menu policy

There is no `nextrip_google_maps_menu` DAG. The weekly DAG does not run menu OCR or
publish menu snapshots.

During canonical refresh, cafe and restaurant menus remain empty unless they
already have human-verification provenance. Missing menus become manual collection
tasks. A human can later supply the source and normalized items through the review
flow; menu approval is independent of weekly place freshness.

## Airflow behavior

`nextrip_google_maps_weekly` runs at 02:00 every Monday in
`Asia/Ho_Chi_Minh` by default, with:

- `catchup=False`
- one active DAG run
- four place-crawl tasks, one per supported entity type
- an Airflow pool (one slot by default) to rate-limit browser access
- two retries with a ten-minute delay
- an eight-hour timeout per crawl branch
- a one-hour canonical-apply timeout

Increase the pool slot count only after validating source stability. The apply
task uses the default all-success trigger behavior and does not materialize a
partial canonical refresh when any branch fails.

Airflow should run in Linux containers. Scheduler and worker processes must mount
the same repository, generated registry files, canonical artifacts, and persistent
raw/normalized/validation/decision directories. Install browser support in the
worker image:

```bash
python -m pip install -e '.[crawl,orchestration]'
playwright install --with-deps chromium
```
# Canonical release rollout

The weekly Google Maps DAG never changes the active pointer directly. After all
four entity batches succeed it materializes an immutable patch, candidate dataset
and readiness report, then triggers `nextrip_canonical_release_rollout` with those
exact artifact paths.

The rollout DAG performs the bounded production sequence:

1. prove the patch parent is the current active dataset (or the same candidate on
   an idempotent retry);
2. rebuild Google Maps and Trivago registries for the candidate;
3. create a candidate-specific completeness audit;
4. stage, verify and atomically activate the V8 Neo4j release;
5. compare-and-swap the JSON active pointer from the expected parent;
6. append current price, availability, opening and menu observations.
7. when `NEXTRIP_V8_EMBEDDING_ENABLED=true`, resume Gemini embeddings for the
   active release and mark semantic search ready only at 100% coverage.

`canonical_release_rollout` has one Airflow pool slot. A failed gate preserves the
last active pointer. The graph importer and pointer promotion are idempotent, so a
retry can recover if a worker stops after graph activation but before pointer
promotion. Place count is derived from the candidate and is never fixed at 692.
