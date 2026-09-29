#!/usr/bin/env bash
set -euo pipefail
exec /srv/pylon/venv/bin/python /srv/pylon/profiles/default/scripts/gmail_cleanup_oauth.py "$@"
