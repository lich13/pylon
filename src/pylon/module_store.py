"""Immutable business releases and atomic registration, independent of Git."""
from __future__ import annotations

from contextlib import contextmanager
import ast
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from . import extensions
from .config import home

SOURCE_DIRS = {'src', 'config', 'tools', 'ci'}
SKIP_DIRS = {'__pycache__', 'node_modules', '.git', '.venv', '.pytest_cache',
             'artifacts', 'profiles', 'private', 'secrets', 'logs', 'cron_state'}
PRIVATE_NAMES = {'cookies.json', 'google_token.json', 'token.json', 'auth.json', 'credentials.json'}
GENERATED_SUFFIXES = {'.pyc', '.pyo', '.db', '.sqlite', '.sqlite3', '.session', '.pem', '.key',
                      '.p12', '.pfx', '.log', '.tar', '.gz', '.zip', '.whl'}


def source_name(path):
    return (not any(part in SKIP_DIRS or part.startswith('.') for part in path.parts)
            and path.name not in PRIVATE_NAMES and path.suffix not in GENERATED_SUFFIXES)


def files(root: Path):
    """Only module code/config is deployable; runtime data is never packaged."""
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not source_name(relative):
            continue
        if relative.as_posix() != 'module.json' and relative.parts[0] not in SOURCE_DIRS:
            continue
        if path.is_symlink(): raise ValueError('module sources cannot contain symlinks')
        if path.is_file():
            yield path, relative.as_posix()


def content_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path, name in files(root):
        data = path.read_bytes()
        mode = b'x' if path.stat().st_mode & 0o111 else b'-'
        digest.update(name.encode() + b'\0' + mode + str(len(data)).encode() + b'\0' + data)
    return digest.hexdigest()


