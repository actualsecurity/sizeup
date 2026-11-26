"""Shed clips caught up (sizeup/sttcatchup.py): a clip speech-to-text skipped while it was busy ("Skipped: busy") is
run again once whisper, or its AWS channel, is idle; the alert tier first, then the newest; never older than the
max age; AWS only on a context channel's own budget, never ours; fire traffic never waits behind it, and nothing
that alerts ever hears its old words. Synthetic clips, local fakes only."""

import threading
import time

import numpy as np
import pytest

from sizeup import stt as stt_mod
from sizeup import sttcatchup
from sizeup.stt import CATCHUP, CONTEXT, HIGH, LIVE, LOW, RERUN, SHED, URGENT, SttQueue, Transcriber
from sizeup.sttcatchup import CatchUp, whisper_idle
from tests.test_app import wait
from tests.test_channels import AWS_TEXT, aws_cfg, comms_row, fake_aws  # noqa: F401 (fixture)
from tests.test_cloud import HubRun, base, call, fm_segment, hub_cfg, station, station_cfg

PD = "p25:mrrs:9211"              # an invented police dispatch talkgroup on another P25 system
SHED_WHY = f"{SHED}: speech-to-text 15 min behind, a context talkgroup (Re-transcribe it if it matters)"


class Whisper:
    """whisper-server stand-in: the text by audio length, every call (and its kind) in order."""

    def __init__(self, texts, hold: threading.Event | None = None):
        self.texts, self.hold, self.calls, self.timeouts = texts, hold, [], []

    def transcribe(self, audio, prompt="", fast=False, timeout_s=None):
        self.calls.append(("fast" if fast else "full", round(len(audio) / 16000)))
        self.timeouts.append(timeout_s)
        if self.hold is not None:
            self.hold.wait(5)
        return self.texts.get(round(len(audio) / 16000), "words"), 0.9


# -- the queue: under every class, held while fire traffic comes in -------------------------------------------------


def q_at(clock):
    return SttQueue(lambda item, why: None, clock=lambda: clock["t"])


def test_a_catch_up_clip_goes_after_every_other_class(monkeypatch):
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    clock = {"t": 1000.0}
    q = q_at(clock)
    q.put((CATCHUP, None, "catchup", None, None))
    for prio, item in ((LOW, "context"), (CONTEXT, "alert"), (RERUN, "rerun"), (LIVE, "live"), (HIGH, "tower"),
                       (URGENT, "fe15")):
        q.put((prio, None, item, None, "x"))
    assert q.qsize() == 7 and q.qsize(catchup=False) == 6
    assert [q.get_nowait()[2] for _ in range(7)] == ["fe15", "tower", "live", "rerun", "alert", "context", "catchup"]
    st = q.stats()
    assert st["catchup"] == 0 and st["queued"] == 0


def test_fire_traffic_coming_in_holds_the_catch_up_until_it_is_quiet():
    assert stt_mod.CATCHUP_QUIET_S == 90.0
    clock = {"t": 1000.0}
    q = q_at(clock)
    q.put((CATCHUP, None, "catchup", None, None))
    with pytest.raises(stt_mod.queue.Empty):                 # just started: what came before isn't known, it waits
        q.get_nowait()
    clock["t"] += stt_mod.CATCHUP_QUIET_S
    assert q.get_nowait()[2] == "catchup"
    q.put((CATCHUP, None, "catchup-2", None, None))
    q.put((URGENT, None, "fe15", None, "tg:307"))            # fire traffic arrives with a catch-up clip waiting
    assert q.get_nowait()[2] == "fe15"                       # fire first, always
    with pytest.raises(stt_mod.queue.Empty):                 # and the catch-up waits for a quiet spell after it
        q.get_nowait()
    clock["t"] += stt_mod.CATCHUP_QUIET_S - 1
    with pytest.raises(stt_mod.queue.Empty):
        q.get_nowait()
    q.ours_heard()                                           # a fire clip that went to AWS: never in this queue,
    clock["t"] += stt_mod.CATCHUP_QUIET_S - 1                # ... but the quiet spell starts again
    with pytest.raises(stt_mod.queue.Empty):
        q.get_nowait()
    assert q.stats()["ours_ago_s"] == stt_mod.CATCHUP_QUIET_S - 1
    clock["t"] += 1
    assert q.get_nowait()[2] == "catchup-2"
    # a catch-up clip is never counted as traffic coming in, nor as whisper being behind
    q.put((CATCHUP, None, "c3", None, None))
    clock["t"] += stt_mod.BEHIND_S + 1
    st = q.stats()
    assert st["catchup"] == 1 and st["in_h"] == 2 and not q.behind() and st["top"] == []   # (in_h: fe15 alone)


def test_a_blocking_get_wakes_for_a_held_catch_up_clip(monkeypatch):
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.3)
    q = SttQueue(lambda item, why: None)
    q.put((LIVE, None, "fg1", None, "FG1"))
    q.put((CATCHUP, None, "catchup", None, None))
    assert q.get(timeout=1)[2] == "fg1"
    t0 = time.monotonic()
    assert q.get(timeout=2)[2] == "catchup"
    assert 0.2 <= time.monotonic() - t0 < 1.5


class Item:
    def __init__(self, cid):
        self.id = cid


def test_fire_is_never_delayed_by_the_catch_up_on_a_running_whisper(monkeypatch):
    """Whisper running: catch-up clips queued, then a fire clip: the fire clip is transcribed next, with full decoding;
    the catch-up clips only after a quiet spell, each one greedy pass."""
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.5)
    done: list[tuple[str, float]] = []
    gate = threading.Event()
    w = Whisper({1: "catch", 2: "fire"}, hold=gate)
    t = Transcriber(w, lambda item: done.append((item.id, time.monotonic())))
    t.submit(Item("first-catchup"), np.zeros(16000, np.float32), priority=CATCHUP)
    t.start()
    assert wait(lambda: len(w.calls) == 1, 3)               # whisper busy on a catch-up clip (nothing else came)
    assert t.busy() == 1 and t.busy(catchup=False) == 0     # (never "busy" for the station's update)
    for i in range(3):
        t.submit(Item(f"catchup-{i}"), np.zeros(16000, np.float32), priority=CATCHUP)
    t.submit(Item("fire"), np.zeros(32000, np.float32), priority=URGENT, channel="tg:307")
    gate.set()
    assert wait(lambda: len(done) == 5, 10)
    order = [d[0] for d in done]
    assert order[:2] == ["first-catchup", "fire"]           # the fire clip next: never behind a waiting catch-up
    assert w.calls[1] == ("full", 2) and w.timeouts[1] is None      # with full decoding
    assert all(c == ("fast", 1) for i, c in enumerate(w.calls) if i != 1)
    assert done[2][1] - done[1][1] >= 0.4                    # the catch-up waited for the quiet spell


def test_a_catch_up_clip_gets_one_short_try_and_never_backs_off(monkeypatch):
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    calls, done = [], []

    class Down:
        def transcribe(self, audio, prompt="", **kw):
            calls.append(kw)
            raise TimeoutError("timed out")

    t = Transcriber(Down(), lambda item: done.append((item.id, item.status, time.monotonic())))
    t0 = time.monotonic()
    t.submit(Item("catchup"), np.zeros(16000, np.float32), priority=CATCHUP)
    t.start()
    assert wait(lambda: done, 3)
    assert done[0][:2] == ("catchup", "error") and done[0][2] - t0 < 1.0          # no 2 + 5 + 8 s of backoff
    assert calls == [{"fast": True, "timeout_s": stt_mod.CATCHUP_TIMEOUT_S}] and stt_mod.CATCHUP_TIMEOUT_S <= 20


# -- the driver --------------------------------------------------------------------------------------------------


def rig(rows, **kw):
    """A CatchUp over fake hooks: what was sent where. The rows are the radio log's (their tries on them)."""
    sent = {"whisper": [], "aws": [], "gave_up": [], "route": [], "caught": 0}
    st = {"on": True, "age": 6.0, "may": True, "route": {}, "ready": (True, ""), "aws_idle": True, "pause": "",
          "whisper": lambda specs: {"queued": [s["id"] for s in specs], "refused": {}, "hold": ""},
          "aws": lambda row: (True, "")}

    def to_whisper(specs):
        sent["whisper"].append([s["id"] for s in specs])
        return st["whisper"](specs)

    def to_aws(row):
        sent["aws"].append(row["id"])
        return st["aws"](row)

    def scan(since, limit, sort):
        out, counts = [], {}
        for r in rows:
            if r["start"] < since:
                continue
            word = sort(r)
            counts[word] = counts.get(word, 0) + 1
            if not word and len(out) < limit:
                out.append(r)
        return out, counts

    def route(key):
        sent["route"].append(key)
        return st["route"].get(key, "whisper")

    def caught(since):
        sent["caught"] += 1
        return 7

    cu = CatchUp(settings=lambda: (st["on"], st["age"]), may_run=lambda: st["may"], pause=lambda: st["pause"], scan=scan,
                 caught=caught, route=route, whisper_ready=lambda after: st["ready"],
                 aws_idle=lambda key: st["aws_idle"], to_whisper=to_whisper, to_aws=to_aws,
                 give_up=lambda cid, why: sent["gave_up"].append((cid, why)), log=lambda m: None, **kw)
    return cu, sent, st


