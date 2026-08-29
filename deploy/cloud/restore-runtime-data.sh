#!/usr/bin/env bash
set -euo pipefail

bucket="gs://corded-gear-500002-f7-nextrip-runtime/snapshots/2026-08-25"
artifact_root="/srv/nextrip/artifacts"
data_root="/srv/nextrip/data"
valhalla_root="/srv/nextrip/valhalla-data"
restore_root="/srv/nextrip/restore-staging"
restore_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
data_stage="${restore_root}/data-${restore_stamp}"
valhalla_stage="${restore_root}/valhalla-${restore_stamp}"

sudo mkdir -p "${artifact_root}" "${restore_root}"
sudo chown "$(id -u):$(id -g)" "${artifact_root}" "${restore_root}"
mkdir -p "${data_stage}" "${valhalla_stage}"

gcloud storage cp \
  "${bucket}/valhalla-vietnam-2026-08-19.tar.gz" \
  "${artifact_root}/valhalla-vietnam-2026-08-19.tar.gz"
gcloud storage cp \
  "${bucket}/nextrip-operational-data-2026-08-25.tar.gz" \
  "${artifact_root}/nextrip-operational-data-2026-08-25.tar.gz"

tar --warning=no-unknown-keyword \
  -tzf "${artifact_root}/valhalla-vietnam-2026-08-19.tar.gz" >/dev/null
tar --warning=no-unknown-keyword \
  -tzf "${artifact_root}/nextrip-operational-data-2026-08-25.tar.gz" >/dev/null

sudo tar --warning=no-unknown-keyword --no-same-owner --no-same-permissions \
  -xzf "${artifact_root}/valhalla-vietnam-2026-08-19.tar.gz" \
  -C "${valhalla_stage}"
sudo tar --warning=no-unknown-keyword --no-same-owner --no-same-permissions \
  -xzf "${artifact_root}/nextrip-operational-data-2026-08-25.tar.gz" \
  -C "${data_stage}"

test -s "${valhalla_stage}/vietnam-2026-08-19/dataset-manifest.json"
test -s "${data_stage}/canonical/datasets/dataset=canonical-active-1350fa394ae42beac50c/canonical-active-dataset.json"

sudo chown -R "$(id -u):$(id -g)" "${data_stage}" "${valhalla_stage}"
if [ -e "${data_root}" ]; then
  sudo mv "${data_root}" "${data_root}.failed-${restore_stamp}"
fi
if [ -e "${valhalla_root}" ]; then
  sudo mv "${valhalla_root}" "${valhalla_root}.failed-${restore_stamp}"
fi
sudo mv "${data_stage}" "${data_root}"
sudo mv "${valhalla_stage}" "${valhalla_root}"

du -sh "${valhalla_root}/vietnam-2026-08-19" "${data_root}"
