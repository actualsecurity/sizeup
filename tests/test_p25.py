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
    # The last key-up has no radio ID: its own turn, talker unknown (issue #62), not the dispatcher's.
    assert [(t.pos, t.talker) for t in c.turns] == [
        (0.0, "60 Ctrl OP 8"), (5.84, "45M6"), (11.76, "60 Ctrl OP 8"), (12.56, "")]


# -- issue #62: one transmission per key-up ---------------------------------------------------------
# Call JSON fixtures in trunk-recorder 5.2.0's shape (call_concluder.cc create_call_json): one srcList
# and one freqList entry per transmission, at the same "pos" (seconds into the WAV, gaps left out);
# "time" whole seconds; src -1 when no radio ID was decoded.

from sizeup.radio.live import LiveTalk                                       # noqa: E402
from sizeup.radio.p25 import Call, Turn, apply_grants, parse_call, split_call  # noqa: E402

T0 = 1_790_954_731.97        # the first key-up, to the ms (start_time_ms)


def tr_call(d, name, txs, tg=307, start=T0, rate=8000):
    """txs: (src, seconds, gap after s, tag[, silent]) per transmission. Writes <name>.json + .wav;
    returns the wall-clock start of each transmission."""
    src_list, freq_list, pcm, starts = [], [], [], []
    at, pos = start, 0.0
    for k, (src, sec, gap, tag, *rest) in enumerate(txs):
        silent = bool(rest and rest[0])
        starts.append(at)
        src_list.append({"src": src, "time": int(at), "pos": round(pos, 2), "emergency": 0, "signal_system": "",
                         "tag": tag, "tag_ota": ""})
        freq_list.append({"freq": 478187500, "time": int(at), "pos": round(pos, 2), "len": sec, "error_count": k,
                          "spike_count": 0})
        n = int(round(sec * rate))
        x = np.zeros(n) if silent else 0.3 * np.sin(2 * np.pi * (300 + 40 * k) * np.arange(n) / rate)
        pcm.append(x)
        pos += sec
        at += sec + gap
    stop = starts[-1] + txs[-1][1]
    meta = {"call_num": 11, "freq": 478187500, "freq_error": 0, "signal": 0, "noise": 0, "source_num": 0,
            "recorder_num": 2, "tdma_slot": 1, "phase2_tdma": 1, "start_time": int(start), "stop_time": int(stop),
            "start_time_ms": int(round(start * 1000)), "stop_time_ms": int(round(stop * 1000)), "emergency": 0,
            "priority": 0, "mode": 0, "duplex": 0, "encrypted": 0, "call_length": int(round(pos)),
            "call_length_ms": int(round(pos * 1000)), "talkgroup": tg, "talkgroup_tag": "Fire/EMS 15",
            "talkgroup_description": "", "talkgroup_group_tag": "", "talkgroup_group": "", "color_code": -1,
            "audio_type": "digital tdma", "short_name": "westchester", "freqList": freq_list, "srcList": src_list}
    (d / f"{name}.json").write_text(json.dumps(meta, indent=2))
    with wave.open(str(d / f"{name}.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes((np.concatenate(pcm) * 32767).astype("<i2").tobytes())
    return starts


def day_dir(tmp_path):
    d = tmp_path / "westchester" / "2026" / "10" / "13"
    d.mkdir(parents=True)
    return d


def ingest(tmp_path, grants=None, aliases=None, alias_of=None):
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None, aliases=aliases).prime()
    if grants is not None:
        ing.grants = lambda system, tg, t0, t1: [g for g in grants if t0 <= g[0] <= t1]
    if alias_of is not None:
        ing.alias_of = alias_of
    return ing


def test_same_radio_keying_twice_is_two_turns_and_two_clips(tmp_path):
    # 77A4 keys, lets go, keys again 0.4 s later; 60 Control answers. One call, three key-ups.
    d = day_dir(tmp_path)
    ing = ingest(tmp_path)
    starts = tr_call(d, "307-1790954731.970_478187500.0-call_11", [
        (41228, 2.5, 0.4, "77A4"), (41228, 1.8, 0.6, "77A4"), (600008, 3.0, 0.0, "60 Ctrl OP 8")])
    [c] = settled(ing)
    assert [(t.pos, t.talker, t.src) for t in c.turns] == [
        (0.0, "77A4", "41228"), (2.5, "77A4", "41228"), (4.3, "60 Ctrl OP 8", "600008")]
    assert [t.length for t in c.turns] == [2.5, 1.8, 3.0] and [t.errors for t in c.turns] == [0, 1, 2]
    assert c.system == "westchester" and c.units == ["77A4", "60 Ctrl OP 8"]
    # wall-clock starts placed from whole-second "time" + the lengths: within half a second
    assert all(abs(t.at - s) <= 0.5 for t, s in zip(c.turns, starts))
    pieces = split_call(c)
    assert [(p.id, p.talker, p.pairs) for p in pieces] == [
        (f"{c.id}-t0", "77A4", [("41228", "77A4")]), (f"{c.id}-t1", "77A4", [("41228", "77A4")]),
        (f"{c.id}-t2", "60 Ctrl OP 8", [("600008", "60 Ctrl OP 8")])]
    assert [round(len(p.audio) / 16000, 2) for p in pieces] == [2.5, 1.8, 3.0]
    assert sum(len(p.audio) for p in pieces) == len(c.audio)                 # nothing lost, nothing doubled
    assert pieces[1].start - pieces[0].start > 2.5                         # the gap between key-ups is real time


def test_key_up_with_no_radio_id_is_its_own_unknown_turn(tmp_path):
    # Two known units with a -1 and a 0 key-up between them: never credited to the one before.
    d = day_dir(tmp_path)
    ing = ingest(tmp_path)
    tr_call(d, "307-a-call_1", [(41228, 2.0, 0.3, "77A4"), (-1, 1.5, 0.3, ""), (0, 1.2, 0.3, ""),
                                (2391001, 2.0, 0.0, "2391")])
    [c] = settled(ing)
    assert [(t.talker, t.src) for t in c.turns] == [("77A4", "41228"), ("", ""), ("", ""), ("2391", "2391001")]
    assert c.units == ["77A4", "2391"] and "" not in c.ids
    pieces = split_call(c)
    assert [(p.id[-3:], p.talker, p.pairs, p.stt) for p in pieces] == [
        ("-t0", "77A4", [("41228", "77A4")], True), ("-t1", "", [], True), ("-t2", "", [], True),
        ("-t3", "2391", [("2391001", "2391")], True)]


def test_freqlist_entry_without_a_source_is_a_key_up_too(tmp_path):
    # Defensive: a transmission in freqList (with "len") that srcList doesn't list is still a boundary.
    d = day_dir(tmp_path)
    meta = {"talkgroup": 307, "start_time": 1790000000, "stop_time": 1790000006,
            "srcList": [{"src": 41228, "time": 1790000000, "pos": 0.0, "tag": "77A4"}],
            "freqList": [{"freq": 478187500, "time": 1790000000, "pos": 0.0, "len": 3.0, "error_count": 0},
                         {"freq": 478187500, "time": 1790000003, "pos": 3.0, "len": 3.0, "error_count": 7}]}
    (d / "307-f.json").write_text(json.dumps(meta))
    c = parse_call(d / "307-f.json", LABELS)
    assert [(t.pos, t.talker, t.length, t.errors) for t in c.turns] == [(0.0, "77A4", 3.0, 0), (3.0, "", 3.0, 7)]


def test_live_grant_names_the_key_up_with_no_radio_id(tmp_path):
    d = day_dir(tmp_path)
    grants = []
    ing = ingest(tmp_path, grants, aliases={"41228": "77A4"}, alias_of={"34701": "45M6"}.get)
    starts = tr_call(d, "307-g-call_2", [(41228, 2.0, 0.5, "77A4"), (-1, 2.2, 0.5, ""), (600008, 2.0, 0.0, "60 Ctrl")])
    # the control channel granted 41228, then 34701 just before the unknown key-up, then 600008
    grants += [(starts[0] - 0.4, "41228"), (starts[1] - 0.3, "34701"), (starts[2] - 0.5, "600008")]
    [c] = settled(ing)
    assert [(t.talker, t.src, t.via) for t in c.turns] == [
        ("77A4", "41228", "srclist"), ("45M6", "34701", "grant"), ("60 Ctrl", "600008", "srclist")]
    assert c.units == ["77A4", "45M6", "60 Ctrl"]
    assert [p.pairs for p in split_call(c)] == [[("41228", "77A4")], [("34701", "45M6")], [("600008", "60 Ctrl")]]


def test_grant_never_renames_a_key_up_the_voice_channel_named(tmp_path):
    d = day_dir(tmp_path)
    starts = tr_call(d, "307-h-call_3", [(41228, 2.0, 0.2, "77A4"), (2391001, 2.0, 0.0, "2391")])
    c = parse_call(d / "307-h-call_3.json", LABELS)
    # a stray grant for someone else right at the second key-up
    assert not apply_grants(c, [(starts[1] - 0.2, "34701")])
    assert [t.talker for t in c.turns] == ["77A4", "2391"]


def test_run_on_transmission_is_cut_where_the_next_radio_was_granted(tmp_path):
    # No terminator decoded between 77A4 and 60 Control: trunk-recorder records one 9 s transmission
    # and credits it to the last radio it heard (600008). The grants say 77A4 keyed first.
    d = day_dir(tmp_path)
    grants = []
    ing = ingest(tmp_path, grants, alias_of={"41228": "77A4"}.get)
    starts = tr_call(d, "307-r-call_4", [(600008, 9.0, 0.0, "60 Ctrl OP 8")])
    grants += [(starts[0] - 0.5, "41228"), (starts[0] + 4.0, "600008")]
    [c] = settled(ing)
    assert [(t.pos, t.talker, t.src, t.via) for t in c.turns] == [
        (0.0, "77A4", "41228", "grant"), (4.5, "60 Ctrl OP 8", "600008", "srclist")]
    assert [t.length for t in c.turns] == [4.5, 4.5]
    pieces = split_call(c)
    assert [(p.id[-3:], p.talker) for p in pieces] == [("-t0", "77A4"), ("-t1", "60 Ctrl OP 8")]
    # with no grant before it the first part is nobody's: unknown, not the dispatcher's
    c2 = parse_call(d / "307-r-call_4.json", LABELS)
    assert apply_grants(c2, [(starts[0] + 4.0, "600008")])
    assert [(t.talker, t.src) for t in c2.turns] == [("", ""), ("60 Ctrl OP 8", "600008")]
    # the radio that opened it granted again inside it (a repeat): nothing to cut
    c4 = parse_call(d / "307-r-call_4.json", LABELS)
    assert not apply_grants(c4, [(starts[0] - 0.5, "600008"), (starts[0] + 4.0, "600008")])
    assert [(t.talker, t.via) for t in c4.turns] == [("60 Ctrl OP 8", "srclist")]
    # a grant for a third radio inside it: the voice channel says otherwise, left alone
    c3 = parse_call(d / "307-r-call_4.json", LABELS)
    assert not apply_grants(c3, [(starts[0] + 4.0, "34701")])


def test_short_key_ups_kept_silent_unknown_fragment_merged():
    rate = 16000
    sound = lambda s: (0.3 * np.sin(np.arange(int(s * rate)) / 3)).astype(np.float32)   # noqa: E731
    quiet = lambda s: np.zeros(int(s * rate), np.float32)                              # noqa: E731
    audio = np.concatenate([sound(2.0), quiet(0.2), quiet(0.2), sound(0.2), sound(2.0)])
    c = Call("307-s", 307, "Fire/EMS 15", 100.0, 105.0, ["77A4", "2391"], audio, "", [
        Turn(0.0, "77A4", "41228", "77A4"),
        Turn(2.0, "", ""),                          # 0.2 s, no ID, no sound: a fragment -> into 77A4's
        Turn(2.2, "2391", "2391001", "2391"),       # 0.2 s, an ID, no sound: a key-up, not transcribed
        Turn(2.4, "", ""),                          # 0.2 s, no ID, sound: kept and transcribed
        Turn(2.6, "77A4", "41228", "77A4")])
    pieces = split_call(c)
    assert [(p.talker, round(len(p.audio) / rate, 2), p.stt) for p in pieces] == [
        ("77A4", 2.2, True), ("2391", 0.2, False), ("", 0.2, True), ("77A4", 2.0, True)]
    assert [p.id for p in pieces] == [f"307-s-t{k}" for k in range(4)]
    # a leading fragment goes to the key-up after it
    c = Call("307-u", 307, "Fire/EMS 15", 100.0, 102.0, ["77A4"], np.concatenate([quiet(0.1), sound(2.0)]), "",
             [Turn(0.0, "", ""), Turn(0.1, "77A4", "41228", "77A4")])
    assert [(p.id, p.talker, len(p.audio)) for p in split_call(c)] == [("307-u", "77A4", int(2.1 * rate))]


def test_live_keeps_grants_for_the_call_turns():
    now = [1000.0]
    lt = LiveTalk(clock=lambda: now[0])
    lt.feed_line("SIZEUP-UNIT westchester 41228 call 307")
    now[0] += 0.3          # trunk-recorder's own log line for the same grant: one grant
    lt.feed_line("[2026-10-13 10:00:00.300] [westchester]\t8C\tTG:    307\tFreq: 478.187500 MHz\t"
                 "Unit ID set via Control Channel, ext: 41228\tcurrent: -1\t samples: 0")
    now[0] += 4.0
    lt.feed_line("[westchester]\t8C\tTG:    307\tFreq: 478.187500 MHz\t"
                 "Unit ID externally set from GRANT: 600008\t caching, doesn't match current: 41228")
    now[0] += 1.0
    lt.feed_line("SIZEUP-UNIT westchester 34701 call 301")
    lt.feed_line("SIZEUP-UNIT westchester -1 call 307")
    assert lt.grants("westchester", 307, 0, 2000) == [(1000.0, "41228"), (1004.3, "600008")]
    assert lt.grants("", 301, 0, 2000) == [(1005.3, "34701")] and lt.grants("other", 307, 0, 2000) == []
    assert lt.grants("westchester", 307, 1001, 2000) == [(1004.3, "600008")]


def test_grants_while_the_last_talker_is_still_keyed(tmp_path):
    # Stepping on each other: each next radio is granted while the one before is still talking, and
    # neither of the last two decoded an ID on the voice channel.
    d = day_dir(tmp_path)
    starts = tr_call(d, "307-k-call_5", [(41228, 2.0, 0.1, "77A4"), (-1, 1.0, 0.1, ""), (-1, 2.0, 0.0, "")])
    names = {"34701": "45M6", "2391001": "2391"}
    c = parse_call(d / "307-k-call_5.json", LABELS, names)
    grants = [(starts[0] - 0.6, "41228"), (starts[1] - 1.2, "34701"), (starts[2] - 0.9, "2391001")]
    assert apply_grants(c, grants, names)
    assert [(t.talker, t.via) for t in c.turns] == [("77A4", "srclist"), ("45M6", "grant"), ("2391", "grant")]


def test_p25_rows_per_key_up_in_the_app_and_comms(tmp_path):
    from sizeup.app import App
    from tests.test_app import FakeWhisper, cfg, voice, wait
    from tests.test_radios import Clock, _event_with, cad, tx
    from dataclasses import replace
    import time as _t

    said = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            said.append(prompt)
            return "words", 0.9

    app = App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}))
    app.stt.client = Spy({})
    app.stt.start()
    now = _t.time()
    audio = np.concatenate([voice(2.0, 1), voice(2.0, 2), np.zeros(int(0.2 * 16000), np.float32), voice(2.0, 3),
                            np.zeros(int(0.1 * 16000), np.float32)])
    app._call(Call("307-q-call_7", 307, "Fire/EMS 15", now, now + 6.3, ["77A4", "2391"], audio, "", [
        Turn(0.0, "77A4", "41228", "77A4", at=now), Turn(2.0, "77A4", "41228", "77A4", at=now + 2.5),
        Turn(4.0, "2391", "2391001", "2391", at=now + 4.6), Turn(4.2, "", "", at=now + 4.8),
        Turn(6.2, "", "", at=now + 6.9)]))            # a 0.1 s fragment, no ID, no sound: into the one before
    assert wait(lambda: len(app.store.loose) == 4)
    rows = sorted(app.store.loose, key=lambda t: t.id)
    assert [(t.id[-3:], t.unit, t.radio_id, t.status) for t in rows] == [
        ("-t0", "77A4", "41228", "done"), ("-t1", "77A4", "41228", "done"),
        ("-t2", "2391", "2391001", "no_speech"), ("-t3", "", "", "done")]
    assert len(said) == 3                                               # the 0.2 s silent key-up: no STT
    assert round(rows[3].end - rows[3].start, 2) == 2.1
    assert rows[1].start == now + 2.5 and all(app.clip(t.id)[:4] == b"RIFF" for t in rows)

    # Hub side: 60 Control answering E60 across an unknown radio's key-up still links to E60's event,
    # whichever of the three comes in first; the unknown one itself goes on no event.
    clock = Clock()
    c = cad(tmp_path / "hub", clock)
    a = _event_with(c, ["E60"], clock)
    clock.t += 5
    t = clock.t
    assert c.record_traffic(tx("307-9-call_4-t0", t, "Fire/EMS 15", "E60", "E60", 307, radio_id="6001")) == [a["id"]]
    assert c.record_traffic(tx("307-9-call_4-t1", t + 2, "Fire/EMS 15", "", "", 307)) == []
    assert c.record_traffic(tx("307-9-call_4-t2", t + 4, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == [a["id"]]
    b = _event_with(c, ["E61"], clock)
    clock.t += 5
    t = clock.t
    assert c.record_traffic(tx("307-8-call_2-t2", t + 4, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == []
    assert c.record_traffic(tx("307-8-call_2-t0", t, "Fire/EMS 15", "E61", "E61", 307)) == [b["id"]]
    assert c.record_traffic(tx("307-8-call_2-t1", t + 2, "Fire/EMS 15", "", "", 307)) == []   # bridges the two
    assert {r["id"]: r["why"] for r in c.full(b["id"])["radio"]} == {"307-8-call_2-t0": "unit",
                                                                     "307-8-call_2-t2": "dispatcher"}
    # ... but a turn not logged yet stops the look (it may be the unit really answered)
    d = _event_with(c, ["E63"], clock)
    clock.t += 5
    t = clock.t
    c.record_traffic(tx("307-7-call_3-t0", t, "Fire/EMS 15", "E63", "E63", 307))
    assert c.record_traffic(tx("307-7-call_3-t2", t + 4, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == []
