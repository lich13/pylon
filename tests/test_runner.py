import json
import sys

from pylon.runner import run


def test_failed_business_result_with_zero_exit_and_repeat_run_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    monkeypatch.setenv('PYLON_OUTBOX', str(tmp_path / 'data/outbox.sqlite3'))
    monkeypatch.setenv('INVOCATION_ID', 'same-scheduled-run')
    scripts = tmp_path / 'profiles/default/scripts'
    scripts.mkdir(parents=True)
    binary = tmp_path / 'venv/bin'
    binary.mkdir(parents=True)
    (binary / 'python').symlink_to(sys.executable)
    (scripts / 'task.py').write_text('''import json,os
from pathlib import Path
p=Path('business-count')
p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
Path(os.environ['PYLON_RESULT_FILE']).write_text(json.dumps({'status':'failed','detail':'upstream unavailable'}))
print('[SILENT]')
''')
    (tmp_path / 'jobs.json').write_text(json.dumps({'test': {'profile':'default','script':'task.py','timeout':10,
        'deliver':True,'name':'test','failure_notify':False}}))
    assert run('test') == 1
    assert run('test') == 1
    assert (scripts.parent / 'business-count').read_text() == '1'
    assert 'upstream unavailable' in capsys.readouterr().out


def test_controller_uses_atomic_reconcile_without_writing_timer_state(monkeypatch):
    from pylon import bot
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(bot.scheduler, 'admin', lambda action: calls.append(action))
    bot.synchronize_chiphell()
    assert calls == ['chiphell-reconcile']


def test_cookie_import_prevents_controller_starting_poller(tmp_path, monkeypatch):
    from pylon import bot
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    journal = tmp_path / 'profiles/webops/data/chiphell_fid26/.cookie-import-transaction.json'
    journal.parent.mkdir(parents=True)
    journal.write_text('{"stage":"VALIDATING"}')
    def unexpected(*args, **kwargs):
        raise AssertionError('controller must leave the importer in control')
    monkeypatch.setattr(bot.scheduler, 'admin', unexpected)
    monkeypatch.setattr(bot.subprocess, 'run', unexpected)
    bot.synchronize_chiphell()
