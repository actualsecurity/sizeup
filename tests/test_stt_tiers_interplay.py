"""Auto · normal (#954) x a station's rows at the clip's close (#931) x background work (#896): the tiers never bleed into
each other. The pre-alert speed gate measures priority work alone; a row already on the timeline is never shown twice,
whichever way its clip is re-routed; a priority clip never waits behind a normal decode; a restart recovers normal clips
by their own rules; a GPU gone frees normal clips at once. Invented clips and ids; fakes on 127.0.0.1 only."""

from __future__ import annotations

import base64
import time

import numpy as np
import pytest

from sizeup import sttpool, sttroute
from sizeup.app import STATION_WHISPER, WHISPER_FALLBACK_STATION
from sizeup.incidents import Traffic
from sizeup.stt import CONTEXT, URGENT
from tests.fake_transcribe import FakeTranscribe
from tests.test_app import voice, wait
from tests.test_channels import AWS_TEXT, aws_cfg, fake_aws  # noqa: F401 (fake_aws: a fixture)
from tests.test_cloud import HubRun, hub_cfg
from tests.test_stt_engine_select import _set
from tests.test_stt_pool import GPU_TEXT, Clock, Jobs, online, pool, send


# -- 1. the pre-alert speed gate: priority work alone ------------------------------------------------------------------


def _decode(p, clk, wid, job, took):
    assert p.submit(job), job.id
    got = p.claim(wid, 0)
    assert got is not None and got.id == job.id
    clk.t += took
    assert p.complete(wid, got.lease, {"text": "x"})


def test_normal_and_background_pre_alert_class_clips_never_feed_the_speed_gate():
    clk, jobs = Clock(), Jobs()
    p = pool(clk)
    w = online(p, "gpu-1")
    for i in range(3):                                   # three 25 s Auto · normal URGENT clips at ~4 s each
        clk.t += sttroute.BG_QUIET_S + 1
        _decode(p, clk, "gpu-1", jobs.job(f"norm-{i}", URGENT, seconds=25.0, forced=True, norm=True,
                                          extra={"claim_wait": sttroute.NORM_GPU_BUDGET_S}), 4.0)
    clk.t += sttroute.BG_QUIET_S + 1                     # an Auto · background overflow clip of a context alert class
    _decode(p, clk, "gpu-1", jobs.job("bg-ctx", CONTEXT, seconds=10.0, forced=True, bg=True,
                                      extra={"overflow": True}), 3.5)
    assert not w.fast
    assert p.ready_forced(URGENT) == (True, "") and p.ready(URGENT) == (True, "")
    # a normal lease lost (the cap) is no pre-alert slowness either
    clk.t += sttroute.BG_QUIET_S + 1
    assert p.submit(jobs.job("norm-lost", URGENT, seconds=5.0, forced=True, norm=True))
    lost = p.claim("gpu-1", 0)
    assert lost.id == "norm-lost"
    clk.t = lost.expires + 0.1
    p.seen("gpu-1")
    assert p.tick() == 1 and p.drain() == 1 and "norm-lost" in jobs.fell
    assert not w.fast and p.ready_forced(URGENT) == (True, "")
    # (the control: one priority pre-alert clip at 3.5 s does close the gate)
    _decode(p, clk, "gpu-1", jobs.job("prio", URGENT, seconds=5.0, forced=True), 3.5)
    assert len(w.fast) == 1 and p.ready_forced(URGENT)[1].startswith("gpu-1 slow on pre-alert clips lately")


def test_the_priority_helper_is_rank_zero_only():
    jobs = Jobs()
    assert sttpool.priority(jobs.job("a", URGENT))
    assert not sttpool.priority(jobs.job("b", URGENT, norm=True))
    assert not sttpool.priority(jobs.job("c", CONTEXT, bg=True))
    assert not sttpool.priority(jobs.job("d", URGENT, extra={"shadow": True}))
    assert not sttpool.priority(jobs.job("e", URGENT, extra={"station_only": True}))


# -- 2 + 3. a row on the timeline once, whatever way its clip goes -------------------------------------------------------


SAID = "Engine 7 on location, nothing showing"
NORM_KEY = "tg:325"


def _audio_b64(seconds=3.0):
    from sizeup.cad.dispatcher import wav_bytes
    return base64.b64encode(wav_bytes((voice(seconds)[::2] * 32767).astype(np.int16), 8000)).decode()


def _src(key):
    return {"source": "Neighbor FD", "talkgroup": 325} if key == NORM_KEY else {"source": "FG1"}


