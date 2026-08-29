#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${NEXTRIP_DATA_ROOT:-/srv/nextrip/data}"

latest_file() {
  local root="$1"
  local pattern="$2"
  find "$root" -type f -name "$pattern" -printf '%T@ %p\n' \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
}

trivago="$(latest_file "$DATA_ROOT/runs/trivago_availability_batch" 'run=*.json')"
maps="$(latest_file "$DATA_ROOT/runs/google_maps/mode=place" 'run=*.json')"
completeness="$(latest_file "$DATA_ROOT/runs/canonical_rollout" 'canonical-completeness-audit.json')"

printf '=== item_shapes ===\n'
jq '{trivago_item_keys: (.items[0] | keys)}' "$trivago"
jq '{google_maps_item_keys: (.items[0] | keys)}' "$maps"
jq '{gap_keys: (.gaps[0] | keys)}' "$completeness"

printf '\n=== trivago_unknown ===\n'
jq '[
  .items[]
  | select(
      (.availability_status? == "unknown")
      or (.status? == "unknown")
      or (.outcome? == "unknown")
      or (.stop_reason? == "unknown_result")
      or ((.availability_counts?.unknown // 0) > 0)
    )
  | {
      hotel_id: (.hotel_id // .entity_id // .place_id),
      status: (
        .availability_status
        // .outcome
        // (if (.stop_reason? == "unknown_result") then "unknown" else .status end)
      ),
      stop_reason: (.stop_reason // .reason // .error_code),
      provider_name: (.provider_name // .matched_name // .name)
    }
]' "$trivago"

printf '\n=== google_maps_no_update ===\n'
for entity_type in attraction cafe nightlife restaurant; do
  file="$(
    find "$DATA_ROOT/runs/google_maps/mode=place" -type f \
      -name "*-${entity_type}-try*.json" -printf '%T@ %p\n' \
      | sort -nr \
      | head -n 1 \
      | cut -d' ' -f2-
  )"
  jq --arg entity_type "$entity_type" '[
    .items[]
    | select(
        (.status? == "no_update")
        or (.outcome? == "no_update")
        or (.decision? == "no_update")
        or (.updated? == false)
      )
    | {
        entity_type: $entity_type,
        place_id: (.place_id // .entity_id),
        status: (.status // .outcome // .decision),
        reason: (.reason // .stop_reason // .error_code),
        name: (.name // .canonical_name // .source_name)
      }
  ]' "$file"
done

printf '\n=== completeness_gap_reasons ===\n'
jq '[
  .gaps
  | group_by([.entity_type, .field, (.status // .reason // "unknown")])[]
  | {
      entity_type: .[0].entity_type,
      field: .[0].field,
      reason: (.[0].status // .[0].reason // "unknown"),
      count: length
    }
] | sort_by(-.count) | .[0:30]' "$completeness"
