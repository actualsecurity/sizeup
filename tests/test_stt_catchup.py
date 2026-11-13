"""Shed clips caught up (sizeup/sttcatchup.py): a clip speech-to-text skipped while it was busy ("Skipped: busy") is
run again once whisper, or its AWS channel, is idle; the alert tier first, then the newest; never older than the
max age; AWS only on a context channel's own budget, never ours; fire traffic never waits behind it. Synthetic clips,
local fakes only."""

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
        self.texts, self.hold, self.calls = texts, hold, []

    def transcribe(self, audio, prompt="", fast=False):
        self.calls.append(("fast" if fast else "full", round(len(audio) / 16000)))
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
    assert [q.get_nowait()[2] for _ in range(7)] == ["fe15", "tower", "live", "rerun", "alert", "context", "catchup"]
    st = q.stats()
    assert st["catchup"] == 0 and st["queued"] == 0


def test_fire_traffic_coming_in_holds_the_catch_up_until_it_is_quiet():
    clock = {"t": 1000.0}
    q = q_at(clock)
    q.put((CATCHUP, None, "catchup", None, None))
    assert q.get_nowait()[2] == "catchup"                    # nothing of ours lately: it goes at once
    q.put((CATCHUP, None, "catchup-2", None, None))
    q.put((URGENT, None, "fe15", None, "tg:307"))            # fire traffic arrives with a catch-up clip waiting
    assert q.get_nowait()[2] == "fe15"                       # fire first, always
    with pytest.raises(stt_mod.queue.Empty):                 # and the catch-up waits for a quiet spell after it
        q.get_nowait()
    clock["t"] += stt_mod.CATCHUP_QUIET_S - 1
    with pytest.raises(stt_mod.queue.Empty):
        q.get_nowait()
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


def test_fire_is_never_delayed_by_the_catch_up_on_a_running_whisper(monkeypatch):
    """Whisper running: catch-up clips queued, then a fire clip: the fire clip is transcribed next, with full decoding;
    the catch-up clips only after a quiet spell, each one greedy pass."""
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.5)
    done: list[tuple[str, float]] = []
    gate = threading.Event()
    w = Whisper({1: "catch", 2: "fire"}, hold=gate)
    t = Transcriber(w, lambda item: done.append((item.id, time.monotonic())))

    class Item:
        def __init__(self, cid):
            self.id = cid

    t.submit(Item("first-catchup"), np.zeros(16000, np.float32), priority=CATCHUP)
    t.start()
    assert wait(lambda: len(w.calls) == 1, 3)               # whisper busy on a catch-up clip (nothing else came)
    for i in range(3):
        t.submit(Item(f"catchup-{i}"), np.zeros(16000, np.float32), priority=CATCHUP)
    t.submit(Item("fire"), np.zeros(32000, np.float32), priority=URGENT, channel="tg:307")
    gate.set()
    assert wait(lambda: len(done) == 5, 10)
    order = [d[0] for d in done]
    assert order[:2] == ["first-catchup", "fire"]           # the fire clip next: never behind a waiting catch-up
    assert w.calls[1] == ("full", 2)                        # with full decoding
    assert all(c == ("fast", 1) for i, c in enumerate(w.calls) if i != 1)
    assert done[2][1] - done[1][1] >= 0.4                    # the catch-up waited for the quiet spell


# -- the driver --------------------------------------------------------------------------------------------------


def rig(rows, **kw):
    """A CatchUp over fake hooks: what was sent where."""
    sent = {"whisper": [], "aws": []}
    st = {"on": True, "age": 6.0, "may": True, "route": {}, "ready": (True, ""), "aws_idle": True, "queued": None}

    def to_whisper(specs):
        sent["whisper"].append([s["id"] for s in specs])
        return [s["id"] for s in specs] if st["queued"] is None else st["queued"]

    def to_aws(row):
        sent["aws"].append(row["id"])
        return True

    def rows_fn(since, limit):
        return [r for r in rows if r["start"] >= since][:limit]

    cu = CatchUp(settings=lambda: (st["on"], st["age"]), may_run=lambda: st["may"], rows=rows_fn,
                 count=lambda since: len(rows_fn(since, 999)), caught=lambda since: 7,
                 route=lambda key: st["route"].get(key, "whisper"), whisper_ready=lambda after: st["ready"],
                 aws_idle=lambda key: st["aws_idle"], to_whisper=to_whisper, to_aws=to_aws, log=lambda m: None,
                 **kw)
    return cu, sent, st


