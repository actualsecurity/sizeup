"""contracts/ (ADR 0004): the cursor of POST /api/station/events, as built. contracts/events/cursor.json holds the rules
as golden sequences; each is replayed here against a real hub over HTTP. The station's side of the same rules (what it
deletes, what it sends again, the one silent re-send of its client) runs a real station against a real hub and against
a stub hub that refuses. The known deviations are pinned behind the switches below. Every id and name is made up."""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import sizeup.cloud.station as station_mod
from sizeup.cloud.client import Backoff, HubClient, HubError
from sizeup.cloud.outbox import Outbox
from tests import contracts as c
from tests import contracts_lane as lane
from tests.test_app import wait

GOLDEN = json.loads((c.ROOT / "events" / "cursor.json").read_text(encoding="utf-8"))
SEQUENCES = {s["name"]: s for s in GOLDEN["sequences"]}

# -- known deviations (contracts/README.md "Known deviations"), pinned as the code is today ------------------------------
# Each test below states today's behaviour and the fixed one. When a fix lands, flip its line here from True to False
# (nothing else), mark the row in contracts/README.md fixed and change the sequence of contracts/events/cursor.json
# that carries its name.
E1_A_FAILED_EVENT_IS_ACKED_AND_GONE = True        # fixed: the answer names it (`failed`), the station keeps it aside
E2_A_REFUSED_BATCH_BLOCKS_THE_OUTBOX = True       # fixed: after a 400 / 413 the station sets the event aside and goes on
E3_ONE_CURSOR_FOR_EVERY_SENDER = True             # fixed: a cursor per (station, epoch): a retry is skipped whoever posted between


# -- the golden sequences against a real hub -----------------------------------------------------------------------------

def event(seq, name: str):
    """One item of a post's `events` from the golden file's [seq, event name]."""
    spec = GOLDEN["events"][name]
    if "item" in spec:
        return spec["item"]
    ev = {"at": c.T0, "type": spec["type"], "data": spec["data"], "audio": None, "audio_dropped": False}
    return ev if seq is None else dict(ev, seq=seq)


def body_of(post):
    if not isinstance(post, dict) or not isinstance(post.get("events"), list):
        return post
    return dict(post, events=[event(seq, name) for seq, name in post["events"]])


def applied_times(lv) -> dict[str, int]:
    """How many times each named activity event of the golden file has been applied on this hub."""
    rows = {(r["source"], r["minute"]): r["calls"] for r in
            lv.hub.app.cad.store.q("SELECT source, minute, calls FROM radio_activity")}
    return {name: rows.get((spec["data"]["source"], spec["data"]["minute"]), 0)
            for name, spec in GOLDEN["events"].items() if spec.get("type") == "activity"}


def hub_fails_marked_events(lv) -> None:
    """The hub fails to apply the golden events marked `hub_fails`, for a reason of its own (as a locked database)."""
    marked = {spec["data"]["event"]["event_id"] for spec in GOLDEN["events"].values() if spec.get("hub_fails")}
    sources = lv.hub.app.cad.sources
    real = sources.on_event

    def on_event(d):
        if d["event"].get("event_id") in marked:
            raise RuntimeError("the hub's own trouble")
        return real(d)
    sources.on_event = on_event


def replay(lv, seq: dict, tmp_path) -> None:
    hub_fails_marked_events(lv)
    for k, step in enumerate(seq["steps"]):
        where = f"{seq['name']}, step {k + 1}"
        if step.get("restart"):
            lv.hub.close()
            from tests.test_cloud import HubRun, hub_cfg
            lv.hub = HubRun(hub_cfg(tmp_path, cloud_token=c.SHARED, cloud_legacy_station_id=c.STATION, **c._cfg_names()))
            hub_fails_marked_events(lv)
            continue
        code, ans = lv.post(body_of(step["post"]))
        assert code == step["status"], (where, code, ans)
        if code == 200:
            assert c.errors(lane.ANSWER, ans) == [] and c.undeclared(lane.ANSWER, ans) == [], (where, ans)
            assert {key: ans[key] for key in step["answer"]} == step["answer"], (where, ans)
        else:
            assert c.errors("station-error", ans) == [], (where, ans)
            assert ans.get("reason") == step.get("reason"), (where, ans)
        h = lv.hub.app.hub
        assert [h.epoch, h.seq] == step["cursor"], where
        assert h.errors == step["errors"], (where, h.last_error)
        if "applied_times" in step:
            got = applied_times(lv)
            assert {name: got[name] for name in step["applied_times"]} == step["applied_times"], where


