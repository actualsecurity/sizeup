"""The grant split (#819): a turn the voice channel credits to one radio, cut where the control channel granted another
radio mid-call (trunk-recorder's "Unit ID externally set from GRANT"), at the quietest gap near the grant, or at the
grant less its latency when there is none. Synthetic calls in trunk-recorder 5.2.0's JSON shape, audio of one tone per
talker (gaps of digital silence where a talker let go, a talker's own pause as low noise), grant events at invented
times, made-up radio IDs."""

from __future__ import annotations

import json
import re
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from sizeup.radio import systems, trunkrecd
from sizeup.radio.live import LiveTalk, LogEvents
from sizeup.radio.p25 import (GRANT_CUT, CallIngest, GrantCount, GrantSplit, apply_grant_cuts, parse_call,
                              plan_grant_cuts, read_wav_16k, split_call)
from tests.test_app import wait
from tests.test_p25 import T0, day_dir
from tests.test_p25_systems import MRRS, PD, TR, rows, two  # noqa: F401 (fixture)

A, B, C = "9900101", "9900102", "9900103"     # made-up radio IDs
IDS = (A, B, C)
RATE8 = 8000
LABELS = {PD: "PD Dispatch", 307: "Fire/EMS 15"}
HZ = {A: 300.0, B: 520.0, C: 760.0}


# -- the audio and the call as trunk-recorder writes them ---------------------------------------------------------------

def audio(*parts) -> np.ndarray:
    """("talk", seconds, radio) a tone; ("gap", seconds) digital silence (a key-up's start, a release);
    ("pause", seconds) a talker's own pause: low noise, quiet but not silent."""
    out, rng = [], np.random.default_rng(7)
    for kind, sec, *who in parts:
        n = int(round(sec * RATE8))
        if kind == "talk":
            out.append(0.3 * np.sin(2 * np.pi * HZ[who[0]] * np.arange(n) / RATE8))
        elif kind == "pause":
            out.append(rng.uniform(-0.003, 0.003, n))
        else:
            out.append(np.zeros(n))
    return np.concatenate(out)


