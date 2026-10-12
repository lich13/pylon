#!/usr/bin/python3
"""Root-only entrypoint with a fixed command and unit allowlist."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

CATALOG = Path('/srv/pylon/jobs.json')
CHIPHELL = '63c6ab6c4af3'


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    if os.geteuid() != 0:
        raise PermissionError('root required')
    if args and args[0] == 'agent':
        from .agents import main as agent_main
        return agent_main(args[1:])
    if args and args[0] == 'software':
        from .software import main as software_main
        return software_main(args[1:])
    if args == ['chiphell-reconcile']:
        from .chiphell import main as chiphell_main
        return chiphell_main(['reconcile'])
    if len(args) >= 2 and args[0] == 'chiphell' and args[1] in {'status', 'test', 'baseline', 'pause', 'resume', 'import-cookies'}:
        if args[1] in {'status', 'test', 'baseline', 'pause', 'resume', 'import-cookies'} and len(args) != 2:
            raise ValueError('unsupported Chiphell arguments')
        from .chiphell import main as chiphell_main
        return chiphell_main(args[1:])
    if len(args) >= 2 and args[:2] == ['ssh', 'travel']:
        if len(args) != 3 or args[2] not in {'status', 'open', 'close'}:
            raise ValueError('unsupported SSH travel action')
        from .ssh_travel import main as travel_main
        return travel_main(args[2:])
    if args and args[0] == 'ssh':
        if len(args) < 2 or args[1] not in {'status', 'check', 'sync', 'refresh'}:
            raise ValueError('unsupported SSH action')
        from .ssh import main as ssh_main
        return ssh_main(args[1:])
    from ..extensions import registry
    catalog = json.loads(CATALOG.read_text())
    for mod in registry(CATALOG.parent / 'modules').values():
        for key, job in mod.manifest.get('jobs', {}).items():
            if key in catalog: raise ValueError('duplicate task')
            catalog[key] = job
    if len(args) != 2 or args[0] not in {'pause', 'resume', 'run', 'logs'} or args[1] not in catalog:
        raise ValueError('unsupported action or task')
    action, key = args
    if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
        raise ValueError('invalid task id')
    timer = f'pylon-job-{key}.timer'
    services = [f'pylon-{kind}@{key}.service' for kind in ('task', 'manual')]
    if action in {'pause', 'resume'}:
        if key == CHIPHELL:
            command(['/usr/local/sbin/chiphell-proxyctl', action])
        else:
            command(['/usr/bin/systemctl', 'disable' if action == 'pause' else 'enable', '--now', timer], capture_output=True)
    elif action == 'run':
        # Oneshoot services report activating while ExecStart is executing.
        for service in services:
            state = command(['/usr/bin/systemctl', 'show', service, '-p', 'ActiveState', '--value'], capture_output=True).stdout.strip()
            if state in {'active', 'activating'}:
                print('任务正在运行')
                return 0
        command(['/usr/bin/systemctl', 'start', '--no-block', services[1]], capture_output=True)
        print('已提交执行')
    else:
        command(['/usr/bin/journalctl', '-u', services[0], '-u', services[1], '-n', '60', '--no-pager', '-o', 'cat'])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
