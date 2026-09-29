#!/usr/bin/env python3
from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from kdocs_electronics_quote_monitor import main

if __name__ == "__main__":
    raise SystemExit(main(["--mode", "force" if os.environ.get('PYLON_TRIGGER') == 'manual' else "main"]))
