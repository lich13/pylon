from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


CHARTOO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = CHARTOO_ROOT.parent

for path in (str(PROFILE_ROOT), str(CHARTOO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_script_module(name: str, script_name: str) -> ModuleType:
    script = CHARTOO_ROOT / 'scripts' / script_name
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load script module from {script}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
