import runpy
import sys

import pytest

from pylon.admin import software as admin


@pytest.fixture
def privileged_root(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'ROOT', tmp_path / 'privileged')
    return monkeypatch


@pytest.mark.parametrize('args', [
    ['apt-update', 'sosreport', 'default', 'software-review:legacy', '1.0', '1.1'],
    ['apt-batch', 'default', 'software-batch:' + 'a' * 24, '[]'],
    ['request', 'software-batch:' + 'a' * 24],
])
def test_retired_apt_and_batch_admin_routes_are_rejected(args, privileged_root):
    with pytest.raises(ValueError, match='unsupported software action'):
        admin.main(args)
    assert not admin.ROOT.exists()


@pytest.mark.parametrize('argv', [
    ['pylon.admin.software', 'apt-worker', 'sosreport'],
    ['pylon.admin.software', 'apt-batch-worker', 'legacy'],
])
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_retired_apt_workers_are_rejected_by_module_entrypoint(argv, monkeypatch):
    # The worker guard is intentionally tested through __main__, where old
    # systemd templates used to enter the privileged module directly.
    monkeypatch.setattr(admin.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(SystemExit, match='unsupported software action'):
        runpy.run_module('pylon.admin.software', run_name='__main__')
