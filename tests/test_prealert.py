"""The phone pre-alert from Tapout's ntfy feed (sizeup/prealert.py; user, 2026-10-08: "lets receive
that feed as a possible pre-alert of a possible incoming call").

Everything here talks to a local fake ntfy server (FakeNtfy, a threaded HTTP server streaming JSON
lines), never ntfy.sh: the ring topic is the fire crew's real alert channel ("never send to it, we
can read from it though"). conftest.py refuses any other host, and anything but GET, for the module."""

import ast
import csv
import io
import json
import re
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from sizeup import prealert as prealert_mod
from sizeup.app import App, make_app
from sizeup.cad.api import CadApi, needs
from sizeup.config import ConfigError, load
from sizeup.prealert import NtfyStream, PreAlerts, parse_ring, parse_status
from tests.test_cad import make_cad, monitor_incident
from tests.test_cloud import HubRun, base, hub_cfg, station_cfg
from tests.test_roles import CASEY, JORDAN, VAL, call, people

ROOT = Path(__file__).resolve().parent.parent
RING, STATUS = "example-ring-topic", "example-status-topic"
RING_TITLE, RING_TEXT = "STATION PHONE RINGING", "Incoming call on the station line at 2026-10-08 14:03:21 EDT."
HB_TEXT = ("Ring detector still online (hourly check-in, seq 12). 2026-10-08 13:00:00 EDT. Rings since boot: 3. "
           "Free heap 181234 B, largest block 110580 B.")


# -- a fake ntfy ---------------------------------------------------------------------------------------


class FakeNtfy:
    """ntfy's JSON stream, locally: GET /<topic>/json?since=<id | 10m | all> -> "open", the cached
    messages after `since`, then each new one as it is added, a keepalive whenever nothing happens.
    Every request is recorded (method, path, query); anything but GET is refused."""

    def __init__(self, keepalive_s: float = 0.1):
        self.topics: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.cond = threading.Condition()
        self.gen = 0
        self.reject: list[int] = []        # answer the next GETs with these codes (429)
        self.retry_after = ""
        self.keepalive_s = keepalive_s
        self.keepalives = 0
        self.n = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                u = urlsplit(self.path)
                with fake.cond:
                    fake.requests.append((self.command, u.path, parse_qs(u.query)))
                    fake.cond.notify_all()

            def _refuse(self):
                self._record()
                self.send_error(405)

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

            def do_GET(self):
                self._record()
                u = urlsplit(self.path)
                m = re.fullmatch(r"/([-\w]+)/json", u.path)
                if not m:
                    self.send_error(404)
                    return
                with fake.cond:
                    code = fake.reject.pop(0) if fake.reject else None
                if code:
                    self.send_response(code)
                    if fake.retry_after:
                        self.send_header("Retry-After", fake.retry_after)
                    self.end_headers()
                    self.wfile.write(b'{"code":42901,"http":429,"error":"limit reached: too many requests"}\n')
                    return
                topic, since = m.group(1), (parse_qs(u.query).get("since") or ["all"])[0]
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.end_headers()

                def send(obj):
                    self.wfile.write((json.dumps(obj) + "\n").encode())
                    self.wfile.flush()
                try:
                    with fake.cond:
                        gen, msgs = fake.gen, fake.topics.setdefault(topic, [])
                        start, n = fake._after(msgs, since), len(msgs)
                        backlog = msgs[start:n]
                    send({"id": "op3n", "time": int(time.time()), "event": "open", "topic": topic})
                    for x in backlog:
                        send(x)
                    while True:
                        with fake.cond:
                            fake.cond.wait_for(lambda: len(msgs) > n or fake.gen != gen, fake.keepalive_s)
                            if fake.gen != gen:
                                return                       # dropped: the connection closes
                            new, n = msgs[n:], len(msgs)
                        for x in new:
                            send(x)
                        if not new:
                            send({"id": "k33p", "time": int(time.time()), "event": "keepalive", "topic": topic})
                            with fake.cond:
                                fake.keepalives += 1
                except OSError:
                    return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @staticmethod
    def _after(msgs: list[dict], since: str) -> int:
        if since == "all":
            return 0
        for k, x in enumerate(msgs):
            if x["id"] == since:
                return k + 1
        m = re.fullmatch(r"(\d+)m", since)
        if m:
            cut = time.time() - int(m.group(1)) * 60
            return next((k for k, x in enumerate(msgs) if x["time"] >= cut), len(msgs))
        return 0

    def add(self, topic: str, title: str = RING_TITLE, message: str = RING_TEXT, tags=("rotating_light", "fire_engine"),
            priority: int = 5, at: float | None = None) -> str:
        """A message in the fake's cache (what Tapout's POST would leave on the real server)."""
        with self.cond:
            self.n += 1
            mid = f"m{self.n:04d}x"
            self.topics.setdefault(topic, []).append(
                {"id": mid, "time": int(at if at is not None else time.time()), "event": "message", "topic": topic,
                 "title": title, "message": message, "tags": list(tags), "priority": priority})
            self.cond.notify_all()
        return mid

    def drop(self):
        """Every open stream ends (a network blip)."""
        with self.cond:
            self.gen += 1
            self.cond.notify_all()

    def gets(self, topic: str) -> list[dict]:
        with self.cond:
            return [q for m, p, q in self.requests if p == f"/{topic}/json"]

    def close(self):
        self.drop()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ntfy():
    f = FakeNtfy()
    yield f
    f.close()
    assert {m for m, _, _ in f.requests} <= {"GET"}, "the pre-alert sent something other than GET"


