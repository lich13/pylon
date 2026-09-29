#!/usr/bin/env bash
set -euo pipefail

FRPS_IMAGE='fatedier/frps@sha256:03d64743945bb5fe439624ba0d057aeb43469dfed54f8df21849d0bc2c677f27'
CONFIG_DIR='/srv/frp-chiphell'
CONFIG_FILE="$CONFIG_DIR/frps.toml"
tmp="$(mktemp)"
step='read_config'
cleanup() {
  status=$?
  rm -f "$tmp"
  trap - EXIT
  if [[ "$status" -ne 0 ]]; then
    printf 'FRPS_INSTALL_FAILED step=%s exit=%s\n' "$step" "$status" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
umask 077
cat >"$tmp"

step='validate_config'
test -s "$tmp"
grep -Fxq 'bindAddr = "0.0.0.0"' "$tmp"
grep -Fxq 'bindPort = 7000' "$tmp"
grep -Fxq 'proxyBindAddr = "127.0.0.1"' "$tmp"
grep -Fxq 'transport.tls.force = true' "$tmp"
grep -Fxq 'allowPorts = [{ single = 18080 }]' "$tmp"
if grep -Eq 'webServer|dashboard|18081' "$tmp"; then
  printf 'unsafe frps configuration rejected\n' >&2
  exit 65
fi

step='install_config'
install -d -m 700 "$CONFIG_DIR"
install -m 600 "$tmp" "$CONFIG_FILE"
step='pull_image'
docker pull "$FRPS_IMAGE" >/dev/null
docker rm -f frps-chiphell >/dev/null 2>&1 || true
step='run_container'
docker run -d \
  --name frps-chiphell \
  --network host \
  --memory 64m \
  --memory-swap 64m \
  --cpus 0.5 \
  --pids-limit 64 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=4194304 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --restart unless-stopped \
  --log-opt max-size=2m \
  --log-opt max-file=2 \
  --label chiphell.proxy.role=frps \
  -v /srv/frp-chiphell/frps.toml:/etc/frp/frps.toml:ro \
  "$FRPS_IMAGE" -c /etc/frp/frps.toml >/dev/null

step='allow_firewall'
ufw allow 7000/tcp comment 'Chiphell frps' >/dev/null
step='verify_container'
docker inspect frps-chiphell --format '{{.State.Status}}' | grep -Fxq running
step='verify_listener'
listener_ready=0
for attempt in {1..20}; do
  if ss -lnt | grep -Eq '0\.0\.0\.0:7000|\*:7000'; then
    listener_ready=1
    break
  fi
  sleep 0.5
done
test "$listener_ready" -eq 1
step='complete'
printf 'FRPS_INSTALL_OK\n'