def write(d: Path, name: str, pcm: np.ndarray, srcs, tg: int = PD, start: float = T0, emergency: bool = False,
          system: str = "mrrs") -> Path:
    """<name>.json + .wav: srcs [(radio, pos)], one transmission each (the audio holds them back to back)."""
    length = len(pcm) / RATE8
    poss = [p for _s, p in srcs] + [length]
    src_list = [{"src": int(s), "time": int(start + p), "pos": p, "emergency": 0, "signal_system": "", "tag": "",
                 "tag_ota": ""} for s, p in srcs]
    freq_list = [{"freq": 770500000, "time": int(start + p), "pos": p, "len": round(poss[k + 1] - p, 2),
                  "error_count": 0, "spike_count": 0} for k, (_s, p) in enumerate(srcs)]
    meta = {"talkgroup": tg, "start_time": int(start), "stop_time": int(start + length),
            "start_time_ms": int(round(start * 1000)), "stop_time_ms": int(round((start + length) * 1000)),
            "call_length": int(round(length)), "call_length_ms": int(round(length * 1000)), "freq": 770500000,
            "emergency": int(emergency), "encrypted": 0, "short_name": system, "srcList": src_list,
            "freqList": freq_list}
    (d / f"{name}.json").write_text(json.dumps(meta))
    with wave.open(str(d / f"{name}.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE8)
        w.writeframes((pcm * 32767).astype("<i2").tobytes())
    return d / f"{name}.json"


def call_of(path: Path, events, mode: str = "on", **tune):
    """The call parsed and cut as CallIngest does it (its _grant_split), with these events; (call, cuts, said)."""
    said: list[str] = []
    ing = CallIngest(path.parent, LABELS, on_call=lambda c: None, on_event=said.append, name="MRRS")
    ing.grant_split = GrantSplit(mode, **tune)
    ing.cut_events = lambda s, tg, t0, t1: [e for e in events if t0 <= e[0] <= t1]
    c = parse_call(path, LABELS)
    c.audio = read_wav_16k(path.with_suffix(".wav"))
    cuts = plan_grant_cuts(c, ing.cut_events(c.system, c.talkgroup, c.start - 2, c.end + 2), ing.grant_split)
    ing._grant_split(c)
    return c, cuts, said


def tone_of(x: np.ndarray) -> float:
    """The loudest frequency of a clip (16 kHz)."""
    f = np.abs(np.fft.rfft(x))
    return float(np.argmax(f) * 16000 / len(x))


def no_ids(said) -> bool:
    return not any(i in x for x in said for i in IDS) and not any(re.search(r"(?<![\d.])\d{5,}", x) for x in said)


# -- where it cuts ------------------------------------------------------------------------------------------------------

def test_a_then_b_by_a_grant_alone_is_cut_at_the_gap_and_b_is_credited(tmp_path):
    d = day_dir(tmp_path)
    # A talks (a pause of its own, quiet, inside the window too), lets go; B keys with no ID on the voice channel
    pcm = audio(("talk", 2.3, A), ("pause", 0.3), ("talk", 0.4, A), ("gap", 0.25), ("talk", 2.75, B))
    p = write(d, "9211-1-call_1", pcm, [(A, 0.0)])
    c, cuts, said = call_of(p, [(T0 + 3.25 + 0.3, B, "grant")])
    assert len(cuts) == 1 and 3.0 <= cuts[0].pos <= 3.25 and cuts[0].gap >= 0.2      # the silence, never the pause
    assert [(t.src, t.via) for t in c.turns] == [(A, "srclist"), (B, GRANT_CUT)]
    pieces = split_call(c)
    assert [(pc.id[-3:], pc.talker, pc.pairs, pc.via) for pc in pieces] == [
        ("-t0", A, [(A, A)], "srclist"), ("-t1", B, [(B, B)], GRANT_CUT)]
    assert abs(tone_of(pieces[0].audio) - HZ[A]) < 5 and abs(tone_of(pieces[1].audio) - HZ[B]) < 5
    assert pieces[0].end == pieces[1].start and abs(pieces[1].start - (T0 + cuts[0].pos)) < 0.01
    assert said == [f"p25 MRRS: grant split (on): PD Dispatch: cut at {cuts[0].pos:.1f} of 6.0 s, "
                    f"gap {cuts[0].gap:.2f} s ({cuts[0].pos - 3.55:+.1f} s from the grant)"] and no_ids(said)


def test_a_b_and_c_each_get_a_part_and_a_grant_update_of_the_talker_is_no_cut(tmp_path):
    d = day_dir(tmp_path)
    pcm = audio(("talk", 2.5, A), ("gap", 0.2), ("talk", 2.5, B), ("gap", 0.3), ("talk", 2.0, C))
    p = write(d, "9211-2-call_2", pcm, [(A, 0.0)])
    ev = [(T0 + 3.0, B, "grant"), (T0 + 3.6, B, "grant"), (T0 + 4.4, B, "grant"),     # B's grant, then its updates
          (T0 + 5.8, C, "grant"), (T0 + 7.0, C, "grant")]
    c, cuts, _said = call_of(p, ev)
    assert [x.src for x in cuts] == [B, C] and [t.src for t in c.turns] == [A, B, C]
    pieces = split_call(c)
    assert [pc.id[-3:] for pc in pieces] == ["-t0", "-t1", "-t2"]
    assert [round(tone_of(pc.audio)) for pc in pieces] == [round(HZ[A]), round(HZ[B]), round(HZ[C])]
    assert 2.5 <= cuts[0].pos <= 2.7 and 5.2 <= cuts[1].pos <= 5.5


def test_a_grant_repeating_the_talker_and_one_inside_half_a_second_cut_nothing(tmp_path):
    d = day_dir(tmp_path)
    pcm = audio(("talk", 3.0, A), ("gap", 0.25), ("talk", 2.75, A))
    p = write(d, "9211-3-call_3", pcm, [(A, 0.0)])
    c, cuts, said = call_of(p, [(T0 + 3.5, A, "grant")])             # A again (its grant update): its own turn
    assert cuts == [] and [t.src for t in c.turns] == [A] and said == []
    c, cuts, _ = call_of(p, [(T0 + 0.3, B, "grant")])                # 0.3 s into A's turn: its own key-up
    assert cuts == [] and len(c.turns) == 1
    # ... and 0.3 s after B's own cut start, C is no new talker either
    pcm = audio(("talk", 3.0, A), ("gap", 0.25), ("talk", 3.0, B))
    p = write(d, "9211-4-call_4", pcm, [(A, 0.0)])
    c, cuts, _ = call_of(p, [(T0 + 3.4, B, "grant"), (T0 + 3.4 + 0.3, C, "grant")])
    assert [x.src for x in cuts] == [B]


def test_a_grant_with_no_gap_is_cut_at_the_grant_less_its_latency(tmp_path):
    d = day_dir(tmp_path)
    pcm = audio(("talk", 3.0, A), ("talk", 3.0, B))                  # B straight after A: not one quiet frame
    p = write(d, "9211-5-call_5", pcm, [(A, 0.0)])
    c, cuts, said = call_of(p, [(T0 + 3.3, B, "grant")])
    assert [(x.pos, x.gap) for x in cuts] == [(3.0, 0.0)]             # 3.3 - latency_s (0.3)
    pieces = split_call(c)
    assert [round(tone_of(pc.audio)) for pc in pieces] == [round(HZ[A]), round(HZ[B])]
    assert "no gap" in said[0]
    _c, cuts, _ = call_of(p, [(T0 + 3.3, B, "grant")], latency_s=0.5)          # tunable
    assert cuts[0].pos == 2.8


def test_no_part_shorter_than_its_minimum(tmp_path):
    d = day_dir(tmp_path)
    p = write(d, "9211-6-call_6", audio(("talk", 0.7, A)), [(A, 0.0)])
    c, cuts, _ = call_of(p, [(T0 + 0.55, B, "grant")])               # 0.4 s on each side would not fit
    assert cuts == [] and len(c.turns) == 1


def test_a_voice_id_cut_and_a_grant_together_are_one_cut(tmp_path):
    d = day_dir(tmp_path)
    pcm = audio(("talk", 3.0, A), ("talk", 3.0, B))
    # the patch on (#816): B's voice-channel ID made its own srcList entry; B's grant came just before
    p = write(d, "9211-7-call_7", pcm, [(A, 0.0), (B, 3.0)])
    c, cuts, said = call_of(p, [(T0 + 2.6, B, "grant"), (T0 + 3.4, B, "grant")])
    assert cuts == [] and [t.src for t in c.turns] == [A, B] and said == []
    # the patch on "log" (#647): one entry, its "would split" (a voice event) where B came in
    p = write(d, "9211-8-call_8", pcm, [(A, 0.0)])
    c, cuts, _ = call_of(p, [(T0 + 3.0, B, "voice"), (T0 + 3.3, B, "grant")])
    assert cuts == [] and len(c.turns) == 1
    # ... while a voice event further than voice_s from the grant is another key-up's
    c, cuts, _ = call_of(p, [(T0 + 1.5, C, "voice"), (T0 + 3.3, B, "grant")])
    assert [x.src for x in cuts] == [B]


def test_log_mode_says_and_counts_each_cut_and_changes_nothing(tmp_path):
    d = day_dir(tmp_path)
    pcm = audio(("talk", 3.0, A), ("gap", 0.25), ("talk", 2.75, B))
    p = write(d, "9211-9-call_9", pcm, [(A, 0.0)])
    said: list[str] = []
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None, on_event=said.append, name="MRRS").prime()
    ing.grant_split = GrantSplit("log")
    ing.cut_events = lambda s, tg, t0, t1: [(T0 + 3.55, B, "grant")] if (s, tg) == ("mrrs", PD) else []
    count = tmp_path / "state" / "p25_grant_cuts.mrrs.json"
    count.parent.mkdir()
    ing.grant_count = GrantCount(count, "mrrs")
    ing._seen.discard(str(p))
    got = ing.scan() + ing.scan()
    assert len(got) == 1 and [t.src for t in got[0].turns] == [A]
    assert [pc.id for pc in split_call(got[0])] == ["9211-9-call_9"]          # one row, as before
    assert len(said) == 1 and said[0].startswith("p25 MRRS: grant split (log): PD Dispatch: would cut at 3.1 of 6.0 s")
    assert no_ids(said)
    assert json.loads(count.read_text())["cuts"] == 1 and json.loads(count.read_text())["mode"] == "log"
    assert GrantCount(count, "mrrs").cuts == 1                      # kept over a restart
    # off: nothing looked at
    said.clear()
    ing.grant_split = GrantSplit("off")
    p2 = write(d, "9211-10-call_10", pcm, [(A, 0.0)])
    ing._seen.discard(str(p2))
    assert len(ing.scan() + ing.scan()) == 1 and said == []


def test_the_hourly_split_line_adds_the_hours_grant_cuts(tmp_path):
    from tests.test_trunk_split_counts import start, would
    said: list[str] = []
    state = tmp_path / "p25_recorder.mrrs.json"
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.grant_path = trunkrecd.grant_cuts_path("mrrs", str(state))
    assert Path(sh.grant_path).name == "p25_grant_cuts.mrrs.json"
    GrantCount(sh.grant_path, "mrrs").add(4, "log")                  # before this hour: never counted in it
    sh.started("on", 100.0)
    sh.tick(100.0)
    for call in range(1, 21):
        sh.line(start("mrrs", call), 100.0 + call)
    sh.line(would("mrrs", 3), 130.0)
    g = GrantCount(sh.grant_path, "mrrs")
    g.add(2, "log")
    g.add(1, "log")
    sh.tick(100.0 + 3600.5)
    assert said == ["p25 mrrs: split (on) last hour: 0 splits, 0 ID flips, 0 aliases, in 20 calls (5.0% of calls), "
                    "3 grant cuts (log)"]
    sh.line(start("mrrs", 21), 3800.0)
    sh.split_flush(3900.0)
    assert said[-1].endswith("in 1 calls (0.0% of calls), 0 grant cuts (log)") and no_ids(said)
    # the county's supervisor and a system's read their own file
    sup = trunkrecd.Supervisor(state=str(tmp_path / "p25_recorder.json"), log=said.append)
    assert Path(sup.shipper.grant_path).name == "p25_grant_cuts.json"
    sys_ = trunkrecd.SystemSupervisor("mrrs", state=str(tmp_path / "p25_recorder.json"), log=said.append)
    assert sys_.shipper.grant_path == str(tmp_path / "p25_grant_cuts.mrrs.json")


# -- where the events come from -----------------------------------------------------------------------------------------

def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1e6):06d}"


