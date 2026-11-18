"""contracts/ (ADR 0004): the station's state post and the hub's answer, as built. A real station posts to a real hub
over HTTP (tests/test_cloud.py's fixtures); what went over the wire is checked against contracts/schemas/, and each
reader is sent keys it does not know. Every id and name here is made up."""
from __future__ import annotations

import json
import time
import urllib.request

import numpy as np
import pytest

from sizeup.cloud import stations as st_mod
from sizeup.cloud.client import HubClient
from tests import contracts as c
from tests.test_app import tone, wait

AS_BUILT = ("station-state", "station-state-answer", "station-hello-answer", "station-error")
T0 = 1_794_990_000.0


@pytest.fixture
def live(tmp_path):
    lv = c.Live(tmp_path)
    yield lv
    lv.close()


def ints(node):
    """Every integer in a parsed JSON document."""
    if isinstance(node, dict):
        for v in node.values():
            yield from ints(v)
    elif isinstance(node, list):
        for v in node:
            yield from ints(v)
    elif isinstance(node, int) and not isinstance(node, bool):
        yield node


def conforms(name: str, raw: bytes | str):
    """raw is JSON a strict reader takes, valid against the schema, with no key the schema does not name and no
    integer a 64-bit float would change. The parsed document."""
    doc = c.loads_strict(raw)
    assert c.errors(name, doc) == []
    assert c.undeclared(name, doc) == [], f"{name}: the code sends keys the schema does not name"
    assert all(c.float_exact(n) for n in ints(doc))
    return doc


def someone_talks_and_a_page_goes_out(st) -> None:
    now = time.time()
    st.live.fm_open("Fireground 1", now)                              # a carrier on an FM channel, its id not decoded yet
    st.live.feed_line("SIZEUP-UNIT countynet 990001 call 4101")       # a P25 grant
    frames = lambda x: [x[i:i + 800] for i in range(0, len(x) - 799, 800)]   # noqa: E731
    for f in frames(np.concatenate([tone(716.1, 1.0), tone(600.9, 1.6)])):
        st.paging.feed("Paging 1", now, f)                            # a two-tone page, as the receiver feeds it


def test_a_real_station_posts_what_the_schema_says_and_the_hub_answers_what_its_schema_says(live):
    st = live.station()
    assert wait(lambda: any("status" in p for p in live.posts()), 30)        # the first post: with status
    someone_talks_and_a_page_goes_out(st)
    assert wait(lambda: any("status" not in p and len(p["talking"]) == 2 and p["paging"] for p in live.posts()), 30)
    st.live.fm_ids("Fireground 1", ["990002"])
    st.link._status_now = True                                               # (what a job from the hub sets)
    st.link.live_changed()
    assert wait(lambda: any("status" in p and p["talking"] and "talkgroups" not in p["status"] for p in live.posts()), 30)
    seen = list(live.seen)
    for raw, code, answer in seen:
        assert code == 200
        post = conforms("station-state", raw)
        ans = conforms("station-state-answer", answer)
        # ... and both are still valid as the designed schemas, whose new keys are all optional (docs/nodes.md)
        assert c.errors("node-state", post) == [] and c.errors("node-state-answer", ans) == []
        assert ans["station"] == c.STATION and ans["caps"] == c.load("station-state-answer")["properties"]["caps"]["x-known"]
    posts = [json.loads(raw) for raw, _, _ in seen]
    # both variants, and every kind of entry, were on the wire
    assert any("status" not in p for p in posts) and any("status" in p for p in posts)
    assert any("talkgroups" in p["status"] for p in posts if "status" in p)
    talkers = [t for p in posts for t in p["talking"]]
    assert {t["system"] for t in talkers} == {"p25", "fm"}
    assert any(t.get("id_on_release") for t in talkers) and any(t["radio_id"] == "990002" for t in talkers)
    assert any(pg["pairs"] and pg["pairs"][0]["names"] for p in posts for pg in p["paging"])
    assert all(p["status"]["id"] == c.STATION for p in posts if "status" in p)


