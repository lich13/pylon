from __future__ import annotations

import json
from pathlib import Path
import stat
from unittest.mock import Mock

import pytest

from pylon.tasks.linuxdo import __main__ as linuxdo_main
from pylon.tasks.linuxdo import following, helper_account
from pylon.tasks.linuxdo.helper_account import AccountError


IMPORTED_AT = '2026-01-02T03:04:05+00:00'


def snapshot(**overrides):
    return {'version': 1, 'complete': True, 'imported_at': IMPORTED_AT,
            'usernames': ['fixture_actor']} | overrides


def write_snapshot(path, value=None):
    path.write_text(json.dumps(snapshot() if value is None else value))
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def private_paths(tmp_path, monkeypatch):
    root = tmp_path / 'helper-private'
    root.mkdir(mode=0o700)
    source = tmp_path / 'import-private'
    source.mkdir(mode=0o700)
    monkeypatch.setattr(following, 'private_root', lambda: root)
    monkeypatch.setattr(helper_account, 'private_root', lambda: root)
    monkeypatch.setattr(helper_account, '_client', Mock(side_effect=AssertionError('no live Helper access')))
    return root, source


def test_missing_list_and_complete_empty_list_have_distinct_configuration(private_paths):
    root, _ = private_paths

    assert following.load() is None
    assert following.available() == (None, '')
    write_snapshot(root / 'following.json', snapshot(usernames=[]))
    loaded, error = following.available()

    assert loaded is not None and loaded.usernames == frozenset()
    assert error == ''
    assert loaded.summary()['configured'] is True and loaded.summary()['count'] == 0


def test_usernames_are_casefolded_deduplicated_and_fingerprint_ignores_order_and_import_time():
    first = following.validate(snapshot(usernames=['FiXtUrE_Actor', 'fixture_other', 'fixture_actor']))
    second = following.validate(snapshot(usernames=['FIXTURE_OTHER', 'fixture_actor'],
                                         imported_at='2026-02-03T04:05:06Z'))

    assert first.usernames == frozenset({'fixture_actor', 'fixture_other'})
    assert first.payload()['usernames'] == ['fixture_actor', 'fixture_other']
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != following.validate(snapshot(usernames=[])).fingerprint
    assert first.imported_at == IMPORTED_AT
    assert set(first.summary()) == {'configured', 'count', 'imported_at', 'fingerprint'}
    assert 'fixture_actor' not in json.dumps(first.summary())


@pytest.mark.parametrize('overrides', [
    {'version': 0}, {'version': 2}, {'version': True}, {'version': '1'},
    {'complete': False}, {'complete': 1}, {'complete': 'true'},
    {'imported_at': '2026-01-02T03:04:05'}, {'imported_at': '2026-02-30T00:00:00Z'},
    {'imported_at': ''}, {'imported_at': None}, {'imported_at': 0}, {'extra': 'fixture'},
])
def test_snapshot_rejects_partial_unversioned_or_invalid_date_metadata(overrides):
    with pytest.raises(ValueError, match='invalid following snapshot'):
        following.validate(snapshot(**overrides))


@pytest.mark.parametrize('missing', ['version', 'complete', 'imported_at', 'usernames'])
def test_snapshot_requires_every_schema_field(missing):
    value = snapshot()
    value.pop(missing)

    with pytest.raises(ValueError, match='invalid following snapshot'):
        following.validate(value)


@pytest.mark.parametrize('names', [
    None, 'fixture_actor', {}, [''], [None], [True], ['@fixture_actor'], ['fixture actor'],
    ['fixture/actor'], ['fixture\nactor'], ['fixture?actor'], ['x' * 101], ['fixture_actor'] * 10001,
])
def test_snapshot_rejects_invalid_username_collections(names):
    with pytest.raises(ValueError, match='invalid following usernames'):
        following.validate(snapshot(usernames=names))


@pytest.mark.parametrize('unsafe', ['file-mode', 'parent-mode', 'file-symlink', 'parent-symlink'])
def test_import_rejects_nonprivate_or_symlinked_input_and_preserves_installed_list(private_paths, unsafe):
    root, source = private_paths
    installed = write_snapshot(root / 'following.json', snapshot(usernames=['fixture_existing']))
    original = installed.read_bytes()
    incoming = write_snapshot(source / 'incoming.json')
    if unsafe == 'file-mode':
        incoming.chmod(0o644)
    elif unsafe == 'parent-mode':
        source.chmod(0o755)
    elif unsafe == 'file-symlink':
        alias = source / 'alias.json'
        alias.symlink_to(incoming)
        incoming = alias
    else:
        alias = source.parent / 'source-alias'
        alias.symlink_to(source, target_is_directory=True)
        incoming = alias / incoming.name

    with pytest.raises(AccountError):
        following.install(incoming)

    assert installed.read_bytes() == original


