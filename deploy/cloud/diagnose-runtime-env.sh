#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${NEXTRIP_TRAFFIC_ENV_FILE:-/srv/nextrip/repo/NexTripAI-KB/deploy/traffic/.env}"
line_number=0

while IFS= read -r line || [[ -n "$line" ]]; do
  line_number=$((line_number + 1))
  line="${line%$'\r'}"
  if [[ -z "$line" || "$line" == \#* ]]; then
    continue
  fi
  if [[ "$line" == *=* ]]; then
    key="${line%%=*}"
    if [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      printf 'VALID_KEY line=%s key=%s\n' "$line_number" "$key"
      continue
    fi
  fi
  printf 'INVALID_LINE line=%s escaped=%q\n' "$line_number" "$line"
done <"$ENV_FILE"
