"""Runtime selection consumes only successful compatibility job artifacts."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('runtime_packaging', Path(__file__).parents[2] / 'scripts/package-external.py')
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


def candidate(root, version):
    target = root / version
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