@pytest.fixture(autouse=True)
def quick_backoff(monkeypatch):
    monkeypatch.setattr(NtfyStream, "BACKOFF_START", 0.05)
    monkeypatch.setattr(NtfyStream, "BACKOFF_MAX", 0.2)
    monkeypatch.setattr(NtfyStream, "BACKOFF_429", 0.6)


def wait(cond, s=5.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


class Clock:
    def __init__(self, t=None):
        self.t = t or time.time()

    def __call__(self):
        return self.t


def ring_msg(mid="r1", at=None, title=RING_TITLE, text=RING_TEXT, tags=("rotating_light", "fire_engine")):
    return {"id": mid, "time": at or time.time(), "event": "message", "topic": RING, "title": title, "message": text,
            "tags": list(tags), "priority": 5}


def status_msg(mid, at, title="Tapout heartbeat", text=HB_TEXT, tags=("green_heart",), priority=1):
    return {"id": mid, "time": at, "event": "message", "topic": STATUS, "title": title, "message": text,
            "tags": list(tags), "priority": priority}


def offline(cad, **kw) -> PreAlerts:
    """PreAlerts that never connects (messages handed to it directly)."""
    return PreAlerts(cad, "http://127.0.0.1:9", RING, STATUS, log=lambda m: None, **kw)


def chron(cad, iid=None):
    if iid is None:
        return [e["text"] for e in cad.store.q("SELECT text FROM events WHERE kind = 'prealert' ORDER BY id")]
    return [e["text"] for e in cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'prealert'", (iid,))]


# -- what Tapout sends --------------------------------------------------------------------------------------


def test_tapouts_messages_parse():
    r = parse_ring(ring_msg(at=1000))
    assert (r["at"], r["title"], r["message"], r["test"]) == (1000, RING_TITLE, RING_TEXT, False)
    late = parse_ring(ring_msg(at=1000, text="Phone rang ~45s ago at 2026-10-08 14:03:21 EDT (alert delayed by network)."))
    assert late["at"] == 955                                   # when it rang, not when the alert got out
    assert parse_ring(ring_msg(title="[TEST] STATION PHONE RINGING", tags=("test_tube", "rotating_light")))["test"]
    hb = parse_status(status_msg("h1", 2000))
    assert (hb["kind"], hb["seq"], hb["at"]) == ("heartbeat", 12, 2000)
    boot = parse_status(status_msg("b1", 3000, "Tapout restarted", "Reconnected to WiFi. Boot #7, last reset: POWERON, "
                                   "free heap 200000 B.", ("white_check_mark",), 2))
    assert (boot["kind"], boot["seq"]) == ("boot", 7)
    assert parse_status(status_msg("w1", 1, "Detector line STUCK active", "...", ("warning",), 4))["kind"] == "other"


# -- the stream ------------------------------------------------------------------------------------------------


def test_a_ring_is_a_pre_alert_in_the_cad_within_a_second_and_keepalives_are_ignored(tmp_path, ntfy):
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_ring_topic=RING, prealert_status_topic=STATUS, prealert_server=ntfy.url))
    pushed = []
    app.prealert.on_change = lambda: pushed.append(time.time())
    app.prealert.start()
    try:
        assert wait(lambda: len(ntfy.gets(RING)) == 1 and len(ntfy.gets(STATUS)) == 1)
        assert ntfy.gets(RING)[0]["since"] == ["10m"] and ntfy.gets(STATUS)[0]["since"] == ["540m"]
        assert wait(lambda: ntfy.keepalives >= 4)              # keepalives flow ...
        assert app.cad.state()["prealert"]["items"] == [] and pushed == []     # ... and make nothing
        t0 = time.time()
        mid = ntfy.add(RING)
        assert wait(lambda: app.cad.state()["prealert"]["open"], 1.0), "not in the CAD state within a second"
        assert time.time() - t0 < 1.0 and pushed
        p = app.cad.state()["prealert"]["open"][0]
        assert (p["source"], p["title"], p["message"], p["ntfy_id"], p["status"], p["test"]) == (
            "Tapout HQ phone", RING_TITLE, RING_TEXT, mid, "open", False)
        assert app.state()["prealert"]["open"][0]["id"] == p["id"]          # the monitor gets it too
        assert chron(app.cad) == [f"Phone pre-alert (Tapout HQ phone): {RING_TITLE}: {RING_TEXT}"]
        assert app.cad.state()["unit_log"] == []                            # not a unit status change
    finally:
        app.prealert.stop()


