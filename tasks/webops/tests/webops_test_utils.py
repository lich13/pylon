from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


PROFILE_ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, script_name: str) -> ModuleType:
    script = PROFILE_ROOT / 'scripts' / script_name
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load script module from {script}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