def test_oversized_snapshot_is_rejected_before_json_is_read(private_paths):
    _, source = private_paths
    incoming = source / 'incoming.json'
    incoming.write_text(' ' * 2_000_001)
    incoming.chmod(0o600)

    with pytest.raises(ValueError, match='following snapshot too large'):
        following.load(incoming)


def test_import_atomically_replaces_a_private_snapshot_and_keeps_the_source(private_paths, monkeypatch):
    root, source = private_paths
    target = write_snapshot(root / 'following.json', snapshot(usernames=['fixture_existing']))
    previous = target.read_bytes()
    incoming = write_snapshot(source / 'incoming.json', snapshot(
        imported_at='2026-02-03T04:05:06Z', usernames=['FiXtUrE_Actor', 'fixture_actor']))
    source_bytes = incoming.read_bytes()
    replace = helper_account.os.replace
    replacements = []

    def checked_replace(temporary, destination):
        temporary, destination = Path(temporary), Path(destination)
        assert destination == target and destination.read_bytes() == previous
        assert temporary.parent == target.parent
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
        assert json.loads(temporary.read_text()) == snapshot(
            imported_at='2026-02-03T04:05:06Z', usernames=['fixture_actor'])
        replacements.append((temporary, destination))
        replace(temporary, destination)

    monkeypatch.setattr(helper_account.os, 'replace', checked_replace)

    summary = following.install(incoming)

    assert len(replacements) == 1
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert following.load().usernames == frozenset({'fixture_actor'})
    assert incoming.read_bytes() == source_bytes
    assert summary['configured'] is True and summary['count'] == 1
    assert 'fixture_actor' not in json.dumps(summary)
    assert not list(root.glob('.helper-*'))


@pytest.mark.parametrize('failure', ['partial-input', 'replace-failed'])
def test_failed_import_preserves_previous_snapshot_and_removes_its_temporary_file(
        private_paths, monkeypatch, failure):
    root, source = private_paths
    target = write_snapshot(root / 'following.json', snapshot(usernames=['fixture_existing']))
    previous = target.read_bytes()
    incoming = write_snapshot(source / 'incoming.json', snapshot(
        imported_at='2026-02-03T04:05:06Z', complete=failure != 'partial-input'))
    if failure == 'replace-failed':
        monkeypatch.setattr(helper_account.os, 'replace', Mock(side_effect=OSError('synthetic atomic replace failure')))

    with pytest.raises(ValueError if failure == 'partial-input' else OSError):
        following.install(incoming)

    assert target.read_bytes() == previous
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(root.glob('.helper-*'))


@pytest.mark.parametrize('arguments', [[], ['--apply'], ['--input', 'fixture.json']])
def test_import_cli_requires_both_apply_and_input(arguments, monkeypatch, capsys):
    install = Mock(side_effect=AssertionError('invalid arguments must not import'))
    monkeypatch.setattr(following, 'install', install)

    with pytest.raises(SystemExit) as error:
        linuxdo_main.main(['helper-import-following', *arguments])

    assert error.value.code == 2
    assert 'requires --apply and a private --input file' in capsys.readouterr().err
    install.assert_not_called()


def test_import_cli_installs_the_complete_snapshot_and_prints_only_summary(private_paths, capsys):
    root, source = private_paths
    incoming = write_snapshot(source / 'incoming.json')

    assert linuxdo_main.main(['helper-import-following', '--apply', '--input', str(incoming)]) == 0

    output = capsys.readouterr()
    assert output.err == ''
    assert json.loads(output.out) == following.load(root / 'following.json').summary() | {
        'added': 1, 'removed': 0, 'changed': True,
    }
    assert 'fixture_actor' not in output.out
    assert str(incoming) not in output.out


def test_import_cli_error_does_not_print_private_input_or_exception_detail(private_paths, capsys):
    root, source = private_paths
    incoming = write_snapshot(source / 'incoming.json', snapshot(usernames=['fixture private input']))

    assert linuxdo_main.main(['helper-import-following', '--apply', '--input', str(incoming)]) == 1

    output = capsys.readouterr()
    assert json.loads(output.out) == {'error': 'ValueError'}
    assert output.err == ''
    assert 'fixture private input' not in output.out and str(incoming) not in output.out
    assert not (root / 'following.json').exists()
