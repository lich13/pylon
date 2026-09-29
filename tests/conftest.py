"""Keep the unified suite away from production profiles and credentials."""
import os
from pathlib import Path
import shutil
import tempfile

_runtime = tempfile.TemporaryDirectory(prefix='pylon-tests-')
_root = Path(_runtime.name)
_prior = {key: os.environ.get(key) for key in ('PYLON_HOME', 'PYLON_PROFILE_ROOT')}
os.environ['PYLON_HOME'] = str(_root)
os.environ['PYLON_PROFILE_ROOT'] = str(_root / 'profiles/webops')
for profile in ('default', 'webops'):
    (_root / 'profiles' / profile).mkdir(parents=True)
shutil.copyfile(Path(__file__).resolve().parents[1] / 'jobs.json', _root / 'jobs.json')


def pytest_sessionfinish(session, exitstatus):
    for key, value in _prior.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _runtime.cleanup()
