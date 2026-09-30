"""Fixed privileged API and an on-demand, single-turn systemd worker."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time

from . import agent_state as state
from . import agent_runtime as runtime
from . import software


def active():
    return software.capture(['/usr/bin/systemctl', 'show', runtime.UNIT, '-p', 'ActiveState', '--value']) in {
        'active', 'activating', 'deactivating', 'reloading'}


def kick():
    if state.need_worker() and not active():
        software.capture(['/usr/bin/systemctl', 'start', '--no-block', runtime.UNIT])


def housekeeping():
    """Only runs in the new worker, after systemd stopped the old cgroup."""
    now, limits = time.time(), state.limits()
    with state.database() as db:
        for row in db.execute('SELECT * FROM tasks').fetchall():
            if row['status'] == 'running':
                state.finish(db, row['id'], 'interrupted', {'summary': '执行服务中断，结果待核对；不会自动重做业务操作。'},
                             elapsed=max(0, now - (row['started_at'] or now)))
            elif row['status'] == 'waiting' and now - row['waiting_since'] >= limits['waiting_seconds']:
                state.finish(db, row['id'], 'timed_out', {'summary': '等待回答已到期，任务结束并清理线程。'})
        cleanup = [dict(r) for r in db.execute('SELECT * FROM tasks WHERE cleanup!=? AND cleanup_after<=?', ('done', now))
                   if r['status'] in state.TERMINAL and not db.execute('SELECT 1 FROM events WHERE task_id=? AND acknowledged=0', (r['id'],)).fetchone()]
    for row in cleanup:
        try:
            runtime.delete_session(row)
        except Exception as exc:
            error = state.clean_text(exc, 1200)
            with state.database() as db:
                db.execute("UPDATE tasks SET cleanup='failed',cleanup_error=?,cleanup_attempts=cleanup_attempts+1,cleanup_after=? WHERE id=?",
                           (error, time.time() + min(300, 5 * 2 ** min(row['cleanup_attempts'], 6)), row['id']))
                if row['cleanup'] != 'failed' or error != row['cleanup_error']:
                    state.notice(db, row['id'])
        else:
            with state.database() as db:
                db.execute('DELETE FROM inputs WHERE task_id=?', (row['id'],))
                db.execute('DELETE FROM events WHERE task_id=? AND acknowledged=1', (row['id'],))
                db.execute("UPDATE tasks SET cleanup='done',cleanup_error='',progress='',updated_at=? WHERE id=?", (time.time(), row['id']))
                state.notice(db, row['id'])
    with state.database() as db:
        expired = [r['id'] for r in db.execute("SELECT id FROM tasks WHERE cleanup='done' AND updated_at<?", (now - limits['retention_days'] * 86400,))]
        for task_id in expired:
            if db.execute('SELECT 1 FROM events WHERE task_id=? AND acknowledged=0', (task_id,)).fetchone():
                continue
            for table in ('events', 'inputs', 'requests'):
                db.execute(f'DELETE FROM {table} WHERE task_id=?', (task_id,))
            db.execute('DELETE FROM tasks WHERE id=?', (task_id,))


def execute_next():
    state.private_dir(state.ROOT)
    with (state.ROOT / 'worker.lock').open('a') as worker_lock:
        try:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # The installer uses this same lock. Do not start a native agent while
        # either CLI is being replaced, including during session deletion.
        with software.database():
            pass
        with (software.ROOT / 'install.lock').open('a') as install_lock:
            try:
                fcntl.flock(install_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            housekeeping()
            with state.database() as db:
                selected = db.execute("SELECT * FROM tasks WHERE status='queued' AND cancelled=0 ORDER BY updated_at LIMIT 1").fetchone()
                if selected is None:
                    return
                row = dict(selected)
                pending = db.execute('SELECT * FROM inputs WHERE task_id=? AND consumed=0 ORDER BY seq', (row['id'],)).fetchall()
                if not pending:
                    state.finish(db, row['id'], 'uncertain', {'summary': '任务没有待处理输入，未执行。'})
                    return
                db.executemany('UPDATE inputs SET consumed=1 WHERE seq=?', [(r['seq'],) for r in pending])
                db.execute("UPDATE tasks SET status='running',started_at=?,turns=turns+1,waiting_since=NULL WHERE id=?", (time.time(), row['id']))
                row['turns'] += 1
            started = time.monotonic()
            try:
                remaining = state.limits()['execution_seconds'] - row['runtime']
                if remaining <= 0:
                    raise TimeoutError('已达到累计执行时间上限')
                result = runtime.run_turn(row, [r['text'] for r in pending], remaining)
                status = {'completed': 'succeeded', 'needs_input': 'waiting', 'failed': 'failed'}[result['status']]
                if status == 'succeeded':
                    try:
                        result['independent_checks'] = runtime.verify(result.pop('checks'))
                    except Exception as exc:
                        result['independent_checks'] = []
                        result['limitations'].append(state.clean_text(exc, 2000))
                    if not result['independent_checks']:
                        status = 'uncertain'
                        result['limitations'].append('Agent 已结束；本任务没有可独立复核的验收项，结论待核验。')
                else:
                    result.pop('checks', None)
            except Exception as exc:
                status = 'cancelled' if isinstance(exc, InterruptedError) else 'timed_out' if isinstance(exc, TimeoutError) else 'uncertain'
                result = {'summary': state.clean_text(exc, 2500), 'limitations': ['不会自动重复执行；取消或中断不会撤销已经完成的修改。']}
            with state.database() as db:
                current = state.task(db, row['id'])
                if current['cancelled']:
                    status = 'cancelled'
                    result['limitations'] = result.get('limitations', []) + ['已收到取消请求，任务结束。']
                elif status in {'succeeded', 'waiting', 'uncertain'} and db.execute('SELECT 1 FROM inputs WHERE task_id=? AND consumed=0', (row['id'],)).fetchone():
                    # Owner messages arriving during the turn are never lost
                    # to the completion/cleanup transition.
                    status = 'queued'
                    result['summary'] += '\n已收到补充要求，将继续同一会话。'
                state.finish(db, row['id'], status, result, elapsed=time.monotonic() - started)
    # Leave the service here: KillMode=control-group reaps the private Grok
    # leader and tool children before another turn or cleanup can begin.


def main(args):
    if not args:
        raise ValueError('missing agent action')
    action = args[0]
    if action in {'run', 'reply', 'cancel'} and len(args) == 4:
        target, profile, key = args[1:]
        if action == 'run':
            output = state.submit(target, profile, key, sys.stdin.read(16001))
        elif action == 'reply':
            output = state.reply(target, profile, key, sys.stdin.read(16001))
        else:
            output = state.cancel(target, profile, key)
        # State is committed before systemctl. A failed launch remains queued
        # and is retried by the existing bot reconciler without duplicating it.
        try:
            kick()
        except Exception as exc:
            output['launch_error'] = state.clean_text(exc, 500)
    elif action in {'status', 'logs'} and len(args) in {1, 2}:
        output = state.rows(args[1] if len(args) == 2 else '')
    elif args == ['results']:
        kick()
        output = state.results()
    elif action == 'ack' and len(args) == 2:
        state.acknowledge(args[1])
        kick()
        output = {'ok': True}
    elif args == ['cleaned']:
        with state.database() as db:
            output = {'ids': [r[0] for r in db.execute("SELECT id FROM tasks WHERE cleanup='done'")],
                      'retention_days': state.limits()['retention_days']}
    else:
        raise ValueError('unsupported agent action')
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if os.geteuid() != 0 or sys.argv[1:] != ['worker']:
        raise SystemExit('root worker accepts no caller-supplied execution parameters')
    execute_next()
