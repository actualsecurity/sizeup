#!/usr/bin/env python3
"""Decode MDC-1200 PTT-IDs from WAV files (mono int16, e.g. rtl_fm discriminator audio).

Run:  .venv/bin/python tools/mdc_decode.py tests/fixtures/mdc/*.wav [--all]
--all also prints sync hits whose CRC failed.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sizeup.radio.mdc1200 import decode  # noqa: E402


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    for name in (a for a in argv if a != "--all"):
        with wave.open(name) as w:
            if w.getsampwidth() != 2:
                print(f"{name}: need 16-bit PCM")
                continue
            x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            x = x.reshape(-1, w.getnchannels())[:, 0]
            rate = w.getframerate()
        pkts = decode(x, rate, only_valid=not show_all)
        print(f"{name}: " + ("; ".join(str(p) for p in pkts) if pkts else "no MDC-1200"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
