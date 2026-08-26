#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/srv/nextrip/repo/NexTripAI-KB}"
cd "$REPO_ROOT"

compose=(
  sudo docker compose
  -p nextrip-traffic
  --env-file deploy/traffic/.env
  -f deploy/traffic/compose.airflow.yaml
)

dags=(
  nextrip_hotel_prices_5h
  nextrip_google_maps_weekly
  nextrip_neo4j_v8_observations
  nextrip_canonical_release_rollout
)

for dag in "${dags[@]}"; do
  "${compose[@]}" exec -T airflow-scheduler airflow dags unpause "$dag"
done

printf '\nDAG pause state:\n'
"${compose[@]}" exec -T airflow-scheduler airflow dags list --output table \
  | grep -E 'dag_id|nextrip_(hotel_prices_5h|google_maps_weekly|neo4j_v8_observations|canonical_release_rollout|traffic_maintenance)'

for dag in "${dags[@]}"; do
  printf '\nRecent runs: %s\n' "$dag"
  "${compose[@]}" exec -T airflow-scheduler airflow dags list-runs "$dag" --output table \
    | head -n 8
done