def test_a_reconnect_resumes_after_the_last_message_without_duplicates(tmp_path, ntfy):
    cad = make_cad(tmp_path)
    pa = PreAlerts(cad, ntfy.url, RING, STATUS, log=lambda m: None).start()
    try:
        first = ntfy.add(RING)
        assert wait(lambda: len(pa.state()["items"]) == 1)
        ntfy.drop()                                           # a blip: the stream ends
        second = ntfy.add(RING)                               # rings while it is down
        assert wait(lambda: len(ntfy.gets(RING)) >= 2)
        assert ntfy.gets(RING)[1]["since"] == [first]         # resumes right after what it had
        assert wait(lambda: len(pa.state()["items"]) == 2)
        ntfy.drop()
        assert wait(lambda: len(ntfy.gets(RING)) >= 3) and ntfy.gets(RING)[2]["since"] == [second]
        time.sleep(0.2)
        assert sorted(p["ntfy_id"] for p in pa.state()["items"]) == sorted([first, second])
        assert len(chron(cad)) == 2
    finally:
        pa.stop()
    # a restart asks for the last 10 minutes again: the same rings, not new ones
    pa2 = PreAlerts(cad, ntfy.url, RING, STATUS, log=lambda m: None).start()
    try:
        assert wait(lambda: ntfy.gets(RING)[-1]["since"] == ["10m"] and pa2.ring_stream.last_id == second)
        assert len(pa2.state()["items"]) == 2 and len(chron(cad)) == 2
    finally:
        pa2.stop()


def test_old_rings_replayed_after_an_outage_are_on_record_but_not_news(tmp_path, ntfy):
    cad = make_cad(tmp_path)
    ntfy.add(RING, at=time.time() - 30 * 60)                  # older than the first look (10 min): not asked for
    late = ntfy.add(RING, at=time.time() - 7 * 60)            # in the window, but past expire_min
    pa = PreAlerts(cad, ntfy.url, RING, STATUS, log=lambda m: None).start()
    try:
        assert wait(lambda: pa.state()["items"])
        time.sleep(0.2)
        items = pa.state()["items"]
        assert [(p["ntfy_id"], p["status"], p["note"]) for p in items] == [(late, "expired", "received late")]
        assert pa.state()["open"] == []
    finally:
        pa.stop()


def test_a_rate_limit_backs_off_longer(tmp_path, ntfy, monkeypatch):
    ntfy.reject, ntfy.retry_after = [429], ""
    cad = make_cad(tmp_path)
    pa = PreAlerts(cad, ntfy.url, RING, STATUS, log=lambda m: None)
    pa.status_stream.start()                                  # just the one topic
    try:
        assert wait(lambda: len(ntfy.gets(STATUS)) == 1)
        t0 = time.time()
        assert wait(lambda: pa.status_stream.last_error == "HTTP 429 (rate limited)", 1)
        assert wait(lambda: len(ntfy.gets(STATUS)) == 2, 3)
        assert time.time() - t0 >= 0.5                       # BACKOFF_429 (0.6 s here, 2 min for real)
        assert wait(lambda: pa.status_stream.connected)
        assert pa.status_stream.last_error == ""
    finally:
        pa.stop()
    # the delays themselves: doubling to a minute; a 429 at least 2 min, or Retry-After (up to 10)
    monkeypatch.undo()
    s = NtfyStream("http://127.0.0.1:9", RING, lambda m: None)
    delays = []
    for n in range(1, 10):
        s.failures = n
        delays.append(s._delay(OSError("down")))
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    h429 = lambda ra: HTTPError("u", 429, "Too Many Requests", {"Retry-After": ra} if ra else {}, None)
    assert (s._delay(h429("")), s._delay(h429("300")), s._delay(h429("99999"))) == (120, 300, 600)


# -- expiry, matching, dismissing -----------------------------------------------------------------------------


def test_an_unanswered_pre_alert_expires(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad, expire_min=5)
    p = pa.ring(ring_msg(at=clock.t))
    assert pa.ring(ring_msg(at=clock.t)) is None              # the same message again: nothing new
    clock.t += 299
    assert pa.state()["open"][0]["id"] == p["id"]
    clock.t += 2
    st = pa.state()
    assert st["open"] == [] and st["items"][0]["status"] == "expired" and st["items"][0]["outcome_at"] == clock.t
    assert chron(cad)[-1].endswith("expired: no call within 5 min")
    clock.t += 25 * 3600
    assert pa.state()["items"] == []                          # listed a day, kept for the CSV
    assert len(pa.store.q("SELECT * FROM prealerts")) == 1


