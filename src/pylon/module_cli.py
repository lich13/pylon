"""Local module development and narrow, content-addressed deployment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from . import extensions, module_store
from .config import home


def workspace() -> Path:
    explicit = os.environ.get('PYLON_MODULES_DIR')
    if explicit: return Path(explicit).resolve()
    sibling = Path(__file__).resolve().parents[3] / 'pylon-tasks'
    return sibling if sibling.is_dir() else extensions.directory()


def affected(name, modules):
    if name == 'all': return set(modules)
    if name not in modules: raise ValueError('unknown module: ' + name)
    selected = {name}
    while True:
        parents = {key for key, m in modules.items() if selected.intersection(m.manifest.get('requires', []))}
        if parents <= selected: return selected
        selected.update(parents)


def check(name, source):
    modules = extensions.registry(source)
    module_store.validate_set(modules)
    selected = affected(name, modules)
    patterns = [pattern for key in sorted(selected) for pattern in modules[key].manifest.get('tests', [])]
    files = set()
    for pattern in patterns:
        if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise ValueError('test pattern escapes workspace')
        for path in source.glob(pattern):
            files.update(path.rglob('test_*.py') if path.is_dir() else [path])
    paths = sorted(str(path.relative_to(source)) for path in files if path.is_file())
    if not paths: raise ValueError('no tests registered for selected modules')
    env = os.environ.copy()
    gateway = Path(__file__).resolve().parents[2]
    env.update(PYLON_MODULES_DIR=str(source), PYLON_TASK_PYTHON=sys.executable,
               PYLON_GATEWAY_ROOT=str(gateway), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join([str(gateway / 'src'), str(source), *(str(m.root / 'src') for m in modules.values())]))
    print(json.dumps({'checking': sorted(selected), 'test_files': len(paths)}, ensure_ascii=False), flush=True)
    result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', *paths], cwd=source, env=env)
    if result.returncode: return result.returncode
    if 'content' in selected:
        for command in (['node', '--test', 'content/tools/linuxdo/linuxdo-following.test.mjs'],
                        ['npm', 'test', '--prefix', 'content/tools/chiphell']):
            result = subprocess.run(command, cwd=source, env=env)
            if result.returncode: return result.returncode
    return 0


def serve(name, service, profile):
    if os.geteuid() == 0: raise PermissionError('module services must run as an unprivileged user')
    with extensions.execution(inherit=True) as modules:
        config = modules[name].manifest.get('services', {})[service]
        os.execve(extensions.python(), [extensions.python(), '-m', config['entry'], *config.get('args', [])], extensions.environment(profile, modules))


def remote(host, arguments):
    # host is an SSH config alias; code/payload never enters a shell unquoted.
    if not host or host.startswith('-'): raise ValueError('invalid SSH alias')
    argv = ['sudo', '-n', '/srv/pylon/venv/bin/python', '-I', '-m', 'pylon.module_cli', *arguments]
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', host, shlex.join(argv)], check=True).returncode


def deploy(name, source, host, destination):
    modules = extensions.registry(source)
    module_store.validate_set(modules)
    ids = sorted(modules) if name == 'all' else [name]
    with tempfile.TemporaryDirectory(prefix='pylon-deploy-') as temporary:
        archives = []
        for key in ids:
            target = Path(temporary) / (key + '.tar.gz')
            receipt = module_store.pack(modules[key].root, target)
            archives.append((target, receipt['archive_sha256']))
            print(json.dumps(receipt), flush=True)
        if not host:
            if destination is None: raise ValueError('specify --host or --destination')
            print(json.dumps(module_store.install(archives, destination, services=False)))
            return 0
        uploaded = []
        try:
            import uuid
            for path, digest in archives:
                remote_path = '/tmp/pylon-module-' + uuid.uuid4().hex + '.tar.gz'
                subprocess.run(['scp', '-q', str(path), host + ':' + remote_path], check=True)
                uploaded.extend([remote_path, digest])
            return remote(host, ['install', *uploaded])
        finally:
            if uploaded:
                subprocess.run(['ssh', '-o', 'BatchMode=yes', host, shlex.join(['rm', '-f', '--', *uploaded[::2]])], check=True)


def main(arguments=None):
    parser = argparse.ArgumentParser(prog='pylon modules')
    parser.add_argument('--source', type=Path, default=workspace())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    c = sub.add_parser('check'); c.add_argument('module', nargs='?', default='all')
    p = sub.add_parser('pack'); p.add_argument('module'); p.add_argument('output', type=Path)
    d = sub.add_parser('deploy'); d.add_argument('module'); d.add_argument('--host'); d.add_argument('--destination', type=Path)
    r = sub.add_parser('rollback'); r.add_argument('module'); r.add_argument('--host'); r.add_argument('--destination', type=Path)
    i = sub.add_parser('install'); i.add_argument('archives', nargs='+'); i.add_argument('--destination', type=Path); i.add_argument('--no-services', action='store_true')
    s = sub.add_parser('serve'); s.add_argument('module'); s.add_argument('service'); s.add_argument('--profile', default='webops', choices=['webops', 'default'])
    e = sub.add_parser('exec'); e.add_argument('module'); e.add_argument('entry'); e.add_argument('args', nargs=argparse.REMAINDER)
    v = sub.add_parser('preview'); v.add_argument('module', choices=['content', 'gmail']); v.add_argument('--port', type=int, default=8795)
    c = sub.add_parser('ci'); c.add_argument('module', nargs='?', default='all'); c.add_argument('--router', action='store_true')
    args = parser.parse_args(arguments)
    source = args.source.resolve()
    if args.command == 'check': return check(args.module, source)
    if args.command == 'ci':
        from .module_ci import dispatch
        return dispatch(source, args.module, router=args.router)
    if args.command == 'list':
        print(json.dumps({k: {'api_version': m.manifest['api_version'], 'sha256': module_store.content_hash(m.root)} for k, m in extensions.registry(source).items()}, indent=2))
    elif args.command == 'pack':
        print(json.dumps(module_store.pack(extensions.registry(source)[args.module].root, args.output)))
    elif args.command == 'deploy': return deploy(args.module, source, args.host, args.destination)
    elif args.command == 'install':
        if len(args.archives) % 2: raise ValueError('expected archive/checksum pairs')
        print(json.dumps(module_store.install(list(zip(args.archives[::2], args.archives[1::2])), args.destination, services=not args.no_services)))
    elif args.command == 'rollback':
        if args.host: return remote(args.host, ['rollback', args.module])
        print(json.dumps(module_store.rollback(args.module, args.destination, services=args.destination is None)))
    elif args.command == 'serve': return serve(args.module, args.service, args.profile)
    elif args.command == 'exec': return extensions.exec_entry(args.module, args.entry, args.args, os.environ.get('PYLON_PROFILE', 'webops'))
    elif args.command == 'preview':
        os.environ['PYLON_MODULES_DIR'] = str(source)
        from .module_preview import run
        return run(args.port, args.module)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