def tr_line(t: float, msg: str, sys_: str = "mrrs", tg: int = PD) -> str:
    return (f"[{stamp(t)}] (error)   [{sys_}]\t\x1b[0;34m12C\x1b[0m\tTG: \x1b[35m{tg:>10}\x1b[0m\t"
            f"Freq: 770.500000 MHz\t{msg}")


def test_live_talk_keeps_mid_call_grants_and_voice_cuts_at_trunk_recorders_time():
    now = [time.time()]
    lt = LiveTalk(clock=lambda: now[0])
    t = now[0] - 0.4                                      # read 0.4 s after trunk-recorder stamped it
    lt.feed_line(tr_line(t, f"Unit ID externally set from GRANT: {B}\t caching, doesn't match current: {A}"))
    lt.feed_line(tr_line(t + 1, f"Unit ID changed on Voice Channel, ext: {C}\tprevious: {B}\t samples: 24000"))
    lt.feed_line(tr_line(t + 2, f"sizeup split (log): would split: prev {C} -> new {A}, 24000 samples into "
                                f"transmission"))
    lt.feed_line(f"SIZEUP-UNIT mrrs {C} call {PD}")       # the unit script's: no time stamp, when it was read
    lt.feed_line(tr_line(t, f"Unit ID externally set from GRANT: {B}\t caching", tg=PD + 1))
    got = lt.cut_events("mrrs", PD, 0, now[0] + 10)
    assert [(round(a - t, 3), s, k) for a, s, k in got] == [(0.0, B, "grant"), (0.4, C, "grant"), (1.0, C, "voice"),
                                                         (2.0, A, "voice")]
    assert lt.cut_events("westchester", PD, 0, now[0] + 10) == []
    # a stamp far from the clock (another time zone, a stepped clock): the time it was read
    lt.feed_line(tr_line(now[0] - 7200, f"Unit ID externally set from GRANT: {C}\t caching", tg=PD + 2))
    assert lt.cut_events("mrrs", PD + 2, 0, now[0] + 10) == [(now[0], C, "grant")]