def rows_at(now, n, key="tg:383", **kw):
    return [{"id": f"c{i}", "key": key, "source": "x", "unit": "", "start": now - 60 * (i + 1), "seconds": 4.0,
             "service": "", "tier": "", "tries": 0, "tried_at": 0.0, **kw} for i in range(n)]


def test_small_batches_only_when_whisper_is_idle_and_never_twice_too_soon():
    now = 100000.0
    rows = rows_at(now, 8)
    cu, sent, st = rig(rows)
    st["ready"] = (False, "fire clips waiting")
    assert cu.tick(now) == 0 and sent["whisper"] == []
    assert "fire clips waiting" in cu.stats()["why"] and cu.stats()["waiting"] == 8 and cu.stats()["caught_24h"] == 7
    st["ready"] = (True, "")
    assert cu.tick(now) == sttcatchup.WHISPER_BATCH and sent["whisper"] == [["c0", "c1", "c2"]]
    assert cu.tick(now + 15) == 3 and sent["whisper"][-1] == ["c3", "c4", "c5"]      # (rows still skipped: the rest)
    cu.tick(now + 30)
    assert sent["whisper"][-1] == ["c6", "c7"] and cu.stats()["waiting"] == 8         # (in flight: still the backlog)
    assert cu.tick(now + 45) == 0
    # one that failed says so on its row (tries): tried again after RETRY_S; twice failed: given up
    rows[0].update(tries=1, tried_at=now + 5)
    rows[1].update(tries=2, tried_at=now + 5)
    assert cu.tick(now + 60) == 0 and cu.stats()["gave_up"] == 1 and cu.stats()["waiting"] == 7
    assert cu.tick(now + 30 + sttcatchup.RETRY_S) == 3 and sent["whisper"][-1] == ["c0", "c2", "c3"]
    # sent twice and never heard of again (a station restarted with it queued): given up too
    later = now + 4 * sttcatchup.RETRY_S
    for k in range(6):
        cu.tick(later + 15 * k)
    assert cu.tick(later + 2 * sttcatchup.RETRY_S) == 0 and cu.stats()["gave_up"] >= 7


def test_fire_work_pauses_all_of_it_whisper_and_aws():
    now = 100000.0
    rows = rows_at(now, 2, PD) + rows_at(now, 2)
    for i, r in enumerate(rows):
        r["id"] = f"r{i}"
    cu, sent, st = rig(rows)
    st["route"] = {PD: "aws"}
    for why in ("an event is open", "an alarm is open", "carrier open on FG1", "someone is talking"):
        st["pause"] = why
        assert cu.tick(now) == 0 and cu.stats()["why"] == f"paused: {why}"
    assert sent["whisper"] == [] and sent["aws"] == [] and cu.stats()["waiting"] == 4
    st["pause"] = ""
    assert cu.tick(now) == 4


def test_off_too_old_or_not_the_cad_of_record_sends_nothing():
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 3) + [dict(rows_at(now, 1)[0], id="old", start=now - 7 * 3600)])
    st["on"] = False
    assert cu.tick(now) == 0 and cu.stats()["on"] is False and cu.stats()["waiting"] is None
    st["on"], st["may"] = True, False
    assert cu.tick(now) == 0 and cu.stats()["why"] == "not the CAD of record"
    st["may"] = True
    assert cu.stats()["waiting"] is None
    cu.tick(now)
    assert "old" not in sent["whisper"][0] and cu.stats()["waiting"] == 3     # older than 6 h: stays skipped
    st["age"] = 8.0
    cu.tick(now + 1)
    assert cu.stats()["waiting"] == 4 and sent["whisper"][-1] == ["old"]


def test_aws_rows_go_to_aws_when_their_channel_is_idle_and_whisper_ones_to_whisper():
    now = 100000.0
    rows = rows_at(now, 3, PD) + rows_at(now, 2)
    for i, r in enumerate(rows):
        r["id"] = f"r{i}"
    cu, sent, st = rig(rows)
    st["route"] = {PD: "aws"}
    st["aws_idle"] = False
    cu.tick(now)
    assert sent["aws"] == [] and sent["whisper"] == [["r3", "r4"]] and f"{PD}: AWS busy" in cu.stats()["why"]
    st["aws_idle"] = True
    cu.tick(now + 15)
    assert sent["aws"] == ["r0", "r1"]                      # AWS_BATCH a channel a tick
    st["aws"] = lambda row: (False, "no recording")         # one that can never go: said on its row, once
    cu.tick(now + 30)
    assert sent["aws"][-1] == "r2" and sent["gave_up"] == [("r2", "no recording")]


def test_a_failed_station_call_burns_no_tries_and_a_refused_clip_is_given_up():
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 3))
    st["whisper"] = lambda specs: None                      # offline, a network error, an older station refusing
    for k in range(5):
        assert cu.tick(now + 15 * k) == 0
    assert sent["whisper"] == [["c0", "c1", "c2"]] * 5 and cu._sent == {} and cu.stats()["waiting"] == 3
    assert "didn't take the call" in cu.stats()["why"]
    st["whisper"] = lambda specs: {"queued": [], "refused": {}, "hold": "a station update is waiting"}
    assert cu.tick(now + 90) == 0 and cu._sent == {} and "update is waiting" in cu.stats()["why"]
    st["whisper"] = lambda specs: {"queued": ["c0", "c2"], "refused": {"c1": "no recording on the station"}, "hold": ""}
    assert cu.tick(now + 105) == 2 and sent["gave_up"] == [("c1", "no recording on the station")]
    assert set(cu._sent) == {"c0", "c2"}


def test_a_write_refused_on_a_fenced_task_stops_it_quietly_until_it_is_the_leader_again():
    from sizeup.fence import FencedWrite
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 3))
    said = []
    cu.log = said.append

    def fenced(cid, why):                                   # the lease ran out in this tick: the row's write is refused
        raise FencedWrite("not the leader: UPDATE refused (nothing written)")
    cu.give_up = fenced
    st["whisper"] = lambda specs: {"queued": [], "refused": {"c0": "no recording on the station"}, "hold": ""}
    assert cu.tick(now) == 0 and len(said) == 1 and "leader" in said[0]          # no crash: said once
    assert cu._sent == {} and cu.stats()["why"] == "not the CAD of record" and cu.stats()["error"] == ""
    st["may"] = False                                       # (the fence says so from here on)
    for k in range(1, 5):
        assert cu.tick(now + 15 * k) == 0
    assert len(said) == 1 and len(sent["whisper"]) == 1 and cu.stats()["waiting"] is None
    st["may"] = True                                        # the leader again: it goes on
    st["whisper"] = lambda specs: {"queued": [s["id"] for s in specs], "refused": {}, "hold": ""}
    assert cu.tick(now + 90) == 3 and len(said) == 1


def test_the_thread_outlives_a_refused_write():
    from sizeup.fence import FencedWrite
    cu, sent, st = rig([])
    said, ticks = [], []
    cu.log = said.append

    def scan(since, limit, sort):
        ticks.append(1)
        st["may"] = False
        raise FencedWrite("not the leader: UPDATE refused (nothing written)")
    cu.scan = scan
    old, sttcatchup.TICK_S = sttcatchup.TICK_S, 0.01
    try:
        cu.start(0.0)
        wait(lambda: cu.stats()["why"] == "not the CAD of record" and ticks)
        time.sleep(0.1)
        assert cu._thread.is_alive() and len(said) == 1 and len(ticks) == 1
    finally:
        cu.stop()
        sttcatchup.TICK_S = old


def test_a_fenced_all_in_one_gives_up_no_row_and_says_it_is_not_the_cad_of_record(tmp_path, monkeypatch):
    from sizeup.app import App
    from sizeup.fence import FencedWrite
    app = App(base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite")))
    try:
        tid = shed_one(app, monkeypatch)
        before = comms_row(app, tid)
        app.cad.store.may_write = lambda: False             # the lease ran out (sizeup/fence.py)
        with pytest.raises(FencedWrite):
            app._catchup_give_up(tid, "no recording")       # refused, and said to the driver (never swallowed)
        assert comms_row(app, tid) == before
        app.catchup.give_up = app._catchup_give_up
        app.catchup.to_whisper = lambda specs: {"queued": [], "refused": {tid: "no recording"}, "hold": ""}
        app.catchup.whisper_ready = lambda after: (True, "")
        app.catchup.pause = lambda: ""
        assert app.catchup.tick() == 0 and app.catchup.stats()["why"] == "not the CAD of record"
        assert comms_row(app, tid) == before
    finally:
        app.stop()


def test_rows_that_cant_go_never_keep_the_ones_behind_them_from_their_turn():
    now = 100000.0
    spent = rows_at(now, 100, tries=2)                      # the 100 newest: given up
    off = [dict(r, id=f"off{i}", key="tg:999", start=now - 7000 - i) for i, r in enumerate(rows_at(now, 30))]
    long_w = [dict(r, id=f"long{i}", seconds=45.0, start=now - 8000 - i) for i, r in enumerate(rows_at(now, 2))]
    long_a = [dict(r, id=f"pdlong{i}", seconds=45.0, start=now - 8500 - i) for i, r in enumerate(rows_at(now, 1, PD))]
    good = [dict(r, id=f"good{i}", start=now - 9000 - i) for i, r in enumerate(rows_at(now, 5))]
    cu, sent, st = rig(spent + off + long_w + long_a + good)
    st["route"] = {"tg:999": "", PD: "aws"}
    assert cu.tick(now) == 4
    assert sent["whisper"] == [["good0", "good1", "good2"]] and sent["aws"] == ["pdlong0"]   # (a long one: AWS only)
    s = cu.stats()
    assert s["waiting"] == 6 and s["gave_up"] == 102          # eligible only; given up (and too long) apart
    assert sorted(set(sent["route"])) == sorted({"tg:383", "tg:999", PD}) and len(sent["route"]) == 3   # once a key


def test_the_24_h_count_is_read_once_a_minute():
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 1))
    for k in range(4):
        cu.tick(now + 15 * k)
    assert sent["caught"] == 1
    cu.tick(now + 61)
    assert sent["caught"] == 2