def rows_at(now, n, key="tg:383"):
    return [{"id": f"c{i}", "key": key, "source": "x", "unit": "", "start": now - 60 * (i + 1), "service": "", "tier": ""}
            for i in range(n)]


def test_small_batches_only_when_whisper_is_idle_and_never_twice_too_soon():
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 8))
    st["ready"] = (False, "fire clips waiting")
    assert cu.tick(now) == 0 and sent["whisper"] == []
    assert "fire clips waiting" in cu.stats()["why"] and cu.stats()["waiting"] == 8 and cu.stats()["caught_24h"] == 7
    st["ready"] = (True, "")
    assert cu.tick(now) == sttcatchup.WHISPER_BATCH and sent["whisper"] == [["c0", "c1", "c2"]]
    assert cu.tick(now + 15) == 3 and sent["whisper"][-1] == ["c3", "c4", "c5"]      # (rows still skipped: the rest)
    # sent ones still skipped are tried again only after RETRY_S, at most MAX_TRIES times
    cu.tick(now + 30)
    assert sent["whisper"][-1] == ["c6", "c7"]
    assert cu.tick(now + 45) == 0
    assert cu.tick(now + 30 + sttcatchup.RETRY_S) == 3 and sent["whisper"][-1] == ["c0", "c1", "c2"]
    for k in range(4):
        cu.tick(now + 60 + sttcatchup.RETRY_S + 15 * k)
    later = now + 3 * sttcatchup.RETRY_S
    assert cu.tick(later) == 0                                # tried twice each: left skipped


def test_off_too_old_or_not_the_cad_of_record_sends_nothing():
    now = 100000.0
    cu, sent, st = rig(rows_at(now, 3) + [{"id": "old", "key": "tg:383", "source": "x", "unit": "", "start": now - 7 * 3600,
                                            "service": "", "tier": ""}])
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


def test_whisper_idle_wants_nothing_of_ours_nor_an_alert_clip_waiting():
    ok = {"queued": 2, "urgent": 0, "high": 0, "live": 0, "context": 0, "low": 2, "rerun": 0, "catchup": 0}
    assert whisper_idle(ok) == (True, "")
    for k in ("urgent", "high", "live", "context", "catchup"):
        assert whisper_idle(dict(ok, **{k: 1}))[0] is False
    assert whisper_idle(dict(ok, queued=sttcatchup.QUEUE_IDLE_MAX + 1))[0] is False
    assert whisper_idle(None)[0] is False


def test_nothing_is_read_or_written_at_start():
    calls = []
    cu = CatchUp(settings=lambda: calls.append("settings") or (True, 6.0), may_run=lambda: True,
                 rows=lambda s, n: calls.append("rows") or [], count=lambda s: 0, caught=lambda s: 0,
                 route=lambda k: "whisper", whisper_ready=lambda a: (True, ""), aws_idle=lambda k: True,
                 to_whisper=lambda s: [], to_aws=lambda r: False, log=lambda m: None)
    cu.start()
    time.sleep(0.2)
    cu.stop()
    assert calls == [] and sttcatchup.START_DELAY_S >= 60


# -- the radio log: which rows, in which order ---------------------------------------------------------------------


def record(app, tid, start, *, status="skipped", model=f"m.bin ({SHED_WHY})", tg=None, system="", tier=""):
    app.cad.comms.record({"id": tid, "start": start, "end": start + 2, "source": "Chan" if tg is None else "",
                          "talkgroup": tg, "system": system, "tier": tier, "unit": "", "text": "", "status": status,
                          "model": model})


