"""P25 calls written while sizeup was down (#747): CallIngest's high-water mark, the calls it ingests late at the next
start, and what a late call never reaches (the alarms, the pre-alerts, the pushes) while it still goes to the radio log,
speech-to-text and the hub. Synthetic data only: invented radio IDs and words."""

from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

import sizeup.radio.p25 as p25
from sizeup.radio.p25 import Call, CallIngest, Turn
from tests.test_app import FakeWhisper, wait
from tests.test_p25 import LABELS, day_dir, write_call
from tests.test_p25_systems import PD, a_call, rows, two  # noqa: F401 (fixture)

RADIO = 9900001                 # invented radio IDs
OTHER = 9900002


def put(d, name, age=0.0, tg=307, emergency=False, radio=RADIO):
    """A finished call as trunk-recorder writes it, its files `age` seconds old."""
    write_call(d, name, tg, [radio])
    t = time.time() - age
    meta = json.loads((d / f"{name}.json").read_text())
    meta.update(start_time=int(t - 2), stop_time=int(t), emergency=int(emergency))
    for s in meta["srcList"]:
        s["time"] = int(t - 2)
    (d / f"{name}.json").write_text(json.dumps(meta))
    for ext in (".json", ".wav"):
        os.utime(d / f"{name}{ext}", (t, t))


def run(base, mark, got, logs, on_call=None):
    """One run of sizeup's ingest, started as the app starts it (resume), polled by hand instead of by its thread."""
    ing = CallIngest(base, LABELS, on_call=on_call or got.append, on_event=logs.append, mark_path=mark)
    return ing.resume()


def settle(ing):
    """A call is taken once its WAV size holds still across two polls."""
    return ing.poll() + ing.poll()


@pytest.fixture
def cap(tmp_path):
    return tmp_path / "p25", day_dir(tmp_path / "p25"), tmp_path / "state" / "p25_ingest_mark.json"


def test_calls_written_while_down_are_ingested_once_and_marked_late(cap):
    base, d, mark = cap
    put(d, "307-1-call_1", age=60)               # on disk before the first start ever: not replayed (as before)
    got, logs = [], []
    ing = run(base, mark, got, logs)
    assert logs == ["p25 P25: no ingest mark yet (first start here): the 1 call(s) on disk not replayed"]
    put(d, "307-2-call_2", age=40)
    assert [(c.id, c.late) for c in settle(ing)] == [("307-2-call_2", False)]
    ing.stop()                                   # the mark, written at stop
    assert json.loads(mark.read_text())["name"] == "westchester/2026/10/13/307-2-call_2.json"
    # sizeup down (an auto-update): trunk-recorder keeps writing
    put(d, "307-3-call_3", age=20)
    put(d, "301-4-call_4", age=10, tg=301)
    got, logs, asked = [], [], []
    ing = run(base, mark, got, logs)
    ing.grants = lambda *a: asked.append(a[3]) or []         # the live grants: never asked for a late call's
    assert logs == []                                         # (said once they are in)
    took = settle(ing)
    assert [(c.id, c.late) for c in took] == [("307-3-call_3", True), ("301-4-call_4", True)]
    assert logs == ["p25 P25: 2 calls from while it was down ingested late (0 older skipped)"]
    assert asked == []
    put(d, "307-5-call_5")                       # live again: not late, and its grants are looked up
    assert [(c.id, c.late) for c in settle(ing)] == [("307-5-call_5", False)] and len(asked) == 1
    assert settle(ing) == [] and len(got) == 3


def test_calls_older_than_replay_s_are_skipped(cap):
    base, d, mark = cap
    run(base, mark, [], []).stop()               # the first start: an empty capture dir, a mark of nothing yet
    put(d, "307-1-call_1", age=p25.REPLAY_S + 60)            # a long outage: too old to be news
    put(d, "307-2-call_2", age=30)
    got, logs = [], []
    ing = run(base, mark, got, logs)
    assert [(c.id, c.late) for c in settle(ing)] == [("307-2-call_2", True)]
    assert logs == ["p25 P25: 1 calls from while it was down ingested late (1 older skipped)"]
    ing.stop()
    got, logs = [], []
    ing = run(base, mark, got, logs)                          # and never later either (behind the mark now)
    assert settle(ing) == [] and logs == []                   # (nothing from while it was down: no line)


