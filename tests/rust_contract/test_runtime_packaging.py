"""Runtime selection consumes only successful compatibility job artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('runtime_packaging', Path(__file__).parents[2] / 'scripts/package-external.py')
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def candidate(root, version, name=None):
    target = root / (name or version)
    target.mkdir()
    (target / 'runtime.json').write_text(json.dumps({'python': version}))
    return target


def test_prefer_gil_314_otherwise_use_tested_312(tmp_path):
    with pytest.raises(ValueError, match='no compatible'):
        packaging.select_runtime(tmp_path)
    baseline = candidate(tmp_path, '3.12.16')
    assert packaging.select_runtime(tmp_path) == baseline
    preferred = candidate(tmp_path, '3.14.10')
    assert packaging.select_runtime(tmp_path) == preferred


@pytest.mark.parametrize('version', ['3.14.10', '3.12.16'])
def test_reject_duplicate_compatibility_results(tmp_path, version):
    candidate(tmp_path, '3.14.10')
    candidate(tmp_path, '3.12.16')
    candidate(tmp_path, version, 'duplicate')
    with pytest.raises(ValueError, match='ambiguous runtime'):
        packaging.select_runtime(tmp_path)


def test_unchecked_version_is_not_a_candidate(tmp_path):
    candidate(tmp_path, '3.15.0')
    with pytest.raises(ValueError, match='no compatible runtime'):
        packaging.select_runtime(tmp_path)


@pytest.fixture
def runtime_download(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'fixture/pylon')
    monkeypatch.setenv('JOB', 'a' * 24)
    candidates = tmp_path / 'candidates'
    candidates.mkdir()
    payloads = {version: f'fixture runtime {version}'.encode()
                for version in ('3.12.16', '3.14.10')}
    metadata = {version: {'python': version,
                         'sha256': hashlib.sha256(payload).hexdigest(),
                         'gateway_revision': 'b' * 40, 'source_sha256': 'c' * 64}
                for version, payload in payloads.items()}
    calls = []
    state = {'tamper': None}

    def download(args, *, check):
        calls.append(args)
        assert check is True
        assert args[:6] == ['gh', 'run', 'download', '123', '--repo', 'fixture/pylon']
        if state['tamper'] == 'download':
            raise subprocess.CalledProcessError(1, args)
        name = args[args.index('--name') + 1]
        version = next(version for version in payloads
                       if name == f"built-runtime-{'a' * 24}-{version}")
        destination = Path(args[args.index('--dir') + 1])
        destination.mkdir()
        saved = dict(metadata[version])
        if state['tamper'] == 'metadata':
            saved['source_sha256'] = 'd' * 64
        (destination / 'runtime.json').write_text(json.dumps(saved))
        (destination / 'pylon-runtime.tar.gz').write_bytes(
            b'changed runtime' if state['tamper'] == 'archive' else payloads[version])

    monkeypatch.setattr(packaging.subprocess, 'run', download)

    def approve(version):
        proof = candidate(candidates, version)
        (proof / 'runtime.json').write_text(json.dumps(metadata[version]))
        return proof

    return candidates, approve, calls, state, metadata


@pytest.mark.parametrize('preferred_passed', [True, False])
def test_download_only_selected_compatible_runtime(tmp_path, runtime_download, preferred_passed):
    candidates, approve, calls, _, metadata = runtime_download
    approve('3.12.16')
    if preferred_passed:
        approve('3.14.10')
    selected = packaging.select_runtime(candidates)
    destination = packaging.download_runtime(selected, tmp_path / 'download')
    expected = '3.14.10' if preferred_passed else '3.12.16'
    assert len(calls) == 1
    assert calls[0][calls[0].index('--name') + 1] == f"built-runtime-{'a' * 24}-{expected}"
    assert json.loads((destination / 'runtime.json').read_text()) == metadata[expected]
    assert not (selected / 'pylon-runtime.tar.gz').exists()


@pytest.mark.parametrize('tamper,message', [
    ('metadata', 'compatibility metadata mismatch'),
    ('archive', 'checksum mismatch'),
])
def test_reject_runtime_not_matching_compatibility_result(tmp_path, runtime_download, tamper, message):
    _, approve, _, state, _ = runtime_download
    proof = approve('3.14.10')
    state['tamper'] = tamper
    with pytest.raises(ValueError, match=message):
        packaging.download_runtime(proof, tmp_path / 'download')


def test_runtime_download_failure_stops_packaging(tmp_path, runtime_download):
    _, approve, _, state, _ = runtime_download
    proof = approve('3.14.10')
    state['tamper'] = 'download'
    with pytest.raises(subprocess.CalledProcessError):
        packaging.download_runtime(proof, tmp_path / 'download')
