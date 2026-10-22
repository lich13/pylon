#!/usr/bin/env bash
# Deploy a CI-built pylon binary with mandatory freeze / thaw handling.
# Usage: scripts/deploy-gateway.sh --home /srv/pylon --release-dir /path/to/extracted/dist
# Requires root. On success removes run/freeze; on any failure keeps freeze and exits non-zero.
set -euo pipefail

HOME_DIR=/srv/pylon
RELEASE_DIR=""
PREVIOUS_VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) HOME_DIR=$2; shift 2 ;;
    --release-dir) RELEASE_DIR=$2; shift 2 ;;
    --previous-version) PREVIOUS_VERSION=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$RELEASE_DIR" ]] || { echo "--release-dir required" >&2; exit 2; }
[[ "$(id -u)" -eq 0 ]] || { echo "must run as root" >&2; exit 2; }

BIN="$RELEASE_DIR/pylon-linux-x86_64"
SUMS="$RELEASE_DIR/SHA256SUMS"
REV="$RELEASE_DIR/REVISION"
[[ -x "$BIN" && -f "$SUMS" && -f "$REV" ]] || { echo "release dir incomplete" >&2; exit 2; }

REVISION=$(tr -d '[:space:]' <"$REV")
TARGET_DIR="$HOME_DIR/releases/v$( "$BIN" --version | awk '{print $2}' )-$REVISION"
CURRENT="$HOME_DIR/bin/pylon"
SUCCESS=0
# Prefer an installed binary for diagnose/thaw after switch; freeze must use a
# binary that understands the subcommand (old installs may still be pre-0.5.9).
PYLON_BIN=$BIN
if [[ -x "$CURRENT" ]]; then
  PYLON_BIN=$CURRENT
fi
FREEZE_BIN=$BIN
if [[ -x "$CURRENT" ]] && "$CURRENT" --help 2>/dev/null | grep -Eq '^[[:space:]]*freeze[[:space:]]'; then
  FREEZE_BIN=$CURRENT
fi

cleanup() {
  local code=$?
  local frozen=false
  [[ -e "$HOME_DIR/run/freeze" ]] && frozen=true
  if [[ $SUCCESS -eq 1 ]]; then
    if [[ -e "$HOME_DIR/run/freeze" ]]; then
      "$PYLON_BIN" --home "$HOME_DIR" thaw || rm -f "$HOME_DIR/run/freeze"
    fi
    echo "{\"phase\":\"complete\",\"revision\":\"$REVISION\",\"freeze\":false}"
  else
    echo "{\"phase\":\"failed\",\"revision\":\"$REVISION\",\"freeze\":$frozen,\"exit\":$code}" >&2
    if [[ -e "$HOME_DIR/run/freeze" ]]; then
      echo "KEEPING freeze marker at $HOME_DIR/run/freeze; inspect services before thawing." >&2
    fi
  fi
}
trap cleanup EXIT

"$FREEZE_BIN" --home "$HOME_DIR" freeze
# Verify freeze exists before stopping anything.
[[ -f "$HOME_DIR/run/freeze" ]]

# Install release tree
install -d -o root -g root -m 0755 "$TARGET_DIR/dist"
install -o root -g root -m 0755 "$BIN" "$TARGET_DIR/dist/pylon-linux-x86_64"
install -o root -g root -m 0644 "$SUMS" "$TARGET_DIR/dist/SHA256SUMS"
install -o root -g root -m 0644 "$REV" "$TARGET_DIR/dist/REVISION"
(
  cd "$TARGET_DIR"
  sha256sum -c dist/SHA256SUMS
)
[[ "$(cat "$TARGET_DIR/dist/REVISION")" == "$REVISION" ]]

if [[ -n "$PREVIOUS_VERSION" ]]; then
  "$TARGET_DIR/dist/pylon-linux-x86_64" --home "$HOME_DIR" downgrade-check --target-version "$PREVIOUS_VERSION"
fi

systemctl stop pylon-executor.service pylon-broker.service pylon-gateway.service

if [[ -n "$PREVIOUS_VERSION" ]]; then
  "$TARGET_DIR/dist/pylon-linux-x86_64" --home "$HOME_DIR" downgrade-check --target-version "$PREVIOUS_VERSION"
fi

ln -sfn "$TARGET_DIR/dist/pylon-linux-x86_64" "$HOME_DIR/bin/.pylon-new.tmp"
mv -Tf "$HOME_DIR/bin/.pylon-new.tmp" "$HOME_DIR/bin/pylon"
install -o root -g root -m 0644 "$TARGET_DIR/dist/REVISION" "$HOME_DIR/.REVISION.tmp"
mv -Tf "$HOME_DIR/.REVISION.tmp" "$HOME_DIR/REVISION"
python3 -c 'import os; [os.close(fd) for fd in [os.open(p, os.O_RDONLY|os.O_DIRECTORY) for p in ("'"$HOME_DIR/bin"'", "'"$HOME_DIR"'")]]'
PYLON_BIN="$HOME_DIR/bin/pylon"

systemctl start pylon-gateway.service pylon-executor.service pylon-broker.service
for _ in $(seq 1 30); do
  if "$PYLON_BIN" --home "$HOME_DIR" diagnose >/tmp/pylon-deploy-diagnose.json 2>/dev/null; then
    python3 - <<'PY'
import json
d=json.load(open("/tmp/pylon-deploy-diagnose.json"))
assert d.get("version"), "missing version"
print("diagnose_ok", d["version"], "maintenance", d.get("maintenance"))
PY
    break
  fi
  sleep 2
done
[[ "$(cat "$HOME_DIR/REVISION")" == "$REVISION" ]]
SUCCESS=1
