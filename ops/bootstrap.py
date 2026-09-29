"""Install native pylon units without enabling or starting a scheduler."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess

ROOT = Path('/srv/pylon')
LIB = Path('/usr/local/lib/pylon')
UNITS = Path('/etc/systemd/system')


def calendar(expression):
    minute, hour, day, month, weekday = expression.split()
    if month != '*' or weekday != '*':
        raise ValueError('unsupported schedule')
    def field(value):
        return '0/' + value[2:] if value.startswith('*/') else value
    return f'*-*-{field(day)} {field(hour)}:{field(minute)}:00 Asia/Shanghai'


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
    # Writable profile data is separate from privileged imports. Only fixed
    # root-owned copies below LIB are ever imported by a privileged process.
    LIB.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chown(LIB, 0, 0)
    LIB.chmod(0o755)
    shutil.copytree(ROOT / 'src/pylon', LIB / 'pylon', dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for path in [LIB / 'pylon', *(LIB / 'pylon').rglob('*')]:
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() else 0o644)
    for name in ['admin.py', 'ssh_admin.py']:
        install_file(ROOT / 'ops' / name, LIB / name)
    install_file(ROOT / 'jobs.json', LIB / 'jobs.json')
    install_file(ROOT / 'tasks/webops/scripts/chiphell_proxy_runtime.py', LIB / 'chiphell_proxy_runtime.py')
    launcher = Path('/usr/local/sbin/pylon-admin')
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/lib/pylon/admin.py "$@"\n')
    launcher.chmod(0o755)
    cli = Path('/usr/local/bin/pylon')
    cli.write_text('#!/bin/sh\nif [ "$(id -un)" = pylon ]; then\n  exec /srv/pylon/venv/bin/pylon "$@"\nfi\nexec /usr/bin/sudo -n -u pylon -- /srv/pylon/venv/bin/pylon "$@"\n')
    cli.chmod(0o755)
    install_file(ROOT / 'ops/chiphell_proxyctl.py', Path('/usr/local/sbin/chiphell-proxyctl'), 0o755)
    for profile in ['default', 'webops']:
        target = ROOT / 'profiles' / profile
        for child in ['scripts', 'lib', 'chartoo', 'plugins']:
            source = ROOT / 'tasks' / profile / child
            if source.exists():
                shutil.copytree(source, target / child, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for service in ['pylon-bot.service', 'pylon-ssh-load.service', 'chiphell-fid26-poller.service']:
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
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('native units prepared; no scheduler started')


if __name__ == '__main__':
    main()