def test_the_shed_rows_alert_tier_first_then_newest_and_their_keys(tmp_path):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "clips")))
    now = time.time()
    record(app, "a-old", now - 3000, tg=9211, system="mrrs", tier="alert")
    record(app, "t-new", now - 100, tg=9570, system="mrrs", tier="text")
    record(app, "county", now - 200, tg=383, system="westchester")
    record(app, "a-new", now - 500, tg=9211, system="mrrs", tier="alert")
    record(app, "fm", now - 50)
    record(app, "too-old", now - 7 * 3600, tg=383, system="westchester")
    record(app, "error", now - 60, status="error", model="m.bin (not transcribed: speech-to-text queue full)")
    record(app, "done", now - 70, status="done", model="m.bin")
    rows = app.cad.comms.shed_clips(now - 6 * 3600)
    assert [r["id"] for r in rows] == ["a-new", "a-old", "fm", "t-new", "county"]
    keys = {r["id"]: r["key"] for r in rows}
    assert keys == {"a-new": PD, "a-old": PD, "fm": "Chan", "t-new": "p25:mrrs:9570", "county": "tg:383"}
    assert app.cad.comms.shed_count(now - 6 * 3600) == 5 and app.cad.comms.shed_count(now - 8 * 3600) == 6


# -- all-in-one, end to end: shed by the real queue, caught up through the re-run path ------------------------------


def shed_one(app, monkeypatch):
    """A transmission shed for real: its clip waits in the whisper queue as a context talkgroup's (LOW) until whisper,
    stopped meanwhile, is past LOW_MAX_WAIT_S behind."""
    monkeypatch.setattr(app, "_stt_priority", lambda item, key: LOW)
    tid = fm_segment(app)
    q = app.stt._q
    assert wait(lambda: q.qsize() == 1, 5)
    real = q.clock
    q.clock = lambda: real() + stt_mod.LOW_MAX_WAIT_S + 5
    with pytest.raises(stt_mod.queue.Empty):
        q.get_nowait()
    q.clock = real
    assert wait(lambda: (comms_row(app, tid) or {}).get("status") == "skipped", 5), comms_row(app, tid)
    return tid


def test_all_in_one_a_shed_clip_is_caught_up_like_any_rerun(tmp_path, monkeypatch):
    from sizeup.app import App
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    app = App(base(clips_dir=str(tmp_path / "clips")))
    w = app.stt.client = Whisper({3: "Car 12 clear of the scene"})
    try:
        tid = shed_one(app, monkeypatch)
        row = comms_row(app, tid)
        assert row["model"].startswith("m.bin (skipped: busy") and row["text"] == ""
        app.stt.start()
        app.whisper.state = "ok"
        rev = app.cad.comms.rev
        assert app.catchup.tick() == 1
        assert wait(lambda: (comms_row(app, tid) or {}).get("rerun_result"), 10), comms_row(app, tid)
        row = comms_row(app, tid)
        assert row["status"] == "done" and row["text"] == "Car 12 clear of the scene" and row["model"] == "m.bin"
        assert row["rerun_result"] == "changed; catch-up" and app.cad.comms.rev > rev
        assert w.calls == [("fast", 3)]
        assert app.rerun.get("total", 0) == 0                    # no person's re-run progress
        app.catchup.tick()
        assert app.catchup.stats()["caught_24h"] == 1 and app.catchup.stats()["waiting"] == 0
        assert app.catchup.tick() == 0                            # nothing left
    finally:
        app.stop()


def test_a_catch_up_clip_dropped_from_the_queue_stays_skipped_quietly(tmp_path, monkeypatch):
    from sizeup.app import App, Rerun
    app = App(base(clips_dir=str(tmp_path / "clips")))
    try:
        tid = shed_one(app, monkeypatch)
        app._rerun_ids.add(tid)
        app._stt_dropped(Rerun(tid, "traffic", True, catchup=True), "speech-to-text queue full (2000 waiting)")
        row = comms_row(app, tid)
        assert row["status"] == "skipped" and not row.get("rerun_result") and tid not in app._rerun_ids
    finally:
        app.stop()


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


