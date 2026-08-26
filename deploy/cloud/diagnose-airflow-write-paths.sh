#!/usr/bin/env bash
set -euo pipefail

bucket_object="gs://corded-gear-500002-f7-nextrip-runtime/snapshots/2026-08-25/nextrip-quality-2026-08-25.tar.gz"
artifact="/srv/nextrip/artifacts/nextrip-quality-2026-08-25.tar.gz"

gcloud storage cp "${bucket_object}" "${artifact}"
sudo tar --warning=no-unknown-keyword --no-same-owner --no-same-permissions \
  -xzf "${artifact}" -C /srv/nextrip/data

host_paths=(
  /srv/nextrip/data
  /srv/nextrip/data/neo4j
  /srv/nextrip/data/neo4j/v8
  /srv/nextrip/data/neo4j/v8/observation_runs
  /srv/nextrip/data/quality
  /srv/nextrip/data/quality/trivago_mapping
  /srv/nextrip/repo/NexTripAI-KB/config/generated
)

printf 'HOST_PATHS\n'
stat -c '%U:%G %a %n' "${host_paths[@]}"

printf '\nCONTAINER_PATHS\n'
sudo docker exec nextrip-traffic-airflow-scheduler-1 bash -lc '
  id
  stat -c "%U:%G %a %n" \
    /opt/airflow/nextrip/data/neo4j/v8/observation_runs \
    /opt/airflow/nextrip/data/quality/trivago_mapping \
    /opt/airflow/nextrip/config/generated
'
