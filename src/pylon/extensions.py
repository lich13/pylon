"""Validated external modules. The gateway never imports their Python code."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .config import home, redact

API_VERSION = 1
IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,63}')
PYTHON_MODULE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')


@dataclass(frozen=True)
class Module:
    id: str
    root: Path
    manifest: dict


def directory() -> Path:
    return Path(os.environ.get('PYLON_MODULES_DIR', str(home() / 'modules'))).resolve()


def read_module(root: Path, expected: str | None = None) -> Module:
    root = root.resolve(strict=True)
    data = json.loads((root / 'module.json').read_text())
    name = data.get('id', '')
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or expected not in (None, name):
        raise ValueError('invalid module identifier')
    if type(data.get('api_version')) is not int or data['api_version'] != API_VERSION:
        raise ValueError('incompatible module API')
    if type(data.get('data_version', 1)) is not int or data.get('data_version', 1) != 1:
        raise ValueError('module requires an explicit data migration')
    for key in ('handler',):
        if not isinstance(data.get(key), str) or not PYTHON_MODULE.fullmatch(data[key]):
            raise ValueError('invalid module entrypoint')
    packages = data.get('packages', [])
    if not packages or any(not isinstance(p, str) or not PYTHON_MODULE.fullmatch(p) or not p.startswith('pylon_tasks.') for p in packages):
        raise ValueError('invalid module packages')
    for name in (data['handler'], *([data['preview']] if data.get('preview') else []), *(j.get('module', '') for j in data.get('jobs', {}).values()), *data.get('cli', {}).values()):
        if not any(name == p or name.startswith(p + '.') for p in packages):
            raise ValueError('entrypoint is outside declared packages')
        entry = root / 'src' / name.replace('.', '/')
        if not entry.with_suffix('.py').is_file() and not (entry / '__main__.py').is_file():
            raise ValueError('module entrypoint missing: ' + name)
    for key, job in data.get('jobs', {}).items():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key) or job.get('profile') not in {'default', 'webops'}:
            raise ValueError('invalid external task')
        if 'admin_action' in job or 'argv' in job or 'python' in job:
            raise ValueError('external tasks cannot declare privileged execution')
        if job.get('timeout') is not None and (type(job['timeout']) is not int or job['timeout'] <= 0):
            raise ValueError('invalid task timeout')
        if not isinstance(job.get('args', []), list) or any(not isinstance(x, str) for x in job.get('args', [])):
            raise ValueError('invalid task arguments')
        if not isinstance(job.get('name'), str) or '\n' in job['name']:
            raise ValueError('invalid task name')
        if not re.fullmatch(r'[0-9*/ ,\-]+', job.get('schedule', '')) or len(job['schedule'].split()) != 5:
            raise ValueError('invalid task schedule')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', job.get('lock', key)):
            raise ValueError('invalid task lock')
    for item in data.get('commands', []):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', item.get('name', '')):
            raise ValueError('invalid command')
        if set(item) - {'name', 'description', 'route', 'action', 'argument', 'example', 'detail', 'menu', 'profiles', 'order', 'profile_error'}:
            raise ValueError('unknown command field')
        if not item.get('profiles') or set(item['profiles']) - {'default', 'webops'}:
            raise ValueError('invalid command profiles')
        if not isinstance(item.get('description'), str) or not isinstance(item.get('route'), str):
            raise ValueError('invalid command metadata')
        if type(item.get('order', 50)) is not int:
            raise ValueError('invalid command order')
    for callback in data.get('callbacks', []):
        if not isinstance(callback, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+\|', callback):
            raise ValueError('invalid callback prefix')
    if not (root / 'src').is_dir():
        raise ValueError('module source missing')
    # Namespace directories cannot shadow the installed, root-owned gateway.
    for path in (root / 'src').iterdir():
        if path.name.startswith('.') or path.name == '__pycache__': continue
        if path.name != 'pylon_tasks':
            raise ValueError('module may only provide the pylon_tasks namespace')
    namespace = root / 'src/pylon_tasks'
    if (namespace / '__init__.py').exists():
        raise ValueError('pylon_tasks must remain a namespace package')
    for item in namespace.iterdir():
        if item.name.startswith('.') or item.name == '__pycache__': continue
        if 'pylon_tasks.' + item.name not in packages:
            raise ValueError('undeclared source package')
    for key, service in data.get('services', {}).items():
        if not IDENTIFIER.fullmatch(key): raise ValueError('invalid service identifier')
        entry = service.get('entry', '')
        if entry != 'uvicorn' and not any(entry == p or entry.startswith(p + '.') for p in packages):
            raise ValueError('invalid service entrypoint')
        if set(service) - {'entry', 'args'} or not isinstance(service.get('args', []), list) or any(not isinstance(x, str) for x in service.get('args', [])):
            raise ValueError('invalid service arguments')
    return Module(data['id'], root, data)


def registry(base: Path | None = None) -> dict[str, Module]:
    base = (base or directory()).resolve()
    index = base / 'registry.json'
    if index.exists():
        data = json.loads(index.read_text())
        if data.get('api_version') != API_VERSION or not isinstance(data.get('modules'), dict):
            raise ValueError('invalid module registry')
        roots = {}
        for name, relative in data['modules'].items():
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError('invalid module location')
            root = (base / relative).resolve(strict=True)
            if not root.is_relative_to(base):
                raise ValueError('module escapes registry')
            roots[name] = root
    else:
        roots = {p.parent.name: p.parent for p in sorted(base.glob('*/module.json'))}
    modules = {key: read_module(root, key) for key, root in roots.items()}
    owners = {key: {} for key in ('jobs', 'commands', 'callbacks', 'cli', 'packages')}
    from .command_catalog import CORE_COMMANDS
    owners['commands'].update({key: 'gateway' for key in CORE_COMMANDS})
    owners['callbacks'].update({key: 'gateway' for key in ('st1|', 'su1|', 'su2|', 'ch1|', 'ch2|')})
    core_jobs = home() / 'jobs.json'
    if core_jobs.exists():
        owners['jobs'].update({key: 'gateway' for key in json.loads(core_jobs.read_text())})
    for mod in modules.values():
        for kind in owners:
            items = mod.manifest.get(kind, {})
            names = [item['name'] for item in items] if kind == 'commands' else items
            for name in names:
                if name in owners[kind]:
                    raise ValueError(f'duplicate {kind} registration: {name}')
                if kind in {'packages', 'callbacks'} and any(name.startswith(old + ('.' if kind == 'packages' else '')) or old.startswith(name + ('.' if kind == 'packages' else '')) for old in owners[kind]):
                    raise ValueError(f'overlapping {kind} registration')
                owners[kind][name] = mod.id
        for required in mod.manifest.get('requires', []):
            if required not in modules:
                raise ValueError(f'missing dependency: {required}')
    return modules


def python() -> str:
    return os.environ.get('PYLON_TASK_PYTHON') or (str(home() / 'venv/bin/python') if (home() / 'venv/bin/python').exists() else sys.executable)


def environment(profile: str, modules: dict[str, Module] | None = None) -> dict[str, str]:
    from .runner import task_environment
    env = task_environment(profile)
    modules = modules if modules is not None else snapshot()
    # Resolve every immutable release before spawning. Children inherit the
    # same snapshot even if a module is deployed while a model is waiting.
    pinned = {name: str(mod.root) for name, mod in modules.items()}
    env['PYLON_MODULE_SNAPSHOT'] = json.dumps(pinned)
    core_src = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = os.pathsep.join([core_src, *(str(m.root / 'src') for m in modules.values())])
    env['PYLON_TASK_PYTHON'] = python()
    return env


def snapshot() -> dict[str, Module]:
    value = os.environ.get('PYLON_MODULE_SNAPSHOT')
    if value:
        return {key: read_module(Path(root), key) for key, root in json.loads(value).items()}
    return registry()


def task(job_id: str):
    modules = snapshot()
    for mod in modules.values():
        if job_id in mod.manifest.get('jobs', {}):
            return mod, mod.manifest['jobs'][job_id], modules
    return None


def invoke(module_id: str, action: str, profile: str = 'webops', **payload):
    if os.geteuid() == 0:
        raise PermissionError('external code must run as an unprivileged user')
    request = {'api_version': API_VERSION, 'action': action, 'profile': profile, 'payload': payload}
    with execution() as modules:
        mod = modules[module_id]
        env = environment(profile, modules)
        process = subprocess.run([python(), '-m', mod.manifest['handler']], input=json.dumps(request),
                                 env=env, cwd=mod.root, text=True, capture_output=True, timeout=150)
    if process.returncode:
        raise RuntimeError(f'{module_id} handler failed: ' + redact(process.stderr[-1200:]))
    response = json.loads(process.stdout)
    if response.get('api_version') != API_VERSION or set(response) != {'api_version', 'value'}:
        raise ValueError('invalid module response')
    return response['value']


def hook(hook_name: str, profile: str = 'webops', **payload):
    for mod in snapshot().values():
        if hook_name in mod.manifest.get('hooks', []):
            value = invoke(mod.id, hook_name, profile, **payload)
            if value is not None:
                return value
    return None


def exec_entry(module_id: str, entry: str, args: list[str], profile: str = 'webops'):
    if os.geteuid() == 0: raise PermissionError('external code must run as an unprivileged user')
    with execution(inherit=True) as modules:
        mod = modules[module_id]
        if not any(entry == p or entry.startswith(p + '.') for p in mod.manifest['packages']):
            raise ValueError('entrypoint is outside module')
        os.execve(python(), [python(), '-m', entry, *args], environment(profile, modules))


@contextmanager
def execution(inherit=False):
    """Resolve and lease a snapshot under the same lock used by deployment."""
    lock_path = directory() / '.leases.lock'
    lock = lock_path.open('r') if lock_path.exists() else None
    try:
        if lock: fcntl.flock(lock, fcntl.LOCK_SH)
        modules = snapshot()
        guard = lease(modules, inherit=inherit)
        guard.__enter__()
    finally:
        if lock: lock.close()
    try:
        yield modules
    finally:
        guard.__exit__(None, None, None)


@contextmanager
def lease(modules, inherit=False):
    """Keep old releases alive for the entire lifetime of their consumers."""
    base = home() / 'run/module-leases'
    base.mkdir(parents=True, exist_ok=True)
    path = base / (uuid.uuid4().hex + '.json')
    with path.open('x+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps([str(m.root) for m in modules.values()]))
        handle.flush()
        os.set_inheritable(handle.fileno(), inherit)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


def document_module(profile: str, message: dict) -> str | None:
    document = message.get('document')
    if not isinstance(document, dict): return None
    caption = str(message.get('caption', '')).split(maxsplit=1)[0:1]
    for mod in registry().values():
        for item in mod.manifest.get('documents', []):
            if profile in item['profiles'] and (document.get('file_name') == item['filename'] or caption == [item['caption']]):
                return mod.id
    return None
