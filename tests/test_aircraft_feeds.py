"""The aircraft sources' health (#535: "we keep having issues with the plane data").

What the hub's log showed: adsb.lol's TLS handshakes timing out (~230 a day, up to 69 an hour) and 429s with
Retry-After 1-7 s; adsb.fi never failing; the FAA registry refused (403 from its bot protection) every hour. Each
adsb.lol timeout held the one aircraft thread 10 s, so adsb.fi's answers, our receiver and the map waited too.

Proven here against fake servers on this machine (no real host is ever reached: conftest.py): a hanging TLS
handshake gives up after CONNECT_S and never holds up the other feed or the map; a slow answer after READ_S; one
connection kept open between reads; a 429's Retry-After honoured and longer while it keeps refusing; a feed that
failed lately is not the primary; [aircraft] primary; the FAA registry refused: once a day, one log line, the copy
we have kept, a file or the CLI as the way round; Settings > Station > Health's rows. Every aircraft in these
fixtures is fictional (tests/test_aircraft.py)."""

import gzip
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

from sizeup import aircraft as A
from sizeup import datasources as ds
from sizeup.aircraft import AircraftLayer, KeepAlive, Registry
from tests.test_aircraft import FAA, FI3, FI_URL, LOL3, LOL_URL, T3, Clock, Feeds, lol_without

POINT = "/v2/point/{lat}/{lon}/{radius}"


class Fake:
    """An HTTP/1.1 server (keep-alive) answering each request with the next of `plan` (the last repeats):
    bytes -> 200 JSON; ("gzip", bytes); (429, retry_after or None); (403,); ("sleep", s, bytes); ("close", bytes)."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.hits: list[str] = []
        self.conns = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def setup(self):
                super().setup()
                outer.conns += 1

            def do_GET(self):
                outer.hits.append(self.path)
                a = outer.plan.pop(0) if len(outer.plan) > 1 else outer.plan[0]
                code, hdrs, body = 200, {"Content-Type": "application/json"}, b""
                if isinstance(a, bytes):
                    body = a
                elif a[0] == "gzip":
                    body, hdrs["Content-Encoding"] = gzip.compress(a[1]), "gzip"
                elif a[0] == "sleep":
                    time.sleep(a[1])
                    body = a[2]
                elif a[0] == "close":
                    body, hdrs["Connection"] = a[1], "close"
                else:
                    code, body = a[0], b'{"error":"no"}'
                    if code == 429 and a[1]:
                        hdrs["Retry-After"] = str(a[1])
                self.send_response(code)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                if hdrs.get("Connection") == "close":
                    self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path=POINT):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Hang:
    """Accepts TCP connections and never says a word: a TLS handshake to it never completes (what adsb.lol did to
    the hub: "_ssl.c:1067: The handshake operation timed out")."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(c)

    def url(self, path=POINT):
        return f"https://127.0.0.1:{self.port}{path}"

    def close(self):
        self._stop = True
        for c in self.held:
            c.close()
        self.sock.close()


@pytest.fixture
def servers():
    made = []

    def make(kind, *a):
        s = kind(*a)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


# ---- the client: short connect / TLS and read timeouts, one connection kept ----------------------------------------------

def test_a_tls_handshake_that_hangs_gives_up_after_connect_s(servers, monkeypatch):
    monkeypatch.setattr(A, "CONNECT_S", 0.5)
    hang = servers(Hang)
    t0 = time.monotonic()
    with pytest.raises(URLError) as e:
        KeepAlive()(hang.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)
    assert time.monotonic() - t0 < 2.0 and str(e.value.reason).startswith("connect")


def test_a_slow_answer_gives_up_after_read_s(servers, monkeypatch):
    monkeypatch.setattr(A, "READ_S", 0.5)
    fake = servers(Fake, [("sleep", 2.0, LOL3)])
    t0 = time.monotonic()
    with pytest.raises(URLError) as e:
        KeepAlive()(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)
    assert time.monotonic() - t0 < 1.8 and str(e.value.reason).startswith("read")