def test_no_call_is_ingested_twice_across_three_restarts(cap):
    """The files stay on disk (no app here to remove them): only the mark keeps each call to one ingest."""
    base, d, mark = cap
    taken: list[Call] = []
    for k in range(4):                           # the first start, then three restarts
        put(d, f"307-{k}0-down", age=40 - 10 * k)            # written while sizeup was down (before the first: never)
        ing = run(base, mark, taken, [])
        settle(ing)
        put(d, f"307-{k}1-live", age=35 - 10 * k)
        settle(ing)
        ing.stop()
    ids = [c.id for c in taken]
    assert len(ids) == len(set(ids)) == 7
    assert {c.id for c in taken if c.late} == {"307-10-down", "307-20-down", "307-30-down"}
    assert not any(c.late for c in taken if c.id.endswith("live"))


def test_the_mark_moves_only_past_a_call_that_was_taken(cap):
    base, d, mark = cap
    run(base, mark, [], []).stop()
    put(d, "307-1-call_1", age=20)

    def fails(call):
        raise RuntimeError("the app's handler broke")
    logs = []
    ing = run(base, mark, [], logs, on_call=fails)
    assert settle(ing) == []
    assert logs == ["p25 call handler error: RuntimeError: the app's handler broke"]      # (none taken: no line)
    ing.stop()
    got = []
    ing = run(base, mark, got, [])               # the next start tries it again
    assert [(c.id, c.late) for c in settle(ing)] == [("307-1-call_1", True)]
    ing.stop()
    ing = run(base, mark, got, [])               # and once taken, never again
    assert settle(ing) == [] and len(got) == 1


@pytest.mark.parametrize("bad", ["{not json", "[1]", json.dumps({"v": 1, "mtime": "x", "name": 3}),
                                 json.dumps({"v": 2, "mtime": 0, "name": ""}), ""])
def test_an_unreadable_mark_falls_back_to_priming_everything(cap, bad):
    base, d, mark = cap
    mark.parent.mkdir(parents=True)
    if bad and bad.startswith("{\"v\""):
        bad = json.dumps({**json.loads(bad), "dir": str(base)})
    mark.write_text(bad)
    put(d, "307-1-call_1", age=20)               # written while down, but nothing says what was taken: as before
    got, logs = [], []
    ing = run(base, mark, got, logs)
    assert settle(ing) == [] and got == []
    assert logs == ["p25 P25: ingest mark unreadable: the 1 call(s) on disk not replayed"]
    assert json.loads(mark.read_text())["name"].endswith("307-1-call_1.json")   # a good mark again from here
    ing.stop()
    put(d, "307-2-call_2", age=10)
    ing = run(base, mark, got, [])
    assert [(c.id, c.late) for c in settle(ing)] == [("307-2-call_2", True)]


def test_another_capture_dirs_mark_is_not_used(cap):
    base, d, mark = cap
    mark.parent.mkdir(parents=True)
    mark.write_text(json.dumps({"v": 1, "dir": str(base / "elsewhere"), "mtime": 0.0, "name": "", "waiting": []}))
    put(d, "307-1-call_1", age=20)
    logs = []
    ing = run(base, mark, [], logs)
    assert settle(ing) == [] and logs == ["p25 P25: ingest mark is another capture dir's: the 1 call(s) on disk not replayed"]


def test_a_late_call_still_being_written_waits_for_its_wav(cap):
    base, d, mark = cap
    run(base, mark, [], []).stop()
    (d / "307-1-call_1.json").write_text(json.dumps({"talkgroup": 307}))
    (d / "307-1-call_1.wav").write_bytes(b"")                 # trunk-recorder still writing it
    got, logs = [], []
    ing = run(base, mark, got, logs)
    assert settle(ing) == [] and logs == []                   # waited for, not dropped
    write_call(d, "307-1-call_1", 307, [RADIO])               # finished
    assert ing.poll() == []                                   # its size just changed: one more poll
    assert [(c.id, c.late) for c in ing.poll()] == [("307-1-call_1", True)]
    assert logs == ["p25 P25: 1 calls from while it was down ingested late (0 older skipped)"]