def test_trunk_recorders_log_gives_a_late_call_its_events(tmp_path):
    t = time.time() - 120
    log = tmp_path / trunkrecd.LOG
    old = tmp_path / (trunkrecd.LOG + ".1")
    old.write_text(tr_line(t, f"Unit ID externally set from GRANT: {B}\t caching, doesn't match current: {A}") + "\n")
    log.write_text(f"SIZEUP-UNIT mrrs {C} call {PD}\n" + tr_line(t + 2, f"Unit ID changed on Voice Channel, ext: {C}")
                   + "\n" + tr_line(t + 3, "Unit ID externally set from GRANT: 9900104", tg=PD + 1) + "\n")
    ev = LogEvents([old, log])
    got = ev.events("mrrs", PD, t - 5, t + 5)
    assert [(round(a - t, 3), s, k) for a, s, k in got] == [(0.0, B, "grant"), (2.0, C, "voice")]
    with open(log, "a") as f:                             # what is added later is read on its own
        f.write(tr_line(t + 4, f"Unit ID externally set from GRANT: {A}\t caching") + "\n" + tr_line(t + 5, "half"))
    assert [s for _a, s, _k in ev.events("mrrs", PD, t - 5, t + 5)] == [B, C, A]
    assert ev.events("other", PD, t - 5, t + 5) == [] and LogEvents([tmp_path / "none"]).events("mrrs", PD, 0, 1e12) == []


