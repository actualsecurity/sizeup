import json
import wave

import numpy as np

from sizeup.radio.p25 import CallIngest

LABELS = {307: "Fire/EMS 15", 301: "60 Control"}


def write_call(d, name, tg, srcs, rate=8000, seconds=2.0):
    meta = {"talkgroup": tg, "start_time": 1790000000, "stop_time": 1790000002, "freq": 478187500,
            "srcList": [{"src": s, "time": 1790000000, "pos": 0.0, "tag": ""} for s in srcs]}
    (d / f"{name}.json").write_text(json.dumps(meta))
    with wave.open(str(d / f"{name}.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes((0.2 * np.sin(np.arange(int(rate * seconds)) / 5) * 32767).astype("<i2").tobytes())


def test_scan_keeps_our_talkgroups_resamples_and_names_units(tmp_path):
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    write_call(day, "307-1789999000_478187500-call_0", 307, [1])  # already there at startup: not replayed
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None, aliases={"2391001": "2391"}).prime()
    write_call(day, "307-1790000000_478187500-call_1", 307, [2391001, 2391001, 6000123])
    write_call(day, "999-1790000001_478187500-call_2", 999, [5])            # a talkgroup not in our list
    (day / "301-1790000002_478312500-call_3.json").write_text(json.dumps({"talkgroup": 301}))  # wav not done yet
    calls = ing.scan()
    # Every talkgroup is ingested (sizeup decides what to keep); unlisted ones are named "TG n".
    assert sorted(c.label for c in calls) == ["Fire/EMS 15", "TG 999"]
    calls = [c for c in calls if c.talkgroup == 307]
    c = calls[0]
    assert c.units == ["2391", "6000123"] and c.talkgroup == 307
    assert len(c.audio) == 32000 and c.audio.dtype == np.float32   # 2 s at 16 kHz
    write_call(day, "301-1790000002_478312500-call_3", 301, [1])
    assert [c.label for c in ing.scan()] == ["60 Control"]           # picked up once the wav lands
    assert ing.scan() == []                                          # each call once


def test_empty_wav_is_skipped_not_fatal(tmp_path):
    # trunk-recorder can leave a header-less/zero-length WAV (wave raises EOFError): skip it, keep going.
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None).prime()
    (day / "307-1.json").write_text(json.dumps({"talkgroup": 307}))
    (day / "307-1.wav").write_bytes(b"")
    write_call(day, "307-2", 307, [1])
    assert [c.label for c in ing.scan()] == ["Fire/EMS 15"]
