"""Parked cameras (#550, sizeup/dvr.py): a camera dead upstream is parked instead of retried forever, looked at now
and then, and back on its own when it answers.

The shapes are the hub's log from 2026-11-08 on: the Thruway's 1ne01240n (TA_057) connects and never answers
(TimeoutError), 1cw00710e (TA_011) answers its master playlist with a 404; the 511NY cameras that restart at their
origin every 5-30 min fail in short bursts (timeouts, 404s) and must never park. The cameras are the Thruway fixtures,
the stream tests/test_hlsseg.py's Origin.
"""

from __future__ import annotations

import json
import random
from urllib.error import HTTPError, URLError

from sizeup import dvr as dvr_mod
from sizeup import errwatch
from sizeup.dvr import PARK_404_S, PARK_AFTER_S, PARK_CHECK_S, PARKED_FILE, Dvr
from tests.test_dvr import setup

DEAD = "1ne01240n"                      # TA_057: TLS connects, no HTTP answer
GONE = "1cw00710e"                      # TA_011: master playlist 404


class Upstream:
    """The origin as the DVR sees it: Origin's stream, except the cameras made to fail (by their stream folder)."""

    def __init__(self, r, f):
        self.r, self.f, self.down, self.asked = r, f, {}, []

    def stream(self, cid: str) -> str:
        return self.r.camera(cid)["video"].rsplit("/", 1)[0] + "/"

    def __call__(self, url, timeout, limit):
        for cid, how in self.down.items():
            if url.startswith(self.stream(cid)):
                self.asked.append((cid, url))
                if how == "timeout":
                    raise URLError(TimeoutError("_ssl.c:989: The handshake operation timed out"))
                if how == "404":
                    raise HTTPError(url, 404, "Not Found", None, None)
        return self.f(url, timeout, limit)


def rig(tmp_path, **kw):
    r, d, f, clock = setup(tmp_path, **kw)
    up = Upstream(r, f)
    r.fetch = up
    return r, d, f, clock, up


def run(d, f, clock, seconds, dt=15.0):
    end = clock.t + seconds
    while clock.t < end:
        f.advance()
        clock.t += dt
        d.tick()


def parked_lines(d):
    return [m for m in d.logs if " parked: " in m]


def test_a_camera_that_never_answers_is_parked_after_an_hour_with_one_line(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD}
    up.down[DEAD] = "timeout"
    run(d, f, clock, PARK_AFTER_S - 120)
    assert DEAD not in d.parked and d.wanted() == {DEAD: ["pinned"]}             # still learning: tried, not parked
    run(d, f, clock, 120 + dvr_mod.SESSION_RETRY_S + 30)
    assert DEAD in d.parked and d.parked[DEAD]["why"] == "no answer"
    (line,) = parked_lines(d)
    assert line.startswith(f"dvr: {DEAD} parked: no answer for ")
    assert errwatch.classify(line) is None                                         # not a hub fault: errwatch doesn't count it
    assert d.wanted() == {} and not d.status(DEAD)["recording"] and d.status(DEAD)["parked"]["why"] == "no answer"
    cam = [c for c in r.public()["cameras"] if c["id"] == DEAD][0]
    assert cam["rec"] is False                                                     # the map and the wall: not recording


