"""Fixed privileged operations. No user supplied commands, paths or URLs."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import time
import uuid

from ..config import redact
from ..software_inventory import SOFTWARE, latest, newer, version
from ..software_policy import apt_policy, is_allowed_apt_package

ROOT = Path('/var/lib/pylon-updates')
BINARIES = {'codex': '/usr/local/bin/codex', 'grok': '/usr/local/bin/grok'}
TERMINAL = {'updated', 'current', 'busy', 'failed', 'uncertain', 'deferred', 'stale'}


def capture(argv, timeout=15, *, include_stderr=False):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True,
                          env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    return (proc.stdout + ('\n' + proc.stderr if include_stderr else '')).strip()


@contextlib.contextmanager
def database():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if ROOT.is_symlink() or ROOT.stat().st_uid != os.geteuid() or ROOT.stat().st_mode & 0o077:
        raise PermissionError('unsafe software update directory')
    path = ROOT / 'updates.sqlite3'
    if path.is_symlink():
        raise PermissionError('unsafe software update database')
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,target TEXT,status TEXT,before_version TEXT,after_version TEXT,
                  latest_version TEXT,detail TEXT,created_at REAL,finished_at REAL,
                  expected_before TEXT DEFAULT '', expected_latest TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY,run_id TEXT,profile TEXT,acknowledged INTEGER DEFAULT 0);
            """)
            for column in ('expected_before', 'expected_latest', 'items', 'item_results', 'install_items'):
                try:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT DEFAULT ''")
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc).lower():
                        raise
            with db:
                yield db
        finally:
            db.close()



def active(target):
    if str(target).startswith('apt-batch:'):
        unit = 'pylon-apt-batch@' + target.removeprefix('apt-batch:') + '.service'
        return capture(['/usr/bin/systemctl', 'show', unit, '-p', 'ActiveState', '--value']) in {
            'active', 'activating', 'deactivating', 'reloading'}
    package = target.removeprefix('apt:') if str(target).startswith('apt:') else ''
    unit_prefix = 'pylon-apt-update@' if package else 'pylon-update@'
    instance = package or target
    value = capture(['/usr/bin/systemctl', 'show', unit_prefix + instance + '.service', '-p', 'ActiveState', '--value'])
    return value in {'active', 'activating', 'deactivating', 'reloading'}


def busy_processes(target, proc_root=Path('/proc')):
    found = []
    for proc in proc_root.iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            executable = str((proc / 'exe').readlink()).removesuffix(' (deleted)')
        except OSError:
            continue
        match = (target == 'codex' and Path(executable).name == 'codex' and '/.codex/' in executable or
                 target == 'grok' and Path(executable).name.startswith('grok') and '/.grok/' in executable)
        if match:
            found.append(int(proc.name))
    return sorted(found)


def start(target, profile, request_id):
    if target not in BINARIES or profile not in {'default', 'webops'} or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', request_id):
        raise ValueError('invalid software update request')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT r.* FROM runs r JOIN requests q ON q.run_id=r.id WHERE q.id=?', (request_id,)).fetchone()
        if previous:
            return dict(previous)
        running = db.execute("SELECT * FROM runs WHERE target=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
        if running:
            db.execute('INSERT INTO requests(id,run_id,profile) VALUES(?,?,?)', (request_id, running['id'], profile))
            return dict(running)
        run_id = uuid.uuid4().hex
        db.execute("INSERT INTO runs(id,target,status,created_at) VALUES(?,?,'queued',?)", (run_id, target, time.time()))
        db.execute('INSERT INTO requests(id,run_id,profile) VALUES(?,?,?)', (request_id, run_id, profile))
    try:
        capture(['/usr/bin/systemctl', 'start', '--no-block', 'pylon-update@' + target + '.service'])
    except Exception as exc:
        finish(run_id, 'failed', detail=redact(str(exc)))
    with database() as db:
        return dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())


