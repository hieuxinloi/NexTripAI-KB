#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/srv/nextrip/data}"
REPO_ROOT="${REPO_ROOT:-/srv/nextrip/repo/NexTripAI-KB}"
AIRFLOW_UID="${AIRFLOW_UID:-50000}"
AIRFLOW_GID="${AIRFLOW_GID:-0}"

probe_dir() {
  local label="$1"
  local relative_path="$2"

  sudo docker run --rm \
    --user "${AIRFLOW_UID}:${AIRFLOW_GID}" \
    --volume "${DATA_ROOT}:/data" \
    alpine:3.20 \
    sh -c "touch '/data/${relative_path}/.airflow-write-probe' && rm '/data/${relative_path}/.airflow-write-probe'"
  printf 'WRITE_OK %s\n' "$label"
}

probe_repo_dir() {
  local label="$1"
  local relative_path="$2"

  sudo docker run --rm \
    --user "${AIRFLOW_UID}:${AIRFLOW_GID}" \
    --volume "${REPO_ROOT}:/repo" \
    alpine:3.20 \
    sh -c "touch '/repo/${relative_path}/.airflow-write-probe' && rm '/repo/${relative_path}/.airflow-write-probe'"
  printf 'WRITE_OK %s\n' "$label"
}

probe_deepest_dir() {
  local store="$1"
  local target
  local relative_path

  target="$(find "${DATA_ROOT}/current/${store}" -type d -print | tail -n 1)"
  relative_path="${target#${DATA_ROOT}/}"
  probe_dir "current/${store} deepest=${relative_path}" "$relative_path"
}

probe_dir "runs/trivago_availability_batch" "runs/trivago_availability_batch"
probe_dir "runs/google_maps" "runs/google_maps"
probe_dir "runs/google_maps/mode=place" "runs/google_maps/mode=place"
probe_dir "canonical/datasets" "canonical/datasets"
probe_dir "neo4j/v8/observation_runs" "neo4j/v8/observation_runs"
probe_repo_dir "config/generated" "config/generated"

probe_deepest_dir "trivago_mappings"
probe_deepest_dir "hotel_price"
probe_deepest_dir "hotel_availability"
