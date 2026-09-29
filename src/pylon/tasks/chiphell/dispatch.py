#!/usr/bin/env python3
from __future__ import annotations

import time
from pathlib import Path

from pylon.tasks.chiphell.runtime import DeliveryQueue


from pylon.shared.paths import CHIPHELL_DATA_ROOT

QUEUE_PATH = CHIPHELL_DATA_ROOT / "delivery.sqlite3"


def main() -> int:
    messages = DeliveryQueue(QUEUE_PATH).claim(limit=20, now=int(time.time()))
    if not messages:
        print('{"wakeAgent": false}')
        return 0
    print("\n\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