def test_a_call_waiting_for_its_wav_at_a_stop_comes_in_at_the_next_start(cap):
    """A newer call taken first moves the mark past an older one whose WAV hadn't landed: the mark lists it, so the next
    start ingests it (late) rather than priming it as taken."""
    base, d, mark = cap
    ing = run(base, mark, [], [])
    (d / "307-1-call_1.json").write_text(json.dumps({"talkgroup": 307}))
    os.utime(d / "307-1-call_1.json", (time.time() - 30, time.time() - 30))
    put(d, "307-2-call_2", age=20)
    assert [c.id for c in settle(ing)] == ["307-2-call_2"]
    ing.stop()
    m = json.loads(mark.read_text())
    assert m["name"].endswith("307-2-call_2.json") and m["waiting"] == ["westchester/2026/10/13/307-1-call_1.json"]
    write_call(d, "307-1-call_1", 307, [RADIO])
    os.utime(d / "307-1-call_1.json", (time.time() - 30, time.time() - 30))
    got = []
    ing = run(base, mark, got, [])
    assert [(c.id, c.late) for c in settle(ing)] == [("307-1-call_1", True)]
    assert settle(ing) == []


def test_the_mark_is_written_at_most_every_few_seconds(cap, monkeypatch):
    base, d, mark = cap
    ing = run(base, mark, [], [])
    first = mark.read_text()
    put(d, "307-1-call_1", age=5)
    settle(ing)
    assert mark.read_text() == first                          # (just written at start: not again yet)
    monkeypatch.setattr(p25, "MARK_SAVE_S", 0.0)
    ing.poll()
    assert json.loads(mark.read_text())["name"].endswith("307-1-call_1.json")
    assert not list(mark.parent.glob("*.tmp"))


class Clock:
    """p25's clock, `off` seconds from the real one (a station's clock running fast, then stepped back)."""

    def __init__(self, off=0.0):
        self.off = off

    def time(self):
        return time.time() + self.off

    def monotonic(self):
        return time.monotonic()


def test_a_mark_taken_while_the_clock_ran_fast_loses_no_call(cap, monkeypatch):
    """WSL2 steps the station's clock: a mark written while it ran 2 min fast is ahead of every call after the step.
    Not trusted: every call of the last REPLAY_S comes in late (any taken before is dropped by its id in the app)."""
    base, d, mark = cap
    clock = Clock(120.0)
    monkeypatch.setattr(p25, "time", clock)
    ing = run(base, mark, [], [])
    put(d, "307-1-call_1", age=-120)                         # written while the clock ran fast
    assert [c.id for c in settle(ing)] == ["307-1-call_1"]
    ing.stop()
    for ext in (".json", ".wav"):                            # (the app removes the files of a call it took)
        (d / f"307-1-call_1{ext}").unlink()
    assert json.loads(mark.read_text())["saved_at"] > time.time() + 100
    clock.off = 0.0                                          # the clock stepped back 120 s; sizeup down meanwhile
    put(d, "307-2-call_2", age=30)                           # older than the mark's newest: would count as seen
    put(d, "307-3-call_3", age=p25.REPLAY_S + 60)
    got, logs = [], []
    ing = run(base, mark, got, logs)
    assert [(c.id, c.late) for c in settle(ing)] == [("307-2-call_2", True)]
    assert logs == ["p25 P25: ingest mark is ahead of the clock (it stepped back): every call of the last 10 min "
                    "ingested late, any taken before dropped by its id",
                    "p25 P25: 1 calls from while it was down ingested late (1 older skipped)"]
    ing.stop()
    m = json.loads(mark.read_text())
    assert m["name"].endswith("307-2-call_2.json") and m["saved_at"] <= time.time()
    logs = []
    ing = run(base, mark, got, logs)                         # and the start after that is an ordinary one
    assert settle(ing) == [] and logs == [] and len(got) == 1


# -- what a late call reaches in the app -----------------------------------------------------------------------------


class Says(FakeWhisper):
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, prompt=""):
        return self.text, 0.9


def spies(app, monkeypatch):
    """The live consumers, each still running for real, each call to it noted."""
    got: dict[str, list] = {}
    for name, obj, attr in (("alarm", app.cad.alarms, "on_traffic"), ("intel", app.cad.intel, "consider"),
                            ("hints", app.cad.hints, "consider"), ("incoming", app.cad.sources, "consider_radio"),
                            ("airport", app.cad.airport_watch, "heard_radio"), ("radiolog", app.cad.radiolog, "consider")):
        real = getattr(obj, attr)
        got[name] = []

        def spy(*a, _real=real, _name=name, **kw):
            got[_name].append(a[0] if a else None)
            return _real(*a, **kw)
        monkeypatch.setattr(obj, attr, spy)
    got["context"] = []
    app.cad.comms.on_context_text = got["context"].append
    return got


