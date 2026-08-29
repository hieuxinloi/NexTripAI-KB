#!/usr/bin/env bash
set -euo pipefail

summary_path="/srv/nextrip/data/runs/google_maps/mode=place"
sudo mkdir -p "$summary_path"
sudo chown 50000:0 "$summary_path"
sudo chmod 2775 "$summary_path"
stat -c '%u:%g %a %n' "$summary_path"