def test_the_committed_vectors_are_shaped_like_what_the_station_sends(live):
    """The valid vectors were written by hand from a recording, with made-up values. They and the code must not drift
    apart: every key the vector has in a part the schema lists completely, the real post has too, and the other way
    round (the two posts are of the same variant: with status and talkgroups, nobody talking)."""
    live.station()
    assert wait(lambda: any("talkgroups" in p.get("status", {}) for p in live.posts()), 30)
    real = next(p for p in live.posts() if "talkgroups" in p.get("status", {}))
    vector = dict(c.vectors("station-state", "valid"))["with-status.json"]
    doc = c.load("station-state")
    status = doc["$defs"]["status"]

    def complete(schema, a, b, path):
        if schema.get("x-complete") is True and isinstance(a, dict) and isinstance(b, dict):
            optional = set(schema.get("properties", {})) - set(schema.get("required", []))
            assert set(a) | optional == set(b) | optional, path
            for k, sub in schema.get("properties", {}).items():
                if k in a and k in b:
                    complete(sub, a[k], b[k], f"{path}.{k}")
    assert set(real) == set(vector)
    complete(status, real["status"], vector["status"], "$.status")


def test_the_hub_takes_every_valid_post_and_ignores_the_keys_it_does_not_know(live):
    """The compatibility rule, the hub as the reader: unknown keys at every level, and the designed node fields
    (capabilities, capabilities_rev, roles_applied: a newer station), change nothing and refuse nothing."""
    known = c.load("station-state-answer")["properties"]["caps"]["x-known"]
    for name in ("station-state", "node-state"):
        for file, post in c.vectors(name, "valid"):
            code, ans = live.call("POST", "/api/station/state", post)
            assert code == 200, (name, file, ans)
            assert c.errors("station-state-answer", ans) == [] and ans["caps"] == known and ans["station"] == c.STATION
    hub = live.hub.app.hub
    post = dict(c.vectors("station-state", "valid"))["unknown-keys.json"]
    assert post["from_the_future"] and post["talking"][0]["from_the_future"] and post["status"]["from_the_future"]
    post = dict(post, at=time.time())
    code, _ = live.call("POST", "/api/station/state", post)
    assert code == 200
    assert hub.status["id"] == c.STATION and hub.status["outbox"]["pending"] == post["status"]["outbox"]["pending"]
    assert [t["radio_id"] for t in live.hub.app.live.state()] == ["990001"]
    assert [p["channel"] for p in live.hub.app.cad.state()["paging"]] == ["Paging 1"]
    assert hub.talkgroups[0]["tg"] == post["status"]["talkgroups"][0]["tg"] and hub.talkgroups[0]["tier_set"] is False
    newer = dict(c.vectors("node-state", "valid"))["capabilities-in-full.json"]
    assert newer["capabilities"] and newer["capabilities_rev"]
    code, ans = live.call("POST", "/api/station/state", newer)
    assert code == 200 and "capabilities_wanted" not in ans           # today's hub: the designed keys are not there
    assert hub.status["id"] == c.STATION                              # ... and the status it had is as it was


