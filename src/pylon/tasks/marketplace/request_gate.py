"""A host-wide request budget shared by archival and live collectors."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import time

from pylon.config import home


class SourceRateLimited(RuntimeError):
    """The source requested a cooldown; retain the current cursor."""


@contextlib.contextmanager
def _locked_state():
    path = home() / 'run/marketplace-nga-rate.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        state = json.loads(raw) if raw else {}
        if (not isinstance(state, dict) or any(type(value) not in (float, int)
                or not math.isfinite(value) for value in state.values())):
            raise ValueError('invalid NGA request budget')

        def save():
            handle.seek(0)
            json.dump(state, handle)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

        yield state, save


def cooldown(seconds: float = 60) -> None:
    with _locked_state() as (state, save):
        state['cooldown_until'] = max(state.get('cooldown_until', 0), time.time() + seconds)
        save()


@contextlib.contextmanager
def request_slot(interval: float):
    # Keep the inode and the exclusive lock through navigation. Atomic file
    # replacement would let two processes lock different inodes and overlap.
    with _locked_state() as (state, save):
        now = time.time()
        if state.get('cooldown_until', 0) > now:
            raise SourceRateLimited('nga_shared_cooldown')
        if 'last_started' in state:
            # A backward wall-clock adjustment must not cause an unbounded wait.
            remaining = min(interval, state['last_started'] + interval - now)
            if remaining > 0:
                time.sleep(remaining)
        state['last_started'] = time.time()
        save()
        try:
            yield
        except SourceRateLimited:
            state['cooldown_until'] = time.time() + 60
            save()
            raise
