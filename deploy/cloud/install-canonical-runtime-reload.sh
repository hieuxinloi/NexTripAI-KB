#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${NEXTRIP_REPO_ROOT:-/srv/nextrip/repo/NexTripAI-KB}"
RUNTIME_ENV="${REPO_ROOT}/deploy/traffic/.env"
STABLE_DATASET="data/canonical/active-canonical-dataset.json"

test -f "$RUNTIME_ENV"
test -x /usr/local/sbin/nextrip-sync-active-canonical-dataset

NEXTRIP_RESTART_CANONICAL_SERVICES=false \
  /usr/local/sbin/nextrip-sync-active-canonical-dataset

temporary_env="$(mktemp "${RUNTIME_ENV}.tmp.XXXXXX")"
cleanup() {
  rm -f "$temporary_env"
}
trap cleanup EXIT

awk -v value="$STABLE_DATASET" '
  BEGIN { replaced = 0 }
  { sub(/\r$/, "", $0) }
  /^[[:space:]]*NUL[[:space:]]*$/ { next }
  /^NEXTRIP_CANONICAL_DATASET=/ {
    print "NEXTRIP_CANONICAL_DATASET=" value
    replaced = 1
    next
  }
  { print }
  END {
    if (!replaced) {
      print "NEXTRIP_CANONICAL_DATASET=" value
    }
  }
' "$RUNTIME_ENV" >"$temporary_env"
chmod 600 "$temporary_env"
mv -f "$temporary_env" "$RUNTIME_ENV"
trap - EXIT

if [[ ! -e "${REPO_ROOT}/.env" ]]; then
  install -m 600 /dev/null "${REPO_ROOT}/.env"
fi

cd "$REPO_ROOT"
sudo docker compose \
  -p nextrip-traffic \
  --env-file deploy/traffic/.env \
  -f deploy/traffic/compose.yaml \
  up -d --no-deps --force-recreate traffic-api current-data-api

sudo systemctl daemon-reload
sudo systemctl enable --now nextrip-canonical-reload.path

for url in 'http://127.0.0.1:8010/ready' 'http://127.0.0.1:8020/ready'; do
  ready=false
  for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null; then
      printf 'ready=%s\n' "$url"
      ready=true
      break
    fi
    sleep 2
  done
  if [[ "$ready" != "true" ]]; then
    printf 'Readiness timeout: %s\n' "$url" >&2
    exit 1
  fi
done

printf 'canonical_dataset=%s\n' "$STABLE_DATASET"
systemctl is-active nextrip-canonical-reload.path
