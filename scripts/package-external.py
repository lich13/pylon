"""Package the verified private input only after the module matrix has passed."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile


def select_runtime(candidates):
    for version in ('3.14.10', '3.12.16'):
        matches = [path.parent for path in candidates.glob('*/runtime.json')
                   if json.loads(path.read_text())['python'] == version]
        if len(matches) > 1:
            raise ValueError('ambiguous runtime candidate')
        if matches:
            return matches[0]
    raise ValueError('no compatible runtime')


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
    # Only successful compatibility jobs upload candidates. Prefer the GIL 3.14 build.
    candidates = Path(os.environ['RUNTIMES_DIR'])
    selected = select_runtime(candidates)
    metadata = package(source, temporary / "external-dist", os.environ["CORE_SHA"], checksum, selected)
    print('Selected Python ' + metadata['runtime']['python'])


if __name__ == "__main__":
    main()
