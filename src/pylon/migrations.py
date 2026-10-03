"""Explicit, idempotent upgrades run while task and bot services are stopped."""
from __future__ import annotations

import json
from pathlib import Path

from .config import home, profile_root
from .outbox import Outbox
from .shared.state_json import save_atomic

OLD_XIANBAO = 'xianbao-hermes-poll'
NEW_XIANBAO = 'xianbao-poll'


def rename_xianbao(box: Outbox) -> None:
    # Keep event IDs, receipts, payloads and retry state unchanged. They are the
    # durable delivery identity, including notifications pending at cutover.
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for table in ('runs', 'events'):
            db.execute(f'UPDATE {table} SET job_id=? WHERE job_id=?', (NEW_XIANBAO, OLD_XIANBAO))
        prefix = OLD_XIANBAO + ':'
        for row in db.execute('SELECT id FROM runs WHERE substr(id,1,?)=?', (len(prefix), prefix)).fetchall():
            new_id = NEW_XIANBAO + row['id'][len(OLD_XIANBAO):]
            db.execute('UPDATE run_events SET run_id=? WHERE run_id=?', (new_id, row['id']))
            db.execute('UPDATE runs SET id=? WHERE id=?', (new_id, row['id']))
        old_key, new_key = 'failed:' + OLD_XIANBAO, 'failed:' + NEW_XIANBAO
        db.execute('UPDATE kv SET key=? WHERE key=?', (new_key, old_key))


def migrate_profile(root: Path) -> None:
    old = root / 'config/xianbao-hermes-rules.json'
    new = root / 'config/xianbao-rules.json'
    if old.exists():
        if new.exists() and new.read_bytes() != old.read_bytes():
            raise ValueError('both Xianbao rule files exist with different contents')
        old.replace(new)
    env_file = root / 'config/xianbao-filter.env'
    if env_file.exists():
        lines = []
        for line in env_file.read_text().splitlines():
            key = line.strip().removeprefix('export ').partition('=')[0].strip()
            if key in {'XIANBAO_ALERT_SCRIPT', 'HERMES_HOME'}:
                continue
            line = line.replace('xianbao-hermes-rules.json', 'xianbao-rules.json')
            line = line.replace(OLD_XIANBAO, NEW_XIANBAO)
            line = line.replace('xianbao-hermes.service', 'pylon-task@xianbao-poll.service')
            lines.append(line)
        env_file.write_text('\n'.join(lines) + '\n')
    alert = root / 'data/xianbao_filter/alert_state.json'
    if alert.exists():
        values = json.loads(alert.read_text())
        migrated = {}
        for key, value in values.items():
            key = key.replace(OLD_XIANBAO, NEW_XIANBAO).replace('xianbao-hermes.service', 'pylon-task@xianbao-poll.service')
            migrated[key] = max(migrated.get(key, 0), value)
        save_atomic(alert, migrated)


def migrate_gmail() -> None:
    from .tasks.gmail import review, cleanup, store
    store.migrate([(review.REVIEWS_PATH, {"version": 1, "reviews": {}}),
                   (review.PROPOSALS_PATH, {"version": 1, "proposals": {}}),
                   (cleanup.RULE_OVERRIDES_PATH, {"version": 1, "operations": []}),
                   (cleanup.EXTRA_RULES_PATH, {"version": 1, "rules": []})])


def main() -> None:
    migrate_profile(profile_root('default'))
    rename_xianbao(Outbox())
    migrate_gmail()
    from .tasks.quotes.health import suppress_legacy_notices
    suppress_legacy_notices(Outbox())
    old_lock = home() / 'run' / (OLD_XIANBAO + '.lock')
    # No process may hold the old lock during the deployment migration.
    old_lock.unlink(missing_ok=True)
    print('pylon layout migration complete')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--gmail-only', action='store_true')
    args = parser.parse_args()
    migrate_gmail() if args.gmail_only else main()
