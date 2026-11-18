"""A batch the hub refuses for good never blocks the station's outbox (sizeup/cloud/station.py _forward_once /
_rejected, sizeup/cloud/outbox.py quarantine, the hub's reasons in sizeup/cloud/hub.py _events, the alert in
sizeup/cloud/health.py). Invented calls only."""

from __future__ import annotations

import base64
import json
import time

import numpy as np
import pytest

from sizeup.app import make_app
from sizeup.cad.dispatcher import wav_bytes
from sizeup.cloud import hub as hub_mod
from sizeup.cloud import outbox as outbox_mod
from sizeup.cloud.client import Backoff, HubError
from sizeup.cloud.outbox import Outbox
from tests.test_app import wait
from tests.test_cloud import HubRun, _traffic_event, all_traffic, call, free_port, hub_cfg, station, station_cfg
from tests.test_health import Rig, station_status

T = 1_790_000_000.0


def traffic(k: int, at: float = T) -> dict:
    """A transmission as the station queues it (App._transcribed): invented."""
    return {"id": f"FG1-{int(at * 1000) + k}", "source": "FG1", "start": at + k, "end": at + k + 2,
            "text": "Engine 60 responding", "status": "done", "unit": "", "talkgroup": None, "rig": "", "radio_id": "",
            "raw": "Engine 60 responding", "confidence": 0.9, "model": "m.bin", "attach": True, "pairs": None}


def page(k: int, at: float = T) -> dict:
    return {"id": f"South2-{int(at * 1000) + k}", "label": "South 2", "start": at + k, "end": at + k + 8,
            "codes": ["PCFD"], "status": "pending"}


class FakeHub:
    """The hub's events route, as an older hub answers it: a batch holding a poisoned event (by its id) is refused
    as a whole (400, no reason); one over max_bytes is 413; `down`: 503 for everything."""

    def __init__(self, poison=(), max_bytes: int | None = None):
        self.poison, self.max_bytes = set(poison), max_bytes
        self.down = False
        self.got: list[str] = []            # the ids taken, in order
        self.posts = 0
        self.acked = 0

    def __call__(self, timeout):
        return self

    def close(self):
        pass

    def request(self, method, path, body=None, timeout=None, **kw):
        assert (method, path) == ("POST", "/api/station/events")
        self.posts += 1
        if self.down:
            raise HubError("hub said 503: the hub is updating: back in a few seconds", 503)
        evs = body["events"]
        if self.max_bytes is not None and len(json.dumps(body)) > self.max_bytes:
            raise HubError("hub said 413: too large (1 MB max): send smaller batches", 413, None,
                           {"error": "too large", "reason": "too_large"})
        if any(e["data"].get("id") in self.poison for e in evs):
            raise HubError("hub said 400: bad request: invalid literal", 400, None, {"error": "bad request: invalid literal"})
        self.got += [e["data"].get("id") for e in evs]
        self.acked = max([e["seq"] for e in evs], default=self.acked)
        return {"ok": True, "acked": self.acked, "applied": len(evs)}


@pytest.fixture
def link(tmp_path):
    """A station's hub link (not started: the test drives _forward_once), its outbox on disk."""
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    logs: list[str] = []
    app.link.log = logs.append
    app.link.logs_said = logs
    yield app.link
    app.link.stop()
    app.link.outbox.close()


def drain(link, hub, now=None, steps=400) -> int:
    """_forward_once until nothing waits; every step goes on at once (no backoff here). The posts it took."""
    backoff = Backoff(jitter=lambda a, b: 1.0)
    n0 = hub.posts
    for _ in range(steps):
        w = link._forward_once(hub, backoff, now)
        if w is None:
            return hub.posts - n0
        assert w == 0, (w, link.last_error)
    raise AssertionError("never drained")


def alerts(link, dt=0.0):
    """The hub's Health on this station's status (its quarantine as reported)."""
    r = Rig()
    ob = {"pending": link.outbox.pending()[0], "oldest": None, "dropped_audio": 0, "quarantine": link.outbox.summary()}
    r.at(dt, status=station_status(outbox=ob))
    return {a["key"]: a for a in r.h.alerts()}


# -- one bad event never blocks the rest -----------------------------------------------------------------------------


