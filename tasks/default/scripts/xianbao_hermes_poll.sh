#!/usr/bin/env bash
set -u

export HERMES_HOME="${HERMES_HOME:-/srv/pylon/profiles/default}"
ENV_FILE="$HERMES_HOME/config/xianbao-filter.env"
DATA_DIR="$HERMES_HOME/data/xianbao_filter"
WORKER="$HERMES_HOME/scripts/xianbao_hermes_worker.py"
ALERT_SCRIPT="$HERMES_HOME/scripts/xianbao_alert.py"
FAILURE_STATE="$DATA_DIR/cron_poll_failures"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

mkdir -p "$DATA_DIR"

OUT_FILE="$(mktemp "$DATA_DIR/xianbao-poll-out.XXXXXX")"
ERR_FILE="$(mktemp "$DATA_DIR/xianbao-poll-err.XXXXXX")"
cleanup() {
  rm -f "$OUT_FILE" "$ERR_FILE"
}
trap cleanup EXIT

/srv/pylon/venv/bin/python "$WORKER" --once >"$OUT_FILE" 2>"$ERR_FILE"
RC=$?

if [[ "$RC" -eq 0 ]]; then
  rm -f "$FAILURE_STATE"
  exit 0
fi

cat "$ERR_FILE" >&2
cat "$OUT_FILE" >&2

COUNT=1
if [[ -f "$FAILURE_STATE" ]]; then
  PREV="$(tr -cd '0-9' < "$FAILURE_STATE" | head -c 9)"
  if [[ -n "$PREV" ]]; then
    COUNT=$((PREV + 1))
  fi
fi
printf '%s\n' "$COUNT" > "$FAILURE_STATE"
chmod 600 "$FAILURE_STATE" 2>/dev/null || true

THRESHOLD="${XIANBAO_ALERT_CONSECUTIVE_ERRORS:-3}"
case "$THRESHOLD" in
  ''|*[!0-9]*) THRESHOLD=3 ;;
esac

if [[ "$THRESHOLD" -gt 0 && "$COUNT" -ge "$THRESHOLD" && -f "$ALERT_SCRIPT" ]]; then
  DETAIL="$(tail -n 1 "$ERR_FILE" | cut -c1-600)"
  if [[ -z "$DETAIL" ]]; then
    DETAIL="$(tail -n 1 "$OUT_FILE" | cut -c1-600)"
  fi
  /srv/pylon/venv/bin/python "$ALERT_SCRIPT" \
    --kind runtime-error \
    --unit xianbao-hermes-poll \
    --journal-lines 0 \
    --message "worker loop has ${COUNT} consecutive poll errors; latest error: ${DETAIL}" \
    >/dev/null 2>&1 || true
fi

exit "$RC"