def test_whisper_idle_wants_nothing_of_ours_nor_an_alert_clip_waiting():
    ok = {"queued": 2, "urgent": 0, "high": 0, "live": 0, "context": 0, "low": 2, "rerun": 0, "catchup": 0}
    assert whisper_idle(ok) == (True, "")
    for k in ("urgent", "high", "live", "context", "catchup"):
        assert whisper_idle(dict(ok, **{k: 1}))[0] is False
    assert whisper_idle(dict(ok, queued=sttcatchup.QUEUE_IDLE_MAX + 1))[0] is False
    assert whisper_idle(None)[0] is False


def test_nothing_is_read_or_written_at_start():
    calls = []
    cu, sent, st = rig([])
    cu.settings = lambda: calls.append("settings") or (True, 6.0)
    cu.start()
    time.sleep(0.2)
    cu.stop()
    assert calls == [] and sent["caught"] == 0 and sttcatchup.START_DELAY_S >= 60


# -- the radio log: which rows, in which order ---------------------------------------------------------------------


def record(app, tid, start, *, status="skipped", model=f"m.bin ({SHED_WHY})", tg=None, system="", tier="", seconds=2.0,
           **kw):
    app.cad.comms.record({"id": tid, "start": start, "end": start + seconds, "source": "Chan" if tg is None else "",
                          "talkgroup": tg, "system": system, "tier": tier, "unit": "", "text": "", "status": status,
                          "model": model, **kw})


def test_the_shed_rows_alert_tier_first_then_newest_their_keys_and_their_tries(tmp_path):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    comms = app.cad.comms
    now = time.time()
    record(app, "a-old", now - 3000, tg=9211, system="mrrs", tier="alert")
    record(app, "t-new", now - 100, tg=9570, system="mrrs", tier="text", seconds=41.0)
    record(app, "county", now - 200, tg=383, system="westchester")
    record(app, "a-new", now - 500, tg=9211, system="mrrs", tier="alert")
    record(app, "fm", now - 50)
    record(app, "too-old", now - 7 * 3600, tg=383, system="westchester")
    record(app, "error", now - 60, status="error", model="m.bin (not transcribed: speech-to-text queue full)")
    record(app, "done", now - 70, status="done", model="m.bin")
    rows, counts = comms.shed_clips(now - 6 * 3600)
    assert [r["id"] for r in rows] == ["a-new", "a-old", "fm", "t-new", "county"] and counts == {"": 5}
    assert {r["id"]: r["key"] for r in rows} == {"a-new": PD, "a-old": PD, "fm": "Chan", "t-new": "p25:mrrs:9570",
                                                 "county": "tg:383"}
    assert {r["id"]: round(r["seconds"]) for r in rows}["t-new"] == 41 and all(r["tries"] == 0 for r in rows)
    assert len(comms.shed_clips(now - 8 * 3600)[0]) == 6
    # a failed try is on the row and counted from it; sorting happens before the limit
    app._rerun_finish("a-new", "failed: speech-to-text error; catch-up")       # (as the station's event says it)
    app._rerun_finish("a-new", "failed: AWS: timed out; catch-up")
    app._catchup_give_up("fm", "no recording on the station")
    got = {r["id"]: r["tries"] for r in comms.shed_clips(now - 6 * 3600)[0]}
    assert got["a-new"] == 2 and got["fm"] == sttcatchup.MAX_TRIES and comms.catchup_tries("a-old") == 0
    rows, counts = comms.shed_clips(now - 6 * 3600, 1, lambda r: "gave up" if r["tries"] >= 2 else "")
    assert [r["id"] for r in rows] == ["a-old"] and counts == {"gave up": 2, "": 3}
    assert comms.caught_up(now - 60, now - 86400) == 0          # a failed try is never "caught up"


# -- all-in-one, end to end: shed by the real queue, caught up through the re-run path ------------------------------


def shed_one(app, monkeypatch, segment=fm_segment, prio=LOW, after=stt_mod.LOW_MAX_WAIT_S):
    """A transmission shed for real: its clip waits in the whisper queue (a context talkgroup's class, LOW) until
    whisper, stopped meanwhile, is `after` behind. Its id."""
    with monkeypatch.context() as m:
        if prio is not None:
            m.setattr(app, "_stt_priority", lambda item, key: prio)
        tid = segment(app)
    q = app.stt._q
    assert wait(lambda: q.qsize() == 1, 5)
    real = q.clock
    q.clock = lambda: real() + after + 5
    with pytest.raises(stt_mod.queue.Empty):
        q.get_nowait()
    q.clock = real
    if tid is None:                                          # (a segment helper that doesn't say its id)
        assert wait(lambda: len(app.store.loose) == 1, 5)
        tid = next(iter(app.store.loose)).id
    assert wait(lambda: (comms_row(app, tid) or {}).get("status") == "skipped", 5), comms_row(app, tid)
    return tid


def ready(app, monkeypatch):
    """Whisper up and the start's quiet spell over."""
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    app.whisper.state = "ok"
    app.stt.start()


def test_all_in_one_a_shed_clip_is_caught_up_like_any_rerun(tmp_path, monkeypatch):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    w = app.stt.client = Whisper({3: "Car 12 clear of the scene"})
    try:
        tid = shed_one(app, monkeypatch)
        row = comms_row(app, tid)
        assert row["model"].startswith("m.bin (skipped: busy") and row["text"] == ""
        ready(app, monkeypatch)
        rev = app.cad.comms.rev
        assert app.catchup.tick() == 1
        assert wait(lambda: (comms_row(app, tid) or {}).get("rerun_result"), 10), comms_row(app, tid)
        row = comms_row(app, tid)
        assert row["status"] == "done" and row["text"] == "Car 12 clear of the scene" and row["model"] == "m.bin"
        assert row["rerun_result"] == "changed; catch-up" and app.cad.comms.rev > rev
        assert w.calls == [("fast", 3)] and w.timeouts == [stt_mod.CATCHUP_TIMEOUT_S]
        assert app.rerun.get("total", 0) == 0 and not app._catchup_ids and not app._rerun_ids   # no person's progress
        app.catchup._caught = (0.0, None)
        app.catchup.tick()
        assert app.catchup.stats()["caught_24h"] == 1 and app.catchup.stats()["waiting"] == 0
        assert app.catchup.tick() == 0                            # nothing left
    finally:
        app.stop()


def test_a_catch_up_that_fails_stays_skipped_and_its_tries_outlive_a_restart(tmp_path, monkeypatch):
    from sizeup.app import App, Rerun
    cfg = base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite"))
    app = App(cfg)
    try:
        tid = shed_one(app, monkeypatch)
        for n in (1, 2):
            app._rerun_ids.add(tid)
            app._catchup_ids.add(tid)
            app._rerun_note[tid] = "catch-up"
            app._stt_dropped(Rerun(tid, "traffic", True, catchup=True), "speech-to-text queue full (2000 waiting)")
            row = comms_row(app, tid)
            assert row["status"] == "skipped" and row["text"] == ""
            assert row["rerun_result"] == f"failed: speech-to-text queue full (2000 waiting); catch-up try {n}"
            assert tid not in app._rerun_ids and tid not in app._catchup_ids and app.rerun.get("failed", 0) == 0
    finally:
        app.stop()
    again = App(cfg)                                              # a restart (the hub: a handover): the count is the row's
    try:
        again.whisper.state = "ok"
        assert again.catchup.tick() == 0
        assert again.catchup.stats()["gave_up"] == 1 and again.catchup.stats()["waiting"] == 0
    finally:
        again.stop()


def test_settings_turn_it_off_and_set_the_max_age(tmp_path):
    from sizeup.cad.api import CadError
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        s = app.cad.settings
        assert s.stt_catchup() == (True, 6.0) and s.stt_page()["catchup"] == {"on": True, "max_age_h": 6.0, "edited": False}
        s.post("stt", {"action": "set", "key": "catchup", "value": False})
        s.post("stt", {"action": "set", "key": "catchup_max_age_h", "value": 12})
        assert s.stt_catchup() == (False, 12.0) and s.stt_page()["catchup"]["edited"]
        for key, bad in (("catchup", "yes"), ("catchup_max_age_h", 0), ("catchup_max_age_h", 100), ("catchup_max_age_h", True)):
            with pytest.raises(CadError):
                s.post("stt", {"action": "set", "key": key, "value": bad})
        s.post("stt", {"action": "reset", "key": "catchup"})
        assert s.stt_catchup() == (True, 12.0)
        app.catchup.tick()
        assert app.catchup.stats()["on"] is True and app.catchup.stats()["max_age_h"] == 12.0
    finally:
        app.stop()


