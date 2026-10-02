"""Publish one confirmation card for each newly discovered safe package."""
from __future__ import annotations

from pylon import software_updates
from pylon.software_inventory import check


def main() -> int:
    snapshot = check(max_age=0)
    published = software_updates.publish_candidates(snapshot)
    # Cards are persisted in the outbox; the scheduler must stay silent.
    print('[SILENT]')
    return 0 if published >= 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
