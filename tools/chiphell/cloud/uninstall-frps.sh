#!/usr/bin/env bash
set -euo pipefail

FRPS_IMAGE='fatedier/frps@sha256:03d64743945bb5fe439624ba0d057aeb43469dfed54f8df21849d0bc2c677f27'

docker rm -f frps-chiphell >/dev/null 2>&1 || true
docker image rm "$FRPS_IMAGE" >/dev/null 2>&1 || true
rm -rf -- /srv/frp-chiphell
if ufw status | grep -q '7000/tcp'; then
  ufw --force delete allow 7000/tcp >/dev/null
fi
printf 'FRPS_UNINSTALL_OK\n'