def start_apt(package, profile, request_id, expected_before, expected_latest):
    if (not is_allowed_apt_package(package) or profile != 'default'
            or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', request_id)
            or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', expected_before)
            or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', expected_latest)):
        raise ValueError('invalid APT update request')
    target = 'apt:' + package
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT r.* FROM runs r JOIN requests q ON q.run_id=r.id WHERE q.id=?', (request_id,)).fetchone()
        if previous:
            return dict(previous)
        running = db.execute("SELECT * FROM runs WHERE target=? AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
        if running:
            db.execute('INSERT INTO requests(id,run_id,profile) VALUES(?,?,?)', (request_id, running['id'], profile))
            return dict(running)
        run_id = uuid.uuid4().hex
        db.execute("""INSERT INTO runs
            (id,target,status,created_at,expected_before,expected_latest)
            VALUES(?,?, 'queued',?,?,?)""",
                   (run_id, target, time.time(), expected_before, expected_latest))
        db.execute('INSERT INTO requests(id,run_id,profile) VALUES(?,?,?)', (request_id, run_id, profile))
    try:
        capture(['/usr/bin/systemctl', 'start', '--no-block', 'pylon-apt-update@' + package + '.service'])
    except Exception as exc:
        finish(run_id, 'failed', detail=redact(str(exc)))
    with database() as db:
        return dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())


def finish(run_id, status, *, before='', after='', target_version='', detail=''):
    with database() as db:
        db.execute('UPDATE runs SET status=?,before_version=?,after_version=?,latest_version=?,detail=?,finished_at=? WHERE id=?',
                   (status, before, after, target_version, redact(detail)[-2000:], time.time(), run_id))


def native_update(target):
    argv = ['/usr/bin/sudo', '-n', '-H', '--', BINARIES[target], 'update']
    if target == 'grok':
        argv.append('--stable')
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise TimeoutError('原生更新超过 15 分钟，结果需要核对')
    return process.returncode, output


def installed(target):
    return probe_version(target, capture(['/usr/bin/sudo', '-n', '-H', '--', BINARIES[target], '--version']))


def repair_codex_links():
    # The official installer maintains ~/.local/bin; retain the server's two
    # historical entrypoints even when an archive stops shipping current/codex.
    entry = Path('/root/.local/bin/codex')
    resolved = entry.resolve(strict=True)
    if not resolved.is_relative_to('/root/.codex/packages/standalone/releases') or not resolved.is_file():
        raise ValueError('Codex 安装路径不符合当前 standalone 布局')
    capture([str(entry), '--version'])
    os.chown(resolved, 0, 0)
    for target in [Path('/usr/local/bin/codex'), Path('/usr/local/bin/codex-raw')]:
        temporary = target.with_name('.pylon-' + target.name)
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(entry)
        temporary.replace(target)


def _apt_version(package: str) -> str:
    if not is_allowed_apt_package(package):
        raise ValueError('APT 包不在固定白名单中')
    value = capture(['/usr/bin/dpkg-query', '-W', '-f=${Version}', package], timeout=15)
    if not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', value):
        raise ValueError('无法读取已安装 APT 版本')
    return value


def _apt_candidate(package: str) -> str:
    output = capture(['/usr/bin/apt-cache', 'policy', package], timeout=20)
    match = re.search(r'^\s+Candidate:\s*(\S+)', output, re.MULTILINE)
    if not match or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', match.group(1)):
        raise ValueError('无法读取 APT 候选版本')
    return match.group(1)


def _apt_newer(candidate: str, installed: str) -> bool:
    """Compare Debian versions with dpkg semantics, including epochs and tildes."""
    pattern = r'[A-Za-z0-9.+:~_-]{1,160}'
    if not re.fullmatch(pattern, candidate) or not re.fullmatch(pattern, installed):
        raise ValueError('APT 版本格式异常')
    result = subprocess.run(['/usr/bin/dpkg', '--compare-versions', candidate, 'gt', installed],
                            capture_output=True, text=True, timeout=15)
    if result.returncode not in {0, 1}:
        raise RuntimeError('APT 版本比较失败')
    return result.returncode == 0


def _apt_environment():
    return {**os.environ, 'LC_ALL': 'C', 'LANG': 'C', 'DEBIAN_FRONTEND': 'noninteractive'}


def _apt_phase_candidate(package):
    output = capture(['/usr/bin/apt-cache', '-o', 'APT::Get::Phase-Policy=true',
                      'policy', package], timeout=20)
    match = re.search(r'^\s+Candidate:\s*(\S+)', output, re.MULTILINE)
    if not match:
        raise ValueError('无法确认系统分阶段发布策略')
    return match.group(1)


def _apt_simulation(packages, *, upgrade=False):
    if upgrade:
        raise ValueError('不允许全系统模拟升级')
    packages = [packages] if isinstance(packages, str) else list(packages)
    if not packages or any(not is_allowed_apt_package(p) for p in packages):
        raise ValueError('APT 包不在固定白名单中')
    argv = ['/usr/bin/apt-get', '-s', '-o', 'APT::Get::Assume-Yes=true',
            '-o', 'APT::Get::Show-Upgraded=true', '-o', 'APT::Get::Phase-Policy=true',
            'install', '--only-upgrade', '--no-install-recommends', *packages]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=_apt_environment())
    output = (result.stdout + '\n' + result.stderr).strip()
    if result.returncode:
        raise RuntimeError('APT 模拟升级失败：' + redact(output)[-1200:])
    return output


