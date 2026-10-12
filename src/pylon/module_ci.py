"""Private, short-lived CI inputs for business code kept outside Git."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid

from . import extensions, module_store


def gh(*args, **kwargs):
    return subprocess.check_output(['gh', *args], text=True, **kwargs).strip()


def bundle(source, target):
    modules = extensions.registry(source)
    entries = []
    for name, mod in modules.items():
        entries.extend((path, name + '/' + relative) for path, relative in module_store.files(mod.root))
    for path in sorted((source / 'tests').rglob('*')):
        relative = path.relative_to(source)
        if path.is_symlink(): raise ValueError('test symlink is not allowed')
        if any(part in module_store.SKIP_DIRS or part.startswith('.') for part in relative.parts): continue
        if path.is_file() and path.suffix in {'.py', '.json', '.html', '.txt', '.csv'}:
            entries.append((path, relative.as_posix()))
    entries.append((source / 'requirements.lock', 'requirements.lock'))
    with tarfile.open(target, 'w:gz') as archive:
        for path, name in sorted(entries, key=lambda x: x[1]):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), (0o755 if path.stat().st_mode & 0o111 else 0o644), 0
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(target.read_bytes()).hexdigest()


def extract(archive, destination, checksum):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise ValueError('CI input checksum mismatch')
    total, seen = 0, set()
    with tarfile.open(archive) as source:
        for member in source:
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or '..' in path.parts or member.name in seen or path.as_posix() != member.name:
                raise ValueError('unsafe CI source archive')
            total += member.size
            if total > 128 * 1024**2: raise ValueError('CI input too large')
            seen.add(member.name)
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as output:
                output.write(source.extractfile(member).read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def dispatch(source, module, router=False):
    gateway = Path(__file__).resolve().parents[2]
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=gateway, text=True).strip():
        raise ValueError('commit gateway changes before submitting a pinned CI check')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=gateway, text=True).strip()
    repo = json.loads(gh('repo', 'view', '--json', 'nameWithOwner,isPrivate', cwd=gateway))
    if not repo['isPrivate']: raise ValueError('external source CI requires a private repository')
    modules = extensions.registry(source)
    if module != 'all' and module not in modules: raise ValueError('unknown module')
    job = uuid.uuid4().hex[:24]
    tag = 'pylon-input-' + job
    with tempfile.TemporaryDirectory(prefix='pylon-ci-') as temporary:
        archive = Path(temporary) / 'source.tar.gz'
        checksum = bundle(source, archive)
        gh('release', 'create', tag, str(archive), '--repo', repo['nameWithOwner'], '--draft', '--target', revision,
           '--title', tag, '--notes', 'Temporary private module input; automatically removed after validation.')
        release = json.loads(gh('api', 'repos/' + repo['nameWithOwner'] + '/releases/tags/' + tag))
        try:
            gh('workflow', 'run', 'modules.yml', '--repo', repo['nameWithOwner'], '--ref', 'main',
               '-f', 'job=' + job, '-f', 'release_id=' + str(release['id']), '-f', 'sha256=' + checksum,
               '-f', 'core_sha=' + revision, '-f', 'module=' + module, '-f', 'router=' + str(router).lower())
        except BaseException:
            gh('release', 'delete', tag, '--repo', repo['nameWithOwner'], '--yes')
            raise
    print(json.dumps({'job': job, 'core_sha': revision, 'source_sha256': checksum, 'temporary_release_id': release['id']}))
    return 0