class Push:
    """The alarms' notifier, kept here: each push it was asked for."""
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        self.sent.append((title, body))
        return True


LATE = "min late)"


def late(n):
    return f"(raised {n} min late)"


def test_a_late_emergency_call_alarms_once_labelled_late_and_raises_no_pre_alert(tmp_path, monkeypatch):
    """A firefighter in trouble during a restart is never left unalarmed: the button and the mayday on a call from while
    sizeup was down alarm and push once, labelled late at the call's own time. Nothing else that alerts hears it."""
    from sizeup.app import App
    from sizeup.cad.alarms import _hms, line
    from tests.test_cloud import base as cloud_base
    app = App(cloud_base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite"),
                         p25_talkgroups={307: "Fire/EMS 15"}))
    try:
        app.stt.client = Says("mayday mayday mayday, invented unit trapped")
        app.stt.start()
        push = app.cad.alarms.notifier = Push()
        got = spies(app, monkeypatch)
        d = day_dir(tmp_path / "p25")
        mark = tmp_path / "state" / "p25_ingest_mark.json"
        run(tmp_path / "p25", mark, [], [], on_call=app._call).stop()
        put(d, "307-1-call_1", age=200, emergency=True)       # the button, while sizeup was down: 3+ min ago
        ing = run(tmp_path / "p25", mark, [], [], on_call=app._call)
        assert [c.id for c in settle(ing)] == ["307-1-call_1"]
        assert wait(lambda: rows(app).get("307-1-call_1", {}).get("status") == "done")
        r = rows(app)["307-1-call_1"]
        assert r["emergency"] == 1 and r["text"].startswith("mayday")              # in the radio log, as heard
        assert getattr(app.store.loose[0], "late", False) is True and app.has_clip("307-1-call_1")
        # one alarm (the button, then its words on the same alarm), one push, both saying it is late
        al = app.cad.store.q("SELECT * FROM alarms")
        assert len(al) == 1 and al[0]["traffic_id"] == "307-1-call_1" and al[0]["emergency"] == 1
        a = app.cad.alarms.get(al[0]["id"])
        assert a["keywords"] and 190 <= a["late"] <= 260
        said = float(json.loads((tmp_path / "emergency" / "307-1-call_1.json").read_text())["start_time"])
        assert said <= a["at"] <= said + 1 and time.time() - a["at"] >= 150     # when it was said, not when it came in
        assert a["what"].endswith(late(3)) and line(a).endswith(f"at {_hms(a['at'])} {late(3)}")
        assert len(push.sent) == 1 and late(3) in push.sent[0][0]
        assert push.sent[0][1].startswith(_hms(a["at"]))                             # its own time, not now
        # no pre-alert of any kind; the per-incident radio log still takes it
        assert got["radiolog"] == ["307-1-call_1"]
        assert {k: v for k, v in got.items() if k in ("intel", "hints", "incoming", "airport", "context")} == {
            "intel": [], "hints": [], "incoming": [], "airport": [], "context": []}
        # a later re-run of its words (a person's, or AWS's text arriving): the same alarm, never a second push
        app._retext("307-1-call_1", "mayday mayday, invented unit still trapped", "done", 0.9, "m.bin")
        assert len(app.cad.store.q("SELECT * FROM alarms")) == 1 and len(push.sent) == 1
        assert got["intel"] == got["hints"] == [] and got["radiolog"] == ["307-1-call_1"] * 2
        # taken before a restart the mark didn't hear of (a crash between two writes): dropped, never a second row
        again = Call("307-1-call_1", 307, "Fire/EMS 15", time.time() - 30, time.time() - 28, [str(RADIO)],
                     np.zeros(32000, np.float32), "", [Turn(0.0, str(RADIO), str(RADIO))], emergency=True, late=True)
        app._call(again)
        assert len(app.store.loose) == 1 and len(push.sent) == 1
        # a call live, from someone else: its own alarm, not late, and the pre-alert hooks hear it (the spies are wired)
        put(d, "307-2-call_2", emergency=True, radio=OTHER)
        assert [(c.id, c.late) for c in settle(ing)] == [("307-2-call_2", False)]
        assert wait(lambda: rows(app).get("307-2-call_2", {}).get("status") == "done")
        assert wait(lambda: len(app.cad.store.q("SELECT * FROM alarms")) == 2)
        live = app.cad.store.one("SELECT * FROM alarms WHERE traffic_id = '307-2-call_2'")
        assert live["late"] == 0 and LATE not in app.cad.alarms.get(live["id"])["what"]
        assert "307-2-call_2" in got["intel"] and "307-2-call_2" in got["hints"]
    finally:
        app.stop()