def _apt_simulation_safe(output, packages, *, strict=True):
    expected = {packages} if isinstance(packages, str) else set(packages)
    lowered = output.lower()
    if 'kept back' in lowered or 'deferred due to phasing' in lowered:
        return False, '所选包被系统暂缓（held-back / phased updates），本次未安装'
    summary = re.search(r'(\d+) upgraded,\s*(\d+) newly installed,\s*(\d+) to remove', output)
    if (re.search(r'^Remv\s', output, re.MULTILINE)
            or 'will be removed:' in lowered or 'will be newly installed:' in lowered
            or (summary and (int(summary[2]) or int(summary[3])))):
        return False, '模拟升级包含新增或删除包'
    # APT may merely suggest 'apt autoremove' for unrelated orphan packages.
    # Actual removals are rejected above; the install argv never uses autoremove.
    actions = re.findall(r'^Inst\s+(\S+)\s+\[([^]\n]+)\]\s+\(([^ )]+)', output, re.MULTILINE)
    all_inst = re.findall(r'^Inst\s+(\S+)', output, re.MULTILINE)
    if len(actions) != len(all_inst):
        return False, '模拟升级包含新增包或无法识别的动作'
    if any(':' in row[0] for row in actions):
        return False, '模拟升级包含不同架构的包，本次未安装'
    changed = {row[0] for row in actions}
    extra = changed - expected
    if extra:
        return False, '需要同时选择以下关联包：' + '、'.join(sorted(extra)) + '；请在 /updates 勾选后一起提交。本次未安装。'
    if not summary or not actions or int(summary[1]) != len(actions) or len(changed) != len(actions) or changed != expected:
        return False, '模拟升级未完整确认所选包，本次未安装'
    configured = {v.split(':', 1)[0] for v in re.findall(r'^Conf\s+(\S+)', output, re.MULTILINE)}
    if configured - expected:
        return False, '模拟升级将配置未选择的包，本次未安装'
    return True, ''


def _validate_apt_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError('一次需选择 1–20 个系统包')
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {'package', 'before', 'candidate'}:
            raise ValueError('系统包批次参数无效')
        package = item['package']
        if not isinstance(package, str) or not is_allowed_apt_package(package) or package in seen:
            raise ValueError('系统包不在白名单内或重复')
        if any(not isinstance(item[k], str) or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', item[k])
               for k in ('before', 'candidate')):
            raise ValueError('系统包版本格式无效')
        seen.add(package)
        result.append(dict(item))
    return sorted(result, key=lambda row: row['package'])


