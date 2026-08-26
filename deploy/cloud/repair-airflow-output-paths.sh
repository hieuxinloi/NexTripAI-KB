#!/usr/bin/env bash
set -euo pipefail

# Existing current snapshots stay read-only. Airflow only needs write access on
# their directory hierarchy to atomically replace one contextual JSON file.
current_directory_trees=(
  /srv/nextrip/data/current/trivago_mappings
  /srv/nextrip/data/current/hotel_price
  /srv/nextrip/data/current/hotel_availability
)

for root in "${current_directory_trees[@]}"; do
  sudo mkdir -p "${root}"
  sudo find "${root}" -type d -exec chown 50000:0 {} +
  sudo find "${root}" -type d -exec chmod 2775 {} +
done

# These are exact entry directories for new immutable runs/releases. Existing
# JSON files are never chmod/chowned. The mode=place directory is listed
# explicitly because the restored archive created it before setgid was enabled.
output_entry_paths=(
  /srv/nextrip/data/crawl_artifacts
  /srv/nextrip/data/runs/google_maps
  /srv/nextrip/data/runs/google_maps/mode=place
  /srv/nextrip/data/runs/trivago_stay
  /srv/nextrip/data/runs/trivago_availability_batch
  /srv/nextrip/data/canonical
  /srv/nextrip/data/canonical/datasets
  /srv/nextrip/data/canonical/google-maps-patches
  /srv/nextrip/data/canonical/manifests
  /srv/nextrip/data/canonical/completeness
  /srv/nextrip/data/canonical/readiness
  /srv/nextrip/data/neo4j/v8/releases
  /srv/nextrip/data/neo4j/v8/observation_runs
  /srv/nextrip/data/neo4j/v8/embedding_runs
  /srv/nextrip/data/cache/v8_embeddings
)

for path in "${output_entry_paths[@]}"; do
  sudo mkdir -p "${path}"
  sudo chown 50000:0 "${path}"
  sudo chmod 2775 "${path}"
done

stat -c '%u:%g %a %n' \
  "${current_directory_trees[@]}" \
  "${output_entry_paths[@]}"
