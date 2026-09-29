"""Explicit one-time registration of the approved infrastructure feeds."""
from . import monitor


def github(key, name, repo, pattern=r'^v?\d+\.\d+\.\d+$'):
    return {'id': key, 'name': name, 'url': 'https://github.com/' + repo + '/releases.atom', 'kind': 'atom',
            'release_exclude_prerelease': True, 'item_title_include_patterns': ['re:' + pattern],
            'poll_interval_seconds': 3600}


SOURCES = [
    github('infra_frp_releases', 'FRP Releases', 'fatedier/frp'),
    github('infra_openlist_releases', 'OpenList Releases', 'OpenListTeam/OpenList'),
    github('infra_sing_box_releases', 'sing-box Releases', 'SagerNet/sing-box'),
    github('infra_easy_proxies_releases', 'easy_proxies Releases', 'jasonwong1991/easy_proxies', r'^v?\d+\.\d+\.\d+(?:\s+-\s+.*)?$'),
    github('infra_certbot_releases', 'Certbot Releases', 'certbot/certbot', r'^Certbot v?\d+\.\d+\.\d+$'),
    github('infra_redis_86_releases', 'Redis 8.6 Releases', 'redis/redis', r'^v?8\.6\.\d+$'),
    {'id': 'infra_postgresql_18_releases', 'name': 'PostgreSQL 18 Releases',
     'url': 'https://www.postgresql.org/versions.rss', 'kind': 'rss',
     'item_title_include_patterns': [r're:^18\.\d+$'], 'poll_interval_seconds': 3600},
]


def install():
    config = monitor.load_custom_sources_config()
    known = {s['id'] for s in config['sources']} | set(config.get('disabled_source_ids', []))
    additions = [s for s in SOURCES if s['id'] not in known]
    monitor.add_custom_sources(additions)
    result = monitor.baseline_sources(additions)
    validation = monitor.validate_custom_sources_config()
    return {'added': len(additions), 'baseline': result, 'validation': validation}


if __name__ == '__main__':
    import json
    with monitor.NonBlockingLock(monitor.LOCK_PATH) as lock:
        if not lock.acquired:
            raise SystemExit('软件监控正在运行，未修改订阅')
        result = install()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(int(bool(result['baseline']['failed'] or result['validation']['errors'])))
