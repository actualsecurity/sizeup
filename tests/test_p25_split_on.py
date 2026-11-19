"""P25 talker splitting on (#647): MRRS's config.json says "split": "on", so the station's patched trunk-recorder ends a
transmission where the next talker's ID comes in on the voice channel. A call with A, then B, then C keying back to back
is one call JSON with three srcList entries (each at its own "pos", no gap between them), which sizeup must make three
rows of, end to end: its own id, times, radio ID and talker, clip, speech-to-text job (PD Dispatch, an alert-tier
talkgroup: whisper's CONTEXT class, the pre-alert lane), radio log row and pre-alert hook; the live "talking now" and
grants right; an emergency on one part alarming on that part only; a call from while sizeup was down coming in late part
by part. And the rollout: a new config.json restarts only that system's trunk-recorder, and only between its calls.
Synthetic calls (one tone per talker), invented radio IDs and words."""

from __future__ import annotations

import json
import re
import time
import wave
from pathlib import Path

import numpy as np

from sizeup.cloud import sttinput
from sizeup.radio import trunkrecd
from sizeup.radio.live import LiveTalk
from sizeup.radio.p25 import CallIngest, parse_call, read_wav_16k
from sizeup.stt import CONTEXT, CONTEXT_CLASSES, HIGH, RERUN, URGENT
from tests.test_app import wait
from tests.test_p25_replay import Says, run, settle, spies
from tests.test_p25_systems import PD, ask_county, ask_system, rows, service, two  # noqa: F401 (fixture)
from tests.test_p25_transmissions import A, B, C, LABELS, TONES, day, near, tdma_call, tone
from tests.test_trunk_split_counts import cut, start, tr
from tests.test_trunk_watched import Out

ROOT = Path(__file__).resolve().parent.parent
THREE = [(A, 2.0, 0.0), (B, 1.4, 0.0), (C, 1.8, 0.0)]     # back to back: the cuts split "on" makes (no terminator)


def three(d: Path, name: str, at: float, tg: int = PD, system: str = "mrrs", emergency_on: int | None = None,
          call_emergency: bool = False) -> list[float]:
    """A, B, C on one call as the patched build writes it with split on; emergency_on: the srcList entry flagged."""
    starts = tdma_call(d, name, THREE, tg=tg, system=system, start=at)
    if emergency_on is not None or call_emergency:
        meta = json.loads((d / f"{name}.json").read_text())
        if emergency_on is not None:
            meta["srcList"][emergency_on]["emergency"] = 1
        meta["emergency"] = int(call_emergency)
        (d / f"{name}.json").write_text(json.dumps(meta))
    return starts


def stt_spy(app, monkeypatch) -> list[tuple]:
    """Each speech-to-text job as _stt gets it: (id, channel key, whisper's class, STT input lane, late, seconds)."""
    seen: list[tuple] = []
    real = app._stt

    def spy(item, audio, eng, key, late=False):
        seen.append((item.id, key, app._late_prio(app._stt_priority(item, key), late), app._stt_lane(item, key), late,
                     round(len(audio) / 16000, 2)))
        return real(item, audio, eng, key, late)
    monkeypatch.setattr(app, "_stt", spy)
    return seen


# -- MRRS PD Dispatch: three talkers, three rows -----------------------------------------------------------------------


