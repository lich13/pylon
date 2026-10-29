#!/usr/bin/env python3
"""Build or reuse the offline module runtime locally; never uploads module source.

The build runs in a pinned Ubuntu 24.04 root filesystem (same glibc as the
server and the former CI runner) through `sudo chroot`, using the external
source's own `deployment/build_runtime.py`. Results are cached by a key over
the Python version, dependency locks and build scripts, so ordinary module
changes reuse the existing runtime.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

FORMAT = 1
ARCHIVE = 'pylon-runtime.tar.gz'
# Inputs that change the runtime bytes. Module source is deliberately absent.
KEY_FILES = (
    'requirements.lock',
    'deployment/bootstrap.lock',
    'deployment/build_runtime.py',
    'deployment/runtime.py',
    'sdk/pyproject.toml',
    'privileged/pyproject.toml',
)
# Copied into the build root; build_runtime.py wheels sdk and privileged.
BUILD_INPUTS = (*KEY_FILES, 'sdk/src', 'privileged/src')
BASE_NAME = 'ubuntu-base-24.04.5-base-amd64.tar.gz'
BASE_URL = 'https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/' + BASE_NAME
BASE_SHA256 = 'e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86'
# Same build dependencies the former ubuntu-24.04 runner installed, plus what
# the bare base image lacks (python3 for the driver, certificates, pkg-config).
APT_PACKAGES = (
    'ca-certificates', 'python3', 'pkg-config', 'xz-utils',
    'build-essential', 'libssl-dev', 'zlib1g-dev', 'libbz2-dev', 'libreadline-dev',
    'libsqlite3-dev', 'libffi-dev', 'liblzma-dev', 'libncurses-dev', 'uuid-dev',
    'libgdbm-dev', 'libgdbm-compat-dev', 'tk-dev',
)
MAX_TOTAL = 2 * 1024**3
MAX_MEMBERS = 100000
SKIP_PARTS = {'__pycache__', '.pytest_cache', 'build', 'dist'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def default_cache():
    configured = os.environ.get('PYLON_RUNTIME_CACHE')
    if configured:
        return Path(configured)
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'pylon/runtime'


def check_version(version):
    if not re.fullmatch(r'3\.\d+\.\d+', version):
        raise ValueError('invalid Python version')


def cache_key(source, version):
    check_version(version)
    source = Path(source)
    files = {}
    for relative in KEY_FILES:
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError('missing runtime input: ' + relative)
        files[relative] = digest(path)
    identity = {'format': FORMAT, 'python': version, 'files': files,
                'builder': digest(Path(__file__)), 'base': BASE_SHA256}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), identity


def check_archive(path):
    """Refuse what deployment/runtime.py extract() refuses, without extracting."""
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError('unsafe runtime archive')
        names, links, total = set(), set(), 0
        for item in members:
            name = Path(item.name)
            total += item.size
            if (not name.parts or name.is_absolute() or '..' in name.parts
                    or name.as_posix() != item.name or item.name in names
                    or not (item.isfile() or item.isdir() or item.issym()) or total > MAX_TOTAL):
                raise ValueError('unsafe runtime archive')
            names.add(item.name)
            if item.issym():
                target = Path(item.linkname)
                resolved = Path(os.path.normpath(str(name.parent / target)))
                if target.is_absolute() or '..' in resolved.parts:
                    raise ValueError('unsafe runtime link')
                links.add(item.name)
        for item in members:
            if any(parent.as_posix() in links for parent in Path(item.name).parents):
                raise ValueError('runtime archive traverses a link')
        if 'runtime.json' not in names:
            raise ValueError('runtime archive lacks metadata')
        inner = json.load(archive.extractfile('runtime.json'))
    return inner


def verify(directory, source, version, key):
    """Accept a cached runtime only when every recorded property still matches."""
    directory = Path(directory)
    metadata = json.loads((directory / 'runtime.json').read_text())
    record = json.loads((directory / 'build.json').read_text())
    archive = directory / ARCHIVE
    if (record.get('cache_key') != key or metadata.get('file') != ARCHIVE
            or metadata.get('python') != version or metadata.get('source_sha256') != key
            or metadata.get('requirements_sha256') != digest(Path(source) / 'requirements.lock')):
        raise ValueError('cached runtime metadata mismatch')
    if archive.is_symlink() or digest(archive) != metadata.get('sha256'):
        raise ValueError('cached runtime checksum mismatch')
    inner = check_archive(archive)
    if {k: v for k, v in metadata.items() if k not in ('file', 'sha256')} != inner:
        raise ValueError('cached runtime metadata mismatch')
    return metadata


def stage_inputs(source, destination):
    """Copy only lockfiles, build scripts, SDK and privileged package sources."""
    source = Path(source)
    for relative in BUILD_INPUTS:
        origin = source / relative
        if origin.is_symlink():
            raise ValueError('symlinked runtime input: ' + relative)
        if origin.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            continue
        if not origin.is_dir():
            raise ValueError('missing runtime input: ' + relative)
        for path in sorted(origin.rglob('*')):
            parts = path.relative_to(source).parts
            if SKIP_PARTS.intersection(parts) or any(part.endswith('.egg-info') for part in parts):
                continue
            if path.is_symlink():
                raise ValueError('symlinked runtime input: ' + path.relative_to(source).as_posix())
            target = destination / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif path.is_file() and path.suffix != '.pyc':
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


def privileged(args):
    return [str(arg) for arg in (args if os.geteuid() == 0 else ['sudo', '-n', *args])]


def base_image(cache):
    target = cache.parent / 'base' / BASE_NAME
    if not target.is_file() or digest(target) != BASE_SHA256:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix('.partial')
        with urllib.request.urlopen(BASE_URL, timeout=120) as incoming, partial.open('wb') as output:
            shutil.copyfileobj(incoming, output)
        if digest(partial) != BASE_SHA256:
            partial.unlink()
            raise ValueError('base image checksum mismatch')
        partial.replace(target)
    return target


DRIVER = '''import sys
from pathlib import Path
sys.path.insert(0, '/work/source/deployment')
import build_runtime
build_runtime.build(Path('/work/source'), Path('/work/build'), sys.argv[1], sys.argv[2], sys.argv[3])
'''


def mounted(path):
    path = os.path.realpath(path)
    with open('/proc/self/mounts') as mounts:
        return [line.split()[1] for line in mounts if line.split()[1].startswith(path + '/')]


def chroot_build(source, version, key, revision, work, cache):
    """Run the source's build_runtime.build() inside a fresh Ubuntu 24.04 root."""
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('runtime builds require Linux x86_64')
    image = base_image(cache)
    root = work / 'rootfs'
    staged = work / 'input'
    stage_inputs(source, staged)
    (work / 'driver.py').write_text(DRIVER)
    subprocess.run(privileged(['mkdir', root]), check=True)
    subprocess.run(privileged(['tar', '-xzf', image, '-C', root]), check=True)
    subprocess.run(privileged(['mkdir', '-p', root / 'work']), check=True)
    subprocess.run(privileged(['cp', '-r', staged, root / 'work/source']), check=True)
    subprocess.run(privileged(['cp', work / 'driver.py', root / 'work/driver.py']), check=True)
    subprocess.run(privileged(['cp', '--remove-destination', '/etc/resolv.conf', root / 'etc/resolv.conf']), check=True)
    environment = ['PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME=/root',
                   'LANG=C.UTF-8', 'DEBIAN_FRONTEND=noninteractive']
    environment += [name + '=' + os.environ[name] for name in
                    ('http_proxy', 'https_proxy', 'no_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')
                    if name in os.environ]
    script = ('set -e; apt-get update -q; apt-get install -y -q --no-install-recommends '
              + ' '.join(APT_PACKAGES) + '; exec python3 /work/driver.py "$@"')
    try:
        subprocess.run(privileged(['mount', '-t', 'proc', 'proc', root / 'proc']), check=True)
        subprocess.run(privileged(['mount', '--rbind', '/dev', root / 'dev']), check=True)
        subprocess.run(privileged(['mount', '--make-rslave', root / 'dev']), check=True)
        subprocess.run(privileged(['chroot', root, '/usr/bin/env', '-i', *environment,
                                   '/bin/sh', '-c', script, 'build', version, revision, key]), check=True)
        output = work / 'output'
        output.mkdir()
        for name in (ARCHIVE, 'runtime.json'):
            subprocess.run(privileged(['cp', root / 'work/build/dist' / name, output / name]), check=True)
            subprocess.run(privileged(['chown', f'{os.getuid()}:{os.getgid()}', output / name]), check=True)
        return output
    finally:
        cleanup_root(root)