def test_one_bad_event_in_fifty_the_other_49_go_in_order_and_it_is_quarantined_with_an_alert(link):
    ids = [traffic(k)["id"] for k in range(50)]
    for k in range(50):
        link.outbox.add("traffic", traffic(k), b"RIFF" + bytes(400))
    hub = FakeHub(poison={ids[30]})
    posts = drain(link, hub)
    assert hub.got == ids[:30] + ids[31:]                       # in order, all but the bad one
    assert link.outbox.pending()[0] == 0 and posts < 20         # split in halves: a few posts, not one per event
    q = link.outbox.summary()
    assert (q["n"], q["critical"]) == (1, 0)
    assert q["rows"][0]["kind"] == "traffic" and q["rows"][0]["reason"] == "http_400"
    assert "invalid literal" in q["rows"][0]["error"] and q["rows"][0]["next_at"] is None     # never sent again
    assert any("refused a traffic" in s for s in link.logs_said)
    a = alerts(link)
    assert a["rejected_minor:traffic"]["severity"] == "warning"
    assert a["rejected_minor:traffic"]["title"] == "Station event rejected by hub: traffic"
    # Everything after it goes in whole batches again.
    for k in range(50, 60):
        link.outbox.add("traffic", traffic(k), None)
    assert drain(link, hub) == 1 and hub.got[-10:] == [traffic(k)["id"] for k in range(50, 60)]


def test_two_bad_events_far_apart_are_both_found(link):
    ids = [traffic(k)["id"] for k in range(40)]
    for k in range(40):
        link.outbox.add("traffic", traffic(k), None)
    hub = FakeHub(poison={ids[3], ids[37]})
    drain(link, hub)
    assert hub.got == [i for i in ids if i not in (ids[3], ids[37])]
    assert link.outbox.summary()["n"] == 2


def test_a_page_the_hub_refuses_is_sent_again_slowly_with_a_critical_alert_until_it_takes_it(link):
    p = page(1)
    link.outbox.add("traffic", traffic(0), None)
    link.outbox.add("page", p, b"RIFF" + bytes(800))
    link.outbox.add("traffic", traffic(2), None)
    hub = FakeHub(poison={p["id"]})
    drain(link, hub, now=T)
    assert hub.got == [traffic(0)["id"], traffic(2)["id"]]              # the rest went
    q = link.outbox.summary()
    assert (q["n"], q["critical"]) == (1, 1) and q["rows"][0]["next_at"] == T + outbox_mod.RETRY_S[0]
    assert any("CRITICAL" in s and "page" in s for s in link.logs_said)
    a = alerts(link)
    assert a["rejected:page"]["severity"] == "critical"
    assert a["rejected:page"]["title"] == "Station event rejected by hub: page"
    # Not before its time; then once, as a new event at the end, refused again: later still.
    assert drain(link, hub, now=T + 599) == 0
    drain(link, hub, now=T + 601)
    q = link.outbox.summary()
    assert q["rows"][0]["tries"] == 2 and q["rows"][0]["next_at"] == T + 601 + outbox_mod.RETRY_S[1]
    assert link.outbox.pending()[0] == 0
    # The hub fixed (a deploy): the next try goes, its audio with it, and nothing is left.
    hub.poison.clear()
    sent = hub.posts
    drain(link, hub, now=T + 601 + 1801)
    assert hub.got[-1] == p["id"] and hub.posts == sent + 1
    assert link.outbox.summary()["n"] == 0 and "rejected:page" not in alerts(link)
    assert any("took the page" in s for s in link.logs_said)


def test_a_413_splits_by_size_and_an_event_too_large_alone_goes_without_its_clip(link):
    clip = b"RIFF" + bytes(30_000)
    for k in range(12):
        link.outbox.add("traffic", traffic(k), clip)
    link.outbox.add("traffic", traffic(12), b"RIFF" + bytes(200_000))           # too large for this hub on its own
    link.outbox.add("page", page(13), None)
    hub = FakeHub(max_bytes=100_000)
    drain(link, hub)
    assert hub.got == [traffic(k)["id"] for k in range(13)] + [page(13)["id"]]
    assert link.outbox.summary()["n"] == 0 and link.outbox.too_large_audio == 1 and link.outbox.dropped_audio == 0
    assert any("sent without its clip" in s for s in link.logs_said)


