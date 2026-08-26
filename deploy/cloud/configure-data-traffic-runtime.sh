#!/usr/bin/env bash
set -euo pipefail

repo_root="${NEXTRIP_REPO_ROOT:-/srv/nextrip/repo/NexTripAI-KB}"
runtime_env="${repo_root}/deploy/traffic/.env"

secret_value() {
  gcloud secrets versions access latest \
    --project corded-gear-500002-f7 \
    --secret "$1"
}

umask 077
here_api_key="$(secret_value here-api-key)"
traffic_api_key="$(secret_value traffic-api-key)"
current_data_api_key="$(secret_value current-data-api-key)"

cat >"${runtime_env}" <<EOF
TRAFFIC_BIND_ADDRESS=127.0.0.1
TRAFFIC_PORT=8010
CURRENT_DATA_BIND_ADDRESS=0.0.0.0
CURRENT_DATA_PORT=8020
KB_API_BIND_ADDRESS=127.0.0.1
KB_API_PORT=8011
VALHALLA_BIND_ADDRESS=127.0.0.1
VALHALLA_PORT=8002
VALHALLA_DATA_DIR=/srv/nextrip/valhalla-data
VALHALLA_DATASET_VERSION=vietnam-2026-08-19
VALHALLA_SERVER_THREADS=2
VALHALLA_ENABLED=true
NEXTRIP_DATA_ROOT=/srv/nextrip/data
NEXTRIP_CANONICAL_DATASET=data/canonical/active-canonical-dataset.json
NEXTRIP_CANONICAL_DATASET_POINTER=data/canonical/active-dataset-pointer.json
HERE_ENABLED=true
HERE_API_KEY=${here_api_key}
HERE_ROUTES_URL=https://router.hereapi.com/v8/routes
TRAFFIC_API_KEY=${traffic_api_key}
CURRENT_DATA_API_KEY=${current_data_api_key}
CURRENT_DATA_TRAFFIC_TIMEOUT_SECONDS=20
CURRENT_DATA_TRIVAGO_REFRESH_ENABLED=false
TRAFFIC_PROVIDER_TIMEOUT_SECONDS=20
TRAFFIC_ROUTE_TTL_SECONDS=600
TRAFFIC_MATRIX_TTL_SECONDS=600
TRAFFIC_FREE_FLOW_TTL_SECONDS=86400
TRAFFIC_DEPARTURE_BUCKET_MINUTES=5
TRAFFIC_MAX_MATRIX_CELLS=25
TRAFFIC_STALE_IF_ERROR_SECONDS=3600
TRAFFIC_CIRCUIT_FAILURE_THRESHOLD=3
TRAFFIC_CIRCUIT_COOLDOWN_SECONDS=60
EOF

chmod 600 "${runtime_env}"
echo "runtime_env=${runtime_env}"