def test_three_talkers_cut_on_mrrs_are_three_rows_each_with_its_own_stt_lane_log_row_and_pre_alert_hook(
        two, tmp_path, monkeypatch):
    app, said, _logs = two
    rt = app.systems["mrrs"]
    # as on the hub: the PD pack pushes from PD Dispatch (whisper's HIGH for it, sizeup.intel.is_push_source)
    monkeypatch.setattr(app, "is_push_source", lambda key, kind="": key == f"p25:mrrs:{PD}")
    seen = stt_spy(app, monkeypatch)
    hooks = spies(app, monkeypatch)
    now = [time.time() - 30]
    rt.live.clock = lambda: now[0]
    base = tmp_path / "in"
    d = day(base)
    ing = CallIngest(base, LABELS, on_call=lambda c: None).prime()
    ing.grants, ing.alias_of = rt.live.grants, lambda src: ""
    name = f"{PD}-1790954731.970_770181250.1-call_9"
    starts = three(d, name, now[0])
    # what MRRS's trunk-recorder said live: the grant that opened the call, then (split on) each next talker named by
    # the voice channel, its grant update too
    head = "[2026-11-19 10:00:00.000] [mrrs]\t9C\tTG:   9211\tFreq: 770.181250 MHz\t"
    for at, line in ((starts[0] - 0.3, f"SIZEUP-UNIT mrrs {A} call {PD}"),
                     (starts[0] - 0.2, head + "Starting P25 Recorder Num [0]\tTDMA: true\tSlot: 1\tQPSK: true"),
                     (starts[1] - 0.2, f"SIZEUP-UNIT mrrs {B} call {PD}"),
                     (starts[1] + 1.0, head + f"Unit ID changed on Voice Channel, ext: {B}\tprevious: {A}\t samples: 0"),
                     (starts[2] - 0.2, f"SIZEUP-UNIT mrrs {C} call {PD}"),
                     (starts[2] + 1.0, head + f"Unit ID changed on Voice Channel, ext: {C}\tprevious: {B}\t samples: 0")):
        now[0] = at
        rt.live.feed_line(line)
    # the grants are the unit script's only (a voice-channel change is not one), and they name what the call JSON does
    assert [s for _at, s in rt.live.grants("mrrs", PD, 0, now[0] + 1)] == [str(A), str(B), str(C)]
    got = ing.scan() + ing.scan()
    assert len(got) == 1
    call = got[0]
    assert [(t.src, t.via) for t in call.turns] == [(str(A), "srclist"), (str(B), "srclist"), (str(C), "srclist")]
    app._call_system(rt, call)
    ids = [f"{name}-t{k}" for k in range(3)]
    assert wait(lambda: all(rows(app).get(i, {}).get("status") == "done" for i in ids))
    r = rows(app)
    assert [(r[i]["call_id"], r[i]["turn"], r[i]["radio_id"], r[i]["talker"], r[i]["rig"], r[i]["system"],
             r[i]["talkgroup"], r[i]["ours"]) for i in ids] == [
        (name, k, str(x), str(x), "", "mrrs", PD, 0) for k, x in enumerate((A, B, C))]
    # each its own time: back to back, A's end is B's start
    assert [round(r[i]["end"] - r[i]["start"], 2) for i in ids] == [2.0, 1.4, 1.8]
    assert all(abs(r[i]["start"] - s) < 0.05 for i, s in zip(ids, starts))
    assert abs(r[ids[0]]["end"] - r[ids[1]]["start"]) < 0.05 and abs(r[ids[1]]["end"] - r[ids[2]]["start"]) < 0.05
    # one speech-to-text job each, on its own audio: PD Dispatch, the alert tier the pre-alerts push from: whisper's
    # HIGH (under fire traffic, with the airband), the STT input's pre-alert lane (#701)
    assert sorted(seen) == [(i, f"p25:mrrs:{PD}", HIGH, sttinput.PRE, False, s) for i, s in zip(ids, (2.0, 1.4, 1.8))]
    assert len(said) == 3
    for i, f in zip(ids, TONES):
        with wave.open(str(app.clips_dir / f"{i}.src.wav")) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
        assert near(tone(x), f), i
    # each part on the pre-alert hook once (context: never our consumers, never an incident's)
    assert wait(lambda: sorted(hooks["context"]) == ids)
    assert hooks["intel"] == [] and hooks["hints"] == [] and hooks["alarm"] == []
    assert not app.cad.store.q("SELECT * FROM radio_links") and not app.cad.store.q("SELECT * FROM alarms")
    assert sorted(t.id for t in app.store.loose if t.id.startswith(name)) == ids


