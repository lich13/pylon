from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pylon.tasks.xianbao import alert


def test_journal_reads_fixed_unit_without_shell(monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout='poll source unavailable', stderr=''))
    monkeypatch.setattr(alert.subprocess, 'run', run)
    assert alert.journal_tail('pylon-task@xianbao-poll.service', 12) == 'poll source unavailable'
    assert run.call_args.args[0][:3] == ['journalctl', '-u', 'pylon-task@xianbao-poll.service']
    assert not run.call_args.kwargs.get('shell')


def test_no_journal_read_for_quiet_poll_error(monkeypatch):
    run = Mock(side_effect=AssertionError('no journal needed'))
    monkeypatch.setattr(alert.subprocess, 'run', run)
    assert alert.journal_tail('pylon-task@xianbao-poll.service', 0) == ''


def test_cooldown_advances_only_after_notification_is_persisted(tmp_path, monkeypatch):
    state = tmp_path / 'alerts.json'
    monkeypatch.setattr(alert.sys, 'argv', ['alert', '--state', str(state), '--profile-root', str(tmp_path), '--journal-lines', '0'])
    send = Mock(side_effect=OSError('outbox unavailable'))
    monkeypatch.setattr(alert, 'send_telegram', send)
    with pytest.raises(OSError, match='outbox unavailable'):
        alert.main()
    assert not state.exists()
    send.side_effect = None
    assert alert.main() == 0 and state.exists()
    assert alert.main() == 0
    assert send.call_count == 2
