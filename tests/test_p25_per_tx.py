"""Per-transmission hand-off (#986): with a system's config.json "sizeup": {"per_tx": "on"}, the station's patched
trunk-recorder (SIZEUP_TR_PERTX=1) links each transmission's WAV into <captureDir>/sizeup-tx/ as it ends, with a small
JSON beside it, and sizeup makes that transmission's row then (its own id "<call>-t<k>", radio ID, clip, speech-to-text,
radio log row), not at the call's end. The call's JSON still comes at its end: the transmissions taken already make no
second row; any other is made then, under the same id it would have had as a part. "log" only notes them (the hourly
line says what they would have gained), "off" is as before. Synthetic calls (one tone per talker), invented radio IDs."""

from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np

from sizeup.radio import p25 as p25mod
from sizeup.radio import trunkrecd
from sizeup.radio.p25 import PARTS_DIR, CallIngest, split_call
from tests.test_app import wait
from tests.test_p25_systems import PD, rows, two  # noqa: F401 (fixture)
from tests.test_p25_transmissions import A, B, C, LABELS, TONES, day, near, tdma_call, tone

ROOT = Path(__file__).resolve().parent.parent
THREE = [(A, 2.0, 0.5), (B, 1.4, 0.5), (C, 1.8, 0.0)]


def stem_of(tg: int, start: float, n: int, system_slot: str = ".1") -> str:
    """The call file's stem as trunk-recorder names it (and the patch names its parts after)."""
    ms = int(round(start * 1000))
    return f"{tg}-{ms // 1000}.{ms % 1000:03d}_770181250{system_slot}-call_{n}"