def test_pd_dispatch_is_the_pre_alert_lane_whether_or_not_the_pre_alerts_push_from_it(two, monkeypatch):
    """#701: an alert-tier talkgroup's clips go up first. #639 then raised a push source to whisper's HIGH, which took PD
    Dispatch out of the PRE lane (the lane read HIGH as the airband's): its lane is its tier's again."""
    from sizeup.app import Traffic
    app, _said, _logs = two
    t = Traffic(f"{PD}-1-call_1-t1", "PD Dispatch", 0.0, 1.0, "")
    key = f"p25:mrrs:{PD}"
    # (here AWS can't take it: whisper's CONTEXT_FALLBACK; else CONTEXT)
    assert app._stt_priority(t, key) in CONTEXT_CLASSES and app._stt_lane(t, key) == sttinput.PRE
    monkeypatch.setattr(app, "is_push_source", lambda k, kind="": k in (key, "p25:mrrs:9212"))
    assert (app._stt_priority(t, key), app._stt_lane(t, key)) == (HIGH, sttinput.PRE)
    assert app._stt_lane(t, "p25:mrrs:9212") == sttinput.NORMAL      # PD Tac (text tier) pushed from: as before
    assert app._stt_lane(t, "tg:307") == sttinput.FIRE


def test_the_live_talker_follows_each_cut_and_nobody_is_flashed_again_at_the_end():
    """The county's live view (the station's "talking now", the updater's "someone talking"): each "Unit ID changed"
    names the next talker at once; the call's "Transmission src" lines at its end flash nobody already shown."""
    now = [1000.0]
    lt = LiveTalk(clock=lambda: now[0], resolve=lambda s, r, a: {"unit": {str(B): "E98", str(C): "E99"}.get(r, "")})
    head = "[2026-11-19 10:00:00.000] [westchester]\t5C\tTG:   307\tFreq: 770.181250 MHz\t"
    lt.feed_line(head + "Starting P25 Recorder Num [0]\tTDMA: true\tSlot: 1\tQPSK: true")
    lt.feed_line(f"SIZEUP-UNIT westchester {A} call 307")
    assert [e["radio_id"] for e in lt.state() if not e["ended"]] == [str(A)]
    for prev, nxt, rig in ((A, B, "E98"), (B, C, "E99")):
        now[0] += 2.0
        assert lt.feed_line(head + f"Unit ID changed on Voice Channel, ext: {nxt}\tprevious: {prev}\t samples: 16000")
        assert [(e["radio_id"], e["rig"]) for e in lt.state() if not e["ended"]] == [(str(nxt), rig)]
    assert lt.grants("westchester", 307, 0, 2000) == [(1000.0, str(A))]
    now[0] += 2.0
    lt.feed_line(head + "Concluding Recorded Call - Last Update: 4s")
    for k, x in enumerate((A, B, C)):
        lt.feed_line(head + f"- Transmission src: {x} pos: {2.0 * k:.2f} length: 2.00")
    assert lt.state() == []                                  # all three shown live: none flashed as "just transmitted"


# -- the emergency button on one part --------------------------------------------------------------------------------


def county_app(tmp_path):
    from sizeup.app import App
    from tests.test_cloud import base as cloud_base
    app = App(cloud_base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite"),
                         p25_talkgroups={307: "Fire/EMS 15"}))
    app.stt.client = Says("invented words")
    app.stt.start()
    return app


def test_an_emergency_on_one_part_alarms_on_that_part_only_and_once(tmp_path):
    app = county_app(tmp_path)
    try:
        d = day(tmp_path / "p25", "westchester")
        # B's srcList entry flagged (a trunk-recorder that fills it): B's row alarms, A's and C's don't
        three(d, "307-1-call_1", time.time() - 20, tg=307, system="westchester", emergency_on=1)
        c = parse_call(d / "307-1-call_1.json", LABELS)
        c.audio = read_wav_16k(Path(c.wav_path))
        app._call(c)
        assert wait(lambda: all(rows(app).get(f"307-1-call_1-t{k}", {}).get("status") == "done" for k in range(3)))
        r = rows(app)
        assert [r[f"307-1-call_1-t{k}"]["emergency"] for k in range(3)] == [0, 1, 0]
        al = app.cad.store.q("SELECT traffic_id, emergency FROM alarms")
        assert al == [{"traffic_id": "307-1-call_1-t1", "emergency": 1}]
        # 5.2.0's own flag is the call's (the grant that opened it): its first key-up, A's, only
        three(d, "307-2-call_2", time.time() - 10, tg=307, system="westchester", call_emergency=True)
        c = parse_call(d / "307-2-call_2.json", LABELS)
        c.audio = read_wav_16k(Path(c.wav_path))
        app._call(c)
        assert wait(lambda: all(rows(app).get(f"307-2-call_2-t{k}", {}).get("status") == "done" for k in range(3)))
        assert [rows(app)[f"307-2-call_2-t{k}"]["emergency"] for k in range(3)] == [1, 0, 0]
        assert sorted(a["traffic_id"] for a in app.cad.store.q("SELECT traffic_id FROM alarms")) == [
            "307-1-call_1-t1", "307-2-call_2-t0"]
    finally:
        app.stop()


