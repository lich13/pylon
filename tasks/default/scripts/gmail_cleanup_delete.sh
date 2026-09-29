#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME=/srv/hermes
PYTHON=/srv/hermes/venvs/google-workspace/bin/python
SCRIPT=/srv/hermes/scripts/gmail_cleanup_delete.py

if [ "$#" -eq 0 ]; then
  exec "$PYTHON" "$SCRIPT" --format text --quiet-noop
fi

exec "$PYTHON" "$SCRIPT" "$@"