def cleanup_root(root):
    for target in ('dev', 'proc'):
        if os.path.ismount(root / target):
            subprocess.run(privileged(['umount', '-R', root / target]), check=False)
    if mounted(root):
        raise RuntimeError('build root still has mounts; left in place: ' + str(root))
    if os.path.lexists(root):
        subprocess.run(privileged(['rm', '-rf', '--one-file-system', root]), check=True)


def gateway_revision():
    revision = subprocess.run(['git', '-C', str(Path(__file__).resolve().parents[1]), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True).stdout.strip()
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('gateway revision unavailable')
    return revision


@contextlib.contextmanager
def locked(cache):
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / '.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def ensure(source, version, cache, builder=chroot_build, revision=None):
    """Return the cached runtime directory, building it only when the key is new."""
    source, cache = Path(source).resolve(), Path(cache).resolve()
    key, identity = cache_key(source, version)
    final = cache / key
    with locked(cache):
        if final.exists():
            return final, verify(final, source, version, key), True
        revision = revision or gateway_revision()
        work = Path(tempfile.mkdtemp(prefix='.build-' + key[:12] + '-', dir=cache))
        try:
            output = builder(source, version, key, revision, work, cache)
            (output / 'build.json').write_text(json.dumps({
                'cache_key': key, 'inputs': identity, 'gateway_revision': revision,
                'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
            }, indent=2, sort_keys=True) + '\n')
            metadata = verify(output, source, version, key)
            output.rename(final)
        finally:
            cleanup_root(work / 'rootfs')
            shutil.rmtree(work, ignore_errors=True)
        return final, metadata, False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('key', 'build', 'verify'):
        command = sub.add_parser(name)
        command.add_argument('--source', type=Path, required=True, help='pylon-tasks directory')
        command.add_argument('--python', required=True)
        command.add_argument('--cache', type=Path, default=default_cache())
    args = parser.parse_args(argv)
    if args.action == 'key':
        print(cache_key(args.source, args.python)[0])
        return 0
    if args.action == 'verify':
        key = cache_key(args.source, args.python)[0]
        metadata = verify(args.cache.resolve() / key, args.source.resolve(), args.python, key)
        print(json.dumps({'directory': str(args.cache.resolve() / key), 'sha256': metadata['sha256']}))
        return 0
    directory, metadata, reused = ensure(args.source, args.python, args.cache)
    print(json.dumps({'directory': str(directory), 'archive': str(directory / ARCHIVE),
                      'sha256': metadata['sha256'], 'python': metadata['python'], 'reused': reused}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
