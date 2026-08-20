# Google Maps batch runner and Airflow

## Mapping manifest

The batch input is `config/google-maps-batch-manifest.json`. It points to the
generated registry containing all verified master-data entities.

```json
{
  "registry_file": "generated/google-maps-mapping-registry.json"
}
```

Regenerate the registry whenever a verified master file changes:

```powershell
python -m nextrip_pipeline.cli build-google-maps-registry `
  --master-dir travel_data_verified `
  --override config/google-maps-mapping.json `
  --override config/google-maps-mapping-cafe-dn-062.json
```

The build fails on missing names/coordinates, unsupported cities, count mismatch,
duplicate IDs, or invalid overrides. It writes a coverage report beside the registry.
The current registry has 692 mappings: 386 Đà Nẵng and 306 Quy Nhơn.

## Run a batch manually

Daily place, opening, location, cover, and menu-source capture:

```powershell
python -m nextrip_pipeline.cli batch-google-maps `
  --manifest config/google-maps-batch-manifest.json `
  --mode place `
  --max-requests 32
```

During a trial, exclude an already tested entity and focus on menu-bearing place
types:

```powershell
python -m nextrip_pipeline.cli batch-google-maps `
  --manifest config/google-maps-batch-manifest.json `
  --mode place `
  --entity-type cafe `
  --entity-type restaurant `
  --entity-type nightlife `
  --exclude-entity-id cafe_dn_062 `
  --max-requests 10 `
  --offset 0 `
  --headed
```

For additional batches on the same day, use `--offset 10`, `--offset 20`, and so
on. Omit `--offset` in Airflow so the runner uses automatic daily rotation.

For a complete production pass, use the checked PowerShell coordinator. It limits
menu discovery and OCR to cafe, restaurant, and nightlife, excludes the already
tested Xóm Mèo mapping by default, and processes all 500 remaining entities in
sequential chunks:

```powershell
.\scripts\run-google-maps-menu-full.ps1 `
  -ChunkSize 25 `
  -PauseSeconds 60 `
  -Python ..\.venv\Scripts\python.exe
```

The first phase crawls all 500 target places to discover menu sources. The second
phase OCRs every source actually discovered. Failed chunk offsets are reported at
the end and their immutable batch summaries remain under `data/runs/google_maps` for
targeted retries. Hotel and attraction records cannot enter the menu batch even if a
page exposes an image that resembles a menu.

Periodic menu OCR and human-review queue refresh:

```powershell
python -m nextrip_pipeline.cli batch-google-maps `
  --manifest config/google-maps-batch-manifest.json `
  --mode menu `
  --max-requests 16
```

Every run writes an immutable summary under `data/runs/google_maps`. One failed
mapping does not stop the remaining mappings, but the CLI exits with code 1 when the
summary contains failures so Airflow can retry and alert.

The daily place batch also records menu images it discovers under
`data/current/google_maps_menu_sources`. The menu batch combines this discovery index
with manually verified menu URLs, allowing new cafe/restaurant menu sources to enter
OCR and human review without editing the generated master registry.

## Airflow DAGs

`dags/nextrip_google_maps.py` exposes two DAGs:

- `nextrip_google_maps_daily`: every day at 02:00 Asia/Ho_Chi_Minh;
- `nextrip_google_maps_menu`: manual-only (`schedule=None`).

The menu/OCR DAG is unscheduled pending menu-source verification and OCR review;
it can only be triggered manually. It is not on the critical path for Google Maps identity, opening-status,
location, cover, current-place publication, or the later Neo4j sync. Keep the
daily place/status branch independent so it can be validated and enabled without
starting menu OCR.

Both DAGs have two retries, a ten-minute retry delay, a two-hour task timeout,
`catchup=False`, and `max_active_runs=1`.

Airflow should run in Linux containers rather than directly on Windows. Mount the
repository and DAG directory into both scheduler and worker, persist the `data`
directory, and install the project with its crawl, OCR, and orchestration extras:

```bash
python -m pip install -e '.[crawl,menu-ocr,orchestration]'
playwright install --with-deps chromium
```

Set the repository path visible inside the worker:

```text
NEXTRIP_KB_ROOT=/opt/airflow/nextrip
```

Optional limits for the trial phase:

```text
NEXTRIP_MAPS_DAILY_LIMIT=32
NEXTRIP_MAPS_MENU_LIMIT=16
```

The scheduler and worker must use the same mounted repository, `data` directory,
and mapping manifest. Airflow only orchestrates the CLI; all crawl, validation,
decision, review, and summary logic remains in `nextrip_pipeline` and can be tested
without Airflow.