def test_one_connection_is_kept_and_opened_again_when_closed(servers):
    fake = servers(Fake, [LOL3, LOL3, ("close", LOL3), LOL3, LOL3])
    k = KeepAlive()
    for _ in range(2):
        code, _h, body = k(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)
        assert code == 200 and body == LOL3
    assert k.opened == 1 and fake.conns == 1                    # one TLS handshake, not one per read
    k(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)            # the server says Connection: close
    k(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)
    assert k.opened == 2
    k.conn.sock.close()                                         # gone under us between reads (idle): once more, quietly
    code, _h, _b = k(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)
    assert code == 200 and k.opened == 3


def test_a_gzipped_answer_is_read_and_an_oversized_one_refused(servers):
    fake = servers(Fake, [("gzip", LOL3)])
    k = KeepAlive()
    assert k(fake.url("/x"), A.TIMEOUT_S, A.MAX_FEED_BYTES)[2] == LOL3
    with pytest.raises(ValueError):
        k(fake.url("/x"), A.TIMEOUT_S, 1000)


# ---- the layer: one feed hanging never holds up the other or the map ----------------------------------------------------

def test_one_feed_hanging_never_holds_up_the_other_feed_or_the_map(servers, monkeypatch):
    monkeypatch.setattr(A, "CONNECT_S", 3.0)
    hang, fi = servers(Hang), servers(Fake, [FI3])
    logs: list[str] = []
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=hang.url(), adsbfi_url=fi.url("/lat/{lat}/lon/{lon}/dist/{radius}"),
                       registry=False, watch=False, log=logs.append).start()
    try:
        t0 = time.monotonic()
        ly.ask()                                                 # someone opens the map: waits for the read it wakes
        d = ly.public()
        took = time.monotonic() - t0
        lol, fis = ly.feeds
        assert d["fetched"] and took < 2.0                       # adsb.fi's answer, while adsb.lol still hangs
        assert lol.st["busy"] and lol.st["ok"] is None and fis.st["ok"] is not None
        assert {a["src"] for a in d["aircraft"]} == {"adsbfi"} and d["aircraft"]
        deadline = time.monotonic() + 6
        while lol.st["busy"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert lol.st["rest_until"] > time.time() and "connect" in lol.st["why"]   # it gave up after CONNECT_S: resting
        assert ly.primary() is fis                                # adsb.fi carries on as the primary
        assert any("adsb.lol: URLError: connect" in m and "resting" in m for m in logs)
    finally:
        ly.stop()


def test_the_layer_never_says_no_data_while_a_read_is_in_flight(servers, monkeypatch):
    monkeypatch.setattr(A, "CONNECT_S", 1.5)
    hang = servers(Hang)
    ly = AircraftLayer(feeds=("adsblol",), adsblol_url=hang.url(), registry=False, watch=False, log=lambda m: None)
    ly.asked = time.time()
    ly.tick(parallel=True)
    assert ly.feeds[0].st["busy"] and ly.st["error"] == ""      # not "no feed answering" before it has answered
    deadline = time.monotonic() + 4
    while ly.feeds[0].st["busy"] and time.monotonic() < deadline:
        time.sleep(0.05)
    ly.tick(parallel=True)
    assert "no feed answering" in ly.st["error"] and "connect" in ly.st["error"]


# ---- 429: Retry-After honoured, longer while it keeps refusing -------------------------------------------------------------

def test_a_429s_retry_after_is_honoured_and_lengthened_while_it_goes_on(servers):
    fake = servers(Fake, [(429, 7), (429, 3), (429, None), LOL3])
    clock = Clock(T3 + 0.5)
    ly = AircraftLayer(feeds=("adsblol",), adsblol_url=fake.url(), registry=False, clock=clock, log=lambda m: None)
    f = ly.feeds[0]
    waits = []
    for _ in range(3):
        assert ly._read(f, clock.t, 5.0) is False
        waits.append(f.st["rest_until"] - clock.t)
        clock.t = f.st["rest_until"]
    assert waits == [7, 10, 20]                         # its 7 s; then 3 s asked but 2nd refusal: 10; then 20
    assert ly._read(f, clock.t, 5.0) is True and f.st["fails"] == 0 and f.st["failures"] == 3
    assert len(fake.hits) == 4                          # nothing asked while resting


# ---- the primary: the steady feed, or [aircraft] primary ---------------------------------------------------------------------

def test_a_feed_that_failed_lately_is_not_the_primary_though_it_gives_more():
    clock = Clock(T3 + 0.5)
    few = lol_without({"abedb1", "a4fe82", "a12add"})               # adsb.fi with 7, adsb.lol with 10
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL, registry=False,
                       fetch=Feeds(lol=(0, LOL3), fi=(few,)), clock=clock, log=lambda m: None)
    ly.refresh()                                                     # adsb.lol times out
    clock.t += 11
    ly.refresh()                                                     # both answer: adsb.lol has more
    assert ly.feeds[0].st["score"] > ly.feeds[1].st["score"]
    assert ly.primary().key == "adsbfi"                              # but it failed 11 s ago
    clock.t += A.FLAKY_S
    assert ly.primary().key == "adsblol"                             # steady for FLAKY_S: the busier again
    pinned = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL, registry=False,
                           fetch=Feeds(lol=(LOL3,), fi=(few,)), clock=Clock(T3 + 0.5), log=lambda m: None,
                           primary="adsbfi")
    pinned.refresh()
    assert pinned.primary().key == "adsbfi"                          # [aircraft] primary, though it gives fewer
    pinned.feeds[1].st["rest_until"] = pinned.clock() + 60
    assert pinned.primary().key == "adsblol"                         # ... unless it is resting