def start_apt_batch(items, profile, request_id):
    items = _validate_apt_items(items)
    if profile != 'default' or not re.fullmatch(r'software-batch:[0-9a-f]{24}', request_id):
        raise ValueError('invalid APT batch request')
    encoded = json.dumps(items, sort_keys=True)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT r.* FROM runs r JOIN requests q ON q.run_id=r.id WHERE q.id=?',
                              (request_id,)).fetchone()
        if previous:
            if previous['items'] != encoded:
                raise ValueError('同一批次不能修改所选包')
            return dict(previous)
        run_id = uuid.uuid4().hex
        db.execute("INSERT INTO runs(id,target,status,created_at,items) VALUES(?,?,'queued',?,?)",
                   (run_id, 'apt-batch:' + run_id, time.time(), encoded))
        db.execute('INSERT INTO requests(id,run_id,profile) VALUES(?,?,?)', (request_id, run_id, profile))
    try:
        capture(['/usr/bin/systemctl', 'start', '--no-block', 'pylon-apt-batch@' + run_id + '.service'])
    except Exception as exc:
        finish(run_id, 'failed', detail=redact(str(exc)))
    with database() as db:
        return dict(db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())


def _finish_apt(run_id, status, items, detail):
    with database() as db:
        db.execute("UPDATE runs SET status=?,item_results=?,detail=?,finished_at=?,"
                   "before_version=?,after_version=?,latest_version=? WHERE id=?",
                   (status, json.dumps(items, ensure_ascii=False), redact(detail)[-2000:], time.time(),
                    items[0].get('before', '') if len(items) == 1 else '',
                    items[0].get('after', '') if len(items) == 1 else '',
                    items[0].get('candidate', '') if len(items) == 1 else '', run_id))


