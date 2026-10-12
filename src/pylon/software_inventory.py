"""Read-only software inventory and official stable release checks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo

import httpx

from .config import redact
from .outbox import Outbox

SOFTWARE = {
    'codex': ('Codex CLI', 'github', 'openai/codex'),
    'grok': ('Grok Build', 'text', 'https://x.ai/cli/stable'),
    'sub2api': ('sub2api', 'github', 'Wei-Shaw/sub2api'),
    'frp': ('FRP', 'github', 'fatedier/frp'),
    'openlist': ('OpenList', 'github', 'OpenListTeam/OpenList'),
    'sing-box': ('sing-box', 'github', 'SagerNet/sing-box'),
    'nexushub': ('NexusHub', 'github', 'lich13/nexushub'),
    'postgres': ('PostgreSQL 18', 'postgres', '18'),
    'redis': ('Redis 8.6', 'redis', '8.6'),
    'docker': ('Docker（Ubuntu 软件源）', 'apt', 'docker.io'),
    'certbot': ('Certbot', 'pypi', 'certbot'),
    'npm': ('npm', 'npm', 'npm'),
    'pnpm': ('pnpm', 'npm', 'pnpm'),
    'corepack': ('Corepack', 'npm', 'corepack'),
    'uv': ('uv', 'github', 'astral-sh/uv'),
    'rsync': ('rsync（手动安装）', 'github', 'RsyncProject/rsync'),
    'chrome': ('Grok Chrome for Testing', 'chrome', ''),
}

CACHE_SCHEMA = 3
GENERATION_KEY = 'software_inventory_generation'


def invalidate(box: Outbox):
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM kv WHERE key=?', (GENERATION_KEY,)).fetchone()
        generation = json.loads(row[0]) if row else 0
        db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (GENERATION_KEY, json.dumps(generation + 1)))
        db.execute('DELETE FROM kv WHERE key=?', ('software_inventory',))


def version(value: str) -> str:
    match = re.search(r'(?<![\d.])\d+(?:\.\d+){1,3}(?![\d.])', value)
    if not match:
        raise ValueError('无法识别版本号')
    return match.group()


def newer(candidate: str, installed: str) -> bool:
    return tuple(map(int, version(candidate).split('.'))) > tuple(map(int, version(installed).split('.')))


def latest(kind: str, source: str, http=None) -> tuple[str, str]:
    if http is None:
        with httpx.Client(timeout=12, follow_redirects=True, headers={'User-Agent': 'pylon-software-check'}) as client:
            return latest(kind, source, client)

    def get(url):
        result = http.get(url)
        result.raise_for_status()
        return result

    if kind == 'github':
        data = get('https://api.github.com/repos/' + source + '/releases/latest').json()
        if data.get('prerelease') or data.get('draft'):
            raise ValueError('官方返回了非稳定版本')
        return version(data['tag_name']), data['html_url']
    if kind == 'text':
        value = get(source).text.strip()
        if not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
            raise ValueError('stable 版本响应格式异常')
        return version(value), source
    if kind == 'pypi':
        info = get('https://pypi.org/pypi/' + source + '/json').json()['info']
        if not re.fullmatch(r'\d+(?:\.\d+){1,3}', info['version']):
            raise ValueError('PyPI 未返回稳定版本')
        return info['version'], info['package_url']
    if kind == 'npm':
        info = get('https://registry.npmjs.org/' + source + '/latest').json()
        return version(info['version']), 'https://www.npmjs.com/package/' + source
    if kind == 'postgres':
        from xml.etree import ElementTree as ET
        root = ET.fromstring(get('https://www.postgresql.org/versions.rss').content)
        for item in root.findall('./channel/item'):
            title = (item.findtext('title') or '').strip()
            if re.fullmatch(re.escape(source) + r'\.\d+', title):
                return title, (item.findtext('link') or '').strip()
        raise ValueError('未找到 PostgreSQL ' + source + ' 稳定版')
    if kind == 'redis':
        data = get('https://api.github.com/repos/redis/redis/releases?per_page=100').json()
        choices = [item for item in data if not item.get('prerelease') and not item.get('draft')
                   and re.fullmatch(r'v?' + re.escape(source) + r'\.\d+', item['tag_name'])]
        if not choices:
            raise ValueError('未找到 Redis ' + source + ' 补丁版本')
        item = max(choices, key=lambda row: tuple(map(int, version(row['tag_name']).split('.'))))
        return version(item['tag_name']), item['html_url']
    if kind == 'chrome':
        url = 'https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json'
        return get(url).json()['channels']['Stable']['version'], url
    raise ValueError('不支持的版本源')


def check(*, max_age: int = 600, _retries: int = 1) -> dict:
    from . import scheduler
    box = Outbox()
    generation = box.get(GENERATION_KEY, 0)
    cached = box.get('software_inventory')
    if (cached and cached.get('schema') == CACHE_SCHEMA and cached.get('generation') == generation
            and time.time() - cached['checked_at'] < max_age):
        return cached
    started = time.time()
    local = json.loads(scheduler.admin('software', 'inventory', timeout=90))
    checks = []
    for key, (name, kind, source) in SOFTWARE.items():
        current = local['installed'].get(key, {})
        checks.append({'id': key, 'name': name, 'kind': kind, 'source': source, **current})
    for environment, packages in local.get('python', {}).items():
        for package, installed in packages.items():
            if package.lower().replace('_', '-') in {'pylon', 'pylon-tasks', 'pylon-gateway'}:
                continue
            checks.append({'id': environment + ':' + package, 'name': environment + ' / ' + package,
                           'kind': 'pypi', 'source': package, 'version': installed})
    if local['installed'].get('system-python', {}).get('error'):
        checks.append({'id': 'system-python', 'name': '系统 Python 依赖', **local['installed']['system-python']})

    with httpx.Client(timeout=10, follow_redirects=True, headers={'User-Agent': 'pylon-software-check'}) as http:
        def one(row):
            if not row.get('version'):
                return row | {'error': row.get('error', '未检测到安装版本')}
            try:
                if row['kind'] == 'apt':
                    package = local['apt'].get('managed', {}).get(row['source'])
                    if not package:
                        raise ValueError('未能核对对应的 Ubuntu 软件包候选')
                    return row | {'latest': package['candidate'], 'available': package['upgradable'],
                                  'basis': 'Engine 自报；按 Ubuntu 软件源判断更新',
                                  'note': '已安装软件包 ' + package['installed']}
                target, url = latest(row['kind'], row['source'], http)
                note = ''
                if row['id'] == 'chrome' or row['source'].lower() == 'playwright':
                    note = '需验证浏览器、登录态与业务兼容性'
                elif version(target).split('.')[0] != version(row['version']).split('.')[0]:
                    note = '跨主版本，需兼容性验证'
                return row | {'latest': target, 'url': url, 'available': newer(target, row['version']), 'note': note}
            except Exception as exc:
                return row | {'error': redact(str(exc))[:160]}
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(one, checks))
    result = {'schema': CACHE_SCHEMA, 'generation': generation, 'checked_at': started,
              'apt': local['apt'], 'rows': rows}
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM kv WHERE key=?', (GENERATION_KEY,)).fetchone()
        current_generation = json.loads(row[0]) if row else 0
        stale = current_generation != generation
        if not stale:
            db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', ('software_inventory', json.dumps(result, ensure_ascii=False)))
    if stale:
        if _retries:
            return check(max_age=0, _retries=_retries - 1)
        raise RuntimeError('检查期间软件多次更新，请重新查询')
    return result


def render(data: dict, *, full: bool = False) -> str:
    stamp = datetime.fromtimestamp(data['checked_at'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
    lines = [f'腾讯云软件检查（北京时间 {stamp}）']
    apt = data['apt']
    if apt.get('error'):
        lines.append('系统包检查失败：' + apt['error'])
    else:
        index = datetime.fromtimestamp(apt['index_time'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M') if apt.get('index_time') else '未知'
        lines += [f"系统包：{len(apt['packages'])} 项候选，其中 {sum(p['security'] for p in apt['packages'])} 项来自安全更新源",
                  f"软件源索引：{index}；当前需要重启：{'是' if apt['reboot_required'] else '否'}"]
        if not apt.get('index_time') or time.time() - apt['index_time'] > 172800:
            lines.append('软件源索引超过 48 小时或时间未知，候选数量可能过期。')
        packages = apt['packages'] if full else [p for p in apt['packages'] if p['name'] in {'nodejs', 'nginx', 'sudo', 'curl', 'linux-generic', 'python3.12'}]
        lines += [f"  {p['name']}：{p['installed']} → {p['candidate']}" for p in packages]
    pending = [row for row in data['rows'] if row.get('available')]
    lines.append('\n软件与依赖：')
    for row in (data['rows'] if full else pending):
        text = f"{row['name']}：{row.get('version', '未知')}"
        if row.get('basis'):
            text += '（' + row['basis'] + '）'
        if row.get('error'):
            text += '；检查未完成：' + row['error']
        elif row.get('available'):
            text += ' → ' + row['latest']
        else:
            text += '（当前分支无更新）'
        if row.get('note'):
            text += '；' + row['note']
        lines.append(text)
    if not full:
        failed = [row for row in data['rows'] if row.get('error')]
        lines += [f"检查未完成：{row['name']}（{row['error']}）" for row in failed]
        lines.append('\n其余明细保留在只读检查结果中。')
    lines.append('原生更新：/update_codex 或 /update_grok。')
    return '\n'.join(lines)