def test_the_station_ignores_the_keys_it_does_not_know_in_the_answer(tmp_path):
    """The compatibility rule, the station as the reader: a newer hub's answer (the designed node fields, a capability
    string and keys this station has never heard of) is read like today's."""
    newer = dict(c.vectors("node-state-answer", "valid"))["roles-in-full-and-a-lease.json"]
    extra = {k: newer[k] for k in ("roles_rev", "roles", "leases")}
    extra.update(capabilities_wanted=True, from_the_future={"x": [1, 2]},
                 caps=["p25_systems", "stt_input", "stt_pool", "from_the_future"],
                 pool={"on": True, "worker": False, "ready": 1, "max_s": 45.0, "gpu_idle": False, "gpu_idle_n": 0,
                       "from_the_future": 1},
                 services={"fm:Fireground 1": "fire", "fm:Paging 1": "a_service_from_the_future"})
    lv = c.Live(tmp_path, answer_with=extra)
    try:
        st = lv.station()
        link = st.link
        assert wait(lambda: len(lv.seen) >= 3 and link.connected(), 30)          # it keeps posting: nothing tripped
        assert all(code == 200 and c.errors("node-state-answer", json.loads(a)) == [] for _, code, a in lv.seen)
        assert link.last_error == "" and link.station_id == c.STATION
        assert link.hub_caps == frozenset(extra["caps"])                         # kept, and acted on by nothing
        assert link.pool_takes() and link.pool_has_room(30.0) and not link.pool_has_room(60.0)
        from sizeup import sourcekeys
        assert sourcekeys.service_overrides() == {"fm:Fireground 1": "fire"}     # the unknown service: dropped, no error
        assert json.loads((tmp_path / "st" / "hub_caps.json").read_text(encoding="utf-8")) == sorted(extra["caps"])
    finally:
        lv.close()             # (the process-wide service map is put back by conftest's fixture)


def test_an_answer_without_caps_clears_the_stations_set(tmp_path):
    """As built, and in the schema's words: each answer replaces the whole set. A hub that sends no caps (an older
    one) switches every capability off on the station, on disk too."""
    lv = c.Live(tmp_path)
    try:
        st = lv.station()
        known = c.load("station-state-answer")["properties"]["caps"]["x-known"]
        assert wait(lambda: st.link.hub_caps == frozenset(known), 30)
    finally:
        lv.close()
    older = c.Live(tmp_path / "older", answer_without=("caps", "pool", "services", "station"))
    try:
        st = older.station()
        st.link._set_caps(known)
        assert wait(lambda: older.seen and st.link.hub_caps == frozenset(), 30)
        assert st.link.connected() and st.link.pool == {} and st.link.station_id == c.STATION
    finally:
        older.close()


def test_paging_is_read_only_beside_talking(live):
    """As built (sizeup/cloud/hub.py Hub._state): `paging` is taken only when `talking` is a list, which is why the
    schema requires both. A writer that sent a page alone would show no page on the hub."""
    page = dict(c.vectors("station-state", "valid"))["talking-only.json"]["paging"]
    alone = dict(c.vectors("station-state", "invalid"))["no-talking.json"]
    assert c.errors("station-state", alone) and alone["paging"]
    assert live.call("POST", "/api/station/state", {"at": time.time(), "paging": page})[0] == 200
    assert live.hub.app.cad.state()["paging"] == []
    assert live.call("POST", "/api/station/state", {"at": time.time(), "talking": [], "paging": page})[0] == 200
    assert [p["channel"] for p in live.hub.app.cad.state()["paging"]] == ["Paging 1"]


def test_hello_answers_what_its_schema_says(live, tmp_path):
    for token, kind in (("sz1", "own"), ("shared", "shared")):
        code, ans = live.call("GET", "/api/station/hello", token=token)
        assert code == 200 and ans == {"ok": True, "station": c.STATION, "token": kind}
        assert c.errors("station-hello-answer", ans) == [] and c.undeclared("station-hello-answer", ans) == []
    assert not live.hub.app.hub.connected()                    # never "seen": the station does not look connected
    from tests.test_cloud import HubRun, call, hub_cfg
    fresh = HubRun(hub_cfg(tmp_path / "fresh", cloud_token=c.SHARED))        # a shared token no station has used yet
    try:
        code, ans = call(fresh.url + "/api/station/hello", token=c.SHARED)
        assert code == 200 and ans == {"ok": True, "station": "", "token": "shared"}
        assert c.errors("station-hello-answer", ans) == []
    finally:
        fresh.close()