def _close(tid, key, seq, at=None):
    """The station's transmission at its close (#927): on its whisper, no text yet."""
    end = time.time() - 20 if at is None else at
    return {"seq": seq, "at": time.time(), "type": "traffic", "audio": _audio_b64(),
            "data": {"id": tid, **_src(key), "start": end - 3.0, "end": end, "text": "", "raw": "", "status": "pending",
                     "model": WHISPER_FALLBACK_STATION, "transcript_pending": "station", "attach": True}}


def _handed(tid, key, seq):
    """The station handing a clip straight to the hub's pool (#639 phase 2: its whisper behind)."""
    end = time.time() - 5
    return {"seq": seq, "at": time.time(), "type": "traffic", "audio": _audio_b64(),
            "data": {"id": tid, **_src(key), "start": end - 3.0, "end": end, "text": "", "status": "remote",
                     "model": "pool", "transcript_pending": "pool", "stt_class": "low", "attach": True}}


def _pool_fill(tid, seq):
    """(#931) whisper shed the row it showed at the close: the hub's pool's now."""
    return {"seq": seq, "at": time.time(), "type": "retext",
            "data": {"id": tid, "fill": True, "transcript_pending": "pool", "text": "", "status": "remote",
                     "model": "pool", "stt_class": "low"}}


def _late_fill(tid, seq):
    """The station's whisper finishing a clip the hub had withdrawn from it (authoritative: false)."""
    return {"seq": seq, "at": time.time(), "type": "retext",
            "data": {"id": tid, "fill": True, "authoritative": False, "text": SAID, "status": "done",
                     "model": "m.bin", "engine": STATION_WHISPER}}


class Hooks:
    """What a row sets off on the hub, by id: the traffic event, the CAD's row, the alarm and ATC hooks (at the row's
    showing: no words; on its text: the words)."""

    def __init__(self, app, monkeypatch):
        self.n: dict[tuple[str, str], int] = {}
        for name in ("_emit", "_to_cad", "_alarm", "_atc"):
            real = getattr(app, name)

            def spy(*a, _real=real, _name=name, **k):
                if _name == "_emit":
                    if a[0] == "traffic":
                        self._add("traffic", str(a[1].get("id")))
                else:
                    words = a[1] if len(a) > 1 else k.get("text")
                    self._add(_name.strip("_") + (":text" if words else ""), str(a[0].id))
                return _real(*a, **k)
            monkeypatch.setattr(app, name, spy)

    def _add(self, what, tid):
        self.n[(what, tid)] = self.n.get((what, tid), 0) + 1

    def of(self, tid):
        return {w: c for (w, t), c in self.n.items() if t == tid}


def _held(app, tid):
    return [t for t in list(app.store.loose) + [t for i in app.store.incidents for t in i.traffic] if t.id == tid]


def _row(hub, tid):
    return hub.app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,)) or {}


def _evict(app):
    for k in range(1000):                                # (an evening's newer rows: it leaves memory)
        app.store.loose.append(Traffic(f"FG1-{k}", "FG1", 1.0 + k, 2.0 + k, "", status="done"))


@pytest.fixture
def nhub(tmp_path, monkeypatch, fake_aws):  # noqa: F811 (the fixture: test credentials only)
    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    asked = []
    monkeypatch.setattr(hub.app.hub, "remote", lambda fn, args, *a, **k: asked.append((fn, args)) or 1)
    hub.asked = asked
    yield hub
    hub.close()
    fake.close()


def _gpu(hub):
    """A GPU worker in a claim now (the test claims and completes its jobs itself)."""
    hub.app.hub.workers.add("gpu-1")
    online(hub.app.sttpool, "gpu-1")