def test_a_60_control_page_for_port_chester_matches_the_ring(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    cad.prealert = pa = offline(cad)
    p = pa.ring(ring_msg(at=clock.t))
    test = pa.ring(ring_msg("t1", at=clock.t, title="[TEST] STATION PHONE RINGING", tags=("test_tube",)))
    assert test["test"]
    clock.t += 95
    _, inc = monitor_incident()
    q = cad.from_monitor(inc)
    got = pa.get(p["id"])
    assert (got["status"], got["incident_id"], got["outcome_at"]) == ("matched", q["id"], clock.t)
    assert pa.get(test["id"])["status"] == "open"             # a test is never a call
    assert chron(cad, q["id"]) == [f"Phone pre-alert {prealert_mod._hms(p['at'])} matched: Tapout HQ phone rang "
                                   f"95 s before this event (60 Control page)"]
    # numbered later, the CSV says which event
    cad.accept(q["id"], {"type": "ALARM", "subtype": "COMM", "operator": "Lt. Ruiz"})
    num = cad.store.incident(q["id"])["number"]
    rows = list(csv.reader(io.StringIO(cad.export("prealerts.csv").decode("utf-8-sig"))))
    assert rows[0][:7] == ["Rang", "Received", "Source", "Title", "Message", "Test", "Outcome"]
    assert [(r[6], r[8], r[9]) for r in rows[1:]] == [("matched", "95", num), ("open", "", "")]
    assert "PRE-ALERT" in cad.export("event-log.csv").decode("utf-8-sig")


def test_a_new_emergency_event_matches_a_non_emergency_one_does_not(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    cad.prealert = pa = offline(cad)
    p = pa.ring(ring_msg(at=clock.t))
    clock.t += 30
    ne = cad.create({"type": "NOTIF", "address": "Westchester Ave", "operator": "JD"})
    assert not ne["emergency"] and pa.get(p["id"])["status"] == "open"
    inc = cad.create({"type": "STRU", "address": "209 Westchester Ave", "operator": "JD"})
    assert (pa.get(p["id"])["status"], pa.get(p["id"])["incident_id"]) == ("matched", inc["id"])
    assert "30 s before this event (event)" in chron(cad, inc["id"])[0]
    # after the window: no match (it expired first)
    q = pa.ring(ring_msg("r2", at=clock.t))
    clock.t += 6 * 60
    late = cad.create({"type": "STRU", "address": "1 King St", "operator": "JD"})
    assert pa.get(q["id"])["status"] == "expired" and chron(cad, late["id"]) == []


def test_a_ring_just_after_the_page_matches_it_at_once(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    cad.prealert = pa = offline(cad)
    _, inc = monitor_incident()
    q = cad.from_monitor(inc)
    clock.t += 40
    p = pa.ring(ring_msg(at=clock.t))
    assert (p["status"], p["incident_id"]) == ("matched", q["id"])
    assert "40 s after this event" in chron(cad, q["id"])[0]
    clock.t += 1
    assert pa.ring(ring_msg("r2", at=clock.t))["status"] == "open"     # that page is taken


def test_dismiss_is_for_dispatchers_and_admins_and_is_in_their_activity(tmp_path):
    clock = Clock()
    api = CadApi(make_cad(tmp_path, clock=clock))
    people(api.cad)
    api.cad.prealert = pa = offline(api.cad)
    assert needs("/api/cad/prealert/1/dismiss") == "dispatch"
    p = pa.ring(ring_msg(at=clock.t))
    code, out = call(api, f"/api/cad/prealert/{p['id']}/dismiss", {"reason": "wrong number"}, VAL)
    assert code == 403 and pa.get(p["id"])["status"] == "open"
    code, out = call(api, f"/api/cad/prealert/{p['id']}/dismiss", {"reason": "wrong number", "operator": "Casey"}, JORDAN)
    assert code == 200 and (out["status"], out["operator"], out["note"]) == ("dismissed", "Jordan <jordan@example.org>",
                                                                              "wrong number")
    hms = prealert_mod._hms(p["at"])
    assert [(r["kind"], r["summary"]) for r in api.cad.activity.rows(JORDAN)][:1] == [
        ("action", f"Dismissed the phone pre-alert of {hms}: wrong number")]
    assert any(r["kind"] == "refused" and "prealert" in r["route"] for r in api.cad.activity.rows(VAL))
    ev = api.cad.store.one("SELECT * FROM events WHERE kind = 'prealert' ORDER BY id DESC")
    assert (ev["text"], ev["operator"]) == (f"Phone pre-alert {hms} dismissed: wrong number", "Jordan <jordan@example.org>")
    code, out = call(api, f"/api/cad/prealert/{p['id']}/dismiss", {}, CASEY)
    assert code == 400 and "already dismissed" in out["error"]
    assert call(api, "/api/cad/prealert/999/dismiss", {}, CASEY)[0] == 400
    # no pre-alerts here: a 400 that says why
    api.cad.prealert = None
    code, out = call(api, f"/api/cad/prealert/{p['id']}/dismiss", {})
    assert code == 400 and "station" in out["error"]
    assert api.cad.state()["prealert"] is None and api.cad.export("prealerts.csv") is None


# -- Tapout's health ---------------------------------------------------------------------------------------------


def test_tapout_health_from_the_heartbeat(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad, heartbeat_stale_min=90)
    t = pa.state()["tapout"]
    assert (t["state"], t["word"]) == ("unknown", "no heartbeat yet")       # never heard: says so
    pa.status(status_msg("h1", clock.t - 12 * 60))
    t = pa.state()["tapout"]
    assert (t["state"], t["word"]) == ("ok", "ok, heartbeat 12 min ago") and "seq 12" in t["detail"]
    pa.status(status_msg("b1", clock.t - 60, "Tapout restarted", "Reconnected to WiFi. Boot #7, last reset: SW, free heap 1 B.",
                         ("white_check_mark",), 2))
    assert pa.state()["tapout"]["word"] == "ok, restarted 60 s ago"
    pa.status(status_msg("w1", clock.t - 30, "Detector line STUCK active", "D4 low for 10 min.", ("warning",), 4))
    assert "Detector line STUCK active" in pa.state()["tapout"]["detail"]
    clock.t += 2 * 3600
    t = pa.state()["tapout"]
    assert (t["state"], t["word"]) == ("warn", "no heartbeat for 2 h")
    # kept across a restart (the CAD SQLite)
    assert offline(cad, heartbeat_stale_min=90).tapout()["word"] == "no heartbeat for 2 h"


def test_a_6_hour_heartbeat_warns_only_once_one_is_missed(tmp_path):
    # Tapout's heartbeat went from hourly to every 6 h: "no heartbeat for 1.6 h" was a false alarm
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad)                                                       # the defaults: every 6 h, stale after 411 min
    assert (pa.every_s, pa.stale_s) == (360 * 60, 411 * 60)
    pa.status(status_msg("h1", clock.t))
    clock.t += 5 * 3600
    t = pa.state()["tapout"]
    assert (t["state"], t["word"]) == ("ok", "ok, heartbeat 5 h ago")
    clock.t += 3600 + 50 * 60                                               # 6 h 50 min: the next one is late, not missing
    assert pa.state()["tapout"]["state"] == "ok"
    clock.t += 10 * 60                                                      # 7 h: missed
    t = pa.state()["tapout"]
    assert (t["state"], t["word"]) == ("warn", "no heartbeat for 7 h")
    assert offline(cad, heartbeat_every_min=60).tapout()["state"] == "warn"  # hourly still works: stale after 81 min
    assert offline(cad, heartbeat_every_min=60).stale_s == 81 * 60


def test_any_status_message_proves_tapout_is_alive(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad)
    pa.status(status_msg("h1", clock.t - 7 * 3600))
    assert pa.tapout()["word"] == "no heartbeat for 7 h"
    pa.status(status_msg("o1", clock.t - 10 * 60, "OTA update applied", "Now running v2.4.0.", ("arrows_counterclockwise",), 3))
    t = pa.tapout()
    assert (t["state"], t["word"]) == ("ok", "ok, heard 10 min ago")
    assert "OTA update applied" in t["detail"] and "last heartbeat" in t["detail"]
    pa.status(status_msg("w1", clock.t - 60, "Low memory", "Free heap 9000 B.", ("warning",), 4))
    t = pa.tapout()
    assert (t["state"], t["word"]) == ("warn", "Low memory, 60 s ago") and t["detail"].count("Low memory") == 1
    pa.status(status_msg("h2", clock.t - 30))                               # a heartbeat says so by name again
    assert pa.tapout()["word"] == "ok, heartbeat 30 s ago"
    assert offline(cad).tapout()["word"] == "ok, heartbeat 30 s ago"        # kept across a restart


def test_a_device_fault_warns_with_its_title_until_a_heartbeat_or_restart(tmp_path):
    # the firmware's logStatus(..., "high", "warning"): priority 4+; it is alive, but not well
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad)
    pa.status(status_msg("h1", clock.t - 3600))
    pa.status(status_msg("w1", clock.t - 180, "ALERT DELIVERY FAILED", "3 tries.", ("warning",), 4))
    t = pa.tapout()
    assert (t["state"], t["word"]) == ("warn", "ALERT DELIVERY FAILED, 3 min ago") and "3 tries." in t["detail"]
    pa.status(status_msg("o1", clock.t - 120, "OTA update available", "v2.5.0.", ("arrow_down",), 3))
    assert pa.tapout()["word"] == "ALERT DELIVERY FAILED, 3 min ago"          # a normal message doesn't clear it
    assert offline(cad).tapout()["word"] == "ALERT DELIVERY FAILED, 3 min ago"  # kept across a restart
    pa.status(status_msg("h2", clock.t - 60))                                    # the next heartbeat: all clear
    assert pa.tapout()["state"] == "ok" and pa.tapout()["word"] == "ok, heartbeat 60 s ago"
    pa.status(status_msg("w2", clock.t - 30, "Ring detector NOT armed", "", ("warning",), 5))
    assert pa.tapout()["word"] == "Ring detector NOT armed, 30 s ago"
    pa.status(status_msg("w0", clock.t - 7200, "Pre-alert NOT sent (too late)", "", ("warning",), 4))   # replayed, older
    assert pa.tapout()["word"] == "Ring detector NOT armed, 30 s ago"
    pa.status(status_msg("b1", clock.t - 10, "Tapout restarted", "Boot #8, last reset: POWERON.", ("white_check_mark",), 2))
    assert (pa.tapout()["state"], pa.tapout()["word"]) == ("ok", "ok, restarted 10 s ago")    # a restart starts afresh
    pa.status(status_msg("w3", clock.t, "Detector line STUCK active", "D4 low for 10 min.", ("warning",), 4))
    clock.t += 7 * 3600                                                          # and then silence: that is worse
    assert pa.tapout()["word"] == "no heartbeat for 7 h"
    long = "x" * 150
    pa.status(status_msg("w4", clock.t, long, "", ("warning",), 4))
    assert len(pa.tapout()["word"]) < 80                                          # the header stays one short line


def test_an_unexpected_restart_is_a_restart_shown_as_a_warning(tmp_path):
    clock = Clock()
    cad = make_cad(tmp_path, clock=clock)
    pa = offline(cad)
    b = parse_status(status_msg("b1", 1, "Tapout restarted UNEXPECTEDLY", "Boot #9, last reset: TASK_WDT.",
                                ("rotating_light",), 4))
    assert (b["kind"], b["seq"], b["unexpected"]) == ("boot", 9, True)
    assert "unexpected" not in parse_status(status_msg("b0", 1, "Tapout restarted", "Boot #8.", (), 2))
    pa.status(status_msg("h1", clock.t - 3600))
    pa.status(status_msg("b1", clock.t - 120, "Tapout restarted UNEXPECTEDLY", "Boot #9, last reset: TASK_WDT.",
                         ("rotating_light",), 4))
    t = pa.tapout()
    assert (t["state"], t["word"]) == ("warn", "restarted unexpectedly 2 min ago") and "TASK_WDT" in t["detail"]
    assert "warning" not in pa.health                                          # a boot, not an "other" warning
    pa.status(status_msg("h2", clock.t - 60))
    t = pa.tapout()
    assert (t["state"], t["word"]) == ("ok", "ok, heartbeat 60 s ago") and "restarted unexpectedly" in t["detail"]


def test_a_fresh_start_finds_a_heartbeat_5_h_old(tmp_path, ntfy):
    # a new hub (empty database: nothing kept) after a deploy: the first look reaches back a period and a half (9 h,
    # inside ntfy.sh's 12 h cache), so the heartbeat 5 h ago is found: ok, not "no heartbeat yet" until the next one
    ntfy.add(STATUS, "Tapout heartbeat", HB_TEXT, ("green_heart",), 1, at=time.time() - 10 * 3600)   # outside the look-back
    hb = ntfy.add(STATUS, "Tapout heartbeat", HB_TEXT, ("green_heart",), 1, at=time.time() - 5 * 3600)
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_ring_topic=RING,
                   prealert_status_topic=STATUS, prealert_server=ntfy.url))
    assert app.prealert.health == {}                                        # nothing kept: a new database
    app.prealert.start()
    try:
        assert wait(lambda: app.prealert.health.get("heartbeat", {}).get("id") == hb)
        assert ntfy.gets(STATUS)[0]["since"] == ["540m"]
        t = app.prealert.state()["tapout"]
        assert (t["state"], t["word"]) == ("ok", "ok, heartbeat 5 h ago")
    finally:
        app.prealert.stop()