def test_a_late_call_is_grant_split_from_the_log_and_its_replay_is_not_taken_twice(two, tmp_path):
    app, said, logs = two
    rt = app.systems["mrrs"]
    base = Path(rt.system.capture_dir)
    d = base / "mrrs" / "2026" / "11" / "19"
    d.mkdir(parents=True, exist_ok=True)
    mark = tmp_path / "state" / "p25_mrrs_ingest_mark.json"
    got: list = []
    first = CallIngest(base, rt.system.labels(), on_call=got.append, mark_path=mark).resume()   # the mark, as before
    first.stop()
    before = mark.read_text()
    start = time.time() - 60                              # written while sizeup was down
    pcm = audio(("talk", 3.0, A), ("gap", 0.25), ("talk", 2.75, B))
    p = write(d, f"{PD}-11-call_11", pcm, [(A, 0.0)], start=start)
    log = Path(rt.system.dir) / trunkrecd.LOG
    log.write_text(tr_line(start + 3.55, f"Unit ID externally set from GRANT: {B}\t caching, doesn't match current: "
                                         f"{A}") + "\n")

    def again():
        ing = CallIngest(base, rt.system.labels(), on_call=got.append, mark_path=mark, name="MRRS",
                         on_event=logs.append)
        ing.record_all = False
        systems.wire_grant_split(ing, rt.live, {"grant_split": "on"}, rt.system.dir, None, "mrrs", "mrrs")
        ing.resume()
        return ing.poll() + ing.poll()

    late = again()
    assert [(c.id, c.late, [t.src for t in c.turns]) for c in late] == [(p.stem, True, [A, B])]
    assert any("grant split (on): PD Dispatch: cut at 3.1 of 6.0 s" in x and x.endswith("(late)") for x in logs)
    files = {x: x.read_bytes() for x in (p, p.with_suffix(".wav"))}
    app._call_system(rt, late[0])                         # (its files go once taken)
    assert wait(lambda: all(r["status"] != "pending" for r in rows(app).values()) and len(rows(app)) == 2)
    r = rows(app)
    assert {k[-3:]: (v["radio_id"], v["cut"]) for k, v in r.items()} == {"-t0": (A, ""), "-t1": (B, "grant")}
    assert all(getattr(t, "late", False) for t in app.store.loose)
    # the mark never moved (a crash between two writes): the same call comes in late again, and is dropped by its id
    mark.write_text(before)
    for x, b in files.items():                            # (... and were still there: the app stopped before that)
        x.write_bytes(b)
    replay = again()
    assert [c.id for c in replay] == [p.stem]
    app._call_system(rt, replay[0])
    assert len(rows(app)) == 2 and any("taken before the restart: not again" in x for x in logs)


# -- the rows ------------------------------------------------------------------------------------------------------------

def test_on_makes_each_part_a_row_with_its_radio_its_text_and_its_intel(two, tmp_path):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    heard: list[str] = []
    app.cad.comms.on_context_text = heard.append                    # the intel pre-alerts' hook
    d = day_dir(tmp_path / "cap")
    now = time.time()
    pcm = audio(("talk", 2.5, A), ("gap", 0.2), ("talk", 2.5, B), ("gap", 0.3), ("talk", 2.0, C))
    p = write(d, f"{PD}-12-call_12", pcm, [(A, 0.0)], start=now)
    c, cuts, _ = call_of(p, [(now + 3.0, B, "grant"), (now + 5.8, C, "grant")])
    assert len(cuts) == 2
    app._call_system(rt, c)
    cid = p.stem
    assert wait(lambda: len(heard) == 3)
    r = rows(app)
    assert {k: (v["radio_id"], v["talker"], v["cut"], v["call_id"], v["turn"], v["status"]) for k, v in r.items()} == {
        f"{cid}-t0": (A, A, "", cid, 0, "done"), f"{cid}-t1": (B, B, "grant", cid, 1, "done"),
        f"{cid}-t2": (C, C, "grant", cid, 2, "done")}
    assert sorted(heard) == sorted(r) and len(said) == 3            # one speech-to-text job each
    assert r[f"{cid}-t0"]["end"] == pytest.approx(r[f"{cid}-t1"]["start"], abs=0.01)
    assert all(app.clip(k)[:4] == b"RIFF" for k in r)


