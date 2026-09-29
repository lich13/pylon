import json
import sys

from pylon.runner import run


def test_failed_business_result_with_zero_exit_and_repeat_run_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    monkeypatch.setenv('PYLON_OUTBOX', str(tmp_path / 'data/outbox.sqlite3'))
    monkeypatch.setenv('DAG_RUN_ID', 'same-scheduled-run')
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


def test_panel_pause_also_stops_chiphell_poller(monkeypatch):
    from pylon import bot
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(bot, 'request', lambda method, path: {'paused':True} if 'scheduler' in path else {'suspended':False})
    monkeypatch.setattr(bot.subprocess, 'run', lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    bot.synchronize_chiphell()
    assert calls[-1] == ['sudo','-n','/usr/bin/systemctl','stop','chiphell-fid26-poller.service']