# -- no topic in the repo: Settings (the DB) sets them, env overrides, none = off ----------------------------------------


def test_with_no_topic_set_the_feed_is_off_not_configured_and_never_crashes_or_loops(tmp_path, ntfy):
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_server=ntfy.url))
    pa = app.prealert
    assert pa is not None and pa.ring_stream.topic == "" and pa.status_stream.topic == ""
    pa.start()
    try:
        time.sleep(0.5)
        assert ntfy.requests == [] and pa.running                      # no request, no retry loop
        t = app.cad.state()["prealert"]["tapout"]
        assert (t["state"], t["word"]) == ("unconfigured", "not configured") and "Settings > System" in t["detail"]
        assert [f["configured"] for f in app.cad.state()["prealert"]["feeds"]] == [False, False]
        h = pa.feed_health()
        assert (h["state"], h["label"]) == ("not configured", "Phone pre-alert (Tapout)") and "Settings" in h["why"]
    finally:
        pa.stop()


def test_the_ring_topic_set_in_settings_starts_the_feed_and_changes_follow_with_no_restart(tmp_path, ntfy):
    from tests.test_roles import CASEY as ADMIN
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_server=ntfy.url))
    api = CadApi(app.cad)
    ids = people(app.cad)
    app.prealert.start()
    try:
        assert ntfy.requests == []
        code, _ = call(api, "/api/cad/settings/notify", {"action": "set", "key": "prealert_ring", "topic": RING}, ADMIN)
        assert code == 200
        assert wait(lambda: ntfy.gets(RING))                           # subscribed within seconds, status still off
        assert ntfy.gets(STATUS) == [] and app.prealert.status_stream.topic == ""
        mid = ntfy.add(RING)
        assert wait(lambda: app.cad.state()["prealert"]["open"], 3)
        # the topic in effect is protected from publishing, in any case
        assert app.pushes.problem(RING) and app.pushes.problem(RING.upper())
        code, _ = call(api, "/api/cad/settings/notify", {"action": "set", "key": "prealert_status", "topic": STATUS}, ADMIN)
        assert code == 200 and wait(lambda: ntfy.gets(STATUS))
        code, _ = call(api, "/api/cad/settings/notify", {"action": "off", "key": "prealert_ring"}, ADMIN)
        assert code == 200 and app.prealert.ring_stream.topic == ""
        n = len(ntfy.gets(RING))
        time.sleep(0.4)
        assert len(ntfy.gets(RING)) == n and mid                         # cleared: no more reads
    finally:
        app.prealert.stop()