def test_a_503_is_retried_as_before_nothing_split_nor_quarantined(link):
    for k in range(5):
        link.outbox.add("traffic", traffic(k), None)
    hub = FakeHub()
    hub.down = True
    backoff = Backoff(jitter=lambda a, b: 1.0)
    waits = [link._forward_once(hub, backoff) for _ in range(4)]
    assert waits == [1.0, 2.0, 4.0, 5.0]                    # the usual backoff (doubling, 5 s at most at first)
    assert link.outbox.pending()[0] == 5 and link.outbox.summary()["n"] == 0 and link._isolate is None
    assert hub.posts == 4 and "503" in link.last_error
    hub.down = False
    drain(link, hub)
    assert hub.got == [traffic(k)["id"] for k in range(5)]


def test_a_hub_that_refuses_everything_holds_every_event_never_quarantines_them(link):
    class Broken(FakeHub):
        def request(self, method, path, body=None, timeout=None, **kw):
            self.posts += 1
            raise HubError("hub said 400: bad request: no", 400, None, {"error": "bad request: no"})
    for k in range(3):
        link.outbox.add("page", page(k), None)
    hub, backoff = Broken(), Backoff(jitter=lambda a, b: 1.0)
    for _ in range(12):
        w = link._forward_once(hub, backoff)
    assert w > 0 and link.outbox.pending()[0] == 3 and link.outbox.summary()["n"] == 0
    assert sum("refuses even an empty batch" in s for s in link.logs_said) == 1


def test_a_quarantined_page_survives_a_restart_and_goes_once_the_hub_takes_it(tmp_path):
    path = tmp_path / "outbox.sqlite"
    ob = Outbox(path)
    seq = ob.add("page", page(1), b"RIFF" + bytes(100))
    ob.add("traffic", traffic(2), None)
    row = ob.batch(meta=True)[0]
    q = ob.quarantine(row, "http_400", "bad request", now=T)
    assert q["critical"] and ob.pending()[0] == 1 and row["seq"] == seq
    epoch = ob.epoch
    ob.close()
    ob = Outbox(path)                                         # the station restarted
    assert ob.epoch == epoch and ob.summary()["n"] == 1 and ob.summary()["critical"] == 1
    assert ob.requeue_due(now=T + 10) == []
    assert [x["kind"] for x in ob.requeue_due(now=T + 601)] == ["page"]
    assert ob.requeue_due(now=T + 9999) == []                 # one copy at a time
    rows = ob.batch(meta=True)
    assert [r["type"] for r in rows] == ["traffic", "page"] and rows[1]["qid"] and rows[1]["audio"][:4] == b"RIFF"
    assert rows[1]["seq"] > rows[0]["seq"] > seq              # a new event, at the end
    ob.close()
    ob = Outbox(path)                                         # restarted again with the copy queued: still one
    assert ob.pending()[0] == 2 and ob.requeue_due(now=T + 9999) == []
    rows = ob.batch(meta=True)
    ob.released([rows[1]["qid"]])
    ob.ack(rows[1]["seq"])
    assert ob.summary()["n"] == 0 and ob.pending()[0] == 0
    ob.close()


def test_the_quarantine_is_bounded_and_never_drops_a_critical_one(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox_mod, "QUARANTINE_ROWS", 5)
    ob = Outbox(tmp_path / "o.sqlite")
    ob.add("page", page(0), None)
    for k in range(1, 9):
        ob.add("traffic", traffic(k), None)
    for row in ob.batch(meta=True):
        ob.quarantine(row, "http_400", "bad request", now=T)
    s = ob.summary(limit=50)
    assert s["n"] == 5 and s["critical"] == 1 and s["evicted"] == 4
    assert [r["kind"] for r in s["rows"]][0] == "page"
    ob.close()


def test_an_older_station_without_a_quarantine_is_no_alert():
    r = Rig()
    r.at(0, status=station_status())
    assert not [k for k in r.keys() if k.startswith("rejected")]


# -- the hub: its reasons ----------------------------------------------------------------------------------------------