@pytest.mark.parametrize("name", list(SEQUENCES))
def test_the_hub_follows_the_golden_sequence(tmp_path, name):
    """One sequence of contracts/events/cursor.json, on a hub that has never had a post: every answer, the cursor
    after every post, how many times each event was applied and how many failed."""
    lv = lane.Lane(tmp_path)
    try:
        replay(lv, SEQUENCES[name], tmp_path)
    finally:
        lv.close()


def test_the_golden_file_is_well_formed_and_its_events_are_what_the_schema_says():
    assert GOLDEN["x-version"] == 1 and len(SEQUENCES) == len(GOLDEN["sequences"]) >= 15
    used = set()
    for s in GOLDEN["sequences"]:
        assert set(s) == {"name", "rule", "steps"} and s["rule"].endswith((".", ".)")) and s["steps"], s["name"]
        for step in s["steps"]:
            if step.get("restart"):
                continue
            assert set(step) <= {"post", "status", "answer", "reason", "cursor", "applied_times", "errors"}, s["name"]
            assert ("answer" in step) == (step["status"] == 200), s["name"]
            post = step["post"]
            if isinstance(post, dict) and isinstance(post.get("events"), list):
                used |= {name for _, name in post["events"]}
                if all(isinstance(seq, int) for seq, _ in post["events"]) and step["status"] == 200:
                    whole = dict(body_of(post), station=c.STATION, epoch=post.get("epoch") or "0" * 16)
                    known = [ev for ev in whole["events"] if ev["type"] in lane.types()]
                    good = [ev for ev in known if not _made_to_fail(ev)]
                    assert c.errors(lane.EVENTS, dict(whole, events=good)) == [], s["name"]
    assert used == set(GOLDEN["events"]), "an event of the golden file that no sequence uses (or the reverse)"
    # every deviation a sequence names is one this file pins, and the reverse
    named = {d for s in GOLDEN["sequences"] for d in re.findall(r"\bE\d+\b", s["rule"])}
    assert named == {"E1", "E2", "E3"}


def _made_to_fail(ev: dict) -> bool:
    """A golden event that breaks its sub-schema on purpose (a clip id the hub refuses, a feed event that is none)."""
    node = lane.data_schema(ev["type"])
    from jsonschema import Draft202012Validator
    return not Draft202012Validator(node, registry=c.registry()).is_valid(ev["data"])


# -- the station's side of the cursor -------------------------------------------------------------------------------------

def test_a_real_station_sends_in_order_and_deletes_what_the_hub_acked(tmp_path):
    """The writer's half, on the real code: `seq` starts at 1 and rises by one, every post carries the outbox's epoch
    and the station's id, the hub's `acked` is the last `seq` of the post, and the station's outbox is empty once the
    hub has it all."""
    lv = lane.Lane(tmp_path)
    try:
        st = lv.station()
        for k in range(3):
            lane.fm(st, at=time.time() - 30 + 5 * k, radio=0x7301 + k)
        assert lv.drained(st, 6)
        seqs = [ev["seq"] for ev in lv.events()]
        assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) == 6                  # 3 x (heard, traffic)
        for raw, code, answer in lv.events_seen:
            post, ans = lane.conforms(raw), json.loads(answer)
            assert code == 200 and c.errors(lane.ANSWER, ans) == [] and c.undeclared(lane.ANSWER, ans) == []
            assert post["epoch"] == st.link.outbox.epoch and post["station"] == c.STATION
            assert ans["acked"] == post["events"][-1]["seq"] and ans["applied"] == len(post["events"])
        assert st.link.outbox.pending() == (0, None) and wait(lambda: st.link.sent == 6, 10)
        h = lv.hub.app.hub
        assert (h.epoch, h.seq) == (st.link.outbox.epoch, 6)
    finally:
        lv.close()


class StubHub:
    """A hub that is not one: it answers the state post and the job poll like a hub and does to an events post what
    the test says. `posts`: every events body it got, parsed. answer(body) -> (status, JSON body) | None (None: the
    connection is closed without an answer, after the whole request was read)."""

    def __init__(self, answer):
        self.posts: list[dict] = []
        self.answer = answer
        self.stop = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code: int, obj) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path.startswith("/api/station/jobs"):
                    stub.stop.wait(1.0)                       # (a long poll, cut short)
                self._send(200, {"jobs": []} if "jobs" in self.path else {"ok": True})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path != "/api/station/events":
                    return self._send(200, {"ok": True, "build": "", "caps": ["p25_systems"], "pool": {}, "services": {}})
                body = json.loads(raw)
                stub.posts.append(body)
                got = stub.answer(body)
                if got is None:
                    self.close_connection = True
                    return
                self._send(*got)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, name="stub-hub", daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def acks_all(body) -> tuple[int, dict]:
    return 200, {"ok": True, "acked": max((ev["seq"] for ev in body["events"]), default=0), "applied": len(body["events"])}