def test_a_master_playlist_404_is_parked_sooner(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {GONE}
    up.down[GONE] = "404"
    run(d, f, clock, PARK_404_S - 60)
    assert GONE not in d.parked
    run(d, f, clock, 60 + dvr_mod.SESSION_RETRY_S + 30)
    assert GONE in d.parked and d.parked[GONE]["why"] == "HTTP 404"
    assert clock.t - d.parked[GONE]["since"] < PARK_AFTER_S                        # well before the hour


def test_a_camera_that_restarts_every_5_to_30_min_never_parks(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD}
    rnd = random.Random(550)
    end = clock.t + 12 * 3600
    while clock.t < end:
        run(d, f, clock, rnd.uniform(300, 1800))                                   # up 5-30 min
        up.down[DEAD] = rnd.choice(["timeout", "404"])                             # a restart: a short burst of either
        run(d, f, clock, rnd.uniform(20, 240))
        up.down.pop(DEAD)
    run(d, f, clock, 600)
    assert d.parked == {} and parked_lines(d) == [] and d.status(DEAD)["recording"]
    assert sum("no stream" in m for m in d.logs) > 10                              # (the bursts did fail it, and wait)
    assert d.status(DEAD)["segments"] > 300                                        # recorded between the restarts


def park(tmp_path, cid=DEAD, **kw):
    r, d, f, clock, up = rig(tmp_path, **kw)
    d.always = (cid,)
    up.down[cid] = "timeout"
    run(d, f, clock, PARK_AFTER_S + dvr_mod.SESSION_RETRY_S + 30)
    assert cid in d.parked
    return r, d, f, clock, up


def test_while_parked_only_the_check_asks_every_30_min_and_logs_nothing(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.asked.clear()
    n = len(d.logs)
    t0 = clock.t
    run(d, f, clock, 2 * 3600 + 60)
    checks = [u for _, u in up.asked]
    assert len(checks) == 4 and all(u.endswith("/playlist.m3u8") for u in checks)   # one request each (it doesn't answer)
    assert d.logs[n:] == []                                                        # no per-try lines while parked
    assert d.parked[DEAD]["next"] > clock.t and d.parked[DEAD]["checked"] > t0


def test_a_check_that_answers_brings_it_back_and_it_records_again(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.down.pop(DEAD)                                                              # the camera is back upstream
    nxt = d.parked[DEAD]["next"]
    run(d, f, clock, nxt - clock.t - 30)
    assert DEAD in d.parked                                                        # not before its check
    run(d, f, clock, 60)
    assert DEAD not in d.parked
    back = [m for m in d.logs if f"dvr: {DEAD} back" in m]
    assert len(back) == 1 and errwatch.classify(back[0]) is None
    assert not any(m.startswith("dvr: recording ") and DEAD in m for m in d.logs[d.logs.index(back[0]):])   # one line
    run(d, f, clock, 60)
    st = d.status(DEAD)
    assert st["recording"] and st["parked"] is None and st["segments"] > 0
    assert json.loads((tmp_path / "dvr" / PARKED_FILE).read_text()) == {}


def test_the_check_runs_on_the_pool_when_started(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    r, d, f, clock, up = park(tmp_path)
    up.down.pop(DEAD)
    clock.t = d.parked[DEAD]["next"] + 1
    d._pool = ThreadPoolExecutor(max_workers=2)
    try:
        d.tick()
        d.tick()                                                                   # (under way or done: never twice)
    finally:
        d._pool.shutdown(wait=True)
    assert DEAD not in d.parked and d._busy == set()
    assert sum(f"dvr: {DEAD} back" in m for m in d.logs) == 1


def test_roads_own_probe_counts_and_an_open_live_stream_brings_it_back(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.asked.clear()
    clock.t = d.parked[DEAD]["next"] + 2
    r._probe[DEAD] = {"at": clock.t - 1, "ok": False}                              # roads' sweep looked: still down
    d.tick()
    assert up.asked == [] and d.parked[DEAD]["next"] == clock.t - 1 + PARK_CHECK_S  # no second request to the provider
    r._probe[DEAD] = {"at": clock.t + 1, "ok": True}                               # ... and now it streams
    clock.t += 5
    d.tick()
    assert up.asked == [] and DEAD not in d.parked
    r2, d2, f2, clock2, up2 = park(tmp_path / "b")
    up2.down.pop(DEAD)
    assert r2.get(f"/api/cad/cameras/{DEAD}/stream/playlist.m3u8", "")[0] == 200   # someone opens it and it plays
    r2.pump_once(DEAD)
    d2.tick()
    assert DEAD not in d2.parked and any("back (watched live)" in m for m in d2.logs)


def test_the_slot_goes_to_the_next_camera_and_comes_back(tmp_path):
    r, d, f, clock, up = rig(tmp_path, max_cameras=2)
    vids = [c["id"] for c in r.merged("cameras") if c.get("video") and c["id"] != DEAD][:2]
    d.always = (DEAD,)
    d.pins = set(vids)
    assert list(d.wanted()) == [DEAD, vids[0]]                                     # the cap: the second kept one waits
    up.down[DEAD] = "timeout"
    run(d, f, clock, PARK_AFTER_S + dvr_mod.SESSION_RETRY_S + 30)
    assert list(d.wanted()) == [vids[0], vids[1]] and d.status(vids[1])["recording"]
    assert d.panel()["recording"] == 2 and [p["id"] for p in d.panel()["parked"]] == [DEAD]
    up.down.pop(DEAD)
    run(d, f, clock, PARK_CHECK_S + 30)
    assert list(d.wanted()) == [DEAD, vids[0]] and not d.status(vids[1])["recording"]


def test_errwatch_is_quiet_for_a_parked_camera(tmp_path):
    r, d, f, clock, up = park(tmp_path, cid=GONE)
    n = len(d.logs)
    run(d, f, clock, 6 * 3600)
    w = errwatch.ErrorWatch(clock=lambda: clock.t, log=lambda m: None)
    for m in d.logs[n:] + parked_lines(d):
        w.feed(m + "\n")
    w.drain()
    assert d.logs[n:] == [] and w.total == 0


def test_parked_survives_a_restart_and_only_the_leader_writes_it(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    p = dict(d.parked[DEAD])
    saved = json.loads((tmp_path / "dvr" / PARKED_FILE).read_text())
    assert saved[DEAD]["why"] == "no answer" and not list((tmp_path / "dvr").glob("*.tmp"))
    # the next task (a deploy, a restart): still parked, no hour of learning, its check when it was due
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=d.logs.append, free_floor=0)
    again.always = (DEAD,)
    assert again.parked == {DEAD: p} and again.wanted() == {}
    up.asked.clear()
    again.tick()
    assert up.asked == []
    # a task that isn't the leader (shared storage: the lease) never writes it
    leader = {"on": False}
    standby = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0, may_write=lambda: leader["on"])
    standby._unpark(DEAD, "answered")
    assert DEAD in json.loads((tmp_path / "dvr" / PARKED_FILE).read_text())
    leader["on"] = True
    standby._save_parked()
    assert json.loads((tmp_path / "dvr" / PARKED_FILE).read_text()) == {}


def test_a_damaged_parked_file_is_no_parked_camera(tmp_path):
    (tmp_path / "dvr").mkdir()
    (tmp_path / "dvr" / PARKED_FILE).write_text('{"../x": {"since": 1}, "1ne01240n": {"why": "x"}, "ok": [1]')
    r, d, f, clock = setup(tmp_path)
    assert d.parked == {}
    (tmp_path / "dvr" / PARKED_FILE).write_text('{"../x": {"since": 1, "at": 1, "checked": 1, "next": 1}, '
                                                 '"1ne01240n": {"why": "x"}}')
    r, d, f, clock = setup(tmp_path)
    assert d.parked == {}


def test_health_shows_the_parked_cameras(tmp_path):
    from tests.test_health import Rig
    r, d, f, clock, up = park(tmp_path)
    h = Rig()
    assert h.h.panel()["dvr"] is None                                              # no DVR: no section
    h.h.dvr = d.panel
    dv = h.h.panel()["dvr"]
    assert dv["max"] == d.max_cameras and dv["check_min"] == PARK_CHECK_S / 60
    (p,) = dv["parked"]
    assert p["id"] == DEAD and p["why"] == "no answer" and p["next"] == p["checked"] + PARK_CHECK_S
    h.h.dvr = lambda: 1 / 0
    assert h.h.panel()["dvr"] == {"error": "ZeroDivisionError"}                    # never in the way of the rest
    from pathlib import Path
    js = (Path(dvr_mod.__file__).parent / "web" / "static" / "health.js").read_text()
    assert '"Camera DVR"' in js and "next check ${hm(p.next)}" in js
    ml = (Path(dvr_mod.__file__).parent / "web" / "static" / "maplayers.js").read_text()
    assert 'd.parked ? "Parked"' in ml