def test_the_hub_lists_each_event_it_cannot_take_and_takes_the_rest(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        batch = {"epoch": "e1", "events": [_traffic_event(1, "FG1-1", now),
                                           {"seq": 2, "type": "traffic", "data": {"id": "../etc"}},
                                           {"type": "traffic", "data": {}},
                                           "not an event",
                                           {"seq": 3, "type": "traffic", "data": ["a", "list"]},
                                           _traffic_event(4, "FG1-4", now)]}
        code, r = call(hub.url + "/api/station/events", "POST", batch)
        assert code == 200 and r["acked"] == 4 and r["applied"] == 2
        assert sorted((x["i"], x["seq"], x["reason"]) for x in r["rejected"]) == [
            (1, 2, "malformed"), (2, None, "malformed"), (3, None, "malformed"), (4, 3, "malformed")]
        assert {t["id"] for t in all_traffic(hub.app.state())} == {"FG1-1", "FG1-4"}
        # A batch it can't read at all: why, for the station.
        code, r = call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": "no"})
        assert code == 400 and r["reason"] == "bad_batch"
        req = __import__("urllib.request").request.Request(
            hub.url + "/api/station/events", method="POST", data=b"{not json",
            headers={"Authorization": f"Bearer {hub_cfg(tmp_path).cloud_token}", "Content-Type": "application/json"})
        try:
            __import__("urllib.request").request.urlopen(req, timeout=10)
            raise AssertionError("taken")
        except __import__("urllib.error").error.HTTPError as e:
            assert e.code == 400 and json.loads(e.read())["reason"] == "bad_json"
    finally:
        hub.close()


def test_through_a_real_hub_a_batch_too_large_splits_and_an_event_it_cannot_apply_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_mod, "MAX_EVENTS_BODY", 120_000)
    hub = HubRun(hub_cfg(tmp_path))
    st = None
    try:
        st = station(station_cfg(tmp_path, hub.url), {})
        wav = wav_bytes(np.zeros(16_000, np.float32), 8000)              # ~32 KB, ~43 KB in base64
        now = time.time()
        ids = []
        for k in range(8):
            d = traffic(k, now)
            ids.append(d["id"])
            st.link.outbox.add("traffic", d, wav)
        st.link.outbox.add("page_text", {"id": "../etc", "raw": "invented"}, None)   # the hub can't apply it
        st.link._wake.set()
        assert wait(lambda: {t["id"] for t in all_traffic(hub.get("/api/state"))} >= set(ids), 20)
        assert wait(lambda: st.link.outbox.pending()[0] == 0, 10)
        assert hub.app.has_clip(ids[0])
        q = st.link.outbox.summary()
        assert q["n"] == 1 and q["rows"][0]["kind"] == "page_text" and q["rows"][0]["reason"] == "malformed"
        assert q["critical"] == 0                   # a bad id never applies: kept, never sent again, never critical
    finally:
        if st is not None:
            st.link.stop()
        hub.close()


def test_a_retried_post_after_a_503_still_lists_the_event_the_hub_could_not_apply(tmp_path, monkeypatch):
    """The hub applied event 1 badly, then lost its lease on event 2 (503): the station sends both again, and the answer
    still says event 1 was refused (its cursor is already past it)."""
    from sizeup import fence as fence_mod
    hub = HubRun(hub_cfg(tmp_path))
    try:
        h, apply = hub.app.hub, hub.app.apply
        fail = {"FG1-1": RuntimeError("a hub bug"), "FG1-2": fence_mod.FencedWrite("not the leader")}

        def flaky(kind, d, audio):
            if d.get("id") in fail:
                raise fail[d["id"]]
            return apply(kind, d, audio)
        monkeypatch.setattr(hub.app, "apply", flaky)
        now = time.time()
        batch = json.dumps({"epoch": "e1", "events": [_traffic_event(1, "FG1-1", now),
                                                      _traffic_event(2, "FG1-2", now)]}).encode()
        with pytest.raises(fence_mod.FencedWrite):
            h._events(batch)
        del fail["FG1-2"]
        code, body, _ = h._events(batch)
        r = json.loads(body)
        assert r["acked"] == 2 and [(x["seq"], x["reason"]) for x in r["rejected"]] == [(1, "apply_failed")]
    finally:
        hub.close()


