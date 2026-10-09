"""Install native pylon units without enabling or starting a scheduler."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

ROOT = Path('/srv/pylon')
UNITS = Path('/etc/systemd/system')


def calendar(expression):
    minute, hour, day, month, weekday = expression.split()
    if month != '*' or weekday not in {'*', '0', '1', '2', '3', '4', '5', '6', '7'}:
        raise ValueError('unsupported schedule')
    def field(value):
        return '0/' + value[2:] if value.startswith('*/') else value
    week = '' if weekday == '*' else ('Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[int(weekday)] + ' '
    return f'{week}*-*-{field(day)} {field(hour)}:{field(minute)}:00 Asia/Shanghai'


def timer_text(key, job):
    schedule = ('OnBootSec=1min\nOnUnitActiveSec=5min\nAccuracySec=30s' if job.get('monotonic') else
                f'OnCalendar={calendar(job["schedule"])}\nPersistent=true\nAccuracySec=1s')
    jitter = '\nRandomizedDelaySec=30m' if job.get('jitter') else ''
    return (f'[Unit]\nDescription=pylon {job["name"]}\n\n[Timer]\n{schedule}{jitter}\n'
            f'Unit=pylon-task@{key}.service\n\n[Install]\nWantedBy=timers.target\n')


def task_service(trigger):
    return f'''[Unit]
Description=pylon {trigger} task %i
After=network-online.target pylon-bot.service
Wants=network-online.target

[Service]
Type=oneshot
User=pylon
Group=pylon
UMask=0077
WorkingDirectory=/srv/pylon
Environment=HOME=/srv/pylon/home
Environment=PYLON_HOME=/srv/pylon
Environment=TZ=Asia/Shanghai
ExecStart=/srv/pylon/venv/bin/pylon execute %i --trigger {trigger}
TimeoutStartSec=2000
TimeoutStopSec=40
KillMode=mixed
ProtectHome=true
PrivateTmp=true
'''


def install_file(source, target, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    os.chown(target, 0, 0)
    target.chmod(mode)


def main():
    owner = pwd.getpwnam('pylon')
    ROOT.chmod(0o755)
    os.chown(ROOT, 0, 0)
    for child in ['data', 'run', 'home', 'secrets']:
        path = ROOT / child
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, owner.pw_uid, owner.pw_gid)
        path.chmod(0o700)
    # Privileged modules use the same root-owned installed package as tasks.
    # Refuse a writable import path before installing any sudo entrypoint.
    for tree in [ROOT / 'src', ROOT / 'venv']:
        for path in [tree, *tree.rglob('*')]:
            if path.is_symlink():
                continue
            stat = path.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022:
                raise PermissionError(f'unsafe privileged import path: {path}')
    launchers = {
        '/usr/local/sbin/pylon-admin': '#!/bin/sh\nexec /srv/pylon/venv/bin/python -I -m pylon.admin.control "$@"\n',
        '/usr/local/sbin/chiphell-proxyctl': '#!/bin/sh\nexec /srv/pylon/venv/bin/python -I -m pylon.admin.chiphell "$@"\n',
        '/usr/local/bin/pylon': '#!/bin/sh\nif [ "$(id -un)" = pylon ]; then\n  exec /srv/pylon/venv/bin/pylon "$@"\nfi\nexec /usr/bin/sudo -n -u pylon -- /srv/pylon/venv/bin/pylon "$@"\n',
    }
    for name, content in launchers.items():
        path = Path(name)
        path.write_text(content)
        os.chown(path, 0, 0)
        path.chmod(0o755)
    for service in ['pylon-bot.service', 'pylon-ssh-load.service', 'pylon-ssh-travel.service', 'chiphell-fid26-poller.service',
                    'pylon-update@.service', 'pylon-apt-update@.service', 'pylon-apt-batch@.service', 'pylon-agent.service', 'pylon-market-web.service']:
        install_file(ROOT / 'ops' / service, UNITS / service)
    (UNITS / 'pylon-task@.service').write_text(task_service('scheduled'))
    (UNITS / 'pylon-manual@.service').write_text(task_service('manual'))
    catalog = json.loads((ROOT / 'jobs.json').read_text())
    for key, job in catalog.items():
        (UNITS / f'pylon-job-{key}.timer').write_text(timer_text(key, job))
    subprocess.run(['/usr/sbin/visudo', '-cf', str(ROOT / 'ops/pylon.sudoers')], check=True)
    install_file(ROOT / 'ops/pylon.sudoers', Path('/etc/sudoers.d/pylon'), 0o440)
    # A dedicated configuration is installed once, never rewritten by upgrades.
    config = Path('/etc/pylon/ssh.json')
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not config.exists():
        install_file(ROOT / 'ops/ssh-config.json', config)
    agent_config = Path('/etc/pylon/agents.json')
    if not agent_config.exists():
        install_file(ROOT / 'ops/agents-config.json', agent_config, 0o600)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('native units prepared; no scheduler started')


if __name__ == '__main__':
    main()
