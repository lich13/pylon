#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pylon import events as notification_events
from pylon.config import read_env
import fcntl
import hashlib
import html
import json
import os
import random
import re
import subprocess
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import httpx
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

PROFILE_ROOT = Path(os.environ.get('PYLON_PROFILE_ROOT', '/srv/pylon/profiles/webops'))
STATE_FILE = PROFILE_ROOT / 'cron_state' / 'software_update_monitor.json'
DATA_ROOT = PROFILE_ROOT / 'data' / 'software_update_monitor'
TRANSLATION_CACHE_FILE = DATA_ROOT / 'translation_cache.json'
ENRICHMENT_CACHE_FILE = DATA_ROOT / 'enrichment_cache.json'
ITEMS_DIR = DATA_ROOT / 'items'
LOCK_PATH = PROFILE_ROOT / 'cron_state' / 'software_update_monitor.lock'
CUSTOM_SOURCES_FILE = PROFILE_ROOT / 'config' / 'software_update_monitor_sources.json'
TRANSLATION_CONFIG_FILE = PROFILE_ROOT / 'config' / 'software_update_translation.json'

USER_AGENT = 'pylon-SoftwareUpdateMonitor/1.0'
# Kept for replay and compatibility tests. Production enrichment never calls it.
DEEPL_WEB_URL = 'https://www2.deepl.com/jsonrpc'
MAX_SEEN_IDS = 800
MAX_EVENTS_PER_RUN = 8
MAX_TRANSLATION_BLOCKS_PER_RUN = 8
MAX_ALERT_CHARS = 12000
SOURCE_TIMEOUT_SECONDS = 15
TRANSLATE_TIMEOUT_SECONDS = 12
MIN_TRANSLATE_SLEEP_SECONDS = 0.8
MAX_TRANSLATE_SLEEP_SECONDS = 1.5
TRANSLATE_FAILURE_BACKOFF_SECONDS = 10 * 60
TRANSLATION_MAX_ATTEMPTS = 3
TRANSLATE_RETRY_SLEEP_SECONDS = 0.5
GROK_DIGEST_TIMEOUT_SECONDS = 45
GROK_DIGEST_INPUT_CHARS = 6000
GROK_DIGEST_MAX_ITEMS_PER_RUN = 4
# Initial request plus at most two retries, as configured by the rollout plan.
GROK_DIGEST_MAX_ATTEMPTS = 3
GROK_DIGEST_PROMPT_VERSION = '2026-10-02-v1'
GROK_ENDPOINT_ENV = Path(os.environ.get('PYLON_GROK_ENV', '/srv/pylon/secrets/grok.env'))
TRANSLATION_ENDPOINT_ENV = Path(os.environ.get('PYLON_TRANSLATION_ENV', '/srv/pylon/secrets/translation.env'))
GROK_POLISH_TIMEOUT_SECONDS = 300
GROK_POLISH_INPUT_CHARS = 2400
GROK_POLISH_SCHEMA = json.dumps({
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'summary': {'type': 'string'},
        'translation': {'type': 'string'},
    },
    'required': ['summary', 'translation'],
}, separators=(',', ':'))
GROK_DIGEST_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'title_zh': {'type': 'string'},
        'importance': {'type': 'string', 'enum': ['high', 'normal', 'low']},
        'summary_zh': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': {'type': 'string'}},
        'impact_zh': {'type': 'string'},
        'action_zh': {'type': 'string'},
        'change_types': {'type': 'array', 'items': {'type': 'string', 'enum': [
            'security', 'bugfix', 'feature', 'breaking', 'compatibility',
            'performance', 'docs', 'internal',
        ]}},
        'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': {'type': 'string'}},
        'confidence': {'type': 'number'},
    },
    'required': [
        'title_zh', 'importance', 'summary_zh', 'impact_zh', 'action_zh',
        'change_types', 'evidence', 'confidence',
    ],
}
GROK_DIGEST_SCHEMA_JSON = json.dumps(GROK_DIGEST_SCHEMA, separators=(',', ':'))
GROK_DIGEST_CHANGE_TYPES = {
    'security', 'bugfix', 'feature', 'breaking', 'compatibility',
    'performance', 'docs', 'internal',
}
HIGH_PRIORITY_SOURCE_IDS = {
    'openai_codex_changelog', 'openai_codex_cli_releases', 'grok_build_version',
    'pi_releases', 'github_anthropics_claude_code_releases',
    'github_wei_shaw_sub2api_releases',
}
SOURCE_FAILURE_ALERT_THRESHOLD = 3
LITERAL_PATTERNS = (
    re.compile(r'```[\s\S]*?```'),
    re.compile(r'`[^`\n]+`'),
    re.compile(r'https?://[^\s)>\]]+'),
    re.compile(r'\b(?:Ctrl|Cmd|Control|Alt|Option|Shift)(?:\+[A-Za-z0-9]+)+\b'),
    re.compile(r'(?<![A-Za-z0-9])/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+'),
    re.compile(r'\b[\w.+/-]+\.(?:toml|md|json|py|rs|lock|yml|yaml|sh)\b'),
    re.compile(r'\bv?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.]+)?\b'),
)
GITHUB_RELEASE_RECHECK_SECONDS = 120
DEFAULT_GITHUB_RELEASE_READY_AFTER_SECONDS = 600
DEFAULT_GITHUB_RELEASE_STABLE_CHECKS = 2
GITHUB_RELEASE_PENDING_MAX_SECONDS = 24 * 60 * 60
VERSION_CHANGELOG_RECHECK_SECONDS = 120
VERSION_CHANGELOG_PENDING_MAX_SECONDS = 24 * 60 * 60
WAKE_AGENT_FALSE = '{"wakeAgent": false}'
SOURCE_KINDS = {'rss', 'atom', 'version_json', 'version_text'}
GROK_BUILD_STABLE_URL = 'https://x.ai/cli/stable'
GROK_BUILD_CHANGELOG_TEMPLATE = 'https://x.ai/cli/changelogs/{version}.external.md'
GROK_BUILD_CHANGELOG_PAGE = 'https://x.ai/build/changelog'
VERSION_TEXT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')
READINESS_INT_FIELDS = {'release_ready_after_seconds', 'release_stable_checks', 'release_min_body_chars'}
SOURCE_INT_FIELDS = READINESS_INT_FIELDS | {'poll_interval_seconds'}
READINESS_PATTERN_FIELDS = {'release_required_asset_patterns', 'release_required_content_patterns'}
ITEM_TITLE_PATTERN_FIELDS = {'item_title_include_patterns', 'item_title_exclude_patterns'}
READINESS_BOOL_FIELDS = {'release_exclude_prerelease'}
RECOVERABLE_RELEASE_PENDING_STATUSES = {'prerelease', 'body_too_short'}

SOURCES: list[dict[str, Any]] = [
    {
        'id': 'openai_codex_changelog',
        'name': 'OpenAI Codex Changelog',
        'url': 'https://developers.openai.com/codex/changelog/rss.xml',
        'kind': 'rss',
    },
    {
        'id': 'openai_codex_cli_releases',
        'name': 'OpenAI Codex CLI Releases',
        'url': 'https://github.com/openai/codex/releases.atom',
        'kind': 'atom',
        'item_title_include_patterns': ['re:^\\d+\\.\\d+\\.\\d+$'],
        'release_ready_after_seconds': 0,
        'release_stable_checks': 1,
    },
    {
        'id': 'grok_build_version',
        'name': 'Grok Build',
        'url': GROK_BUILD_STABLE_URL,
        'kind': 'version_text',
        'changelog_url': GROK_BUILD_CHANGELOG_TEMPLATE,
    },
    {
        'id': 'design_director_releases',
        'name': 'Design Director Releases',
        'url': 'https://github.com/lich13/codex-design-director-plugin/releases.atom',
        'kind': 'atom',
    },
    {
        'id': 'design_director_version',
        'name': 'Design Director Plugin Version',
        'url': 'https://raw.githubusercontent.com/lich13/codex-design-director-plugin/main/plugins/design-director/.codex-plugin/plugin.json',
        'kind': 'version_json',
        'version_path': 'version',
        'commits_url': 'https://github.com/lich13/codex-design-director-plugin/commits/main.atom',
    },
    {
        'id': 'grok_search_rs_releases',
        'name': 'Grok Search RS Releases',
        'url': 'https://github.com/lich13/codex-grok-search-rs-plugin/releases.atom',
        'kind': 'atom',
    },
    {
        'id': 'grok_search_rs_version',
        'name': 'Grok Search RS Plugin Version',
        'url': 'https://raw.githubusercontent.com/lich13/codex-grok-search-rs-plugin/main/plugins/grok-search-rs/.codex-plugin/plugin.json',
        'kind': 'version_json',
        'version_path': 'version',
        'commits_url': 'https://github.com/lich13/codex-grok-search-rs-plugin/commits/main.atom',
    },
]

BUILTIN_SOURCE_IDS = {source['id'] for source in SOURCES}


class FetchError(Exception):
    pass


class TranslationError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def epoch_to_utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec='seconds')


def parse_time_to_epoch(text: str) -> float | None:
    value = normalize_ws(text)
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        parsed = None
    if parsed is None:
        try:
            normalized = value[:-1] + '+00:00' if value.endswith('Z') else value
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def default_state() -> dict[str, Any]:
    return {
        'version': 2,
        'created_at': utc_now_iso(),
        'updated_at': None,
        'sources': {},
        'pending_events': [],
        'enrichment': {
            'grok_successes': 0,
            'grok_failures': 0,
            'fallback_translations': 0,
            'original_fallbacks': 0,
            'cache_hits': 0,
            'last_error': None,
            'last_success_at': None,
            'last_provider': None,
        },
        'translation': {
            'consecutive_failures': 0,
            'backoff_until': 0,
            'last_error': None,
        },
    }


def load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    from pylon.shared.state_json import save_atomic
    save_atomic(path, value)


def slugify_source_id(text: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', (text or '').lower()).strip('_')
    return slug or 'custom_source'


def normalize_url_for_dedupe(url: str) -> str:
    parsed = urllib.parse.urlsplit((url or '').strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r'/+', '/', parsed.path).rstrip('/')
    return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ''))


def canonical_source_url(url: str) -> str:
    value = (url or '').strip()
    if normalize_url_for_dedupe(value) == 'https://pi.dev/news.xml':
        return 'https://pi.dev/changelog.xml'
    return value


def default_poll_interval(source_id: str) -> int:
    source_id = str(source_id or '')
    if source_id in HIGH_PRIORITY_SOURCE_IDS:
        return 5 * 60
    if source_id.startswith('infra_'):
        return 60 * 60
    return 30 * 60


def normalize_source(source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return None
    url = canonical_source_url(str(source.get('url') or ''))
    if not re.match(r'^https?://', url):
        return None
    name = normalize_ws(str(source.get('name') or ''))
    if not name:
        name = url
    kind = str(source.get('kind') or '').strip().lower()
    if kind not in SOURCE_KINDS:
        kind = 'rss' if re.search(r'(rss|xml)(?:[?#].*)?$', url, re.I) else 'atom'
    source_id = str(source.get('id') or '').strip()
    if not source_id:
        source_id = slugify_source_id(name)
    source_id = slugify_source_id(source_id)
    normalized = {
        'id': source_id,
        'name': name,
        'url': url,
        'kind': kind,
        'poll_interval_seconds': default_poll_interval(source_id),
    }
    version_path = str(source.get('version_path') or '').strip()
    if version_path:
        normalized['version_path'] = version_path
    commits_url = str(source.get('commits_url') or '').strip()
    if re.match(r'^https?://', commits_url):
        normalized['commits_url'] = commits_url
    changelog_url = str(source.get('changelog_url') or '').strip()
    if re.match(r'^https?://', changelog_url):
        normalized['changelog_url'] = changelog_url
    for field in SOURCE_INT_FIELDS:
        if field not in source:
            continue
        try:
            value = int(source.get(field))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            normalized[field] = value
    for field in READINESS_BOOL_FIELDS:
        if field in source and isinstance(source.get(field), bool):
            normalized[field] = source[field]
    for field in READINESS_PATTERN_FIELDS:
        raw_patterns = source.get(field)
        if isinstance(raw_patterns, str):
            patterns = [raw_patterns]
        elif isinstance(raw_patterns, list):
            patterns = raw_patterns
        else:
            patterns = []
        cleaned = [normalize_ws(str(pattern)) for pattern in patterns if normalize_ws(str(pattern))]
        if cleaned:
            normalized[field] = cleaned
    for field in ITEM_TITLE_PATTERN_FIELDS:
        raw_patterns = source.get(field)
        if isinstance(raw_patterns, str):
            patterns = [raw_patterns]
        elif isinstance(raw_patterns, list):
            patterns = raw_patterns
        else:
            patterns = []
        cleaned = [normalize_ws(str(pattern)) for pattern in patterns if normalize_ws(str(pattern))]
        if cleaned:
            normalized[field] = cleaned
    return normalized


def empty_custom_sources_config() -> dict[str, Any]:
    return {'version': 1, 'updated_at': None, 'disabled_source_ids': [], 'sources': []}


def load_custom_sources_config() -> dict[str, Any]:
    raw = load_json(CUSTOM_SOURCES_FILE, default=empty_custom_sources_config())
    if isinstance(raw, list):
        raw = {'version': 1, 'sources': raw}
    if not isinstance(raw, dict):
        raw = empty_custom_sources_config()
    sources = raw.get('sources')
    if not isinstance(sources, list):
        sources = []
    try:
        version = int(raw.get('version') or 1)
    except (TypeError, ValueError):
        version = 1
    disabled_raw = raw.get('disabled_source_ids')
    if not isinstance(disabled_raw, list):
        disabled_raw = []
    return {
        'version': version,
        'updated_at': raw.get('updated_at'),
        'disabled_source_ids': [slugify_source_id(str(value)) for value in disabled_raw if str(value).strip()],
        'sources': sources,
    }


def save_custom_sources_config(
    sources: list[dict[str, Any]],
    *,
    disabled_source_ids: list[str] | None = None,
) -> None:
    if disabled_source_ids is None:
        disabled_source_ids = load_custom_sources_config().get('disabled_source_ids', [])
    save_json(CUSTOM_SOURCES_FILE, {
        'version': 1,
        'updated_at': utc_now_iso(),
        'disabled_source_ids': list(dict.fromkeys(str(value) for value in disabled_source_ids if str(value).strip())),
        'sources': sources,
    })


def load_sources() -> list[dict[str, str]]:
    config = load_custom_sources_config()
    disabled = {str(value) for value in config.get('disabled_source_ids', [])}
    sources = [dict(source) for source in SOURCES if source['id'] not in disabled]
    seen = {source['id'] for source in sources}
    seen_urls = {normalize_url_for_dedupe(source.get('url', '')) for source in sources}
    for raw_source in config.get('sources', []):
        source = normalize_source(raw_source)
        source_url = normalize_url_for_dedupe(source['url']) if source else ''
        if source is None or source['id'] in disabled or source['id'] in seen or source_url in seen_urls:
            continue
        sources.append(source)
        seen.add(source['id'])
        seen_urls.add(source_url)
    return sources


def monitor_poll_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        'openai_codex_cli_releases': 0,
        'openai_codex_changelog': 1,
    }
    return [
        source for _, source in sorted(
            enumerate(sources),
            key=lambda item: (priority.get(str(item[1].get('id') or ''), 10), item[0]),
        )
    ]


