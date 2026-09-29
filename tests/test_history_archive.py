import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pylon_archive_history', Path(__file__).parents[1] / 'ops/archive_history.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


def history(root, sessions):
    root.mkdir()
    (root / 'sessions').mkdir()
    with sqlite3.connect(root / 'state.db') as db:
        db.executescript('''
        CREATE TABLE schema_version(version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES(1);
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, system_prompt_hash TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT REFERENCES sessions(id), content TEXT);
        CREATE TABLE system_prompts(hash TEXT PRIMARY KEY, prompt TEXT);
        CREATE TABLE session_model_usage(session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT, task TEXT);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content);
        CREATE INDEX messages_by_session ON messages(session_id);
        ''')
        for n, (ident, source, parent) in enumerate(sessions, 1):
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (ident, source, parent, ident))
            db.execute('INSERT INTO messages VALUES(?,?,?)', (n, ident, f'content:{ident}'))
            db.execute('INSERT INTO system_prompts VALUES(?,?)', (ident, f'prompt:{ident}'))
            db.execute('INSERT INTO messages_fts VALUES(?)', (f'content:{ident}',))
            (root / 'sessions' / f'session_{ident}.json').write_text(json.dumps({'session_id': ident, 'platform': source, 'messages': [f'content:{ident}']}))


def test_preserves_retained_rows_files_and_documents_without_discarded_pages(tmp_path):
    source, quant = tmp_path / 'source', tmp_path / 'quant'
    history(source, [('keep', 'telegram', None), ('drop-weixin', 'weixin', None), ('drop-child', 'cli', 'drop-weixin')])
    history(quant, [('drop-quant', 'telegram', None)])
    (source / 'sessions' / 'early.json').write_text(json.dumps({'session_id':'early', 'platform':'feishu', 'messages':['old user history']}))
    (source / 'sessions' / 'request_dump_drop-quant.json').write_text('{}')
    (source / 'sessions' / 'sessions.json').write_text(json.dumps({'telegram':'keep', 'agent:quant:telegram':'drop-quant', 'agent:main:weixin':'drop-weixin'}))
    (source / 'docs').mkdir()
    (source / 'docs' / 'notes.md').write_text('User document')
    excluded = archive.excluded_sessions([source], [quant])
    assert excluded == {'drop-weixin', 'drop-child', 'drop-quant'}
    target = tmp_path / 'retained'
    result = archive.archive_profile(source, target, excluded)
    with sqlite3.connect(target / 'state.sqlite3') as db:
        assert db.execute('SELECT id FROM sessions').fetchall() == [('keep',)]
        assert db.execute('SELECT content FROM messages').fetchall() == [('content:keep',)]
        assert db.execute('SELECT prompt FROM system_prompts').fetchall() == [('prompt:keep',)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%fts%'").fetchall() == []
    assert (target / 'sessions' / 'session_keep.json').read_bytes() == (source / 'sessions' / 'session_keep.json').read_bytes()
    assert (target / 'sessions' / 'early.json').read_bytes() == (source / 'sessions' / 'early.json').read_bytes()
    assert json.loads((target / 'sessions' / 'sessions.json').read_text()) == {'telegram':'keep'}
    assert (target / 'documents' / 'docs' / 'notes.md').read_text() == 'User document'
    assert result['excluded_session_files'] == 3
    assert all(b'drop-' not in path.read_bytes() for path in target.rglob('*') if path.is_file())
    assert (source / 'sessions' / 'session_drop-weixin.json').exists()
    with pytest.raises(FileExistsError):
        archive.archive_profile(source, target, excluded)


def test_jsonl_checks_every_record_and_metadata(tmp_path):
    path = tmp_path / 'early.jsonl'
    path.write_text('{}\n' + json.dumps({'session_id':'excluded'}))
    assert archive.session_file(path, {'excluded'}) is None
    path.write_text(json.dumps({'origin_json':json.dumps({'platform':'weixin'})}))
    assert archive.session_file(path, set()) is None


def test_refuses_unknown_session_format_and_symlinks(tmp_path):
    path = tmp_path / 'history.bin'
    path.write_bytes(b'unknown')
    with pytest.raises(ValueError):
        archive.session_file(path, set())
    linked = tmp_path / 'link.json'
    linked.symlink_to(path)
    with pytest.raises(ValueError):
        archive.copy_checked(linked, tmp_path / 'output.json')