class QuickBackoff(Backoff):
    """The link's backoff with its waits cut to hundredths of a second (the rule stays: the same batch again)."""

    def fail(self, err=None) -> float:
        super().fail(err)
        return 0.02


def stub_station(tmp_path, stub: StubHub, monkeypatch):
    from tests.test_cloud import base, station
    monkeypatch.setattr(station_mod, "Backoff", QuickBackoff)
    return station(base(clips_dir=str(tmp_path / "st" / "clips"), cloud_outbox=str(tmp_path / "st" / "outbox.sqlite"),
                        cloud_role="station", cloud_hub_url=stub.url, cloud_token=c.SHARED, **c._cfg_names()), {})


@pytest.mark.parametrize("answer", [(503, {"error": "updating", "updating": True}), (200, {"ok": True}),
                                    (200, {"ok": True, "acked": "three"}), (200, {"ok": True, "acked": None}),
                                    (401, {"error": "refused", "reason": "token_revoked"})],
                         ids=["503", "no-acked", "acked-a-word", "acked-null", "401"])
def test_the_station_sends_the_same_batch_again_until_an_answer_has_a_usable_acked(tmp_path, monkeypatch, answer):
    """Anything but a 2xx answer with an `acked` that int() takes is a failed post: nothing is deleted and the same
    oldest rows go again after the backoff. Once the hub acks, they are gone."""
    refuse = [True]
    stub = StubHub(lambda body: answer if refuse[0] else acks_all(body))
    st = stub_station(tmp_path, stub, monkeypatch)
    try:
        st.link.emit("heard", {"system": "p25", "radio_id": "990001", "alias": "", "at": c.T0, "channel": "Fire Dispatch 1"})
        assert wait(lambda: len(stub.posts) >= 3, 20)
        assert st.link.outbox.pending()[0] == 1 and st.link.sent == 0
        assert all(p == stub.posts[0] and [ev["seq"] for ev in p["events"]] == [1] for p in stub.posts)
        refuse[0] = False
        assert wait(lambda: st.link.outbox.pending()[0] == 0, 20) and wait(lambda: st.link.sent == 1, 10)
    finally:
        st.link.stop()
        stub.close()


def test_an_acked_under_the_last_seq_keeps_the_rest_and_one_over_it_deletes_no_more_than_there_is(tmp_path, monkeypatch):
    """The station deletes exactly the rows up to `acked`: an answer under the post's last `seq` (the hub stopped at a
    source_event) leaves the rest for the next post, which starts at the first row left."""
    stub = StubHub(lambda body: (200, {"ok": True, "acked": body["events"][0]["seq"], "applied": 1}))
    waiting = Outbox(tmp_path / "st" / "outbox.sqlite")       # three rows wait in the file before the station starts,
    for k in range(3):                                        # so its first post has all three
        waiting.add("heard", {"system": "p25", "radio_id": f"99000{k}", "alias": "", "at": c.T0, "channel": "x"})
    waiting.close()
    st = stub_station(tmp_path, stub, monkeypatch)
    try:
        assert wait(lambda: st.link.outbox.pending()[0] == 0, 20)
        assert [[ev["seq"] for ev in p["events"]] for p in stub.posts[:3]] == [[1, 2, 3], [2, 3], [3]]
        assert wait(lambda: st.link.sent == 3, 10)
    finally:
        st.link.stop()
        stub.close()


def test_the_stations_client_sends_a_post_once_more_without_a_word_when_the_connection_drops(tmp_path):
    """HubClient (sizeup/cloud/client.py): a request whose connection is closed before an answer came is sent ONCE
    more on a new connection, silently; a second drop is the error. The hub may have handled the first one: so every
    POST of a station can arrive twice. Events are safe by the cursor (shown with a real hub below). A second
    implementation either does the same or must not assume it is not done to it."""
    drops = [1]

    def answer(body):
        if drops[0]:
            drops[0] -= 1
            return None                                       # read whole, then the connection closed: no answer
        return acks_all(body)
    stub = StubHub(answer)
    post = dict(c.vectors(lane.EVENTS, "valid"))["heard.json"]
    client = HubClient(stub.url, c.SHARED, timeout=10.0)
    try:
        assert client.request("POST", "/api/station/events", post)["acked"] == post["events"][-1]["seq"]
        assert stub.posts == [post, post]                     # delivered twice, the caller told nothing
        drops[0] = 2
        with pytest.raises(HubError) as e:
            client.request("POST", "/api/station/events", post)
        assert "closed the connection" in str(e.value) and e.value.status == 0 and len(stub.posts) == 4
    finally:
        client.close()
        stub.close()
    lv = lane.Lane(tmp_path)                                  # the same double delivery on a real hub: applied once
    try:
        assert lv.post(post)[1] == {"ok": True, "acked": 1, "applied": 1}
        assert lv.post(post)[1] == {"ok": True, "acked": 1, "applied": 0}
    finally:
        lv.close()