# -- a split call from while sizeup was down (#747 / #753) ------------------------------------------------------------


def test_a_split_call_from_while_down_comes_in_late_part_by_part_each_once(two, tmp_path, monkeypatch):
    app, _said, logs = two
    rt = app.systems["mrrs"]
    seen = stt_spy(app, monkeypatch)
    hooks = spies(app, monkeypatch)
    base, mark = tmp_path / "cap", tmp_path / "state" / "p25_mrrs_ingest_mark.json"
    d = day(base)
    on_call = lambda c: app._call_system(rt, c)     # noqa: E731
    run(base, mark, [], [], on_call=on_call).stop()           # sizeup ran, then stopped (an update)
    name = f"{PD}-1-call_1"
    three(d, name, time.time() - 60)                           # the call concluded while it was down: one sidecar
    keep = tmp_path / "keep"                                   # (a copy: sizeup takes the recorder's files away)
    keep.mkdir()
    for ext in (".json", ".wav"):
        (keep / f"{name}{ext}").write_bytes((d / f"{name}{ext}").read_bytes())
    ing = run(base, mark, [], [], on_call=on_call)
    took = settle(ing)
    assert [(c.id, c.late) for c in took] == [(name, True)]
    ing.stop()
    ids = [f"{name}-t{k}" for k in range(3)]
    assert wait(lambda: all(rows(app).get(i, {}).get("status") == "done" for i in ids))
    # each part late: whisper's RERUN class at most (after the live traffic), still the pre-alert lane; no pre-alert
    assert [(i, k, lane, late, s) for i, k, _c, lane, late, s in sorted(seen)] == [
        (i, f"p25:mrrs:{PD}", sttinput.PRE, True, s) for i, s in zip(ids, (2.0, 1.4, 1.8))]
    assert all(c in CONTEXT_CLASSES and c > RERUN for _i, _k, c, *_r in seen)     # (its own class: lower than RERUN)
    assert all(getattr(t, "late", False) for t in app.store.loose if t.id in ids)
    assert hooks["context"] == []
    # the mark is past its one sidecar: the next start takes nothing
    ing = run(base, mark, [], [], on_call=on_call)
    assert settle(ing) == []
    ing.stop()
    # taken before a restart the mark never heard of (a crash between two writes): all its parts there, none again
    again = parse_call(keep / f"{name}.json", LABELS)
    again.audio, again.late = read_wav_16k(Path(again.wav_path)), True
    app._call_system(rt, again)
    assert len(seen) == 3 and any("taken before the restart: not again" in x for x in logs)
    assert sorted(t.id for t in app.store.loose if t.id.startswith(name)) == ids