def _execute_apt(row, items):
    run_id = row['id']
    results = [item | {'after': '', 'status': 'pending'} for item in items]
    started_install = False

    def done(status, detail):
        for item in results:
            if item['status'] == 'pending' or (status in {'failed', 'uncertain'} and item['status'] == 'updated'):
                item['status'] = status
        _finish_apt(run_id, status, results, detail)

    try:
        with (ROOT / 'install.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                done('busy', '另一项软件更新正在执行，本次未更新')
                return
            stale, deferred = [], []
            for item in results:
                package = item['package']
                actual = _apt_version(package)
                candidate = _apt_candidate(package)
                item['after'] = actual
                if actual == item['candidate'] and candidate == item['candidate']:
                    item['status'] = 'current'
                    continue
                if actual != item['before'] or candidate != item['candidate']:
                    stale.append(package)
                elif not _apt_newer(candidate, actual):
                    item['status'] = 'current'
            if stale:
                done('stale', '以下包版本已变化，请重新 /updates 后选择：' + '、'.join(stale))
                return
            work = [item for item in results if item['status'] != 'current']
            if not work:
                done('current', '所选包已是当前候选版本，本次无需安装')
                return
            for item in work:
                if _apt_phase_candidate(item['package']) != item['candidate']:
                    deferred.append(item['package'])
            if deferred:
                done('deferred', '系统分阶段发布暂未放行：' + '、'.join(deferred) + '；本批未安装，系统放行后可重新选择。')
                return
            held = set(capture(['/usr/bin/apt-mark', 'showhold']).split())
            if held.intersection(item['package'] for item in work):
                done('deferred', '所选包被系统 hold，本次未安装；不会绕过系统策略')
                return
            if capture(['/usr/bin/dpkg', '--audit'], timeout=30):
                done('failed', 'dpkg 存在未完成配置，本次未安装，请先核对系统包状态')
                return
            packages = [item['package'] for item in work]
            simulation = _apt_simulation(packages[0] if len(packages) == 1 else packages)
            safe, reason = _apt_simulation_safe(simulation, packages)
            if not safe:
                done('deferred' if 'phased' in reason else 'failed', reason)
                return
            versions = {name.split(':', 1)[0]: (before, candidate) for name, before, candidate in
                        re.findall(r'^Inst\s+(\S+)\s+\[([^]\n]+)\]\s+\(([^ )]+)', simulation, re.MULTILINE)}
            if any(versions.get(item['package']) != (item['before'], item['candidate']) for item in work):
                done('stale', '模拟升级版本与确认版本不符，本次未安装')
                return
            # APT invokes this fixed root hook under its dpkg lock, before any
            # unpack/configure action. It closes the simulation/install race.
            with database() as db:
                db.execute('UPDATE runs SET install_items=? WHERE id=?', (json.dumps(work, sort_keys=True), run_id))
            guard = '/srv/pylon/venv/bin/python -I -m pylon.admin.apt_guard ' + run_id
            command = ['/usr/bin/apt-get', '-y', '--no-remove',
                       '-o', 'Dpkg::Options::=--force-confold',
                       '-o', 'APT::Get::Phase-Policy=true',
                       '-o', 'DPkg::Pre-Install-Pkgs::=' + guard,
                       '-o', 'DPkg::Tools::Options::/srv/pylon/venv/bin/python::Version=2',
                       '--only-upgrade', '--no-install-recommends', 'install',
                       *(item['package'] + '=' + item['candidate'] for item in work)]
            started_install = True
            proc = subprocess.run(command, capture_output=True, text=True, timeout=900, env=_apt_environment())
            for item in work:
                try:
                    item['after'] = _apt_version(item['package'])
                    item['status'] = 'updated' if item['after'] == item['candidate'] else 'failed'
                except Exception:
                    item['status'] = 'uncertain'
            if proc.returncode:
                done('failed', f'APT 安装退出码 {proc.returncode}；已核对各包实装版本。\n' +
                     redact((proc.stdout + '\n' + proc.stderr))[-1000:])
                return
            if any(item['status'] not in {'updated', 'current'} for item in results):
                done('uncertain' if any(item['status'] == 'uncertain' for item in results) else 'failed',
                     '命令已结束，但部分包实装版本未达到候选版本，请核对')
                return
            if capture(['/usr/bin/dpkg', '--audit'], timeout=30):
                done('failed', '实装版本已更新，但 dpkg 状态检查未通过')
                return
            if any(item['package'] == 'thermald' for item in work):
                state = capture(['/usr/bin/systemctl', 'is-active', 'thermald'], timeout=15)
                if state != 'active':
                    done('failed', '实装版本已更新，但 thermald 服务健康检查未通过')
                    return
            note = '所选包实装版本、dpkg 状态及相关服务检查通过'
            if Path('/var/run/reboot-required').exists():
                note += '；系统标记需要重启，pylon 未自动重启主机'
            done('updated', note)
    except Exception as exc:
        for item in results:
            if started_install:
                try:
                    item['after'] = _apt_version(item['package'])
                except Exception:
                    item['after'] = ''
                item['status'] = 'uncertain'
        done('uncertain' if started_install else 'failed',
             '执行结果需核对；不会自动重复安装。' + redact(str(exc)))


def apt_update(package):
    if not is_allowed_apt_package(package):
        raise ValueError('APT 包不在固定白名单中')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM runs WHERE target=? AND status='queued' ORDER BY created_at LIMIT 1",
                         ('apt:' + package,)).fetchone()
        if not row:
            return
        row = dict(row)
        db.execute("UPDATE runs SET status='running' WHERE id=?", (row['id'],))
    _execute_apt(row, [{'package': package, 'before': row['expected_before'], 'candidate': row['expected_latest']}])


def apt_batch(run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('invalid APT batch id')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM runs WHERE id=? AND target=? AND status='queued'",
                         (run_id, 'apt-batch:' + run_id)).fetchone()
        if not row:
            return
        row = dict(row)
        items = _validate_apt_items(json.loads(row['items']))
        db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    _execute_apt(row, items)


