# Runtime data root

The traffic and Airflow Compose files share one configurable host data root:

```dotenv
NEXTRIP_DATA_ROOT=../../data
```

That default preserves the existing repository-local `data` directory. The
traffic API mounts only `${NEXTRIP_DATA_ROOT}/canonical` as read-only. Airflow
mounts the full root as read/write so raw crawl evidence, normalized records,
quality decisions, accepted observations, current projections, and immutable
canonical releases survive container replacement.

The data Airflow services depend only on their PostgreSQL metadata database.
They do not wait for Valhalla, so hotel and Google Maps schedules can run while
the traffic stack is stopped. Start Valhalla and the traffic API separately
before enabling the traffic maintenance or prewarm DAGs.

## Windows drive F

Create the directory first, copy the existing repository `data` directory only
when a migration is intended, then use an absolute host path in
`deploy/traffic/.env`:

```dotenv
NEXTRIP_DATA_ROOT=F:\nextrip-runtime\data
```

Keep the dotenv value unquoted. This prevents `\n` in a Windows path from being
interpreted as an escape sequence by a double-quoted dotenv parser. Docker
Desktop must have access to drive `F:`. Compose continues to use container paths
such as `/opt/airflow/nextrip/data`, so application settings remain relative to
`data/...` and do not need Windows-specific values.

Validate the resolved mounts without starting containers:

```powershell
docker-compose `
  --env-file .env `
  --env-file deploy\traffic\.env `
  -f deploy\traffic\compose.yaml `
  -f deploy\traffic\compose.airflow.yaml `
  config
```

The repository `.env` remains the single source for shared Neo4j credentials;
the later traffic dotenv supplies the F-drive mount, provider keys, and Airflow
runtime secrets. Compose applies the later file when a key exists in both.

Before switching the root, stop writers and verify that the destination contains
the selected canonical dataset or active pointer. Changing the variable does not
copy or delete any data.