def test_the_env_topic_overrides_settings_and_is_protected(tmp_path, ntfy):
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_server=ntfy.url,
                   prealert_ring_topic="example-env-ring", prealert_ring_source="env"))
    app.cad.settings_post("notify", {"action": "set", "key": "prealert_ring", "topic": "example-db-ring", "operator": "x"})
    assert app.pushes.prealert_topics()["ring"] == {"topic": "example-env-ring", "source": "env"}
    assert app.pushes.problem("EXAMPLE-ENV-RING") and app.pushes.problem("example-db-ring")


# -- where it runs ----------------------------------------------------------------------------------------------


def test_the_hub_subscribes_and_shows_it_station_independent(tmp_path, ntfy):
    hub = HubRun(hub_cfg(tmp_path, prealert_enabled=True, prealert_ring_topic=RING, prealert_status_topic=STATUS, prealert_server=ntfy.url))
    try:
        assert wait(lambda: ntfy.gets(RING) and ntfy.gets(STATUS))
        ntfy.add(STATUS, "Tapout heartbeat", HB_TEXT, ("green_heart",), 1)
        ntfy.add(RING)
        assert wait(lambda: hub.get("/api/cad/state")["prealert"]["open"], 3)
        st = hub.get("/api/cad/state")
        assert st["cloud"]["state"] == "down"                    # no station connected: the pre-alert is there anyway
        assert st["prealert"]["tapout"]["state"] == "ok"
        assert wait(lambda: hub.get("/api/state")["prealert"]["open"], 3)   # the monitor too
    finally:
        hub.close()


