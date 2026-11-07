"""An incident source's remote reset (#301): an adapter that stopped itself ("failing: login (manual reset needed)") is
reset from the hub's Settings > Health by an admin (a sources_reset job over the station's jobs channel) or on the
station (sizeup-station adapter reset); reset() runs on the adapter's own thread; one without reset() is a no-op."""

from __future__ import annotations

import threading

from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.cloud.adapters import Resets, request_reset
from tests.test_app import wait
from tests.test_cad import make_cad
import json
import urllib.error
import urllib.request

import pytest

from sizeup.cloud import adapters
from tests.test_cloud import HubRun, hub_cfg, station, station_cfg


def reset(url, source, headers=None):
    """POST /api/sources/reset as the Health page does: JSON, X-Sizeup-Action: reset."""
    h = {"Content-Type": "application/json", "X-Sizeup-Action": "reset"} if headers is None else headers
    req = urllib.request.Request(url + "/api/sources/reset", method="POST", data=json.dumps({"source": source}).encode(),
                                 headers=h)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
from tests.test_roles import COREY, JULIAN, VAL, people


class Feed:
    source, label, rank, poll_s = "feed", "Feed", 1, 5.0

    def __init__(self):
        self.resets: list[int] = []         # the thread each reset() ran on

    def reset(self):
        self.resets.append(threading.get_ident())


class NoReset:
    source, label, rank, poll_s = "other", "Other", 2, 5.0


class Runner:
    """#297's SourceRunner as far as a reset goes: runs (with .a), resets, and a loop per adapter that calls
    resets.run_due(adapter) before each poll on that adapter's own thread."""

    def __init__(self, *adapters):
        self.runs = [type("Run", (), {"a": a})() for a in adapters]
        self.resets = Resets()
        self.logs: list[str] = []
        self.threads: dict[str, int] = {}
        self.stop = threading.Event()
        self.wakes = {a.source: threading.Event() for a in adapters}
        self.resets.wake = lambda src: self.wakes[src].set()
        for a in adapters:
            threading.Thread(target=self._loop, args=(a,), daemon=True).start()

    def _loop(self, a):
        self.threads[a.source] = threading.get_ident()
        while not self.stop.is_set():
            self.resets.run_due(a, self.logs.append)
            self.wakes[a.source].wait(0.05)
            self.wakes[a.source].clear()


def test_reset_runs_on_the_adapters_own_thread_and_is_a_noop_without_reset():
    feed, other = Feed(), NoReset()
    r = Runner(feed, other)
    try:
        out = request_reset(r, "feed", "corey@example.org", r.logs.append)
        assert out == {"ok": True, "result": "feed: reset asked (runs before its next poll)"}
        assert wait(lambda: feed.resets, 3)
        assert feed.resets == [r.threads["feed"]] and feed.resets[0] != threading.get_ident()
        assert any("reset (asked by corey@example.org)" in ln for ln in r.logs)
        out = request_reset(r, "other", "x", r.logs.append)
        assert out["ok"] and out["result"] == "other: no reset(): nothing to do"
        assert not r.resets.pending("other")                       # nothing queued for it
        out = request_reset(r, "", "x", r.logs.append)             # all: the one with reset()
        assert "feed: reset asked" in out["result"] and "other: no reset()" in out["result"]
        assert wait(lambda: len(feed.resets) == 2, 3)
        assert request_reset(r, "nope", "x")["ok"] is False
        assert request_reset(r, "Bad Id", "x")["error"] == "source: a source id"
        assert request_reset(None, "feed", "x")["error"] == "no incident sources run on this station"
    finally:
        r.stop.set()
    # an adapter queued for reset with no reset() (the runner's side): said, nothing raised
    res = Resets()
    res.request("other")
    logs = []
    assert res.run_due(NoReset(), logs.append) == "no reset(): nothing to do" and "nothing to do" in logs[0]
    assert res.run_due(NoReset(), logs.append) is None                            # once

    class Broken(Feed):
        def reset(self):
            raise RuntimeError("password=hunter2 rejected")
    res.request("feed")
    assert res.run_due(Broken(), logs.append) == "failed: RuntimeError"          # never takes the thread down
    assert "hunter2" not in "\n".join(logs) and logs[-1].endswith("failed: RuntimeError")   # its type only


