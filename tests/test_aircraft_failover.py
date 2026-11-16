"""Self-healing between the aircraft API feeds: the primary by a stable preference order with hysteresis, a slow probe
of the others while nobody looks, and "no source answering" said once, only when every source was tried and failed.

Two fake providers on 127.0.0.1 (no real host is ever reached: conftest.py), each answering in adsb.fi / adsb.lol's
JSON at the layer's own clock, switched between answering, 429 with Retry-After, trickling (a byte at a time, never
the whole answer) and 503. The layer's clock is a fake one, so the 10-minute clean window takes no time; the HTTP is
real (KeepAlive, its whole-read deadline shortened). Every aircraft here is invented (hex a0f0xx, callsigns TEST..).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sizeup import aircraft as A
from sizeup.aircraft import AircraftLayer
from tests.test_aircraft import Clock, T3

FI_PATH = "/lat/{lat}/lon/{lon}/dist/{radius}"
LOL_PATH = "/v2/point/{lat}/{lon}/{radius}"


def plane(hex_, cs, lat, lon, alt=3000, squawk="1200", **kw):
    return {"hex": hex_, "flight": cs, "alt_baro": alt, "gs": 120.0, "track": 90.0, "baro_rate": 0, "squawk": squawk,
            "lat": lat, "lon": lon, "seen": 0.2, "seen_pos": 0.2, "category": "A1", **kw}


TRAFFIC = [plane("a0f001", "TEST01", 41.10, -73.80), plane("a0f002", "TEST02", 41.00, -73.62)]
MAYDAY = plane("a0f077", "TEST77", 41.04, -73.686, alt=900, squawk="7700", gs=80.0, track=330.0, baro_rate=-500)


class Provider:
    """One API feed. mode: "ok" | ("429", retry_after) | "trickle" | "503". Answers at clock.t (the layer's)."""

    def __init__(self, clock: Clock):
        self.clock, self.mode, self.planes = clock, "ok", list(TRAFFIC)
        self.hits: list[float] = []                   # the layer's clock at each request
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(outer.clock.t)
                mode = outer.mode
                if mode == "trickle":
                    try:
                        self.send_response(200)
                        self.send_header("Content-Length", "100000")
                        self.end_headers()
                        for _ in range(100):
                            time.sleep(0.3)
                            self.wfile.write(b"{")
                            self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                code, hdrs = 200, {"Content-Type": "application/json"}
                if mode == "ok":
                    body = json.dumps({"now": outer.clock.t * 1000, "ac": outer.planes}).encode()
                else:
                    code, body = (429 if mode[0] == "429" else 503) if isinstance(mode, tuple) else 503, b'{"error":"no"}'
                    if isinstance(mode, tuple) and mode[0] == "429":
                        hdrs["Retry-After"] = str(mode[1])
                self.send_response(code)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(A, "CONNECT_S", 0.5)          # a trickle gives up after 1.5 s of real time
    monkeypatch.setattr(A, "READ_S", 1.0)
    clock = Clock(T3)
    fi, lol = Provider(clock), Provider(clock)
    logs: list[str] = []
    alarms: list[dict] = []
    ly = AircraftLayer(feeds=("adsbfi", "adsblol"), adsbfi_url=fi.url(FI_PATH), adsblol_url=lol.url(LOL_PATH),
                       registry=False, clock=clock, log=logs.append)
    ly.on_emergency = lambda ev: alarms.append(ev) or len(alarms)
    yield ly, clock, fi, lol, logs, alarms
    for p in (fi, lol):
        p.close()
    for f in ly.feeds:
        f.fetch.close()


def step(ly: AircraftLayer, clock: Clock, dt: float, looking: bool = True) -> str:
    """dt on, a viewer asking (or not), one turn of the layer: the primary's key after it ('' for none)."""
    clock.t += dt
    if looking:
        ly.asked = clock.t
    ly.tick()
    p = ly.primary()
    return p.key if p is not None else ""


def changes(seq: list[str]) -> list[str]:
    return [k for i, k in enumerate(seq) if i == 0 or k != seq[i - 1]]


def test_fi_rate_limited_hands_over_to_lol_at_once_and_takes_it_back_once_after_the_clean_window(rig):
    ly, clock, fi, lol, logs, alarms = rig
    seq = [step(ly, clock, 0)]
    assert seq == ["adsbfi"] and len(fi.hits) == 1 and len(lol.hits) == 1
    fi.mode = ("429", 30)
    n = len(lol.hits)
    seq.append(step(ly, clock, 5))                                   # adsb.fi read: 429, Retry-After 30
    failed_at = ly.feeds[0].st["failed_at"]
    assert seq[-1] == "adsblol" and ly.feeds[0].st["rest_until"] == failed_at + 30
    assert len(lol.hits) == n + 1 and lol.hits[-1] == clock.t        # promoted and read in the same turn
    assert any("primary adsb.lol (was adsb.fi: rate limited (HTTP 429), Retry-After 30 s)" in m for m in logs)
    lol.planes = TRAFFIC + [MAYDAY]                                  # a 7700 on the feed that answers
    shown = []
    while clock.t < failed_at + A.FLAKY_S + 60:
        if clock.t >= failed_at + 35:
            fi.mode = "ok"                                           # adsb.fi back (and steady from here on)
            fi.planes = TRAFFIC + [MAYDAY]                           # ... with the same emergency
        seq.append(step(ly, clock, 5))
        shown.append(len(ly.public()["aircraft"]))
        assert all(fi.hits[i + 1] - fi.hits[i] >= 5 for i in range(len(fi.hits) - 1))   # never faster than 1 a 5 s
    assert min(shown) >= 3                                           # the map kept its aircraft throughout
    assert changes(seq) == ["adsbfi", "adsblol", "adsbfi"]           # no flapping
    back = clock.t - 5 * (len(seq) - 1 - seq.index("adsbfi", 2))     # when adsb.fi took it back ...
    assert back >= failed_at + A.FLAKY_S                             # ... only once clean for the window
    assert [a["code"] for a in alarms] == ["7700"] and alarms[0]["src"] == "adsblol"     # once, from the answering feed
    assert any("primary adsb.fi (was adsb.lol: steady again)" in m for m in logs)
    assert not any("no source answering" in m for m in logs)


def test_lol_stalls_while_primary_then_both_down_says_so_once_then_recovers(rig):
    ly, clock, fi, lol, logs, alarms = rig
    fi.mode = ("429", 30)
    step(ly, clock, 0)
    assert ly.primary().key == "adsblol"
    clock.t += 31
    fi.mode = "ok"                                                   # adsb.fi answers again (failed lately: not first)
    step(ly, clock, 0)
    assert ly.feeds[0].st["ok"] == clock.t and ly.primary().key == "adsblol"
    lol.mode = "trickle"                                             # the primary stalls
    t0 = time.monotonic()
    assert step(ly, clock, 5) == "adsblol"                           # cut at the deadline; one failure, a 5 s rest:
    assert time.monotonic() - t0 < 4 and "no whole answer" in ly.feeds[1].st["why"]   # (none clean) it stays
    assert step(ly, clock, 5) == "adsbfi"                            # stalls again: adsb.fi (answering) takes over
    assert not ly.all_down() and ly.public()["aircraft"]
    fi.mode = ("429", 120)                                           # and now adsb.fi refuses too: nothing answers
    downs = []
    for _ in range(8):
        step(ly, clock, 5)
        downs.append(ly.down_since is not None)
    assert downs[-1] and ly.all_down()
    said = [m for m in logs if "no source answering" in m]
    assert len(said) == 1 and "adsb.fi: rate limited" in said[0] and "adsb.lol: URLError" in said[0]   # once
    rows = ly.feed_health()
    assert rows[0]["key"] == "aircraft:down" and rows[0]["label"] == "Aircraft: no source answering"
    assert rows[0]["state"] == "down" and rows[0]["why"] == "" and "adsb.fi: rate limited" in rows[0]["title"]
    assert sum(1 for r in rows if r["state"] == "down") == 1 and "no feed answering" in ly.public()["error"]
    ly.asked, ly.watch = 0.0, False                                  # map closed and alarms off: nothing is asked
    assert ly.feed_health()[0]["state"] == "idle" and "not reading" in ly.feed_health()[0]["title"]
    ly.watch = True
    lol.mode = "ok"                                                # adsb.lol comes back first
    for _ in range(12):
        if step(ly, clock, 5) and ly.down_since is None:
            break
    assert ly.down_since is None and ly.primary().key == "adsblol" and ly.public()["aircraft"]
    assert len([m for m in logs if "answering again after" in m]) == 1
    assert ly.feed_health()[0]["key"] != "aircraft:down" and ly.public()["error"] == ""
    assert alarms == []


def test_an_emergency_on_whichever_feed_answers_fires_once_while_the_primary_moves(rig):
    ly, clock, fi, lol, logs, alarms = rig
    fi.planes = lol.planes = TRAFFIC + [MAYDAY]
    step(ly, clock, 0, looking=False)                                # the map closed: the watch reads on its own
    assert [a["src"] for a in alarms] in (["adsbfi"], ["adsblol"])
    fi.mode = ("429", 20)
    for _ in range(6):
        step(ly, clock, A.WATCH_S, looking=False)
    assert ly.primary().key == "adsblol" and len(alarms) == 1        # still one alarm


def test_with_the_map_closed_a_recovered_feed_is_probed_slowly_and_proven_back(rig):
    ly, clock, fi, lol, logs, alarms = rig
    seq = [step(ly, clock, 0, looking=False)]
    assert seq == ["adsbfi"] and len(lol.hits) == 1                 # the other's first probe
    fi.mode = ("429", 30)
    seq.append(step(ly, clock, A.WATCH_S, looking=False))
    failed_at = ly.feeds[0].st["failed_at"]
    assert seq[-1] == "adsblol"
    fi.mode = "ok"
    fi_n, lol_n = len(fi.hits), len(lol.hits)
    t_start = clock.t
    while clock.t < failed_at + A.FLAKY_S + 2 * A.PROBE_S:
        seq.append(step(ly, clock, A.WATCH_S, looking=False))
    span = clock.t - t_start
    assert changes(seq) == ["adsbfi", "adsblol", "adsbfi"]
    probes = [t for t in fi.hits[fi_n:] if t < failed_at + A.FLAKY_S]
    assert 1 <= len(probes) <= A.FLAKY_S / A.PROBE_S + 1             # adsb.fi probed every PROBE_S, no more
    assert all(b - a >= A.PROBE_S for a, b in zip(probes, probes[1:]))
    assert ly.feeds[0].st["ok"] > failed_at                         # proven back by a probe, not just quiet
    assert len(lol.hits) - lol_n <= span / A.WATCH_S + 2            # modest: the watch's own rate at most


def test_the_preference_order_and_config():
    from sizeup.config import ConfigError, _aircraft
    from pathlib import Path
    d = _aircraft({"aircraft": {"feeds": ["adsblol", "adsbfi"]}}, Path("."), ())
    assert d["aircraft_feeds"] == ("adsblol", "adsbfi") and d["aircraft_prefer"] == ()      # a deployed list keeps working
    assert AircraftLayer(feeds=d["aircraft_feeds"], adsblol_url="https://l.example" + LOL_PATH,
                         adsbfi_url="https://f.example" + FI_PATH, registry=False, log=lambda m: None).order[0] == "adsbfi"
    d = _aircraft({"aircraft": {"prefer": ["adsblol", "adsbfi"]}}, Path("."), ())
    assert d["aircraft_prefer"] == ("adsblol", "adsbfi")
    ly = AircraftLayer(feeds=("adsbfi", "adsblol"), prefer=d["aircraft_prefer"], adsblol_url="https://l.example" + LOL_PATH,
                       adsbfi_url="https://f.example" + FI_PATH, registry=False, log=lambda m: None)
    assert ly.primary().key == "adsblol" and [f.key for f in ly.ordered()] == ["adsblol", "adsbfi"]
    d = _aircraft({"aircraft": {"feeds": ["adsbfi"]}}, Path("."), ())                    # one provider off
    assert d["aircraft_feeds"] == ("adsbfi",)
    for bad in (["opensky"], ["adsbfi", "adsbfi"], "adsbfi", [1]):
        with pytest.raises(ConfigError):
            _aircraft({"aircraft": {"prefer": bad}}, Path("."), ())


def test_health_js_draws_the_down_row_as_bad():
    js = (A.ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
    assert 's === "down" ? "hl-bad"' in js or '|| s === "down"' in js


# ---- #573 review -----------------------------------------------------------------------------------------------------------

def test_a_feed_promoted_just_after_a_read_waits_out_the_providers_minimum_gap(rig, monkeypatch):
    """adsb.lol read as the other feed, then adsb.fi (the primary) refused in the same turn: adsb.lol is promoted but
    not asked again until MIN_GAP_S after its last request (adsb.fi's limit is 1 a second). PRIMARY_S made 0.2 s:
    the gap alone holds it (normally its fresh answer makes it due only PRIMARY_S after that read)."""
    _ly, clock, fi, lol, logs, _alarms = rig
    monkeypatch.setattr(A, "PRIMARY_S", 0.2)
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsbfi_url=fi.url(FI_PATH), adsblol_url=lol.url(LOL_PATH),
                       registry=False, clock=clock, log=logs.append)       # adsb.lol read first in each pass
    step(ly, clock, 0)
    fi.mode = ("429", 30)
    n = len(lol.hits)
    assert step(ly, clock, A.SECONDARY_S) == "adsblol"                     # both due: adsb.lol read, adsb.fi refused
    assert len(lol.hits) == n + 1                                         # promoted, but not asked a second time
    assert step(ly, clock, 0.5) == "adsblol" and len(lol.hits) == n + 1   # still within the gap
    step(ly, clock, 0.5)
    assert len(lol.hits) == n + 2                                         # the gap out: read as the primary
    assert all(b - a >= A.MIN_GAP_S for a, b in zip(lol.hits, lol.hits[1:]))
    for f in ly.feeds:
        f.fetch.close()


class RealClock:
    @property
    def t(self) -> float:
        return time.time()


def test_on_the_layers_thread_no_provider_is_asked_twice_within_the_gap(monkeypatch):
    """Real time, each feed on its own thread: adsb.fi refuses at once, adsb.lol (just read) is promoted; its next
    request comes no sooner than MIN_GAP_S after the last (it came 0.15 s after, before the fix). PRIMARY_S is made
    0.2 s here so that the gap alone holds the requests apart."""
    monkeypatch.setattr(A, "PRIMARY_S", 0.2)
    clock = RealClock()
    fi, lol = Provider(clock), Provider(clock)
    fi.mode = ("429", 30)
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsbfi_url=fi.url(FI_PATH), adsblol_url=lol.url(LOL_PATH),
                       registry=False, log=lambda m: None, watch=False)
    try:
        ly.asked = time.time() + 30                                       # someone looking throughout
        ly.start()
        time.sleep(3.5)
        assert ly.primary().key == "adsblol" and len(lol.hits) >= 3
        assert all(b - a >= A.MIN_GAP_S - 0.05 for a, b in zip(lol.hits, lol.hits[1:])), lol.hits
        assert len(fi.hits) == 1                                          # resting its Retry-After
    finally:
        ly.stop()
        for p in (fi, lol):
            p.close()


