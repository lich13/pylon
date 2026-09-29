#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

if /usr/bin/env node "$ROOT/bin/sync-brave-cookies.mjs"; then
  printf '\nChiphell 登录态同步完成。\n'
  exit 0
else
  status=$?
fi

printf '\nChiphell 登录态同步失败，请根据上方阶段码排查。\n' >&2
if [[ -t 0 ]]; then
  read -r -p '按回车关闭窗口...' _
fi
exit "$status"
