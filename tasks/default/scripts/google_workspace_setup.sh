#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/srv/hermes/hermes-agent${PYTHONPATH:+:$PYTHONPATH}"
exec /srv/hermes/venvs/google-workspace/bin/python \
  /srv/hermes/skills/productivity/google-workspace/scripts/setup.py "$@"
