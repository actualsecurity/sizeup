"""Auto · normal (#954) x a station's rows at the clip's close (#931) x background work (#896): the tiers never bleed into
each other. The pre-alert speed gate measures priority work alone; a row already on the timeline is never shown twice,
whichever way its clip is re-routed; a priority clip never waits behind a normal decode; a restart recovers normal clips
by their own rules; a GPU gone frees normal clips at once. Invented clips and ids; fakes on 127.0.0.1 only."""

from __future__ import annotations

from sizeup import sttpool, sttroute
from sizeup.stt import CONTEXT, URGENT
from tests.test_stt_pool import Clock, Jobs, online, pool


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