def write_part(base: Path, stem: str, k: int, src: int, start: float, seconds: float, *, tg: int = PD,
               system: str = "mrrs", n: int = 9, emergency: int = 0, encrypted: int = 0, rate: int = 8000) -> None:
    """One transmission as the patch hands it on: its WAV (a tone of its own), then its JSON."""
    d = base / PARTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    x = 0.3 * np.sin(2 * np.pi * TONES[k % len(TONES)] * np.arange(int(round(seconds * rate))) / rate)
    with wave.open(str(d / f"{stem}-t{k}.wav"), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    ms = int(round(start * 1000))
    (d / f"{stem}-t{k}.json").write_text(json.dumps({
        "v": 1, "short_name": system, "talkgroup": tg, "call_num": n, "call": stem, "k": k, "src": src,
        "start_time_ms": ms, "stop_time_ms": ms + int(round(seconds * 1000)), "samples": int(round(seconds * rate)),
        "rate": rate, "error_count": 0, "spike_count": 0, "freq": 770181250, "slot": 1, "emergency": emergency,
        "encrypted": encrypted}))


def ingest(base: Path, mode: str, took=None):
    """A CallIngest with the hand-off in `mode`: (ingest, parts handed on, calls handed on)."""
    parts, calls = [], []

    def on_part(c):
        parts.append(c)
        return True if took is None else took(c)
    ing = CallIngest(base, LABELS, on_call=calls.append).prime()
    ing.per_tx, ing.on_part = mode, on_part
    return ing, parts, calls


def three(base: Path, at: float, upto: int = 3, n: int = 9) -> tuple[str, list[float]]:
    """The three transmissions' parts, the first `upto` of them; (call stem, each one's start)."""
    stem = stem_of(PD, at, n)
    starts, t = [], at
    for k, (src, sec, gap) in enumerate(THREE):
        starts.append(t)
        if k < upto:
            write_part(base, stem, k, src, t, sec, n=n)
        t += sec + gap
    return stem, starts


# -- the ingest ------------------------------------------------------------------------------------------------------


def test_each_transmission_is_handed_on_as_it_ends_with_its_own_id_turn_and_radio_id(tmp_path):
    base = tmp_path / "in"
    d = day(base)
    ing, parts, calls = ingest(base, "on")
    at = time.time() - 10
    stem, starts = three(base, at, upto=2)
    ing.poll()
    assert calls == [] and len(parts) == 2                         # two have ended: two rows now, the call goes on
    pieces = [split_call(c)[0] for c in parts]
    assert [(p.id, p.turn, p.pairs, round(p.end - p.start, 2)) for p in pieces] == [
        (f"{stem}-t0", 0, [(str(A), str(A))], 2.0), (f"{stem}-t1", 1, [(str(B), str(B))], 1.4)]
    assert [abs(p.start - s) < 0.01 for p, s in zip(pieces, starts)] == [True, True]
    assert all(near(tone(p.audio), f) for p, f in zip(pieces, TONES))
    assert [c.id for c in parts] == [stem, stem] and all(c.system == "mrrs" and c.talkgroup == PD for c in parts)
    assert list((base / PARTS_DIR).iterdir()) == []                 # taken: its files gone
    # the last one, then the call's JSON (trunk-recorder writes them in that order): no second row of any
    write_part(base, stem, 2, C, starts[2], 1.8)
    tdma_call(d, stem, THREE, start=at)
    ing.poll()
    assert len(parts) == 3 and [c.id for c in calls] == [stem]
    assert calls[0].parts == {0, 1, 2} and split_call(calls[0]) == []


def test_a_transmission_not_taken_as_a_part_is_made_at_the_calls_end_under_the_parts_id(tmp_path):
    base = tmp_path / "in"
    d = day(base)
    ing, parts, calls = ingest(base, "on", took=lambda c: c.part != 1)   # the second one not taken (say Record off)
    at = time.time() - 10
    stem, _starts = three(base, at)
    tdma_call(d, stem, THREE, start=at)
    ing.poll()
    assert [c.part for c in parts] == [0, 1, 2] and calls[0].parts == {0, 2}
    calls[0].audio = p25mod.read_wav_16k(Path(calls[0].wav_path))
    left = split_call(calls[0])
    assert [(p.id, p.turn, p.pairs[0][0]) for p in left] == [(f"{stem}-t1", 1, str(B))]
    assert near(tone(left[0].audio), TONES[1])


def test_a_part_with_no_radio_id_is_named_by_the_live_grant_and_never_cut(tmp_path):
    base = tmp_path / "in"
    day(base)
    ing, parts, _calls = ingest(base, "on")
    at = time.time() - 10
    stem = stem_of(PD, at, 4)
    write_part(base, stem, 0, -1, at, 3.0, n=4)
    # the grant that opened it, and one inside it for another radio (no cut: a part is one transmission)
    ing.grants = lambda system, tg, t0, t1: [(at - 0.4, str(A)), (at + 1.5, str(B))]
    ing.poll()
    p = split_call(parts[0])
    assert len(p) == 1 and p[0].id == f"{stem}-t0" and round(p[0].end - p[0].start, 1) == 3.0
    ing2, parts2, _ = ingest(tmp_path / "in2", "on")
    day(tmp_path / "in2")
    write_part(tmp_path / "in2", stem, 0, -1, at, 3.0, n=4)
    ing2.grants = lambda system, tg, t0, t1: [(at - 0.4, str(A))]
    ing2.poll()
    got = split_call(parts2[0])
    assert [(x.id, x.pairs, x.via) for x in got] == [(f"{stem}-t0", [(str(A), str(A))], "grant")]


def test_off_is_as_before_and_log_only_notes_them(tmp_path, monkeypatch):
    for mode in ("off", "log"):
        base = tmp_path / mode
        d = day(base)
        events: list[str] = []
        ing, parts, calls = ingest(base, mode)
        ing.on_event = events.append
        at = time.time() - 10
        stem, _starts = three(base, at)
        tdma_call(d, stem, THREE, start=at)
        ing.poll()
        assert parts == [] and [c.id for c in calls] == [stem] and calls[0].parts == frozenset()
        calls[0].audio = p25mod.read_wav_16k(Path(calls[0].wav_path))
        assert [p.id for p in split_call(calls[0])] == [f"{stem}-t{k}" for k in range(3)]    # the rows, as before
        left = sorted(x.name for x in (base / PARTS_DIR).iterdir())
        assert len(left) == (6 if mode == "off" else 0)             # off: never read (swept once they are stale)
        ing._part_said -= p25mod.PART_REPORT_S
        ing._parts()
        said = [e for e in events if "per-tx" in e]
        if mode == "off":
            assert said == []
        else:
            assert len(said) == 1 and said[0].startswith("p25 P25: per-tx (log) last hour: 3 transmissions in 1 calls; "
                                                         "ready ")
            assert str(A) not in said[0] and stem not in said[0]   # counts and seconds only
    # off: a stale one is swept
    old = time.time() - p25mod.PART_STALE_S - 5
    import os
    for f in (tmp_path / "off" / PARTS_DIR).iterdir():
        os.utime(f, (old, old))
    ing, _p, _c = ingest(tmp_path / "off", "off")
    ing._parts()
    assert list((tmp_path / "off" / PARTS_DIR).iterdir()) == []


def test_emergency_and_encrypted_calls_are_left_to_their_end(tmp_path):
    base = tmp_path / "in"
    day(base)
    ing, parts, _calls = ingest(base, "on")
    at = time.time() - 10
    write_part(base, stem_of(PD, at, 1), 0, A, at, 1.0, n=1, emergency=1)
    write_part(base, stem_of(PD, at + 5, 2), 0, A, at + 5, 1.0, n=2, encrypted=1)
    ing.poll()
    assert parts == [] and list((base / PARTS_DIR).iterdir()) == []


def test_a_part_after_its_calls_json_is_dropped_and_a_bad_one_is_said(tmp_path):
    base = tmp_path / "in"
    d = day(base)
    events: list[str] = []
    ing, parts, calls = ingest(base, "on")
    ing.on_event = events.append
    at = time.time() - 10
    stem = stem_of(PD, at, 9)
    tdma_call(d, stem, THREE, start=at)
    ing.poll()
    assert len(calls) == 1 and calls[0].parts == frozenset()
    write_part(base, stem, 2, C, at + 4.4, 1.8)
    (base / PARTS_DIR / "junk-t0.json").write_text("{")
    ing.poll()
    assert parts == [] and list((base / PARTS_DIR).iterdir()) == []
    assert any("per-tx: junk-t0.json not read" in e for e in events)


def test_the_calls_json_named_by_another_first_start_still_finds_its_parts(tmp_path):
    # a clock step between the part and the call's end: the same call by its system, talkgroup and number
    base = tmp_path / "in"
    d = day(base)
    ing, parts, calls = ingest(base, "on")
    at = time.time() - 10
    stem = stem_of(PD, at, 9)
    write_part(base, stem, 0, A, at, 2.0)
    ing.poll()
    other = stem_of(PD, at + 0.002, 9)
    tdma_call(d, other, THREE, start=at + 0.002)
    ing.poll()
    assert len(parts) == 1 and calls[0].id == other and calls[0].parts == {0}


def test_parts_from_before_a_start_are_cleared_and_never_mistaken_for_calls(tmp_path):
    base = tmp_path / "in"
    day(base)
    at = time.time() - 10
    stem, _ = three(base, at)
    ing, parts, calls = ingest(base, "on")
    assert all(PARTS_DIR not in str(p) for _k, p in ing._listing())
    ing.start()
    ing.stop()
    assert list((base / PARTS_DIR).iterdir()) == [] and parts == [] and calls == []


# -- the app: rows as they end, none twice ------------------------------------------------------------------------------


def test_mrrs_parts_are_rows_as_they_end_and_the_calls_end_adds_none(two, tmp_path, monkeypatch):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    base = tmp_path / "in"
    d = day(base)
    ing = CallIngest(base, LABELS, on_call=lambda c: app._call_system(rt, c)).prime()
    ing.per_tx, ing.on_part = "on", (lambda c: app._part_system(rt, c))
    at = time.time() - 30
    stem, starts = three(base, at, upto=2)
    ing.poll()
    ids = [f"{stem}-t{k}" for k in range(3)]
    assert wait(lambda: all(rows(app).get(i, {}).get("status") == "done" for i in ids[:2]))
    r = rows(app)
    assert [(r[i]["call_id"], r[i]["turn"], r[i]["radio_id"], r[i]["system"], r[i]["talkgroup"]) for i in ids[:2]] == [
        (stem, 0, str(A), "mrrs", PD), (stem, 1, str(B), "mrrs", PD)]
    assert all(abs(r[i]["start"] - s) < 0.05 for i, s in zip(ids[:2], starts))
    seen = app.tg._seen.get(app.tg._k(PD, "mrrs"), {}).get("count", 0)
    write_part(base, stem, 2, C, starts[2], 1.8)
    tdma_call(d, stem, THREE, start=at)
    ing.poll()
    assert wait(lambda: rows(app).get(ids[2], {}).get("status") == "done")
    time.sleep(0.3)
    got = {i: x for i, x in rows(app).items() if x["call_id"] == stem}
    assert sorted(got) == ids and len(said) == 3                       # three rows, three transcriptions, once each
    assert app.tg._seen[app.tg._k(PD, "mrrs")]["count"] == seen + 1     # the call counted once, at its end
    for i, f in zip(ids, TONES):
        with wave.open(str(app.clips_dir / f"{i}.src.wav")) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        assert near(tone(x), f), i


def test_county_parts_are_rows_when_recorded_and_left_to_the_end_when_not(two, tmp_path):
    app, said, _logs = two
    base = tmp_path / "in"
    d = day(base, "westchester")
    ing = CallIngest(base, {307: "Fire/EMS 15", 301: "60 Control"}, on_call=app._call).prime()
    ing.per_tx, ing.on_part = "on", app._part
    at = time.time() - 30
    stem = stem_of(307, at, 3, "")
    write_part(base, stem, 0, A, at, 2.0, tg=307, system="westchester", n=3)
    write_part(base, stem, 1, B, at + 2.5, 1.4, tg=307, system="westchester", n=3)
    ing.poll()
    assert wait(lambda: all(rows(app).get(f"{stem}-t{k}", {}).get("status") == "done" for k in (0, 1)))
    tdma_call(d, stem, THREE[:2], tg=307, system="westchester", start=at, tdma=False)
    ing.poll()
    time.sleep(0.3)
    assert sorted(i for i, x in rows(app).items() if x["call_id"] == stem) == [f"{stem}-t0", f"{stem}-t1"]
    assert len(said) == 2
    # Record off: no part is taken, and the call's end drops it, as before
    app.tg.set(301, record=False)
    assert not app.tg.record(301)
    stem2 = stem_of(301, at + 10, 5, "")
    write_part(base, stem2, 0, A, at + 10, 1.0, tg=301, system="westchester", n=5)
    ing.poll()
    assert len(said) == 2 and not [i for i in rows(app) if i.startswith(stem2)]
    assert ing._calls[stem2]["taken"] == set()                          # its end takes it (and drops it), as before
    tdma_call(d, stem2, [(A, 1.0, 0.0)], tg=301, system="westchester", start=at + 10, tdma=False)
    ing.poll()
    time.sleep(0.2)
    assert len(said) == 2 and not [i for i in rows(app) if i.startswith(stem2)]


def test_a_call_going_on_at_a_restart_makes_no_second_row_of_a_part_taken_before_it(two, tmp_path):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    base = tmp_path / "in"
    d = day(base)
    ing = CallIngest(base, LABELS, on_call=lambda c: app._call_system(rt, c)).prime()
    ing.per_tx, ing.on_part = "on", (lambda c: app._part_system(rt, c))
    at = time.time() - 30
    stem, starts = three(base, at, upto=1)
    ing.poll()
    assert wait(lambda: rows(app).get(f"{stem}-t0", {}).get("status") == "done")
    # sizeup restarts: a new ingest (nothing in mind), the call goes on and ends
    ing2 = CallIngest(base, LABELS, on_call=lambda c: app._call_system(rt, c)).prime()
    ing2.per_tx, ing2.on_part = "on", (lambda c: app._part_system(rt, c))
    ing2.started_at = at + 1
    write_part(base, stem, 1, B, starts[1], 1.4)
    write_part(base, stem, 2, C, starts[2], 1.8)
    tdma_call(d, stem, THREE, start=at)
    ing2.poll()
    assert wait(lambda: all(rows(app).get(f"{stem}-t{k}", {}).get("status") == "done" for k in range(3)))
    time.sleep(0.3)
    assert sorted(i for i, x in rows(app).items() if x["call_id"] == stem) == [f"{stem}-t{k}" for k in range(3)]
    assert len(said) == 3


# -- the switch -----------------------------------------------------------------------------------------------------------


def test_the_switch_is_per_system_off_unless_it_says_and_sets_the_patchs_env(tmp_path):
    from sizeup.radio import systems
    tr = ROOT / "deploy" / "trunk-recorder"
    for conf in [tr / "config.json", *sorted((tr / "systems").glob("*/config.json"))]:
        assert "per_tx" not in json.loads(conf.read_text())["sizeup"], conf      # off everywhere at first
        assert trunkrecd.per_tx_mode(["trunk-recorder", "--config=config.json"], conf.parent) == "off"
    assert systems.per_tx_of({"per_tx": "on"}) == "on" and systems.per_tx_of({"per_tx": "yes"}) == "off"
    assert systems.per_tx_of(None) == "off" and systems.load(tr / "systems" / "mrrs").per_tx == "off"
    d = tmp_path / "x"
    d.mkdir()
    for mode, want in (("log", "log"), ("on", "on"), ("off", "off"), ("maybe", "off")):
        (d / "config.json").write_text(json.dumps({"sizeup": {"per_tx": mode}}))
        assert trunkrecd.per_tx_mode(["t", "--config=config.json"], d) == want


def test_each_system_starts_its_trunk_recorder_with_its_own_per_tx_env(tmp_path):
    from tests.test_p25_systems import ask_county, ask_system, service
    county, mrrs, pop, _logs, d = service(tmp_path)
    c = json.loads((d / "config.json").read_text())
    c["sizeup"]["per_tx"] = "log"
    (d / "config.json").write_text(json.dumps(c))
    ask_county(tmp_path)
    county.step(100.0)
    ask_system(tmp_path, {0: "pd-a"})
    mrrs.step(101.0)
    assert [p.kw["env"][trunkrecd.PERTX_ENV] for p in pop.started] == ["0", "1"]   # MRRS log: written; the county not
    assert "sizeup" not in json.loads((d / "config.runtime.json").read_text())       # trunk-recorder never sees it