def test_both_feeds_failing_now_and_then_for_an_hour_keeps_the_primary_and_the_log_quiet():
    """A hub network wobble: each feed fails every 7th request, for an hour, someone looking throughout. None is
    clean, so the primary stays through each feed's short rests: a handful of changes, the log rate-limited, and no
    more requests than the schedule's."""
    from tests.test_aircraft import FI3, FI_URL, LOL3, LOL_URL
    from urllib.error import URLError
    clock = Clock(T3)
    n = {"lol.example": 0, "fi.example": 0}

    def fetch(url, timeout, limit, headers=None):
        host = url.split("/")[2]
        n[host] += 1
        if n[host] % 7 == 0:
            raise URLError("timed out")
        return 200, {}, LOL3 if host == "lol.example" else FI3

    logs: list[str] = []
    ly = AircraftLayer(feeds=("adsbfi", "adsblol"), adsbfi_url=FI_URL, adsblol_url=LOL_URL, registry=False,
                       fetch=fetch, clock=clock, log=logs.append, watch=False)
    seq = []
    for _ in range(3600):
        seq.append(step(ly, clock, 1.0))
    switches = len(changes(seq)) - 1
    said = [m for m in logs if m.startswith("aircraft: primary ")]
    print(f"wobble: {switches} primary change(s), {len(said)} line(s), requests {n}")
    assert switches <= 4 and len(said) <= 4, (switches, said)
    # the schedule's own rate (the primary every 5 s, the other every 10 s), plus at most the one early retry (5 s
    # back-off) after each failure; no growth from flipping
    base = 3600 / A.PRIMARY_S + 3600 / A.SECONDARY_S
    total = n["fi.example"] + n["lol.example"]
    assert total <= base + total / 7 / 2 and total <= 1.05 * base, n


