from __future__ import annotations

import os

from .paths import PLAYWRIGHT_BROWSERS_PATH


def ensure() -> str:
    return os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(PLAYWRIGHT_BROWSERS_PATH))

