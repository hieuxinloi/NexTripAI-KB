#!/usr/bin/env bash
set -euo pipefail

CANONICAL_ROOT="${NEXTRIP_CANONICAL_ROOT:-/srv/nextrip/data/canonical}"
POINTER_PATH="${NEXTRIP_CANONICAL_POINTER:-${CANONICAL_ROOT}/active-dataset-pointer.json}"
ACTIVE_LINK="${NEXTRIP_CANONICAL_ACTIVE_LINK:-${CANONICAL_ROOT}/active-canonical-dataset.json}"
RESTART_SERVICES="${NEXTRIP_RESTART_CANONICAL_SERVICES:-true}"

dataset_path="$(jq -er '.dataset_path | select(type == "string" and length > 0)' "$POINTER_PATH")"
case "$dataset_path" in
  /*|*..*)
    printf 'Unsafe dataset_path in %s: %s\n' "$POINTER_PATH" "$dataset_path" >&2
    exit 2
    ;;
esac

target="${CANONICAL_ROOT}/${dataset_path}"
canonical_real="$(realpath "$CANONICAL_ROOT")"
target_real="$(realpath "$target")"
case "$target_real" in
  "${canonical_real}"/*) ;;
  *)
    printf 'Canonical target escapes root: %s\n' "$target_real" >&2
    exit 2
    ;;
esac
test -f "$target_real"

current_target="$(readlink "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ "$current_target" == "$dataset_path" ]]; then
  printf 'canonical_link_unchanged=%s\n' "$dataset_path"
  exit 0
fi

temporary_link="${ACTIVE_LINK}.tmp.$$"
cleanup() {
  rm -f "$temporary_link"
}
trap cleanup EXIT
ln -s "$dataset_path" "$temporary_link"
mv -Tf "$temporary_link" "$ACTIVE_LINK"
trap - EXIT
printf 'canonical_link_updated=%s\n' "$dataset_path"

if [[ "$RESTART_SERVICES" != "true" ]]; then
  exit 0
fi

mapfile -t container_ids < <(
  docker ps \
    --filter 'label=com.docker.compose.project=nextrip-traffic' \
    --filter 'label=com.docker.compose.service=traffic-api' \
    --format '{{.ID}}'
  docker ps \
    --filter 'label=com.docker.compose.project=nextrip-traffic' \
    --filter 'label=com.docker.compose.service=current-data-api' \
    --format '{{.ID}}'
)
if [[ "${#container_ids[@]}" -ne 2 ]]; then
  printf 'Expected traffic-api and current-data-api containers, found %s\n' \
    "${#container_ids[@]}" >&2
  exit 3
fi

docker restart "${container_ids[@]}" >/dev/null

wait_ready() {
  local url="$1"
  local attempt
  for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null; then
      printf 'ready=%s\n' "$url"
      return 0
    fi
    sleep 2
  done
  printf 'Readiness timeout: %s\n' "$url" >&2
  return 1
}

wait_ready 'http://127.0.0.1:8010/ready'
wait_ready 'http://127.0.0.1:8020/ready'