def test_the_hub_attaches_a_late_page_events_clip_to_the_page_its_text_made(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        pid = f"South2-{int(now * 1000)}"
        d = {"id": pid, "label": "South 2", "mhz": 470.2, "start": now - 20, "end": now - 10,
             "raw": "Engine 60 respond to a drill",
             "status": "done", "kind": "voice", "tones": []}
        wav = wav_bytes(np.zeros(8000, np.float32), 8000)
        r = call(hub.url + "/api/station/events", "POST",
                 {"epoch": "e1", "events": [{"seq": 1, "at": now, "type": "page_text", "data": d}]})[1]
        assert r["applied"] == 1 and not hub.app.audio.has(pid)
        r = call(hub.url + "/api/station/events", "POST",
                 {"epoch": "e1", "events": [{"seq": 2, "at": now, "type": "page", "data": d,
                                             "audio": base64.b64encode(wav).decode()}]})[1]
        assert r["acked"] == 2 and "rejected" not in r and hub.app.audio.has(pid)
        assert sum(p.id == pid for p in hub.app.pages) == 1                 # still one page
    finally:
        hub.close()


# -- source events: a snapshot never goes back -------------------------------------------------------------------------


def _feed(source="county", ref="A1", units=("E60",), typ="ALARM/COMM", where="12 Example Ave", seen=T):
    return {"source": source, "label": "County feed", "rank": 1, "seen_at": seen, "first_seen": T,
            "event": {"source": source, "event_id": ref, "type": typ, "location": where, "units": list(units)}}


def test_a_quarantined_source_event_is_dropped_once_the_feed_has_a_newer_report(link):
    old = _feed()
    link.outbox.add("source_event", old, None)
    class Refuse(FakeHub):
        def request(self, method, path, body=None, timeout=None, **kw):
            if any(e["type"] == "source_event" and e["data"]["seen_at"] == T for e in body["events"]):
                self.posts += 1
                raise HubError("hub said 400: bad request", 400, None, {"error": "bad request"})
            return super().request(method, path, body, timeout, **kw)
    hub = Refuse()
    drain(link, hub, now=T)
    assert link.outbox.summary()["critical"] == 1                 # a feed's event: critical, sent again later
    link.outbox.add("source_event", _feed(units=("E60", "L31"), seen=T + 60), None)     # the feed moved on
    drain(link, hub, now=T + 60)
    drain(link, hub, now=T + 700)                                 # its time to go again: superseded, dropped
    assert link.outbox.summary()["n"] == 0 and link.outbox.pending()[0] == 0
    assert any("newer report" in s for s in link.logs_said)


def test_the_hub_ignores_a_snapshot_older_than_the_one_it_applied(tmp_path):
    from tests.test_incident_sources import Rig as SourceRig
    rig = SourceRig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": rig.clock.t})
    rig.poll()
    [inc] = rig.incidents()
    snap1 = [d for k, d in rig.sent if k == "source_event"][-1]
    rig.at(60)
    rig.feed.change("A1", type="STRU", location="14 Example Ave", units=("E60", "L31"))
    rig.poll()
    after = rig.cad.store.incident(inc["id"])
    assert (after["type"], after["address"]) == ("STRU", "14 Example Ave")
    lines = rig.feed_lines(inc["id"])
    rig.at(700)
    rig.cad.sources.apply("source_event", snap1)                  # the station's quarantine sends the old one again
    again = rig.cad.store.incident(inc["id"])
    assert (again["type"], again["address"]) == ("STRU", "14 Example Ave")
    assert rig.feed_lines(inc["id"]) == lines                     # nothing on the timeline
    assert json.loads(rig.cad.store.one("SELECT data FROM source_reports WHERE ref = 'A1'")["data"])["units"] == ["E60", "L31"]


# -- critical, but never forever ------------------------------------------------------------------------------------


def test_a_critical_one_is_stuck_after_its_tries_and_a_person_drops_it(tmp_path, capsys):
    path = tmp_path / "outbox.sqlite"
    ob = Outbox(path)
    ob.add("page", page(1), None)
    row = ob.batch(meta=True)[0]
    t = T
    q = ob.quarantine(row, "http_400", "bad request", now=t)
    for _ in range(outbox_mod.CRITICAL_TRIES):
        t = q["next_at"] or t
        [copy] = [r for r in ob.batch(meta=True) if r["qid"]] or [None] if ob.requeue_due(now=t) else [None]
        q = ob.quarantine(copy, "http_400", "bad request", now=t)
    assert q["state"] == "stuck" and q["next_at"] is None and ob.requeue_due(now=t + 10 ** 6) == []
    s = ob.summary()
    assert s["kinds"]["page"] == {"n": 1, "critical": 1, "stuck": 1, "late": 0}
    r = Rig()
    r.at(0, status=station_status(outbox={"pending": 0, "oldest": None, "dropped_audio": 0, "quarantine": s}))
    a = {x["key"]: x for x in r.h.alerts()}
    assert a["rejected:page"]["severity"] == "critical" and "stuck" in a["rejected:page"]["text"]
    ob.close()
    assert outbox_mod.main(["--path", str(path), "list"]) == 0
    qid = s["rows"][0]["id"]
    assert f"{qid:>6}  page" in capsys.readouterr().out
    assert outbox_mod.main(["--path", str(path), "drop", str(qid)]) == 0
    ob = Outbox(path)
    assert ob.summary()["n"] == 0
    ob.close()


def test_a_quarantined_clip_is_capped_and_a_resend_keeps_the_outbox_under_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox_mod, "QUARANTINE_CLIP", 1500)
    ob = Outbox(tmp_path / "o.sqlite", max_audio_bytes=3000)
    ob.add("page", page(1), b"a" * 2000)
    ob.add("page", page(2), b"b" * 1000)
    rows = ob.batch(meta=True)
    big = ob.quarantine(rows[0], "http_400", "bad request", now=T)
    ob.quarantine(rows[1], "http_400", "bad request", now=T)
    ob.add("traffic", traffic(3), b"c" * 2500)                       # the outbox nearly full again
    assert [x["kind"] for x in ob.requeue_due(now=T + 601)] == ["page", "page"]
    assert ob.audio_bytes <= 3000
    copies = [r for r in ob.batch(meta=True) if r["qid"]]
    assert copies[0]["qid"] == big["id"] and copies[0]["audio"] is None and copies[0]["audio_dropped"]   # over the cap
    ob.close()


