#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${NEXTRIP_DATA_ROOT:-/srv/nextrip/data}"

latest_file() {
  local root="$1"
  local name="$2"
  find "$root" -type f -name "$name" -printf '%T@ %p\n' \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
}

show_shape() {
  local label="$1"
  local path="$2"
  printf '\n=== %s ===\npath=%s\n' "$label" "$path"
  jq '{
    keys: keys,
    status: (.status // null),
    summary: (.summary // null),
    counts: (.counts // null),
    report: (
      if (.report | type) == "object" then
        (.report | del(.records, .items, .places, .issues, .findings))
      else .report end
    ),
    publish_ready: (.publish_ready // .ready // null),
    missing_count: ((.missing // .missing_records // []) | length),
    issue_count: ((.issues // .findings // []) | length)
  }' "$path"
}

completeness="$(latest_file "$DATA_ROOT/runs/canonical_rollout" 'canonical-completeness-audit.json')"
release="$(latest_file "$DATA_ROOT/neo4j/v8/releases" 'canonical-v8-release-manifest.json')"
observations="$(latest_file "$DATA_ROOT/neo4j/v8/observation_runs" 'v8-observation-publish-manifest.json')"
embedding="$(latest_file "$DATA_ROOT/neo4j/v8/embedding_runs" 'summary.json')"
trivago="$(latest_file "$DATA_ROOT/runs/trivago_availability_batch" 'run=*.json')"

show_shape completeness "$completeness"
show_shape neo4j_release "$release"
show_shape observations "$observations"
show_shape embedding "$embedding"
show_shape trivago "$trivago"

printf '\n=== completeness_counts ===\n'
jq '{
  static_ingest_ready,
  identity_publish_ready,
  operational_fresh,
  deferred_complete,
  open_vacancy_count,
  gap_count: (.gaps | length),
  summaries
}' "$completeness"

printf '\n=== neo4j_release_counts ===\n'
jq '{
  release_id,
  place_count,
  city_count,
  document_count,
  text_unit_count,
  fact_count,
  claim_count,
  entity_counts,
  embedding_dimension,
  semantic_index_status
}' "$release"

printf '\n=== embedding_counts ===\n'
jq '{model, dimension, semantic_index_status, families}' "$embedding"

printf '\n=== trivago_counts ===\n'
jq '{
  registry_count,
  eligible_count,
  selected_count,
  completed_count,
  failed_count,
  availability_counts,
  stop_reason_counts,
  request_context
}' "$trivago"

for entity_type in attraction cafe nightlife restaurant; do
  maps="$(
    find "$DATA_ROOT/runs/google_maps/mode=place" -type f \
      -name "*-${entity_type}-try*.json" -printf '%T@ %p\n' \
      | sort -nr \
      | head -n 1 \
      | cut -d' ' -f2-
  )"
  show_shape "google_maps_${entity_type}" "$maps"
  jq '{
    entity_type: "'"$entity_type"'",
    eligible_count,
    selected_count,
    succeeded_count,
    no_update_count,
    failed_count
  }' "$maps"
done
