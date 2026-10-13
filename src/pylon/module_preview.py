"""Run an external preview against disposable local data."""
import os
from pathlib import Path
import subprocess
import tempfile

from . import extensions


def run(port, module="content"):
    if not 1024 <= port <= 65535: raise ValueError('invalid preview port')
    modules = extensions.snapshot()
    preview = modules[module].manifest['preview']
    profile = 'default' if module == 'gmail' else 'webops'
    with tempfile.TemporaryDirectory(prefix='pylon-preview-') as temporary:
        root = Path(temporary)
        for name in ['home', 'data', 'run', 'profiles/default', 'profiles/webops']:
            (root / name).mkdir(parents=True, exist_ok=True)
        env = extensions.environment(profile, modules)
        # No production profile or credentials are loaded into previews.
        for key in list(env):
            if key not in {'PATH', 'PYTHONPATH', 'PYLON_MODULE_SNAPSHOT', 'PYLON_TASK_PYTHON', 'LANG', 'TZ'}:
                env.pop(key)
        env.update(HOME=str(root / 'home'), PYLON_HOME=str(root), PYLON_PROFILE=profile,
                   PYLON_PROFILE_ROOT=str(root / 'profiles' / profile), PYLON_OUTBOX=str(root / 'data/outbox.sqlite3'),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1', PYLON_FIXTURE_PREVIEW='1')
        proc = subprocess.Popen([extensions.python(), '-m', preview, '--port', str(port)], env=env)
        try:
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
