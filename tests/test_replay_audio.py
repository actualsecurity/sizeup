"""Replay: real airband clips through the heterodyne-tone notch (GitHub #283, #263; sizeup/radio/amtones.py).

The data is private and not in git: tests/replay.py (tools/replay_sync.py pull, or tools/capture_fixture.py audio:
HPN Tower / Ground / NY Approach clips restored read-only from the hub's backup; never dispatch audio).

On each real clip: the notch leaves its length, never adds energy or a non-finite sample, takes out every steady
tone it finds (a second look finds none), and keeps the voice (most of the clip's energy stays).
"""

from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from sizeup.radio import amtones

pytestmark = pytest.mark.replay("audio")


def read_wav(path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, f"{path.name}: not 16-bit mono"
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    return x, rate


def test_the_tone_notch_on_real_airband_clips(replay_sets):
    n = 0
    for s in replay_sets("audio"):
        for c in json.loads(s.file("clips.json").read_text()):
            x, rate = read_wav(s.file(c["file"]))
            assert len(x) > rate // 4, f"{s.name} {c['file']}: {len(x)} samples"
            tones = amtones.find_tones(x, rate)
            y = amtones.detone(x, rate, tones)
            label = f"{s.name} {c['file']} (tones {[round(f) for f, _ in tones]})"
            assert len(y) == len(x) and np.all(np.isfinite(y)), label
            rms_x, rms_y = float(np.sqrt(np.mean(x ** 2))), float(np.sqrt(np.mean(np.asarray(y, np.float64) ** 2)))
            assert rms_y <= rms_x * 1.01, f"{label}: louder after the notch ({rms_x:.4f} -> {rms_y:.4f})"
            assert rms_y >= rms_x * 0.5, f"{label}: the voice went with the tone ({rms_x:.4f} -> {rms_y:.4f})"
            assert amtones.find_tones(y, rate) == [], f"{label}: a tone left after the notch"
            # the carrier part (the hub's clips keep the 0.4 s pre-roll and up to 0.2 s tail): none left there either
            span = (int(0.4 * rate), len(x) - int(0.2 * rate))
            if span[1] - span[0] > rate // 2:
                yy = amtones.detone(x, rate, amtones.find_tones(x, rate, span=span))
                assert amtones.find_tones(yy, rate, span=span) == [], f"{label}: a tone left on its carrier"
            if not tones:
                assert y is x, f"{label}: changed with no tone found"
            n += 1
    assert n, "no clip"
