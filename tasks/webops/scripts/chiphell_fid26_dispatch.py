#!/usr/bin/env python3
from __future__ import annotations

import time
from pathlib import Path

from chiphell_proxy_runtime import DeliveryQueue


QUEUE_PATH = Path(__file__).resolve().parents[1] / "data" / "chiphell_fid26" / "delivery.sqlite3"


def main() -> int:
    messages = DeliveryQueue(QUEUE_PATH).claim(limit=20, now=int(time.time()))
    if not messages:
        print('{"wakeAgent": false}')
        return 0
    print("\n\n".join(messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
