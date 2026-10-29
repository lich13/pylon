"""Local runtime cache: key scope, reuse and archive refusal (no network, no root)."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/module-runtime.py'
spec = importlib.util.spec_from_file_location('module_runtime', SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
REVISION = 'a' * 40


def source_tree(root):
    for relative in runtime.KEY_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + '\n')
    for package in ('sdk/src/pylon_sdk', 'privileged/src/pylon_privileged'):
        (root / package).mkdir(parents=True)
        (root / package / '__init__.py').write_text('')
    (root / 'gmail/src').mkdir(parents=True)
    (root / 'gmail/src/private.py').write_text('SECRET = 1\n')
    return root


def write_archive(path, members):
    with tarfile.open(path, 'w:gz') as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)


def entry(name, data=b'', kind=tarfile.REGTYPE, link=''):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None


def fake_builder(calls):
    def build(source, version, key, revision, work, cache):
        calls.append(key)
        assert not (work / 'input/gmail').exists()
        metadata = {'gateway_revision': revision, 'source_sha256': key, 'python': version,
                    'requirements_sha256': runtime.digest(source / 'requirements.lock'), 'files': {}}
        output = work / 'output'
        output.mkdir()
        inner = json.dumps(metadata, sort_keys=True).encode()
        write_archive(output / runtime.ARCHIVE, [entry('runtime.json', inner), entry('python', kind=tarfile.DIRTYPE),
                                                 entry('python/bin', kind=tarfile.DIRTYPE),
                                                 entry('python/bin/python3', kind=tarfile.SYMTYPE, link='python3.14')])
        (output / 'runtime.json').write_text(json.dumps(
            {**metadata, 'file': runtime.ARCHIVE, 'sha256': runtime.digest(output / runtime.ARCHIVE)}))
        return output
    return build


def test_key_tracks_python_and_dependencies_but_not_module_source(tmp_path):
    source = source_tree(tmp_path / 'src')
    key = runtime.cache_key(source, '3.14.10')[0]
    assert key == runtime.cache_key(source, '3.14.10')[0]
    assert key != runtime.cache_key(source, '3.12.16')[0]
    (source / 'gmail/src/private.py').write_text('SECRET = 2\n')
    (source / 'sdk/src/pylon_sdk/__init__.py').write_text('changed = 1\n')
    assert runtime.cache_key(source, '3.14.10')[0] == key
    for relative in ('requirements.lock', 'deployment/bootstrap.lock', 'deployment/build_runtime.py'):
        before = (source / relative).read_text()
        (source / relative).write_text(before + 'x\n')
        assert runtime.cache_key(source, '3.14.10')[0] != key
        (source / relative).write_text(before)
    with pytest.raises(ValueError):
        runtime.cache_key(source, '3.14')


def test_build_once_then_reuse_without_touching_source(tmp_path):
    source = source_tree(tmp_path / 'src')
    before = sorted(p.relative_to(source) for p in source.rglob('*'))
    cache, calls = tmp_path / 'cache', []
    first, metadata, reused = runtime.ensure(source, '3.14.10', cache, fake_builder(calls), REVISION)
    assert not reused and first.parent == cache.resolve() and len(calls) == 1
    assert json.loads((first / 'build.json').read_text())['cache_key'] == first.name == metadata['source_sha256']
    (source / 'gmail/src/private.py').write_text('SECRET = 3\n')
    again, _, reused = runtime.ensure(source, '3.14.10', cache, fake_builder(calls), REVISION)
    assert reused and again == first and len(calls) == 1
    assert sorted(p.relative_to(source) for p in source.rglob('*')) == before
    assert [p.name for p in cache.iterdir() if p.name != '.lock'] == [first.name]
    (source / 'requirements.lock').write_text('changed\n')
    third, _, reused = runtime.ensure(source, '3.14.10', cache, fake_builder(calls), REVISION)
    assert not reused and third != first and len(calls) == 2


def test_stage_inputs_copies_only_runtime_inputs(tmp_path):
    source = source_tree(tmp_path / 'src')
    (source / 'sdk/src/pylon_sdk/__pycache__').mkdir()
    (source / 'sdk/src/pylon_sdk/__pycache__/x.pyc').write_bytes(b'')
    runtime.stage_inputs(source, tmp_path / 'staged')
    copied = {p.relative_to(tmp_path / 'staged').as_posix() for p in (tmp_path / 'staged').rglob('*') if p.is_file()}
    assert copied == {*runtime.KEY_FILES, 'sdk/src/pylon_sdk/__init__.py', 'privileged/src/pylon_privileged/__init__.py'}
    (source / 'sdk/src/link.py').symlink_to(source / 'gmail/src/private.py')
    with pytest.raises(ValueError, match='symlinked'):
        runtime.stage_inputs(source, tmp_path / 'again')


def test_tampered_cache_is_refused(tmp_path):
    source = source_tree(tmp_path / 'src')
    cache, calls = tmp_path / 'cache', []
    directory, _, _ = runtime.ensure(source, '3.14.10', cache, fake_builder(calls), REVISION)
    with (directory / runtime.ARCHIVE).open('ab') as archive:
        archive.write(b'x')
    with pytest.raises(ValueError, match='checksum'):
        runtime.ensure(source, '3.14.10', cache, fake_builder(calls), REVISION)
    assert len(calls) == 1


@pytest.mark.parametrize('members', [
    [entry('/abs')],
    [entry('../escape')],
    [entry('a/../../escape')],
    [entry('./runtime.json')],
    [entry('dup'), entry('dup')],
    [entry('dev', kind=tarfile.CHRTYPE)],
    [entry('hard', kind=tarfile.LNKTYPE, link='runtime.json')],
    [entry('out', kind=tarfile.SYMTYPE, link='/etc/passwd')],
    [entry('up', kind=tarfile.SYMTYPE, link='../../x')],
    [entry('dir', kind=tarfile.SYMTYPE, link='.'), entry('dir/file')],
], ids=['absolute', 'parent', 'nested-parent', 'dot-prefix', 'duplicate', 'device', 'hardlink',
        'absolute-link', 'escaping-link', 'through-link'])
def test_unsafe_archives_are_refused(tmp_path, members):
    path = tmp_path / 'bad.tar.gz'
    write_archive(path, [entry('runtime.json', b'{}'), *members])
    with pytest.raises(ValueError):
        runtime.check_archive(path)


def test_archive_without_metadata_is_refused(tmp_path):
    path = tmp_path / 'empty.tar.gz'
    write_archive(path, [entry('python', kind=tarfile.DIRTYPE)])
    with pytest.raises(ValueError, match='metadata'):
        runtime.check_archive(path)