# -- old words alert nobody ------------------------------------------------------------------------------------------


def test_an_airband_clip_caught_up_later_raises_nothing_and_pushes_nothing(tmp_path, monkeypatch):
    """The stale alert: a shed tower clip whose words would be a pre-alert, transcribed later by the catch-up. Nothing
    is raised or pushed; the same clip re-transcribed by a person (fresh) does raise it, so the words are the kind."""
    from dataclasses import replace
    from sizeup.app import App
    from sizeup.radio.am import CarrierSquelch
    from sizeup.radio.fm import Channel
    from tests.test_app import cfg
    from tests.test_atc import FIVE, PLANES, FakeNotifier, envelope
    words = "Westchester tower, JetBlue 1234, looks like a brush fire off the departure end of runway one six"
    app = App(replace(cfg(tmp_path), channels=FIVE))
    app.stt.client = Whisper({4: words})
    push = app.cad.airport_alerts.notifier = FakeNotifier()
    heard = {"text": [], "alarm": [], "atc": []}
    real_on_text, real_alarm, real_heard = app.cad.comms.on_text, app.cad.alarms.on_traffic, app.atc.heard
    app.cad.comms.on_text = lambda tid: (heard["text"].append(tid), real_on_text(tid))[1]
    app.cad.alarms.on_traffic = lambda d: (heard["alarm"].append(d["id"]), real_alarm(d))[1]
    app.atc.heard = lambda d: (heard["atc"].append(d["id"]), real_heard(d))[1]
    app.atc.aircraft = lambda: PLANES

    def tower(a):
        sq, seg = CarrierSquelch(3.5), None
        for f in envelope([(5, 0.01, False), (3, 0.05, True), (1, 0.01, False)], np.random.default_rng(2)):
            seg = sq.feed(f) or seg
        a._segment(Channel(118.575, "HPN Tower"), seg)

    try:
        tid = shed_one(app, monkeypatch, tower, prio=None, after=stt_mod.HIGH_MAX_WAIT_S)     # (airband: shed at 5 min)
        # (#531) the shed clip's own pass, wordless, reaches the alarms just after its row says "skipped" (App._transcribed_one:
        # _to_cad, then _alarm): let it, so a slow runner doesn't count it against the catch-up below
        assert wait(lambda: tid in heard["alarm"], 5), heard
        for k in heard:
            heard[k].clear()
        ready(app, monkeypatch)
        assert app.catchup.tick() == 1
        assert wait(lambda: (comms_row(app, tid) or {}).get("rerun_result"), 10), comms_row(app, tid)
        row = comms_row(app, tid)
        assert row["text"] == words and row["rerun_result"] == "changed; catch-up"      # the transcript is there ...
        assert app._find(tid)[0].text == words
        assert heard == {"text": [], "alarm": [], "atc": []}                            # ... and nobody acted on it
        assert app.cad.state()["airport_alerts"]["items"] == [] and push.sent == []
        # a person's own re-run of the same (fresh) clip is heard as ever
        before = row["rerun_at"]
        assert app.retranscribe([tid]) == 1
        assert wait(lambda: comms_row(app, tid)["rerun_at"] > before, 10)
        assert wait(lambda: app.cad.state()["airport_alerts"]["open"], 10) and tid in heard["atc"] and tid in heard["text"]
    finally:
        app.stop()


def test_the_atc_watch_itself_never_alerts_on_a_transcript_of_an_old_transmission():
    from sizeup import atc
    from tests.test_atc import T0, Clock, watch
    words = "Westchester tower, aircraft down off the end of runway one six"
    clock = Clock()
    cad, w, hear, push = watch(clock, [])
    clock.t = T0 + 5 * 3600
    old = w.heard({"id": "t-old", "source": "HPN Tower", "start": T0 - 2, "end": T0, "text": words})    # 5 h late
    assert old is None and push.sent == [] and cad.state()["airport_alerts"]["items"] == []
    edge = T0 + 5 * 3600 - atc.MAX_AGE_S + 1
    assert w.heard({"id": "t-new", "source": "HPN Tower", "start": edge - 2, "end": edge, "text": words}) is not None
    assert len(push.sent) == 1


def pd_event(tid, now, seq=1, **kw):
    import base64
    from sizeup.cad.dispatcher import wav_bytes
    from tests.test_app import voice
    return {"seq": seq, "at": now, "type": "traffic",
            "data": {"id": tid, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs", "tier": "alert",
                     "start": now - 3, "end": now, "text": "", "status": "skipped", "model": f"m.bin ({SHED_WHY})", **kw},
            "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}


def test_a_police_clip_caught_up_reaches_no_hook_on_the_hub(tmp_path):
    """The station's catch-up text for a police clip (its retext event says "catchup"): written, and neither on_text
    nor the intel hook (on_context_text) hears it. The same event without the mark does reach a hook."""
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app, now = hub.app, time.time()
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [pd_event("9211-1794-call_3", now)]})[0] == 200
        tid = "9211-1794-call_3"
        row = lambda: app.cad.store.one("SELECT status, text FROM radio_traffic WHERE id = ?", (tid,)) or {}  # noqa: E731
        assert wait(lambda: row().get("status") == "skipped", 10)
        heard = {"text": [], "context": []}
        app.cad.comms.on_text = heard["text"].append
        app.cad.comms.on_context_text = heard["context"].append             # the intel hook, stubbed
        seen = app.cad.comms.rev
        ev = {"seq": 2, "at": now, "type": "retext",
              "data": {"id": tid, "text": "Car 12 out with one at the plaza", "status": "done", "model": "m.bin", "catchup": True}}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: row().get("text") == "Car 12 out with one at the plaza", 10) and row()["status"] == "done"
        assert heard == {"text": [], "context": []} and app.cad.comms.rev > seen       # stored and noted, not heard
        ev = {"seq": 3, "at": now, "type": "retext", "data": {"id": tid, "text": "Car 12 clear", "status": "done", "model": "m.bin"}}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        assert wait(lambda: row().get("text") == "Car 12 clear", 10)
        assert heard == {"text": [], "context": [tid]}                                 # (a person's re-run: its own hook)
    finally:
        hub.close()


def test_a_catch_up_retext_of_a_context_row_calls_neither_hook(tmp_path):
    """Comms.retext routes another P25 system's row to on_context_text and ours to on_text; notify=False (a shed clip
    caught up) gates both: the text and the change note are written, neither hook hears it."""
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        record(app, "pd-1", now - 600, tg=9211, system="mrrs", tier="alert")       # another system's (context)
        record(app, "fm-1", now - 600)                                             # ours
        heard = {"text": [], "context": []}
        comms.on_text, comms.on_context_text = heard["text"].append, heard["context"].append
        rev = comms.rev
        assert comms.retext("pd-1", "Car 12 out with one at the plaza", "done", "m.bin", notify=False)
        assert comms.retext("fm-1", "Car 12 clear of the scene", "done", "m.bin", notify=False)
        assert heard == {"text": [], "context": []} and comms.rev == rev + 2
        got = {r["id"]: r["text"] for r in app.cad.store.q("SELECT id, text FROM radio_traffic")}
        assert got == {"pd-1": "Car 12 out with one at the plaza", "fm-1": "Car 12 clear of the scene"}
        # the same texts as any other re-run: each to its own hook
        assert comms.retext("pd-1", "Car 12 clear", "done", "m.bin") and comms.retext("fm-1", "Car 12 clear", "done", "m.bin")
        assert heard == {"text": ["fm-1"], "context": ["pd-1"]}
        # ... and through the app's quiet retext (the catch-up's path) neither
        app._retext("pd-1", "Car 12 back in service", "done", 0.9, "m.bin", quiet=True)
        app._retext("fm-1", "Car 12 back in service", "done", 0.9, "m.bin", quiet=True)
        assert heard == {"text": ["fm-1"], "context": ["pd-1"]}
    finally:
        app.stop()


