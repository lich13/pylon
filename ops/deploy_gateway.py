"""Explicit gateway cutover; module-only updates use pylon modules deploy.

The staged tree and module archives must already have verified checksums and
root ownership. This operation drains existing work without killing it.
"""
from __future__ import annotations

import argparse
import contextlib
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
    # pip's editable .pth and dist-info must be readable by the runtime user,
    # including when this root operation starts with a private service umask.
    return subprocess.check_output([str(x) for x in argv], text=True, umask=0o022).strip()


def active(unit):
    return command(['systemctl', 'show', unit, '-p', 'ActiveState', '--value']) in {'active', 'activating'}


def active_timers():
    # The gateway catalog contains only core jobs. External modules own most
    # timers, and all existing trigger sources must stop before draining work.
    output = command(['systemctl', 'list-units', 'pylon-job-*.timer',
                      '--state=active,activating', '--plain', '--no-legend', '--no-pager'])
    units = []
    for line in output.splitlines():
        if not line.strip():
            continue
        unit = line.split()[0]
        if not re.fullmatch(r'pylon-job-[A-Za-z0-9_-]+\.timer', unit):
            raise ValueError('unexpected timer listing')
        units.append(unit)
    return sorted(set(units))


def ready_services(services):
    """A successful start request is not proof that the process stayed alive."""
    initial, restarts, observed = {}, {}, {}
    stable = 0
    for attempt in range(36):
        ready = True
        for unit in services:
            output = command(['systemctl', 'show', unit, '-p', 'ActiveState', '-p', 'SubState',
                              '-p', 'MainPID', '-p', 'NRestarts'])
            state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
            observed[unit] = state
            detail = json.dumps(state, sort_keys=True)
            if state.get('SubState') == 'auto-restart' or state.get('ActiveState') == 'failed':
                raise RuntimeError('service did not stay running: ' + unit + ' ' + detail)
            count = state.get('NRestarts')
            if count != restarts.setdefault(unit, count):
                raise RuntimeError('service restarted during startup: ' + unit + ' ' + detail)
            running = (state.get('ActiveState') == 'active' and state.get('SubState') == 'running'
                       and state.get('MainPID') not in (None, '', '0'))
            if not running:
                if unit in initial:
                    raise RuntimeError('service did not stay running: ' + unit + ' ' + detail)
                ready = False
                continue
            identity = (state['MainPID'], count)
            if identity != initial.setdefault(unit, identity):
                raise RuntimeError('service restarted during startup: ' + unit + ' ' + detail)
        stable = stable + 1 if ready else 0
        if stable == 6 or not services:
            return
        if attempt < 35:
            time.sleep(1)
    raise RuntimeError('service startup did not settle: ' + json.dumps(observed, sort_keys=True))


def control_guard(services):
    # The bot reconciles the poller with its timer. During cutover a stopped
    # timer means quiescing, not a user pause. Keep that existing controller
    # locked until both services and their original timers have been restored.
    if 'chiphell-fid26-poller.service' in services:
        from pylon.admin.chiphell import cookie_import_lock
        return cookie_import_lock()
    return contextlib.nullcontext()


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
    linked_previous = previous.is_dir()
    if linked_previous:
        for name in FILES:
            target = ROOT / name
            if target.exists() and (not target.is_symlink() or target.resolve() != previous / name):
                raise ValueError('current gateway is not entirely linked to the recorded revision')
    original_links = {name: os.readlink(ROOT / name) for name in FILES if (ROOT / name).is_symlink()}
    registry_path = ROOT / 'modules/registry.json'
    registry_before = registry_path.read_bytes() if registry_path.exists() else None
    # Prepare the build backend before quiescing production. Installation and
    # rollback must not depend on a package-index request during downtime.
    command([PYTHON, '-c', 'import setuptools; assert int(setuptools.__version__.split(".")[0]) >= 68'])
    timers = active_timers()
    services = [unit for unit in ('pylon-bot.service', 'pylon-market-web.service', 'chiphell-fid26-poller.service',
                                 'pylon-gmail-web.service', 'pylon-gmail-worker.service') if active(unit)]
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
        if not linked_previous:
            previous.mkdir(parents=True)
        for name in FILES:
            target = ROOT / name
            if name in original_links:
                target.unlink()
                moved.append(name)
            elif target.exists():
                target.rename(previous / name)
                moved.append(name)
            if (stage / name).exists():
                target.symlink_to(stage / name, target_is_directory=(stage / name).is_dir())
                linked.append(name)
        command([PYTHON, '-m', 'pip', 'uninstall', '-y', 'pylon-tasks'])
        command([PYTHON, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', ROOT])
        command(['/usr/sbin/runuser', '-u', 'pylon', '--', PYTHON, '-I', '-c',
                 'import pylon.cli, pylon.sdk'])
        command([PYTHON, '-I', '-m', 'pylon.module_cli', 'install', '--no-services', *args.modules])
        command([PYTHON, ROOT / 'ops/bootstrap.py'])
        if storage_identity() != state['data_before']:
            raise RuntimeError('business data changed during source-only migration')
        state['data_unchanged'] = True
        (ROOT / 'REVISION').write_text(args.revision + '\n')
        switched = True
        state['phase'] = 'starting'
        save(state_path, state)
        with control_guard(services):
            if services: command(['systemctl', 'start', *services])
            ready_services(services)
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
            if name in original_links:
                (ROOT / name).symlink_to(original_links[name])
            else:
                (previous / name).rename(ROOT / name)
        if registry_before is not None and registry_path.read_bytes() != registry_before:
            temporary = registry_path.with_suffix('.rollback')
            temporary.write_bytes(registry_before)
            temporary.chmod(0o644)
            temporary.replace(registry_path)
        if previous.exists() and not any(previous.iterdir()):
            previous.rmdir()
        if moved:
            command([PYTHON, '-m', 'pip', 'uninstall', '-y', 'pylon-gateway'])
            command([PYTHON, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', ROOT])
            command([PYTHON, ROOT / 'ops/bootstrap.py'])
            (ROOT / 'REVISION').write_text(previous_revision + '\n')
        with control_guard(services):
            if services: command(['systemctl', 'start', *services])
            if timers: command(['systemctl', 'start', *timers])
        state['phase'] = 'failed-old-restored'
        save(state_path, state)
        raise


if __name__ == '__main__': main()