# ---- the FAA registry refused: once a day, one line, the copy kept --------------------------------------------------------

def test_the_faa_registry_refused_tries_once_a_day_logs_once_and_keeps_its_copy(servers, tmp_path):
    fake = servers(Fake, [(403,)])
    url = fake.url("/database/ReleasableAircraft.zip")
    A.build_registry_file(FAA, tmp_path / "faa_registry.sqlite", at=T3 - 8 * 86400)     # last good copy, 8 days old
    clock, logs = Clock(T3), []
    reg = Registry(url, tmp_path, A.http_get, logs.append, clock)
    assert reg.due() and reg.lookup("a2002f")["n"] == "N228QZ"
    assert reg.refresh() is False
    assert reg.st["retry_at"] == clock.t + A.REGISTRY_REFUSED_S and reg.st["next"] == clock.t + A.REGISTRY_REFUSED_S
    assert len(logs) == 1 and "Forbidden" in logs[0] and "using the copy of" in logs[0] and "next try in 24 h" in logs[0]
    assert "refused" in logs[0]
    assert reg.lookup("a2002f")["n"] == "N228QZ"                     # still used
    clock.t += 3600
    reg.refresh()                                                     # by hand within the day: refused again, not logged
    assert len(logs) == 1 and len(fake.hits) == 2
    clock.t += A.REGISTRY_LOG_S
    reg.refresh()
    assert len(logs) == 2                                             # a day on: said again
    h = reg.health()
    assert h["state"] == "error" and "copy we have" in h["why"] and "next try" in h["title"]