def test_live_fire_traffic_goes_ahead_of_late_calls_in_whisper(tmp_path):
    """After a restart up to REPLAY_S of late calls arrive at once: each goes in at whisper's RERUN class, after every
    live clip (a live Fire/EMS 15 key-up is never held behind them, and they never make whisper "behind"). A late
    emergency keeps its own class."""
    from sizeup.app import App
    from sizeup.stt import RERUN, URGENT
    from tests.test_app import voice
    from tests.test_cloud import base as cloud_base
    app = App(cloud_base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite"),
                         p25_talkgroups={307: "Fire/EMS 15"}))
    try:
        now = time.time()

        def c(cid, at, late=False, emergency=False, radio=RADIO):
            return Call(cid, 307, "Fire/EMS 15", at, at + 2, [str(radio)], voice(2.0, 1), "",
                        [Turn(0.0, str(radio), str(radio), at=at)], emergency=emergency, late=late)
        for k in range(3):
            app._call(c(f"307-{k}-call_{k}", now - 300 + 10 * k, late=True))
        app._call(c("307-8-call_8", now - 200, late=True, emergency=True, radio=OTHER))
        app._call(c("307-9-call_9", now - 2))                                       # live
        order = [(prio, item.id) for prio, item, _a, _ch in app.stt._q.withdraw_all()]
        assert order == [(URGENT, "307-8-call_8"), (URGENT, "307-9-call_9"),
                         (RERUN, "307-0-call_0"), (RERUN, "307-1-call_1"), (RERUN, "307-2-call_2")]
    finally:
        app.stop()


def test_a_late_emergency_already_alarmed_live_before_the_restart_alarms_once(tmp_path):
    """The alarms dedupe a late emergency by its id and by its radio at the time it was said (not when it arrived): the
    button that radio pressed live just before the restart is the same emergency."""
    from tests.test_radios import Clock, cad
    clock = Clock()
    c = cad(tmp_path, clock)
    push = c.alarms.notifier = Push()
    t0 = clock.t

    def em(tid, at, radio=str(RADIO), late=False):
        return {"id": tid, "start": at, "end": at + 3, "source": "Fire/EMS 15", "talkgroup": 307, "unit": radio, "rig": "",
                "radio_id": radio, "text": "", "emergency": True, **({"late": True} if late else {})}
    first = c.alarms.on_traffic(em("307-1-call_1", t0 - 3))                     # live, before the restart
    assert first is not None and len(push.sent) == 1
    clock.t = t0 + 400                                                          # sizeup comes back 6+ min later
    # the same call again (a crash between two mark writes): its own alarm, no second push
    assert c.alarms.on_traffic(em("307-1-call_1", t0 - 3, late=True))["id"] == first["id"]
    # the same radio's next call, written while it was down: the same emergency (its start within REPEAT_S of the last)
    assert c.alarms.on_traffic(em("307-2-call_2", t0 + 30, late=True))["id"] == first["id"]
    assert len(c.store.q("SELECT * FROM alarms")) == 1 and len(push.sent) == 1
    # another radio's button from while it was down: its own alarm, labelled late
    other = c.alarms.on_traffic(em("307-3-call_3", t0 + 60, radio=str(OTHER), late=True))
    assert other["id"] != first["id"] and other["late"] > 0 and LATE in other["what"]
    assert len(push.sent) == 2 and LATE in push.sent[1][0]


def test_a_station_sends_a_late_emergency_up_as_a_late_alarm(tmp_path, monkeypatch):
    from sizeup.app import App
    from tests.test_cloud import base as cloud_base
    app = App(cloud_base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite"),
                         p25_talkgroups={307: "Fire/EMS 15"}))
    try:
        sent = []
        monkeypatch.setattr(app, "forwarding", lambda: True)
        app.events = lambda kind, data, clip: sent.append((kind, data))
        app.stt.client = Says("invented unit on location")
        app.stt.start()
        now = time.time() - 30
        app._call(Call("307-7-call_7", 307, "Fire/EMS 15", now, now + 2, [str(OTHER)], np.full(32000, 0.2, np.float32),
                       "", [Turn(0.0, str(OTHER), str(OTHER), at=now)], emergency=True, late=True))
        assert wait(lambda: any(k == "traffic" for k, _d in sent))
        alarm = [d for k, d in sent if k == "alarm"]                   # ahead of its transcript, as a live one's
        assert len(alarm) == 1 and alarm[0]["late"] is True and alarm[0]["start"] == now
        t = next(d for k, d in sent if k == "traffic")
        assert t["id"] == "307-7-call_7" and t["late"] is True and t["emergency"] is True
    finally:
        app.stop()


