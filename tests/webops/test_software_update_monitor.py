import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import load_script_module


_MODULE_COUNTER = itertools.count()


def load_monitor_module():
    return load_script_module(
        f'software_update_monitor_test_{next(_MODULE_COUNTER)}',
        'software_update_monitor.py',
    )


RSS_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>Codex CLI Release: 0.133.0</title>
      <link>https://developers.openai.com/codex/changelog/#github-release-327109656</link>
      <guid>https://developers.openai.com/codex/changelog/#github-release-327109656</guid>
      <pubDate>Thu, 21 May 2026 00:00:00 GMT</pubDate>
      <description>0.133.0</description>
      <content:encoded><![CDATA[
# Codex CLI Release: 0.133.0

## Improvements

- Added appshots support.
- Fixed goal mode progress rendering.
      ]]></content:encoded>
    </item>
  </channel>
</rss>
'''


ATOM_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1/v0.1.7</id>
    <title>Design Director v0.1.7</title>
    <updated>2026-05-23T20:12:05Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/lich13/codex-design-director-plugin/releases/tag/v0.1.7"/>
    <content type="html">&lt;p&gt;Update bundled design references&lt;/p&gt;</content>
  </entry>
</feed>
'''


VERSION_JSON_FIXTURE = b'''{
  "name": "design-director",
  "version": "0.1.7",
  "description": "Automatically choose and apply DESIGN.md."
}
'''


COMMIT_ATOM_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Grit::Commit/abc123</id>
    <title>Update plugin manifest version</title>
    <updated>2026-05-24T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/example/plugin/commit/abc123"/>
    <content type="html">&lt;p&gt;Bump version to 0.1.8&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Grit::Commit/def456</id>
    <title>Refresh bundled references</title>
    <updated>2026-05-24T09:00:00Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/example/plugin/commit/def456"/>
    <content type="html">&lt;p&gt;Add new reference files&lt;/p&gt;</content>
  </entry>
</feed>
'''


GITHUB_RELEASE_ATOM_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/2/v0.1.131</id>
    <title>Sub2API 0.1.131</title>
    <updated>2026-05-26T06:43:35Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Wei-Shaw/sub2api/releases/tag/v0.1.131"/>
    <content type="html">&lt;p&gt;docker pull weishaw/sub2api:0.1.131&lt;/p&gt;</content>
  </entry>
</feed>
'''


CODEX_GITHUB_RELEASE_ATOM_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/965415649/rust-v0.139.0</id>
    <title>0.139.0</title>
    <updated>2026-06-08T23:01:11Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/openai/codex/releases/tag/rust-v0.139.0"/>
    <content type="html">&lt;p&gt;Codex CLI stable release&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/965415649/rust-v0.139.0-alpha.7</id>
    <title>0.139.0-alpha.7</title>
    <updated>2026-06-08T14:50:10Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/openai/codex/releases/tag/rust-v0.139.0-alpha.7"/>
    <content type="html">&lt;p&gt;Codex CLI alpha release&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/965415649/rusty-v8-v149.2.0</id>
    <title>rusty-v8-v149.2.0</title>
    <updated>2026-06-06T00:14:03Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/openai/codex/releases/tag/rusty-v8-v149.2.0"/>
    <content type="html">&lt;p&gt;Dependency release&lt;/p&gt;</content>
  </entry>
</feed>
'''


CODEX_CHANGELOG_MIXED_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Codex app updates</title>
      <link>https://developers.openai.com/codex/changelog/#codex-2026-06-04-app</link>
      <guid>https://developers.openai.com/codex/changelog/#codex-2026-06-04-app</guid>
      <pubDate>Thu, 04 Jun 2026 00:00:00 GMT</pubDate>
      <description>Codex app 26.602</description>
    </item>
    <item>
      <title>Codex CLI Release: 0.138.0</title>
      <link>https://developers.openai.com/codex/changelog/#github-release-334999999</link>
      <guid>https://developers.openai.com/codex/changelog/#github-release-334999999</guid>
      <pubDate>Thu, 04 Jun 2026 00:00:00 GMT</pubDate>
      <description>0.138.0</description>
    </item>
  </channel>
</rss>
'''


GITHUB_RELEASE_ATOM_NO_DOCKER_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/2/v0.1.132</id>
    <title>Sub2API 0.1.132</title>
    <updated>2026-05-26T06:43:35Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/Wei-Shaw/sub2api/releases/tag/v0.1.132"/>
    <content type="html">&lt;p&gt;Release notes are still being prepared.&lt;/p&gt;</content>
  </entry>
</feed>
'''


RELEASE_HTML_READY = b'''<!doctype html>
<html>
  <head>
    <title>Release Sub2API 0.1.131 - Wei-Shaw/sub2api - GitHub</title>
    <meta property="og:title" content="Release Sub2API 0.1.131 - Wei-Shaw/sub2api">
  </head>
  <body>
    <a href="/Wei-Shaw/sub2api/releases/latest">Latest</a>
    <span>github-actions[bot]</span>
    <span>released this</span>
    <article>
      <p>docker pull weishaw/sub2api:0.1.131</p>
      <p>docker pull ghcr.io/wei-shaw/sub2api:0.1.131</p>
    </article>
  </body>
</html>
'''


RELEASE_HTML_TAG_ONLY = b'''<!doctype html>
<html>
  <head><title>Tag v0.1.131 - Wei-Shaw/sub2api - GitHub</title></head>
  <body>
    <h1>v0.1.131</h1>
    <span>This tag was signed</span>
  </body>
</html>
'''


ASSETS_HTML_READY = b'''<div>
  <a href="/Wei-Shaw/sub2api/releases/download/v0.1.131/checksums.txt">checksums.txt</a>
  <a href="/Wei-Shaw/sub2api/releases/download/v0.1.131/sub2api_0.1.131_linux_amd64.tar.gz">sub2api_0.1.131_linux_amd64.tar.gz</a>
  <a href="/Wei-Shaw/sub2api/releases/download/v0.1.131/sub2api_0.1.131_linux_arm64.tar.gz">sub2api_0.1.131_linux_arm64.tar.gz</a>
</div>
'''


ASSETS_HTML_MISSING_ARM64 = b'''<div>
  <a href="/Wei-Shaw/sub2api/releases/download/v0.1.131/checksums.txt">checksums.txt</a>
  <a href="/Wei-Shaw/sub2api/releases/download/v0.1.131/sub2api_0.1.131_linux_amd64.tar.gz">sub2api_0.1.131_linux_amd64.tar.gz</a>
</div>
'''


CODEX_RELEASE_HTML_READY = b'''<!doctype html>
<html>
  <head>
    <title>Release 0.139.0 - openai/codex - GitHub</title>
    <meta property="og:title" content="Release 0.139.0 - openai/codex">
  </head>
  <body>
    <span>github-actions[bot]</span>
    <span>released this</span>
  </body>
</html>
'''


CC_SWITCH_RELEASE_ATOM_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:github.com,2008:Repository/1031912724/v3.16.3</id>
    <title>CC Switch v3.16.3</title>
    <updated>2026-06-14T16:49:06Z</updated>
    <link rel="alternate" type="text/html" href="https://github.com/farion1231/cc-switch/releases/tag/v3.16.3"/>
    <content type="html">&lt;p&gt;CC Switch v3.16.3&lt;/p&gt;&lt;p&gt;Download DMG / MSI / AppImage files.&lt;/p&gt;</content>
  </entry>
</feed>
'''


CC_SWITCH_RELEASE_HTML_READY = b'''<!doctype html>
<html>
  <head>
    <title>Release CC Switch v3.16.3 - farion1231/cc-switch - GitHub</title>
    <meta property="og:title" content="Release CC Switch v3.16.3 - farion1231/cc-switch">
  </head>
  <body>
    <span>github-actions[bot]</span>
    <span>released this</span>
    <article>
      <p>CC Switch v3.16.3</p>
      <p>Detailed release notes for routing, usage accounting, cloud sync, and platform fixes.</p>
    </article>
  </body>
</html>
'''


CC_SWITCH_RELEASE_HTML_PRERELEASE = b'''<!doctype html>
<html>
  <head>
    <title>Release CC Switch v3.16.3 - farion1231/cc-switch - GitHub</title>
    <meta property="og:title" content="Release CC Switch v3.16.3 - farion1231/cc-switch">
  </head>
  <body>
    <span>github-actions[bot]</span>
    <span>released this</span>
    <span>Pre-release</span>
    <article>
      <p>CC Switch v3.16.3</p>
    </article>
  </body>
</html>
'''


class SoftwareUpdateMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / 'webops'
        self.root.mkdir()
        self.monitor = load_monitor_module()
        self.monitor.PROFILE_ROOT = self.root
        self.monitor.STATE_FILE = self.root / 'cron_state' / 'software_update_monitor.json'
        self.monitor.DATA_ROOT = self.root / 'data' / 'software_update_monitor'
        self.monitor.TRANSLATION_CACHE_FILE = self.monitor.DATA_ROOT / 'translation_cache.json'
        self.monitor.ITEMS_DIR = self.monitor.DATA_ROOT / 'items'
        self.monitor.LOCK_PATH = self.root / 'cron_state' / 'software_update_monitor.lock'
        self.monitor.CUSTOM_SOURCES_FILE = self.root / 'config' / 'software_update_monitor_sources.json'

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_parse_rss_reads_content_encoded_as_body(self):
        items = self.monitor.parse_feed(RSS_FIXTURE, source_id='openai_codex_changelog')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'https://developers.openai.com/codex/changelog/#github-release-327109656')
        self.assertEqual(items[0]['title'], 'Codex CLI Release: 0.133.0')
        self.assertIn('Added appshots support', items[0]['body'])
        self.assertEqual(items[0]['published_at'], 'Thu, 21 May 2026 00:00:00 GMT')

    def test_parse_atom_reads_link_and_html_body(self):
        items = self.monitor.parse_feed(ATOM_FIXTURE, source_id='design_director_releases')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'Design Director v0.1.7')
        self.assertEqual(items[0]['url'], 'https://github.com/lich13/codex-design-director-plugin/releases/tag/v0.1.7')
        self.assertEqual(items[0]['body'], 'Update bundled design references')

    def test_parse_version_json_reads_version_field(self):
        source = {
            'id': 'design_director_version',
            'name': 'Design Director Plugin Version',
            'url': 'https://raw.githubusercontent.com/lich13/codex-design-director-plugin/main/plugins/design-director/.codex-plugin/plugin.json',
            'kind': 'version_json',
            'version_path': 'version',
        }

        items = self.monitor.parse_source_items(source, VERSION_JSON_FIXTURE)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'version:0.1.7')
        self.assertEqual(items[0]['title'], 'Design Director Plugin Version 版本 0.1.7')
        self.assertEqual(items[0]['url'], source['url'])
        self.assertIn('当前版本：0.1.7', items[0]['body'])

    def test_parse_version_text_reads_channel_pointer(self):
        source = {
            'id': 'grok_build_version',
            'name': 'Grok Build',
            'url': 'https://x.ai/cli/stable',
            'kind': 'version_text',
            'changelog_url': 'https://x.ai/cli/changelogs/{version}.external.md',
        }

        items = self.monitor.parse_source_items(source, b'1.0.40\n')

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'version:1.0.40')
        self.assertEqual(items[0]['title'], 'Grok Build 版本 1.0.40')
        self.assertEqual(items[0]['url'], 'https://x.ai/build/changelog')
        self.assertIn('当前版本：1.0.40', items[0]['body'])

    def test_version_text_attaches_changelog_only_when_emitting(self):
        state = self.monitor.default_state()
        source = {
            'id': 'grok_build_version',
            'name': 'Grok Build',
            'url': 'https://x.ai/cli/stable',
            'kind': 'version_text',
            'changelog_url': 'https://x.ai/cli/changelogs/{version}.external.md',
        }
        changelog = b'# 1.0.41 - 2026-09-21\n\n- Added Grok Build monitor.\n\n# 1.0.40 - 2026-09-20\n\n- Older notes.\n'

        def fetch(url, **_):
            if url == 'https://x.ai/cli/changelogs/1.0.41.external.md':
                return changelog
            raise self.monitor.FetchError(f'HTTP 404 from {url}')

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            first = self.monitor.collect_source_items(state, source, b'1.0.40\n', emit_existing=0)
            second = self.monitor.collect_source_items(state, source, b'1.0.41\n', emit_existing=0)
            third = self.monitor.collect_source_items(state, source, b'1.0.41\n', emit_existing=0)

        self.assertEqual(first, [])
        self.assertEqual([item['id'] for item in second], ['version:1.0.41'])
        self.assertIn('当前版本：1.0.41', second[0]['body'])
        self.assertNotIn('Added Grok Build monitor.', second[0]['body'])
        self.assertEqual([item['id'] for item in third], ['version:1.0.41:notes'])
        self.assertEqual(third[0]['title'], 'Grok Build 1.0.41 更新说明')
        self.assertIn('Added Grok Build monitor.', third[0]['body'])
        self.assertNotIn('Older notes.', third[0]['body'])

    def test_extract_version_changelog_keeps_subsection_notes(self):
        notes = self.monitor.extract_version_changelog(
            '# 1.0.41 - 2026-09-22\n\n## Features\n\n- Added sports search.\n\n# 1.0.40 - 2026-09-20\n\n## Features\n\n- Older notes.\n',
            '1.0.41',
        )

        self.assertIn('## Features', notes)
        self.assertIn('Added sports search.', notes)
        self.assertNotIn('Older notes.', notes)
        self.assertFalse(self.monitor.changelog_has_notes('# 1.0.41 - 2026-09-22'))
        self.assertTrue(self.monitor.changelog_has_notes(notes))

    def test_version_text_holds_heading_only_changelog_until_notes_arrive(self):
        state = self.monitor.default_state()
        source = {
            'id': 'grok_build_version',
            'name': 'Grok Build',
            'url': 'https://x.ai/cli/stable',
            'kind': 'version_text',
            'changelog_url': 'https://x.ai/cli/changelogs/{version}.external.md',
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        current['seen_ids'] = ['version:1.0.40']
        heading_only = b'# 1.0.41 - 2026-09-22\n'
        full = b'# 1.0.41 - 2026-09-22\n\n## Features\n\n- Added sports search.\n'

        def fetch(url, **_):
            if url != 'https://x.ai/cli/changelogs/1.0.41.external.md':
                raise self.monitor.FetchError(f'HTTP 404 from {url}')
            fetch.calls += 1
            return heading_only if fetch.calls == 1 else full

        fetch.calls = 0

        with mock.patch.object(self.monitor.time, 'time', side_effect=[1_000, 1_000]), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            announced = self.monitor.collect_source_items(state, source, b'1.0.41\n', emit_existing=0)

        self.assertEqual([item['id'] for item in announced], ['version:1.0.41'])
        self.assertNotIn('Added sports search.', announced[0]['body'])
        self.assertIn('version:1.0.41', current['seen_ids'])
        self.assertEqual(fetch.calls, 0)
        self.assertEqual(current['pending_version_changelogs']['version:1.0.41']['status'], 'changelog_pending')

        with mock.patch.object(self.monitor.time, 'time', side_effect=[1_000, 1_000]), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            waiting = self.monitor.collect_source_items(state, source, b'1.0.41\n', emit_existing=0)

        self.assertEqual(waiting, [])
        self.assertEqual(fetch.calls, 1)
        self.assertGreater(current['pending_version_changelogs']['version:1.0.41']['next_check_after'], 1_000)

        with mock.patch.object(self.monitor.time, 'time', side_effect=[1_300, 1_300]), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            released = self.monitor.collect_source_items(state, source, b'1.0.41\n', emit_existing=0)

        self.assertEqual([item['id'] for item in released], ['version:1.0.41:notes'])
        self.assertIn('Added sports search.', released[0]['body'])
        self.assertNotIn('version:1.0.41', current.get('pending_version_changelogs', {}))
        self.assertIn('version:1.0.41:notes', current['seen_ids'])
        self.assertIn('version:1.0.41', current['seen_ids'])

    def test_version_json_second_run_emits_when_version_changes(self):
        state = self.monitor.default_state()
        source = {
            'id': 'design_director_version',
            'name': 'Design Director Plugin Version',
            'url': 'https://raw.githubusercontent.com/lich13/codex-design-director-plugin/main/plugins/design-director/.codex-plugin/plugin.json',
            'kind': 'version_json',
        }
        old_items = self.monitor.parse_source_items(source, VERSION_JSON_FIXTURE)
        self.monitor.select_new_items(state, source, old_items, emit_existing=0)
        new_items = self.monitor.parse_source_items(source, b'{"version":"0.1.8","description":"Update references."}')

        selected = self.monitor.select_new_items(state, source, new_items, emit_existing=0)

        self.assertEqual([item['id'] for item in selected], ['version:0.1.8'])

    def test_builtin_sources_remove_plugin_commit_feeds(self):
        source_ids = {source['id'] for source in self.monitor.SOURCES}

        self.assertNotIn('spec_driven_develop_commits', source_ids)
        self.assertNotIn('spec_driven_develop_releases', source_ids)
        self.assertNotIn('spec_driven_develop_version', source_ids)
        self.assertNotIn('cc_switch_releases', source_ids)
        self.assertNotIn('design_director_commits', source_ids)
        self.assertNotIn('grok_search_rs_commits', source_ids)
        self.assertIn('grok_build_version', source_ids)

    def test_version_json_caches_commits_without_emitting_when_version_unchanged(self):
        state = self.monitor.default_state()
        source = {
            'id': 'design_director_version',
            'name': 'Design Director Plugin Version',
            'url': 'https://raw.githubusercontent.com/example/plugin/main/plugin.json',
            'kind': 'version_json',
            'commits_url': 'https://github.com/example/plugin/commits/main.atom',
        }
        with mock.patch.object(self.monitor, 'fetch_url', return_value=COMMIT_ATOM_FIXTURE):
            old_items = self.monitor.collect_source_items(state, source, VERSION_JSON_FIXTURE, emit_existing=0)
            new_items = self.monitor.collect_source_items(state, source, VERSION_JSON_FIXTURE, emit_existing=0)

        current = state['sources']['design_director_version']
        self.assertEqual(old_items, [])
        self.assertEqual(new_items, [])
        self.assertEqual([commit['id'] for commit in current['pending_commits']], [
            'tag:github.com,2008:Grit::Commit/abc123',
            'tag:github.com,2008:Grit::Commit/def456',
        ])

    def test_version_json_includes_cached_commits_when_version_changes(self):
        state = self.monitor.default_state()
        source = {
            'id': 'design_director_version',
            'name': 'Design Director Plugin Version',
            'url': 'https://raw.githubusercontent.com/example/plugin/main/plugin.json',
            'kind': 'version_json',
            'commits_url': 'https://github.com/example/plugin/commits/main.atom',
        }
        with mock.patch.object(self.monitor, 'fetch_url', return_value=COMMIT_ATOM_FIXTURE):
            self.monitor.collect_source_items(state, source, VERSION_JSON_FIXTURE, emit_existing=0)
            emitted = self.monitor.collect_source_items(state, source, b'{"version":"0.1.8","description":"Update references."}', emit_existing=0)

        self.assertEqual([item['id'] for item in emitted], ['version:0.1.8'])
        self.assertIn('本次版本更新包含的 commits：', emitted[0]['body'])
        self.assertIn('Update plugin manifest version', emitted[0]['body'])
        self.assertEqual(state['sources']['design_director_version']['pending_commits'], [])

    def test_baseline_records_seen_items_without_emitting(self):
        state = self.monitor.default_state()
        source = {'id': 'openai_codex_changelog', 'name': 'OpenAI Codex', 'kind': 'rss'}
        items = self.monitor.parse_feed(RSS_FIXTURE, source_id=source['id'])

        new_items = self.monitor.select_new_items(state, source, items, emit_existing=0)

        self.assertEqual(new_items, [])
        self.assertIn(items[0]['id'], state['sources'][source['id']]['seen_ids'])

    def test_second_run_emits_unseen_items(self):
        state = self.monitor.default_state()
        source = {'id': 'design_director_releases', 'name': 'Design Director', 'kind': 'atom'}
        old_items = [dict(self.monitor.parse_feed(ATOM_FIXTURE, source_id=source['id'])[0])]
        self.monitor.select_new_items(state, source, old_items, emit_existing=0)
        new_item = dict(old_items[0])
        new_item['id'] = 'tag:github.com,2008:Repository/1/v0.1.8'
        new_item['title'] = 'Design Director v0.1.8'

        new_items = self.monitor.select_new_items(state, source, [new_item, old_items[0]], emit_existing=0)

        self.assertEqual([item['id'] for item in new_items], [new_item['id']])
        self.assertIn(new_item['id'], state['sources'][source['id']]['seen_ids'])

    def test_script_does_not_depend_on_github_rest_api_or_token(self):
        script_text = Path(self.monitor.__file__).read_text(encoding='utf-8')

        self.assertNotIn('api.github.com', script_text)
        self.assertNotIn('GITHUB_TOKEN', script_text)
        import inspect
        self.assertNotIn('Authorization', inspect.getsource(self.monitor.fetch_url))
        self.assertNotIn('actions/checkout', script_text)

    def test_github_release_tag_only_page_stays_pending_without_seen(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor, 'fetch_url', return_value=RELEASE_HTML_TAG_ONLY):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        self.assertNotIn(item['id'], current['seen_ids'])
        pending = current['pending_github_release_checks'][item['id']]
        self.assertEqual(pending['status'], 'not_release_page')

    def test_github_release_waits_for_settle_delay_without_fetching_html(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 900,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor.time, 'time', return_value=1779777915), \
                mock.patch.object(self.monitor, 'fetch_url') as fetch_mock:
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        fetch_mock.assert_not_called()
        self.assertEqual(current['pending_github_release_checks'][item['id']]['status'], 'settling')

    def test_github_release_requires_stable_fingerprint_before_emit(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 2,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor.time, 'time', side_effect=[1779779000, 1779779201]), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[
                    RELEASE_HTML_READY,
                    ASSETS_HTML_READY,
                    RELEASE_HTML_READY,
                    ASSETS_HTML_READY,
                ]):
            first = self.monitor.select_new_items(state, source, [dict(item)], emit_existing=0)
            second = self.monitor.select_new_items(state, source, [dict(item)], emit_existing=0)

        self.assertEqual(first, [])
        self.assertEqual([selected['id'] for selected in second], [item['id']])
        self.assertIn(item['id'], current['seen_ids'])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_strongly_gated_release_emits_without_stability_wait(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 2,
            'release_required_asset_patterns': [
                'checksums.txt',
                'sub2api_{version}_linux_amd64.tar.gz',
                'sub2api_{version}_linux_arm64.tar.gz',
            ],
            'release_required_content_patterns': [
                'docker pull weishaw/sub2api:{version}',
            ],
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual([item['id'] for item in selected], [item['id']])
        self.assertIn(item['id'], current['seen_ids'])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_grok_build_builtin_uses_stable_channel_pointer(self):
        source = next(source for source in self.monitor.load_sources() if source['id'] == 'grok_build_version')

        self.assertEqual(source['kind'], 'version_text')
        self.assertEqual(source['url'], 'https://x.ai/cli/stable')
        self.assertEqual(source['changelog_url'], 'https://x.ai/cli/changelogs/{version}.external.md')
        self.assertNotIn('cc_switch', {project['id'] for project in self.monitor.list_source_projects()})
        self.assertNotIn('spec_driven_develop', {project['id'] for project in self.monitor.list_source_projects()})

    def test_prerelease_release_page_stays_pending_without_seen(self):
        state = self.monitor.default_state()
        source = {
            'id': 'cc_switch_releases',
            'name': 'CC Switch Releases',
            'url': 'https://github.com/farion1231/cc-switch/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
            'release_exclude_prerelease': True,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(CC_SWITCH_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        item['body'] = 'CC Switch v3.16.3\n' + ('Complete release notes. ' * 200)

        with mock.patch.object(self.monitor.time, 'time', return_value=1781456000), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[CC_SWITCH_RELEASE_HTML_PRERELEASE, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        self.assertNotIn(item['id'], current['seen_ids'])
        pending = current['pending_github_release_checks'][item['id']]
        self.assertEqual(pending['status'], 'prerelease')

    def test_short_release_body_waits_until_body_is_complete_and_stable(self):
        state = self.monitor.default_state()
        source = {
            'id': 'cc_switch_releases',
            'name': 'CC Switch Releases',
            'url': 'https://github.com/farion1231/cc-switch/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 2,
            'release_min_body_chars': 2000,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        short_item = self.monitor.parse_feed(CC_SWITCH_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        complete_item = dict(short_item)
        complete_item['body'] = 'CC Switch v3.16.3\n' + ('Detailed release notes. ' * 200)

        with mock.patch.object(self.monitor.time, 'time', side_effect=[1781456000, 1781456120, 1781456240]), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[
                    CC_SWITCH_RELEASE_HTML_READY,
                    ASSETS_HTML_READY,
                    CC_SWITCH_RELEASE_HTML_READY,
                    ASSETS_HTML_READY,
                    CC_SWITCH_RELEASE_HTML_READY,
                    ASSETS_HTML_READY,
                ]):
            first = self.monitor.select_new_items(state, source, [dict(short_item)], emit_existing=0)
            self.assertEqual(current['pending_github_release_checks'][short_item['id']]['status'], 'body_too_short')
            second = self.monitor.select_new_items(state, source, [dict(complete_item)], emit_existing=0)
            third = self.monitor.select_new_items(state, source, [dict(complete_item)], emit_existing=0)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual([item['id'] for item in third], [complete_item['id']])
        self.assertIn(complete_item['id'], current['seen_ids'])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_recoverable_short_body_pending_can_emit_after_more_than_one_day(self):
        state = self.monitor.default_state()
        source = {
            'id': 'cc_switch_releases',
            'name': 'CC Switch Releases',
            'url': 'https://github.com/farion1231/cc-switch/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
            'release_min_body_chars': 2000,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(CC_SWITCH_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        item['body'] = 'CC Switch v3.16.3\n' + ('Detailed release notes. ' * 200)
        current['pending_github_release_checks'] = {
            item['id']: {
                'status': 'body_too_short',
                'first_seen_at': '2026-06-14T16:52:21+00:00',
                'first_seen_epoch': 1781455941,
                'next_check_after': 0,
            },
        }

        with mock.patch.object(self.monitor.time, 'time', return_value=1781545942), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[CC_SWITCH_RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual([selected_item['id'] for selected_item in selected], [item['id']])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_validate_custom_sources_config_accepts_release_readiness_bool_and_min_body(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'custom_release_feed',
                    'name': 'Custom Release Feed',
                    'url': 'https://example.com/releases.atom',
                    'kind': 'atom',
                    'release_min_body_chars': 2000,
                    'release_exclude_prerelease': True,
                },
            ],
        })

        result = self.monitor.validate_custom_sources_config()
        sources = self.monitor.load_sources()
        custom = next(source for source in sources if source['id'] == 'custom_release_feed')

        self.assertTrue(result['ok'])
        self.assertEqual(custom['release_min_body_chars'], 2000)
        self.assertTrue(custom['release_exclude_prerelease'])

    def test_validate_custom_sources_config_rejects_bad_release_readiness_values(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'custom_release_feed',
                    'name': 'Custom Release Feed',
                    'url': 'https://example.com/releases.atom',
                    'kind': 'atom',
                    'release_min_body_chars': -1,
                    'release_exclude_prerelease': 'false',
                },
            ],
        })

        result = self.monitor.validate_custom_sources_config()

        self.assertFalse(result['ok'])
        self.assertTrue(any('release_min_body_chars' in error for error in result['errors']))
        self.assertTrue(any('release_exclude_prerelease' in error for error in result['errors']))

    def test_settling_pending_rechecks_after_config_removes_delay(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
            'release_required_asset_patterns': [
                'checksums.txt',
                'sub2api_{version}_linux_amd64.tar.gz',
                'sub2api_{version}_linux_arm64.tar.gz',
            ],
            'release_required_content_patterns': [
                'docker pull weishaw/sub2api:{version}',
            ],
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        current['pending_github_release_checks'] = {
            item['id']: {
                'status': 'settling',
                'first_seen_at': '2026-05-29T08:40:00+00:00',
                'first_seen_epoch': 1780044000,
                'next_check_after': 9999999999,
            },
        }

        with mock.patch.object(self.monitor.time, 'time', return_value=1780044100), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual([item['id'] for item in selected], [item['id']])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_settle_delay_uses_first_seen_not_later_feed_updated(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 900,
            'release_stable_checks': 1,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        current['pending_github_release_checks'] = {
            item['id']: {
                'status': 'settling',
                'first_seen_at': '2026-05-26T06:30:00+00:00',
                'first_seen_epoch': 1779777000,
                'next_check_after': 0,
            },
        }
        item['published_at'] = '2026-05-26T07:00:00Z'

        with mock.patch.object(self.monitor.time, 'time', return_value=1779778000), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual([item['id'] for item in selected], [item['id']])
        self.assertEqual(current['pending_github_release_checks'], {})

    def test_baseline_marks_unready_github_releases_seen_so_history_is_not_emitted_later(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_ifuryst_open_codex_computer_use_releases',
            'name': 'Open Codex Computer Use Releases',
            'url': 'https://github.com/iFurySt/open-codex-computer-use/releases.atom',
            'kind': 'atom',
        }
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        current = self.monitor.source_state(state, source['id'])

        with mock.patch.object(self.monitor.time, 'time', return_value=1779777915), \
                mock.patch.object(self.monitor, 'fetch_url') as fetch_mock:
            first = self.monitor.select_new_items(state, source, [dict(item)], emit_existing=0)

        self.assertEqual(first, [])
        self.assertTrue(current['initialized'])
        self.assertIn(item['id'], current['seen_ids'])
        self.assertEqual(current.get('pending_github_release_checks') or {}, {})
        fetch_mock.assert_not_called()

        with mock.patch.object(self.monitor.time, 'time', return_value=1779777915 + 700), \
                mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            second = self.monitor.select_new_items(state, source, [dict(item)], emit_existing=0)

        self.assertEqual(second, [])
        self.assertIn(item['id'], current['seen_ids'])

    def test_baseline_marks_ready_github_release_seen_without_stability_wait(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 2,
        }
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        current = state['sources'][source['id']]
        self.assertEqual(selected, [])
        self.assertIn(item['id'], current['seen_ids'])
        self.assertFalse(current.get('pending_github_release_checks'))

    def test_sub2api_required_asset_patterns_keep_release_pending(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
            'release_required_asset_patterns': [
                'checksums.txt',
                'sub2api_{version}_linux_amd64.tar.gz',
                'sub2api_{version}_linux_arm64.tar.gz',
            ],
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_MISSING_ARM64]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        pending = current['pending_github_release_checks'][item['id']]
        self.assertEqual(pending['status'], 'missing_required_assets')
        self.assertIn('sub2api_0.1.131_linux_arm64.tar.gz', pending['missing'])

    def test_sub2api_required_content_patterns_keep_release_pending(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
            'release_required_content_patterns': [
                'docker pull weishaw/sub2api:{version}',
            ],
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_NO_DOCKER_FIXTURE, source_id=source['id'])[0]

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=[RELEASE_HTML_READY, ASSETS_HTML_READY]):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        pending = current['pending_github_release_checks'][item['id']]
        self.assertEqual(pending['status'], 'missing_required_content')
        self.assertIn('docker pull weishaw/sub2api:0.1.132', pending['missing'])

    def test_stale_pending_release_records_status_without_emit(self):
        state = self.monitor.default_state()
        source = {
            'id': 'github_wei_shaw_sub2api_releases',
            'name': 'sub2api Releases',
            'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
            'kind': 'atom',
            'release_ready_after_seconds': 0,
            'release_stable_checks': 1,
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        item = self.monitor.parse_feed(GITHUB_RELEASE_ATOM_FIXTURE, source_id=source['id'])[0]
        current['pending_github_release_checks'] = {
            item['id']: {
                'first_seen_at': '2026-05-25T00:00:00+00:00',
                'first_seen_epoch': 1779667200,
                'next_check_after': 0,
            },
        }

        with mock.patch.object(self.monitor.time, 'time', return_value=1779753601), \
                mock.patch.object(self.monitor, 'fetch_url', return_value=RELEASE_HTML_TAG_ONLY):
            selected = self.monitor.select_new_items(state, source, [item], emit_existing=0)

        self.assertEqual(selected, [])
        pending = current['pending_github_release_checks'][item['id']]
        self.assertEqual(pending['status'], 'stale_pending_release')
        self.assertIn('stale_pending_release', current['last_error'])

    def test_deepl_payload_matches_web_jsonrpc_shape(self):
        payload, body = self.monitor.build_deepl_payload('Hello friend', source_lang='EN', target_lang='ZH-HANS', request_id=100002000, now_ms=1676555144560)

        self.assertEqual(payload['jsonrpc'], '2.0')
        self.assertEqual(payload['method'], 'LMT_handle_texts')
        self.assertEqual(payload['params']['lang']['source_lang_user_selected'], 'EN')
        self.assertEqual(payload['params']['lang']['target_lang'], 'ZH')
        self.assertEqual(payload['params']['commonJobParams']['regionalVariant'], 'ZH-HANS')
        self.assertEqual(payload['params']['texts'][0]['text'], 'Hello friend')
        self.assertEqual(payload['params']['timestamp'], 1676555144562)
        self.assertIn('"method" : "LMT_handle_texts"', body)

    def test_translation_cache_avoids_duplicate_deepl_calls(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        with mock.patch.object(translator, '_request_translation', return_value='你好') as request:
            self.assertEqual(translator.translate('Hello'), '你好')
            self.assertEqual(translator.translate('Hello'), '你好')

        self.assertEqual(request.call_count, 1)
        saved = json.loads(self.monitor.TRANSLATION_CACHE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(len(saved), 1)

    def test_format_alert_uses_translated_summary_and_link(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)

        def polish(**_kwargs):
            return {'summary': '- 修复了进度显示。', 'translation': 'ZH body'}

        with mock.patch.object(translator, 'translate', side_effect=lambda text, **_: f'ZH:{text[:40]}'), \
                mock.patch.object(self.monitor, 'polish_release_notes', side_effect=polish):
            item = self.monitor.parse_feed(RSS_FIXTURE, source_id='openai_codex_changelog')[0]
            alert = self.monitor.format_alerts(
                [{'source': {'id': 'openai_codex_changelog', 'name': 'OpenAI Codex Changelog'}, 'item': item}],
                translator=translator,
            )

        self.assertIn('## OpenAI Codex Changelog — ZH:Codex CLI Release: 0.133.0', alert)
        self.assertIn('https://developers.openai.com/codex/changelog/#github-release-327109656', alert)
        self.assertIn('中文摘要', alert)
        self.assertIn('- 修复了进度显示。', alert)
        self.assertIn('更新说明：', alert)
        self.assertIn('ZH body', alert)

    def test_format_alerts_translation_error_falls_back_to_original_summary(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        with mock.patch.object(translator, 'translate', side_effect=self.monitor.TranslationError('boom')):
            item = self.monitor.parse_feed(RSS_FIXTURE, source_id='openai_codex_changelog')[0]
            alert = self.monitor.format_alerts(
                [{'source': {'id': 'openai_codex_changelog', 'name': 'OpenAI Codex Changelog'}, 'item': item}],
                translator=translator,
            )

        self.assertIn('## OpenAI Codex Changelog — Codex CLI Release: 0.133.0', alert)
        self.assertIn('Added appshots support.', alert)
        self.assertIn('Fixed goal mode progress rendering.', alert)
        self.assertIn('翻译暂不可用或不完整，已保留原文摘要以保证时效。', alert)

    def test_translation_backoff_skips_deepl_and_uses_original_summary(self):
        translator = self.monitor.DeepLWebTranslator(
            cache_path=self.monitor.TRANSLATION_CACHE_FILE,
            translation_state={'backoff_until': 9999999999, 'consecutive_failures': 1},
            now_func=lambda: 1,
        )

        text, ok = self.monitor.translate_text_safe(translator, 'Hello world')

        self.assertFalse(ok)
        self.assertEqual(text, 'Hello world')

    def test_readable_translation_preserves_literals_and_strips_bold(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        seen = []

        def translate(text, **_kwargs):
            seen.append(text)
            return text.replace('Fixed the button', '修复了按钮')

        with mock.patch.object(translator, 'translate', side_effect=translate):
            rendered, ok = self.monitor.translate_readable_document(
                translator,
                '- **Fixed the button** so `Ctrl+P` opens the palette in 1.0.41.',
            )

        self.assertTrue(ok)
        self.assertEqual(len(seen), 1)
        self.assertNotIn('**', seen[0])
        self.assertIn('__PH0__', seen[0])
        self.assertIn('- 修复了按钮', rendered)
        self.assertIn('`Ctrl+P`', rendered)
        self.assertIn('1.0.41', rendered)

    def test_deepl_request_retries_three_times_then_succeeds(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        attempts = {'count': 0}

        def once(text, **_kwargs):
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise self.monitor.TranslationError('DeepL Web HTTP 429')
            return '你好'

        with mock.patch.object(translator, '_request_translation_once', side_effect=once), \
                mock.patch.object(self.monitor.time, 'sleep'):
            translated = translator._request_translation('Hello', source_lang='EN', target_lang='ZH-HANS')

        self.assertEqual(translated, '你好')
        self.assertEqual(attempts['count'], 3)

    def grok_response(self, **values):
        payload = {'summary': '- 修复了按钮。', 'translation': '修复了按钮。'} | values
        return {'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': json.dumps(payload, ensure_ascii=False)}]}]}

    def test_grok_polish_retries_three_times(self):
        request = mock.Mock(side_effect=self.monitor.TranslationError('HTTP 503'))
        with self.assertRaises(self.monitor.TranslationError):
            self.monitor.polish_release_notes(source_name='Grok Build', title='1.0.41',
                original='Fixed the button.', translated='修复了按钮。', request=request, sleep=lambda _: None)
        self.assertEqual(request.call_count, 3)

    def test_grok_polish_preserves_structured_output_and_disables_tools_and_storage(self):
        import httpx
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            self.assertEqual(str(request.url), 'https://example.com/v1/responses')
            event = {'type': 'response.completed', 'response': self.grok_response()}
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n',
                                  headers={'content-type': 'text/event-stream'})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(self.monitor, 'read_env', return_value={
                'GROK_MODELS_BASE_URL': 'https://example.com/v1', 'XAI_API_KEY': 'test-key'}), \
             mock.patch.object(self.monitor.httpx, 'Client', return_value=client), \
             mock.patch.dict(self.monitor.os.environ, {}, clear=True):
            result = self.monitor.polish_release_notes(source_name='Grok Build', title='1.0.41',
                original='Fixed the button.', translated='修复了按钮。')
        self.assertEqual(result['summary'], '- 修复了按钮。')
        self.assertEqual(seen[0]['model'], 'grok-4.7')
        self.assertEqual(seen[0]['reasoning'], {'effort': 'xhigh'})
        self.assertEqual(seen[0]['tools'], [])
        self.assertFalse(seen[0]['store'])
        self.assertTrue(seen[0]['stream'])
        self.assertTrue(seen[0]['text']['format']['strict'])

    def test_grok_polish_prompt_limits_summary_and_keeps_terms(self):
        prompt = self.monitor.build_grok_polish_prompt('Grok Build', '1.0.41', 'Fixed `Ctrl+P`.', '修复了。')

        self.assertIn('最多 6 条', prompt)
        self.assertIn('只有一条变更时只写一条', prompt)
        self.assertIn('不要把来源标题或单独的版本号再写成一行', prompt)
        self.assertIn('命令面板', prompt)
        self.assertIn('`Ctrl+P`', prompt)

    def test_release_notes_for_polish_drops_commit_dump(self):
        notes = self.monitor.release_notes_for_polish(
            'New Features\nAdded a button.\n\nFull Changelog: a...b\n#47063 Do a thing\n'
        )

        self.assertEqual(notes, 'New Features\nAdded a button.')

    def test_grok_polish_omits_commit_dump(self):
        request = mock.Mock(return_value=self.grok_response())
        self.monitor.polish_release_notes(source_name='OpenAI Codex CLI Releases', title='0.157.0',
            original='New Features\nAdded a button.\n\nFull Changelog: a...b\n#47063 Do a thing',
            translated='新功能\n添加了按钮。\n\nFull Changelog: a...b\n#47063 Do a thing', request=request)
        prompt = request.call_args.args[0]
        self.assertIn('Added a button.', prompt)
        self.assertNotIn('Full Changelog', prompt)
        self.assertNotIn('#47063', prompt)

    def test_grok_polish_rejects_incomplete_or_empty_outputs(self):
        for response in [{'status': 'incomplete', 'output': []}, self.grok_response(translation=''),
                         self.grok_response(summary=['not a string']),
                         {'status': 'completed', 'output': None},
                         {'status': 'completed', 'output': [{'type': 'message', 'content': None}]}]:
            with self.subTest(response=response), self.assertRaises(self.monitor.TranslationError):
                self.monitor.parse_grok_polish_output(response)

    def test_chinese_majority_text_skips_translation_models(self):
        self.assertTrue(self.monitor.chinese_text_is_majority('修复了若干问题，改进了启动速度。'))
        self.assertTrue(self.monitor.chinese_text_is_majority(
            '修复了 Windows 下的代理问题，支持 Clash Meta 和 TUN 模式。'
        ))
        self.assertTrue(self.monitor.chinese_text_is_majority(
            '本次更新主要修复了连接问题，并改进了启动速度。另外 Fixed a rare crash。'
        ))
        self.assertFalse(self.monitor.chinese_text_is_majority(
            'Fixed the button so the palette opens. 同时修复了按钮。'
        ))
        self.assertFalse(self.monitor.chinese_text_is_majority('Added GPT-6 Sol and Luna.'))

    def test_chinese_update_skips_deepl_and_grok(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        event = {
            'source': {'name': 'chen08209/FlClash Releases'},
            'item': {
                'title': 'v0.8.98',
                'body': '修复了 Windows 下的代理问题，支持 Clash Meta 和 TUN 模式。\n改进了启动速度。',
                'url': 'https://github.com/chen08209/FlClash/releases/tag/v0.8.98',
                'published_at': '2026-09-25T00:00:00Z',
            },
        }

        with mock.patch.object(translator, 'translate', side_effect=AssertionError('deepl')), \
                mock.patch.object(self.monitor, 'polish_release_notes', side_effect=AssertionError('grok')):
            alert = self.monitor.format_event(event, translator=translator)

        self.assertIn('## chen08209/FlClash Releases — v0.8.98', alert)
        self.assertIn('修复了 Windows 下的代理问题', alert)
        self.assertIn('改进了启动速度。', alert)
        self.assertNotIn('翻译暂不可用或不完整', alert)
        self.assertNotIn('摘要优化暂不可用', alert)

    def test_format_event_survives_polish_failure(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        event = {
            'source': {'name': 'OpenAI Codex CLI Releases'},
            'item': {
                'title': '0.157.0',
                'body': 'New Features\nAdded a button.',
                'url': 'https://example.com/codex/0.157.0',
                'published_at': '2026-09-25T02:31:06Z',
            },
        }

        with mock.patch.object(translator, 'translate', side_effect=lambda text, **_: f'ZH:{text}'), \
                mock.patch.object(self.monitor, 'polish_release_notes', side_effect=self.monitor.TranslationError('timed out')):
            alert = self.monitor.format_event(event, translator=translator)

        self.assertIn('## OpenAI Codex CLI Releases — ZH:0.157.0', alert)
        self.assertIn('https://example.com/codex/0.157.0', alert)
        self.assertIn('ZH:Added a button.', alert)
        self.assertIn('摘要优化暂不可用，已保留分条翻译。', alert)

    def test_successful_polish_hides_partial_translation_warning(self):
        translator = self.monitor.DeepLWebTranslator(cache_path=self.monitor.TRANSLATION_CACHE_FILE)
        event = {
            'source': {'name': 'OpenAI Codex CLI Releases'},
            'item': {
                'title': '0.157.0',
                'body': 'New Features\nAdded a button.\n\n#47063 Do a thing',
                'url': 'https://example.com/codex/0.157.0',
            },
        }

        with mock.patch.object(self.monitor, 'translate_readable_text', return_value=('0.157.0', True)), \
                mock.patch.object(self.monitor, 'translate_readable_document', return_value=('英文残留', False)), \
                mock.patch.object(self.monitor, 'polish_release_notes', return_value={'summary': '- 新增了按钮。', 'translation': '新增了按钮。'}):
            alert = self.monitor.format_event(event, translator=translator)

        self.assertIn('- 新增了按钮。', alert)
        self.assertNotIn('翻译暂不可用或不完整', alert)

    def test_grok_timeout_stays_a_translation_error(self):
        import httpx
        def handler(request):
            raise httpx.ReadTimeout('timeout', request=request)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch.object(self.monitor, 'read_env', return_value={
                'GROK_MODELS_BASE_URL': 'https://example.com/v1', 'XAI_API_KEY': 'test-key'}), \
             mock.patch.object(self.monitor.httpx, 'Client', return_value=client), \
             mock.patch.dict(self.monitor.os.environ, {}, clear=True), \
             self.assertRaises(self.monitor.TranslationError):
            self.monitor.request_grok_polish('Fixed a button.')

    def test_load_sources_includes_custom_config_sources(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'custom_tool_releases',
                    'name': 'Custom Tool Releases',
                    'url': 'https://example.com/releases.atom',
                    'kind': 'atom',
                },
            ],
        })

        sources = self.monitor.load_sources()

        self.assertTrue(any(source['id'] == 'openai_codex_changelog' for source in sources))
        self.assertTrue(any(source['id'] == 'openai_codex_cli_releases' for source in sources))
        self.assertTrue(any(source['id'] == 'custom_tool_releases' for source in sources))

    def test_codex_github_release_source_filters_to_stable_cli_versions(self):
        source = next(source for source in self.monitor.load_sources() if source['id'] == 'openai_codex_cli_releases')
        state = {
            'sources': {
                source['id']: {
                    'initialized': True,
                    'seen_ids': [],
                },
            },
        }
        items = self.monitor.parse_source_items(source, CODEX_GITHUB_RELEASE_ATOM_FIXTURE)

        with mock.patch.object(self.monitor, 'fetch_url', return_value=CODEX_RELEASE_HTML_READY):
            selected = self.monitor.select_new_items(state, source, items)

        current = state['sources'][source['id']]
        self.assertEqual([item['title'] for item in selected], ['0.139.0'])
        self.assertEqual(current.get('pending_github_release_checks'), {})
        self.assertEqual(current['seen_ids'], [
            'tag:github.com,2008:Repository/965415649/rust-v0.139.0',
        ])

    def test_title_filtered_items_do_not_permanently_suppress_same_id_later(self):
        state = self.monitor.default_state()
        source = {
            'id': 'same_id_release_feed',
            'name': 'Same ID Release Feed',
            'kind': 'atom',
            'item_title_include_patterns': ['re:^\\d+\\.\\d+\\.\\d+$'],
        }
        current = self.monitor.source_state(state, source['id'])
        current['initialized'] = True
        noisy_item = {
            'source_id': source['id'],
            'id': 'tag:github.com,2008:Repository/965415649/rust-v0.139.0',
            'title': '0.139.0-alpha.1',
            'url': 'https://github.com/openai/codex/releases/tag/rust-v0.139.0-alpha.1',
            'body': 'Alpha release.',
            'published_at': '2026-06-08T14:50:10Z',
        }
        stable_item = dict(noisy_item)
        stable_item.update({
            'title': '0.139.0',
            'url': 'https://github.com/openai/codex/releases/tag/rust-v0.139.0',
            'body': 'Stable release.',
            'published_at': '2026-06-08T23:01:11Z',
        })

        skipped = self.monitor.select_new_items(state, source, [noisy_item])

        self.assertEqual(skipped, [])
        self.assertNotIn(noisy_item['id'], current['seen_ids'])

        selected = self.monitor.select_new_items(state, source, [stable_item])

        self.assertEqual([item['title'] for item in selected], ['0.139.0'])
        self.assertIn(stable_item['id'], current['seen_ids'])

    def test_codex_changelog_source_skips_cli_release_duplicates_after_github_alert(self):
        source = next(source for source in self.monitor.load_sources() if source['id'] == 'openai_codex_changelog')
        github_source = next(source for source in self.monitor.load_sources() if source['id'] == 'openai_codex_cli_releases')
        self.monitor.save_raw_item(github_source, {
            'source_id': github_source['id'],
            'id': 'tag:github.com,2008:Repository/965415649/rust-v0.138.0',
            'title': '0.138.0',
            'url': 'https://github.com/openai/codex/releases/tag/rust-v0.138.0',
            'body': 'Codex CLI stable release.',
            'published_at': '2026-06-04T00:00:00Z',
        })
        state = {
            'sources': {
                source['id']: {
                    'initialized': True,
                    'seen_ids': [],
                },
            },
        }
        items = self.monitor.parse_source_items(source, CODEX_CHANGELOG_MIXED_FIXTURE)

        selected = self.monitor.select_new_items(state, source, items)

        self.assertEqual([item['title'] for item in selected], ['Codex app updates'])
        self.assertEqual(state['sources'][source['id']]['seen_ids'], [
            'https://developers.openai.com/codex/changelog/#codex-2026-06-04-app',
        ])

    def test_codex_changelog_source_can_backfill_cli_release_when_github_missed_it(self):
        source = next(source for source in self.monitor.load_sources() if source['id'] == 'openai_codex_changelog')
        state = {
            'sources': {
                source['id']: {
                    'initialized': True,
                    'seen_ids': [],
                },
            },
        }
        items = self.monitor.parse_source_items(source, CODEX_CHANGELOG_MIXED_FIXTURE)

        selected = self.monitor.select_new_items(state, source, items)

        self.assertEqual([item['title'] for item in selected], [
            'Codex app updates',
            'Codex CLI Release: 0.138.0',
        ])
        self.assertEqual(state['sources'][source['id']]['seen_ids'], [
            'https://developers.openai.com/codex/changelog/#codex-2026-06-04-app',
            'https://developers.openai.com/codex/changelog/#github-release-334999999',
        ])

    def test_validate_config_accepts_empty_config_with_codex_github_release_source(self):
        with mock.patch('builtins.print') as print_mock:
            exit_code = self.monitor.main(['--validate-config', '--json'])

        payload = json.loads(print_mock.call_args.args[0])
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload['ok'])
        self.assertTrue(any(source['id'] == 'openai_codex_cli_releases' for source in self.monitor.load_sources()))

    def test_load_sources_excludes_disabled_builtin_source_ids(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'disabled_source_ids': ['openai_codex_changelog'],
            'sources': [],
        })

        source_ids = [source['id'] for source in self.monitor.load_sources()]

        self.assertNotIn('openai_codex_changelog', source_ids)
        self.assertNotIn('hermes_agent_releases', source_ids)

    def test_list_source_projects_groups_plugin_release_and_version_sources(self):
        projects = self.monitor.list_source_projects()

        design_director = next(project for project in projects if project['id'] == 'design_director')

        self.assertEqual(design_director['number'], 4)
        self.assertEqual(design_director['name'], 'Design Director')
        self.assertEqual(design_director['source_ids'], [
            'design_director_releases',
            'design_director_version',
        ])
        self.assertFalse(design_director['custom'])

    def test_remove_custom_project_deletes_all_matching_custom_sources(self):
        self.monitor.add_custom_sources([
            {
                'id': 'github_owner_example_tool_releases',
                'name': 'Example Tool Releases',
                'url': 'https://github.com/owner/example-tool/releases.atom',
                'kind': 'atom',
            },
            {
                'id': 'github_owner_example_tool_version',
                'name': 'Example Tool Plugin Version',
                'url': 'https://raw.githubusercontent.com/owner/example-tool/main/.codex-plugin/plugin.json',
                'kind': 'version_json',
                'version_path': 'version',
            },
        ])

        result = self.monitor.remove_source_project('github_owner_example_tool')
        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))

        self.assertTrue(result['ok'])
        self.assertEqual(result['removed_source_ids'], [
            'github_owner_example_tool_releases',
            'github_owner_example_tool_version',
        ])
        self.assertEqual(saved['sources'], [])

    def test_remove_builtin_project_persists_disabled_source_ids(self):
        result = self.monitor.remove_source_project('design_director')
        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))

        self.assertTrue(result['ok'])
        self.assertEqual(result['disabled_source_ids'], [
            'design_director_releases',
            'design_director_version',
        ])
        self.assertTrue(any(source['id'] == 'design_director_releases' for source in self.monitor.SOURCES))
        self.assertNotIn('design_director_releases', [source['id'] for source in self.monitor.load_sources()])

    def test_load_sources_preserves_release_readiness_config(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'github_wei_shaw_sub2api_releases',
                    'name': 'sub2api Releases',
                    'url': 'https://github.com/Wei-Shaw/sub2api/releases.atom',
                    'kind': 'atom',
                    'release_ready_after_seconds': 900,
                    'release_stable_checks': 2,
                    'release_required_asset_patterns': [
                        'checksums.txt',
                        'sub2api_{version}_linux_amd64.tar.gz',
                    ],
                    'release_required_content_patterns': [
                        'docker pull weishaw/sub2api:{version}',
                    ],
                },
            ],
        })

        source = next(source for source in self.monitor.load_sources() if source['id'] == 'github_wei_shaw_sub2api_releases')

        self.assertEqual(source['release_ready_after_seconds'], 900)
        self.assertEqual(source['release_stable_checks'], 2)
        self.assertEqual(source['release_required_asset_patterns'], [
            'checksums.txt',
            'sub2api_{version}_linux_amd64.tar.gz',
        ])
        self.assertEqual(source['release_required_content_patterns'], [
            'docker pull weishaw/sub2api:{version}',
        ])

    def test_load_sources_ignores_bad_custom_config_version(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 'not-a-number',
            'sources': [],
        })

        sources = self.monitor.load_sources()

        self.assertTrue(any(source['id'] == 'openai_codex_changelog' for source in sources))

    def test_add_github_repo_source_infers_release_and_commit_atom_urls(self):
        result = self.monitor.add_github_repo_source(
            'owner/example-tool',
            name='Example Tool',
            include_commits=True,
            branch='develop',
        )

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertEqual(result['added'], 2)
        self.assertEqual([source['id'] for source in saved['sources']], [
            'github_owner_example_tool_releases',
            'github_owner_example_tool_commits',
        ])
        self.assertEqual(saved['sources'][0]['url'], 'https://github.com/owner/example-tool/releases.atom')
        self.assertEqual(saved['sources'][1]['url'], 'https://github.com/owner/example-tool/commits/develop.atom')

    def test_add_monitor_url_source_adds_github_software_release_source(self):
        def fetch(url, **_):
            raise self.monitor.FetchError(f'HTTP 404 from {url}')

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            result = self.monitor.add_monitor_url_source('https://github.com/owner/example-tool', name='Example Tool')

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'github_software')
        self.assertEqual([source['id'] for source in saved['sources']], [
            'github_owner_example_tool_releases',
        ])
        self.assertEqual(saved['sources'][0]['url'], 'https://github.com/owner/example-tool/releases.atom')

    def test_add_monitor_url_source_adds_direct_atom_feed(self):
        result = self.monitor.add_monitor_url_source('https://example.com/releases.atom', name='Example Feed')

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'feed')
        self.assertEqual(saved['sources'][0]['id'], 'example_feed')
        self.assertEqual(saved['sources'][0]['kind'], 'atom')
        self.assertEqual(saved['sources'][0]['url'], 'https://example.com/releases.atom')

    def test_add_monitor_url_source_adds_codex_plugin_version_and_release_sources(self):
        manifest = b'{"name":"example-plugin","version":"0.1.0"}'

        def fetch(url, **_):
            if url == 'https://raw.githubusercontent.com/owner/example-plugin/main/.codex-plugin/plugin.json':
                return manifest
            raise self.monitor.FetchError(f'HTTP 404 from {url}')

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            result = self.monitor.add_monitor_url_source('owner/example-plugin', name='Example Plugin')

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'codex_plugin')
        self.assertEqual([source['id'] for source in saved['sources']], [
            'github_owner_example_plugin_releases',
            'github_owner_example_plugin_version',
        ])
        self.assertEqual(saved['sources'][1]['url'], 'https://raw.githubusercontent.com/owner/example-plugin/main/.codex-plugin/plugin.json')
        self.assertEqual(saved['sources'][1]['kind'], 'version_json')
        self.assertEqual(saved['sources'][1]['commits_url'], 'https://github.com/owner/example-plugin/commits/main.atom')

    def test_add_monitor_url_source_discovers_codex_plugin_under_plugins_directory(self):
        manifest = b'{"name":"nested-plugin","version":"0.2.0"}'
        tree_html = b'<a href="/owner/repo/tree/main/plugins/nested-plugin/.codex-plugin">.codex-plugin</a>'

        def fetch(url, **_):
            if url == 'https://github.com/owner/repo/tree/main/plugins':
                return tree_html
            if url == 'https://raw.githubusercontent.com/owner/repo/main/plugins/nested-plugin/.codex-plugin/plugin.json':
                return manifest
            raise self.monitor.FetchError(f'HTTP 404 from {url}')

        with mock.patch.object(self.monitor, 'fetch_url', side_effect=fetch):
            result = self.monitor.add_monitor_url_source('https://github.com/owner/repo', name='Nested Plugin')

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'codex_plugin')
        self.assertEqual(saved['sources'][1]['url'], 'https://raw.githubusercontent.com/owner/repo/main/plugins/nested-plugin/.codex-plugin/plugin.json')

    def test_add_monitor_url_source_rejects_unknown_url_without_mutating_config(self):
        result = self.monitor.add_monitor_url_source('https://example.com/product-page', name='Product Page')

        self.assertFalse(result['ok'])
        self.assertTrue(result['unsupported'])
        self.assertFalse(self.monitor.CUSTOM_SOURCES_FILE.exists())

    def test_add_monitor_url_source_maps_grok_build_github_to_official_channel(self):
        result = self.monitor.add_monitor_url_source('https://github.com/xai-org/grok-build', name='Grok Build')

        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'grok_build')
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['sources'][0]['id'], 'grok_build_version')
        self.assertEqual(result['sources'][0]['url'], 'https://x.ai/cli/stable')
        self.assertFalse(self.monitor.CUSTOM_SOURCES_FILE.exists())

    def test_add_monitor_url_source_maps_official_cli_stable_pointer(self):
        result = self.monitor.add_monitor_url_source('https://x.ai/cli/stable')

        self.assertTrue(result['ok'])
        self.assertEqual(result['classification'], 'grok_build')
        self.assertEqual(result['sources'][0]['kind'], 'version_text')
        self.assertFalse(self.monitor.CUSTOM_SOURCES_FILE.exists())

    def test_add_source_updates_existing_custom_source_by_id(self):
        self.monitor.add_custom_sources([
            {
                'id': 'custom_tool_releases',
                'name': 'Old Name',
                'url': 'https://example.com/old.atom',
                'kind': 'atom',
            },
        ])

        result = self.monitor.add_custom_sources([
            {
                'id': 'custom_tool_releases',
                'name': 'New Name',
                'url': 'https://example.com/new.atom',
                'kind': 'rss',
            },
        ])

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertEqual(result['updated'], 1)
        self.assertEqual(len(saved['sources']), 1)
        self.assertEqual(saved['sources'][0]['name'], 'New Name')
        self.assertEqual(saved['sources'][0]['url'], 'https://example.com/new.atom')
        self.assertEqual(saved['sources'][0]['kind'], 'rss')

    def test_add_source_treats_duplicate_url_as_existing(self):
        self.monitor.add_custom_sources([
            {
                'id': 'custom_tool_releases',
                'name': 'Custom Tool Releases',
                'url': 'https://example.com/releases.atom',
                'kind': 'atom',
            },
        ])

        result = self.monitor.add_custom_sources([
            {
                'id': 'same_url_other_id',
                'name': 'Same URL Other ID',
                'url': 'https://example.com/releases.atom',
                'kind': 'atom',
            },
        ])

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertEqual(result['existing'], 1)
        self.assertEqual(len(saved['sources']), 1)
        self.assertEqual(saved['sources'][0]['id'], 'custom_tool_releases')
        self.assertEqual(result['sources'], [])

    def test_add_source_treats_builtin_url_as_existing_without_saving(self):
        result = self.monitor.add_custom_sources([
            {
                'id': 'duplicate_openai',
                'name': 'Duplicate OpenAI',
                'url': 'https://developers.openai.com/codex/changelog/rss.xml',
                'kind': 'rss',
            },
        ])

        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['added'], 0)
        self.assertEqual(saved['sources'], [])
        self.assertEqual(result['sources'], [])

    def test_baseline_sources_records_current_items_without_alerting(self):
        source = {
            'id': 'custom_tool_releases',
            'name': 'Custom Tool Releases',
            'url': 'https://example.com/releases.atom',
            'kind': 'atom',
        }

        with mock.patch.object(self.monitor, 'fetch_url', return_value=ATOM_FIXTURE):
            result = self.monitor.baseline_sources([source])

        state = json.loads(self.monitor.STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual(result['baselined'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(state['sources']['custom_tool_releases']['initialized'])
        self.assertIn('tag:github.com,2008:Repository/1/v0.1.7', state['sources']['custom_tool_releases']['seen_ids'])

    def test_cli_list_sources_prints_custom_sources(self):
        self.monitor.add_custom_sources([
            {
                'id': 'custom_tool_releases',
                'name': 'Custom Tool Releases',
                'url': 'https://example.com/releases.atom',
                'kind': 'atom',
            },
        ])

        with mock.patch('builtins.print') as print_mock:
            exit_code = self.monitor.main(['--list-sources'])

        self.assertEqual(exit_code, 0)
        printed = '\n'.join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn('Custom Tool Releases', printed)
        self.assertIn('custom_tool_releases', printed)

    def test_cli_list_projects_prints_stable_json(self):
        with mock.patch('builtins.print') as print_mock:
            exit_code = self.monitor.main(['--list-projects', '--json'])

        self.assertEqual(exit_code, 0)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertEqual(payload['projects'][0]['number'], 1)
        self.assertEqual(payload['projects'][0]['id'], 'openai_codex_changelog')
        self.assertTrue(any(project['id'] == 'design_director' for project in payload['projects']))

    def test_cli_remove_project_by_number_disables_builtin_project(self):
        with mock.patch('builtins.print') as print_mock:
            exit_code = self.monitor.main(['--remove-project', 'design_director', '--json'])

        self.assertEqual(exit_code, 0)
        payload = json.loads(print_mock.call_args.args[0])
        saved = json.loads(self.monitor.CUSTOM_SOURCES_FILE.read_text(encoding='utf-8'))
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['project']['id'], 'design_director')
        self.assertEqual(saved['disabled_source_ids'], [
            'design_director_releases',
            'design_director_version',
        ])

    def test_cli_add_source_can_use_alternate_config_without_baseline(self):
        alternate_config = self.root / 'tmp' / 'sources.json'

        with mock.patch('builtins.print'):
            exit_code = self.monitor.main([
                '--config', str(alternate_config),
                '--add-source-url', 'https://example.com/feed.xml',
                '--add-source-name', 'Example Feed',
                '--source-kind', 'rss',
                '--no-baseline',
                '--json',
            ])

        self.assertEqual(exit_code, 0)
        saved = json.loads(alternate_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['sources'][0]['id'], 'example_feed')
        self.assertEqual(saved['sources'][0]['url'], 'https://example.com/feed.xml')
        self.assertFalse(self.monitor.CUSTOM_SOURCES_FILE.exists())

    def test_cli_add_version_json_source_preserves_version_path(self):
        alternate_config = self.root / 'tmp' / 'versions.json'

        with mock.patch('builtins.print'):
            exit_code = self.monitor.main([
                '--config', str(alternate_config),
                '--add-source-url', 'https://example.com/plugin.json',
                '--add-source-name', 'Example Plugin Version',
                '--source-kind', 'version_json',
                '--version-path', 'plugin.version',
                '--no-baseline',
                '--json',
            ])

        self.assertEqual(exit_code, 0)
        saved = json.loads(alternate_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['sources'][0]['kind'], 'version_json')
        self.assertEqual(saved['sources'][0]['version_path'], 'plugin.version')

    def test_cli_add_version_text_source_preserves_changelog_url(self):
        alternate_config = self.root / 'tmp' / 'version-text.json'

        with mock.patch('builtins.print'):
            exit_code = self.monitor.main([
                '--config', str(alternate_config),
                '--add-source-url', 'https://example.com/stable',
                '--add-source-name', 'Example CLI',
                '--source-kind', 'version_text',
                '--changelog-url', 'https://example.com/changelogs/{version}.md',
                '--no-baseline',
                '--json',
            ])

        self.assertEqual(exit_code, 0)
        saved = json.loads(alternate_config.read_text(encoding='utf-8'))
        self.assertEqual(saved['sources'][0]['kind'], 'version_text')
        self.assertEqual(saved['sources'][0]['changelog_url'], 'https://example.com/changelogs/{version}.md')

    def test_prune_inactive_source_runtime_drops_abandoned_state_and_items(self):
        state = self.monitor.default_state()
        state['sources'] = {
            'cc_switch_releases': {'initialized': True, 'seen_ids': ['old']},
            'grok_build_version': {'initialized': True, 'seen_ids': ['version:1.0.40']},
        }
        self.monitor.ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        abandoned = {
            'source': {'id': 'cc_switch_releases'},
            'item': {'source_id': 'cc_switch_releases', 'id': 'old'},
        }
        kept = {
            'source': {'id': 'grok_build_version'},
            'item': {'source_id': 'grok_build_version', 'id': 'version:1.0.40'},
        }
        state['pending_events'] = [abandoned, kept]
        abandoned_path = self.monitor.ITEMS_DIR / 'abandoned.json'
        kept_path = self.monitor.ITEMS_DIR / 'kept.json'
        abandoned_path.write_text(json.dumps(abandoned), encoding='utf-8')
        kept_path.write_text(json.dumps(kept), encoding='utf-8')

        pruned = self.monitor.prune_inactive_source_runtime(state, active_ids={'grok_build_version'})

        self.assertEqual(pruned['removed_state'], 1)
        self.assertEqual(pruned['removed_items'], 1)
        self.assertEqual(list(state['sources']), ['grok_build_version'])
        self.assertEqual(state['pending_events'], [kept])
        self.assertFalse(abandoned_path.exists())
        self.assertTrue(kept_path.exists())

    def test_validate_custom_sources_config_reports_duplicate_urls(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'first',
                    'name': 'First',
                    'url': 'https://example.com/feed.atom',
                    'kind': 'atom',
                },
                {
                    'id': 'second',
                    'name': 'Second',
                    'url': 'https://example.com/feed.atom',
                    'kind': 'atom',
                },
            ],
        })

        result = self.monitor.validate_custom_sources_config()

        self.assertFalse(result['ok'])
        self.assertTrue(any('duplicate source url' in error for error in result['errors']))

    def test_cli_validate_config_returns_nonzero_for_invalid_config(self):
        self.monitor.save_json(self.monitor.CUSTOM_SOURCES_FILE, {
            'version': 1,
            'sources': [
                {
                    'id': 'missing_url',
                    'name': 'Missing URL',
                    'kind': 'atom',
                },
            ],
        })

        with mock.patch('builtins.print'):
            exit_code = self.monitor.main(['--validate-config', '--json'])

        self.assertEqual(exit_code, 1)


    def test_more_than_one_batch_of_updates_is_not_marked_seen_and_lost(self):
        source = {'id': 'overflow', 'name': 'Overflow', 'kind': 'rss', 'url': 'https://example.com/feed'}
        state = {'sources': {'overflow': {'initialized': True, 'seen_ids': []}}}
        items = [{'id': str(i), 'title': 'Release '+str(i), 'url': 'https://example.com/'+str(i)} for i in range(12)]
        selected = self.monitor.select_new_items(state, source, items)
        self.assertEqual(len(selected), 12)
        self.assertEqual(set(state['sources']['overflow']['seen_ids']), {str(i) for i in range(12)})

    def test_outbox_failure_does_not_advance_software_notification_state(self):
        source = {'id': 'fixture', 'name': 'Fixture', 'kind': 'rss', 'url': 'https://example.com/feed'}
        state = {'sources': {}, 'pending_events': [{'source': source, 'item': {'id': str(i)}} for i in range(9)]}
        with mock.patch.object(self.monitor, 'load_state', return_value=state), \
             mock.patch.object(self.monitor, 'load_sources', return_value=[source]), \
             mock.patch.object(self.monitor, 'prune_inactive_source_runtime'), \
             mock.patch.object(self.monitor, 'format_alerts', return_value='notification'), \
             mock.patch.object(self.monitor.notification_events, 'publish', side_effect=OSError('disk full')), \
             mock.patch.object(self.monitor, 'save_state') as save:
            with self.assertRaises(OSError):
                self.monitor.run_monitor()
            save.assert_not_called()
        self.assertEqual(len(state['pending_events']), 9)

    def test_short_source_failures_stay_silent_until_three_consecutive_polls(self):
        source = {
            'id': 'pi_releases', 'name': 'Pi Releases',
            'kind': 'atom', 'url': 'https://pi.dev/changelog.xml',
            'poll_interval_seconds': 300,
        }
        state = self.monitor.default_state()
        error = self.monitor.FetchError('network error from https://pi.dev/changelog.xml: timed out')

        with mock.patch.object(self.monitor, 'fetch_source', side_effect=error):
            first, _ = self.monitor._poll_sources_for_enqueue(state, [source], [], now=0)
            second, _ = self.monitor._poll_sources_for_enqueue(state, [source], [], now=100)
            third, _ = self.monitor._poll_sources_for_enqueue(state, [source], [], now=300)

        current = state['sources']['pi_releases']
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(len(third), 1)
        self.assertIn('Pi Releases', third[0])
        self.assertEqual(current['consecutive_failures'], 3)
        self.assertTrue(current['failure_alerted'])

        with mock.patch.object(self.monitor, 'fetch_source', return_value=(RSS_FIXTURE, {})):
            failures, recoveries = self.monitor._poll_sources_for_enqueue(state, [source], [], now=600)

        self.assertEqual(failures, [])
        self.assertEqual(recoveries, ['Pi Releases'])
        self.assertEqual(current['consecutive_failures'], 0)
        self.assertFalse(current['failure_alerted'])

    def test_transient_failure_recovery_does_not_emit_recovery_notification(self):
        current = self.monitor.source_state(self.monitor.default_state(), 'fixture')
        self.assertFalse(self.monitor.mark_source_failure(current, 'timeout', 0))
        self.assertFalse(self.monitor.mark_source_success(current, 100))

    def test_run_monitor_does_not_write_failure_result_before_alert_threshold(self):
        state = self.monitor.default_state()
        source = {'id': 'pi_releases', 'name': 'Pi Releases', 'kind': 'atom', 'url': 'https://pi.dev/changelog.xml'}
        with mock.patch.object(self.monitor, 'load_state', return_value=state), \
             mock.patch.object(self.monitor, 'load_sources', return_value=[source]), \
             mock.patch.object(self.monitor, 'prune_inactive_source_runtime'), \
             mock.patch.object(self.monitor, '_poll_sources_for_enqueue', return_value=([], [])), \
             mock.patch.object(self.monitor, 'save_state'), \
             mock.patch.object(self.monitor.notification_events, 'result') as result:
            self.monitor.run_monitor(enqueue_only=True)
        result.assert_not_called()

        with mock.patch.object(self.monitor, 'load_state', return_value=state), \
             mock.patch.object(self.monitor, 'load_sources', return_value=[source]), \
             mock.patch.object(self.monitor, 'prune_inactive_source_runtime'), \
             mock.patch.object(self.monitor, '_poll_sources_for_enqueue', return_value=(['Pi Releases（连续失败 3 次）'], [])), \
             mock.patch.object(self.monitor, 'save_state'), \
             mock.patch.object(self.monitor.notification_events, 'result') as result:
            self.monitor.run_monitor(enqueue_only=True)
        result.assert_called_once()
        self.assertEqual(result.call_args.args[0], 'failed')

if __name__ == '__main__':
    unittest.main()
