#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env node "$ROOT/bin/status.mjs"