def test_the_hub_sends_a_sources_reset_job_and_the_station_runs_it(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    feed = Feed()
    st.sources_runner = Runner(feed)
    try:
        assert wait(lambda: hub.app.hub.connected(), 5)
        # without the header (a form or a page elsewhere): refused before anything
        assert reset(hub.url, "feed", {"Content-Type": "application/json"})[0] == 400
        assert reset(hub.url, "feed", {"Content-Type": "text/plain", "X-Sizeup-Action": "reset"})[0] == 400
        code, body = reset(hub.url, "feed")
        assert code == 200 and body["result"] == "feed: reset asked (runs before its next poll)"
        assert wait(lambda: feed.resets, 3) and feed.resets == [st.sources_runner.threads["feed"]]
        code, body = reset(hub.url, "feed")                     # again within 5 min: no
        assert code == 429 and "one per 5 min" in body["error"] and len(feed.resets) == 1
        for _ in range(2):                                      # a reset that didn't go isn't counted
            code, body = reset(hub.url, "nope")
            assert code == 503 and "no source 'nope'" in body["error"]
        code, body = reset(hub.url, "Bad Id")
        assert code == 400
        # on the station itself (sizeup-station adapter reset): the same, locally
        srv_station = st.station.sources_reset({"source": "feed"})
        assert srv_station["ok"] and wait(lambda: len(feed.resets) == 2, 3)
    finally:
        st.sources_runner.stop.set()
        st.link.stop()
        hub.close()


def test_reset_is_admin_only_and_logs_who_asked(tmp_path):
    assert needs("/api/sources/reset") == "admin"
    api = CadApi(make_cad(tmp_path))
    api.cad.log = lambda m: None
    people(api.cad)
    assert api.permit("/api/sources/reset", Identity(email=COREY)) is None                    # an admin: yes
    for who in (JULIAN, VAL):                                                                 # a dispatcher, a viewer
        code, raw, _ = api.permit("/api/sources/reset", Identity(email=who))
        assert code == 403 and b"Admins only" in raw
    # the hub logs who asked (the Access identity), never more than the source and the answer
    from sizeup.cloud.hub import Hub
    logs, sent = [], []
    h = Hub.__new__(Hub)
    h.log = logs.append
    h.call = lambda kind, args, timeout: sent.append((kind, args)) or {"ok": True, "result": "feed: reset asked"}
    assert h.reset_source("feed", COREY) == {"ok": True, "result": "feed: reset asked"}
    assert sent == [("sources_reset", {"source": "feed", "by": COREY})]
    assert logs[0] == f"incident sources: reset of feed asked by {COREY}"


def test_the_health_panel_has_the_reset_button_only_for_failing_sources():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "health.js").read_text()
    assert 'filter((x) => x.state === "failing")' in js and 'fetch("/api/sources/reset"' in js and "confirm(" in js


def test_the_limit_is_one_per_source_per_5_min_counted_only_when_it_went():
    t = [0.0]
    lim = adapters.ResetLimit(clock=lambda: t[0])
    assert lim.blocked("feed") == 0
    assert lim.blocked("feed") == 0                       # (asked, not done: not counted)
    lim.done("feed")
    assert lim.blocked("feed") == 300 and lim.blocked("other") == 0
    assert lim.blocked("") == 300                         # all: held up by any source's
    t[0] = 299
    assert lim.blocked("feed") > 0
    t[0] = 301
    assert lim.blocked("feed") == 0
    lim.done("", ["feed", "other"])                       # all: every source's bucket
    assert lim.blocked("feed") == 300 and lim.blocked("other") == 300 and lim.blocked("third") == 300
    for i in range(200):                                  # bounded
        lim.done(f"s{i}")
    assert len(lim._at) <= lim.MAX


def test_against_the_real_source_runner():
    """Once #297 (PR #304) is on main: its SourceRunner runs a reset on the adapter's own thread (Resets.run_due)."""
    runner = pytest.importorskip("sizeup.sources.runner")
    if not hasattr(runner.SourceRunner, "__init__") or "resets" not in runner.SourceRunner.__init__.__code__.co_names:
        pytest.skip("#304's SourceRunner has no Resets yet")
    from sizeup.sources import Health
    feed = Feed()
    feed.poll = lambda: []
    feed.health = lambda: Health("ok")
    sent = []
    r = runner.SourceRunner([feed], lambda kind, data: sent.append(kind), log=lambda m: None).start()
    try:
        assert request_reset(r, "feed", "x")["ok"]
        assert wait(lambda: feed.resets, 10) and feed.resets[0] != threading.get_ident()
    finally:
        r.stop()
