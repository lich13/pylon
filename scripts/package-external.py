"""Package the verified private input only after the module matrix has passed."""
import hashlib
import os
from pathlib import Path
import sys
import tarfile


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
    package(source, temporary / "external-dist", os.environ["CORE_SHA"], checksum)


if __name__ == "__main__":
    main()