def test_the_hub_alarms_a_late_emergency_from_its_station_once_and_nothing_else(tmp_path):
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app, now = hub.app, time.time()
        push = app.cad.alarms.notifier = Push()
        heard = {"text": [], "late": []}
        app.cad.comms.on_text = heard["text"].append
        app.cad.comms.on_late_text = heard["late"].append

        def ev(seq, tid, kind="traffic", **kw):
            data = {"id": tid, "source": "Fire/EMS 15", "talkgroup": 307, "start": now - 240, "end": now - 237,
                    "text": "mayday mayday mayday", "raw": "mayday mayday mayday", "status": "done", "model": "m.bin",
                    "unit": str(OTHER), "radio_id": str(OTHER), "emergency": True, **kw}
            if kind == "retext":
                data = {"id": tid, "text": kw.get("text", ""), "status": "done", "model": "m.bin",
                        **({"late": True} if kw.get("late") else {})}
            return {"seq": seq, "at": now, "type": kind, "data": data}
        post = lambda e: call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [e]})[0]  # noqa: E731
        row = lambda tid: app.cad.store.one("SELECT text, emergency FROM radio_traffic WHERE id = ?", (tid,)) or {}  # noqa: E731
        alarms = lambda: app.cad.store.q("SELECT * FROM alarms ORDER BY id")  # noqa: E731
        assert post(ev(1, "307-8-call_8", late=True)) == 200
        assert wait(lambda: row("307-8-call_8").get("text") == "mayday mayday mayday", 10)
        assert row("307-8-call_8")["emergency"] == 1
        assert wait(lambda: len(alarms()) == 1, 10)
        a = app.cad.alarms.get(alarms()[0]["id"])
        assert a["traffic_id"] == "307-8-call_8" and a["at"] == now - 240 and a["what"].endswith(late(4))
        assert len(push.sent) == 1 and late(4) in push.sent[0][0]
        assert heard == {"text": [], "late": ["307-8-call_8"]}                    # no pre-alert hook
        # its words again from the station (whisper standing in, a re-run): the same alarm, no second push
        assert post(ev(2, "307-8-call_8", "retext", text="mayday mayday, invented unit trapped", late=True)) == 200
        assert wait(lambda: row("307-8-call_8").get("text") == "mayday mayday, invented unit trapped", 10)
        assert heard["text"] == [] and len(alarms()) == 1 and len(push.sent) == 1
        # the same event again (the outbox resends): one row, one alarm
        assert post(ev(3, "307-8-call_8", late=True)) == 200
        assert len(app.cad.store.q("SELECT id FROM radio_traffic WHERE id = '307-8-call_8'")) == 1
        assert len(alarms()) == 1 and len(push.sent) == 1
        # a live one from another radio: its own alarm, not late, and the pre-alert hook hears it
        assert post(ev(4, "307-9-call_9", radio_id=str(RADIO), unit=str(RADIO), start=now - 3, end=now)) == 200
        assert wait(lambda: len(alarms()) == 2, 10)
        assert alarms()[1]["late"] == 0 and heard["text"] == ["307-9-call_9"]
    finally:
        hub.close()


def test_a_late_call_on_another_system_reaches_no_intel_hook(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    heard = []
    app.cad.comms.on_context_text = heard.append                 # the intel pre-alerts' hook
    late = a_call(app, "1-call_1", PD, "mrrs", label="PD Dispatch")
    late.late = True
    app._call_system(rt, late)
    assert wait(lambda: rows(app).get(f"{PD}-1-call_1", {}).get("status") == "done")
    assert heard == []
    app._call_system(rt, a_call(app, "2-call_2", PD, "mrrs", label="PD Dispatch"))
    assert wait(lambda: heard == [f"{PD}-2-call_2"])
    assert rt.ingest.mark_path.name == "p25_mrrs_ingest_mark.json" and app.p25.mark_path.name == "p25_ingest_mark.json"
