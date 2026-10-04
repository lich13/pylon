"""Validate APT's final dpkg action plan under its lock before installation."""
import json
import os
import re
import sys

from . import software


def validate_actions(text, items):
    expected = {item['package']: (item['before'], item['candidate']) for item in items}
    lines = text.splitlines()
    if not lines or lines[0] != 'VERSION 2':
        raise ValueError('APT 动作协议不支持')
    try:
        boundary = lines.index('', 1)
    except ValueError:
        raise ValueError('APT 动作协议不完整') from None
    installs = set()
    for line in lines[boundary + 1:]:
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            raise ValueError('APT 动作格式无效')
        name, before, direction, after, action = parts
        if (name not in expected or (before, after) != expected[name] or direction != '<'
                or before == '-' or action == '**REMOVE**'):
            raise ValueError('APT 实际事务超出已确认的包或版本')
        if action != '**CONFIGURE**':
            if not action.endswith('.deb'):
                raise ValueError('APT 安装动作无效')
            installs.add(name)
    if installs != set(expected):
        raise ValueError('APT 实际事务缺少已确认的包')


def main(run_id, text):
    if os.geteuid() != 0 or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('invalid guarded APT request')
    with software.database() as db:
        row = db.execute("SELECT install_items FROM runs WHERE id=? AND status='running'", (run_id,)).fetchone()
    if not row:
        raise ValueError('APT 批次不存在或已结束')
    items = json.loads(row['install_items'])
    validate_actions(text, items)
    for item in items:
        if software._apt_phase_candidate(item['package']) != item['candidate']:
            raise ValueError('系统分阶段策略已变化，取消本次安装')


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('invalid guard arguments')
        main(sys.argv[1], sys.stdin.read(655360))
    except Exception as exc:
        print('pylon APT guard: ' + software.redact(str(exc)), file=sys.stderr)
        raise SystemExit(1)
