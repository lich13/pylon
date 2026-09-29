#!/usr/bin/env bash
set -euo pipefail

export HERMES_HOME=/srv/pylon/profiles/default
PYTHON=/srv/pylon/venv/bin/python
SCRIPT=/srv/pylon/profiles/default/scripts/gmail_cleanup_delete.py

if [ "$#" -eq 0 ]; then
  exec "$PYTHON" "$SCRIPT" --format text --quiet-noop
fi

exec "$PYTHON" "$SCRIPT" "$@"