def pack(root: Path, target: Path) -> dict:
    mod = extensions.read_module(root)
    with tarfile.open(target, 'w:gz') as archive:
        for path, name in files(root):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), (0o755 if path.stat().st_mode & 0o111 else 0o644), 0
            archive.addfile(info, io.BytesIO(data))
    return {'module': mod.id, 'content_sha256': content_hash(root),
            'archive_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}


def unpack(archive: Path, target: Path, expected: str):
    if not re.fullmatch('[0-9a-f]{64}', expected) or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise ValueError('module archive checksum mismatch')
    total = 0
    with tarfile.open(archive) as bundle:
        seen = set()
        for member in bundle:
            name = Path(member.name)
            if (not member.isfile() or name.is_absolute() or '..' in name.parts or
                    name.as_posix() != member.name or member.name in seen or
                    not source_name(name)):
                raise ValueError('unsafe or duplicate archive entry')
            if member.name != 'module.json' and name.parts[0] not in SOURCE_DIRS:
                raise ValueError('archive includes non-source content')
            total += member.size
            if member.size > 32 * 1024**2 or total > 128 * 1024**2:
                raise ValueError('module archive too large')
            seen.add(member.name)
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open('xb') as output:
                shutil.copyfileobj(bundle.extractfile(member), output)
            dest.chmod(0o755 if member.mode & 0o111 else 0o644)


def atomic_json(path: Path, value):
    fd, name = tempfile.mkstemp(prefix='.registry-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(base):
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o755)
    lease_lock = base / '.leases.lock'
    lease_lock.touch(exist_ok=True)
    lease_lock.chmod(0o644)
    with (base / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def index(base):
    path = base / 'registry.json'
    return json.loads(path.read_text()) if path.exists() else {'api_version': 1, 'modules': {}, 'previous': {}}


def validate_set(modules):
    """Check the entire proposed registry, including collisions with the core."""
    from .command_catalog import CORE_COMMANDS
    jobs_path = home() / 'jobs.json'
    jobs = set(json.loads(jobs_path.read_text())) if jobs_path.exists() else set()
    commands = set(CORE_COMMANDS)
    callbacks = {'st1|', 'su1|', 'su2|', 'ch1|', 'ch2|'}
    packages, aliases = set(), set()
    for mod in modules.values():
        for path in (mod.root / 'src').rglob('*.py'):
            ast.parse(path.read_text(), filename=str(path))
        if set(mod.manifest.get('requires', [])) - set(modules):
            raise ValueError('missing dependencies for ' + mod.id)
        for kind, owned, names in (
            ('task', jobs, mod.manifest.get('jobs', {})),
            ('command', commands, [c['name'] for c in mod.manifest.get('commands', [])]),
            ('callback', callbacks, mod.manifest.get('callbacks', [])),
            ('package', packages, mod.manifest['packages']),
            ('cli', aliases, mod.manifest.get('cli', {})),
        ):
            for name in names:
                if name in owned or (kind == 'package' and any(name.startswith(old + '.') or old.startswith(name + '.') for old in owned)):
                    raise ValueError('duplicate ' + kind + ': ' + name)
                owned.add(name)


def health(modules, ids):
    """Import/validate candidates without production credentials or root code execution."""
    with tempfile.TemporaryDirectory(prefix='pylon-module-health-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        for child in ['data', 'run', 'home', 'profiles/default', 'profiles/webops']:
            (root / child).mkdir(parents=True, exist_ok=True)
        prefix = []
        if os.geteuid() == 0:
            import pwd
            user = pwd.getpwnam('pylon')
            for path in [root, *root.rglob('*')]: os.chown(path, user.pw_uid, user.pw_gid)
            prefix = ['/usr/sbin/runuser', '-u', 'pylon', '--']
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(root / 'home'),
               'PYLON_HOME': str(root), 'PYLON_TASK_PYTHON': extensions.python(),
               'PYLON_MODULE_SNAPSHOT': json.dumps({k: str(m.root) for k, m in modules.items()}),
               'PYTHONPATH': os.pathsep.join([str(Path(__file__).resolve().parents[1]), *(str(m.root / 'src') for m in modules.values())]),
               'PYTHONDONTWRITEBYTECODE': '1'}
        for name in ids:
            request = {'api_version': 1, 'action': 'health', 'profile': 'webops', 'payload': {}}
            proc = subprocess.run([*prefix, extensions.python(), '-m', modules[name].manifest['handler']],
                                  input=json.dumps(request), env=env, capture_output=True, text=True, timeout=60)
            if proc.returncode or json.loads(proc.stdout).get('value', {}).get('status') != 'ok':
                from .config import redact
                raise ValueError(name + ' health check failed: ' + redact(proc.stderr)[-1200:])


def running(modules):
    jobs = [key for mod in modules for key in mod.manifest.get('jobs', {})]
    if not jobs: return False
    units = [f'pylon-{kind}@{job}.service' for job in jobs for kind in ('task', 'manual')]
    proc = subprocess.run(['systemctl', 'show', *units, '-p', 'ActiveState', '--value'], capture_output=True, text=True, check=True)
    return any(state in {'active', 'activating', 'deactivating'} for state in proc.stdout.splitlines())


def service_units(ids):
    path = home() / 'ops/module-services.json'
    bindings = json.loads(path.read_text()) if path.exists() else {}
    # These bindings live with the trusted gateway, never in uploaded manifests.
    return [unit for name in ids for unit in bindings.get(name, {}).values()]


def systemctl(*args):
    return subprocess.run(['systemctl', *args], check=True, capture_output=True, text=True).stdout.strip()


def activate(base, prior, proposed, changed, *, services=True):
    modules = {key: extensions.read_module(base / path, key) for key, path in proposed['modules'].items()}
    validate_set(modules)
    health(modules, changed)
    old = {key: extensions.read_module(base / path, key) for key, path in prior['modules'].items()}
    schedule_changes = [name for name in changed if name not in old or old[name].manifest.get('jobs') != modules[name].manifest.get('jobs')]
    units = service_units(changed) if services else []
    active = [unit for unit in units if systemctl('show', unit, '-p', 'ActiveState', '--value') == 'active']
    switched = False
    def restore():
        nonlocal switched
        if switched:
            atomic_json(base / 'registry.json', prior)
            switched = False
            for unit in active: systemctl('restart', unit)
    try:
        with schedules(old, modules, schedule_changes if services else [], restore=restore):
            atomic_json(base / 'registry.json', proposed)
            switched = True
            for unit in active: systemctl('restart', unit)
            for unit in active:
                if systemctl('is-active', unit) != 'active': raise RuntimeError('module service failed: ' + unit)
    except BaseException:
        restore()
        raise
    with (base / 'deployments.jsonl').open('a') as log:
        log.write(json.dumps({'at': time.time(), 'modules': {name: proposed['modules'][name] for name in changed}}) + '\n')
        log.flush()
        os.fsync(log.fileno())
    prune(base)
    return {'modules': {name: Path(proposed['modules'][name]).name for name in changed}, 'restarted': active}


def install(archives, base=None, *, services=True):
    base = (base or extensions.directory()).resolve()
    with locked(base):
        prior = index(base)
        proposed = json.loads(json.dumps(prior))
        changed = []
        for archive, checksum in archives:
            with tempfile.TemporaryDirectory(prefix='.incoming-', dir=base) as temporary:
                root = Path(temporary)
                unpack(Path(archive), root, checksum)
                mod = extensions.read_module(root)
                digest = content_hash(root)
                release = base / 'releases' / mod.id / digest
                release.parent.mkdir(parents=True, exist_ok=True)
                (base / 'releases').chmod(0o755)
                release.parent.chmod(0o755)
                if release.exists():
                    if content_hash(release) != digest: raise ValueError('installed release was modified')
                else:
                    shutil.copytree(root, release)
                    for path in [release, *release.rglob('*')]:
                        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
                relative = release.relative_to(base).as_posix()
                if proposed['modules'].get(mod.id) == relative: continue
                if mod.id in proposed['modules']:
                    proposed.setdefault('previous', {})[mod.id] = proposed['modules'][mod.id]
                proposed['modules'][mod.id] = relative
                changed.append(mod.id)
        if not changed: return {'modules': {}, 'restarted': []}
        return activate(base, prior, proposed, changed, services=services)


def rollback(name, base=None, *, services=True):
    base = (base or extensions.directory()).resolve()
    with locked(base):
        prior = index(base)
        if name not in prior.get('previous', {}): raise ValueError('no previous release')
        proposed = json.loads(json.dumps(prior))
        proposed['modules'][name], proposed['previous'][name] = prior['previous'][name], prior['modules'][name]
        return activate(base, prior, proposed, [name], services=services)


def prune(base):
    with (base / '.leases.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _prune(base)


def _prune(base):
    """Remove only unreferenced releases; never delete a running snapshot."""
    data = index(base)
    retained = {(base / p).resolve() for key in ('modules', 'previous') for p in data.get(key, {}).values()}
    lease_dir = home() / 'run/module-leases'
    for path in lease_dir.glob('*.json'):
        try:
            with path.open('r') as handle:
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    retained.update(Path(p).resolve() for p in json.load(handle))
                    continue
            path.unlink(missing_ok=True)
        except (FileNotFoundError, PermissionError, ValueError):
            return  # Unknown ownership/state must never cause source deletion.
    releases = base / 'releases'
    for namespace in releases.iterdir() if releases.exists() else []:
        if not namespace.is_dir() or namespace.is_symlink(): continue
        for release in namespace.iterdir():
            if release.is_symlink() or release.resolve() in retained or not re.fullmatch('[0-9a-f]{64}', release.name): continue
            if content_hash(release) != release.name: continue
            # Exact validated release path, no shell or glob deletion.
            shutil.rmtree(release)


@contextmanager
def schedules(old, proposed, names, *, restore=lambda: None):
    """Update only affected timer units, preserving disabled schedules."""
    from .module_units import paths
    before = {key: job for name in names if name in old for key, job in old[name].manifest.get('jobs', {}).items()}
    after = {key: job for name in names for key, job in proposed[name].manifest.get('jobs', {}).items()}
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    saved, locks, timers, acquired, enabled = {}, [], [], set(), set()
    try:
        for key in sorted(changed):
            timer = f'pylon-job-{key}.timer'
            if systemctl('show', timer, '-p', 'ActiveState', '--value') == 'active':
                timers.append(timer)
                systemctl('stop', timer)
            state = subprocess.run(['systemctl', 'is-enabled', timer], capture_output=True, text=True)
            if state.stdout.strip() == 'enabled': enabled.add(timer)
            if key not in after and timer in enabled: systemctl('disable', timer)
            for lock_name in {job.get('lock', key) for job in (before.get(key), after.get(key)) if job}:
                if lock_name in acquired: continue
                path = home() / 'run' / (lock_name + '.lock')
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open('a')
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    raise RuntimeError('affected task is still running; wait before changing its descriptor')
                locks.append(handle)
                acquired.add(lock_name)
        for key in changed:
            for path, value in paths(key, after.get(key)).items():
                saved[path] = path.read_bytes() if path.exists() else None
                if value is None: path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value)
                    path.chmod(0o644)
        if changed: systemctl('daemon-reload')
        yield
        # New jobs start after the registry is active. Existing pause state
        # and schedules are preserved. No running business service is killed.
        for handle in locks: handle.close()
        locks.clear()
        for key in after.keys() - before.keys():
            systemctl('enable', '--now', f'pylon-job-{key}.timer')
        for timer in timers:
            if timer.removeprefix('pylon-job-').removesuffix('.timer') in after:
                systemctl('start', timer)
    except BaseException:
        # Restore the old registry before any timer can start another task.
        for key in changed:
            subprocess.run(['systemctl', 'stop', f'pylon-job-{key}.timer'], capture_output=True)
        restore()
        for key in after.keys() - before.keys():
            subprocess.run(['systemctl', 'disable', '--now', f'pylon-job-{key}.timer'], capture_output=True)
        for path, value in saved.items():
            if value is None: path.unlink(missing_ok=True)
            else: path.write_bytes(value)
        if saved: systemctl('daemon-reload')
        for timer in enabled: systemctl('enable', timer)
        for timer in timers: systemctl('start', timer)
        raise
    finally:
        for handle in locks: handle.close()