def test_a_catch_up_retext_with_intel_tells_the_intel_hook_alone(tmp_path):
    """Comms.retext intel=True (a catch-up or a late row on a push source): another system's row to on_context_text,
    ours to on_intel_text, once each with its own id; on_text never; a late row (#747) too, beside on_late_text."""
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        assert comms.on_intel_text == app.cad.intel.consider                      # wired to intel alone
        record(app, "pd-1", now - 600, tg=9211, system="mrrs", tier="alert")
        record(app, "fm-1", now - 600)
        record(app, "pd-late", now - 600, tg=9211, system="mrrs", tier="alert", late=True)
        record(app, "fm-late", now - 600, late=True)
        heard = {"text": [], "context": [], "intel": [], "late": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text, comms.on_late_text = (
            heard["text"].append, heard["context"].append, heard["intel"].append, heard["late"].append)
        assert comms.retext("pd-1", "Car 12 out with one at the plaza", "done", "m.bin", notify=False, intel=True)
        assert comms.retext("fm-1", "Car 12 clear of the scene", "done", "m.bin", notify=False, intel=True)
        assert heard == {"text": [], "context": ["pd-1"], "intel": ["fm-1"], "late": []}
        assert comms.retext("pd-late", "Car 12 clear", "done", "m.bin", notify=False, intel=True, late=True)
        assert comms.retext("fm-late", "Car 12 clear", "done", "m.bin", intel=True, late=True)      # a late retext
        assert heard == {"text": [], "context": ["pd-1", "pd-late"], "intel": ["fm-1", "fm-late"], "late": ["fm-late"]}
        # a non-quiet, non-late retext: its usual hook alone (which tells intel itself), never twice
        assert comms.retext("pd-1", "Car 12 clear", "done", "m.bin", intel=True)
        assert heard["context"] == ["pd-1", "pd-late", "pd-1"] and heard["intel"] == ["fm-1", "fm-late"]
        lates = {r["id"]: r["late"] for r in app.cad.store.q("SELECT id, late FROM radio_traffic")}
        assert lates == {"pd-1": 0, "fm-1": 0, "pd-late": 1, "fm-late": 1}          # kept through the retexts
    finally:
        app.stop()


def test_a_catch_up_on_a_push_source_reaches_intel_and_nothing_else(tmp_path, monkeypatch):
    """App._retext quiet=True (every catch-up path): a row on a source intel pushes from (sizeup.intel.is_push_source)
    goes to the intel hook once, under its own id, its air time untouched; a row on any other source to no hook. Both
    stay quiet otherwise: no on_text (status hints, possible incoming, dispatch steps), no alarm or ATC pre-alert, and
    the retext event still says "catchup"."""
    from sizeup import intel
    from sizeup.app import App
    asked = []

    def push(key, kind=""):
        asked.append(key)
        return key in (PD, "Chan")

    monkeypatch.setattr(intel, "is_push_source", push)
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        record(app, "pd-1", now - 600, tg=9211, system="mrrs", tier="alert")      # a push source, another system's
        record(app, "fm-1", now - 500)                                             # a push source, ours ("Chan")
        record(app, "pd-txt", now - 400, tg=9570, system="mrrs", tier="text")     # not a push source
        starts = {r["id"]: r["start"] for r in app.cad.store.q("SELECT id, start FROM radio_traffic")}
        heard = {"text": [], "context": [], "intel": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text = (heard["text"].append, heard["context"].append,
                                                                     heard["intel"].append)
        loud, events = [], []
        monkeypatch.setattr(app, "_alarm", lambda *a, **k: loud.append(("alarm", a)))
        monkeypatch.setattr(app, "_atc", lambda *a, **k: loud.append(("atc", a)))
        monkeypatch.setattr(app, "_emit", lambda kind, data, clip=None: events.append((kind, data)))
        for tid in ("pd-1", "fm-1", "pd-txt"):
            app._retext(tid, "Car 12 back in service", "done", 0.9, "m.bin", why="catch-up", quiet=True)
        assert heard == {"text": [], "context": ["pd-1"], "intel": ["fm-1"]}
        assert set(asked) == {PD, "Chan", "p25:mrrs:9570"}
        assert loud == [] and [k for k, _ in events] == ["retext"] * 3 and all(d.get("catchup") for _, d in events)
        rows = {r["id"]: r for r in app.cad.store.q("SELECT id, start, text FROM radio_traffic")}
        assert {i: r["start"] for i, r in rows.items()} == starts                  # same ids, air times untouched
        assert all(r["text"] == "Car 12 back in service" for r in rows.values())
        # no words on a row that had some: intel too (to withdraw what it scored); on one that never had any: nothing
        heard["context"].clear()
        record(app, "pd-2", now - 300, tg=9211, system="mrrs", tier="alert")
        app._retext("pd-1", "", "no_speech", None, "m.bin", why="catch-up", quiet=True)
        app._retext("pd-2", "", "no_speech", None, "m.bin", why="catch-up", quiet=True)
        assert heard == {"text": [], "context": ["pd-1"], "intel": ["fm-1"]}
        heard["context"].clear()
        # the same rows re-run by a person (not quiet): their usual hooks, as before
        app._retext("pd-1", "Car 12 clear", "done", 0.9, "m.bin")
        app._retext("pd-txt", "Car 12 clear", "done", 0.9, "m.bin")
        assert heard["context"] == ["pd-1", "pd-txt"] and heard["intel"] == ["fm-1"]
    finally:
        app.stop()


def test_a_late_catch_up_on_a_push_source_reaches_intel_with_late_on_its_row(tmp_path, monkeypatch):
    """A #747 late row: its late column is 1 from Comms.record (0 for any other row) and stays through a re-record
    without the flag. Caught up quietly on a push source, it reaches the intel hook once, same id, same air time (intel
    scores it, never pushes it); on any other source, no hook at all."""
    from sizeup import intel
    from sizeup.app import App
    monkeypatch.setattr(intel, "is_push_source", lambda key, kind="": key == PD)
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        record(app, "pd-late", now - 900, tg=9211, system="mrrs", tier="alert", late=True)     # a push source
        record(app, "txt-late", now - 800, tg=9570, system="mrrs", tier="text", late=True)    # not one
        record(app, "pd-now", now - 700, tg=9211, system="mrrs", tier="alert")
        record(app, "pd-late", now - 900, tg=9211, system="mrrs", tier="alert")               # again, without the flag
        row = lambda tid: app.cad.store.one("SELECT start, late FROM radio_traffic WHERE id = ?", (tid,))  # noqa: E731
        assert (row("pd-late")["late"], row("txt-late")["late"], row("pd-now")["late"]) == (1, 1, 0)
        heard = {"text": [], "context": [], "intel": [], "late": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text, comms.on_late_text = (
            heard["text"].append, heard["context"].append, heard["intel"].append, heard["late"].append)
        loud = []
        monkeypatch.setattr(app, "_alarm", lambda *a, **k: loud.append("alarm"))
        monkeypatch.setattr(app, "_atc", lambda *a, **k: loud.append("atc"))
        for tid in ("pd-late", "txt-late"):
            app._retext(tid, "Car 12 out with one at the plaza", "done", 0.9, "m.bin", why="catch-up", quiet=True,
                        late=True)
        assert heard == {"text": [], "context": ["pd-late"], "intel": [], "late": []} and loud == []
        assert row("pd-late")["start"] == now - 900 and row("pd-late")["late"] == 1     # air time and the flag kept
    finally:
        app.stop()


def test_an_unclear_catch_up_on_a_scored_push_source_row_reaches_intel(tmp_path, monkeypatch):
    """A row intel scored, caught up again as unclear (sizeup/stt_quality.py: no text, the words kept apart): the intel
    hook hears it once, under its own id, so intel can withdraw what it scored; nothing else hears it."""
    from sizeup import intel, stt_quality
    from sizeup.app import App
    monkeypatch.setattr(intel, "is_push_source", lambda key, kind="": key == PD)
    monkeypatch.setattr(stt_quality, "assess", lambda *a, **k: "loop")
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        record(app, "pd-1", now - 600, tg=9211, system="mrrs", tier="alert", status="done", model="m.bin",
               text="Car 12 out with one at the plaza")
        heard = {"text": [], "context": [], "intel": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text = (heard["text"].append, heard["context"].append,
                                                                     heard["intel"].append)
        app._retext("pd-1", "thank you thank you thank you", "done", 0.9, "m.bin", why="catch-up", quiet=True)
        r = app.cad.store.one("SELECT text, status FROM radio_traffic WHERE id = 'pd-1'")
        assert (r["text"], r["status"]) == ("", stt_quality.UNCLEAR)
        assert heard == {"text": [], "context": ["pd-1"], "intel": []}
    finally:
        app.stop()


@pytest.mark.parametrize("chan, want_key, want_kind", [
    ({"tg": 307, "system": "westchester"}, "tg:307", ""),     # the county's talkgroup: its tg:<n> key, no kind
    ({"source": "HPN Tower"}, "HPN Tower", "airband"),       # an airband channel: its label and kind
    ({"source": "FG1"}, "FG1", "traffic"),                   # an analog channel of ours
])
def test_a_catch_up_asks_is_push_source_with_the_channels_key_and_kind(tmp_path, monkeypatch, chan, want_key,
                                                                       want_kind):
    from sizeup import intel
    from sizeup.app import App
    from sizeup.config import ChannelCfg
    from tests.test_cloud import CHANNELS
    asked = []

    def push(key, kind=""):
        asked.append((key, kind))
        assert (key, kind) == (want_key, want_kind)
        return True

    monkeypatch.setattr(intel, "is_push_source", push)
    app = App(base(clips_dir=str(tmp_path / "clips"), channels=CHANNELS + (ChannelCfg("HPN Tower", 118.575, "airband"),)))
    try:
        comms, now = app.cad.comms, time.time()
        source = chan.get("source", "")
        comms.record({"id": "row-1", "start": now - 600, "end": now - 598, "source": source,
                      "talkgroup": chan.get("tg"), "system": chan.get("system", ""), "unit": "", "text": "",
                      "status": "skipped", "model": f"m.bin ({SHED_WHY})",
                      **({"mhz": 118.575} if want_kind == "airband" else {})})
        heard = {"text": [], "context": [], "intel": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text = (heard["text"].append, heard["context"].append,
                                                                     heard["intel"].append)
        monkeypatch.setattr(app, "_atc", lambda *a, **k: None)
        app._retext("row-1", "Engine 60 responding", "done", 0.9, "m.bin", why="catch-up", quiet=True)
        assert asked == [(want_key, want_kind)]
        assert heard == {"text": [], "context": [], "intel": ["row-1"]}     # ours (the county's, airband, analog)
    finally:
        app.stop()


def test_a_late_row_recorded_transcribed_on_a_push_source_reaches_intel_and_the_late_hook(tmp_path, monkeypatch):
    """A #747 late row that reaches Comms.record with its words: on a push source, the intel hook (ours on_intel_text,
    another system's on_context_text) once, same id, its air start and late = 1, beside on_late_text for ours; on any
    other source, on_late_text only; empty or unclear words reach no intel hook. The check runs off store.lock."""
    from sizeup import intel
    from sizeup.app import App
    locked = []

    def push(key, kind=""):
        locked.append(owned())                                  # held by this thread (locked(): by any thread)
        return key in (PD, "Chan")

    monkeypatch.setattr(intel, "is_push_source", push)
    if not hasattr(threading.RLock(), "_is_owned"):
        pytest.skip("this Python's RLock can't say whether the current thread holds it")
    app = App(base(clips_dir=str(tmp_path / "clips")))
    lock = app.cad.store.lock
    owned = lock._is_owned
    try:
        comms, now = app.cad.comms, time.time()
        assert comms.intel_pushes == app._intel_source
        with lock:
            assert owned()                                      # (the check itself works)
        heard = {"text": [], "context": [], "intel": [], "late": []}
        comms.on_text, comms.on_context_text, comms.on_intel_text, comms.on_late_text = (
            heard["text"].append, heard["context"].append, heard["intel"].append, heard["late"].append)
        words = {"status": "done", "model": "m.bin", "text": "Car 12 out with one at the plaza", "late": True}
        record(app, "fm-late", now - 900, **words)                                            # ours, a push source
        record(app, "pd-late", now - 880, tg=9211, system="mrrs", tier="alert", **words)    # another system's, one
        assert heard == {"text": [], "context": ["pd-late"], "intel": ["fm-late"], "late": ["fm-late"]}
        rows = {r["id"]: r for r in app.cad.store.q("SELECT id, start, late FROM radio_traffic")}
        assert (rows["fm-late"]["start"], rows["fm-late"]["late"]) == (now - 900, 1)
        assert (rows["pd-late"]["start"], rows["pd-late"]["late"]) == (now - 880, 1)
        # not a push source: on_late_text only (another system's late row: nothing at all, as before)
        record(app, "txt-late", now - 860, tg=9570, system="mrrs", tier="text", **words)
        record(app, "county-late", now - 850, tg=307, system="westchester", **words)
        assert heard == {"text": [], "context": ["pd-late"], "intel": ["fm-late"], "late": ["fm-late", "county-late"]}
        # no words, or unclear ones: no intel hook
        record(app, "fm-empty", now - 840, status="no_speech", model="m.bin", late=True)
        record(app, "fm-unclear", now - 830, status="unclear", model="m.bin", late=True, quality="loop",
               unclear_text="thank you thank you")
        record(app, "pd-empty", now - 820, tg=9211, system="mrrs", tier="alert", status="unclear", model="m.bin",
               late=True, quality="loop", unclear_text="thank you thank you")
        assert heard["intel"] == ["fm-late"] and heard["context"] == ["pd-late"] and heard["text"] == []
        assert heard["late"] == ["fm-late", "county-late", "fm-empty", "fm-unclear"]
        assert locked and not any(locked)                                             # never under store.lock
    finally:
        app.stop()


def test_a_context_row_re_recorded_without_its_words_reaches_intel_to_withdraw(tmp_path):
    """Another system's row recorded again (replace=True) with no text, or unclear: on_context_text if it had words,
    so intel withdraws what it scored; a row that never had words (a late one too) hears nothing."""
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        heard = {"text": [], "context": [], "late": []}
        comms.on_text, comms.on_context_text, comms.on_late_text = (heard["text"].append, heard["context"].append,
                                                                    heard["late"].append)
        pd = {"tg": 9211, "system": "mrrs", "tier": "alert", "model": "m.bin"}
        record(app, "pd-1", now - 900, status="done", text="Car 12 out with one at the plaza", **pd)
        record(app, "pd-2", now - 880, status="done", text="Car 14 on scene", **pd)
        assert heard["context"] == ["pd-1", "pd-2"]
        record(app, "pd-1", now - 900, status="no_speech", **pd)                              # its words gone
        record(app, "pd-2", now - 880, status="unclear", quality="loop", unclear_text="thank you thank you", **pd)
        assert heard["context"] == ["pd-1", "pd-2", "pd-1", "pd-2"]
        # never had words: nothing, late or not, again or not
        record(app, "pd-3", now - 860, status="no_speech", **pd)
        record(app, "pd-4", now - 850, status="skipped", late=True, **pd)
        record(app, "pd-4", now - 850, status="no_speech", late=True, **pd)
        record(app, "pd-1", now - 900, status="no_speech", **pd)                              # (already empty)
        assert heard == {"text": [], "context": ["pd-1", "pd-2", "pd-1", "pd-2"], "late": []}
    finally:
        app.stop()


def test_our_late_row_re_recorded_without_its_words_reaches_intel_to_withdraw(tmp_path, monkeypatch):
    """One of our late rows (#747) recorded with words, then again with none (no_speech, unclear): on a push source,
    on_intel_text each time beside on_late_text, so intel withdraws what it scored; on any other source, or a row that
    never had words, on_late_text only."""
    from sizeup import intel
    from sizeup.app import App
    monkeypatch.setattr(intel, "is_push_source", lambda key, kind="": key == "Chan")
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        heard = {"text": [], "intel": [], "late": []}
        comms.on_text, comms.on_intel_text, comms.on_late_text = (heard["text"].append, heard["intel"].append,
                                                                  heard["late"].append)
        words = {"status": "done", "model": "m.bin", "text": "Engine 60 responding", "late": True}
        record(app, "fm-1", now - 900, **words)
        record(app, "fm-2", now - 880, **words)
        assert heard["intel"] == ["fm-1", "fm-2"]
        record(app, "fm-1", now - 900, status="no_speech", model="m.bin", late=True)          # its words gone
        record(app, "fm-2", now - 880, status="unclear", model="m.bin", late=True, quality="loop",
               unclear_text="thank you thank you")
        assert heard["intel"] == ["fm-1", "fm-2", "fm-1", "fm-2"]
        # never had words: on_late_text only
        record(app, "fm-3", now - 860, status="no_speech", model="m.bin", late=True)
        record(app, "fm-3", now - 860, status="no_speech", model="m.bin", late=True)
        assert heard["intel"] == ["fm-1", "fm-2", "fm-1", "fm-2"] and heard["text"] == []
        assert heard["late"] == ["fm-1", "fm-2", "fm-1", "fm-2", "fm-3", "fm-3"]
        rows = {r["id"]: r for r in app.cad.store.q("SELECT id, start, late FROM radio_traffic")}
        assert rows["fm-1"]["start"] == now - 900 and all(r["late"] == 1 for r in rows.values())
    finally:
        app.stop()


def test_our_late_row_on_another_source_losing_its_words_reaches_only_the_late_hook(tmp_path, monkeypatch):
    from sizeup import intel
    from sizeup.app import App
    monkeypatch.setattr(intel, "is_push_source", lambda key, kind="": False)
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        comms, now = app.cad.comms, time.time()
        heard = {"intel": [], "late": []}
        comms.on_intel_text, comms.on_late_text = heard["intel"].append, heard["late"].append
        record(app, "fm-1", now - 900, status="done", model="m.bin", text="Engine 60 responding", late=True)
        record(app, "fm-1", now - 900, status="no_speech", model="m.bin", late=True)
        assert heard == {"intel": [], "late": ["fm-1", "fm-1"]}
    finally:
        app.stop()


def test_the_late_column_is_added_to_an_old_database(tmp_path):
    from tests.test_cad import make_cad
    cad, now = make_cad(tmp_path), time.time()
    cad.comms.record({"id": "9211-1-call_1", "start": now - 60, "end": now - 58, "source": "", "talkgroup": 9211,
                      "system": "mrrs", "text": "", "status": "done", "model": "m.bin"})
    cad.shutdown()
    import sqlite3
    db = sqlite3.connect(tmp_path / "cad.sqlite")           # the database as the last release left it
    db.execute("ALTER TABLE radio_traffic DROP COLUMN late")
    db.commit()
    db.close()
    cad = make_cad(tmp_path)
    try:
        assert [(r["id"], r["late"]) for r in cad.store.q("SELECT id, late FROM radio_traffic")] == [("9211-1-call_1", 0)]
        cad.comms.record({"id": "9211-2-call_2", "start": now - 30, "end": now - 28, "source": "", "talkgroup": 9211,
                          "system": "mrrs", "text": "", "status": "done", "model": "m.bin", "late": True})
        assert cad.store.one("SELECT late FROM radio_traffic WHERE id = '9211-2-call_2'")["late"] == 1
    finally:
        cad.shutdown()


# -- fire first: what pauses it ----------------------------------------------------------------------------------------


def test_an_open_event_an_alarm_a_window_a_carrier_or_a_talker_pauses_it(tmp_path, monkeypatch):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        assert app._catchup_pause() == ""
        iid = app.cad.create({"type": "ALARM", "subtype": "CO-RESD"})["id"]
        assert app._catchup_pause() == "an event is open"
        app.cad.store.db.execute("UPDATE incidents SET status = 'closed' WHERE id = ?", (iid,))
        assert app._catchup_pause() == ""
        app.cad.store.db.execute("UPDATE incidents SET status = 'queued', created_at = ? WHERE id = ?", (time.time() - 60, iid))
        assert app._catchup_pause() == "an event is open"                      # a page just queued
        app.cad.store.db.execute("UPDATE incidents SET created_at = ? WHERE id = ?", (time.time() - 7200, iid))
        assert app._catchup_pause() == ""                                      # (one nobody took for hours: not for ever)
        app.cad.alarms.windows.open(307, "Fire/EMS 15", ["7001"], ours=True)
        assert app._catchup_pause() == "an emergency window is open"
        app.cad.alarms.windows._tg.clear()
        app.cad.alarms.on_traffic({"id": "FG1-1", "start": time.time() - 2, "end": time.time(), "source": "FG1",
                                   "talkgroup": None, "unit": "", "rig": "", "radio_id": "", "text": "mayday mayday mayday"})
        assert app._catchup_pause() == "an alarm is open"
        app.cad.store.db.execute("UPDATE alarms SET ack_at = ?", (time.time(),))
        assert app._catchup_pause() == ""
        monkeypatch.setattr(app, "radio_status", lambda: [{"label": "FG1", "open": True}, {"label": "South 2", "open": False}])
        assert app._catchup_pause() == "carrier open on FG1"
        monkeypatch.setattr(app, "radio_status", lambda: [])
        monkeypatch.setattr(app.live, "state", lambda: [{"talkgroup": 307}])
        assert app._catchup_pause() == "someone is talking"
        # and the driver sends nothing meanwhile
        record(app, "c1", time.time() - 60)
        app.whisper.state = "ok"
        assert app.catchup.tick() == 0 and app.catchup.stats()["why"] == "paused: someone is talking"
        assert app.catchup.stats()["waiting"] == 1
    finally:
        app.stop()


def test_a_fire_clip_that_goes_to_aws_restarts_the_quiet_spell(tmp_path):
    from tests.fake_transcribe import FakeTranscribe
    from sizeup.app import App
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    app = App(base(clips_dir=str(tmp_path / "clips"), **aws_cfg(fake)))
    try:
        q = app.stt._q
        q._ours_at -= 1000
        assert app.channel_pref("FG1")["engine"] == "aws" and q.stats()["ours_ago_s"] >= 1000
        fm_segment(app)                                        # FG1 on AWS: never in the whisper queue
        assert q.qsize() == 0 and q.stats()["ours_ago_s"] < 5
    finally:
        app.stop()
        fake.close()


def test_a_forgotten_event_or_alarm_stops_pausing_it_after_12_hours(tmp_path):
    """An event nobody closed, an alarm nobody acknowledged: only while created or updated in the last 12 h, so the
    catch-up never starves for ever."""
    from sizeup.app import CATCHUP_PAUSE_MAX_S, App
    assert CATCHUP_PAUSE_MAX_S == 12 * 3600
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        db, now = app.cad.store.db, time.time()
        old, recent = now - CATCHUP_PAUSE_MAX_S - 60, now - CATCHUP_PAUSE_MAX_S + 60
        iid = app.cad.create({"type": "ALARM", "subtype": "CO-RESD"})["id"]
        assert app._catchup_pause() == "an event is open"
        db.execute("UPDATE incidents SET created_at = ?, accepted_at = ? WHERE id = ?", (old, old, iid))
        db.execute("UPDATE events SET at = ? WHERE incident_id = ?", (old, iid))
        assert app._catchup_pause() == ""                                      # open, untouched for 12 h: no pause
        db.execute("UPDATE incidents SET status = 'active' WHERE id = ?", (iid,))
        assert app._catchup_pause() == ""
        db.execute("UPDATE incidents SET accepted_at = ? WHERE id = ?", (recent, iid))
        assert app._catchup_pause() == "an event is open"                      # taken within 12 h
        db.execute("UPDATE incidents SET accepted_at = ? WHERE id = ?", (old, iid))
        app.cad.note(iid, {"text": "still working", "operator": "JD"})         # anything logged on it: updated
        assert app._catchup_pause() == "an event is open"
        db.execute("UPDATE incidents SET status = 'closed' WHERE id = ?", (iid,))
        app.cad.alarms.on_traffic({"id": "FG1-1", "start": now - 2, "end": now, "source": "FG1", "talkgroup": None,
                                   "unit": "", "rig": "", "radio_id": "", "text": "mayday mayday mayday"})
        assert app._catchup_pause() == "an alarm is open"
        db.execute("UPDATE alarms SET created_at = ?, updated_at = ?", (old, old))
        assert app._catchup_pause() == ""                                      # never acknowledged, 12 h old: no pause
        db.execute("UPDATE alarms SET updated_at = ?", (recent,))
        assert app._catchup_pause() == "an alarm is open"
    finally:
        app.stop()


# -- a person's Re-transcribe, the station's update --------------------------------------------------------------------


def test_a_manual_retranscribe_promotes_a_clip_the_catch_up_holds(tmp_path, monkeypatch):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    w = app.stt.client = Whisper({3: "Car 12 clear of the scene"})
    try:
        tid = shed_one(app, monkeypatch)
        app.whisper.state = "ok"
        assert app.catchup.tick() == 1 and app._catchup_ids == {tid}
        q = app.stt._q
        assert q.stats()["catchup"] == 1 and q.stats()["rerun"] == 0
        assert app.retranscribe([tid]) == 1                    # taken, not refused as "already running"
        assert q.stats()["catchup"] == 0 and q.stats()["rerun"] == 1 and q.qsize() == 1
        assert not app._catchup_ids and app._rerun_ids == {tid} and app.rerun["total"] == 1
        app.stt.start()                                        # (a re-run: no quiet spell to wait for)
        assert wait(lambda: (comms_row(app, tid) or {}).get("rerun_result"), 10)
        assert comms_row(app, tid)["rerun_result"] == "changed" and w.calls == [("full", 3)]
        assert app.rerun["done"] == 1
    finally:
        app.stop()


def test_the_catch_up_never_holds_up_a_station_update_and_yields_to_one(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sizeup.app import App
    from sizeup.cloud.stationmode import StationMode
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        tid = shed_one(app, monkeypatch)
        app.whisper.state = "ok"
        assert app.catchup.tick() == 1 and app.stt._q.stats()["catchup"] == 1
        busy = StationMode.busy.__get__(SimpleNamespace(app=app))
        assert busy() == ""                                    # queued for the catch-up: a quiet moment all the same
        app._rerun_ids.add("someone-asked")
        assert busy() == "1 re-run(s) pending"
        app._rerun_ids.discard("someone-asked")
        # an update waiting: the catch-up takes nothing, and takes back what it had queued (no try counted)
        app.station = SimpleNamespace(forward=False, updates=SimpleNamespace(status=lambda: {"update": {"state": "waiting"}}))
        got = app.catch_up([{"id": tid, "key": "FG1", "source": "FG1"}])
        assert got == {"queued": [], "refused": {}, "hold": "a station update is waiting"}
        assert app.stt._q.qsize() == 0 and not app._rerun_ids and not app._catchup_ids
        assert comms_row(app, tid)["status"] == "skipped" and not comms_row(app, tid).get("rerun_result")
    finally:
        app.station = None
        app.stop()


def test_a_long_clip_or_one_with_no_recording_is_refused_by_the_station(tmp_path, monkeypatch):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        tid = shed_one(app, monkeypatch)
        monkeypatch.setattr("sizeup.app.CATCHUP_MAX_CLIP_S", 2.0)
        got = app.catch_up([{"id": tid, "key": "FG1", "source": "FG1"}, {"id": "FG1-404", "key": "FG1", "source": "FG1"}])
        assert got == {"queued": [], "refused": {tid: "longer than 2 s", "FG1-404": "no recording on the station"}, "hold": ""}
        assert app.stt._q.qsize() == 0 and not app._rerun_ids
    finally:
        app.stop()


def test_a_restarted_station_catches_up_from_the_hubs_spec_and_the_clip_on_disk(tmp_path, monkeypatch):
    """The station restarted since it shed the clip: nothing of it in memory, its audio on disk. The hub's spec is
    all it has; the text goes up as a quiet retext."""
    from sizeup.app import App
    cfg = base(clips_dir=str(tmp_path / "clips"))
    first = App(cfg)
    try:
        tid = shed_one(first, monkeypatch)
        start = first._find(tid)[0].start
    finally:
        first.stop()
    app = App(cfg)                                              # the restart
    out = []
    try:
        app.store.loose.clear()
        app.store.incidents.clear()
        assert app._find(tid) == (None, "") and (tmp_path / "clips" / f"{tid}.src.wav").is_file()
        app.events = lambda kind, d, clip=None: out.append((kind, d))
        w = app.stt.client = Whisper({3: "Car 12 clear of the scene"})
        prompts = []
        app.stt.prompt = lambda item: prompts.append((item.source_key, item.unit, item.start)) or ""
        ready(app, monkeypatch)
        got = app.catch_up([{"id": tid, "key": "FG1", "source": "FG1", "unit": "Car 12", "start": start, "service": "fire"}])
        assert got == {"queued": [tid], "refused": {}, "hold": ""}
        assert wait(lambda: [k for k, _ in out] == ["retext", "rerun"], 10), out
        assert out[0][1] == {"id": tid, "text": "Car 12 clear of the scene", "status": "done", "confidence": 0.9,
                             "model": "m.bin", "catchup": True}
        assert out[1][1]["result"] == "changed; catch-up" and w.calls == [("fast", 3)]
        assert prompts == [("FG1", "Car 12", start)]            # the spec's fields, as the live pass had them
    finally:
        app.stop()


# -- the hub: police on its own AWS budget, everything else on the station's whisper -------------------------------


def pd_alert(app):
    """The invented police dispatch talkgroup in the other system's file, alert tier (transcribed, its alerts first)."""
    app.tg.info[("mrrs", 9211)] = {"name": "PD Dispatch", "tier": "alert"}


def test_the_hub_routes_police_to_its_own_aws_budget_and_never_ours(tmp_path):
    from sizeup.transcribe import PRICE_PER_MIN
    from tests.fake_transcribe import FakeTranscribe
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        app = hub.app
        pd_alert(app)
        assert app.channel_pref("FG1")["engine"] == "aws"           # ours on AWS: still never caught up on AWS
        assert app._catchup_route(PD) == "aws" and app._catchup_route("FG1") == "whisper"
        assert app._catchup_route("tg:383") == "whisper"
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_context_budget_usd",
                                                                 "value": 0}, token=None)[0] == 200
        assert app._catchup_route(PD) == "whisper"                  # its own budget used up: whisper, not ours
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_context_budget_usd",
                                                                 "value": 15}, token=None)[0] == 200
        assert call(hub.url + "/api/cad/settings/stt", "POST", {"action": "set", "key": "aws_monthly_limit_usd",
                                                                 "value": 0}, token=None)[0] == 200
        assert app._catchup_route(PD) == "aws" and app._catchup_route("FG1") == "whisper"   # ours used up: police unaffected
        # at 80 % of the police month the catch-up leaves the rest to what is heard live
        app.aws.usage.add(11.9 / PRICE_PER_MIN * 60, 0, PD)
        assert app._catchup_route(PD) == "aws"
        app.aws.usage.add(0.2 / PRICE_PER_MIN * 60, 0, PD)
        assert app.aws.available(PD)[0] and app._catchup_route(PD) == "whisper"
    finally:
        hub.close()
        fake.close()


def test_a_catch_up_clip_failing_on_aws_never_opens_the_breaker_fire_shares():
    from sizeup.app import AwsJob
    from sizeup.transcribe import Job
    from tests.test_police_stt import svc
    t, _ = svc()
    failed = []
    t.on_fail = lambda item, why: failed.append(item.id)
    for i in range(20):
        job = Job(PD, AwsJob("traffic", f"c{i}", PD, rerun=True, catchup=True), np.zeros(8000, np.int16), submitted=t.clock())
        t._open.add(job)
        assert t._timeout(job, "AWS timed out: no text after 60 s")
    assert len(failed) == 20 and t.streak == 0 and t.failing_since is None and t.breaker["state"] == "closed"
    assert t.health()["state"] == "ok"
    job = Job(PD, AwsJob("traffic", "live", PD), np.zeros(8000, np.int16), submitted=t.clock())
    t._open.add(job)
    t._timeout(job, "AWS timed out: no text after 60 s")
    assert t.streak == 1                                         # (a live clip's does count)


def test_the_hub_catches_up_a_police_clip_on_aws_within_its_own_budget(tmp_path):
    from sizeup.transcribe import CONTEXT as POOL
    from tests.fake_transcribe import FakeTranscribe
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        now = time.time()
        tid = "9211-1794-call_7"
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [pd_event(tid, now)]})[0] == 200
        app = hub.app
        pd_alert(app)
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)[0]] == [tid], 10)
        heard = []
        app.cad.comms.on_text = heard.append
        app.cad.comms.on_context_text = heard.append
        ours = app.aws.spent()["billed_s"]
        assert app.catchup.tick() == 1
        assert wait(lambda: (app.cad.comms.store.one("SELECT rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {})
                    .get("rerun_result"), 20)
        r = app.cad.comms.store.one("SELECT status, text, model, rerun_result FROM radio_traffic WHERE id = ?", (tid,))
        assert r["status"] == "done"
        assert r["text"] == AWS_TEXT + "." and r["model"] == "aws-transcribe" and r["rerun_result"] == "changed; catch-up"
        assert app.aws.spent(POOL)["billed_s"] > 0 and app.aws.spent()["billed_s"] == ours      # never our pool
        assert heard == []                                                                      # and nobody alerted
        app.catchup._caught = (0.0, None)
        assert app.catchup.tick() == 0 and app.catchup.stats()["caught_24h"] == 1
        assert app.catchup.stats()["waiting"] == 0
        assert app.health.model()["stt_catchup"]["caught_24h"] == 1
    finally:
        hub.close()
        fake.close()


def test_a_police_clip_failing_on_aws_stays_skipped_with_the_try_on_its_row(tmp_path):
    from sizeup.app import AwsJob
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now, tid, app = time.time(), "9211-1794-call_8", hub.app
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [pd_event(tid, now)]})[0] == 200
        row = lambda: app.cad.store.one("SELECT status, rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {}  # noqa: E731
        assert wait(lambda: row().get("status") == "skipped", 10)
        asked = []
        app.hub.remote = lambda *a, **k: asked.append(a)
        app._rerun_note[tid] = "catch-up"
        app._aws_failed(AwsJob("traffic", tid, PD, rerun=True, catchup=True), "AWS timed out: no text after 60 s")
        assert row() == {"status": "skipped", "rerun_result": "failed: AWS: AWS timed out: no text after 60 s; catch-up try 1"}
        assert asked == []                                       # (not handed to the station as a person's re-run)
    finally:
        hub.close()


def test_the_hub_has_the_stations_whisper_catch_up_once_it_is_idle(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        w = Whisper({3: "Car 12 clear of the scene"})
        tid = shed_one(st, monkeypatch)                           # (the station's whisper not running yet)
        app = hub.app
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(time.time() - 3600)[0]] == [tid], 15)
        st.stt.client = w
        monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
        st.whisper.state = "ok"
        # (once the station's report says its whisper is idle: a tick or a few)
        assert wait(lambda: (app.catchup.tick(), app.catchup.stats()["sent"])[1] == 1, 20), app.catchup.stats()
        assert wait(lambda: (app.cad.comms.store.one("SELECT rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {})
                    .get("rerun_result"), 20)
        r = app.cad.comms.store.one("SELECT status, text, model, rerun_result FROM radio_traffic WHERE id = ?", (tid,))
        assert r["status"] == "done"
        assert r["text"] == "Car 12 clear of the scene" and r["model"] == "m.bin" and r["rerun_result"] == "changed; catch-up"
        assert w.calls == [("fast", 3)]
        app.catchup._caught = (0.0, None)
        assert app.catchup.tick() == 0 and app.catchup.stats()["caught_24h"] == 1
        assert app.catchup.stats()["waiting"] == 0
        assert app.health.model()["stt_catchup"]["caught_24h"] == 1
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_an_older_station_refusing_the_call_burns_no_tries_and_is_left_alone_for_minutes(tmp_path, monkeypatch):
    from sizeup.cloud import station as station_mod
    monkeypatch.setattr(station_mod, "CALLS", tuple(c for c in station_mod.CALLS if c != "catch_up"))   # an older build
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        st.whisper.state = "ok"
        tid = shed_one(st, monkeypatch)
        app = hub.app
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(time.time() - 3600)[0]] == [tid], 15)
        assert wait(lambda: app._catchup_whisper_ready(0.0)[0], 15)
        calls = []
        real = app.hub.remote
        app.hub.remote = lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1]
        for _ in range(3):
            assert app.catchup.tick() == 0
        assert calls == ["catch_up"]                              # asked once, then left alone
        assert app._catchup_refused_until > time.time() + 300
        assert app.catchup._sent == {} and app.catchup.stats()["waiting"] == 1 and app.catchup.stats()["gave_up"] == 0
        r = app.cad.comms.store.one("SELECT status, rerun_result FROM radio_traffic WHERE id = ?", (tid,))
        assert r == {"status": "skipped", "rerun_result": ""}     # no try on its row
        # a network error (the station gone): the same, and no back-off of minutes
        app._catchup_refused_until = 0.0
        app.hub.remote = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the station didn't answer in 12 s"))
        assert app._catchup_whisper([{"id": tid}]) is None and app._catchup_refused_until == 0.0
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_the_hub_sends_nothing_while_the_stations_whisper_has_fire_work(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        app.hub.last_seen = time.time()
        app.hub.status = {"health": {"whisper": {"state": "ok"},
                                     "stt_queue": {"queued": 1, "urgent": 1, "live": 1, "high": 0, "context": 0}}}
        app.hub.status_at = time.time()
        assert app._catchup_whisper_ready(0.0) == (False, "live clips waiting")
        app.hub.status["health"]["stt_queue"] = {"queued": 0, "urgent": 0, "live": 0, "high": 0, "context": 0}
        assert app._catchup_whisper_ready(0.0) == (True, "")
        assert app._catchup_whisper_ready(time.time() + 1)[0] is False        # a report from before the last batch
        app.hub.status["health"]["whisper"]["state"] = "down"
        assert app._catchup_whisper_ready(0.0) == (False, "whisper not ready")
        # the station's radios, as it reports them: a carrier open there pauses it here
        app.hub.status = {"radios": [{"label": "FG1", "open": True}]}
        assert app._catchup_pause() == "carrier open on FG1"
    finally:
        hub.close()