def test_a_split_call_taken_part_way_brings_in_only_its_other_parts_late(two, tmp_path, monkeypatch):
    """The handler failed after the call's first part (its mark never moved past it): the next start ingests it late,
    each part by its own id: the one logged before is not logged again, the other two come in."""
    app, _said, logs = two
    rt = app.systems["mrrs"]
    seen = stt_spy(app, monkeypatch)
    real = app._stt
    fail = {"once": True}

    def flaky(item, audio, eng, key, late=False):
        if item.id.endswith("-t1") and fail.pop("once", False):
            raise RuntimeError("invented failure")
        return real(item, audio, eng, key, late)
    monkeypatch.setattr(app, "_stt", flaky)
    base, mark = tmp_path / "cap", tmp_path / "state" / "p25_mrrs_ingest_mark.json"
    d = day(base)
    events: list[str] = []
    on_call = lambda c: app._call_system(rt, c)     # noqa: E731
    ing = CallIngest(base, LABELS, on_call=on_call, on_event=events.append, mark_path=mark).resume()
    name = f"{PD}-2-call_2"
    three(d, name, time.time() - 30)
    assert settle(ing) == [] and any("call handler error" in e for e in events)    # live: t0 taken, then it failed
    ing.stop()
    t0 = f"{name}-t0"
    assert wait(lambda: rows(app).get(t0, {}).get("status") == "done") and len(seen) == 1
    ing = run(base, mark, [], [], on_call=on_call)              # the restart
    took = settle(ing)
    ing.stop()
    assert [(c.id, c.late) for c in took] == [(name, True)]
    ids = [f"{name}-t{k}" for k in range(3)]
    assert wait(lambda: all(rows(app).get(i, {}).get("status") == "done" for i in ids))
    assert [(s[0], s[4]) for s in seen] == [(t0, False), (f"{name}-t1", True), (f"{name}-t2", True)]
    assert sorted(t.id for t in app.store.loose if t.id.startswith(name)) == ids        # each once
    assert any("1 of its 3 key-ups taken before the restart: the other 2 now" in x for x in logs)


# -- #770: the hourly counts under "on" ------------------------------------------------------------------------------


def test_the_hourly_line_counts_each_cut_and_the_calls_cut():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.started("on", 0.0)
    sh.tick(0.0)
    for call in range(1, 5):
        sh.line(start("mrrs", call), call)
    sh.line(cut("mrrs", 2), 10)                             # call 2: A -> B -> C, two cuts
    sh.line(cut("mrrs", 2), 12)
    sh.line(cut("mrrs", 4), 20)                             # call 4: one cut
    sh.tick(3600.0)
    assert said == ["p25 mrrs: split (on) last hour: 3 splits, 0 ID flips, 0 aliases, in 4 calls (50.0% of calls)"]


# -- the rollout: a new config.json restarts only its system's trunk-recorder, between calls ------------------------


def concluding(sys_: str, call: int) -> str:
    return tr(sys_, "\x1b[33mConcluding Recorded Call\x1b[0m - Last Update: 4s\tRecorder last write:3.9", call)


def mrrs_running(tmp_path, lines, mono):
    county, mrrs, pop, logs, d = service(tmp_path)
    orig = pop.__call__

    def popen(argv, **kw):
        p = orig(argv, **kw)
        p.stdout = Out(lines)
        return p
    mrrs.popen = popen
    mrrs.mono = lambda: mono[0]
    ask_county(tmp_path)
    assert county.step(100.0, mono[0])["state"] == "ok"
    ask_system(tmp_path, {0: "pd-a"})
    assert mrrs.step(100.0, mono[0])["state"] == "ok"
    mrrs._drain_thread.join(timeout=5)
    return county, mrrs, pop, logs, d


def set_split(d: Path, mode: str) -> None:
    c = json.loads((d / "config.json").read_text())
    c["sizeup"]["split"] = mode
    (d / "config.json").write_text(json.dumps(c))


def test_a_new_mrrs_config_restarts_its_trunk_recorder_after_its_call_never_the_countys(tmp_path):
    mono = [500.0]
    county, mrrs, pop, logs, d = mrrs_running(tmp_path, [start("mrrs", 7), cut("mrrs", 7)], mono)
    fire, pd = pop.started
    assert pd.kw["env"][trunkrecd.SPLIT_ENV] == "on" and mrrs.calls_open.busy(mono[0]) == "1 call(s) recording"
    set_split(d, "log")                                           # the rollback, as an update installs it
    for k in range(1, 4):
        mono[0] += 10.0
        st = mrrs.step(100.0 + k, mono[0])
        assert st["state"] == "ok" and pd.code is None and len(pop.started) == 2     # mid-call: left running
    assert st["config_wait"] == "1 call(s) recording"
    assert [x for x in logs if "config changed" in x] == [
        "p25 mrrs: MRRS's config changed: trunk-recorder restarts after its call (1 call(s) recording; 5 min at most)"]
    mrrs.calls_open.line(concluding("mrrs", 7), mono[0])          # the call is over; its files are being written
    mono[0] += 1.0
    assert mrrs.step(110.0, mono[0])["config_wait"] == "a call's files being written" and pd.code is None
    mono[0] += trunkrecd.CALL_AFTER_S
    mrrs.step(111.0, mono[0])
    assert pd.code is not None                                     # now restarted, with its new mode
    assert mrrs.step(112.0, mono[0])["state"] == "ok" and pop.started[-1].kw["env"][trunkrecd.SPLIT_ENV] == "log"
    assert "config_wait" not in mrrs.status
    assert any(x.startswith("p25 mrrs: MRRS: its call is over after") for x in logs)
    assert any(x == "p25: trunk-recorder stopped (MRRS: its config changed)" for x in logs)
    # the county's trunk-recorder: the same process throughout
    assert county.step(113.0, mono[0])["state"] == "ok" and fire.code is None and len(pop.started) == 3


