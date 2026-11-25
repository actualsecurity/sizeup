"""The GPU STT worker (deploy/stt-worker/worker.py) as one module for every test: loaded by its path, since
deploy/stt-worker isn't a package (the hyphen). Never the compatibility copy in tools/stt_worker (tests/
test_stt_worker_move.py checks that one is byte-identical)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "deploy" / "stt-worker" / "worker.py"
NAME = "sizeup_stt_worker"


def load():
    mod = sys.modules.get(NAME)
    if mod is None:
        spec = importlib.util.spec_from_file_location(NAME, PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = mod              # (before it runs: its dataclasses look themselves up there)
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(NAME, None)
            raise
    return mod


W = load()
