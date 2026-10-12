"""Explicit gateway cutover; module-only updates use pylon modules deploy.

The staged tree and module archives must already have verified checksums and
root ownership. This operation drains existing work without killing it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

ROOT = Path('/srv/pylon')
PYTHON = ROOT / 'venv/bin/python'
FILES = ('src', 'ops', 'jobs.json', 'pyproject.toml', 'requirements.lock', 'README.md', 'AGENTS.md')


def command(argv):
    return subprocess.check_output([str(x) for x in argv], text=True).strip()


def active(unit):
    return command(['systemctl', 'show', unit, '-p', 'ActiveState', '--value']) in {'active', 'activating'}


def query(path, sql):
    if not path.exists(): return []
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        return db.execute(sql).fetchall()


def pending():
    updates = query(ROOT / 'data/outbox.sqlite3', "SELECT count(*) FROM updates WHERE status IN ('pending','running')")
    agents = query(Path('/var/lib/pylon-agents/tasks.sqlite3'), "SELECT status,count(*) FROM tasks WHERE status IN ('running','queued') GROUP BY status")
    tasks = command(['systemctl', 'list-units', 'pylon-task@*.service', 'pylon-manual@*.service',
                     'pylon-agent.service',
                     'pylon-update@*.service', 'pylon-apt-update@*.service', 'pylon-apt-batch@*.service',
                     '--state=active,activating,deactivating', '--no-legend', '--no-pager']).splitlines()
    return {'commands': updates[0][0] if updates else 0, 'agent_states': agents,
            'services': [line.split()[0] for line in tasks]}


def quiet(value):
    return not value['commands'] and not value['agent_states'] and not value['services']


def data_identity(path=None):
    """Only hashes leave the read-only connection; no user records in receipts."""
    result = {}
    path = path or ROOT / 'data/outbox.sqlite3'
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            if not re.fullmatch('[A-Za-z0-9_]+', name): raise ValueError('unexpected table name')
            digest = hashlib.sha256()
            count = 0
            try:
                rows = db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')
            except sqlite3.OperationalError:
                keys = sorted((row[5], row[1]) for row in db.execute('PRAGMA table_info("' + name + '")') if row[5])
                rows = db.execute('SELECT * FROM "' + name + '" ORDER BY ' + ','.join('"' + key + '"' for _, key in keys))
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=False, default=lambda x: x.hex()).encode() + b'\n')
                count += 1
            result[name] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def storage_identity():
    """Cover module databases and JSON state as well as the central outbox."""
    result = {}
    for directory in (ROOT / 'data', ROOT / 'profiles'):
        for path in sorted(directory.rglob('*')):
            if not path.is_file() or path.is_symlink(): continue
            name = path.relative_to(ROOT).as_posix()
            if path.suffix in {'.db', '.sqlite', '.sqlite3'}:
                result[name] = data_identity(path)
            elif path.suffix == '.json':
                result[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def validate_stage(stage, revision):
    if stage != ROOT / 'gateway-releases' / revision:
        raise ValueError('unexpected staged gateway location')
    for path in [stage, *stage.rglob('*')]:
        stat = path.lstat()
        if path.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise PermissionError('unsafe gateway stage')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--modules', nargs='+', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0: raise PermissionError('root required')
    if not re.fullmatch('[0-9a-f]{40}', args.revision) or len(args.modules) % 2:
        raise ValueError('invalid cutover inputs')
    stage = args.stage.resolve(strict=True)
    validate_stage(stage, args.revision)
    state_path = stage / 'cutover.json'
    if state_path.exists(): raise ValueError('cutover already submitted; inspect its recorded state')
    previous_revision = (ROOT / 'REVISION').read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}', previous_revision): raise ValueError('invalid previous revision')
    previous = ROOT / 'gateway-releases' / previous_revision
    if previous.exists(): raise ValueError('previous source snapshot already exists; inspect before cutover')
    # Prepare the build backend before quiescing production. Installation and
    # rollback must not depend on a package-index request during downtime.
    command([PYTHON, '-c', 'import setuptools; assert int(setuptools.__version__.split(".")[0]) >= 68'])
    catalog = json.loads((ROOT / 'jobs.json').read_text())
    timers = [f'pylon-job-{key}.timer' for key in catalog if active(f'pylon-job-{key}.timer')]
    services = [unit for unit in ('pylon-bot.service', 'pylon-market-web.service', 'chiphell-fid26-poller.service') if active(unit)]
    state = {'revision': args.revision, 'previous': previous_revision, 'phase': 'draining', 'timers': timers, 'services': services}
    save(state_path, state)
    moved, linked, switched = [], [], False
    try:
        if timers: command(['systemctl', 'stop', *timers])
        # No deadline: silence from a model is never permission to kill it.
        last = None
        while True:
            value = pending()
            if value != last:
                state['pending'] = value
                save(state_path, state)
                print(json.dumps({'phase': 'draining', **value}), flush=True)
                last = value
            if quiet(value): break
            time.sleep(5)
        if services: command(['systemctl', 'stop', *services])
        # Close the small window between the drain observation and stopping
        # new command intake. Never replace source under a started task.
        value = pending()
        if not quiet(value): raise RuntimeError('new work started during quiesce; old services restored')
        state['phase'] = 'installing'
        state['data_before'] = storage_identity()
        save(state_path, state)
        previous.mkdir(parents=True)
        for name in FILES:
            target = ROOT / name
            if target.exists():
                target.rename(previous / name)
                moved.append(name)
            if (stage / name).exists():
                target.symlink_to(stage / name, target_is_directory=(stage / name).is_dir())
                linked.append(name)
        command([PYTHON, '-m', 'pip', 'uninstall', '-y', 'pylon-tasks'])
        command([PYTHON, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', ROOT])
        command([PYTHON, '-I', '-m', 'pylon.module_cli', 'install', '--no-services', *args.modules])
        command([PYTHON, ROOT / 'ops/bootstrap.py'])
        if storage_identity() != state['data_before']:
            raise RuntimeError('business data changed during source-only migration')
        state['data_unchanged'] = True
        (ROOT / 'REVISION').write_text(args.revision + '\n')
        switched = True
        state['phase'] = 'starting'
        save(state_path, state)
        if services: command(['systemctl', 'start', *services])
        for unit in services:
            if not active(unit): raise RuntimeError('service did not start: ' + unit)
        if timers: command(['systemctl', 'start', *timers])
        state['phase'] = 'complete'
        save(state_path, state)
        print(json.dumps({'phase': 'complete', 'revision': args.revision}), flush=True)
    except BaseException as error:
        state['phase'] = 'rolling-back'
        state['error_type'] = type(error).__name__
        save(state_path, state)
        if switched and services: command(['systemctl', 'stop', *services])
        for name in linked:
            target = ROOT / name
            if target.is_symlink() and target.resolve() == stage / name: target.unlink()
        for name in moved:
            (previous / name).rename(ROOT / name)
        if previous.exists() and not any(previous.iterdir()):
            previous.rmdir()
        if moved:
            command([PYTHON, '-m', 'pip', 'uninstall', '-y', 'pylon-gateway'])
            command([PYTHON, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', ROOT])
            command([PYTHON, ROOT / 'ops/bootstrap.py'])
            (ROOT / 'REVISION').write_text(previous_revision + '\n')
        if services: command(['systemctl', 'start', *services])
        if timers: command(['systemctl', 'start', *timers])
        state['phase'] = 'failed-old-restored'
        save(state_path, state)
        raise


if __name__ == '__main__': main()
