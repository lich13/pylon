"""Copy only retained business data. Run as root while pylon-bot is stopped."""
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
from pathlib import Path

import yaml

ROOT = Path('/srv/pylon')
OWNER = pwd.getpwnam('pylon')


def own(path):
    os.chown(path, OWNER.pw_uid, OWNER.pw_gid, follow_symlinks=False)


def snapshot(source, target):
    if source.is_symlink():
        return
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        own(target)
        for item in source.iterdir():
            if item.name.endswith(('-wal', '-shm', '.lock')) or item.name in {'SingletonLock', 'SingletonSocket', 'SingletonCookie', 'DevToolsActivePort'}:
                continue
            snapshot(item, target / item.name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.migration-tmp')
    with source.open('rb') as f:
        sqlite = f.read(16) == b'SQLite format 3\x00'
    if sqlite:
        temp.unlink(missing_ok=True)
        with sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=30) as src, sqlite3.connect(temp) as dst:
            src.backup(dst)
        for suffix in ('-wal', '-shm'):
            Path(str(target) + suffix).unlink(missing_ok=True)
    else:
        shutil.copyfile(source, temp)
    os.chmod(temp, 0o600)
    own(temp)
    os.replace(temp, target)


def main():
    assert ROOT.is_dir() and ROOT.resolve() == ROOT
    assert subprocess.run(['systemctl', 'is-active', '--quiet', 'pylon-bot.service']).returncode != 0
    from pylon.config import read_env
    mapping = {
        'default': (Path('/srv/hermes'), ['data/gmail_cleanup', 'data/xianbao_filter', 'config', 'secrets', 'google_client_secret.json', 'google_token.json']),
        'webops': (Path('/srv/hermes/profiles/webops'), ['data', 'config', 'secrets', 'cron_state']),
    }
    for profile, (source, children) in mapping.items():
        target = ROOT / 'profiles' / profile
        target.mkdir(parents=True, exist_ok=True)
        for child in children:
            if (source / child).exists():
                snapshot(source / child, target / child)
        env = read_env(source / '.env')
        config = yaml.safe_load((source / 'config.yaml').read_text())
        channel = env.get('TELEGRAM_HOME_CHANNEL') or config.get('TELEGRAM_HOME_CHANNEL')
        if not channel:
            cron = json.loads((source / 'cron/jobs.json').read_text())
            channel = next(j['origin']['chat_id'] for j in cron['jobs'] if j.get('origin', {}).get('platform') == 'telegram')
        assert str(channel) == '-1001234567890'
        retained = {'TELEGRAM_BOT_TOKEN': env['TELEGRAM_BOT_TOKEN'], 'TELEGRAM_HOME_CHANNEL': str(channel)}
        if profile == 'webops':
            retained['PYLON_GROK_MODEL'] = 'grok-4.7'
        target_env = target / '.env'
        target_env.write_text(''.join(f'{key}={value}\n' for key, value in retained.items()))
        target_env.chmod(0o640)
        os.chown(target_env, 0, OWNER.pw_gid)
        # Imported shell settings contain paths but never rewrite credential values.
        filter_env = target / 'config/xianbao-filter.env'
        if filter_env.exists():
            filter_env.write_text(filter_env.read_text().replace('/srv/hermes', '/srv/pylon/profiles/default'))
        print(profile, 'business snapshot copied')


if __name__ == '__main__':
    main()