# -- the hub: police on its own AWS budget, everything else on the station's whisper -------------------------------


def pd_alert(app):
    """The invented police dispatch talkgroup in the other system's file, alert tier (transcribed, its alerts first)."""
    app.tg.info[("mrrs", 9211)] = {"name": "PD Dispatch", "tier": "alert"}


def test_the_hub_routes_police_to_its_own_aws_budget_and_never_ours(tmp_path):
    from tests.fake_transcribe import FakeTranscribe
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        app = hub.app
        pd_alert(app)                     # police dispatch: transcribed, its alerts first
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
    finally:
        hub.close()
        fake.close()


def test_the_hub_catches_up_a_police_clip_on_aws_within_its_own_budget(tmp_path):
    import base64
    from sizeup.cad.dispatcher import wav_bytes
    from sizeup.transcribe import CONTEXT as POOL
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_app import voice
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    try:
        now = time.time()
        tid = "9211-1794-call_7"
        ev = {"seq": 1, "at": now, "type": "traffic",
              "data": {"id": tid, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs", "tier": "alert",
                       "start": now - 3, "end": now, "text": "", "status": "skipped", "model": f"m.bin ({SHED_WHY})"},
              "audio": base64.b64encode(wav_bytes((voice(3.0)[::2] * 32767).astype(np.int16), 8000)).decode()}
        assert call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})[0] == 200
        app = hub.app
        pd_alert(app)
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(now - 3600)] == [tid], 10)
        ours = app.aws.spent()["billed_s"]
        assert app.catchup.tick() == 1
        assert wait(lambda: (app.cad.comms.store.one("SELECT rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {})
                    .get("rerun_result"), 20)
        r = app.cad.comms.store.one("SELECT status, text, model, rerun_result FROM radio_traffic WHERE id = ?", (tid,))
        assert r["status"] == "done"
        assert r["text"] == AWS_TEXT + "." and r["model"] == "aws-transcribe" and r["rerun_result"] == "changed; catch-up"
        assert app.aws.spent(POOL)["billed_s"] > 0 and app.aws.spent()["billed_s"] == ours      # never our pool
        assert wait(lambda: app.catchup.tick() == 0 and app.catchup.stats()["caught_24h"] == 1, 5)
        assert app.catchup.stats()["waiting"] == 0
        assert app.health.model()["stt_catchup"]["caught_24h"] == 1
    finally:
        hub.close()
        fake.close()


def test_the_hub_has_the_stations_whisper_catch_up_once_it_is_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        st.whisper.state = "ok"
        w = Whisper({3: "Car 12 clear of the scene"})
        tid = shed_one(st, monkeypatch)                           # (the station's whisper not running yet)
        app = hub.app
        assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(time.time() - 3600)] == [tid], 15)
        st.stt.client = w
        st.stt.start()
        # (once the station's report says its whisper is idle: a tick or a few)
        assert wait(lambda: (app.catchup.tick(), app.catchup.stats()["sent"])[1] == 1, 20), app.catchup.stats()
        assert wait(lambda: (app.cad.comms.store.one("SELECT rerun_result FROM radio_traffic WHERE id = ?", (tid,)) or {})
                    .get("rerun_result"), 20)
        r = app.cad.comms.store.one("SELECT status, text, model, rerun_result FROM radio_traffic WHERE id = ?", (tid,))
        assert r["status"] == "done"
        assert r["text"] == "Car 12 clear of the scene" and r["model"] == "m.bin" and r["rerun_result"] == "changed; catch-up"
        assert w.calls == [("fast", 3)]
        assert wait(lambda: app.catchup.tick() == 0 and app.catchup.stats()["caught_24h"] == 1, 5)
        assert app.catchup.stats()["waiting"] == 0
        assert app.health.model()["stt_catchup"]["caught_24h"] == 1
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
    finally:
        hub.close()