def test_the_scheduler_waits_for_the_registrys_own_retry_time(tmp_path):
    from tests.test_datasources import registry
    fetch = Feeds()
    fetch_403 = lambda url, t, lim, h=None: (403, {}, b"")         # noqa: E731
    clock = Clock(T3)
    ly = AircraftLayer(feeds=("adsblol",), adsblol_url=LOL_URL, registry_url="https://faa.example/R.zip",
                       fetch=lambda u, t, lim, h=None: fetch_403(u, t, lim, h) if u.endswith(".zip") else fetch(u, t, lim, h),
                       clock=clock, cache_dir=tmp_path, log=lambda m: None)
    r = registry(clock=clock)
    for s in ds.aircraft_sources(ly):
        r.register(s)
    r.started = clock.t
    assert "aircraft:registry" in r.due()
    r.tick()
    assert ly.registry.st["error"] and r.next_due("aircraft:registry") == clock.t + A.REGISTRY_REFUSED_S
    clock.t += 6 * 3600                                               # the scheduler's own back-off would be 5 min
    assert "aircraft:registry" not in r.due()


def test_the_registry_from_a_zip_downloaded_by_hand_and_the_cli(tmp_path, capsys):
    z = tmp_path / "ReleasableAircraft.zip"
    z.write_bytes(FAA)
    reg = Registry(str(z), tmp_path / "state", None, lambda m: None, Clock(T3))
    assert reg.refresh() and reg.lookup("a2002f")["n"] == "N228QZ"
    out = tmp_path / "built" / "faa_registry.sqlite"
    assert A.main(["build-registry", str(z), str(out)]) == 0 and "US aircraft" in capsys.readouterr().out
    assert Registry("", out.parent).lookup("a2002f")["n"] == "N228QZ"      # what the hub restores (cachesync) opens
    assert A.main(["nonsense"]) == 2


# ---- Settings > Station > Health -----------------------------------------------------------------------------------------

def test_each_source_has_a_health_row_terse_with_details_on_hover():
    clock = Clock(T3 + 0.5)
    ly = AircraftLayer(feeds=("adsblol", "adsbfi"), adsblol_url=LOL_URL, adsbfi_url=FI_URL, registry_url="https://faa.example/R.zip",
                       fetch=Feeds(lol=(0,)), clock=clock, log=lambda m: None, station_feed=True)
    ly.refresh()
    rows = {r["key"]: r for r in ly.feed_health()}
    assert list(rows) == ["aircraft:receiver", "aircraft:adsblol", "aircraft:adsbfi", "aircraft:registry"]
    assert rows["aircraft:receiver"]["state"] == "no data" and rows["aircraft:receiver"]["fallback"] == "adsb.lol, adsb.fi"
    lol, fi = rows["aircraft:adsblol"], rows["aircraft:adsbfi"]
    assert lol["state"] == "resting" and "timed out" in lol["why"] and lol["at"] is None
    assert "1 failure(s) in a row" in lol["title"] and "resting until" in lol["title"] and "not primary" in lol["title"]
    assert fi["state"] == "ok" and fi["at"] == clock.t and fi["why"] == "" and fi["title"].startswith("primary")
    assert rows["aircraft:registry"]["state"] == "starting"
    sf = ly.station_feed()
    clock.t += 6                                                     # (its own retry after "nothing yet": 5 s)
    sf.ingest({"epoch": "e", "seq": 0, "full": True, "ac": [], "gone": []})
    ly.refresh()
    assert {r["key"]: r for r in ly.feed_health()}["aircraft:receiver"]["state"] == "ok"
    for r in ly.feed_health():
        assert len(r["label"]) <= 32 and len(r.get("why") or "") <= 200            # terse; the rest in "title"
    assert json.dumps(ly.feed_health())


def test_the_hubs_health_panel_lists_the_aircraft_sources(tmp_path):
    from tests.test_cloud import HubRun, hub_cfg
    h = HubRun(hub_cfg(tmp_path, aircraft_adsblol_url="https://lol.example" + POINT, aircraft_registry=False,
                       aircraft_alarms=False))
    try:
        keys = [f["key"] for f in h.app.health.panel()["feeds"]]
        assert "aircraft:receiver" in keys and "aircraft:adsblol" in keys
    finally:
        h.close()
    js = (A.ROOT / "sizeup" / "web" / "static" / "health.js").read_text()
    assert "f.title" in js and "td.title = c[2]" in js and '"resting" ? "hl-warn"' in js
