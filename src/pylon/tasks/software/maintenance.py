"""Weekly maintenance digest; never installs software."""
import os
import time

from pylon.events import result
from pylon.outbox import Outbox
from pylon.software_inventory import check, render


def main():
    snapshot = check(max_age=0)
    apt = snapshot['apt']
    failed = apt.get('error') or any(row.get('error') for row in snapshot['rows'])
    stale = not apt.get('index_time') or time.time() - apt['index_time'] > 172800
    actionable = (failed or stale or apt.get('packages') or apt.get('reboot_required') or
                  any(row.get('available') or row.get('error') for row in snapshot['rows']))
    if actionable or os.environ.get('PYLON_TRIGGER') == 'manual':
        box = Outbox()
        run_id = os.environ.get('PYLON_RUN_ID', '')
        box.enqueue(profile='webops', job_id='software-maintenance', kind='text',
                    payload={'text': '每周维护汇总\n' + render(snapshot)},
                    key='maintenance:' + (run_id or str(snapshot['checked_at'])), run_id=run_id)
    if failed:
        result('failed', '部分软件版本检查未完成，详见维护汇总；未确认的版本不会标为最新版')
    print('[SILENT]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
