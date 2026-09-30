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

ROOT = Path('/var/lib/pylon-updates')
BINARIES = {'codex': '/usr/local/bin/codex', 'grok': '/usr/local/bin/grok'}
TERMINAL = {'updated', 'current', 'busy', 'failed', 'uncertain'}


def capture(argv, timeout=15, *, include_stderr=False):
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
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
                  latest_version TEXT,detail TEXT,created_at REAL,finished_at REAL);
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY,run_id TEXT,profile TEXT,acknowledged INTEGER DEFAULT 0);
            """)
            with db:
                yield db
        finally:
            db.close()



def active(target):
    value = capture(['/usr/bin/systemctl', 'show', 'pylon-update@' + target + '.service', '-p', 'ActiveState', '--value'])
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
    elif args == ['status']:
        with database() as db:
            output = [dict(row) for row in db.execute('SELECT * FROM runs ORDER BY created_at DESC LIMIT 5')]
    elif len(args) == 4 and args[0] == 'update':
        output = start(*args[1:])
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
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('root worker requires exactly one fixed software target')
    if sys.argv[1] == 'inventory-worker':
        print(json.dumps(inventory(), ensure_ascii=False))
    else:
        execute(sys.argv[1])