def test_a_station_forwarding_to_a_hub_doesnt_subscribe_and_follows_the_station_switch(tmp_path, ntfy):
    """Forwarding on (config or Settings > Station > Hub link): the hub is the CAD of record and has the
    pre-alert, so this station doesn't (one alert). Switched off at runtime: it subscribes; on again: it stops."""
    app = make_app(station_cfg(tmp_path, "http://127.0.0.1:9", prealert_enabled=True, prealert_ring_topic=RING, prealert_status_topic=STATUS, prealert_server=ntfy.url))
    try:
        assert app.station.forward and app.cad.prealert is None and app.cad.state()["prealert"] is None
        assert app.state()["prealert"] is None
        app._started = True                                   # (App.start without the radios)
        app.prealert_sync()
        time.sleep(0.3)
        assert ntfy.requests == [] and not app.prealert.running
        app.station.set({"forward": False, "operator": "Lt. Ruiz"})       # all-in-one now: the CAD of record
        assert wait(lambda: ntfy.gets(RING) and ntfy.gets(STATUS))
        first = ntfy.add(RING)
        assert wait(lambda: app.cad.state()["prealert"] and app.cad.state()["prealert"]["open"])
        app.station.set({"forward": True, "hub_url": "http://127.0.0.1:9", "operator": "Lt. Ruiz"})
        assert not app.prealert.running and app.cad.state()["prealert"] is None
        n = len(ntfy.gets(RING))
        time.sleep(0.3)
        assert len(ntfy.gets(RING)) == n                      # no reconnecting while forwarding
        app.station.set({"forward": False, "operator": "Lt. Ruiz"})
        assert wait(lambda: len(ntfy.gets(RING)) > n) and ntfy.gets(RING)[-1]["since"] == [first]   # resumes
    finally:
        app.station.set({"forward": False, "operator": "x"})
        app.prealert.stop()


def test_off_without_the_section_and_the_config(tmp_path, monkeypatch):
    monkeypatch.delenv("SIZEUP_PREALERT_SERVER", raising=False)
    app = App(base(cad_db=str(tmp_path / "cad.sqlite")))
    assert app.prealert is None and app.cad.state()["prealert"] is None
    p = tmp_path / "c.toml"
    p.write_text("")
    assert load(p).prealert_enabled is False
    p.write_text("[prealert]\n")
    c = load(p)
    assert (c.prealert_enabled, c.prealert_server, c.prealert_ring_topic, c.prealert_status_topic, c.prealert_expire_min,
            c.prealert_heartbeat_every_min, c.prealert_heartbeat_stale_min) == (
        True, "https://ntfy.sh", "", "", 5.0, 360.0, 411.0)                  # no default topic: Settings sets it
    p.write_text(f'[prealert]\nring_topic = "{RING}"\nstatus_topic = "{STATUS}"\n')
    c = load(p)
    assert (c.prealert_ring_topic, c.prealert_ring_source, c.prealert_status_topic) == (RING, "config", STATUS)
    monkeypatch.setenv("SIZEUP_PREALERT_RING_TOPIC", "example-ring-from-env")
    c = load(p)
    assert (c.prealert_ring_topic, c.prealert_ring_source) == ("example-ring-from-env", "env")
    monkeypatch.delenv("SIZEUP_PREALERT_RING_TOPIC")
    for bad, why in (('server = "http://ntfy.sh"', "server"), (f'ring_topic = "{RING}"\nstatus_topic = "{RING.upper()}"', "two different"),
                     ('ring_topic = "a/b"', "topic"), ("expire_min = 0", "expire_min"), ("enabled = 1", "enabled"),
                     ("heartbeat_every_min = 0", "heartbeat_every_min"), ('heartbeat_every_min = "6h"', "minutes"),
                     ("heartbeat_every_min = 60\nheartbeat_stale_min = 3000", "heartbeat_stale_min"),
                     ("topic = 1", "unknown key")):
        p.write_text(f"[prealert]\n{bad}\n")
        with pytest.raises(ConfigError, match=why):
            load(p)
    monkeypatch.setenv("SIZEUP_PREALERT_SERVER", "http://127.0.0.1:8999/")
    p.write_text("[prealert]\nheartbeat_every_min = 60\n")
    assert (load(p).prealert_heartbeat_every_min, load(p).prealert_heartbeat_stale_min) == (60.0, 81.0)
    p.write_text("[prealert]\nheartbeat_every_min = 60\nheartbeat_stale_min = 90\n")
    assert (load(p).prealert_heartbeat_stale_min, load(p).prealert_config_note) == (90.0, "")    # set: as given
    for old in (90, 360, 377):          # an old config (the example said 90), or not above the 6 h period: never fatal
        p.write_text(f"[prealert]\nheartbeat_stale_min = {old}\n")
        c = load(p)
        assert c.prealert_heartbeat_stale_min == 411.0 and f"heartbeat_stale_min = {old} " in c.prealert_config_note
        assert "using 411" in c.prealert_config_note
    p.write_text("[prealert]\nheartbeat_stale_min = 378\n")                # 1.05 x the period: as given
    assert (load(p).prealert_heartbeat_stale_min, load(p).prealert_config_note) == (378.0, "")
    p.write_text("[prealert]\nenabled = false\n")
    c = load(p)
    assert (c.prealert_enabled, c.prealert_server) == (False, "http://127.0.0.1:8999")
    assert load(ROOT / "config.example.toml").prealert_enabled
    from sizeup.config import Config, heartbeat_stale_min
    assert Config.prealert_heartbeat_stale_min == heartbeat_stale_min(Config.prealert_heartbeat_every_min) == 411


