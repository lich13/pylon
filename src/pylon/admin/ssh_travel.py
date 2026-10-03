"""Root-only, persistent IPv4 SSH travel rule with a recovery journal."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

from .ssh import atomic_write

STATE = Path('/etc/pylon/ssh-travel.json')
JOURNAL = Path('/etc/pylon/ssh-travel-transaction.json')
LOCK = Path('/run/pylon-ssh.lock')
COMMENT = 'pylon-ssh-travel'
RULE = ['allow', 'from', '0.0.0.0/0', 'to', 'any', 'port', '22', 'proto', 'tcp', 'comment', COMMENT]
RULE_HASH = hashlib.sha256(json.dumps(RULE, separators=(',', ':')).encode()).hexdigest()


def run(args):
    return subprocess.run(['/usr/sbin/ufw', *args], text=True, capture_output=True, check=True, timeout=60).stdout


def live():
    text = run(['status'])
    if not text.startswith('Status: active'):
        raise ValueError('UFW 未启用，无法切换旅行模式')
    own, foreign = [], []
    for line in text.splitlines():
        if COMMENT in line:
            if not re.fullmatch(r'22/tcp\s+ALLOW(?: IN)?\s+(?:Anywhere|0\.0\.0\.0/0)\s+# ' + COMMENT, line.strip()):
                raise ValueError('旅行模式受管规则与预期不一致，未修改防火墙')
            own.append(line)
        elif re.match(r'22(?:/tcp)?\s+ALLOW(?: IN)?\s+(?:Anywhere|0\.0\.0\.0/0)(?:\s|$)', line.strip()):
            foreign.append(line)
    if len(own) > 1:
        raise ValueError('发现重复旅行模式规则，未修改防火墙')
    return {'rule_present': bool(own), 'other_global_ssh_rule': bool(foreign)}


def read():
    if not STATE.exists():
        return {'enabled': False, 'changed_at': '', 'changed_by': '', 'rule_sha256': RULE_HASH}
    value = json.loads(STATE.read_text())
    if type(value.get('enabled')) is not bool or value.get('rule_sha256') != RULE_HASH:
        raise ValueError('旅行模式持久状态无效，未修改防火墙')
    return value


def write(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode())
    path.chmod(0o600)


def set_live(enabled):
    observed = live()
    if observed['other_global_ssh_rule']:
        # UFW can identify identical rules without their comments. Refuse to
        # mutate a rule that might belong to another administrator.
        raise ValueError('已有非 pylon 的公网 SSH 放行规则，请先核对；未修改防火墙')
    if observed['rule_present'] != enabled:
        run(RULE if enabled else ['--force', 'delete', *RULE])
    after = live()
    if after['rule_present'] != enabled or after['other_global_ssh_rule']:
        raise ValueError('旅行模式规则读回未通过')


def recover():
    if not JOURNAL.exists():
        return
    record = json.loads(JOURNAL.read_text())
    if type(record.get('was_open')) is not bool or type(record.get('previous', {}).get('enabled')) is not bool:
        raise ValueError('旅行模式恢复记录无效')
    set_live(record['was_open'])
    write(STATE, record['previous'])
    JOURNAL.unlink()


def change(enabled, *, restore=False):
    previous = read()
    before = live()
    if before['other_global_ssh_rule']:
        raise ValueError('已有其他公网 SSH 规则，未修改防火墙')
    if previous['enabled'] == enabled and before['rule_present'] == enabled:
        return {**previous, **before, 'consistent': True, 'changed': False}
    write(JOURNAL, {'previous': previous, 'was_open': before['rule_present']})
    try:
        set_live(enabled)
        current = previous if restore else {'enabled': enabled, 'changed_at': datetime.now(timezone.utc).isoformat(),
                                           'changed_by': os.environ.get('SUDO_USER') or 'root', 'rule_sha256': RULE_HASH}
        write(STATE, current)
        JOURNAL.unlink()
    except BaseException:
        # A failed rollback leaves the journal for boot/next-command recovery.
        recover()
        raise
    return {**current, **live(), 'consistent': True, 'changed': True}


def main(args=None):
    args = sys.argv[1:] if args is None else args
    if os.geteuid() != 0:
        raise PermissionError('SSH 旅行模式需要 root 特权入口')
    if len(args) != 1 or args[0] not in {'status', 'open', 'close', 'restore'}:
        raise ValueError('unsupported SSH travel action')
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = args[0]
        if action == 'status':
            value, actual = read(), live()
            result = {**value, **actual, 'transaction_pending': JOURNAL.exists(),
                      'consistent': value['enabled'] == actual['rule_present'] and not actual['other_global_ssh_rule'] and not JOURNAL.exists()}
        else:
            recover()
            enabled = read()['enabled'] if action == 'restore' else action == 'open'
            result = change(enabled, restore=action == 'restore')
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
