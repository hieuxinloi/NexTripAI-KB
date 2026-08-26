#!/usr/bin/env bash
set -euo pipefail

entry_paths=(
  /srv/nextrip/repo/NexTripAI-KB/config/generated
  /srv/nextrip/data/neo4j/v8/observation_runs
  /srv/nextrip/data/quality/trivago_mapping
  /srv/nextrip/data/quality/google_maps_mapping
)

for path in "${entry_paths[@]}"; do
  sudo mkdir -p "${path}"
  sudo chown 50000:0 "${path}"
  sudo chmod 2775 "${path}"
done

sudo docker exec nextrip-traffic-airflow-scheduler-1 bash -lc '
  set -e
  for path in \
    /opt/airflow/nextrip/config/generated \
    /opt/airflow/nextrip/data/neo4j/v8/observation_runs \
    /opt/airflow/nextrip/data/quality/trivago_mapping \
    /opt/airflow/nextrip/data/quality/google_maps_mapping
  do
    probe="${path}/.cloud-write-probe"
    touch "${probe}"
    rm "${probe}"
  done
'

stat -c '%U:%G %a %n' "${entry_paths[@]}"
