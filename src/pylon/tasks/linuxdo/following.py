"""A private, explicitly imported snapshot of the user's forum following list."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .common import digest, timestamp
from .helper_account import _check_private, _write_private, account_lock, private_root


@dataclass(frozen=True)
class Following:
    usernames: frozenset[str]
    imported_at: str

    @property
    def fingerprint(self) -> str:
        return digest(sorted(self.usernames))

    def payload(self) -> dict:
        return {'version': 1, 'complete': True, 'imported_at': self.imported_at,
                'usernames': sorted(self.usernames)}

    def summary(self) -> dict:
        return {'configured': True, 'count': len(self.usernames), 'imported_at': self.imported_at,
                'fingerprint': self.fingerprint}


def validate(value: dict) -> Following:
    if (not isinstance(value, dict) or set(value) != {'version', 'complete', 'imported_at', 'usernames'}
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('complete') is not True or not isinstance(value.get('imported_at'), str)
            or timestamp(value['imported_at']) is None):
        raise ValueError('invalid following snapshot')
    names = value['usernames']
    if (not isinstance(names, list) or len(names) > 10000
            or any(not isinstance(name, str) or not re.fullmatch(r'[\w.-]{1,100}', name) for name in names)):
        raise ValueError('invalid following usernames')
    return Following(frozenset(name.casefold() for name in names), value['imported_at'])


def load(path: Path | None = None) -> Following | None:
    path = path or private_root() / 'following.json'
    if not path.exists():
        return None
    _check_private(path.parent, directory=True)
    _check_private(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError('following snapshot too large')
    return validate(json.loads(path.read_text()))


def install(path: Path) -> dict:
    value = load(path)
    if value is None:
        raise ValueError('following snapshot missing')
    with account_lock() as root:
        _write_private(root / 'following.json', value.payload())
    return value.summary()


def available() -> tuple[Following | None, str]:
    try:
        return load(), ''
    except Exception as exc:
        # Invalid/missing metadata withholds only ambiguous notices, not personal alerts.
        return None, type(exc).__name__