def test_on_an_emergency_call_only_its_first_part_carries_the_flag(two, tmp_path):
    app, _said, _logs = two
    d = day_dir(tmp_path / "cap")
    now = time.time()
    pcm = audio(("talk", 3.0, A), ("gap", 0.25), ("talk", 2.75, B))
    p = write(d, "307-13-call_13", pcm, [(A, 0.0)], tg=307, start=now, emergency=True, system="westchester")
    c, cuts, _ = call_of(p, [(now + 3.55, B, "grant")])
    assert len(cuts) == 1 and c.emergency
    app._call(c)
    assert wait(lambda: len(rows(app)) == 2 and all(v["status"] != "pending" for v in rows(app).values()))
    r = rows(app)
    assert {k[-3:]: (v["emergency"], v["cut"]) for k, v in r.items()} == {"-t0": (1, ""), "-t1": (0, "grant")}


# -- the setting --------------------------------------------------------------------------------------------------------

def test_mrrs_ships_log_and_the_setting_reads_safely(tmp_path):
    assert MRRS.grant == {"grant_split": "log"} and GrantSplit.of(MRRS.grant).mode == "log"
    county = json.loads((TR / "config.json").read_text())["sizeup"]
    assert GrantSplit.of(county).mode == "off"                       # the county: off until its own review
    assert GrantSplit.of({"grant_split": "yes"}).mode == "off" and GrantSplit.of(None).mode == "off"
    g = GrantSplit.of({"grant_split": "on", "grant_split_tune": {"before_s": 2, "latency_s": 0.4, "after_s": "x",
                                                                   "min_gap_s": 9, "settle_s": 0.1}})
    assert (g.mode, g.before_s, g.latency_s, g.after_s, g.min_gap_s, g.settle_s) == ("on", 2.0, 0.4, 0.5, 0.15, 0.5)


def test_changing_the_grant_split_restarts_no_trunk_recorder(tmp_path):
    conf = json.loads((TR / "systems" / "mrrs" / "config.json").read_text())
    f = tmp_path / "config.json"

    def sig(c):
        f.write_text(json.dumps(c))
        trunkrecd._SIGS.clear()
        return trunkrecd._file_sig(f)

    base = sig(conf)
    assert sig({**conf, "sizeup": {**conf["sizeup"], "grant_split": "on"}}) == base
    assert sig({**conf, "sizeup": {**conf["sizeup"], "grant_split_tune": {"latency_s": 0.5}}}) == base
    assert sig({**conf, "sizeup": {**conf["sizeup"], "split": "on"}}) != base       # the patch's: trunk-recorder's
    without = dict(conf, sizeup={k: v for k, v in conf["sizeup"].items() if k != "grant_split"})
    f.write_text(json.dumps(without))
    trunkrecd._SIGS.clear()
    assert trunkrecd._sig_bytes(f) == f.read_bytes()                  # no grant keys: the file as it is, as before


def test_a_station_runtime_counts_beside_its_state_and_a_bench_does_not(tmp_path):
    class Rec:
        state_path = str(tmp_path / "p25_recorder.mrrs.json")
    ing = CallIngest(tmp_path, LABELS, on_call=lambda c: None)
    systems.wire_grant_split(ing, LiveTalk(), {"grant_split": "log"}, tmp_path, Rec.state_path, "mrrs", "mrrs")
    assert ing.grant_split.mode == "log" and ing.grant_count.path == tmp_path / "p25_grant_cuts.mrrs.json"
    bench = CallIngest(tmp_path, LABELS, on_call=lambda c: None)
    systems.wire_grant_split(bench, LiveTalk(), {"grant_split": "log"}, tmp_path, None, "mrrs", "mrrs")
    assert bench.grant_count is None and bench.log_events is not None and bench.cut_events is not None
    off = CallIngest(tmp_path, LABELS, on_call=lambda c: None)
    systems.wire_grant_split(off, LiveTalk(), {}, tmp_path, Rec.state_path, "mrrs", "mrrs")
    assert off.grant_split.mode == "off" and off.grant_count is None
    assert systems.sizeup_block(TR) == {"split": "log"} and systems.sizeup_block(tmp_path / "none") == {}
    from sizeup.radio.p25 import Call
    assert apply_grant_cuts(Call("x", PD, "PD Dispatch", T0, T0 + 1, [], None), []) is False
