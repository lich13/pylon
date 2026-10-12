from __future__ import annotations

import contextlib
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO


@dataclass
class LockResult:
    acquired: bool
    path: Path
    file: TextIO | None = None


@contextlib.contextmanager
def nonblocking_file_lock(path: Path) -> Iterator[LockResult]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield LockResult(False, path, fh)
            return
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield LockResult(True, path, fh)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