def _take(hub, tid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        hub.app.sttpool.seen("gpu-1")
        job = hub.app.sttpool.claim("gpu-1", 0)
        if job is not None:
            assert job.id == tid, job.id
            return job
        time.sleep(0.02)
    raise AssertionError(f"{tid} never reached the GPU")


def test_auto_normal_refused_by_the_pool_after_all_shows_its_row_once(nhub, monkeypatch):
    hub = nhub
    _set(hub, NORM_KEY, sttroute.AUTO_NORM)
    _gpu(hub)
    hooks = Hooks(hub.app, monkeypatch)
    monkeypatch.setattr(hub.app.sttpool, "submit", lambda job: False)      # (the pool refuses it: _norm_rest on)
    send(hub, _handed("TG325-1", NORM_KEY, 1))
    assert wait(lambda: _row(hub, "TG325-1").get("text") == AWS_TEXT + ".", 30), _row(hub, "TG325-1")
    assert len(_held(hub.app, "TG325-1")) == 1
    got = hooks.of("TG325-1")
    assert (got.get("traffic"), got.get("to_cad"), got.get("alarm"), got.get("atc")) == (1, 1, 1, 1), got


def test_auto_normal_falling_back_from_the_gpu_shows_its_row_once(nhub, monkeypatch):
    hub = nhub
    _set(hub, NORM_KEY, sttroute.AUTO_NORM)
    _gpu(hub)
    hooks = Hooks(hub.app, monkeypatch)
    send(hub, _handed("TG325-2", NORM_KEY, 1))
    job = _take(hub, "TG325-2")
    assert hub.app.sttpool.release("gpu-1", job.lease, "test")            # (its GPU hands it back: stations, AWS)
    assert wait(lambda: _row(hub, "TG325-2").get("text") == AWS_TEXT + ".", 30), _row(hub, "TG325-2")
    assert len(_held(hub.app, "TG325-2")) == 1
    got = hooks.of("TG325-2")
    assert (got.get("traffic"), got.get("to_cad"), got.get("alarm"), got.get("atc")) == (1, 1, 1, 1), got


@pytest.mark.parametrize("engine,key", [(sttroute.AUTO, "FG1"), (sttroute.AUTO_NORM, NORM_KEY)])
@pytest.mark.parametrize("case", ["memory", "evicted", "takeover"])
def test_a_pool_fill_of_a_row_shown_at_its_close_is_one_row_and_the_pools_text_fills_it(nhub, monkeypatch, engine,
                                                                                         key, case):
    hub = nhub
    _set(hub, key, engine)
    _gpu(hub)
    hooks = Hooks(hub.app, monkeypatch)
    tid = f"{'TG325' if key == NORM_KEY else 'FG1'}-{int(time.time() * 1000) % 10_000_000}"
    send(hub, _close(tid, key, 1))
    assert _row(hub, tid)["status"] == "pending" and hub.app.mdb.is_waiting(tid)
    if case in ("evicted", "takeover"):
        _evict(hub.app)
        assert _held(hub.app, tid) == []
    if case == "takeover":                               # (hub.sqlite behind cad.sqlite; the close replayed)
        hub.app.mdb.wait_drop([tid])
        send(hub, dict(_close(tid, key, 2), at=time.time()))
    send(hub, _pool_fill(tid, 3))
    job = _take(hub, tid)                                # the GPU has it now
    assert not hub.app.mdb.is_waiting(tid)               # (the pool's: the sweep and the AWS resend leave it be)
    assert hub.app._flag_waiting(time.time() + 7 * 86400) == 0 and hub.app._aws_resend() == 0
    assert _row(hub, tid)["status"] == "pending"
    # the station's whisper finishing it anyway after the withdraw: a run, never the text
    send(hub, _late_fill(tid, 4))
    assert _row(hub, tid)["status"] == "pending" and not _row(hub, tid)["text"]
    assert [r["role"] for r in hub.app.stt_runs.of(tid)] == ["late"]
    # the pool's text fills the same id
    assert hub.app.sttpool.complete("gpu-1", job.lease, {"text": GPU_TEXT, "model": "ggml-large-v3-turbo-q5_0.bin"})
    assert wait(lambda: _row(hub, tid).get("text") == GPU_TEXT, 10), _row(hub, tid)
    assert len(hub.app.cad.store.q("SELECT id FROM radio_traffic WHERE id = ?", (tid,))) == 1
    assert len(_held(hub.app, tid)) == (1 if case == "memory" else 0)
    got = hooks.of(tid)
    assert (got.get("traffic"), got.get("to_cad"), got.get("alarm"), got.get("atc")) == (1, 1, 1, 1), got
    if case == "memory":             # (the withdrawn whisper's words reach the alarms too, by design: they dedupe by id)
        assert (got.get("alarm:text"), got.get("atc:text")) == (2, 1), got


def test_the_store_never_holds_one_id_twice():
    from sizeup.incidents import IncidentStore
    st = IncidentStore(set())
    a = Traffic("FG1-1", "FG1", 1.0, 2.0, "", status="pending")
    st.on_traffic(a, attach=False)
    b = Traffic("FG1-1", "FG1", 1.0, 2.0, "", status="pending")
    b.model = "gpu-worker"
    st.on_traffic(b, attach=False)
    st.on_traffic(a, attach=False)
    assert list(st.loose) == [a] and a.model == "gpu-worker"
