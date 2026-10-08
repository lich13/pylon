import json
import multiprocessing
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from pylon.tasks.marketplace import request_gate, sources


def _navigate(root, ready, output, number):
    request_gate.home = lambda: Path(root)
    ready.wait(3)
    with request_gate.request_slot(.08):
        output.put(('start', number, time.time()))
        time.sleep(.12)
        output.put(('end', number, time.time()))


def test_archival_and_live_processes_share_spacing_and_never_overlap(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    ready, output = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_navigate, args=(str(tmp_path), ready, output, n)) for n in range(2)]
    try:
        for process in processes:
            process.start()
        ready.set()
        records = [output.get(timeout=10) for _ in range(4)]
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        records.sort(key=lambda record: record[2])
        assert [record[0] for record in records] == ['start', 'end', 'start', 'end']
        assert records[2][2] - records[0][2] >= .08
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
        output.close()


def test_persisted_request_and_cooldown_survive_a_new_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(request_gate, 'home', lambda: tmp_path)
    clock = {'now': 100.0}
    sleeps = []
    monkeypatch.setattr(request_gate.time, 'time', lambda: clock['now'])

    def sleep(seconds):
        sleeps.append(seconds)
        clock['now'] += seconds

    monkeypatch.setattr(request_gate.time, 'sleep', sleep)
    with request_gate.request_slot(15):
        pass
    with request_gate.request_slot(15):
        pass
    assert sleeps == [15]
    with pytest.raises(request_gate.SourceRateLimited):
        with request_gate.request_slot(15):
            raise request_gate.SourceRateLimited('fixture_limit')
    state = json.loads((tmp_path / 'run/marketplace-nga-rate.json').read_text())
    assert state['cooldown_until'] == 190
    with pytest.raises(request_gate.SourceRateLimited, match='shared_cooldown'):
        with request_gate.request_slot(15):
            pytest.fail('cooldown must prevent navigation')
    assert json.loads((tmp_path / 'run/marketplace-nga-rate.json').read_text())['cooldown_until'] == 190
    clock['now'] = 191
    with request_gate.request_slot(15):
        pass


def test_late_limit_and_corrupt_budget_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(request_gate, 'home', lambda: tmp_path)
    request_gate.cooldown()
    with pytest.raises(request_gate.SourceRateLimited):
        with request_gate.request_slot(15):
            pytest.fail('late limit must apply to other processes')
    (tmp_path / 'run/marketplace-nga-rate.json').write_text('{"last_started":"bad"}')
    with pytest.raises(ValueError, match='invalid NGA request budget'):
        with request_gate.request_slot(15):
            pytest.fail('corrupt state must not bypass the request budget')


def test_browser_cleanup_does_not_extend_an_existing_shared_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv('PYLON_NGA_COOKIES', str(tmp_path/'absent.json'))
    manager=MagicMock()
    api=ModuleType('playwright.sync_api')
    api.sync_playwright=lambda: manager
    engine=manager.__enter__.return_value.chromium.launch.return_value
    with (
        patch.dict(sys.modules, {'playwright.sync_api':api}),
        patch('pylon.shared.playwright_env.ensure'),
        patch.object(request_gate, 'cooldown') as postpone,
    ):
        with pytest.raises(request_gate.SourceRateLimited, match='shared_cooldown'):
            with sources.browser('nga'):
                raise request_gate.SourceRateLimited('nga_shared_cooldown')
    postpone.assert_not_called()
    engine.new_context.return_value.close.assert_called_once()
    engine.close.assert_called_once()
