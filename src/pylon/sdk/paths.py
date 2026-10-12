from __future__ import annotations

from pathlib import Path
import os

from pylon.config import home, profile_root

PROFILE_ROOT = Path(os.environ.get('PYLON_PROFILE_ROOT') or profile_root(os.environ.get('PYLON_PROFILE', 'webops')))
CRON_STATE_DIR = PROFILE_ROOT / 'cron_state'
DATA_DIR = PROFILE_ROOT / 'data'
CHARTOO_DATA_ROOT = DATA_DIR / 'chartoo_itunes_daily'
CHIPHELL_DATA_ROOT = DATA_DIR / 'chiphell_fid26'
PYLON_ROOT = home()
PYTHON = Path(os.environ.get('PYLON_TASK_PYTHON') or PYLON_ROOT / 'venv/bin/python')
PLAYWRIGHT_BROWSERS_PATH = PYLON_ROOT / 'playwright-browsers'