def test_an_alarm_sent_again_too_late_to_raise_stays_critical_as_late(link):
    end = T - 3600
    al = {"id": f"FG1-{int(end * 1000)}", "source": "FG1", "start": end - 5, "end": end, "emergency": True,
          "radio_id": "6301", "unit": "", "talkgroup": None, "rig": "", "text": ""}
    link.outbox.add("alarm", al, None)
    hub = FakeHub(poison={al["id"]})
    drain(link, hub, now=T)
    hub.poison.clear()
    drain(link, hub, now=T + 601)
    q = link.outbox.summary()
    assert q["n"] == 1 and q["rows"][0]["state"] == "late" and q["rows"][0]["next_at"] is None
    assert hub.got[-1] == al["id"]                                  # delivered, once
    a = alerts(link)
    assert a["rejected:alarm"]["severity"] == "critical"
    assert a["rejected:alarm"]["title"] == "Station alarm delivered late, not raised"
    assert drain(link, hub, now=T + 99999) == 0                     # never sent again


def test_muting_the_other_types_never_hides_a_critical_one(link):
    from sizeup.cloud.health import alert_kind
    link.outbox.add("traffic", traffic(0), None)
    link.outbox.add("page", page(1), None)
    hub = FakeHub(poison={traffic(0)["id"], page(1)["id"]})
    drain(link, hub, now=T)
    r = Rig()
    r.h.mutes = lambda: {"rejected_minor": {"until": None}}
    ob = {"pending": 0, "oldest": None, "dropped_audio": 0, "quarantine": link.outbox.summary()}
    r.at(0, status=station_status(outbox=ob))
    a = {x["key"]: x for x in r.h.alerts()}
    assert a["rejected_minor:traffic"]["muted"] and not a["rejected:page"].get("muted")
    assert (alert_kind("rejected:page"), alert_kind("rejected_minor:traffic")) == ("rejected", "rejected_minor")
