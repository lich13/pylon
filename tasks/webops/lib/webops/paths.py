from __future__ import annotations

from pathlib import Path

PROFILE_ROOT = Path('/srv/hermes/profiles/webops')
SCRIPTS_DIR = PROFILE_ROOT / 'scripts'
CRON_STATE_DIR = PROFILE_ROOT / 'cron_state'
DATA_DIR = PROFILE_ROOT / 'data'
CHARTOO_DATA_ROOT = DATA_DIR / 'chartoo_itunes_daily'
CHIPHELL_DATA_ROOT = DATA_DIR / 'chiphell_fid26'
HERMES_AGENT_ROOT = Path('/srv/hermes/hermes-agent')
PYTHON = HERMES_AGENT_ROOT / 'venv' / 'bin' / 'python'
PLAYWRIGHT_BROWSERS_PATH = Path('/srv/hermes/playwright-browsers')

