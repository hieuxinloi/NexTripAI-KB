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
  nextrip_traffic_maintenance
)

extract_json() {
  sed -n '/^[[:space:]]*[\[{]/,$p'
}

for dag in "${dags[@]}"; do
  printf '\n=== %s ===\n' "$dag"
  runs="$("${compose[@]}" exec -T airflow-scheduler airflow dags list-runs "$dag" --output json | extract_json)"
  printf '%s\n' "$runs" | jq '.[0:5] | map({run_id, state, start_date, end_date})'

  run_id="$(printf '%s\n' "$runs" | jq -r '.[0].run_id // empty')"
  if [[ -n "$run_id" ]]; then
    printf '%s\n' "Latest task states for ${run_id}:"
    "${compose[@]}" exec -T airflow-scheduler \
      airflow tasks states-for-dag-run "$dag" "$run_id" --output json \
      | extract_json \
      | jq 'map({task_id, state, try_number})'
  fi
done
