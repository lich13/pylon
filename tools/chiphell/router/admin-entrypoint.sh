#!/bin/sh
set -eu

FRPC_CONTAINER="frpc-chiphell"
LEGACY_CONFIG_VOLUME="chiphell-frp-config"

daemon() {
  trap 'exit 0' TERM INT
  while :; do
    sleep 3600 &
    wait "$!"
  done
}

apply_config() {
  frpc_image="${1:?frpc image is required}"
  umask 077
  env_file="$(mktemp)"
  trap 'rm -f "$env_file"' EXIT HUP INT TERM
  cat >"$env_file"
  chmod 600 "$env_file"
  test "$(wc -l <"$env_file")" -eq 2
  test "$(grep -c '^FRP_SERVER_ADDR=[A-Za-z0-9.-][A-Za-z0-9.-]*$' "$env_file")" -eq 1
  test "$(grep -Ec '^FRP_TOKEN=[A-Fa-f0-9]{64}$' "$env_file")" -eq 1

  docker rm -f "$FRPC_CONTAINER" >/dev/null 2>&1 || true
  docker run -d \
    --name "$FRPC_CONTAINER" \
    --network host \
    --memory 64m \
    --cpuset-cpus 0 \
    --pids-limit 64 \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=4194304 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --restart unless-stopped \
    --log-opt max-size=1m \
    --log-opt max-file=2 \
    --label chiphell.proxy.role=frpc \
    --env-file "$env_file" \
    "$frpc_image" -c /config/frpc.toml
  rm -f "$env_file"
  trap - EXIT HUP INT TERM
}

uninstall_proxy() {
  docker rm -f "$FRPC_CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$LEGACY_CONFIG_VOLUME" >/dev/null 2>&1 || true
}

case "${1:-daemon}" in
  daemon)
    daemon
    ;;
  apply)
    shift
    apply_config "$@"
    ;;
  uninstall)
    uninstall_proxy
    ;;
  healthcheck)
    docker --version
    ;;
  *)
    printf 'unknown command\n' >&2
    exit 64
    ;;
esac
