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


def settled(ing):
    """A call is taken once its WAV size holds still across two polls."""
    return ing.scan() + ing.scan()


def test_scan_keeps_our_talkgroups_resamples_and_names_units(tmp_path):
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    write_call(day, "307-1789999000_478187500-call_0", 307, [1])  # already there at startup: not replayed
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None, aliases={"2391001": "2391"}).prime()
    write_call(day, "307-1790000000_478187500-call_1", 307, [2391001, 2391001, 6000123])
    write_call(day, "999-1790000001_478187500-call_2", 999, [5])            # a talkgroup not in our list
    (day / "301-1790000002_478312500-call_3.json").write_text(json.dumps({"talkgroup": 301}))  # wav not done yet
    calls = settled(ing)
    # Every talkgroup is ingested (sizeup decides what to keep); unlisted ones are named "TG n".
    assert sorted(c.label for c in calls) == ["Fire/EMS 15", "TG 999"]
    calls = [c for c in calls if c.talkgroup == 307]
    c = calls[0]
    assert c.units == ["2391", "6000123"] and c.talkgroup == 307
    assert len(c.audio) == 32000 and c.audio.dtype == np.float32   # 2 s at 16 kHz
    write_call(day, "301-1790000002_478312500-call_3", 301, [1])
    assert [c.label for c in settled(ing)] == ["60 Control"]           # picked up once the wav lands
    assert ing.scan() == []                                          # each call once


def test_wav_still_being_written_is_waited_for(tmp_path):
    # Seen live: the JSON lands while the WAV is 0 bytes; a moment later it's the full call.
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None).prime()
    (day / "307-1.json").write_text(json.dumps({"talkgroup": 307}))
    (day / "307-1.wav").write_bytes(b"")
    assert ing.scan() == [] and ing.scan() == []          # still empty: wait, don't drop it
    write_call(day, "307-1", 307, [1])                    # recorder finishes the file
    assert ing.scan() == []                               # size just changed: one more poll
    assert [c.label for c in ing.scan()] == ["Fire/EMS 15"]


def test_wav_that_never_fills_is_given_up_without_stopping_ingest(tmp_path, monkeypatch):
    import sizeup.radio.p25 as p25
    monkeypatch.setattr(p25, "SETTLE_GIVE_UP_S", 0.0)
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    events = []
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None, on_event=events.append).prime()
    (day / "307-1.json").write_text(json.dumps({"talkgroup": 307}))
    (day / "307-1.wav").write_bytes(b"")
    write_call(day, "307-2", 307, [1])
    assert [c.label for c in ing.scan()] == ["Fire/EMS 15"]
    assert any("307-1.wav" in e for e in events)


def test_talker_tag_takes_callsign_title(tmp_path):
    # Over the air the duty officer's radio sends "2398"; config names it.
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    meta = {"talkgroup": 307, "srcList": [{"src": 2398001, "tag": "2398"}, {"src": 7077003, "tag": "77M3"}]}
    (day / "307-5.json").write_text(json.dumps(meta))
    from sizeup.radio.p25 import parse_call
    c = parse_call(day / "307-5.json", LABELS, {"2398": "2398 Duty Officer"})
    assert c.units == ["2398 Duty Officer", "77M3"]


def test_turns_follow_srclist_positions(tmp_path):
    day = tmp_path / "westchester" / "2026" / "10" / "1"
    day.mkdir(parents=True)
    meta = {"talkgroup": 307, "srcList": [
        {"src": 600008, "pos": 0.0, "tag": "60 Ctrl OP 8"}, {"src": 41101, "pos": 5.84, "tag": "45M6"},
        {"src": 600008, "pos": 11.76, "tag": "60 Ctrl OP 8"}, {"src": -1, "pos": 12.56, "tag": ""}]}
    (day / "307-9.json").write_text(json.dumps(meta))
    from sizeup.radio.p25 import parse_call
    c = parse_call(day / "307-9.json", LABELS)
    assert c.turns == [(0.0, "60 Ctrl OP 8"), (5.84, "45M6"), (11.76, "60 Ctrl OP 8")]