def validate_custom_sources_config() -> dict[str, Any]:
    raw = load_json(CUSTOM_SOURCES_FILE, default=empty_custom_sources_config())
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(raw, list):
        raw_sources = raw
    elif isinstance(raw, dict):
        raw_sources = raw.get('sources')
    else:
        raw_sources = None
    if not isinstance(raw_sources, list):
        errors.append('sources must be a list')
        raw_sources = []

    seen_ids = set(BUILTIN_SOURCE_IDS)
    seen_urls = {normalize_url_for_dedupe(source['url']) for source in SOURCES}
    valid_sources = 0
    for index, raw_source in enumerate(raw_sources):
        source = normalize_source(raw_source)
        if source is None:
            errors.append(f'source #{index + 1} must include id/name and an http(s) url')
            continue
        if source['id'] in seen_ids:
            errors.append(f"duplicate source id: {source['id']}")
            continue
        source_url = normalize_url_for_dedupe(source['url'])
        if source_url in seen_urls:
            errors.append(f"duplicate source url: {source['url']}")
            continue
        if source['kind'] not in SOURCE_KINDS:
            warnings.append(f"source kind normalized for {source['id']}: {source['kind']}")
        for field in SOURCE_INT_FIELDS:
            if field not in raw_source:
                continue
            try:
                value = int(raw_source.get(field))
            except (TypeError, ValueError):
                errors.append(f'{field} for {source["id"]} must be a non-negative integer')
                continue
            if value < 0:
                errors.append(f'{field} for {source["id"]} must be a non-negative integer')
        for field in READINESS_BOOL_FIELDS:
            if field in raw_source and not isinstance(raw_source.get(field), bool):
                errors.append(f'{field} for {source["id"]} must be a boolean')
        for field in READINESS_PATTERN_FIELDS:
            if field not in raw_source:
                continue
            raw_patterns = raw_source.get(field)
            if isinstance(raw_patterns, str):
                patterns = [raw_patterns]
            elif isinstance(raw_patterns, list):
                patterns = raw_patterns
            else:
                errors.append(f'{field} for {source["id"]} must be a string or list of strings')
                continue
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    errors.append(f'{field} for {source["id"]} contains an empty or non-string pattern')
                    continue
                expanded = expand_release_pattern(pattern, 'v1.2.3')
                if expanded.startswith('re:'):
                    try:
                        re.compile(expanded[3:])
                    except re.error as exc:
                        errors.append(f'{field} for {source["id"]} has invalid regex {pattern!r}: {exc}')
        for field in ITEM_TITLE_PATTERN_FIELDS:
            if field not in raw_source:
                continue
            raw_patterns = raw_source.get(field)
            if isinstance(raw_patterns, str):
                patterns = [raw_patterns]
            elif isinstance(raw_patterns, list):
                patterns = raw_patterns
            else:
                errors.append(f'{field} for {source["id"]} must be a string or list of strings')
                continue
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    errors.append(f'{field} for {source["id"]} contains an empty or non-string pattern')
                    continue
                pattern = normalize_ws(pattern)
                if pattern.startswith('re:'):
                    try:
                        re.compile(pattern[3:])
                    except re.error as exc:
                        errors.append(f'{field} for {source["id"]} has invalid regex {pattern!r}: {exc}')
        changelog_url = str(raw_source.get('changelog_url') or '').strip()
        if changelog_url and not re.match(r'^https?://', changelog_url):
            errors.append(f'changelog_url for {source["id"]} must be an http(s) URL')
        seen_ids.add(source['id'])
        seen_urls.add(source_url)
        valid_sources += 1
    return {
        'ok': not errors,
        'errors': errors,
        'warnings': warnings,
        'custom_source_count': valid_sources,
        'config_path': str(CUSTOM_SOURCES_FILE),
    }


def add_custom_sources(new_sources: list[dict[str, str]]) -> dict[str, Any]:
    config = load_custom_sources_config()
    sources: list[dict[str, str]] = []
    index: dict[str, int] = {}
    url_index: dict[str, int] = {}
    builtin_urls = {normalize_url_for_dedupe(source['url']) for source in SOURCES}
    for raw_source in config.get('sources', []):
        source = normalize_source(raw_source)
        if source is None or source['id'] in BUILTIN_SOURCE_IDS or source['id'] in index:
            continue
        source_url = normalize_url_for_dedupe(source['url'])
        if source_url in url_index:
            continue
        index[source['id']] = len(sources)
        url_index[source_url] = len(sources)
        sources.append(source)

    added = 0
    updated = 0
    existing = 0
    changed_sources: list[dict[str, str]] = []
    for raw_source in new_sources:
        source = normalize_source(raw_source)
        if source is None:
            raise ValueError('source must include an http(s) feed URL')
        if source['id'] in BUILTIN_SOURCE_IDS:
            raise ValueError(f"source id conflicts with built-in source: {source['id']}")
        source_url = normalize_url_for_dedupe(source['url'])
        if source_url in builtin_urls:
            existing += 1
            continue
        if source_url in url_index and source['id'] not in index:
            existing += 1
            continue
        if source['id'] in index:
            old_url = normalize_url_for_dedupe(sources[index[source['id']]]['url'])
            sources[index[source['id']]] = source
            url_index.pop(old_url, None)
            url_index[source_url] = index[source['id']]
            updated += 1
        else:
            index[source['id']] = len(sources)
            url_index[source_url] = len(sources)
            sources.append(source)
            added += 1
        changed_sources.append(source)

    save_custom_sources_config(sources)
    return {
        'added': added,
        'updated': updated,
        'existing': existing,
        'sources': changed_sources,
        'config_path': str(CUSTOM_SOURCES_FILE),
    }


def project_id_from_source_id(source_id: str) -> str:
    source_id = slugify_source_id(source_id)
    for suffix in ('_releases', '_version', '_commits'):
        if source_id.endswith(suffix):
            return source_id[:-len(suffix)]
    return source_id


def project_name_from_sources(project_id: str, sources: list[dict[str, Any]]) -> str:
    version_source = next((source for source in sources if str(source.get('id', '')).endswith('_version')), None)
    release_source = next((source for source in sources if str(source.get('id', '')).endswith('_releases')), None)
    source = version_source or release_source or (sources[0] if sources else {})
    name = normalize_ws(str(source.get('name') or project_id))
    for suffix in (' Plugin Version', ' Releases', ' Version', ' Commits'):
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return name or project_id