def test_the_events_post_is_plain_json_with_a_content_length_and_the_hub_caps_its_size(tmp_path, monkeypatch):
    """Transport: never gzipped, a Content-Length (the hub decodes no chunked body: one without a length reads as
    {} and is 400), 32 MiB at most (413, here with the cap lowered: the test sends no 32 MiB)."""
    import http.client
    from sizeup.cloud import hub as hub_mod
    assert hub_mod.MAX_EVENTS_BODY == 32 * 1024 * 1024
    lv = lane.Lane(tmp_path)
    try:
        post = dict(c.vectors(lane.EVENTS, "valid"))["heard.json"]
        raw = json.dumps(post).encode()
        host, port = lv.url.removeprefix("http://").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.putrequest("POST", "/api/station/events")
        for k, v in (("Authorization", f"Bearer {lv.own}"), ("Content-Type", "application/json"),
                     ("Transfer-Encoding", "chunked")):
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(raw), raw))
        r = conn.getresponse()
        assert r.status == 400 and c.errors("station-error", json.loads(r.read())) == []
        conn.close()
        assert lv.hub.app.hub.seq == 0
        monkeypatch.setattr(hub_mod, "MAX_EVENTS_BODY", len(raw) - 1)
        code, ans = lv.post(post)
        assert code == 413 and c.errors("station-error", ans) == [] and lv.hub.app.hub.seq == 0
        monkeypatch.setattr(hub_mod, "MAX_EVENTS_BODY", len(raw))
        assert lv.post(post)[1]["acked"] == 1
    finally:
        lv.close()


# -- known deviations: pinned (the switches are at the top of this file) -------------------------------------------------

def test_known_deviation_an_event_the_hub_fails_to_apply_is_acked_and_gone(tmp_path):
    """E1. One event that raises on the hub (a hub bug, a shape it does not take) is counted, logged and skipped, and
    the answer's `acked` covers it with no word about it: the real station deletes it, and with it the page, the
    transmission or the words. Only `source_event` is spared (the golden sequences). Fixed: the answer names what
    failed (`failed`: [seq]) and the station keeps those rows aside instead of deleting them."""
    lv = lane.Lane(tmp_path)
    real = lv.hub.app.apply
    fails: list[str] = []

    def apply(kind, d, audio):
        if kind == "traffic" and d.get("id") in fails:
            raise RuntimeError("a bug on the hub")
        return real(kind, d, audio)
    lv.hub.app.apply = apply
    try:
        st = lv.station()
        at = time.time() - 20
        fails.append(f"Fireground1-{int(at * 1000)}")         # (known before it is sent: the clip id is its start)
        bad = lane.fm(st, at=at)
        good = lane.fm(st, at=at + 10, radio=0x7302)
        assert bad == fails[0]
        assert wait(lambda: any(t.id == good for t in lv.hub.app.store.loose) and len(lv.events()) == 4, 20)
        h = lv.hub.app.hub
        seq = next(ev["seq"] for ev in lv.events() if ev["type"] == "traffic" and ev["data"]["id"] == bad)
        answers = [json.loads(a) for _, code, a in lv.events_seen if code == 200]
        assert h.errors == 1 and h.last_error.startswith(f"traffic #{seq}: RuntimeError")
        assert not any(t.id == bad for t in lv.hub.app.store.loose)             # the hub does not have it
        assert wait(lambda: st.link.outbox.pending()[0] == 0, 20)               # ... and the station's outbox is empty
        if E1_A_FAILED_EVENT_IS_ACKED_AND_GONE:
            assert h.seq == 4 and all("failed" not in a for a in answers)       # acked, with no word about it
            assert sum(a["applied"] for a in answers) == 3
        else:
            assert any(a.get("failed") == [seq] for a in answers)               # (the row is kept aside, not deleted)
    finally:
        lv.hub.app.apply = real
        lv.close()


