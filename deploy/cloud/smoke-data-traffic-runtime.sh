#!/usr/bin/env bash
set -euo pipefail

repo_root="${NEXTRIP_REPO_ROOT:-/srv/nextrip/repo/NexTripAI-KB}"
env_file="${NEXTRIP_TRAFFIC_ENV_FILE:-${repo_root}/deploy/traffic/.env}"

env_value() {
  local key="$1"
  awk -v key="$key" '
    { sub(/\r$/, "", $0) }
    index($0, key "=") == 1 {
      value = substr($0, length(key) + 2)
      if (value ~ /^".*"$/ || value ~ /^\047.*\047$/) {
        value = substr(value, 2, length(value) - 2)
      }
      print value
      exit
    }
  ' "$env_file"
}

CURRENT_DATA_API_KEY="$(env_value CURRENT_DATA_API_KEY)"
if [[ -z "$CURRENT_DATA_API_KEY" ]]; then
  printf 'CURRENT_DATA_API_KEY is missing from %s\n' "$env_file" >&2
  exit 1
fi

curl --fail --silent --show-error \
  -H "X-NexTrip-Current-Key: ${CURRENT_DATA_API_KEY}" \
  http://127.0.0.1:8020/api/current/places/cafe_dn_062 \
  | jq '.place | {place_id, name, entity_type, city}'

curl --fail --silent --show-error \
  -H "X-NexTrip-Current-Key: ${CURRENT_DATA_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data '{"origin_id":"cafe_dn_062","destination_id":"attr_dn_001"}' \
  http://127.0.0.1:8020/api/current/traffic/recommendations \
  | jq '{
      status,
      recommended_mode,
      options: [
        .options[] | {
          mode,
          status,
          duration_seconds,
          distance_meters,
          provider,
          recommended
        }
      ]
    }'
