#!/usr/bin/env bash
# Upgrade a CI-built gateway. A failed cutover restores the previous binary,
# retains live databases and leaves admission frozen. Never retry a cutover here.
set -euo pipefail
umask 077

HOME_DIR=/srv/pylon
RELEASE_DIR=""
PREVIOUS_VERSION=""
DRAIN_TIMEOUT=300
HEALTH_TIMEOUT=60
KEEP_FROZEN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) HOME_DIR=$2; shift 2 ;;
    --release-dir) RELEASE_DIR=$2; shift 2 ;;
    --previous-version) PREVIOUS_VERSION=$2; shift 2 ;;
    --drain-timeout) DRAIN_TIMEOUT=$2; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT=$2; shift 2 ;;
    --keep-frozen) KEEP_FROZEN=1; shift ;;
    *) echo "unknown argument" >&2; exit 2 ;;
  esac
done
[[ -n "$RELEASE_DIR" && $(id -u) -eq 0 ]] || { echo "root and --release-dir required" >&2; exit 2; }
[[ "$DRAIN_TIMEOUT" =~ ^[1-9][0-9]*$ && "$HEALTH_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || exit 2
RELEASE_DIR=$(cd "$RELEASE_DIR" && pwd)
BIN="$RELEASE_DIR/pylon-linux-x86_64"
CURRENT="$HOME_DIR/bin/pylon"
[[ -x "$BIN" && -x "$CURRENT" && -f "$HOME_DIR/REVISION" ]] || { echo "release or current installation incomplete" >&2; exit 2; }

# Validate the CI binary before executing it, and restrict manifest paths.
python3 - "$RELEASE_DIR" <<'PY'
import hashlib, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
lines = (root / "SHA256SUMS").read_text().splitlines()
assert len(lines) == 1, "invalid checksum manifest"
expected, name = lines[0].split()
assert name in ("dist/pylon-linux-x86_64", "pylon-linux-x86_64"), "unexpected checksum path"
with (root / "pylon-linux-x86_64").open("rb") as binary:
    assert hashlib.file_digest(binary, "sha256").hexdigest() == expected, "checksum mismatch"
assert re.fullmatch(r"[0-9a-f]{40}", (root / "REVISION").read_text().strip()), "invalid revision"
PY
REVISION=$(tr -d '\n' <"$RELEASE_DIR/REVISION")
VERSION=$("$BIN" --version | awk '{print $2}')
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 2
PREVIOUS_BIN=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CURRENT")
OLD_VERSION=$("$CURRENT" --version | awk '{print $2}')
OLD_REVISION=$(tr -d '\n' <"$HOME_DIR/REVISION")
[[ "$OLD_REVISION" =~ ^[0-9a-f]{40}$ ]] || exit 2
[[ -z "$PREVIOUS_VERSION" || "$PREVIOUS_VERSION" == "$OLD_VERSION" ]] || exit 2
TARGET_DIR="$HOME_DIR/releases/v$VERSION-$REVISION"
WORK_DIR=$(mktemp -d)
SUCCESS=0
HEALTH=unverified
STOP_ATTEMPTED=0
ROLLBACK=not_needed
SERVICES=(pylon-gateway.service pylon-executor.service pylon-broker.service)

# Bound every probe by the remaining deadline, including a hung diagnose RPC.
limited() {
  python3 - "$@" <<'PY'
import subprocess, sys
try:
    result = subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]))
    sys.exit(result.returncode if result.returncode >= 0 else 1)
except subprocess.TimeoutExpired:
    sys.exit(124)