def test_an_old_stale_setting_is_logged_and_the_app_still_starts(tmp_path, capsys):
    note = "[prealert] heartbeat_stale_min = 90 is not above the 360 min heartbeat period (...): using 411"
    app = App(base(cad_db=str(tmp_path / "cad.sqlite"), prealert_enabled=True, prealert_config_note=note))
    assert app.prealert is not None and app.prealert.stale_s == 411 * 60
    assert f"prealert: config: {note}" in capsys.readouterr().out


# -- read only ------------------------------------------------------------------------------------------------


def test_the_module_only_ever_issues_get():
    """Static: one urlopen, in _get; every Request is method="GET" with no body; nothing else that sends."""
    src = (ROOT / "sizeup" / "prealert.py").read_text()
    tree = ast.parse(src)
    funcs = {n: f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) for n in ast.walk(f)}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    name = lambda c: c.func.id if isinstance(c.func, ast.Name) else c.func.attr if isinstance(c.func, ast.Attribute) else ""
    opens = [c for c in calls if name(c) == "urlopen"]
    assert len(opens) == 1 and funcs[opens[0]] == "_get"
    reqs = [c for c in calls if name(c) == "Request"]
    assert len(reqs) == 1 and funcs[reqs[0]] == "_get"
    kw = {k.arg: k.value for k in reqs[0].keywords}
    assert isinstance(kw.get("method"), ast.Constant) and kw["method"].value == "GET"
    assert "data" not in kw and len(reqs[0].args) == 1
    assert not [c for c in calls if name(c) in ("request", "putrequest", "send", "sendall", "post", "put", "urlretrieve",
                                                 "build_opener", "HTTPConnection", "HTTPSConnection", "create_connection")]
    imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
               for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imports & {"http.client", "socket", "requests", "httpx", "aiohttp", "ssl"}
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body and isinstance(n.body[0], ast.Expr)}
    code_strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docs]
    assert code_strings and not [s for s in code_strings if re.search(r"\b(POST|PUT|PATCH|DELETE)\b(?! FROM)", s)]


def test_the_hub_config_has_no_prealert_topic_and_says_never_to_publish():
    text = (ROOT / "deploy/hub/config.hub.toml").read_text()
    sec = text[text.index("[prealert]"):]
    c = load(ROOT / "deploy/hub/config.hub.toml")
    assert c.prealert_enabled and c.prealert_ring_topic == "" and c.prealert_status_topic == ""
    assert (c.prealert_heartbeat_every_min, c.prealert_heartbeat_stale_min) == (360.0, 411.0)   # Tapout: every 6 h
    assert "Settings > System" in sec and "never publishes" in text[text.index("[prealert]"):].lower()


def test_an_unexpected_error_on_the_stream_never_kills_the_listener(monkeypatch):
    # 2026-10-13: http.client raised AttributeError ("'NoneType' object has no attribute 'peek'") from
    # readline() on a socket closed under it, and the uncaught error ended the ring thread for good.
    import sizeup.prealert as pa
    calls = {"n": 0}

    class Broken:
        fp = None

        def readline(self):
            raise AttributeError("'NoneType' object has no attribute 'peek'")

        def close(self):
            pass

    def fake_get(url, timeout):
        calls["n"] += 1
        return Broken()

    monkeypatch.setattr(pa, "_get", fake_get)
    monkeypatch.setattr(pa.NtfyStream, "BACKOFF_START", 0.01)
    monkeypatch.setattr(pa.NtfyStream, "BACKOFF_MAX", 0.01)
    lines = []
    s = pa.NtfyStream("https://ntfy.example", "pcfd-test", lambda m: None, lines.append).start()
    try:
        deadline = time.time() + 3
        while calls["n"] < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert calls["n"] >= 3                                  # it kept reconnecting
        assert s._thread.is_alive()
        assert "AttributeError" in s.last_error and not s.connected
    finally:
        s.stop()
