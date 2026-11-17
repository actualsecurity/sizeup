"""Parked cameras (#550, sizeup/dvr.py): a camera dead upstream is parked instead of retried forever, looked at now
and then, and back on its own when it answers.

The shapes are the hub's log from 2026-11-08 on: the Thruway's 1ne01240n (TA_057) connects and never answers
(TimeoutError), 1cw00710e (TA_011) answers its master playlist with a 404; the 511NY cameras that restart at their
origin every 5-30 min fail in short bursts (timeouts, 404s) and must never park. The cameras are the Thruway fixtures,
the stream tests/test_hlsseg.py's Origin. Another camera (OK) records alongside, as on the hub: a camera parks only
while others record.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from urllib.error import HTTPError, URLError

from sizeup import dvr as dvr_mod
from sizeup import errwatch
from sizeup.dvr import PARK_404_S, PARK_AFTER_S, PARK_CHECK_S, PARK_ROUND_S, PARKED_FILE, SESSION_RETRY_S, Dvr
from tests.test_dvr import setup

DEAD = "1ne01240n"                      # TA_057: TLS connects, no HTTP answer
GONE = "1cw00710e"                      # TA_011: master playlist 404
OK = "1ne01370s"                        # TA_058: streams
PARK_BY = PARK_AFTER_S + 3 * SESSION_RETRY_S + 60     # the hour, its tries at the 300 s cap, and a little more


class Upstream:
    """The origin as the DVR sees it: Origin's stream, except the cameras made to fail (by their stream folder).
    how: "timeout", "404", "probe" (only a 5 s request times out: the probe's), "ts" (only the segments), or the share of 30 s spans in which
    it times out (a flaky camera: each round or check either answers or doesn't)."""

    def __init__(self, r, f, rnd=None):
        self.r, self.f, self.down, self.asked, self.rnd = r, f, {}, [], rnd or random.Random(550)
        self.spans: dict[tuple, bool] = {}

    def span_down(self, cid: str, share: float) -> bool:
        span = (cid, self.r.clock() // 30)
        if span not in self.spans:
            self.spans[span] = self.rnd.random() < share
        return self.spans[span]

    def stream(self, cid: str) -> str:
        return self.r.camera(cid)["video"].rsplit("/", 1)[0] + "/"

    def __call__(self, url, timeout, limit):
        for cid, how in self.down.items():
            if url.startswith(self.stream(cid)):
                if how == "probe" and timeout != dvr_mod.PROBE_TIMEOUT_S or how == "ts" and not url.endswith(".ts"):
                    break
                self.asked.append((cid, url, self.r.clock()))
                if isinstance(how, float) and not self.span_down(cid, how):
                    break
                if how == "404":
                    raise HTTPError(url, 404, "Not Found", None, None)
                raise URLError(TimeoutError("_ssl.c:989: The handshake operation timed out"))
        return self.f(url, timeout, limit)


def rig(tmp_path, **kw):
    r, d, f, clock = setup(tmp_path, **kw)
    d.mono = lambda: clock.t
    up = Upstream(r, f)
    r.fetch = up
    return r, d, f, clock, up


def until_parked(d, f, clock, cid=DEAD, limit=PARK_BY):
    end = clock.t + limit
    while cid not in d.parked and clock.t < end:                                   # (stops as it parks)
        run(d, f, clock, 15)
    assert cid in d.parked


def run(d, f, clock, seconds, dt=15.0):
    end = clock.t + seconds
    while clock.t < end:
        f.advance()
        clock.t += dt
        d.tick()


def parked_lines(d):
    return [m for m in d.logs if " parked: " in m]


def checks(up, cid=DEAD):
    """The checks' requests (5 s each), grouped by when (a check: up to two tries, at one moment)."""
    out = {}
    for c, u, t in up.asked:
        if c == cid:
            out.setdefault(t, []).append(u)
    return out


def test_a_camera_that_never_answers_is_parked_after_an_hour_with_one_line(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    up.down[DEAD] = "timeout"
    run(d, f, clock, PARK_AFTER_S - 120)
    assert DEAD not in d.parked and DEAD in d.wanted()                             # still learning: tried, not parked
    run(d, f, clock, PARK_BY - PARK_AFTER_S + 120)
    assert DEAD in d.parked and d.parked[DEAD]["why"] == "no answer"
    (line,) = parked_lines(d)
    assert line.startswith(f"dvr: {DEAD} parked: no answer for ")
    assert errwatch.classify(line) is None                                         # not a hub fault: errwatch doesn't count it
    assert d.wanted() == {OK: ["pinned"]} and not d.status(DEAD)["recording"] and d.status(DEAD)["parked"]["why"] == "no answer"
    cam = [c for c in r.public()["cameras"] if c["id"] == DEAD][0]
    assert cam["rec"] is False                                                     # the map and the wall: not recording


def test_a_master_playlist_404_is_parked_sooner(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {GONE, OK}
    up.down[GONE] = "404"
    run(d, f, clock, PARK_404_S - 60)
    assert GONE not in d.parked
    run(d, f, clock, 60 + SESSION_RETRY_S + 30)
    assert GONE in d.parked and d.parked[GONE]["why"] == "HTTP 404"
    assert clock.t - d.parked[GONE]["since"] < PARK_AFTER_S / 2                    # well before the hour


def test_a_camera_that_restarts_every_5_to_30_min_never_parks(tmp_path):
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    rnd = random.Random(550)
    end = clock.t + 12 * 3600
    while clock.t < end:
        run(d, f, clock, rnd.uniform(300, 1800), dt=30.0)                          # up 5-30 min
        up.down[DEAD] = rnd.choice(["timeout", "404"])                             # a restart: a short burst of either
        run(d, f, clock, rnd.uniform(20, 240), dt=30.0)
        up.down.pop(DEAD)
    run(d, f, clock, 600)
    assert d.parked == {} and parked_lines(d) == [] and d.status(DEAD)["recording"]
    assert sum("no stream" in m for m in d.logs) > 10                              # (the bursts did fail it, and wait)
    assert d.status(DEAD)["segments"] > 150                                        # recorded between the restarts


def test_a_camera_recording_from_a_live_viewer_is_not_failing(tmp_path):
    # (review) the counters of an earlier burst must not outlive a recording from the viewers' copy: 50 min failing,
    # then 2 h watched live and recorded, then one 4 s timeout: one failure, not "parked: no answer for 170 min"
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    up.down[DEAD] = "timeout"
    run(d, f, clock, 50 * 60)
    assert d._cams[DEAD]["fails"] > 5 and DEAD not in d.parked
    up.down.pop(DEAD)
    clock.t = max(clock.t, d._cams[DEAD].get("retry_at", 0))
    assert r.get(f"/api/cad/cameras/{DEAD}/stream/playlist.m3u8", "")[0] == 200
    for _ in range(2 * 3600 // 4):                                                 # a viewer keeps it open: the pump's copy
        f.advance()
        clock.t += 4
        r.pump_once(DEAD)
        d.tick()
    assert d._cams[DEAD]["fails"] == 0 and "fail_since" not in d._cams[DEAD] and d.status(DEAD)["segments"] > 1000
    r._streams.pop(DEAD)                                                           # the viewer closes; one round fails
    up.down[DEAD] = "timeout"
    clock.t += 4
    d.tick()
    assert DEAD not in d.parked and d._cams[DEAD]["fails"] == 1


def park(tmp_path, cid=DEAD, **kw):
    r, d, f, clock, up = rig(tmp_path, **kw)
    d.always = (cid,)
    d.pins = {OK}
    up.down[cid] = "timeout"
    until_parked(d, f, clock, cid)
    return r, d, f, clock, up


def test_while_parked_only_the_checks_ask_sooner_then_every_30_min_and_log_nothing(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.asked.clear()
    n, t0 = len(d.logs), d.parked[DEAD]["at"]
    run(d, f, clock, 3 * 3600)
    when = sorted(checks(up))
    assert all(len(u) <= 2 and all(x.endswith("/playlist.m3u8") for x in u) for u in checks(up).values())
    gaps = [round(b - a, -1) for a, b in zip([t0] + when, when)]
    assert gaps[:5] == [SESSION_RETRY_S, 600, 1200, PARK_CHECK_S, PARK_CHECK_S]     # 5 min, doubling to 30 min
    assert d.logs[n:] == []                                                        # no per-try lines while parked
    assert d.parked[DEAD]["next"] > clock.t


def test_a_check_that_answers_brings_it_back_and_it_records_again(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.down.pop(DEAD)                                                              # the camera is back upstream
    run(d, f, clock, SESSION_RETRY_S - 30)
    assert DEAD in d.parked                                                        # not before its first check
    run(d, f, clock, 60)
    assert DEAD not in d.parked
    back = [m for m in d.logs if f"dvr: {DEAD} back" in m]
    assert len(back) == 1 and errwatch.classify(back[0]) is None
    assert not any(m.startswith("dvr: recording ") and DEAD in m for m in d.logs[d.logs.index(back[0]):])   # one line
    run(d, f, clock, 60)
    st = d.status(DEAD)
    assert st["recording"] and st["parked"] is None and st["segments"] > 0
    assert json.loads((tmp_path / "dvr" / PARKED_FILE).read_text()) == {}


def test_a_flaky_but_working_camera_is_never_parked_for_long(tmp_path):
    # (review) a camera that fails 4 tries in 5 still records now and then: it may park after a bad hour, but its
    # checks come back soon (5 min, doubling) and a check that answers brings it back
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    run(d, f, clock, 3600)                                                         # it was recording
    up.down[DEAD] = 0.8
    spans, start, kept = [], None, 0
    for _ in range(24 * 3600 // 30):
        n = d.status(DEAD)["segments"]
        run(d, f, clock, 30, dt=30.0)
        kept += max(0, d.status(DEAD)["segments"] - n)
        if DEAD in d.parked and start is None:
            start = clock.t
        elif DEAD not in d.parked and start is not None:
            spans.append(clock.t - start)
            start = None
    parked = sum(spans) + (clock.t - start if start is not None else 0)
    assert kept > 20 and spans                                                     # it does record (and did park)
    assert parked < 0.1 * 24 * 3600 and max(spans) <= 3600                         # never parked for long: checked every 5 min
    assert len(spans) == len([m for m in d.logs if f"dvr: {DEAD} back" in m])      # each one ended by a check


def test_a_hub_outage_parks_nothing(tmp_path):
    # (review) the hub's own network or DNS down (every camera failing): nothing parks however long; once the
    # others record again, a camera still dead parks after its own hour
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    up.down.update({DEAD: "timeout", OK: "timeout"})
    run(d, f, clock, 3 * 3600, dt=30.0)
    assert d.parked == {} and parked_lines(d) == []
    up.down.pop(OK)
    until_parked(d, f, clock, limit=(dvr_mod.PARK_MIN_FAILS + 2) * SESSION_RETRY_S)   # its own tries from there
    assert OK not in d.parked


def test_a_day_parked_tries_a_real_recording_round(tmp_path):
    # (review) the probe and the recording could judge a stream differently: once a day a real round, not the probe
    r, d, f, clock, up = park(tmp_path)
    up.down[DEAD] = "probe"                                                        # the probe fails, a round would record
    run(d, f, clock, PARK_ROUND_S - 3600, dt=60.0)
    assert DEAD in d.parked
    run(d, f, clock, 3600 + PARK_CHECK_S + 60, dt=60.0)
    assert DEAD not in d.parked and any(f"dvr: {DEAD} back (recorded)" in m for m in d.logs)
    assert d.status(DEAD)["segments"] > 0


def test_the_check_runs_on_the_pool_when_started(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    r, d, f, clock, up = park(tmp_path)
    up.down.pop(DEAD)
    clock.t += SESSION_RETRY_S + 1
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
    clock.t += SESSION_RETRY_S + 2
    r._probe[DEAD] = {"at": clock.t - 1, "ok": False}                              # roads' sweep looked: still down
    d.tick()
    assert up.asked == [] and d.parked[DEAD]["next"] == clock.t + 600              # no second request to the provider
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
    vids = [c["id"] for c in r.merged("cameras") if c.get("video") and c["id"] not in (DEAD, OK)][:1]
    d.always = (DEAD,)
    d.pins = {OK, vids[0]}
    first = sorted([OK, vids[0]])
    assert list(d.wanted()) == [DEAD, first[0]]                                    # the cap: the second kept one waits
    up.down[DEAD] = "timeout"
    until_parked(d, f, clock)
    run(d, f, clock, 15)
    assert list(d.wanted()) == first and d.status(first[1])["recording"]
    assert d.panel()["recording"] == 2 and [p["id"] for p in d.panel()["parked"]] == [DEAD]
    up.down.pop(DEAD)
    run(d, f, clock, SESSION_RETRY_S + 30)
    assert list(d.wanted()) == [DEAD, first[0]] and not d.status(first[1])["recording"]


def test_errwatch_is_quiet_for_a_parked_camera(tmp_path):
    r, d, f, clock, up = park(tmp_path, cid=GONE)
    n = len(d.logs)
    run(d, f, clock, 6 * 3600, dt=60.0)
    w = errwatch.ErrorWatch(clock=lambda: clock.t, log=lambda m: None)
    for m in d.logs[n:] + parked_lines(d):
        w.feed(m + "\n")
    w.drain()
    assert d.logs[n:] == [] and w.total == 0


def test_durations_are_on_the_monotonic_clock(tmp_path):
    # (review) the wall clock stepping (NTP, a VM resumed) neither brings a check forward nor parks a camera early
    r, d, f, clock, up = park(tmp_path)
    mono = {"t": clock.t}
    d.mono = lambda: mono["t"]
    up.asked.clear()
    clock.t += 6 * 3600                                                            # the wall clock jumps; no time passed
    d.tick()
    assert up.asked == [] and DEAD in d.parked
    mono["t"] += SESSION_RETRY_S + 1
    d.tick()
    assert len(checks(up)) == 1


def test_parked_survives_a_restart_and_only_the_leader_writes_it(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    p = dict(d.parked[DEAD])
    saved = json.loads((tmp_path / "dvr" / PARKED_FILE).read_text())
    assert saved[DEAD]["why"] == "no answer" and "due" not in saved[DEAD] and not list((tmp_path / "dvr").glob("*.tmp"))
    # the next task (a deploy, a restart): still parked, no hour of learning, its check when it was due
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=d.logs.append, free_floor=0)
    again.mono = lambda: clock.t + 1e6                                             # (another task's monotonic clock)
    again.always = (DEAD,)
    assert {k: again.parked[DEAD][k] for k in ("why", "since", "at", "next")} == {k: p[k] for k in ("why", "since", "at", "next")}
    assert again.wanted() == {}
    up.asked.clear()
    again.tick()
    assert up.asked == [] and again.parked[DEAD]["next"] == p["next"]
    # a task that isn't the leader (shared storage: the lease) never writes it
    leader = {"on": False}
    standby = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0, may_write=lambda: leader["on"])
    standby._unpark(DEAD, "answered")
    assert DEAD in json.loads((tmp_path / "dvr" / PARKED_FILE).read_text())
    leader["on"] = True
    standby._save_parked()
    assert json.loads((tmp_path / "dvr" / PARKED_FILE).read_text()) == {}


def test_a_check_long_overdue_or_too_far_off_after_a_restart_is_brought_in(tmp_path):
    r, d, f, clock = setup(tmp_path)
    (tmp_path / "dvr").mkdir(exist_ok=True)
    (tmp_path / "dvr" / PARKED_FILE).write_text(json.dumps({
        DEAD: {"why": "no answer", "since": clock.t - 9e5, "at": clock.t - 8e5, "checked": clock.t - 7e5, "next": clock.t - 6e5},
        GONE: {"why": "HTTP 404", "since": clock.t - 9e5, "at": clock.t - 8e5, "checked": clock.t, "next": clock.t + 9e9}}))
    d = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0)
    d.mono = lambda: clock.t
    d.pins = {OK}
    d.tick()
    assert d.parked[DEAD]["next"] == clock.t + dvr_mod.PARK_SOON_S                 # overdue: soon, not at once
    assert d.parked[GONE]["next"] == clock.t + PARK_CHECK_S                        # never further off than 30 min


def test_a_parked_camera_follows_its_new_id_and_one_no_longer_listed_goes(tmp_path, monkeypatch):
    r, d, f, clock, up = park(tmp_path)
    other = [c["id"] for c in r.merged("cameras") if c.get("video") and c["id"] not in (DEAD, OK)][0]
    monkeypatch.setattr(r, "alias", lambda cid: other if cid == DEAD else cid)     # (#320) its records merged
    d.tick()
    assert DEAD not in d.parked and other in d.parked
    assert json.loads((tmp_path / "dvr" / PARKED_FILE).read_text()).keys() == {other}
    real = r.camera
    monkeypatch.setattr(r, "camera", lambda cid: None if cid == other else real(cid))   # gone from the list
    d.tick()
    assert d.parked == {} and d.panel()["parked"] == []


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
    assert dv["max"] == d.max_cameras and dv["check_min"] == PARK_CHECK_S / 60 and dv["first_check_min"] == 5
    (p,) = dv["parked"]
    assert p["id"] == DEAD and p["why"] == "no answer" and p["next"] == p["checked"] + SESSION_RETRY_S
    assert "due" not in p and json.dumps(dv)
    h.h.dvr = lambda: 1 / 0
    assert h.h.panel()["dvr"] == {"error": "ZeroDivisionError"}                    # never in the way of the rest
    js = (Path(dvr_mod.__file__).parent / "web" / "static" / "health.js").read_text()
    assert '"Camera DVR"' in js and "since ${day(p.since)} · next check ${hm(p.next)}" in js
    assert "serverNow() - t > 86400" in js                                         # over a day ago: with its date
    ml = (Path(dvr_mod.__file__).parent / "web" / "static" / "maplayers.js").read_text()
    assert 'd.parked ? "Parked"' in ml


def test_the_check_uses_the_recordings_kept_alive_fetch_with_its_short_timeout(tmp_path, monkeypatch):
    from sizeup import httpkeep
    from sizeup.roads import http_get
    r, d, f, clock, up = park(tmp_path)
    asked = []

    def keep(url, timeout, limit, headers=None):
        asked.append((url, timeout))
        raise URLError(TimeoutError("timed out"))
    monkeypatch.setattr(httpkeep, "get", keep)
    r.fetch = http_get                                                             # the hub's own client: kept alive
    clock.t += SESSION_RETRY_S + 1
    d.tick()
    master = (r.camera(DEAD)["video"], dvr_mod.PROBE_TIMEOUT_S)
    assert master in asked and all(t == dvr_mod.PROBE_TIMEOUT_S for u, t in asked if "TA_057" in u) and DEAD in d.parked


def test_a_check_that_never_answers_leaves_no_connection_in_the_pool(tmp_path, monkeypatch):
    import socket
    import threading
    from sizeup import httpkeep
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    held = []

    def accept():
        try:
            while True:
                held.append(srv.accept())
        except OSError:
            return
    threading.Thread(target=accept, daemon=True).start()
    pool = httpkeep.Pool()
    monkeypatch.setattr(httpkeep, "POOL", pool)
    monkeypatch.setattr(dvr_mod, "PROBE_TIMEOUT_S", 0.2)
    r, d, f, clock, up = park(tmp_path)
    from sizeup.roads import http_get
    r.fetch = http_get
    cam = dict(r.camera(DEAD), video=f"http://127.0.0.1:{srv.getsockname()[1]}/rtplive/TA_057/playlist.m3u8")
    try:
        assert d._check(DEAD, cam) is False                                        # connects, never answers
        assert all(not v for v in pool._idle.values())                             # nothing dead kept for the next one
        assert DEAD in d.parked and d.logs[-1].startswith(f"dvr: {DEAD} parked")   # and still quiet
    finally:
        srv.close()
        for c, _ in held:
            c.close()


def test_back_from_parked_takes_its_slot_on_the_next_tick_with_an_open_event(tmp_path):
    r, d, f, clock, up = rig(tmp_path, max_cameras=2, near_m=5000)
    cam = r.camera(DEAD)
    d.incidents = lambda: [{"lat": cam["lat"] + 0.001, "lon": cam["lon"], "address": ""}]   # its incident cameras cached
    d.always = (OK,)
    assert list(d.wanted())[:2] == [OK, DEAD]
    up.down[DEAD] = "timeout"
    until_parked(d, f, clock)
    assert DEAD in d.parked and DEAD not in d.wanted()                             # excluded after the cached incident set
    other = list(d.wanted())
    assert len(other) == 2 and DEAD not in other                                   # the slot: the next one near it
    up.down.pop(DEAD)
    clock.t += SESSION_RETRY_S + 1
    d.tick()                                                                       # the check: back
    assert DEAD not in d.parked
    f.advance()
    clock.t += 4
    d.tick()
    assert list(d._wanted) == [OK, DEAD] and d.status(DEAD)["recording"]


def test_an_outage_does_not_count_toward_a_cameras_hour(tmp_path):
    # (review) 2 h with every camera failing (the hub's network): the streak starts again at each failure, so a camera
    # still restarting a few minutes after the others are back is not parked at once as "no answer for 123 min"
    r, d, f, clock, up = rig(tmp_path)
    d.pins = {DEAD, OK}
    up.down.update({DEAD: "timeout", OK: "timeout"})
    run(d, f, clock, 2 * 3600, dt=30.0)
    up.down.pop(OK)
    run(d, f, clock, 15 * 60)                                                      # the others back; it still fails
    assert DEAD not in d.parked and parked_lines(d) == []
    assert d._cams[DEAD]["streak"] <= 4
    up.down.pop(DEAD)
    run(d, f, clock, SESSION_RETRY_S + 30)
    assert d.status(DEAD)["recording"] and d._cams[DEAD]["fails"] == 0 and "streak" not in d._cams[DEAD]
    up.down[DEAD] = "timeout"                                                      # dead for real now: its own hour
    until_parked(d, f, clock)
    (line,) = parked_lines(d)
    assert "for 6" in line and "(2" in line                                        # ~1 h, ~20 tries: its own


def test_a_trial_round_that_fails_says_nothing(tmp_path):
    r, d, f, clock, up = park(tmp_path)
    up.down[DEAD] = "ts"                                                           # chunklists fine, segments time out
    n = len(d.logs)
    assert d._check(DEAD, r.camera(DEAD), trial=True) is False
    assert DEAD in d.parked and d.logs[n:] == []
