#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/srv/hermes}"
PY="${XIANBAO_PYTHON:-/usr/bin/python3}"

exec "$PY" "$HERMES_HOME/scripts/xianbao_retention_cleanup.py" "$@"
