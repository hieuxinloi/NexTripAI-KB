#!/usr/bin/env bash
set -euo pipefail

task_id="${1:?usage: inspect-airflow-task-log.sh TASK_ID [LINES]}"
line_count="${2:-160}"

scheduler_id="$(
  sudo docker ps \
    --filter 'label=com.docker.compose.project=nextrip-traffic' \
    --filter 'label=com.docker.compose.service=airflow-scheduler' \
    --format '{{.ID}}' \
    | head -n 1
)"
test -n "$scheduler_id"

log_path="$(
  sudo docker exec "$scheduler_id" \
    find /opt/airflow/logs -type f -path "*/task_id=${task_id}/*" \
      -printf '%T@ %p\n' \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
)"

if [[ -z "$log_path" ]]; then
  printf 'No log found for task_id=%s\n' "$task_id" >&2
  exit 1
fi

printf 'log=%s\n' "$log_path"
sudo docker exec "$scheduler_id" tail -n "$line_count" "$log_path"
