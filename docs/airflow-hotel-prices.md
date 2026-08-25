# Trivago MCP hotel-price pipeline

`dags/nextrip_hotel_prices.py` runs the hotel branch every five hours. It uses
Trivago's remote MCP endpoint over HTTP; it does not launch Playwright and does
not write directly to Neo4j. The public MCP currently needs no API key. NexTrip uses
`trivago-accommodation-search` with the canonical hotel name for discovery;
after confirmation it uses the canonical Trivago name and city. Coordinates
are supporting evidence only; the scheduled pipeline does not auto-confirm a
different nearby hotel.

Each Airflow run performs two ordered tasks:

1. `build-trivago-registry` rebuilds all active hotel search targets from the
   immutable dataset selected by `NEXTRIP_CANONICAL_DATASET`. Confirmed mapping
   overrides and mappings discovered by earlier runs are keyed to canonical
   `place_id` values. No verified-seed or current-place fallback is allowed.
2. `batch-trivago-availability` refreshes only accommodations with a confirmed
   Trivago identity, captures immutable raw evidence, records availability for
   the exact full stay, normalizes and validates priced offers, writes every
   quality-accepted price/availability observation as immutable JSON under
   `data/observations`, and then updates the contextual latest projections under
   `data/current`. Unresolved identity
   research is opt-in through `--include-identity-discovery` and is not part of
   the five-hour price schedule. When an exact search
   proves the stay unavailable, the default policy shifts the complete stay by
   one day and records both windows. `UNKNOWN` technical or identity results do
   not trigger fallback, do not update accepted/current data, and are never
   presented as sold out. They remain in raw evidence and the immutable stay
   run result for retry/audit. The DAG does not publish directly to Neo4j.

One MCP session is reused within a batch. Candidate identity is resolved from
the returned accommodation ID, name, and city. A new strong, unambiguous match
can be confirmed automatically; an ambiguous result remains unresolved/review,
and a previously confirmed ID is never silently replaced by another ID.

The accepted JSON is content-addressed and append-only, so a later five-hour
refresh never overwrites the prior price. `data/current` is only a latest-value
projection for serving and Neo4j publication.

The default request is check-in tomorrow, one-night stay, one fallback day,
two adults, zero children, one room, and VND. The current availability and
price keys retain stay dates, occupancy, and sorted child ages, so different
search contexts do not overwrite each other.

## Airflow configuration

Install the project and orchestration dependency in the Linux scheduler/worker
image, then mount the repository and persistent `data` directory:

```bash
python -m pip install -e '.[orchestration]'
```

Required worker setting:

```text
NEXTRIP_KB_ROOT=/opt/airflow/nextrip
NEXTRIP_CANONICAL_DATASET_POINTER=data/canonical/active-dataset-pointer.json
NEXTRIP_HOTEL_PRICES_AIRFLOW_ENABLED=true
```

The canonical directory and `data` directory must be shared with the worker.
The registry task fails before crawling if the pointer is missing or invalid.
`NEXTRIP_CANONICAL_DATASET` remains an exact-path fallback during migration.
For Docker Compose, use `NEXTRIP_DATA_ROOT` to keep this persistent data tree on
the selected host disk; see [Runtime data root](runtime-data-root.md).

Optional settings and their defaults:

```text
NEXTRIP_HOTEL_CHECK_IN_OFFSET_DAYS=1
NEXTRIP_HOTEL_STAY_NIGHTS=1
NEXTRIP_HOTEL_LOOKAHEAD_DAYS=1
NEXTRIP_HOTEL_ADULTS=2
NEXTRIP_HOTEL_ROOMS=1
NEXTRIP_HOTEL_CURRENCY=VND
NEXTRIP_ACCEPTED_OBSERVATION_ROOT=data/observations
NEXTRIP_TRIVAGO_MAX_REQUESTS=
```

An empty maximum selects every active confirmed hotel; there is no hard-coded
expected hotel count. For a bounded first live run, set
`NEXTRIP_TRIVAGO_MAX_REQUESTS=1`. Check the
summary in `data/runs/trivago_availability_batch` before increasing the limit.
Per-hotel stay results are stored in `data/runs/trivago_stay`. The source and
job are enabled in `config/sources.json` and `config/jobs.json`; those files are
declarative policy, while Airflow provides the actual scheduling.

## Run without Airflow

```powershell
python -m nextrip_pipeline.cli build-trivago-registry `
  --canonical-dataset $env:NEXTRIP_CANONICAL_DATASET `
  --override config/trivago-mapping.json

python -m nextrip_pipeline.cli batch-trivago-availability `
  --max-requests 1
```

Omit `--max-requests` only after the bounded run succeeds. Explicit stay and
occupancy values can be supplied with `--check-in`, `--check-out`, `--adults`,
`--rooms`, `--children`, `--children-ages`, and `--lookahead-days`.
Use `--include-identity-discovery` only for a bounded identity-research batch;
provider-not-listed and identity-reverify terminal review states remain excluded.

A confirmed Trivago ID is never replaced merely because a stay-date search
returns another similarly named hotel. The result is persisted as
`unknown/confirmed_listing_not_returned`, the confirmed mapping is retained,
and the scheduled job may retry it later. Only an exact confirmed ID can
publish a price; an external-ID change requires separately pinned review
evidence.
