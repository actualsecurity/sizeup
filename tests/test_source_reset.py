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
from tests.test_cloud import HubRun, call, hub_cfg, station, station_cfg
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


def test_the_hub_sends_a_sources_reset_job_and_the_station_runs_it(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    feed = Feed()
    st.sources_runner = Runner(feed)
    try:
        assert wait(lambda: hub.app.hub.connected(), 5)
        code, body = call(hub.url + "/api/sources/reset", "POST", {"source": "feed"}, token=None)
        assert code == 200 and body["result"] == "feed: reset asked (runs before its next poll)"
        assert wait(lambda: feed.resets, 3) and feed.resets == [st.sources_runner.threads["feed"]]
        code, body = call(hub.url + "/api/sources/reset", "POST", {"source": "nope"}, token=None)
        assert code == 503 and "no source 'nope'" in body["error"]
        code, body = call(hub.url + "/api/sources/reset", "POST", {"source": "Bad Id"}, token=None)
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