@pytest.mark.parametrize("status", [400, 413])
def test_known_deviation_a_batch_the_hub_always_refuses_blocks_the_outbox_for_good(tmp_path, monkeypatch, status):
    """E2. The station takes a 400 or a 413 like an outage: the same oldest rows again, for ever (the backoff only
    spaces the tries). A batch always starts with the oldest row, so one event the hub will never take (a 413: one
    clip over the hub's 32 MiB once in base64; a 400: a body it cannot read) keeps every later event on the station:
    nothing reaches the hub until someone deletes the outbox. Fixed: after a 400 / 413 the station sends the batch's
    events one by one, sets the refused one aside (kept, counted, said in its status) and goes on."""
    poison = "Fireground1-1794990000000"

    def answer(body):
        if any(ev["data"].get("id") == poison for ev in body["events"]):
            return status, {"error": "too large (32 MB max): send smaller batches" if status == 413 else "bad request: x"}
        return acks_all(body)
    stub = StubHub(answer)
    st = stub_station(tmp_path, stub, monkeypatch)
    try:
        st.link.emit("retext", {"id": poison, "text": "x", "status": "done", "confidence": None, "model": "m.bin"})
        assert wait(lambda: len(stub.posts) >= 2, 20)
        for k in range(3):                                    # what the station hears afterwards
            st.link.emit("heard", {"system": "p25", "radio_id": f"99000{k}", "alias": "", "at": c.T0, "channel": "x"})
        n = len(stub.posts)
        assert wait(lambda: len(stub.posts) >= n + 5, 20)
        if E2_A_REFUSED_BATCH_BLOCKS_THE_OUTBOX:
            assert all(p["events"][0]["seq"] == 1 for p in stub.posts)          # the same oldest row, every time
            assert st.link.outbox.pending()[0] == 4 and st.link.sent == 0       # nothing left the station
        else:
            assert wait(lambda: st.link.sent == 3, 20)
            assert st.link.outbox.pending()[0] <= 1
    finally:
        st.link.stop()
        stub.close()


def test_known_deviation_one_cursor_for_every_sender(tmp_path):
    """E3. The hub keeps ONE (epoch, seq) for everything posted to it (sizeup/cloud/hub.py `station_epoch`,
    `station_seq`), not one per station or per outbox. Two outboxes that post to one hub each reset it for the other:
    a retry of a batch the hub had already applied is applied again (an `activity` counted twice here), and two
    outbox files with the SAME epoch (a copied state folder) lose events instead: the second one's rows are skipped as
    delivered before and acked. A second station is kept out of this route today (409 `multi_station_off`), so the
    two senders here are two outboxes of the one station, as during a move to another machine or another program.
    Fixed: a cursor per (station, epoch): the retry is skipped, whoever posted between."""
    lv = lane.Lane(tmp_path)
    try:
        code, ans = lv.post({"epoch": "c" * 16, "station": c.STATION, "events": []}, token=lv.hub.app.hub.stations.add(
            "st-south", "South firehouse"))
        assert code == 409 and ans["reason"] == "multi_station_off"              # a second station: refused today
        one, two = Outbox(tmp_path / "one.sqlite"), Outbox(tmp_path / "two.sqlite")
        act = lambda k: {"source": "p25:east:7003", "minute": 29916500 + k, "calls": 1, "radios": []}   # noqa: E731
        send = lambda ob: lv.post({"epoch": ob.epoch, "station": c.STATION, "events": [                 # noqa: E731
            dict(r, audio=None) for r in ob.batch()]})[1]
        for k in (1, 2):
            one.add("activity", act(k))
        two.add("activity", act(3))
        assert send(one) == {"ok": True, "acked": 2, "applied": 2}
        assert send(two) == {"ok": True, "acked": 1, "applied": 1}
        again = send(one)                                     # one's answer was lost: it sends the batch again
        counts = {r["minute"] - 29916500: r["calls"] for r in lv.hub.app.cad.store.q("SELECT minute, calls FROM radio_activity")}
        if E3_ONE_CURSOR_FOR_EVERY_SENDER:
            assert again == {"ok": True, "acked": 2, "applied": 2} and counts == {1: 2, 2: 2, 3: 1}
        else:
            assert again == {"ok": True, "acked": 2, "applied": 0} and counts == {1: 1, 2: 1, 3: 1}
        # the same epoch in two files: the second file's first rows are taken for the first one's, and acked
        twin = {"epoch": one.epoch, "station": c.STATION, "events": [
            {"seq": 1, "at": c.T0, "type": "activity", "data": act(9), "audio": None, "audio_dropped": False}]}
        assert lv.post(twin)[1] == {"ok": True, "acked": 2, "applied": 0}
        assert not lv.hub.app.cad.store.q("SELECT 1 FROM radio_activity WHERE minute = ?", (29916509,))
        one.close()
        two.close()
    finally:
        lv.close()