def test_prefer_naming_a_feed_that_is_off_is_said_once():
    logs: list[str] = []
    ly = AircraftLayer(feeds=("adsbfi",), prefer=("adsblol", "adsbfi"), adsbfi_url="https://f.example" + FI_PATH,
                       registry=False, log=logs.append, watch=False)
    try:
        ly.start()
        ly.start()
        assert sum("prefer names adsblol, not in feeds" in m for m in logs) == 1
    finally:
        ly.stop()
        for f in ly.feeds:
            f.fetch.close()


def test_a_stalling_primary_hands_over_at_once_when_the_other_is_not_clean():
    """The map closed, a 7700 in view, adsb.lol failed a few minutes ago (not clean), and adsb.fi (the primary)
    starts stalling: each read held to its whole-read deadline (12 s), then failing. A slow failure hands over at
    once, so the watch goes no more than ~30 s without an update (it went ~44 s when a stall was kept through one
    more rest, like a quick 429), and the alarm fires once."""
    from urllib.error import URLError
    clock = Clock(T3)
    hits = {"fi.example": [], "lol.example": []}

    def fetch(url, timeout, limit, headers=None):
        host = url.split("/")[2]
        t = clock.t - T3
        hits[host].append(t)
        if host == "fi.example" and t >= 300:
            clock.t += A.CONNECT_S + A.READ_S                   # held to the deadline, then cut
            raise URLError("read: no whole answer within 12 s")
        if host == "lol.example" and 40 <= t < 70:
            raise URLError("connect: refused")                  # adsb.lol: failed lately, so not clean at 300 s
        return 200, {}, json.dumps({"now": clock.t * 1000, "ac": TRAFFIC + [MAYDAY]}).encode()

    alarms: list[float] = []
    ly = AircraftLayer(feeds=("adsbfi", "adsblol"), adsbfi_url="https://fi.example" + FI_PATH,
                       adsblol_url="https://lol.example" + LOL_PATH, registry=False, fetch=fetch, clock=clock,
                       log=lambda m: None)
    ly.on_emergency = lambda ev: alarms.append(clock.t) or len(alarms)
    updates, lol_clean_at_stall = [], None
    while clock.t - T3 < 600:
        if lol_clean_at_stall is None and clock.t - T3 >= 300:
            lol_clean_at_stall = ly.clean(ly.feeds[1])
        seq = ly.st["seq"]
        ly.tick()                                               # nobody looking: the watch alone
        if ly.st["seq"] != seq:
            updates.append(clock.t - T3)
        clock.t += 1.0
    assert lol_clean_at_stall is False                          # (adsb.lol failed at 40-70 s: flaky until ~670 s)
    gaps = [b - a for a, b in zip(updates, updates[1:])]
    assert max(gaps) <= 28, (max(gaps), updates)              # 27 s: the stall, then adsb.lol at once
    assert ly.primary().key == "adsblol" and len(alarms) == 1
    first_lol = min(t for t in hits["lol.example"] if t > 300)
    assert first_lol - 300 <= A.CONNECT_S + A.READ_S + 2         # read right after adsb.fi's first stall