def test_refusals_and_bad_bodies_are_the_error_envelope(live):
    known = c.load("station-error")["properties"]["reason"]["x-known"]
    assert set(known) == set(st_mod.REASONS) | {"ingest_off"}            # the code's own list of reasons
    reg = live.hub.app.hub.stations
    other = reg.add("st-south", "South firehouse")
    state = dict(c.vectors("station-state", "valid"))["with-status.json"]
    for want, reason, got in (
            (401, "no_token", live.call("POST", "/api/station/state", state, token=None)),
            (401, "token_unknown", live.call("POST", "/api/station/state", state, token="sz1.st-north." + "A" * 43)),
            (401, "token_unknown", live.call("POST", "/api/station/state", state, token="sw1")),   # a worker's token
            (401, "read_only", live.call("POST", "/api/station/state", state, token="status")),
            (409, "multi_station_off", live.call("POST", "/api/station/state", {"at": T0, "talking": [], "paging": []},
                                                 token=other)),
            (401, "id_mismatch", live.call("POST", "/api/station/state",
                                           dict(state, status=dict(state["status"], id="st-south"))))):
        code, body = got
        assert code == want and body["reason"] == reason and reason in known, (want, reason, got)
        assert c.errors("station-error", body) == [] and c.undeclared("station-error", body) == []
    for file, bad in c.vectors("station-state", "invalid"):
        if isinstance(bad, list):                                  # the one thing the hub refuses: not an object
            code, body = live.call("POST", "/api/station/state", bad)
            assert code == 400 and c.errors("station-error", body) == [] and "reason" not in body, file
    reg.revoke(c.STATION)
    code, body = live.call("GET", "/api/station/hello")
    assert code == 401 and body["reason"] == "token_revoked" and c.errors("station-error", body) == []
    from tests.test_cloud import HubRun, call, hub_cfg
    off = HubRun(hub_cfg(live.tmp / "off", cloud_token=""))            # no token configured, no station added
    try:
        code, body = call(off.url + "/api/station/hello", token=c.SHARED)
        assert code == 403 and body["reason"] == "ingest_off" and c.errors("station-error", body) == []
    finally:
        off.close()


# -- known deviations (contracts/README.md): pinned as they are, so the day one is fixed this fails and the table is
# -- brought up to date ------------------------------------------------------------------------------------------------

def test_known_deviation_a_nan_goes_over_the_wire_as_a_token_that_is_not_json(live):
    """D1. The station's client writes a float NaN as the bare token NaN (json.dumps' default), and the hub's
    json.loads takes it: neither is JSON (RFC 8259), and a reader that is not Python refuses the whole post."""
    client = HubClient(live.url, live.own, timeout=10.0)
    try:
        ans = client.request("POST", "/api/station/state", {"at": float("nan"), "talking": [], "paging": []})
    finally:
        client.close()
    raw, code, _ = live.seen[-1]
    assert code == 200 and ans["ok"] is True and b'"at":NaN' in raw
    with pytest.raises(c.NotStrictJson):
        c.loads_strict(raw)
    assert live.hub.app.hub.skew != live.hub.app.hub.skew              # ... and the hub's clock skew is now NaN
    committed = (c.VECTORS / "station-state" / "invalid" / "nan-is-not-json.json").read_text(encoding="utf-8")
    with pytest.raises(c.NotStrictJson):
        c.loads_strict(committed)


def test_known_deviation_head_on_the_job_poll_takes_the_jobs_and_sends_none(live):
    """D3. The web server answers HEAD as GET without the body (for people's pages), on /api/station/ too: a HEAD on
    the long poll takes the waiting jobs, which are handed out once only, and the station never sees them."""
    hub = live.hub.app.hub
    hub.jobs.put("call", {"fn": "set_mode", "args": []}, 60.0)
    req = urllib.request.Request(live.url + "/api/station/jobs?wait=0", method="HEAD",
                                 headers={"Authorization": f"Bearer {live.own}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200 and r.read() == b""
    assert live.call("GET", "/api/station/jobs?wait=0") == (200, {"jobs": []})