def execute(target):
    if target not in BINARIES:
        raise ValueError('invalid software target')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM runs WHERE target=? AND status='queued' ORDER BY created_at LIMIT 1", (target,)).fetchone()
        if not row:
            return
        run_id = row['id']
        db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    before = after = target_version = ''
    try:
        with (ROOT / 'install.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                finish(run_id, 'busy', detail='另一项软件更新正在执行，本次未更新')
                return
            before = installed(target)
            busy = busy_processes(target)
            if busy:
                finish(run_id, 'busy', before=before, after=before, detail='会话进程占用，未执行更新。PID：' + ', '.join(map(str, busy)))
                return
            _, kind, source = SOFTWARE[target]
            target_version, _ = latest(kind, source)
            if newer(before, target_version):
                finish(run_id, 'current', before=before, after=before, target_version=target_version,
                       detail='已安装版本高于 stable 版本，本次保留现有版本')
                return
            # Check again after the network lookup, immediately before installation.
            if busy_processes(target):
                finish(run_id, 'busy', before=before, after=before, detail='检查期间出现会话进程，本次未更新')
                return
            code, output = native_update(target)
            if code == 0 and target == 'codex':
                repair_codex_links()
            after = installed(target)
            if code:
                finish(run_id, 'failed', before=before, after=after, target_version=target_version,
                       detail=f'原生更新退出码 {code}\n' + output[-1600:])
            elif newer(target_version, after):
                finish(run_id, 'failed', before=before, after=after, target_version=target_version, detail='更新命令结束，但实装版本未达到检查到的 stable 版本')
            else:
                finish(run_id, 'updated' if newer(after, before) else 'current', before=before, after=after,
                       target_version=target_version, detail='原生命令与更新后版本检查通过')
    except Exception as exc:
        try:
            after = installed(target)
        except Exception:
            pass
        finish(run_id, 'uncertain' if isinstance(exc, TimeoutError) else 'failed', before=before, after=after,
               target_version=target_version, detail=str(exc))


def results():
    with database() as db:
        outstanding = db.execute("SELECT * FROM runs WHERE status IN ('queued','running')").fetchall()
    for row in outstanding:
        if time.time() - row['created_at'] > 30 and not active(row['target']):
            finish(row['id'], 'uncertain', before=row['before_version'] or '', detail='更新服务已停止，结果待核对；不会自动重复安装')
    with database() as db:
        return [dict(row) for row in db.execute("SELECT r.*,q.id AS request_id,q.profile FROM runs r JOIN requests q ON q.run_id=r.id WHERE q.acknowledged=0 AND r.status NOT IN ('queued','running') ORDER BY r.created_at")]


APT_SCRIPT = '''import apt,json,os
cache=apt.Cache()
packages=[]
managed={}
for p in cache:
 if p.name=='docker.io' and p.is_installed and p.candidate:
  managed[p.name]=dict(installed=p.installed.version,candidate=p.candidate.version,upgradable=p.is_upgradable)
 if p.is_installed and p.is_upgradable:
  packages.append(dict(name=p.name,installed=p.installed.version,candidate=p.candidate.version,security=any('security' in o.archive for o in p.candidate.origins)))
stamp='/var/lib/apt/periodic/update-success-stamp'
print(json.dumps(dict(packages=packages,managed=managed,index_time=os.path.getmtime(stamp) if os.path.exists(stamp) else 0,reboot_required=os.path.exists('/var/run/reboot-required'))))'''
PYTHON_SCRIPT = '''import importlib.metadata as m,json
print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if str(d.locate_file('')).startswith('/usr/local/') and d.metadata.get('Name')}))'''


CONTAINER_BINARIES = {'sub2api': '/app/sub2api', 'easy_proxies': '/usr/local/bin/easy_proxies'}
CONTAINER_VERSION_PROBES = {
    'sub2api': ['/usr/bin/docker', 'exec', 'sub2api', CONTAINER_BINARIES['sub2api'], '--version'],
}


def container_snapshot(target):
    data = json.loads(capture(['/usr/bin/docker', 'inspect', target]))[0]
    state = data['State']
    if not state['Running'] or state['Pid'] <= 0:
        raise ValueError('容器未运行，无法确认实际版本')
    proc = Path('/proc') / str(state['Pid'])
    binary = proc / 'root' / CONTAINER_BINARIES[target].lstrip('/')
    executable = proc / 'exe'
    if not executable.samefile(binary):
        raise ValueError('运行程序与容器内文件不同，无法确认实际版本')
    stat = executable.stat()
    return (data['Image'], state['Pid'], state['StartedAt'], stat.st_dev, stat.st_ino), data


def container_version(target):
    before, data = container_snapshot(target)
    binary = CONTAINER_BINARIES[target]
    if target == 'sub2api':
        # The application's online updater replaces the binary in the writable
        # container layer; image labels remain at the original image version.
        raw = capture(CONTAINER_VERSION_PROBES[target], timeout=8, include_stderr=True)
        value = probe_version(target, raw)
        basis = '运行程序自报'
    else:
        # easy_proxies has no version flag or embedded release version. Trust
        # its image label only after ruling out mounts and writable-layer edits.
        path = Path(binary)
        if any(path.is_relative_to(mount['Destination']) for mount in data['Mounts']):
            raise ValueError('程序被外部挂载覆盖，不能按镜像标签判定版本')
        for line in capture(['/usr/bin/docker', 'diff', target]).splitlines():
            change, changed_path = line.split(' ', 1)
            if change in {'A', 'C', 'D'} and path.is_relative_to(changed_path):
                raise ValueError('容器程序已被修改，不能按镜像标签判定版本')
        # Read the immutable image, not container labels that can be overridden.
        image_id = before[0]
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('无法确认运行镜像 ID')
        raw = capture(['/usr/bin/docker', 'image', 'inspect', image_id, '--format',
                       '{{index .Config.Labels "org.opencontainers.image.version"}}'])
        value = probe_version(target, raw)
        basis = '镜像标签推定，程序未替换'
    after, _ = container_snapshot(target)
    if before != after:
        raise ValueError('检查期间容器发生变化，请重试')
    return {'version': value, 'basis': basis}


def inventory():
    data = {'installed': {}, 'python': {}}
    probes = {
        'codex': [BINARIES['codex'], '--version'], 'grok': [BINARIES['grok'], '--version'],
        'openlist': ['/var/lib/openlist/openlist', 'version'],
        'sing-box': ['/usr/local/bin/sing-box-anytls', 'version'],
        'nexushub': ['/usr/local/bin/nexushub-webd', '--version'],
        'certbot': ['/opt/certbot-ip/bin/certbot', '--version'],
        'npm': ['/usr/bin/npm', '--version'], 'pnpm': ['/usr/local/bin/pnpm', '--version'],
        'corepack': ['/usr/bin/corepack', '--version'], 'uv': ['/root/.local/bin/uv', '--version'],
        'rsync': ['/usr/local/bin/rsync', '--version'],
        'chrome': ['/root/.grok/linux-runtime/chrome-linux64/chrome', '--version'],
        'docker': ['/usr/bin/docker', 'version', '--format', '{{.Server.Version}}'],
        'postgres': ['/usr/bin/docker', 'exec', 'sub2api-postgres', 'postgres', '--version'],
        'redis': ['/usr/bin/docker', 'exec', 'sub2api-redis', 'redis-server', '--version'],
        'frp': ['/usr/bin/docker', 'exec', 'frps-chiphell', '/usr/bin/frps', '--version'],
    }
    for key, argv in probes.items():
        try:
            data['installed'][key] = {'version': probe_version(key, capture(argv, timeout=8))}
        except Exception as exc:
            data['installed'][key] = {'error': redact(str(exc))[:160]}
    for key in CONTAINER_BINARIES:
        try:
            data['installed'][key] = container_version(key)
        except Exception as exc:
            data['installed'][key] = {'error': redact(str(exc))[:160]}
    try:
        data['apt'] = json.loads(capture(['/usr/bin/python3', '-c', APT_SCRIPT], timeout=30))
    except Exception as exc:
        data['apt'] = {'error': redact(str(exc))[:160]}
    data['python']['pylon'] = {d.metadata['Name']: d.version for d in importlib.metadata.distributions() if d.metadata.get('Name')}
    try:
        data['python']['系统 Python'] = json.loads(capture(['/usr/bin/python3', '-c', PYTHON_SCRIPT]))
    except Exception as exc:
        data['installed']['system-python'] = {'error': redact(str(exc))[:160]}
    return data


def probe_version(target, output):
    number = r'(\d+(?:\.\d+){1,3})(?![\d.])'
    prefixes = {
        'codex': r'^codex-cli ', 'grok': r'^grok ',
        'openlist': r'^Version:\s*v?', 'sing-box': r'^sing-box version ',
        'nexushub': r'^nexushub-webd ', 'certbot': r'^certbot ',
        'uv': r'^uv ', 'rsync': r'^rsync\s+version ',
        'chrome': r'^Google Chrome for Testing ', 'postgres': r'^postgres \(PostgreSQL\) ',
        'redis': r'^Redis server v=', 'sub2api': r'\bSub2API v?',
    }
    if target in {'npm', 'pnpm', 'corepack', 'docker', 'frp', 'easy_proxies'}:
        match = re.fullmatch(r'v?' + number, output.strip())
    else:
        match = re.search(prefixes[target] + number, output, re.MULTILINE)
    if not match:
        name = SOFTWARE[target][0]
        raise ValueError(name + ' 未返回可识别的应用 Version 字段')
    return match.group(1)


def main(args):
    if args == ['inventory']:
        # sudo inherits pylon-bot's ProtectHome mount namespace. A fixed,
        # short-lived root service can read the installed tools under /root.
        output = json.loads(capture([
            '/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
            '--unit=pylon-inventory-' + uuid.uuid4().hex,
            '--property=User=root', '--property=Group=root', '--property=UMask=0077',
            '--property=RuntimeMaxSec=75', '--property=WorkingDirectory=/root',
            '/srv/pylon/venv/bin/python', '-I', '-m', 'pylon.admin.software', 'inventory-worker',
        ], timeout=85))
    elif args == ['results']:
        output = results()
    elif len(args) == 2 and args[0] == 'request' and re.fullmatch(r'software-batch:[0-9a-f]{24}', args[1]):
        with database() as db:
            row = db.execute('SELECT r.* FROM runs r JOIN requests q ON q.run_id=r.id WHERE q.id=?',
                             (args[1],)).fetchone()
            output = dict(row) if row else None
    elif args == ['status']:
        with database() as db:
            output = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY created_at DESC LIMIT 5')]
    elif len(args) == 4 and args[0] == 'update':
        output = start(*args[1:])
    elif len(args) == 6 and args[0] == 'apt-update':
        output = start_apt(*args[1:])
    elif len(args) == 4 and args[0] == 'apt-batch':
        if len(args[3]) > 16000:
            raise ValueError('APT batch payload too large')
        output = start_apt_batch(json.loads(args[3]), args[1], args[2])
    elif len(args) == 2 and args[0] == 'ack' and re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', args[1]):
        with database() as db:
            db.execute('UPDATE requests SET acknowledged=1 WHERE id=?', (args[1],))
        output = {'ok': True}
    else:
        raise ValueError('unsupported software action')
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    import sys
    if os.geteuid() != 0 or len(sys.argv) not in {2, 3}:
        raise SystemExit('root worker requires a fixed software target')
    if sys.argv[1] == 'inventory-worker' and len(sys.argv) == 2:
        print(json.dumps(inventory(), ensure_ascii=False))
    elif sys.argv[1] == 'apt-worker' and len(sys.argv) == 3:
        apt_update(sys.argv[2])
    elif sys.argv[1] == 'apt-batch-worker' and len(sys.argv) == 3:
        apt_batch(sys.argv[2])
    else:
        execute(sys.argv[1])
