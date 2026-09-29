"""Prepare pylon without starting its bots, scheduler, or replacing Hermes."""
import hashlib
import json
import os
import pwd
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path('/srv/pylon')


def main():
    try:
        owner = pwd.getpwnam('pylon')
    except KeyError:
        subprocess.run(['useradd', '--system', '--home-dir', '/srv/pylon/home', '--shell', '/usr/sbin/nologin', 'pylon'], check=True)
        owner = pwd.getpwnam('pylon')
    for child in ['data', 'run', 'home', 'secrets', 'dagu', 'dagu/dags', 'dagu/data', 'dagu/logs']:
        p = ROOT / child
        p.mkdir(parents=True, exist_ok=True)
        os.chown(p, owner.pw_uid, owner.pw_gid)
        p.chmod(0o700)
    for profile in ['default', 'webops']:
        target = ROOT / 'profiles' / profile
        target.mkdir(parents=True, exist_ok=True)
        for child in ['scripts', 'lib', 'chartoo', 'plugins']:
            source = ROOT / 'tasks' / profile / child
            if source.exists():
                shutil.copytree(source, target / child, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    config_path = ROOT / 'dagu/config.yaml'
    if not config_path.exists():
        queues = sorted({j.get('lock', key) for key, j in json.loads((ROOT / 'jobs.json').read_text()).items()})
        config = dict(host='127.0.0.1', port=18090, base_path='/pylon', default_execution_mode='local', check_updates=False,
                      auth={'mode':'builtin','builtin':{'token':{'secret':secrets.token_hex(32),'ttl':'24h'}}},
                      ui={'navbar_title':'pylon'}, coordinator={'enabled':False,'health_port':0}, scheduler={'port':0},
                      worker={'health_port':0}, terminal={'enabled':False}, tunnel={'enabled':False},
                      git_sync={'enabled':False}, opencode={'executable':'/usr/bin/false'},
                      queues={'enabled':True,'config':[{'name':q,'max_concurrency':1} for q in queues]})
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        config_path.chmod(0o600)
        os.chown(config_path, owner.pw_uid, owner.pw_gid)
    # DAGs are installed only after initial authentication and scheduler pause.
    for service in ['pylon-bot.service', 'pylon-dagu.service']:
        shutil.copyfile(ROOT / 'ops' / service, Path('/etc/systemd/system') / service)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('runtime prepared; no service started')


if __name__ == '__main__':
    main()