def list_source_projects(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    config = load_custom_sources_config()
    disabled = {str(value) for value in config.get('disabled_source_ids', [])}
    raw_sources = [dict(source) for source in SOURCES]
    for raw_source in config.get('sources', []):
        source = normalize_source(raw_source)
        if source is not None:
            raw_sources.append(source)

    projects: list[dict[str, Any]] = []
    project_index: dict[str, dict[str, Any]] = {}
    for source in raw_sources:
        source_id = str(source.get('id') or '')
        if not source_id:
            continue
        is_disabled = source_id in disabled
        if is_disabled and not include_disabled:
            continue
        project_id = project_id_from_source_id(source_id)
        project = project_index.get(project_id)
        if project is None:
            project = {
                'number': len(projects) + 1,
                'id': project_id,
                'name': '',
                'source_ids': [],
                'custom': True,
                'disabled': False,
                'sources': [],
            }
            project_index[project_id] = project
            projects.append(project)
        project['source_ids'].append(source_id)
        project['sources'].append({
            'id': source_id,
            'name': source.get('name', ''),
            'url': source.get('url', ''),
            'kind': source.get('kind', ''),
            'custom': source_id not in BUILTIN_SOURCE_IDS,
            'disabled': is_disabled,
        })
        project['custom'] = project['custom'] and source_id not in BUILTIN_SOURCE_IDS
        project['disabled'] = bool(project['disabled'] or is_disabled)

    for project in projects:
        project['name'] = project_name_from_sources(project['id'], project['sources'])
    return projects


def find_source_project(selector: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
    selector = normalize_ws(selector)
    if not selector:
        return None
    projects = list_source_projects(include_disabled=include_disabled)
    if selector.isdigit():
        number = int(selector)
        return next((project for project in projects if project['number'] == number), None)
    normalized = slugify_source_id(selector)
    return next(
        (
            project for project in projects
            if project['id'] == normalized or normalized in {slugify_source_id(source_id) for source_id in project['source_ids']}
        ),
        None,
    )


def remove_source_project(selector: str) -> dict[str, Any]:
    project = find_source_project(selector)
    if project is None:
        return {'ok': False, 'error': f'project not found: {selector}', 'selector': selector}

    config = load_custom_sources_config()
    source_ids = [str(source_id) for source_id in project['source_ids']]
    raw_sources = config.get('sources', [])
    remaining_sources: list[dict[str, Any]] = []
    removed_source_ids: list[str] = []
    for raw_source in raw_sources:
        source = normalize_source(raw_source)
        if source is not None and source['id'] in source_ids:
            removed_source_ids.append(source['id'])
            continue
        if isinstance(raw_source, dict):
            remaining_sources.append(raw_source)

    disabled_ids = list(config.get('disabled_source_ids', []))
    for source_id in source_ids:
        if source_id in BUILTIN_SOURCE_IDS and source_id not in disabled_ids:
            disabled_ids.append(source_id)

    save_custom_sources_config(remaining_sources, disabled_source_ids=disabled_ids)
    state = load_state()
    pruned = prune_inactive_source_runtime(state, active_ids=active_source_ids())
    save_state(state)
    return {
        'ok': True,
        'project': {
            'number': project['number'],
            'id': project['id'],
            'name': project['name'],
        },
        'removed_source_ids': removed_source_ids,
        'disabled_source_ids': [source_id for source_id in source_ids if source_id in BUILTIN_SOURCE_IDS],
        'pruned': pruned,
        'config_path': str(CUSTOM_SOURCES_FILE),
    }


def parse_github_repo(value: str) -> tuple[str, str]:
    text = (value or '').strip()
    match = re.match(r'^https?://github\.com/([^/\s]+)/([^/#?\s]+)', text)
    if match:
        owner, repo = match.group(1), match.group(2)
    else:
        match = re.match(r'^([^/\s]+)/([^/\s]+)$', text)
        if not match:
            raise ValueError('GitHub repo must look like owner/repo or https://github.com/owner/repo')
        owner, repo = match.group(1), match.group(2)
    repo = re.sub(r'\.git$', '', repo)
    if not re.match(r'^[A-Za-z0-9_.-]+$', owner) or not re.match(r'^[A-Za-z0-9_.-]+$', repo):
        raise ValueError('GitHub owner/repo contains unsupported characters')
    return owner, repo


def github_raw_url(owner: str, repo: str, path: str, *, branch: str = 'main') -> str:
    return 'https://raw.githubusercontent.com/{}/{}/{}/{}'.format(
        urllib.parse.quote(owner, safe=''),
        urllib.parse.quote(repo, safe=''),
        urllib.parse.quote(branch, safe=''),
        '/'.join(urllib.parse.quote(part, safe='') for part in path.split('/')),
    )


def github_tree_url(owner: str, repo: str, path: str, *, branch: str = 'main') -> str:
    return 'https://github.com/{}/{}/tree/{}/{}'.format(
        urllib.parse.quote(owner, safe=''),
        urllib.parse.quote(repo, safe=''),
        urllib.parse.quote(branch, safe=''),
        '/'.join(urllib.parse.quote(part, safe='') for part in path.split('/')),
    )


def fetch_codex_plugin_manifest(owner: str, repo: str, *, branch: str = 'main') -> dict[str, Any] | None:
    candidates = ['.codex-plugin/plugin.json']
    try:
        plugins_html = bytes_to_text(fetch_url(github_tree_url(owner, repo, 'plugins', branch=branch)))
    except Exception:
        plugins_html = ''
    for match in re.finditer(r'/[^/\s"\']+/[^/\s"\']+/tree/[^/\s"\']+/plugins/([^/\s"\']+)/\.codex-plugin', plugins_html):
        plugin_name = urllib.parse.unquote(match.group(1))
        if plugin_name and re.match(r'^[A-Za-z0-9_.-]+$', plugin_name):
            candidates.append(f'plugins/{plugin_name}/.codex-plugin/plugin.json')

    for path in candidates:
        try:
            raw = fetch_url(github_raw_url(owner, repo, path, branch=branch))
            data = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            continue
        version = data.get('version') if isinstance(data, dict) else None
        if isinstance(data, dict) and normalize_ws(str(version or '')):
            return {
                'path': path,
                'url': github_raw_url(owner, repo, path, branch=branch),
                'data': data,
            }
    return None


def grok_build_source() -> dict[str, Any] | None:
    source = next((item for item in SOURCES if item.get('id') == 'grok_build_version'), None)
    return dict(source) if source else None


def is_grok_build_monitor_input(value: str) -> bool:
    text = normalize_ws(value).rstrip('/')
    try:
        owner, repo_name = parse_github_repo(text)
    except ValueError:
        owner, repo_name = '', ''
    if owner.lower() == 'xai-org' and repo_name.lower() == 'grok-build':
        return True
    parsed = urllib.parse.urlsplit(text)
    host = parsed.netloc.lower()
    path = re.sub(r'/+', '/', parsed.path).rstrip('/').lower()
    if host in {'x.ai', 'www.x.ai'}:
        if path in {'/cli', '/cli/stable', '/cli/install.sh', '/build/changelog'}:
            return True
        if path.startswith('/cli/changelogs'):
            return True
    return False


def add_monitor_url_source(value: str, *, name: str | None = None, branch: str = 'main') -> dict[str, Any]:
    text = normalize_ws(value)
    if is_grok_build_monitor_input(text):
        source = grok_build_source()
        if source is None:
            return {
                'ok': False,
                'unsupported': True,
                'input': value,
                'error': 'Grok Build builtin source is missing',
                'config_path': str(CUSTOM_SOURCES_FILE),
            }
        return {
            'ok': True,
            'classification': 'grok_build',
            'added': 0,
            'updated': 0,
            'existing': 1,
            'sources': [source],
            'config_path': str(CUSTOM_SOURCES_FILE),
        }
    if re.match(r'^https?://', text) and re.search(r'(?:rss|atom|feed|xml)(?:[?#].*)?$', urllib.parse.urlsplit(text).path, re.I):
        display_name = normalize_ws(name or text)
        result = add_custom_sources([{
            'id': display_name,
            'name': display_name,
            'url': text,
            'kind': 'rss' if re.search(r'(?:rss|xml)(?:[?#].*)?$', urllib.parse.urlsplit(text).path, re.I) else 'atom',
        }])
        result.update({'ok': True, 'classification': 'feed'})
        return result

    try:
        owner, repo_name = parse_github_repo(text)
    except ValueError:
        return {
            'ok': False,
            'unsupported': True,
            'input': value,
            'error': 'unsupported source URL; expected GitHub owner/repo or https://github.com/owner/repo',
            'config_path': str(CUSTOM_SOURCES_FILE),
        }

    display_name = normalize_ws(name or f'{owner}/{repo_name}')
    base_id = f"github_{slugify_source_id(owner)}_{slugify_source_id(repo_name)}"
    manifest = fetch_codex_plugin_manifest(owner, repo_name, branch=branch)
    if manifest is None:
        result = add_custom_sources([{
            'id': f'{base_id}_releases',
            'name': f'{display_name} Releases',
            'url': f'https://github.com/{owner}/{repo_name}/releases.atom',
            'kind': 'atom',
        }])
        result.update({'ok': True, 'classification': 'github_software'})
        return result

    result = add_custom_sources([
        {
            'id': f'{base_id}_releases',
            'name': f'{display_name} Releases',
            'url': f'https://github.com/{owner}/{repo_name}/releases.atom',
            'kind': 'atom',
        },
        {
            'id': f'{base_id}_version',
            'name': f'{display_name} Plugin Version',
            'url': manifest['url'],
            'kind': 'version_json',
            'version_path': 'version',
            'commits_url': f'https://github.com/{owner}/{repo_name}/commits/{urllib.parse.quote(branch, safe="")}.atom',
        },
    ])
    result.update({
        'ok': True,
        'classification': 'codex_plugin',
        'manifest_path': manifest['path'],
    })
    return result


def add_github_repo_source(
    repo: str,
    *,
    name: str | None = None,
    include_commits: bool = False,
    branch: str = 'main',
) -> dict[str, Any]:
    owner, repo_name = parse_github_repo(repo)
    display_name = normalize_ws(name or f'{owner}/{repo_name}')
    base_id = f"github_{slugify_source_id(owner)}_{slugify_source_id(repo_name)}"
    sources = [
        {
            'id': f'{base_id}_releases',
            'name': f'{display_name} Releases',
            'url': f'https://github.com/{owner}/{repo_name}/releases.atom',
            'kind': 'atom',
        },
    ]
    if include_commits:
        branch_path = urllib.parse.quote((branch or 'main').strip(), safe='')
        sources.append({
            'id': f'{base_id}_commits',
            'name': f'{display_name} Commits',
            'url': f'https://github.com/{owner}/{repo_name}/commits/{branch_path}.atom',
            'kind': 'atom',
        })
    return add_custom_sources(sources)


def sanitize_for_json(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r'[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]', '', value)
    if isinstance(value, list):
        return [sanitize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_for_json(key): sanitize_for_json(val) for key, val in value.items()}
    return value


def load_state() -> dict[str, Any]:
    data = load_json(STATE_FILE, default={})
    if not isinstance(data, dict) or not data:
        return default_state()
    try:
        current_version = int(data.get('version') or 0)
    except (TypeError, ValueError):
        current_version = 0
    if current_version < 2:
        data['version'] = 2
    data.setdefault('sources', {})
    data.setdefault('pending_events', [])
    data.setdefault('enrichment', {})
    enrichment = data['enrichment']
    if not isinstance(enrichment, dict):
        enrichment = {}
        data['enrichment'] = enrichment
    for field, default in {
        'grok_successes': 0,
        'grok_failures': 0,
        'fallback_translations': 0,
        'original_fallbacks': 0,
        'cache_hits': 0,
        'last_error': None,
        'last_success_at': None,
        'last_provider': None,
    }.items():
        enrichment.setdefault(field, default)
    data.setdefault('translation', {})
    data['translation'].setdefault('consecutive_failures', 0)
    data['translation'].setdefault('backoff_until', 0)
    return data


def save_state(state: dict[str, Any]) -> None:
    state['updated_at'] = utc_now_iso()
    save_json(STATE_FILE, state)


def strip_html(text: str) -> str:
    text = html.unescape(text or '')
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', text)
    text = re.sub(r'(?s)<br\s*/?>', '\n', text)
    text = re.sub(r'(?s)</p\s*>', '\n', text)
    text = re.sub(r'(?s)<[^>]+>', ' ', text)
    lines = [re.sub(r'[ \t\r\f\v]+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def normalize_ws(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def child_text(children: dict[str, ET.Element], *names: str) -> str:
    for name in names:
        el = children.get(name)
        if el is not None:
            text = ''.join(el.itertext()).strip()
            if text:
                return text
    return ''


def child_link(children: dict[str, ET.Element]) -> str:
    link = children.get('link')
    if link is None:
        return ''
    return (link.attrib.get('href') or link.text or '').strip()


def parse_feed(xml_bytes: bytes, *, source_id: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    items: list[dict[str, str]] = []
    for node in root.iter():
        node_tag = local_name(node.tag)
        if node_tag not in {'item', 'entry'}:
            continue
        children = {local_name(child.tag): child for child in list(node)}
        url = child_link(children)
        guid = child_text(children, 'guid', 'id') or url
        title = normalize_ws(child_text(children, 'title'))
        published = child_text(children, 'pubDate', 'updated', 'published')
        body_raw = child_text(children, 'encoded', 'content', 'summary', 'description')
        body = strip_html(body_raw)
        if not guid:
            seed = '|'.join([source_id, title, published, url, body[:200]])
            guid = hashlib.sha256(seed.encode('utf-8')).hexdigest()
        if not title:
            title = url or guid
        items.append({
            'source_id': source_id,
            'id': guid,
            'title': title,
            'url': url,
            'published_at': published,
            'body': body,
        })
    return items


def nested_value(data: Any, path: str) -> Any:
    current = data
    for part in (path or '').split('.'):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def parse_version_json(raw: bytes, *, source: dict[str, str]) -> list[dict[str, str]]:
    data = json.loads(raw.decode('utf-8', 'replace'))
    version_path = source.get('version_path') or 'version'
    version = nested_value(data, version_path)
    if version is None:
        raise ValueError(f'version field not found at {version_path}')
    version_text = normalize_ws(str(version))
    if not version_text:
        raise ValueError(f'version field is empty at {version_path}')
    description = normalize_ws(str(data.get('description') or '')) if isinstance(data, dict) else ''
    plugin_name = normalize_ws(str(data.get('name') or source.get('name') or '')) if isinstance(data, dict) else source.get('name', '')
    lines = [
        f"当前版本：{version_text}",
        f"版本字段：{version_path}",
    ]
    if plugin_name:
        lines.append(f"插件名称：{plugin_name}")
    if description:
        lines.append(f"说明：{description}")
    return [{
        'source_id': source['id'],
        'id': f'version:{version_text}',
        'title': f"{source['name']} 版本 {version_text}",
        'url': source.get('url', ''),
        'published_at': utc_now_iso(),
        'body': '\n'.join(lines),
    }]


def parse_version_text(raw: bytes, *, source: dict[str, str]) -> list[dict[str, str]]:
    version_text = normalize_ws(bytes_to_text(raw).splitlines()[0] if raw else '')
    if not VERSION_TEXT_RE.fullmatch(version_text):
        raise ValueError(f'version pointer is not a version string: {version_text!r}')
    item_url = expand_changelog_url(source, version_text) or source.get('url', '')
    if source.get('id') == 'grok_build_version':
        item_url = GROK_BUILD_CHANGELOG_PAGE
    return [{
        'source_id': source['id'],
        'id': f'version:{version_text}',
        'title': f"{source['name']} 版本 {version_text}",
        'url': item_url,
        'published_at': utc_now_iso(),
        'body': f"当前版本：{version_text}\n通道：{source.get('url', '')}",
    }]


def expand_changelog_url(source: dict[str, Any], version: str) -> str:
    template = str(source.get('changelog_url') or '').strip()
    if not template:
        return ''
    return template.replace('{version}', urllib.parse.quote(version, safe=''))


def version_from_item(item: dict[str, str]) -> str:
    ident = str(item.get('id') or '')
    if ident.startswith('version:'):
        return ident[len('version:'):]
    return ''


def extract_version_changelog(markdown: str, version: str) -> str:
    text = (markdown or '').replace('\r\n', '\n').strip()
    if not text or not version:
        return ''
    heading = re.search(rf'(?im)^(#+)\s+.*{re.escape(version)}\b.*$', text)
    if heading is None:
        return ''
    level = len(heading.group(1))
    start = heading.start()
    following = re.search(rf'(?m)^#{{1,{level}}}\s+', text[heading.end():])
    if following is None:
        return text[start:].strip()
    return text[start:heading.end() + following.start()].strip()


def changelog_has_notes(notes: str) -> bool:
    for line in (notes or '').splitlines():
        stripped = line.strip()
        if stripped and not re.match(r'^#{1,6}(?:\s+|$)', stripped):
            return True
    return False


def fetch_version_changelog_notes(source: dict[str, Any], version: str) -> str:
    changelog_url = expand_changelog_url(source, version)
    if not changelog_url:
        return ''
    try:
        return extract_version_changelog(bytes_to_text(fetch_url(changelog_url)), version)
    except Exception:
        return ''


def attach_version_changelog_notes(item: dict[str, str], notes: str, changelog_url: str) -> None:
    if not changelog_has_notes(notes):
        return
    item['body'] = (item.get('body', '').rstrip() + '\n\n' + notes.strip()).strip()
    if changelog_url:
        item['url'] = changelog_url


def version_notes_item_id(version_id: str) -> str:
    return f'{version_id}:notes'


def version_notes_followup(
    source: dict[str, Any],
    version_id: str,
    version: str,
    notes: str,
    changelog_url: str,
) -> dict[str, str]:
    item = {
        'source_id': str(source.get('id') or ''),
        'id': version_notes_item_id(version_id),
        'title': f"{source.get('name', source.get('id', ''))} {version} 更新说明",
        'url': changelog_url or str(source.get('url') or ''),
        'published_at': utc_now_iso(),
        'body': f"当前版本：{version}\n通道：{source.get('url', '')}",
    }
    attach_version_changelog_notes(item, notes, changelog_url)
    return item


def remember_version_changelog(
    pending: dict[str, Any],
    version_id: str,
    version: str,
    *,
    now: float,
) -> None:
    if version_id in pending:
        return
    pending[version_id] = {
        'version': version,
        'status': 'changelog_pending',
        'first_seen_at': epoch_to_utc_iso(now),
        'first_seen_epoch': now,
        'last_checked_at': utc_now_iso(),
        'next_check_after': now,
    }


def release_version_changelog(
    pending: dict[str, Any],
    source: dict[str, Any],
    version_id: str,
    seen: set[str],
    *,
    now: float,
) -> dict[str, str] | None:
    entry = pending.get(version_id)
    if not isinstance(entry, dict):
        return None
    notes_id = version_notes_item_id(version_id)
    if notes_id in seen:
        pending.pop(version_id, None)
        return None
    first_seen = pending_float(entry, 'first_seen_epoch', now)
    try:
        next_check_after = float(entry.get('next_check_after') or 0)
    except (TypeError, ValueError):
        next_check_after = 0
    timed_out = now - first_seen > VERSION_CHANGELOG_PENDING_MAX_SECONDS
    if next_check_after > now and not timed_out:
        return None
    version = str(entry.get('version') or version_from_item({'id': version_id}))
    notes = fetch_version_changelog_notes(source, version) if version else ''
    changelog_url = expand_changelog_url(source, version) if version else ''
    if changelog_has_notes(notes):
        pending.pop(version_id, None)
        return version_notes_followup(source, version_id, version, notes, changelog_url)
    if timed_out:
        pending.pop(version_id, None)
        return None
    entry['status'] = 'changelog_pending'
    entry['last_checked_at'] = utc_now_iso()
    entry['next_check_after'] = now + VERSION_CHANGELOG_RECHECK_SECONDS
    pending[version_id] = entry
    return None


def prepare_version_text_items(
    current: dict[str, Any],
    source: dict[str, Any],
    items: list[dict[str, str]],
    *,
    now: float,
) -> list[dict[str, str]]:
    """Emit a version change immediately, then a second item once notes exist.

    The first item stays version-only. A follow-up id ``version:<ver>:notes`` is
    emitted on a later poll, including after the channel pointer has moved on.
    Heading-only or missing changelogs are retried for 24 hours and then dropped.
    """
    if not source.get('changelog_url'):
        return items
    pending = current.setdefault('pending_version_changelogs', {})
    if not isinstance(pending, dict):
        pending = {}
        current['pending_version_changelogs'] = pending
    seen = {str(value) for value in current.get('seen_ids', [])}
    ready: list[dict[str, str]] = []
    current_ids: set[str] = set()

    for item in items:
        ident = str(item.get('id') or '')
        version = version_from_item(item)
        if ident:
            current_ids.add(ident)
        if not ident or not version:
            ready.append(item)
            continue
        notes_id = version_notes_item_id(ident)
        if ident not in seen and notes_id not in seen:
            remember_version_changelog(pending, ident, version, now=now)
            ready.append(item)
            continue
        ready.append(item)
        if ident in seen:
            followup = release_version_changelog(pending, source, ident, seen, now=now)
            if followup is not None:
                ready.append(followup)

    for ident, entry in list(pending.items()):
        if ident in current_ids or ident not in seen or not isinstance(entry, dict):
            continue
        followup = release_version_changelog(pending, source, ident, seen, now=now)
        if followup is not None:
            ready.append(followup)
    return ready


def parse_source_items(source: dict[str, str], raw: bytes) -> list[dict[str, str]]:
    if source.get('kind') == 'version_json':
        return parse_version_json(raw, source=source)
    if source.get('kind') == 'version_text':
        return parse_version_text(raw, source=source)
    return parse_feed(raw, source_id=source['id'])


def format_commit_for_body(commit: dict[str, str]) -> str:
    title = normalize_ws(commit.get('title', '')) or commit.get('id', '')
    url = commit.get('url', '')
    published = commit.get('published_at', '')
    short_id = ''
    match = re.search(r'/commit/([0-9a-fA-F]{7,40})', url)
    if match:
        short_id = match.group(1)[:7]
    elif commit.get('id'):
        short_id = str(commit['id']).rsplit('/', 1)[-1][:12]
    prefix = f"- {short_id} " if short_id else "- "
    parts = [prefix + title]
    if published:
        parts.append(f"  时间：{published}")
    if url:
        parts.append(f"  链接：{url}")
    return '\n'.join(parts)


def append_pending_commits(item: dict[str, str], commits: list[dict[str, str]]) -> None:
    if not commits:
        return
    lines = ['本次版本更新包含的 commits：']
    for commit in commits[:20]:
        lines.append(format_commit_for_body(commit))
    if len(commits) > 20:
        lines.append(f"- 另有 {len(commits) - 20} 个 commit 已省略。")
    item['body'] = (item.get('body', '').rstrip() + '\n\n' + '\n'.join(lines)).strip()


def update_pending_commits(state: dict[str, Any], source: dict[str, str]) -> list[dict[str, str]]:
    commits_url = source.get('commits_url')
    if not commits_url:
        return []
    current = source_state(state, source['id'])
    pending = current.setdefault('pending_commits', [])
    seen_commit_ids = {str(value) for value in current.setdefault('seen_commit_ids', [])}
    try:
        commit_items = parse_feed(fetch_url(commits_url), source_id=f"{source['id']}_commits")
    except Exception as exc:
        current['last_commit_error'] = repr(exc)[:500]
        current['last_commit_failure_at'] = utc_now_iso()
        return pending if isinstance(pending, list) else []

    if not isinstance(pending, list):
        pending = []
    new_commits: list[dict[str, str]] = []
    for commit in commit_items:
        commit_id = str(commit.get('id') or '')
        if not commit_id or commit_id in seen_commit_ids:
            continue
        new_commits.append(commit)
        seen_commit_ids.add(commit_id)

    current['pending_commits'] = (new_commits + pending)[:100]
    current['seen_commit_ids'] = list(seen_commit_ids)[-MAX_SEEN_IDS:]
    current['last_commit_success_at'] = utc_now_iso()
    current['last_commit_error'] = None
    return current['pending_commits']


def collect_source_items(
    state: dict[str, Any],
    source: dict[str, str],
    raw: bytes,
    *,
    emit_existing: int = 0,
) -> list[dict[str, str]]:
    items = parse_source_items(source, raw)
    if source.get('kind') == 'version_json':
        current = source_state(state, source['id'])
        pending_commits = update_pending_commits(state, source)
        new_items = select_new_items(state, source, items, emit_existing=emit_existing)
        if new_items:
            for item in new_items:
                append_pending_commits(item, pending_commits)
            current['pending_commits'] = []
        return new_items

    if source.get('kind') == 'version_text' and source.get('changelog_url'):
        current = source_state(state, source['id'])
        if current.get('initialized'):
            items = prepare_version_text_items(current, source, items, now=time.time())
    return select_new_items(state, source, items, emit_existing=emit_existing)


def active_source_ids(sources: list[dict[str, Any]] | None = None) -> set[str]:
    if sources is None:
        sources = load_sources()
    return {str(source.get('id') or '') for source in sources if source.get('id')}


def prune_inactive_source_runtime(state: dict[str, Any], *, active_ids: set[str]) -> dict[str, int]:
    sources = state.setdefault('sources', {})
    if 'pending_events' in state:
        state['pending_events'] = [event for event in state['pending_events']
                                   if event.get('source', {}).get('id') in active_ids]
    removed_state = 0
    for source_id in list(sources):
        if source_id not in active_ids:
            sources.pop(source_id, None)
            removed_state += 1
    removed_items = 0
    if ITEMS_DIR.exists():
        for path in ITEMS_DIR.glob('*.json'):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            item = payload.get('item')
            source = payload.get('source')
            source_id = ''
            if isinstance(item, dict):
                source_id = str(item.get('source_id') or '')
            if not source_id and isinstance(source, dict):
                source_id = str(source.get('id') or '')
            if source_id and source_id not in active_ids:
                path.unlink()
                removed_items += 1
    return {'removed_state': removed_state, 'removed_items': removed_items}


def source_state(state: dict[str, Any], source_id: str) -> dict[str, Any]:
    sources = state.setdefault('sources', {})
    current = sources.setdefault(source_id, {})
    current.setdefault('seen_ids', [])
    current.setdefault('initialized', False)
    current.setdefault('consecutive_failures', 0)
    current.setdefault('backoff_until', 0)
    current.setdefault('last_error', None)
    current.setdefault('failure_alerted', False)
    return current


def github_release_tag_from_atom_item(source: dict[str, str], item: dict[str, str]) -> tuple[str, str, str] | None:
    source_url = urllib.parse.urlsplit(source.get('url', ''))
    if source_url.netloc.lower() != 'github.com' or not source_url.path.endswith('/releases.atom'):
        return None
    item_url = urllib.parse.urlsplit(item.get('url', ''))
    if item_url.netloc.lower() != 'github.com':
        return None
    parts = [part for part in item_url.path.split('/') if part]
    if len(parts) < 5 or parts[2] != 'releases' or parts[3] != 'tag':
        return None
    owner = urllib.parse.unquote(parts[0])
    repo = urllib.parse.unquote(parts[1])
    tag = urllib.parse.unquote('/'.join(parts[4:]))
    if not owner or not repo or not tag:
        return None
    return owner, repo, tag


def source_int(source: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(source.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def source_patterns(source: dict[str, Any], key: str) -> list[str]:
    raw = source.get(key)
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    return [normalize_ws(str(value)) for value in values if normalize_ws(str(value))]


def release_version_from_tag(tag: str) -> str:
    return tag[1:] if tag.lower().startswith('v') else tag


def expand_release_pattern(pattern: str, tag: str) -> str:
    version = release_version_from_tag(tag)
    return str(pattern).replace('{tag}', tag).replace('{version}', version)


def missing_release_patterns(patterns: list[str], text: str, tag: str) -> list[str]:
    haystack = text or ''
    haystack_lower = haystack.lower()
    missing: list[str] = []
    for pattern in patterns:
        expanded = expand_release_pattern(pattern, tag)
        if expanded.startswith('re:'):
            try:
                if re.search(expanded[3:], haystack, re.I | re.S):
                    continue
            except re.error:
                pass
        elif expanded.lower() in haystack_lower:
            continue
        missing.append(expanded)
    return missing


def text_matches_pattern(pattern: str, text: str) -> bool:
    pattern = normalize_ws(pattern)
    haystack = text or ''
    if not pattern:
        return False
    if pattern.startswith('re:'):
        try:
            return bool(re.search(pattern[3:], haystack, re.I | re.S))
        except re.error:
            return False
    return pattern.lower() in haystack.lower()


def text_matches_any_pattern(patterns: list[str], text: str) -> bool:
    return any(text_matches_pattern(pattern, text) for pattern in patterns)


def item_allowed_by_title_filters(source: dict[str, Any], item: dict[str, str]) -> bool:
    title = normalize_ws(str(item.get('title') or ''))
    include_patterns = source_patterns(source, 'item_title_include_patterns')
    exclude_patterns = source_patterns(source, 'item_title_exclude_patterns')
    if include_patterns and not text_matches_any_pattern(include_patterns, title):
        return False
    if exclude_patterns and text_matches_any_pattern(exclude_patterns, title):
        return False
    return True


def monitor_item_saved(source_id: str, item_id: str) -> bool:
    if not source_id or not item_id:
        return False
    seed = f'{source_id}:{item_id}'
    return (ITEMS_DIR / (hashlib.sha256(seed.encode('utf-8')).hexdigest() + '.json')).exists()


def codex_changelog_cli_release_version(item: dict[str, str]) -> str | None:
    title = normalize_ws(str(item.get('title') or ''))
    match = re.match(r'^Codex CLI Release:\s*(\d+\.\d+\.\d+)\s*$', title)
    return match.group(1) if match else None


def codex_cli_release_item_id(version: str) -> str:
    return f'tag:github.com,2008:Repository/965415649/rust-v{version}'


def codex_changelog_cli_release_should_skip(source: dict[str, Any], item: dict[str, str]) -> bool:
    if source.get('id') != 'openai_codex_changelog':
        return False
    title = normalize_ws(str(item.get('title') or ''))
    if not title.startswith('Codex CLI Release:'):
        return False
    version = codex_changelog_cli_release_version(item)
    if not version:
        return True
    github_item_id = codex_cli_release_item_id(version)
    return monitor_item_saved('openai_codex_cli_releases', github_item_id)


def github_expanded_assets_url(owner: str, repo: str, tag: str) -> str:
    return 'https://github.com/{}/{}/releases/expanded_assets/{}'.format(
        urllib.parse.quote(owner, safe=''),
        urllib.parse.quote(repo, safe=''),
        urllib.parse.quote(tag, safe=''),
    )


def bytes_to_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode('utf-8', 'replace')
    return str(raw)


def github_release_page_looks_ready(page_html: str) -> bool:
    page = html.unescape(page_html or '')
    lower = page.lower()
    if not page.strip():
        return False
    not_release_markers = [
        'this tag was signed',
        'there aren&#39;t any releases here',
        "there aren't any releases here",
        '<title>sign in to github',
        '<title>page not found',
    ]
    if any(marker in lower for marker in not_release_markers):
        return False
    has_release_title = bool(re.search(r'<title>\s*Release\b', page, re.I))
    has_release_meta = bool(re.search(r'<meta[^>]+content=["\']Release\b', page, re.I))
    has_release_action = 'released this' in lower
    return (has_release_title or has_release_meta) and has_release_action


def github_release_page_is_prerelease(page_html: str) -> bool:
    page_text = normalize_ws(strip_html(html.unescape(page_html or ''))).lower()
    if not page_text:
        return False
    prerelease_markers = [
        'pre-release',
        'this is a pre-release',
    ]
    return any(marker in page_text for marker in prerelease_markers)


def github_release_fingerprint(item: dict[str, str], page_html: str, assets_html: str) -> str:
    release_text = strip_html(page_html)[:20000]
    assets_text = strip_html(assets_html)[:10000]
    seed = json.dumps({
        'title': item.get('title', ''),
        'body': item.get('body', ''),
        'release_page': release_text,
        'assets': assets_text,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()


def pending_float(entry: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(entry.get(key, default))
    except (TypeError, ValueError):
        return default


def update_release_pending(
    pending: dict[str, Any],
    ident: str,
    item: dict[str, str],
    tag: str,
    now: float,
    *,
    status: str,
    next_check_after: float,
    **extra: Any,
) -> dict[str, Any]:
    existing = pending.get(ident)
    entry = dict(existing) if isinstance(existing, dict) else {}
    first_seen_epoch = pending_float(entry, 'first_seen_epoch', now)
    entry.update({
        'url': item.get('url', ''),
        'tag': tag,
        'status': status,
        'first_seen_at': entry.get('first_seen_at') or epoch_to_utc_iso(first_seen_epoch),
        'first_seen_epoch': first_seen_epoch,
        'feed_updated_at': item.get('published_at', ''),
        'last_checked_at': utc_now_iso(),
        'next_check_after': next_check_after,
    })
    entry.update(extra)
    pending[ident] = entry
    return entry


def has_release_strong_gates(source: dict[str, Any]) -> bool:
    return bool(
        source_patterns(source, 'release_required_asset_patterns')
        or source_patterns(source, 'release_required_content_patterns')
    )


def github_release_atom_item_ready(
    current: dict[str, Any],
    source: dict[str, Any],
    item: dict[str, str],
    *,
    now: float,
    baseline: bool = False,
) -> bool:
    tag_ref = github_release_tag_from_atom_item(source, item)
    if tag_ref is None:
        return True
    ident = str(item.get('id') or '')
    if not ident:
        return False
    pending = current.setdefault('pending_github_release_checks', {})
    if not isinstance(pending, dict):
        pending = {}
        current['pending_github_release_checks'] = pending
    cached = pending.get(ident)
    ready_after = source_int(source, 'release_ready_after_seconds', DEFAULT_GITHUB_RELEASE_READY_AFTER_SECONDS)
    strong_gates = has_release_strong_gates(source)
    if isinstance(cached, dict):
        first_seen_epoch = pending_float(cached, 'first_seen_epoch', now)
        cached_status = str(cached.get('status') or '')
        if now - first_seen_epoch > GITHUB_RELEASE_PENDING_MAX_SECONDS and cached_status not in RECOVERABLE_RELEASE_PENDING_STATUSES:
            update_release_pending(
                pending,
                ident,
                item,
                tag_ref[2],
                now,
                status='stale_pending_release',
                next_check_after=now + 3600,
            )
            current['last_error'] = f"stale_pending_release: {item.get('url', ident)}"
            return False
        try:
            next_check_after = float(cached.get('next_check_after') or 0)
        except (TypeError, ValueError):
            next_check_after = 0
        should_honor_throttle = not (cached_status == 'settling' and (ready_after == 0 or strong_gates))
        if should_honor_throttle and next_check_after > now:
            return False

    owner, repo, tag = tag_ref
    first_seen_epoch = pending_float(cached, 'first_seen_epoch', now) if isinstance(cached, dict) else now
    ready_after_epoch = first_seen_epoch + ready_after
    if ready_after and now < ready_after_epoch:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='settling',
            next_check_after=ready_after_epoch,
        )
        return False

    try:
        page_html = bytes_to_text(fetch_url(item.get('url', '')))
    except Exception as exc:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='release_page_fetch_error',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
            last_error=repr(exc)[:500],
        )
        return False
    if not github_release_page_looks_ready(page_html):
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='not_release_page',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
        )
        return False

    if source.get('release_exclude_prerelease') is True and github_release_page_is_prerelease(page_html):
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='prerelease',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
        )
        return False

    required_asset_patterns = source_patterns(source, 'release_required_asset_patterns')
    assets_html = ''
    try:
        assets_html = bytes_to_text(fetch_url(github_expanded_assets_url(owner, repo, tag)))
    except Exception as exc:
        if required_asset_patterns:
            update_release_pending(
                pending,
                ident,
                item,
                tag,
                now,
                status='release_assets_fetch_error',
                next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
                last_error=repr(exc)[:500],
            )
            return False

    min_body_chars = source_int(source, 'release_min_body_chars', 0)
    body_length = len(strip_html(item.get('body', '')).strip())
    if min_body_chars and body_length < min_body_chars:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='body_too_short',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
            body_length=body_length,
            min_body_chars=min_body_chars,
        )
        return False

    content_text = '\n'.join([
        item.get('title', ''),
        item.get('body', ''),
        html.unescape(page_html),
        strip_html(page_html),
    ])
    missing_content = missing_release_patterns(
        source_patterns(source, 'release_required_content_patterns'),
        content_text,
        tag,
    )
    if missing_content:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='missing_required_content',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
            missing=missing_content,
        )
        return False

    asset_text = '\n'.join([html.unescape(assets_html), strip_html(assets_html)])
    missing_assets = missing_release_patterns(required_asset_patterns, asset_text, tag)
    if missing_assets:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='missing_required_assets',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
            missing=missing_assets,
        )
        return False

    fingerprint = github_release_fingerprint(item, page_html, assets_html)
    stable_required = 1 if baseline or strong_gates else max(1, source_int(source, 'release_stable_checks', DEFAULT_GITHUB_RELEASE_STABLE_CHECKS))
    previous_fingerprint = str(cached.get('fingerprint') or '') if isinstance(cached, dict) else ''
    previous_stable_count = int(cached.get('stable_count') or 0) if isinstance(cached, dict) else 0
    stable_count = previous_stable_count + 1 if previous_fingerprint == fingerprint else 1
    if stable_count < stable_required:
        update_release_pending(
            pending,
            ident,
            item,
            tag,
            now,
            status='waiting_for_stability',
            next_check_after=now + GITHUB_RELEASE_RECHECK_SECONDS,
            fingerprint=fingerprint,
            stable_count=stable_count,
        )
        return False

    pending.pop(ident, None)
    return True


def select_new_items(
    state: dict[str, Any],
    source: dict[str, str],
    items: list[dict[str, str]],
    *,
    emit_existing: int = 0,
) -> list[dict[str, str]]:
    current = source_state(state, source['id'])
    seen = {str(value) for value in current.get('seen_ids', [])}
    was_initialized = bool(current.get('initialized'))
    new_items: list[dict[str, str]] = []
    trackable_items: list[dict[str, str]] = []
    now = time.time()

    for item in items:
        ident = str(item.get('id') or '')
        if not ident:
            continue
        already_seen = ident in seen
        baseline_mode = not was_initialized and emit_existing == 0
        if not item_allowed_by_title_filters(source, item):
            continue
        if codex_changelog_cli_release_should_skip(source, item):
            continue
        if not already_seen and not baseline_mode and not github_release_atom_item_ready(
            current,
            source,
            item,
            now=now,
            baseline=False,
        ):
            continue
        if was_initialized and not already_seen:
            new_items.append(item)
        seen.add(ident)
        trackable_items.append(item)

    if not was_initialized and emit_existing > 0:
        new_items = trackable_items[:emit_existing]

    ordered = [str(item.get('id')) for item in trackable_items if item.get('id')]
    remaining = [value for value in current.get('seen_ids', []) if value not in set(ordered)]
    current['seen_ids'] = (ordered + remaining)[:MAX_SEEN_IDS]
    current['initialized'] = True
    current['last_success_at'] = utc_now_iso()
    current['consecutive_failures'] = 0
    pending_checks = current.get('pending_github_release_checks')
    has_stale_pending = any(
        isinstance(value, dict) and value.get('status') == 'stale_pending_release'
        for value in pending_checks.values()
    ) if isinstance(pending_checks, dict) else False
    if not has_stale_pending:
        current['last_error'] = None
    return new_items


def get_i_count(text: str) -> int:
    return (text or '').count('i')


def deepl_timestamp(text: str, now_ms: int | None = None) -> int:
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    i_count = get_i_count(text)
    if i_count:
        i_count += 1
        return ts - (ts % i_count) + i_count
    return ts


def random_deepl_id() -> int:
    return random.randint(100000, 199998) * 1000


def build_deepl_payload(
    text: str,
    *,
    source_lang: str = 'EN',
    target_lang: str = 'ZH-HANS',
    request_id: int | None = None,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], str]:
    request_id = int(request_id if request_id is not None else random_deepl_id())
    deep_target = 'ZH' if target_lang in {'ZH-HANS', 'ZH-HANT'} else target_lang
    lang: dict[str, str] = {
        'source_lang_user_selected': source_lang,
        'target_lang': deep_target,
    }
    params: dict[str, Any] = {
        'texts': [{'text': text, 'requestAlternatives': 3}],
        'splitting': 'newlines',
        'lang': lang,
        'timestamp': deepl_timestamp(text, now_ms=now_ms),
    }
    if target_lang in {'ZH-HANS', 'ZH-HANT'}:
        params['commonJobParams'] = {'regionalVariant': target_lang}
    payload = {
        'jsonrpc': '2.0',
        'method': 'LMT_handle_texts',
        'id': request_id,
        'params': params,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    if (request_id + 5) % 29 == 0 or (request_id + 3) % 13 == 0:
        body = body.replace('"method":"', '"method" : "')
    else:
        body = body.replace('"method":"', '"method": "')
    return payload, body


class GrokDigestError(TranslationError):
    pass


def event_key(event: dict[str, Any]) -> str:
    source = event.get('source') if isinstance(event.get('source'), dict) else {}
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    seed = f"{source.get('id') or item.get('source_id') or ''}:{item.get('id') or ''}"
    return hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]


def enrichment_cache_key(source: dict[str, Any], item: dict[str, Any]) -> str:
    env = read_env(GROK_ENDPOINT_ENV) | os.environ.copy()
    model = env.get('PYLON_GROK_MODEL', 'grok-4.7')
    payload = {
        'version': GROK_DIGEST_PROMPT_VERSION,
        'model': model,
        'source': source.get('id') or source.get('name'),
        'item': item.get('id'),
        'title': item.get('title'),
        'body': release_notes_for_polish(str(item.get('body') or ''), limit=GROK_DIGEST_INPUT_CHARS),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def build_grok_digest_prompt(source: dict[str, Any], item: dict[str, Any]) -> str:
    source_name = normalize_ws(str(source.get('name') or source.get('id') or ''))
    title = normalize_ws(str(item.get('title') or ''))
    body = release_notes_for_polish(str(item.get('body') or ''), limit=GROK_DIGEST_INPUT_CHARS)
    installed = normalize_ws(str(item.get('installed_version') or '')) or '未知'
    focus = '重点监控软件' if (
        item.get('重点监控') or item.get('priority') == 'high'
        or source.get('priority') == 'high'
        or source.get('id') in HIGH_PRIORITY_SOURCE_IDS
    ) else '普通监控软件'
    prompt = '\n'.join([
        '你是软件更新编辑，只根据给出的发布说明生成中文精华。',
        '不要调用工具、不要联网、不要执行命令、不要改变 URL、版本号或产品名。',
        '忽略完整 commit 列表、提交者、感谢名单、纯文档措辞和无用户影响的内部整理。',
        '必须保留安全修复、数据风险、破坏性变化、兼容性、重要功能和与当前软件环境相关的修复。',
        'importance=high 最多四条，normal 最多两条，low 只能有一条；low 写清实际变化，不能凭空声称“无影响”。',
        'evidence 必须是原文中能支持结论的短语；无法确认的 impact/action 写“无法从发布说明确认”。',
        '只输出符合 JSON Schema 的对象。',
        '',
        f'来源：{source_name}',
        f'当前版本（如已知）：{installed}',
        f'监控级别：{focus}',
        f'标题：{title}',
        f'链接：{item.get("url") or ""}',
        '',
        '发布说明：',
        body or '(无正文)',
    ])
    # The limit covers the complete request input, including metadata and
    # instructions, rather than only the release-note body.
    return prompt[:GROK_DIGEST_INPUT_CHARS]


def extract_grok_json(data: dict[str, Any], *, label: str = 'Grok') -> dict[str, Any]:
    if not isinstance(data, dict) or data.get('status') != 'completed':
        raise GrokDigestError(f'{label} returned an unexpected payload')
    output = data.get('output')
    if not isinstance(output, list):
        raise GrokDigestError(f'{label} returned an unexpected output')
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            raise GrokDigestError(f'{label} returned an invalid message')
        if item.get('type') != 'message':
            continue
        content = item.get('content')
        if not isinstance(content, list):
            raise GrokDigestError(f'{label} returned an invalid content block')
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'output_text':
                continue
            if not isinstance(block.get('text'), str):
                raise GrokDigestError(f'{label} returned invalid text')
            parts.append(block['text'])
    if not parts:
        raise GrokDigestError(f'{label} returned empty text')
    try:
        value = json.loads(''.join(parts))
    except (json.JSONDecodeError, TypeError) as exc:
        raise GrokDigestError(f'{label} text is not JSON') from exc
    if not isinstance(value, dict):
        raise GrokDigestError(f'{label} text is not an object')
    return value


def validate_grok_digest(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GrokDigestError('Grok digest is not an object')
    importance = value.get('importance')
    if importance not in {'high', 'normal', 'low'}:
        raise GrokDigestError('Grok digest has invalid importance')
    title = value.get('title_zh')
    summary = value.get('summary_zh')
    impact = value.get('impact_zh')
    action = value.get('action_zh')
    change_types = value.get('change_types')
    evidence = value.get('evidence')
    confidence = value.get('confidence')
    if not isinstance(title, str) or not title.strip():
        raise GrokDigestError('Grok digest missing title_zh')
    if not isinstance(summary, list) or not summary or len(summary) > 4 or not all(isinstance(v, str) and v.strip() for v in summary):
        raise GrokDigestError('Grok digest has invalid summary_zh')
    if importance == 'low' and len(summary) != 1:
        raise GrokDigestError('low importance digest must contain one summary')
    if importance == 'normal' and len(summary) > 2:
        raise GrokDigestError('normal importance digest must contain at most two summaries')
    if not isinstance(impact, str) or not isinstance(action, str):
        raise GrokDigestError('Grok digest missing impact/action')
    if not isinstance(change_types, list) or not all(value in GROK_DIGEST_CHANGE_TYPES for value in change_types):
        raise GrokDigestError('Grok digest has invalid change_types')
    if not isinstance(evidence, list) or not evidence or not all(isinstance(v, str) and v.strip() for v in evidence):
        raise GrokDigestError('Grok digest requires evidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise GrokDigestError('Grok digest has invalid confidence')
    return {
        'title_zh': title.strip(),
        'importance': importance,
        'summary_zh': [str(value).strip() for value in summary],
        'impact_zh': impact.strip(),
        'action_zh': action.strip(),
        'change_types': list(dict.fromkeys(change_types)),
        'evidence': [str(value).strip() for value in evidence[:4]],
        'confidence': round(float(confidence), 3),
    }


def _request_grok_structured(prompt: str, *, schema: dict[str, Any], name: str,
                             timeout: int = GROK_DIGEST_TIMEOUT_SECONDS,
                             total_timeout: int | None = None) -> tuple[dict[str, Any], str]:
    env = read_env(GROK_ENDPOINT_ENV) | os.environ.copy()
    base = env.get('GROK_MODELS_BASE_URL', '').rstrip('/')
    key = env.get('XAI_API_KEY', '')
    if not base or not key:
        raise GrokDigestError('Grok API configuration is missing')
    request_id = uuid.uuid4().hex
    deadline = time.monotonic() + total_timeout if total_timeout else None
    payload = {
        'model': env.get('PYLON_GROK_MODEL', 'grok-4.7'),
        'input': [{'role': 'user', 'content': prompt}],
        'reasoning': {'effort': 'medium'},
        'text': {'format': {'type': 'json_schema', 'name': name,
                            'strict': True, 'schema': schema}},
        'store': False,
        'tools': [],
        'stream': True,
    }
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with client.stream('POST', base + '/responses', headers={
                'Authorization': 'Bearer ' + key,
                'X-Request-ID': request_id,
            }, json=payload) as response:
                if response.status_code != 200:
                    raise GrokDigestError(f'Grok HTTP {response.status_code}')
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise GrokDigestError('Grok total deadline exceeded')
                    if line.startswith('data:'):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        data = '\n'.join(data_lines)
                        data_lines = []
                        if data == '[DONE]':
                            continue
                        event = json.loads(data)
                        if not isinstance(event, dict):
                            raise GrokDigestError('Grok stream returned an invalid event')
                        if event.get('type') == 'response.completed':
                            return event.get('response'), request_id
                        if event.get('type') in {'response.failed', 'response.incomplete', 'error'}:
                            raise GrokDigestError('Grok stream failed')
                raise GrokDigestError('Grok stream ended before completion')
    except GrokDigestError:
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise GrokDigestError(f'Grok request failed: {type(exc).__name__}') from exc


def request_grok_digest(source: dict[str, Any], item: dict[str, Any]) -> tuple[dict[str, Any], str]:
    response, request_id = _request_grok_structured(
        build_grok_digest_prompt(source, item),
        schema=GROK_DIGEST_SCHEMA,
        name='software_update_digest',
    )
    return validate_grok_digest(extract_grok_json(response, label='Grok digest')), request_id


def load_translation_endpoints() -> list[dict[str, str]]:
    config = load_json(TRANSLATION_CONFIG_FILE, default={})
    raw = config.get('endpoints', []) if isinstance(config, dict) else []
    if not isinstance(raw, list):
        raw = []
    env = read_env(TRANSLATION_ENDPOINT_ENV) | os.environ.copy()
    env_url = str(env.get('PYLON_TRANSLATE_URL') or '').strip()
    if env_url:
        raw = [{'url': env_url, 'api_key_env': 'PYLON_TRANSLATE_API_KEY'}] + raw
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw:
        if isinstance(value, str):
            value = {'url': value}
        if not isinstance(value, dict):
            continue
        url = str(value.get('url') or '').strip().rstrip('/')
        if not re.match(r'^https?://', url) or url in seen:
            continue
        seen.add(url)
        endpoints.append({
            'url': url,
            'api_key_env': str(value.get('api_key_env') or 'PYLON_TRANSLATE_API_KEY'),
        })
    return endpoints


class LibreTranslateClient:
    def __init__(self, *, state: dict[str, Any] | None = None):
        self.state = state if isinstance(state, dict) else {}
        self.endpoints = load_translation_endpoints()

    def translate(self, text: str, *, source_lang: str = 'en', target_lang: str = 'zh') -> str:
        text = (text or '').strip()
        if not text:
            return ''
        if not self.endpoints:
            raise TranslationError('no LibreTranslate endpoint configured')
        last_error: Exception | None = None
        now = time.time()
        backoffs = self.state.setdefault('endpoint_backoffs', {})
        if not isinstance(backoffs, dict):
            backoffs = {}
            self.state['endpoint_backoffs'] = backoffs
        for endpoint in self.endpoints:
            endpoint_url = endpoint['url']
            if float(backoffs.get(endpoint_url) or 0) > now:
                last_error = TranslationError(f'LibreTranslate endpoint is in backoff: {endpoint_url}')
                continue
            api_key = str((read_env(TRANSLATION_ENDPOINT_ENV) | os.environ.copy()).get(endpoint['api_key_env']) or '')
            payload = {'q': text, 'source': source_lang, 'target': target_lang, 'format': 'text'}
            if api_key:
                payload['api_key'] = api_key
            try:
                response = httpx.post(endpoint['url'], json=payload, timeout=TRANSLATE_TIMEOUT_SECONDS, trust_env=False)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get('Retry-After')
                    delay = min(3600, max(30, int(float(retry_after)))) if retry_after and retry_after.replace('.', '', 1).isdigit() else 300
                    backoffs[endpoint_url] = now + delay
                    # Keep the legacy aggregate field for status/compatibility,
                    # while allowing other configured endpoints to be tried.
                    self.state['backoff_until'] = max(
                        [float(value or 0) for value in backoffs.values()] + [0]
                    )
                    raise TranslationError(f'LibreTranslate HTTP {response.status_code}')
                if response.status_code >= 400:
                    raise TranslationError(f'LibreTranslate HTTP {response.status_code}')
                value = response.json().get('translatedText')
                if not isinstance(value, str) or not value.strip():
                    raise TranslationError('LibreTranslate returned empty text')
                self.state['last_success_at'] = utc_now_iso()
                self.state['last_error'] = None
                backoffs[endpoint_url] = 0
                self.state['backoff_until'] = max(
                    [float(value or 0) for value in backoffs.values()] + [0]
                )
                self.state['endpoint'] = endpoint_url
                return value.strip()
            except (httpx.HTTPError, ValueError, OSError, TranslationError) as exc:
                last_error = exc
                continue
        raise TranslationError(str(last_error or 'LibreTranslate failed'))


def local_fallback_digest(source: dict[str, Any], item: dict[str, Any], translated_title: str | None = None,
                         translated_body: str | None = None) -> dict[str, Any]:
    title = (translated_title or str(item.get('title') or '')).strip()
    body = (translated_body or str(item.get('body') or '')).strip()
    lines = [normalize_ws(line.lstrip('-*0123456789.) ')) for line in body.splitlines() if normalize_ws(line)]
    lines = [line for line in lines if not re.match(r'^(?:full changelog|contributors?|thanks to)\b', line, re.I)]
    summary = (lines[:2] or [body[:280] or title])
    return {
        'title_zh': title or str(item.get('title') or item.get('id') or ''),
        'importance': 'normal',
        'summary_zh': [value[:280] for value in summary[:2]],
        'impact_zh': '无法从发布说明确认。',
        'action_zh': '请查看原文后决定是否更新。',
        'change_types': ['internal'],
        'evidence': [summary[0][:280]],
        'confidence': 0.2,
    }


class DeepLWebTranslator:
    def __init__(
        self,
        *,
        cache_path: Path,
        max_blocks: int = MAX_TRANSLATION_BLOCKS_PER_RUN,
        translation_state: dict[str, Any] | None = None,
        now_func=time.time,
    ):
        self.cache_path = cache_path
        self.max_blocks = max_blocks
        self.blocks_used = 0
        self.translation_state = translation_state if isinstance(translation_state, dict) else {}
        self.now_func = now_func
        self.cache = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        data = load_json(self.cache_path, default={})
        return data if isinstance(data, dict) else {}

    def _save_cache(self) -> None:
        save_json(self.cache_path, self.cache)

    def cache_key(self, text: str, source_lang: str, target_lang: str) -> str:
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        return f'deepl_web:{source_lang}:{target_lang}:{digest}'

    def translate(self, text: str, *, source_lang: str = 'EN', target_lang: str = 'ZH-HANS') -> str:
        text = (text or '').strip()
        if not text:
            return ''
        key = self.cache_key(text, source_lang, target_lang)
        if key in self.cache:
            return self.cache[key]
        if float(self.translation_state.get('backoff_until') or 0) > float(self.now_func()):
            raise TranslationError('DeepL Web translation is in backoff')
        if self.blocks_used >= self.max_blocks:
            raise TranslationError('translation block budget exhausted')
        try:
            translated = self._request_translation(text, source_lang=source_lang, target_lang=target_lang)
        except TranslationError as exc:
            failures = int(self.translation_state.get('consecutive_failures') or 0) + 1
            self.translation_state['consecutive_failures'] = failures
            self.translation_state['last_error'] = str(exc)[:500]
            self.translation_state['last_failure_at'] = utc_now_iso()
            self.translation_state['backoff_until'] = float(self.now_func()) + min(3600, TRANSLATE_FAILURE_BACKOFF_SECONDS * failures)
            raise
        self.blocks_used += 1
        self.translation_state['consecutive_failures'] = 0
        self.translation_state['last_error'] = None
        self.translation_state['backoff_until'] = 0
        self.translation_state['last_success_at'] = utc_now_iso()
        self.cache[key] = translated
        self._save_cache()
        return translated

    def _request_translation(self, text: str, *, source_lang: str, target_lang: str) -> str:
        last_error: Exception | None = None
        for attempt in range(TRANSLATION_MAX_ATTEMPTS):
            try:
                translated = self._request_translation_once(text, source_lang=source_lang, target_lang=target_lang)
            except TranslationError as exc:
                last_error = exc
                if attempt + 1 >= TRANSLATION_MAX_ATTEMPTS:
                    break
                time.sleep(TRANSLATE_RETRY_SLEEP_SECONDS * (attempt + 1))
                continue
            time.sleep(random.uniform(MIN_TRANSLATE_SLEEP_SECONDS, MAX_TRANSLATE_SLEEP_SECONDS))
            return translated
        raise last_error or TranslationError('DeepL Web translation failed')

    def _request_translation_once(self, text: str, *, source_lang: str, target_lang: str) -> str:
        _, body = build_deepl_payload(text, source_lang=source_lang, target_lang=target_lang)
        req = urllib.request.Request(
            DEEPL_WEB_URL,
            data=body.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TRANSLATE_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as exc:
            raise TranslationError(f'DeepL Web HTTP {exc.code}') from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TranslationError(f'DeepL Web network error: {exc}') from exc

        try:
            data = json.loads(raw)
            if 'error' in data:
                raise TranslationError(f"DeepL Web JSON-RPC error: {data['error']}")
            translated = data['result']['texts'][0]['text']
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f'DeepL Web unexpected response: {raw[:300]}') from exc
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationError('DeepL Web returned empty text')
        return translated.strip()


def split_translation_blocks(text: str, *, max_chars: int = 3200, max_blocks: int = 12) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r'\n{2,}|\r\n{2,}', text or '') if p.strip()]
    if not paragraphs and text.strip():
        paragraphs = [text.strip()]
    blocks: list[str] = []
    current = ''
    for paragraph in paragraphs:
        if len(blocks) >= max_blocks:
            break
        paragraph = paragraph[:max_chars]
        if not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= max_chars:
            current += '\n\n' + paragraph
        else:
            blocks.append(current)
            current = paragraph
    if current and len(blocks) < max_blocks:
        blocks.append(current)
    return blocks[:max_blocks]


def fallback_summary(text: str, *, max_chars: int = 900) -> str:
    text = strip_html(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + '...'


def translate_text_safe(translator: DeepLWebTranslator, text: str) -> tuple[str, bool]:
    if not text.strip():
        return '', True
    try:
        return translator.translate(text), True
    except TranslationError:
        return fallback_summary(text), False


def strip_markdown_emphasis(text: str) -> str:
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text or '')
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'\1', text)
    return text


def protect_literals(text: str) -> tuple[str, list[str]]:
    slots: list[str] = []

    def replace(match: re.Match[str]) -> str:
        slots.append(match.group(0))
        return f'__PH{len(slots) - 1}__'

    protected = text or ''
    for pattern in LITERAL_PATTERNS:
        protected = pattern.sub(replace, protected)
    return protected, slots


def restore_literals(text: str, slots: list[str]) -> str:
    restored = text or ''
    for index, value in enumerate(slots):
        restored = restored.replace(f'__PH{index}__', value)
    return restored


def split_readable_units(text: str) -> list[dict[str, Any]]:
    lines = (text or '').replace('\r\n', '\n').split('\n')
    units: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith('```'):
            block = [line]
            index += 1
            while index < len(lines) and not lines[index].startswith('```'):
                block.append(lines[index])
                index += 1
            if index < len(lines):
                block.append(lines[index])
                index += 1
            units.append({'kind': 'literal', 'text': '\n'.join(block)})
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)$', line.strip())
        if heading:
            units.append({'kind': 'heading', 'level': len(heading.group(1)), 'text': heading.group(2).strip()})
            index += 1
            continue
        bullet = re.match(r'^(?:[-*+]|\d+[.)])\s+(.*)$', line.strip())
        if bullet:
            units.append({'kind': 'bullet', 'text': bullet.group(1).strip()})
            index += 1
            continue
        if line.strip():
            units.append({'kind': 'paragraph', 'text': line.strip()})
        index += 1
    return units


def translate_readable_text(translator: DeepLWebTranslator, text: str) -> tuple[str, bool]:
    cleaned = strip_markdown_emphasis(text).strip()
    if not cleaned:
        return '', True
    protected, slots = protect_literals(cleaned)
    translated, ok = translate_text_safe(translator, protected)
    if not ok:
        return text.strip(), False
    return restore_literals(translated, slots).strip(), True


def translate_readable_document(translator: DeepLWebTranslator, text: str) -> tuple[str, bool]:
    units = split_readable_units(text)
    if not units:
        return '', True
    rendered: list[str] = []
    ok_all = True
    for unit in units:
        if unit['kind'] == 'literal':
            rendered.append(str(unit['text']))
            continue
        translated, ok = translate_readable_text(translator, str(unit['text']))
        ok_all = ok_all and ok
        if unit['kind'] == 'heading':
            rendered.append('#' * int(unit['level']) + ' ' + translated)
        elif unit['kind'] == 'bullet':
            rendered.append('- ' + translated)
        else:
            rendered.append(translated)
    return '\n'.join(rendered).strip(), ok_all


def request_grok_polish(prompt: str) -> dict[str, Any]:
    env = read_env(GROK_ENDPOINT_ENV) | os.environ.copy()
    base = env.get('GROK_MODELS_BASE_URL', '').rstrip('/')
    key = env.get('XAI_API_KEY', '')
    if not base or not key:
        raise TranslationError('Grok API configuration is missing')
    payload = {
        'model': env.get('PYLON_GROK_MODEL', 'grok-4.7'),
        'input': [{'role': 'user', 'content': prompt}],
        'reasoning': {'effort': 'xhigh'},
        'text': {'format': {'type': 'json_schema', 'name': 'release_notes',
                            'strict': True, 'schema': json.loads(GROK_POLISH_SCHEMA)}},
        'store': False,
        'tools': [],
        'stream': True,
    }
    try:
        with httpx.Client(timeout=GROK_POLISH_TIMEOUT_SECONDS, trust_env=False) as client:
            with client.stream('POST', base + '/responses', headers={'Authorization': 'Bearer ' + key}, json=payload) as response:
                if response.status_code != 200:
                    raise TranslationError(f'Grok polish HTTP {response.status_code}')
                data_lines = []
                for line in response.iter_lines():
                    if line.startswith('data:'):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        data = '\n'.join(data_lines)
                        data_lines = []
                        if data == '[DONE]':
                            continue
                        event = json.loads(data)
                        if not isinstance(event, dict):
                            raise TranslationError('Grok polish stream returned an invalid event')
                        if event.get('type') == 'response.completed':
                            return event.get('response')
                        if event.get('type') in {'response.failed', 'response.incomplete', 'error'}:
                            raise TranslationError('Grok polish stream failed')
                raise TranslationError('Grok polish stream ended before completion')
    except (httpx.HTTPError, ValueError) as exc:
        raise TranslationError(f'Grok polish request failed: {type(exc).__name__}') from exc


def build_grok_polish_prompt(source_name: str, title: str, original: str, translated: str) -> str:
    return '\n'.join([
        '你是软件更新说明编辑。请把下面的英文更新说明整理成适合 Telegram 阅读的简体中文。',
        '要求：',
        '- summary 最多 6 条，每条一行，以“- ”开头。只有一条变更时只写一条。只用「现在可以」「修复了」「改进了」。不要营销腔，不要新增或删掉功能。',
        '- translation 是优化后的完整说明，保留原有标题和列表顺序，把生硬机翻改成自然中文。不要把来源标题或单独的版本号再写成一行。command palette 译成「命令面板」。',
        '- 命令、路径、快捷键、版本号、代码、URL、产品专有名词保持原样。',
        '- 只输出符合 schema 的 JSON，不要调用工具。',
        '',
        f'来源：{source_name}',
        f'标题：{title}',
        '',
        '英文原文：',
        (original or '').strip()[:GROK_POLISH_INPUT_CHARS],
        '',
        '分条机翻：',
        (translated or '').strip()[:GROK_POLISH_INPUT_CHARS],
    ])


def parse_grok_polish_output(data: dict[str, Any]) -> dict[str, str]:
    if not isinstance(data, dict) or data.get('status') != 'completed':
        raise TranslationError('Grok polish returned an unexpected payload')
    parts = []
    output = data.get('output')
    if not isinstance(output, list):
        raise TranslationError('Grok polish returned an unexpected output')
    for item in output:
        if not isinstance(item, dict):
            raise TranslationError('Grok polish returned an invalid message')
        if item.get('type') != 'message':
            continue
        content = item.get('content')
        if not isinstance(content, list):
            raise TranslationError('Grok polish returned an invalid content block')
        for block in content:
            if not isinstance(block, dict):
                raise TranslationError('Grok polish returned an invalid content block')
            if block.get('type') == 'output_text':
                if not isinstance(block.get('text'), str):
                    raise TranslationError('Grok polish returned invalid text')
                parts.append(block['text'])
    text = ''.join(parts)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TranslationError('Grok polish text is not JSON') from exc
    if not isinstance(payload, dict):
        raise TranslationError('Grok polish text is not an object')
    summary, translation = payload.get('summary'), payload.get('translation')
    if not isinstance(summary, str) or not isinstance(translation, str) or not summary.strip() or not translation.strip():
        raise TranslationError('Grok polish response missing summary or translation')
    return {'summary': summary.strip(), 'translation': translation.strip()}


def release_notes_for_polish(text: str, *, limit: int = GROK_POLISH_INPUT_CHARS) -> str:
    kept: list[str] = []
    for line in (text or '').replace('\r\n', '\n').split('\n'):
        stripped = line.strip()
        if re.match(r'^(?:Full Changelog:.*|Changelog|#{1,6}\s+Changelog.*|#\d{3,}\s+\S.*)$', stripped, re.I):
            break
        kept.append(line)
    notes = '\n'.join(kept).strip()
    if not notes:
        notes = (text or '').strip()
    if len(notes) > limit:
        notes = notes[:limit].rstrip()
    return notes


def chinese_text_is_majority(text: str) -> bool:
    cleaned = text or ''
    for pattern in LITERAL_PATTERNS:
        cleaned = pattern.sub(' ', cleaned)
    han_count = len(re.findall(r'[\u4e00-\u9fff]', cleaned))
    if han_count == 0:
        return False
    if not re.search(r'(?:[A-Za-z]{2,}\s+){3}[A-Za-z]{2,}', cleaned):
        return True
    english_words = re.findall(r'[A-Za-z]{2,}', cleaned)
    return han_count > len(english_words) * 2


def polish_release_notes(
    *,
    source_name: str,
    title: str,
    original: str,
    translated: str,
    request: Any | None = None,
    sleep=time.sleep,
) -> dict[str, str]:
    prompt = build_grok_polish_prompt(
        source_name,
        title,
        release_notes_for_polish(original),
        release_notes_for_polish(translated),
    )
    send = request or request_grok_polish
    last_error: Exception | None = None
    for attempt in range(TRANSLATION_MAX_ATTEMPTS):
        try:
            return parse_grok_polish_output(send(prompt))
        except (TranslationError, subprocess.TimeoutExpired, OSError) as exc:
            last_error = exc
        if attempt + 1 < TRANSLATION_MAX_ATTEMPTS:
            sleep(TRANSLATE_RETRY_SLEEP_SECONDS * (attempt + 1))
    if isinstance(last_error, TranslationError):
        raise last_error
    raise TranslationError(f'Grok polish failed: {last_error}') from last_error


def item_file_name(item: dict[str, str]) -> str:
    seed = f"{item.get('source_id','')}:{item.get('id','')}"
    return hashlib.sha256(seed.encode('utf-8')).hexdigest() + '.json'


def save_raw_item(source: dict[str, str], item: dict[str, str]) -> None:
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {'source': source, 'item': item, 'saved_at': utc_now_iso()}
    path = ITEMS_DIR / item_file_name(item)
    path.write_text(json.dumps(sanitize_for_json(payload), ensure_ascii=False, indent=2), encoding='utf-8')


def format_event(event: dict[str, Any], *, translator: DeepLWebTranslator) -> str:
    source = event['source']
    item = event['item']
    original_title = str(item.get('title') or '')
    original_body = str(item.get('body') or '')
    original_notes = release_notes_for_polish(original_body) or original_title
    polished = None
    polish_failed = False
    if chinese_text_is_majority(original_notes):
        title = original_title
        body_text = original_notes or original_title
        title_ok = True
        body_ok = True
    else:
        title, title_ok = translate_readable_text(translator, original_title)
        body_text, body_ok = translate_readable_document(translator, original_body)
        if title_ok or body_ok:
            try:
                polished = polish_release_notes(
                    source_name=str(source.get('name') or ''),
                    title=original_title,
                    original=original_body,
                    translated=body_text,
                )
            except (TranslationError, subprocess.TimeoutExpired, OSError):
                polish_failed = True
    summary = polished['summary'] if polished else (body_text or title)
    detail = polished['translation'] if polished else ''
    lines = [f"## {source['name']} — {title or item.get('title') or '(无标题)'}", '']
    if item.get('published_at'):
        lines.append(f"发布时间：{item['published_at']}")
    if item.get('url'):
        lines.append(f"链接：{item['url']}")
    lines.extend(['', '中文摘要：', summary or '(无正文)'])
    if detail and detail.strip() != summary.strip():
        lines.extend(['', '更新说明：', detail])
    if not polished and (not title_ok or not body_ok):
        lines.extend(['', '翻译暂不可用或不完整，已保留原文摘要以保证时效。'])
    if polish_failed:
        lines.extend(['', '摘要优化暂不可用，已保留分条翻译。'])
    return '\n'.join(lines).strip()


def format_alerts(events: list[dict[str, Any]], *, translator: DeepLWebTranslator) -> str:
    parts: list[str] = []
    for event in events:
        parts.append(format_event(event, translator=translator))
    message = '\n\n---\n\n'.join(part for part in parts if part).strip()
    if len(message) > MAX_ALERT_CHARS:
        message = message[:MAX_ALERT_CHARS].rstrip() + '\n\n（消息过长，已截断；完整内容已保存到本地 data 目录。）'
    return message


def format_digest_event(event: dict[str, Any], digest: dict[str, Any], *, provider: str = 'grok') -> str:
    source = event.get('source') if isinstance(event.get('source'), dict) else {}
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    importance = str(digest.get('importance') or 'normal')
    labels = {'high': '高价值', 'normal': '普通更新', 'low': '低价值精简'}
    title = str(digest.get('title_zh') or item.get('title') or '(无标题)').strip()
    lines = [f"## {source.get('name') or item.get('source_id') or '软件更新'} — {title} 【{labels.get(importance, '普通更新')}】", '']
    if item.get('published_at'):
        lines.append(f"发布时间：{item['published_at']}")
    if item.get('url'):
        lines.append(f"链接：{item['url']}")
    summary = [str(value).strip() for value in digest.get('summary_zh', []) if str(value).strip()]
    if importance == 'low':
        lines.extend(['', '精简摘要：', summary[0] if summary else '更新说明较少，暂无更多可确认信息。'])
    else:
        lines.append('')
        lines.append('中文精华：')
        lines.extend(f'- {value}' for value in summary[:4])
        impact = str(digest.get('impact_zh') or '').strip()
        action = str(digest.get('action_zh') or '').strip()
        if impact:
            lines.extend(['', f'影响：{impact}'])
        if action:
            lines.append(f'建议：{action}')
    if provider != 'grok':
        lines.extend(['', f'处理方式：{provider}'])
    return '\n'.join(lines).strip()


def _translate_for_fallback(client: LibreTranslateClient, text: str) -> str:
    cleaned = strip_markdown_emphasis(text).strip()
    if not cleaned:
        return ''
    protected, slots = protect_literals(cleaned)
    translated = client.translate(protected)
    restored = translated
    for index, value in enumerate(slots):
        token = re.compile(re.escape(f'__PH{index}__'), re.IGNORECASE)
        if len(token.findall(restored)) != 1:
            raise TranslationError(f'LibreTranslate altered placeholder {index}')
        restored = token.sub(lambda _match: value, restored)
    return restored.strip()


def enrich_event(event: dict[str, Any], *, state: dict[str, Any]) -> tuple[dict[str, Any], str, str | None, str | None]:
    """Return digest, provider, request id, and a sanitized error if any."""
    source = event.get('source') if isinstance(event.get('source'), dict) else {}
    item = event.get('item') if isinstance(event.get('item'), dict) else {}
    enrichment = state.setdefault('enrichment', {})
    body = str(item.get('body') or '')
    title = str(item.get('title') or '')
    if chinese_text_is_majority(release_notes_for_polish(body) or title):
        enrichment['last_provider'] = 'source'
        enrichment['last_success_at'] = utc_now_iso()
        return local_fallback_digest(source, item, translated_title=title, translated_body=release_notes_for_polish(body)), 'source', None, None

    cached = event.get('enrichment_result')
    if isinstance(cached, dict):
        try:
            return validate_grok_digest(cached), str(event.get('enrichment_provider') or 'cache'), str(event.get('enrichment_request_id') or '') or None, None
        except GrokDigestError:
            event.pop('enrichment_result', None)

    cache_key = enrichment_cache_key(source, item)
    cache = load_json(ENRICHMENT_CACHE_FILE, default={})
    if isinstance(cache, dict) and isinstance(cache.get(cache_key), dict):
        try:
            digest = validate_grok_digest(cache[cache_key])
            enrichment['cache_hits'] = int(enrichment.get('cache_hits') or 0) + 1
            enrichment['last_provider'] = 'cache'
            enrichment['last_success_at'] = utc_now_iso()
            return digest, 'cache', None, None
        except GrokDigestError:
            cache.pop(cache_key, None)

    request_id = None
    grok_error = None
    for attempt in range(GROK_DIGEST_MAX_ATTEMPTS):
        try:
            digest, request_id = request_grok_digest(source, item)
            enrichment['grok_successes'] = int(enrichment.get('grok_successes') or 0) + 1
            enrichment['last_provider'] = 'grok'
            enrichment['last_success_at'] = utc_now_iso()
            enrichment['last_error'] = None
            if not isinstance(cache, dict):
                cache = {}
            cache[cache_key] = digest
            save_json(ENRICHMENT_CACHE_FILE, cache)
            return digest, 'grok', request_id, None
        except (GrokDigestError, TranslationError, OSError, httpx.HTTPError) as exc:
            grok_error = f'{type(exc).__name__}: {exc}'[:500]
            if attempt + 1 < GROK_DIGEST_MAX_ATTEMPTS:
                time.sleep(0.5 * (attempt + 1))
    enrichment['grok_failures'] = int(enrichment.get('grok_failures') or 0) + 1
    enrichment['last_error'] = grok_error

    client = LibreTranslateClient(state=state.setdefault('translation', {}))
    try:
        translated_title = _translate_for_fallback(client, title)
        translated_body = _translate_for_fallback(client, release_notes_for_polish(body))
        digest = local_fallback_digest(source, item, translated_title=translated_title, translated_body=translated_body)
        enrichment['fallback_translations'] = int(enrichment.get('fallback_translations') or 0) + 1
        enrichment['last_provider'] = 'libretranslate'
        enrichment['last_success_at'] = utc_now_iso()
        return digest, 'libretranslate', request_id, grok_error
    except (TranslationError, httpx.HTTPError, OSError, ValueError) as exc:
        error = f'{grok_error}; fallback {type(exc).__name__}: {exc}'[:500]
        digest = local_fallback_digest(source, item)
        enrichment['original_fallbacks'] = int(enrichment.get('original_fallbacks') or 0) + 1
        enrichment['last_provider'] = 'original'
        enrichment['last_error'] = error
        return digest, 'original', request_id, error


def process_pending_enrichment(*, max_items: int = GROK_DIGEST_MAX_ITEMS_PER_RUN,
                               dry_run: bool = False) -> dict[str, Any]:
    state = load_state()
    pending = state.get('pending_events')
    if not isinstance(pending, list):
        pending = []
        state['pending_events'] = pending
    processed = sent = 0
    errors: list[str] = []
    remaining: list[dict[str, Any]] = []
    for event in pending:
        if not isinstance(event, dict):
            continue
        if processed >= max(1, int(max_items)):
            remaining.append(event)
            continue
        status = str(event.get('enrichment_status') or 'pending')
        if status == 'sent':
            continue
        try:
            digest, provider, request_id, error = enrich_event(event, state=state)
            event['enrichment_status'] = 'ready'
            event['enrichment_provider'] = provider
            event['enrichment_request_id'] = request_id
            event['enrichment_attempts'] = int(event.get('enrichment_attempts') or 0) + 1
            event['enrichment_result'] = digest
            event['enrichment_result_hash'] = hashlib.sha256(json.dumps(digest, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
            event['enrichment_completed_at'] = utc_now_iso()
            if error:
                event['enrichment_error'] = error
            processed += 1
            if not dry_run:
                save_state(state)
            message = format_digest_event(event, digest, provider=provider)
            if dry_run:
                remaining.append(event)
                continue
            key = f"software-update:{event_key(event)}:{event['enrichment_result_hash']}"
            event_id = notification_events.publish(message, key=key)
            if not event_id:
                remaining.append(event)
                errors.append(f"{event_key(event)}: outbox unavailable")
                continue
            sent += 1
        except Exception as exc:
            event['enrichment_status'] = 'error'
            event['enrichment_error'] = f'{type(exc).__name__}: {exc}'[:500]
            remaining.append(event)
            errors.append(event['enrichment_error'])
            processed += 1
        if not dry_run:
            save_state(state)
    state['pending_events'] = remaining
    if not dry_run:
        save_state(state)
    return {'processed': processed, 'sent': sent, 'pending': len(remaining), 'errors': errors}


def fetch_url(url: str, *, timeout: int = SOURCE_TIMEOUT_SECONDS) -> bytes:
    req = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml, text/plain, text/markdown, application/json, */*',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f'HTTP {exc.code} from {url}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f'network error from {url}: {exc}') from exc


class SourceNotModified(Exception):
    """The source returned 304 and its cached state is still valid."""


def fetch_source(url: str, current: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Fetch a source with conditional headers while keeping error text safe."""
    req_headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml, text/plain, text/markdown, application/json, */*',
    }
    if current.get('etag'):
        req_headers['If-None-Match'] = str(current['etag'])
    if current.get('last_modified'):
        req_headers['If-Modified-Since'] = str(current['last_modified'])
    req = urllib.request.Request(canonical_source_url(url), headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT_SECONDS) as resp:
            headers = {
                'etag': str(resp.headers.get('ETag') or ''),
                'last_modified': str(resp.headers.get('Last-Modified') or ''),
                'final_url': str(resp.geturl() or url),
            }
            return resp.read(), headers
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            raise SourceNotModified from exc
        retry_after = str(exc.headers.get('Retry-After') or '') if exc.headers else ''
        suffix = f' retry_after={retry_after}' if retry_after else ''
        raise FetchError(f'HTTP {exc.code} from {canonical_source_url(url)}{suffix}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f'network error from {canonical_source_url(url)}: {exc}') from exc


def source_poll_interval(source: dict[str, Any]) -> int:
    try:
        configured = int(source.get('poll_interval_seconds'))
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else default_poll_interval(str(source.get('id') or ''))


def due_for_source(current: dict[str, Any], now: float) -> bool:
    try:
        return max(float(current.get('backoff_until') or 0), float(current.get('next_poll_at') or 0)) <= now
    except Exception:
        return True


def mark_source_failure(current: dict[str, Any], error: str, now: float) -> bool:
    failures = int(current.get('consecutive_failures') or 0) + 1
    current['consecutive_failures'] = failures
    current['last_error'] = error[:500]
    current['last_failure_error'] = error[:500]
    current['last_failure_at'] = utc_now_iso()
    current['backoff_until'] = now + min(3600, 60 * failures)
    # A single timeout or a brief upstream hiccup is common for public feeds.
    # Keep it in state for diagnostics, but only turn it into a user-facing
    # failure after several consecutive polls. Once alerted, do not repeat
    # until a successful poll clears the incident.
    already_alerted = bool(current.get('failure_alerted'))
    should_alert = failures >= SOURCE_FAILURE_ALERT_THRESHOLD and not already_alerted
    current['failure_alerted'] = already_alerted or should_alert
    return should_alert


def mark_source_success(current: dict[str, Any], now: float, *, headers: dict[str, str] | None = None) -> bool:
    # Do not report recovery for failures that never crossed the alert
    # threshold. They remain visible in status/logs but stay silent.
    recovered = bool(current.get('failure_alerted'))
    current['consecutive_failures'] = 0
    current['last_success_at'] = utc_now_iso()
    current['backoff_until'] = 0
    current['next_poll_at'] = now
    current['last_error'] = None
    current['failure_alerted'] = False
    if headers:
        for key in ('etag', 'last_modified', 'final_url'):
            if headers.get(key):
                current[key] = headers[key]
    return recovered


def baseline_sources(sources: list[dict[str, str]]) -> dict[str, Any]:
    state = load_state()
    baselined = 0
    failed = 0
    failures: list[dict[str, str]] = []
    now = time.time()
    for source in sources:
        current = source_state(state, source['id'])
        try:
            raw = fetch_url(source['url'])
            collect_source_items(state, source, raw, emit_existing=0)
            current['next_poll_at'] = now + source_poll_interval(source)
            baselined += 1
        except Exception as exc:
            mark_source_failure(current, repr(exc), now)
            failed += 1
            failures.append({'id': source['id'], 'error': repr(exc)[:500]})
    save_state(state)
    return {'baselined': baselined, 'failed': failed, 'failures': failures}


def _poll_sources_for_enqueue(state: dict[str, Any], sources: list[dict[str, Any]],
                             events: list[dict[str, Any]], *, emit_existing: int = 0,
                             now: float | None = None) -> tuple[list[str], list[str]]:
    now = time.time() if now is None else now
    due = []
    for source in monitor_poll_sources(sources):
        if len(events) >= MAX_EVENTS_PER_RUN:
            break
        current = source_state(state, source['id'])
        if due_for_source(current, now):
            due.append((source, dict(current)))
    if not due:
        return [], []

    failures: list[str] = []
    recoveries: list[str] = []
    results: dict[str, tuple[dict[str, str], bytes | None, Exception | None]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(due))) as pool:
        future_map = {
            pool.submit(fetch_source, source['url'], snapshot): source
            for source, snapshot in due
        }
        for future, source in future_map.items():
            try:
                raw, headers = future.result()
                results[source['id']] = (headers, raw, None)
            except SourceNotModified:
                results[source['id']] = ({}, None, None)
            except Exception as exc:
                results[source['id']] = ({}, None, exc)

    for source, _snapshot in due:
        if len(events) >= MAX_EVENTS_PER_RUN:
            break
        current = source_state(state, source['id'])
        headers, raw, error = results[source['id']]
        was_failed = int(current.get('consecutive_failures') or 0) > 0 or bool(current.get('failure_alerted'))
        if error is not None:
            if mark_source_failure(current, repr(error), now):
                failures.append(f"{source['name']}（{str(error)[:180]}）")
            continue
        recovered = mark_source_success(current, now, headers=headers)
        if recovered:
            recoveries.append(source['name'])
        current['next_poll_at'] = now + source_poll_interval(source)
        if raw is None:
            continue
        try:
            new_items = collect_source_items(state, source, raw, emit_existing=emit_existing)
        except Exception as exc:
            if mark_source_failure(current, repr(exc), now):
                failures.append(f"{source['name']}（{str(exc)[:180]}）")
            continue
        for item in new_items:
            save_raw_item(source, item)
            events.append({
                'source': source,
                'item': item,
                'enrichment_status': 'pending',
                'enrichment_attempts': 0,
            })
    return failures, recoveries


def run_monitor(*, emit_existing: int = 0, dry_run: bool = False,
                enqueue_only: bool = False) -> str:
    state = load_state()
    sources = load_sources()
    prune_inactive_source_runtime(state, active_ids=active_source_ids(sources))
    events: list[dict[str, Any]] = list(state.get('pending_events') or [])
    now = time.time()
    failures: list[str] = []
    recoveries: list[str] = []
    if enqueue_only:
        failures, recoveries = _poll_sources_for_enqueue(state, sources, events, emit_existing=emit_existing, now=now)
    else:
        for source in monitor_poll_sources(sources):
            if len(events) >= MAX_EVENTS_PER_RUN:
                break
            current = source_state(state, source['id'])
            if not due_for_source(current, now):
                continue
            try:
                raw = fetch_url(source['url'])
                new_items = collect_source_items(state, source, raw, emit_existing=emit_existing)
                recovered = mark_source_success(current, now)
                current['next_poll_at'] = now + source_poll_interval(source)
                if recovered:
                    recoveries.append(source['name'])
                for item in new_items:
                    save_raw_item(source, item)
                    events.append({'source': source, 'item': item})
                if len(events) >= MAX_EVENTS_PER_RUN:
                    break
            except Exception as exc:
                if mark_source_failure(current, repr(exc), now):
                    failures.append(f"{source['name']}（{str(exc)[:180]}）")

    if failures and not dry_run:
        notification_events.result('failed', '订阅源抓取失败：' + '、'.join(failures))
    if recoveries and not dry_run:
        recovery_key = hashlib.sha256(f"{'|'.join(recoveries)}:{int(now // 60)}".encode()).hexdigest()[:16]
        notification_events.publish('软件更新监控源已恢复：' + '、'.join(recoveries), key='software-source-recovery:' + recovery_key)

    state['pending_events'] = events
    if enqueue_only:
        if not dry_run:
            save_state(state)
        return WAKE_AGENT_FALSE

    if not events:
        if not dry_run:
            save_state(state)
        return WAKE_AGENT_FALSE

    translator = DeepLWebTranslator(cache_path=TRANSLATION_CACHE_FILE, translation_state=state.setdefault('translation', {}))
    alert = format_alerts(events[:MAX_EVENTS_PER_RUN], translator=translator)
    if not dry_run:
        notification_events.publish(alert)
        state['pending_events'] = events[MAX_EVENTS_PER_RUN:]
        save_state(state)
    return alert or WAKE_AGENT_FALSE


class NonBlockingLock:
    def __init__(self, path: Path, *, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.handle = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+')
        try:
            flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(self.handle.fileno(), flags)
        except BlockingIOError:
            self.acquired = False
            return self
        self.acquired = True
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if self.acquired:
                try:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            self.handle.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Monitor AI/Codex software update feeds with no-agent output.')
    parser.add_argument('--config', help='Custom sources config path for management commands.')
    parser.add_argument('--emit-existing', type=int, default=0, help='Emit N existing items per source; useful for manual testing.')
    parser.add_argument('--dry-run', action='store_true', help='Do not write monitor state.')
    parser.add_argument('--probe-translate', action='store_true', help='Run a small configured public translation probe.')
    parser.add_argument('--enqueue-only', action='store_true', help='Only fetch and persist new items for the enrichment worker.')
    parser.add_argument('--list-sources', action='store_true', help='List built-in and custom monitor sources without polling feeds.')
    parser.add_argument('--list-projects', action='store_true', help='List active monitor projects with stable numbers.')
    parser.add_argument('--validate-config', action='store_true', help='Validate custom monitor source config without polling feeds.')
    parser.add_argument('--json', action='store_true', help='Print management command output as JSON.')
    parser.add_argument('--add-source-url', help='Add or update a custom RSS/Atom source URL.')
    parser.add_argument('--add-monitor-url', help='Add a monitor source from a supported conversational URL.')
    parser.add_argument('--add-source-name', help='Display name for --add-source-url or --add-github-repo.')
    parser.add_argument('--add-source-id', help='Stable source id for --add-source-url.')
    parser.add_argument('--source-kind', choices=sorted(SOURCE_KINDS), help='Source kind for --add-source-url.')
    parser.add_argument('--version-path', default='version', help='Dot path for version_json sources; default: version.')
    parser.add_argument('--changelog-url', help='Changelog URL template for version_text sources. Use {version}.')
    parser.add_argument('--add-github-repo', help='Add a GitHub repo release feed, owner/repo or GitHub URL.')
    parser.add_argument('--remove-project', help='Remove/disable a monitor project by list number or project id.')
    parser.add_argument('--include-commits', action='store_true', help='With --add-github-repo, also monitor commits.')
    parser.add_argument('--branch', default='main', help='Branch for GitHub commit Atom feeds; default: main.')
    parser.add_argument('--no-baseline', action='store_true', help='Do not fetch the newly added source to establish a baseline.')
    return parser


def print_management_result(value: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(sanitize_for_json(value), ensure_ascii=False, indent=2))
        return
    if isinstance(value, dict) and 'sources' in value and not {'added', 'updated', 'existing'} & set(value):
        for source in value.get('sources', []):
            marker = 'built-in' if source.get('id') in BUILTIN_SOURCE_IDS else 'custom'
            print(f"{source.get('id')} [{marker}] {source.get('name')} - {source.get('url')}")
        print(f"config: {value.get('config_path')}")
        return
    print(json.dumps(sanitize_for_json(value), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    global CUSTOM_SOURCES_FILE
    args = build_parser().parse_args(argv)
    original_config = CUSTOM_SOURCES_FILE
    if args.config:
        CUSTOM_SOURCES_FILE = Path(args.config).expanduser()
    try:
        if args.probe_translate:
            translator = LibreTranslateClient(state={})
            print(translator.translate('Hello, my friend. How are you?'))
            return 0
        if args.list_sources:
            print_management_result({
                'config_path': str(CUSTOM_SOURCES_FILE),
                'sources': load_sources(),
            }, as_json=args.json)
            return 0
        if args.list_projects:
            print_management_result({
                'config_path': str(CUSTOM_SOURCES_FILE),
                'projects': list_source_projects(),
            }, as_json=args.json)
            return 0
        if args.validate_config:
            result = validate_custom_sources_config()
            print_management_result(result, as_json=args.json)
            return 0 if result.get('ok') else 1
        if args.remove_project:
            with NonBlockingLock(LOCK_PATH, blocking=True):
                result = remove_source_project(args.remove_project)
                print_management_result(result, as_json=args.json)
                return 0 if result.get('ok') else 1
        if args.add_source_url or args.add_github_repo or args.add_monitor_url:
            with NonBlockingLock(LOCK_PATH, blocking=True):
                try:
                    if args.add_monitor_url:
                        result = add_monitor_url_source(
                            args.add_monitor_url,
                            name=args.add_source_name,
                            branch=args.branch,
                        )
                    elif args.add_github_repo:
                        result = add_github_repo_source(
                            args.add_github_repo,
                            name=args.add_source_name,
                            include_commits=args.include_commits,
                            branch=args.branch,
                        )
                    else:
                        result = add_custom_sources([{
                            'id': args.add_source_id or args.add_source_name or args.add_source_url,
                            'name': args.add_source_name or args.add_source_url,
                            'url': args.add_source_url,
                            'kind': args.source_kind or 'atom',
                            'version_path': args.version_path if args.source_kind == 'version_json' else '',
                            'changelog_url': args.changelog_url or '',
                        }])
                    if result.get('ok') is False:
                        print_management_result(result, as_json=args.json)
                        return 2 if result.get('unsupported') else 1
                    if not args.no_baseline and result.get('sources'):
                        result['baseline'] = baseline_sources(result['sources'])
                    print_management_result(result, as_json=args.json)
                    return 0
                except ValueError as exc:
                    print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
                    return 2
        with NonBlockingLock(LOCK_PATH) as lock:
            if not lock.acquired:
                print(WAKE_AGENT_FALSE)
                return 0
            print(run_monitor(
                emit_existing=max(0, args.emit_existing),
                dry_run=args.dry_run,
                enqueue_only=args.enqueue_only,
            ))
        return 0
    finally:
        CUSTOM_SOURCES_FILE = original_config


if __name__ == '__main__':
    raise SystemExit(main())
