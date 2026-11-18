"""The minimal Config the App tests build an App from (shared: tests/test_app.py, tests/airport_golden.py ...)."""

from __future__ import annotations

from sizeup.config import Config
from sizeup.radio.tones import ToneCode


def cfg(tmp_path):
    return Config("t", "Test", "127.0.0.1", 0, "rtl_fm", (), (ToneCode("Port Chester FD", 349.1, 311.1, "PCFD"),),
                  frozenset({"Port Chester FD", "Rye Brook FD"}), "whisper-server", "m.bin", 1, 1, "",
                  "", clips_dir=str(tmp_path / "clips"))
