# Neo4j V8 local infrastructure

Neo4j V8 runs as one independent local container. It does not mount, copy, or
modify any V1-V7 volume.

## Endpoints

- Browser/HTTP: `http://localhost:7479`
- Bolt: `bolt://localhost:7692`
- Database: `neo4j`
- Container: `nextrip-neo4j-v8`
- Data volume: `nextrip_neo4j_v8_data`
- Log volume: `nextrip_neo4j_v8_logs`

The ports bind to `127.0.0.1`, so the database is not exposed outside the
development machine.

## Configure

Copy `.env.example` to `.env` if it does not exist, then replace
`NEO4J_V8_PASSWORD=change-me` with a private password. Keep
`NEO4J_V8_URI=bolt://localhost:7692` aligned with the Compose Bolt port.

Validate the resolved Compose model without starting a container:

```powershell
docker-compose --env-file .env -f docker-compose.v8.yml config --quiet
```

## Start and inspect

```powershell
docker-compose --env-file .env -f docker-compose.v8.yml up -d
docker-compose --env-file .env -f docker-compose.v8.yml ps
docker-compose --env-file .env -f docker-compose.v8.yml logs --tail 100 neo4j-v8
```

Wait until `nextrip-neo4j-v8` reports `healthy` before running the V8 schema or
canonical-data publisher.

Stop V8 without deleting its data:

```powershell
docker-compose --env-file .env -f docker-compose.v8.yml down
```

Do not add `--volumes` to the `down` command unless deleting the V8 database is
explicitly intended.

## Index and plugin policy

Neo4j 5 provides the range, uniqueness, full-text, and vector indexes required
by the current GraphRAG loader. The loader creates those indexes through Cypher;
the container therefore does not install APOC or GDS.

GDS is only needed for a later community-detection build. It must be introduced
as a separate, reviewed infrastructure change rather than silently enabled in
this V8 runtime.
