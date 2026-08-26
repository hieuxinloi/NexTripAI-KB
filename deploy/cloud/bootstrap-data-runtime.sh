#!/usr/bin/env bash
set -euo pipefail

data_device="/dev/sdb"
data_mount="/srv/nextrip"

if ! sudo blkid "${data_device}" >/dev/null 2>&1; then
  sudo mkfs.ext4 -F -L nextrip-runtime-data "${data_device}"
fi

sudo mkdir -p "${data_mount}"
data_uuid="$(sudo blkid -s UUID -o value "${data_device}")"
if ! grep -q "${data_uuid}" /etc/fstab; then
  echo "UUID=${data_uuid} ${data_mount} ext4 defaults,nofail 0 2" \
    | sudo tee -a /etc/fstab >/dev/null
fi
sudo mount "${data_mount}" || true

sudo mkdir -p \
  "${data_mount}/data" \
  "${data_mount}/valhalla-data" \
  "${data_mount}/artifacts" \
  "${data_mount}/repo"
sudo chown -R "$(id -u):$(id -g)" "${data_mount}"

sudo timedatectl set-timezone Asia/Ho_Chi_Minh
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates \
  docker.io \
  docker-compose-v2 \
  git \
  jq
sudo systemctl enable --now docker
sudo usermod -aG docker "$(id -un)"

df -h "${data_mount}"
sudo docker --version
sudo docker compose version
