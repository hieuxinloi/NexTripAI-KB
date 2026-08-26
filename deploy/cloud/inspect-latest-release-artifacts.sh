#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${NEXTRIP_DATA_ROOT:-/srv/nextrip/data}"

latest_files() {
  local label="$1"
  local root="$2"
  printf '\n=== %s ===\n' "$label"
  if [[ ! -d "$root" ]]; then
    printf 'missing_directory=%s\n' "$root"
    return
  fi
  find "$root" -type f -printf '%T@ %s %p\n' \
    | sort -nr \
    | head -n 8 \
    | cut -d' ' -f2-
}

latest_files canonical_completeness "$DATA_ROOT/canonical/completeness"
latest_files canonical_readiness "$DATA_ROOT/canonical/readiness"
latest_files canonical_rollout "$DATA_ROOT/runs/canonical_rollout"
latest_files neo4j_releases "$DATA_ROOT/neo4j/v8/releases"
latest_files observation_runs "$DATA_ROOT/neo4j/v8/observation_runs"
latest_files embedding_runs "$DATA_ROOT/neo4j/v8/embedding_runs"
latest_files google_maps_runs "$DATA_ROOT/runs/google_maps"
latest_files trivago_runs "$DATA_ROOT/runs/trivago_availability_batch"

pointer="$DATA_ROOT/canonical/active-dataset-pointer.json"
dataset_relative="$(jq -er '.dataset_path' "$pointer")"
dataset="$DATA_ROOT/canonical/$dataset_relative"
printf '\n=== active_dataset_shape ===\n'
printf 'path=%s\n' "$dataset"
jq '{top_level_keys: keys, place_count: ((.places // .records // []) | length)}' "$dataset"
