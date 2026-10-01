import json
import os
import time

import numpy as np

from sizeup.janitor import clean

H = 3600


def touch(p, age_s, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_audio_12h_everywhere_records_24h(tmp_path):
    # User: "keep audio 12hr across the board" (pages/transcripts keep the 24 h history).
    cap = tmp_path / "captures"
    clips, p25 = cap / "clips", cap / "p25"
    old_clip, new_clip = touch(clips / "a.wav", 13 * H), touch(clips / "b.wav", 11 * H)
    old_skip = touch(clips / "skipped" / "FG1-1.wav", 13 * H)
    page_13h, page_25h = touch(clips / "p1.page.json", 13 * H), touch(clips / "p0.page.json", 25 * H)
    day = p25 / "westchester" / "2026" / "9" / "30"
    old_call, old_meta = touch(day / "307-1.wav", 13 * H), touch(day / "307-1.json", 13 * H)
    orphan = touch(p25 / "westchester" / "2026" / "10" / "1" / "333-2.json", 13 * H)
    tmp_old, tmp_new = touch(cap / "p25-tmp" / "x.wav", 2 * H), touch(cap / "p25-tmp" / "y.wav", 60)
    st = clean(cap, clips, p25, 12 * H, 24 * H)
    assert not old_clip.exists() and new_clip.exists() and not old_skip.exists()
    assert page_13h.exists() and not page_25h.exists()          # transcripts outlive their audio
    assert not old_call.exists() and not old_meta.exists() and not orphan.exists()
    assert not (p25 / "westchester" / "2026").exists()          # empty date folders removed
    assert not tmp_old.exists() and tmp_new.exists()
    assert st["deleted"] == 7 and st["audio_files"] == 2 and st["bytes"] > 0


def test_p25_call_kept_once_and_recorder_log_capped(tmp_path):
    from tests.test_app import App, cfg
    from sizeup.radio.p25 import Call
    from sizeup.radio.fm import RATE
    from dataclasses import replace
    day = tmp_path / "p25" / "westchester" / "2026" / "10" / "1"
    wav = touch(day / "307-9.wav", 5)
    meta = touch(day / "307-9.json", 5, json.dumps({"talkgroup": 307}).encode())
    app = App(replace(cfg(tmp_path), p25_dir=str(tmp_path / "p25"), p25_talkgroups={307: "Fire/EMS 15"},
                      p25_tg_info={307: {"name": "Fire/EMS 15", "group": "Fire/EMS", "encrypted": False}}))
    app.stt.submit = lambda item, audio: None
    now = time.time()
    app._call(Call("307-9", 307, "Fire/EMS 15", now, now + 2, [], np.zeros(RATE * 2, np.float32), str(wav)))
    assert not wav.exists() and not meta.exists()                 # trunk-recorder's original is gone ...
    assert app.clip("307-9")[:4] == b"RIFF"                       # ... sizeup's copy plays

    import sizeup.radio.recorder as rec
    rec.LOG_MAX_BYTES = 2000
    exe = tmp_path / "chatty"
    exe.write_text("#!/bin/sh\ni=0\nwhile [ $i -lt 200 ]; do echo \"line $i of trunk-recorder output\"; i=$((i+1)); done\n")
    exe.chmod(0o755)
    r = rec.TrunkRecorder(str(exe), str(tmp_path)).start()
    end = time.time() + 5
    while time.time() < end and not (tmp_path / "trunk-recorder.log.1").exists():
        time.sleep(0.05)
    r.stop()
    assert (tmp_path / "trunk-recorder.log.1").exists()
    assert (tmp_path / "trunk-recorder.log").stat().st_size <= 2000 + 200