PY
}
switch_binary() {
  python3 - "$HOME_DIR" "$1" "$2" <<'PY'
import os, pathlib, sys, tempfile
root, binary, revision = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
fd, link = tempfile.mkstemp(prefix=".pylon-", dir=root / "bin")
os.close(fd)
os.unlink(link)
try:
    os.symlink(binary, link)
    os.replace(link, root / "bin/pylon")
finally:
    if os.path.lexists(link): os.unlink(link)
fd, record = tempfile.mkstemp(prefix=".REVISION-", dir=root)
try:
    with os.fdopen(fd, "w") as out:
        out.write(revision + "\n")
        out.flush()
        os.fchmod(out.fileno(), 0o644)
        os.fsync(out.fileno())
    os.replace(record, root / "REVISION")
finally:
    if os.path.exists(record): os.unlink(record)
for directory in (root / "bin", root):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
PY
}
healthy() {
  local expected=$1 deadline=$((SECONDS + HEALTH_TIMEOUT)) remaining
  while (( SECONDS < deadline )); do
    remaining=$((deadline - SECONDS))
    if limited "$remaining" systemctl is-active --quiet "${SERVICES[@]}" >"$WORK_DIR/command.log" 2>&1 &&
       (( (remaining = deadline - SECONDS) > 0 )) &&
       limited "$remaining" "$CURRENT" --home "$HOME_DIR" diagnose >"$WORK_DIR/diagnose.json" 2>"$WORK_DIR/command.log" &&
       python3 - "$HOME_DIR" "$WORK_DIR/diagnose.json" "$expected" <<'PY'
import json, pathlib, socket, sys
root = pathlib.Path(sys.argv[1])
try:
    status = json.loads(pathlib.Path(sys.argv[2]).read_text())
    assert status.get("version") == sys.argv[3]
    assert status.get("maintenance") is True
    for name in ("executor", "broker"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(str(root / f"run/{name}.sock"))
except (OSError, ValueError, AssertionError):
    sys.exit(1)
PY
    then
      return 0
    fi
    (( SECONDS < deadline )) && sleep 1
  done
  return 1
}
cleanup() {
  local code=$? frozen=false
  trap - EXIT
  set +e
  if [[ $SUCCESS -ne 1 ]]; then
    [[ $code -ne 0 ]] || code=1
    if [[ $STOP_ATTEMPTED -eq 1 ]]; then
      ROLLBACK=blocked
      # Recreate freeze if thaw removed it before failing. A rollback never thaws.
      if limited 10 "$BIN" --home "$HOME_DIR" freeze >"$WORK_DIR/command.log" 2>&1 &&
         [[ -f "$HOME_DIR/run/freeze" ]] &&
         limited 60 systemctl stop "${SERVICES[@]}" >>"$WORK_DIR/command.log" 2>&1 &&
         limited 10 "$BIN" --home "$HOME_DIR" downgrade-check --target-version "$OLD_VERSION" >>"$WORK_DIR/command.log" 2>&1; then
        if switch_binary "$PREVIOUS_BIN" "$OLD_REVISION" >>"$WORK_DIR/command.log" 2>&1 &&
           limited 60 systemctl start "${SERVICES[@]}" >>"$WORK_DIR/command.log" 2>&1 && healthy "$OLD_VERSION"; then
          ROLLBACK=restored
          HEALTH=previous_healthy
        else
          ROLLBACK=failed
        fi
      fi
    fi
  fi
  [[ -e "$HOME_DIR/run/freeze" ]] && frozen=true
  python3 - "$SUCCESS" "$VERSION" "$REVISION" "$ROLLBACK" "$frozen" "$code" "$HEALTH" "$OLD_VERSION" "$OLD_REVISION" <<'PY'
import json, sys
success, version, revision, rollback, frozen, code, health, old_version, old_revision = sys.argv[1:]
active_version = version if success == "1" else old_version if rollback == "restored" else None
active_revision = revision if success == "1" else old_revision if rollback == "restored" else None
print(json.dumps({"phase": "complete" if success == "1" else "failed", "version": version,
                  "revision": revision, "active_version": active_version, "active_revision": active_revision,
                  "health": health, "rollback": rollback, "freeze": frozen == "true", "exit": int(code)}))
PY
  rm -f "$WORK_DIR/diagnose.json" "$WORK_DIR/command.log"
  rmdir "$WORK_DIR"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Preserve immutable releases, including the previous rollback target.
if [[ -e "$TARGET_DIR" ]]; then
  cmp "$BIN" "$TARGET_DIR/dist/pylon-linux-x86_64"
  cmp "$RELEASE_DIR/REVISION" "$TARGET_DIR/dist/REVISION"
  cmp "$RELEASE_DIR/SHA256SUMS" "$TARGET_DIR/dist/SHA256SUMS"
else
  install -d -o root -g root -m 0755 "$TARGET_DIR/dist"
  install -o root -g root -m 0755 "$BIN" "$TARGET_DIR/dist/pylon-linux-x86_64"
  install -o root -g root -m 0644 "$RELEASE_DIR/REVISION" "$RELEASE_DIR/SHA256SUMS" "$TARGET_DIR/dist/"
fi
limited 10 "$BIN" --home "$HOME_DIR" freeze >"$WORK_DIR/command.log" 2>&1
[[ -f "$HOME_DIR/run/freeze" ]]
deadline=$((SECONDS + DRAIN_TIMEOUT))
drained=0
while (( SECONDS < deadline )); do
  if limited "$((deadline - SECONDS))" "$BIN" --home "$HOME_DIR" drain-status >"$WORK_DIR/command.log" 2>&1; then
    drained=1
    break
  fi
  (( SECONDS < deadline )) && sleep 1
done
[[ $drained -eq 1 ]]
limited 10 "$BIN" --home "$HOME_DIR" downgrade-check --target-version "$OLD_VERSION" >"$WORK_DIR/command.log" 2>&1
STOP_ATTEMPTED=1
limited 60 systemctl stop "${SERVICES[@]}" >"$WORK_DIR/command.log" 2>&1
limited 10 "$BIN" --home "$HOME_DIR" drain-status >"$WORK_DIR/command.log" 2>&1
limited 10 "$BIN" --home "$HOME_DIR" downgrade-check --target-version "$OLD_VERSION" >"$WORK_DIR/command.log" 2>&1
switch_binary "$TARGET_DIR/dist/pylon-linux-x86_64" "$REVISION"
limited 60 systemctl start "${SERVICES[@]}" >"$WORK_DIR/command.log" 2>&1
healthy "$VERSION"
HEALTH=target_healthy
if [[ $KEEP_FROZEN -eq 0 ]]; then
  limited 10 "$CURRENT" --home "$HOME_DIR" thaw >"$WORK_DIR/command.log" 2>&1
  [[ ! -e "$HOME_DIR/run/freeze" ]]
fi
SUCCESS=1
