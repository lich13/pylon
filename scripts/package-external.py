"""Package the verified private input only after the module matrix has passed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


def select_runtime(candidates):
    available = {}
    for path in candidates.glob('*/runtime.json'):
        version = json.loads(path.read_text())['python']
        if version in available:
            raise ValueError('ambiguous runtime candidate')
        available[version] = path.parent
    for version in ('3.14.10', '3.12.16'):
        if version in available:
            return available[version]
    raise ValueError('no compatible runtime')


def download_runtime(candidate, destination):
    verified = json.loads((candidate / 'runtime.json').read_text())
    subprocess.run([
        'gh', 'run', 'download', os.environ['GITHUB_RUN_ID'],
        '--repo', os.environ['GITHUB_REPOSITORY'],
        '--name', f"built-runtime-{os.environ['JOB']}-{verified['python']}",
        '--dir', str(destination),
    ], check=True)
    downloaded = json.loads((destination / 'runtime.json').read_text())
    if downloaded != verified:
        raise ValueError('runtime compatibility metadata mismatch')
    with (destination / 'pylon-runtime.tar.gz').open('rb') as archive:
        if hashlib.file_digest(archive, 'sha256').hexdigest() != verified['sha256']:
            raise ValueError('runtime checksum mismatch')
    return destination


def main():
    temporary = Path(os.environ["RUNNER_TEMP"])
    archive = temporary / "input/source.tar.gz"
    checksum = os.environ["SOURCE_SHA"]
    if hashlib.sha256(archive.read_bytes()).hexdigest() != checksum:
        raise ValueError("CI source checksum mismatch")
    source = temporary / "pylon-tasks"
    seen, total = set(), 0
    with tarfile.open(archive) as bundle:
        for member in bundle:
            path = Path(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or member.name in seen or path.as_posix() != member.name):
                raise ValueError("unsafe CI source archive")
            total += member.size
            if total > 128 * 1024**2:
                raise ValueError("CI source archive too large")
            seen.add(member.name)
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(bundle.extractfile(member).read())
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    sys.path.insert(0, str(source / "sdk/src"))
    from pylon_sdk.ci import package
    # Successful compatibility jobs upload metadata; download only the chosen archive.
    candidates = Path(os.environ['RUNTIMES_DIR'])
    selected = download_runtime(select_runtime(candidates), temporary / 'built-runtime')
    metadata = package(source, temporary / "external-dist", os.environ["CORE_SHA"], checksum, selected)
    print('Selected Python ' + metadata['runtime']['python'])


if __name__ == "__main__":
    main()
