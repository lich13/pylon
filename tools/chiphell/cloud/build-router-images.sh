#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-/tmp/chiphell-frp-proxy-build}"
ARTIFACT_DIR="${2:-/tmp/chiphell-frp-proxy-artifacts}"
FRPC_IMAGE='chiphell-frpc:20260713'
ADMIN_IMAGE='chiphell-proxy-admin:20260713'

rm -rf -- "$ARTIFACT_DIR"
install -d -m 755 "$ARTIFACT_DIR"

docker buildx build \
  --platform linux/arm64 \
  --load \
  -f "$SOURCE_DIR/router/Dockerfile.frpc" \
  -t "$FRPC_IMAGE" \
  "$SOURCE_DIR/router"

docker buildx build \
  --platform linux/arm64 \
  --load \
  -f "$SOURCE_DIR/router/Dockerfile.admin" \
  -t "$ADMIN_IMAGE" \
  "$SOURCE_DIR/router"

docker run --rm --platform linux/arm64 "$FRPC_IMAGE" --version
docker run --rm --platform linux/arm64 --network none \
  --memory 64m \
  --cpuset-cpus 0 \
  --pids-limit 64 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=4194304 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  -e FRP_SERVER_ADDR=127.0.0.1 \
  -e FRP_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  "$FRPC_IMAGE" verify -c /config/frpc.toml
docker run --rm --platform linux/arm64 "$ADMIN_IMAGE" healthcheck

docker save -o "$ARTIFACT_DIR/chiphell-frpc-20260713-arm64.tar" "$FRPC_IMAGE"
docker save -o "$ARTIFACT_DIR/chiphell-proxy-admin-20260713-arm64.tar" "$ADMIN_IMAGE"
sha256sum "$ARTIFACT_DIR/chiphell-frpc-20260713-arm64.tar" >"$ARTIFACT_DIR/chiphell-frpc-20260713-arm64.tar.sha256"
sha256sum "$ARTIFACT_DIR/chiphell-proxy-admin-20260713-arm64.tar" >"$ARTIFACT_DIR/chiphell-proxy-admin-20260713-arm64.tar.sha256"

if [[ -n "${SUDO_USER:-}" ]]; then
  chown -R "$SUDO_USER":"$SUDO_USER" "$ARTIFACT_DIR"
fi
printf 'ROUTER_IMAGES_BUILD_OK\n'