def test_a_call_that_never_ends_holds_the_restart_five_minutes_at_most(tmp_path):
    mono = [500.0]
    _county, mrrs, pop, logs, d = mrrs_running(tmp_path, [start("mrrs", 3)], mono)
    pd = pop.started[1]
    set_split(d, "log")
    mrrs.step(101.0, mono[0])
    for k in range(5):                                             # a line of its own every minute: never stale
        mono[0] += 59.0
        mrrs.calls_open.line(tr("mrrs", f"Unit ID set via Control Channel, ext: {A}\tcurrent: -1", 3), mono[0])
        mrrs.step(102.0 + k, mono[0])
        assert pd.code is None
    mono[0] += 10.0
    mrrs.step(110.0, mono[0])
    assert pd.code is not None
    assert any(re.match(r"p25 mrrs: MRRS's config changed 30\d s ago: restarting trunk-recorder now, 1 call\(s\) "
                        r"recording", x) for x in logs)


def test_a_quiet_system_or_other_dongles_restart_at_once(tmp_path):
    mono = [500.0]
    _county, mrrs, pop, _logs, d = mrrs_running(tmp_path, [start("mrrs", 1), concluding("mrrs", 1)], mono)
    pd = pop.started[1]
    mono[0] += 60.0
    set_split(d, "log")                                            # no call recording: restarted at the next step
    mrrs.step(101.0, mono[0])
    assert pd.code is not None and "config_wait" not in mrrs.status
    assert mrrs.step(102.0, mono[0])["state"] == "ok" and len(pop.started) == 3
    pd = pop.started[2]
    mrrs._drain_thread.join(timeout=5)
    mrrs.calls_open.line(start("mrrs", 2), mono[0])                # mid-call, but its dongles changed: at once
    assert mrrs.calls_open.busy(mono[0])
    ask_system(tmp_path, {0: "pd-a", 1: "pd-b"}, seq=2)
    mrrs.step(103.0, mono[0])
    assert pd.code is not None
    # sizeup switched it off mid-call: at once too
    assert mrrs.step(104.0, mono[0])["state"] == "ok"
    mrrs._drain_thread.join(timeout=5)
    mrrs.calls_open.line(start("mrrs", 3), mono[0])
    assert mrrs.calls_open.busy(mono[0])
    ask_system(tmp_path, {}, on=False, seq=3)
    assert mrrs.step(105.0, mono[0])["state"] == "off" and pop.started[-1].code is not None


def test_only_the_countys_own_files_restart_the_trunk_recorder_service():
    """install-linux.sh restarts the whole service (the county's trunk-recorder and every other system's) only when
    tr_sig changes: the supervisor, its unit, the builds and the county's own config.json / talkgroups.csv. Another
    system's files, written in the same switch before that check, are its own supervisor's to act on."""
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    sig = re.search(r"^tr_sig\(\) \{\n(.*?)^\}\n", sh, re.S | re.M).group(1)
    assert '"$TR_DIR/config.json" "$TR_DIR/talkgroups.csv"' in sig and "systems" not in sig
    act = sh[sh.index("\nactivate() {"):]
    assert act.index('"$TR_DIR/systems/$sysname/config.json"') < act.index('trunk_recorder_service "$tr_was"')
    assert URGENT < CONTEXT                                        # (fire traffic still goes before PD in whisper)
