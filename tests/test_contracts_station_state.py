"""contracts/ (ADR 0004): the station's state post and the hub's answer, as built. A real station posts to a real hub
over HTTP (tests/test_cloud.py's fixtures; the station with everything a status can carry runs on the fakes of
tests/test_antenna_groups.py and tests/test_p25_systems.py); what went over the wire is checked against
contracts/schemas/, and each reader is sent keys it does not know. Every id and name here is made up."""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from sizeup.cloud import stations as st_mod
from sizeup.cloud.client import HubClient
from tests import contracts as c
from tests.test_app import tone, wait

T0 = 1_794_990_000.0
CAPS = c.load("station-state-answer")["properties"]["caps"]["x-known"]
FINE = {p: 200 for p in (*c.PEOPLE_ROUTES, "/api/station/status")}

# -- known deviations (contracts/README.md "Known deviations"), pinned as the code is today ------------------------------
# Each test below states today's behaviour and the fixed one. When a fix lands, flip its line here from True to False
# (nothing else) and mark the row in contracts/README.md fixed.
D1_NAN_GOES_ON_THE_WIRE = True                  # #779: a NaN is sent as null and the hub's skew stays finite
D3_HEAD_ON_THE_JOB_POLL_TAKES_THE_JOBS = True   # #779: HEAD under /api/station/ is 405 and no handler runs
D5_HUB_RAISES_ON_A_WRONG_TYPE = True            # fixed: an HTTP answer, and people's routes go on answering
D6_STATION_RAISES_ON_A_WRONG_TYPE = True        # fixed: the bad value is ignored and the hub-state thread lives


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


def conforms(name: str, raw: bytes | str | dict):
    """raw is JSON a strict reader takes, valid against the schema, with no key the schema does not name and no
    integer a 64-bit float would change. The parsed document."""
    doc = c.loads_strict(raw) if isinstance(raw, (bytes, str)) else raw
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


def status_now(lv, st, want=lambda p: True) -> dict:
    """The station's next post with a status (asked for now, as a job from the hub does) that `want` takes."""
    n = len(lv.seen)
    st.link._status_now = True
    st.link.live_changed()
    assert wait(lambda: any("status" in p and want(p) for p in lv.posts()[n:]), 30)
    return next(p for p in lv.posts()[n:] if "status" in p and want(p))


def both_variants_conform(lv, st) -> list[dict]:
    """Drive the station through both variants of the post; every post and answer on the wire conforms. The posts."""
    assert wait(lambda: any("talkgroups" in p.get("status", {}) for p in lv.posts()), 30)   # the first post: with status
    someone_talks_and_a_page_goes_out(st)
    assert wait(lambda: any("status" not in p and len(p["talking"]) == 2 and p["paging"] for p in lv.posts()), 30)
    st.live.fm_ids("Fireground 1", ["990002"])
    status_now(lv, st, lambda p: p["talking"])
    status_now(lv, st, lambda p: "talkgroups" not in p["status"])      # (the list goes up only when it changed)
    seen = list(lv.seen)
    for raw, code, answer in seen:
        assert code == 200
        post = conforms("station-state", raw)
        ans = conforms("station-state-answer", answer)
        # ... and both are still valid as the designed schemas, whose new keys are all optional (docs/nodes.md)
        assert c.errors("node-state", post) == [] and c.errors("node-state-answer", ans) == []
        assert ans["station"] == c.STATION and ans["caps"] == CAPS
        assert abs(post["at"] - time.time()) < 3600                   # seconds, never milliseconds
    posts = [json.loads(raw) for raw, _, _ in seen]
    assert any("status" not in p for p in posts) and any("status" in p for p in posts)      # both variants
    talkers = [t for p in posts for t in p["talking"]]
    assert {t["system"] for t in talkers} == {"p25", "fm"}
    assert any(t.get("id_on_release") for t in talkers) and any(t["radio_id"] == "990002" for t in talkers)
    assert any(pg["pairs"] and pg["pairs"][0]["names"] for p in posts for pg in p["paging"])
    assert all(p["status"]["id"] == c.STATION for p in posts if "status" in p)
    return posts


def test_a_plain_station_posts_what_the_schema_says_and_the_hub_answers_what_its_schema_says(live):
    """Two FM channels on a dongle each, no P25: `sdr` and `reception` null, `p25_systems` and `talkgroups` empty."""
    posts = both_variants_conform(live, live.station())
    st = next(p["status"] for p in posts if "talkgroups" in p.get("status", {}))
    assert (st["sdr"], st["reception"], st["p25_systems"], st["talkgroups"]) == (None, None, [], [])
    assert [r["label"] for r in st["radios"]] == ["Paging 1", "Fireground 1", "STT", "Voice"]


def test_a_station_with_everything_posts_what_the_schema_says(live, monkeypatch):
    """Shared dongles in an antenna group, an AM channel, the county P25 system with talkgroups, a second P25 system, a
    reception test in the history: every nested object of the status is on the wire, and conforms."""
    st = live.rich_station(monkeypatch)
    both_variants_conform(live, st)
    st.tg.set(4102, record=False)                                      # a switch changed: the talkgroups go up again
    s = conforms("station-state", status_now(live, st, lambda p: p["status"].get("talkgroups")))["status"]
    rows = {r["label"]: r for r in s["radios"]}
    assert set(rows) == {"Paging 1", "Fireground 1", "Tower", st.p25.name, "East", "STT", "Voice"}
    assert rows["Tower"]["am"] is True and rows["Tower"]["freqs"][0]["mhz"] == 118.5 and rows["East"]["system"] == "east"
    assert "keyups" in rows["Fireground 1"] and "min_open_s" in rows["Paging 1"] and "min_open_s" not in rows["Tower"]
    assert s["sdr"]["shared"] is True and len(s["sdr"]["sdrs"]) == 5 and s["sdr"]["groups"][0]["name"] == "roof"
    assert [d.get("group") for d in s["sdr"]["sdrs"]] == ["roof", "roof", "roof", None, None]
    assert [x["name"] for x in s["p25_systems"]] == ["east"] and s["p25_systems"][0]["jobs"][0]["sdr"] == "00000104"
    tgs = {(r["tg"], r["system"], r["tier"]) for r in s["talkgroups"]}
    assert tgs >= {(4101, st.tg.county, None), (7001, "east", "alert")}
    assert s["reception"]["history"][0]["id"] == "r-0001" and s["reception"]["systems"][0]["name"] == "east"
    assert s["health"]["heard"]["tg:4101"]["name"] == "Fire Dispatch 1" and "recorder" in s["health"]
    assert set(s["health"]["jobs"]) == {"Paging 1", "Fireground 1", "Tower", "East 1"}
    assert s["health"]["jobs"]["East 1"]["context"] is True and len(s["health"]["usb"]["devices"]) == 5
    # the hub read all of it: Health, the consoles' state and the deploy check answer
    assert live.people() == FINE
    assert live.hub.app.hub.talkgroups and all("tier_set" in r for r in live.hub.app.hub.talkgroups)


MUTATIONS = {       # what a change in the code would put on the wire: each alone must fail the conformance check
    "a talkgroup row without tier": lambda p: p["status"]["talkgroups"][0].pop("tier"),
    "a new key in health": lambda p: p["status"]["health"].update(new_gauge=1),
    "health.jobs as a list": lambda p: p["status"]["health"].update(jobs=list(p["status"]["health"]["jobs"].values())),
    "a new key on a radio row": lambda p: p["status"]["radios"][0].update(new_flag=True),
    "a new key on a dongle's row": lambda p: p["status"]["sdr"]["sdrs"][0].update(new_flag=True),
    "a new key on a P25 system": lambda p: p["status"]["p25_systems"][0].update(new_flag=True),
    "a new key in a past reception test": lambda p: p["status"]["reception"]["history"][0].update(new_flag=True),
    "a new key in logship": lambda p: p["status"]["logship"].update(new_counter=0),
    "a new key at the top": lambda p: p.update(new_top=1),
    "at in integer milliseconds": lambda p: p.update(at=int(p["at"] * 1000)),
    "last_heard in milliseconds": lambda p: p["status"]["radios"][0].update(last_heard=int(p["at"] * 1000)),
    "a dongle's present as a string": lambda p: p["status"]["sdr"]["sdrs"][0].update(present="yes"),
    "a job's restarts as a string": lambda p: p["status"]["health"]["jobs"]["Tower"].update(restarts="0"),
    "reception history id as a number": lambda p: p["status"]["reception"]["history"][0].update(id=1),
}


def test_a_change_in_what_the_code_sends_fails_the_check(live, monkeypatch):
    """The check on itself: a real post of the station with everything, changed the way a change in the code would
    change it (a key gone, a key added, a type changed), no longer conforms. Each alone."""
    st = live.rich_station(monkeypatch)
    real = status_now(live, st, lambda p: p["status"].get("talkgroups"))
    conforms("station-state", real)
    for what, change in MUTATIONS.items():
        post = copy.deepcopy(real)
        change(post)
        try:
            conforms("station-state", post)
        except AssertionError:
            continue
        pytest.fail(f"not caught: {what}")


def test_the_committed_vector_is_shaped_like_what_the_station_sends(live, monkeypatch):
    """The valid vectors were made from a recording with every name replaced by a made-up one. They and the code must
    not drift apart: wherever the schema lists an object's keys completely, the vector and a real post of the same
    variant (the station with everything, with talkgroups) have the same keys, at every depth."""
    st = live.rich_station(monkeypatch)
    real = status_now(live, st, lambda p: p["status"].get("talkgroups"))
    vector = dict(c.vectors("station-state", "valid"))["with-status.json"]
    doc = c.load("station-state")
    compared = []

    def same(schema, a, b, path):
        if isinstance(a, list) and isinstance(b, list) and isinstance(schema.get("items"), dict):
            for i, (x, y) in enumerate(zip(a, b)):
                same(schema["items"], x, y, f"{path}[{i}]")
        if schema.get("x-complete") is True and isinstance(a, dict) and isinstance(b, dict):
            assert set(a) == set(b), path
            compared.append(path)
            for k, sub in schema.get("properties", {}).items():
                if k in a:
                    same(sub, a[k], b[k], f"{path}.{k}")
    assert set(real) == set(vector)
    same(doc["$defs"]["status"], real["status"], vector["status"], "$.status")
    assert len(compared) > 40 and "$.status.sdr.sdrs[0]" in compared and "$.status.radios[2]" in compared


# -- the compatibility rule on the real readers ------------------------------------------------------------------------

def test_the_hub_takes_a_key_it_does_not_know_in_every_object_at_every_depth(live):
    """The hub as the reader: every valid post, with a key from a newer build added to EVERY object the schema
    describes, at every depth, and the designed node fields (a newer station), is taken like today's; and the
    routes people's consoles and the deploy check read still answer afterwards."""
    hub = live.hub.app.hub
    added = 0
    for name in ("station-state", "node-state"):
        for file, post in c.vectors(name, "valid"):
            post, n = c.with_unknown_keys(name, post)
            added += n
            assert n >= 1 and c.errors(name, post) == [], (name, file)
            code, ans = live.call("POST", "/api/station/state", dict(post, at=time.time()))
            assert code == 200, (name, file, ans)
            conforms("station-state-answer", ans)
            assert ans["caps"] == CAPS and ans["station"] == c.STATION and "capabilities_wanted" not in ans
            assert live.people() == FINE, (name, file)
    assert added > 300                                   # (the status with everything alone has some 60 objects)
    post, n = c.with_unknown_keys("station-state", dict(c.vectors("station-state", "valid"))["with-status.json"])
    talk = dict(c.vectors("station-state", "valid"))["talking-only.json"]
    post.update(c.with_unknown_keys("station-state", talk)[0], status=post["status"], at=time.time())
    paths = set(c.undeclared("station-state", post))
    assert len(paths) > 50 and {"$.from_the_future", "$.talking[0].from_the_future", "$.paging[0].pairs[0].from_the_future",
                                "$.status.sdr.sdrs[0].from_the_future", "$.status.health.jobs.Tower.from_the_future",
                                "$.status.reception.history[0].from_the_future"} <= paths
    assert live.call("POST", "/api/station/state", post)[0] == 200
    assert hub.status["id"] == c.STATION and hub.status["outbox"]["pending"] == post["status"]["outbox"]["pending"]
    assert [t["radio_id"] for t in live.hub.app.live.state()] == [t["radio_id"] for t in talk["talking"] if not t["ended"]]
    assert [p["channel"] for p in live.hub.app.cad.state()["paging"]] == ["Paging 1"]
    assert len(hub.talkgroups) == len(post["status"]["talkgroups"]) and live.people() == FINE


def test_the_station_ignores_the_keys_it_does_not_know_in_the_answer(tmp_path):
    """The station as the reader: a newer hub's answer (the designed node fields, a capability string, a service and
    keys in every object that this station has never heard of) is read like today's."""
    newer = dict(c.vectors("node-state-answer", "valid"))["roles-in-full-and-a-lease.json"]
    extra = {k: newer[k] for k in ("roles_rev", "roles", "leases")}
    extra.update(capabilities_wanted=True, from_the_future={"x": [1, 2]}, caps=[*CAPS, "from_the_future"],
                 pool={"on": True, "worker": False, "ready": 1, "max_s": 45.0, "gpu_idle": False, "gpu_idle_n": 0,
                       "from_the_future": {"x": 1}},
                 services={"fm:Fireground 1": "fire", "fm:Paging 1": "a_service_from_the_future"})
    lv = c.Live(tmp_path, answer_with=extra)
    try:
        st = lv.station()
        link = st.link
        for n in (1, 2, 3):                                        # it keeps posting: nothing tripped
            assert wait(lambda: len(lv.seen) >= n and link.connected(), 30)
            link.live_changed()
        assert all(code == 200 and c.errors("node-state-answer", json.loads(a)) == [] for _, code, a in lv.seen)
        assert link.last_error == "" and link.station_id == c.STATION
        assert all(t.is_alive() for t in link._threads)
        assert link.hub_caps == frozenset(extra["caps"])                         # kept, and acted on by nothing
        assert link.pool_takes() and link.pool_has_room(30.0) and not link.pool_has_room(60.0)
        from sizeup import sourcekeys
        assert sourcekeys.service_overrides() == {"fm:Fireground 1": "fire"}     # the unknown service: dropped, no error
        assert json.loads((tmp_path / "st" / "hub_caps.json").read_text()) == sorted(extra["caps"])
        assert json.loads((tmp_path / "st" / "hub_services.json").read_text()) == {"fm:Fireground 1": "fire"}
    finally:
        lv.close()             # (the process-wide service map is put back by conftest's fixture)


def test_an_answer_without_caps_clears_the_stations_set(tmp_path):
    """As built, and in the schema's words: each answer replaces the whole set. A hub that sends no caps (an older
    one) switches every capability off on the station, on disk too."""
    older = c.Live(tmp_path, answer_without=("caps", "pool", "services", "station"))
    try:
        st = older.station()
        st.link._set_caps(CAPS)
        st.link.live_changed()
        assert wait(lambda: older.seen and st.link.hub_caps == frozenset(), 30)
        assert st.link.connected() and st.link.pool == {} and st.link.station_id == c.STATION
        assert json.loads((tmp_path / "st" / "hub_caps.json").read_text()) == []
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


def test_at_is_seconds_and_the_hub_takes_milliseconds_for_a_clock_far_ahead(live):
    """As built: the station sends time.time(); the hub checks only that `at` is a number. Milliseconds are refused
    by the schema because the hub would take them as they are."""
    ms = dict(c.vectors("station-state", "invalid"))["at-in-milliseconds.json"]
    assert c.errors("station-state", ms) and c.errors("station-state", dict(ms, at=ms["at"] / 1000.0)) == []
    assert c.errors("station-state", dict(ms, at=int(T0))) == []           # whole seconds are seconds too
    assert live.call("POST", "/api/station/state", dict(ms, at=int(time.time() * 1000)))[0] == 200
    assert live.hub.app.hub.skew < -1e11                                   # some 50,000 years
    assert live.call("POST", "/api/station/state", dict(ms, at=time.time()))[0] == 200
    assert abs(live.hub.app.hub.skew) < 5


def test_hello_answers_what_its_schema_says(live, tmp_path):
    for token, kind in (("sz1", "own"), ("shared", "shared")):
        code, ans = live.call("GET", "/api/station/hello", token=token)
        assert code == 200 and ans == {"ok": True, "station": c.STATION, "token": kind}
        conforms("station-hello-answer", ans)
        assert kind in c.load("station-hello-answer")["properties"]["token"]["x-known"]
    assert not live.hub.app.hub.connected()                    # never "seen": the station does not look connected
    from tests.test_cloud import HubRun, call, hub_cfg
    fresh = HubRun(hub_cfg(tmp_path / "fresh", cloud_token=c.SHARED))        # a shared token no station has used yet
    try:
        code, ans = call(fresh.url + "/api/station/hello", token=c.SHARED)
        assert code == 200 and ans == {"ok": True, "station": "", "token": "shared"}
        conforms("station-hello-answer", ans)
    finally:
        fresh.close()


def test_refusals_and_bad_bodies_are_the_error_envelope(live):
    known = c.load("station-error")["properties"]["reason"]["x-known"]
    # the code's own list of reasons, and the three it gives outside that list (the last two: the workers' routes,
    # tests/test_contracts_routes.py)
    assert set(known) == set(st_mod.REASONS) | {"ingest_off", "stations_off", "gpu_only"}
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
        conforms("station-error", body)
    for file, bad in c.vectors("station-state", "invalid"):
        if isinstance(bad, list):                                  # the one thing the hub refuses: not an object
            code, body = live.call("POST", "/api/station/state", bad)
            assert code == 400 and "reason" not in body, file
            conforms("station-error", body)
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


# -- known deviations: pinned (the switches are at the top of this file) -------------------------------------------------

def test_known_deviation_a_nan_goes_over_the_wire_as_a_token_that_is_not_json(live):
    """D1. The station's client writes a float NaN as the bare token NaN (json.dumps' default), and the hub's
    json.loads takes it: neither is JSON (RFC 8259), and a reader that is not Python refuses the whole post. Fixed:
    the NaN goes as null and the hub's clock skew stays a finite number."""
    client = HubClient(live.url, live.own, timeout=10.0)
    try:
        ans = client.request("POST", "/api/station/state", {"at": float("nan"), "talking": [], "paging": []})
    finally:
        client.close()
    raw, code, _ = live.seen[-1]
    assert code == 200 and ans["ok"] is True
    if D1_NAN_GOES_ON_THE_WIRE:
        assert b'"at":NaN' in raw and math.isnan(live.hub.app.hub.skew)
        with pytest.raises(c.NotStrictJson):
            c.loads_strict(raw)
    else:
        assert c.loads_strict(raw)["at"] is None and math.isfinite(live.hub.app.hub.skew)
    with pytest.raises(c.NotStrictJson):           # (the committed vector stays invalid either way: it is not JSON)
        c.loads_strict((c.VECTORS / "station-state" / "invalid" / "nan-is-not-json.json").read_text(encoding="utf-8"))


def test_known_deviation_head_on_the_job_poll_takes_the_jobs_and_sends_none(live):
    """D3. The web server answers HEAD as GET without the body (for people's pages), on /api/station/ too: a HEAD on
    the long poll takes the waiting jobs, which are handed out once only, and the station never sees them. Fixed: a
    HEAD under /api/station/ is 405 and the job is still there for the station's GET."""
    hub = live.hub.app.hub
    job = hub.jobs.put("call", {"fn": "set_mode", "args": []}, 60.0)
    req = urllib.request.Request(live.url + "/api/station/jobs?wait=0", method="HEAD",
                                 headers={"Authorization": f"Bearer {live.own}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            head = r.status
            assert r.read() == b""
    except urllib.error.HTTPError as e:
        head = e.code
    code, got = live.call("GET", "/api/station/jobs?wait=0")
    assert code == 200
    if D3_HEAD_ON_THE_JOB_POLL_TAKES_THE_JOBS:
        assert head == 200 and got == {"jobs": []}
    else:
        assert head == 405 and [j["id"] for j in got["jobs"]] == [job["id"]]


# D5: the invalid vector posted -> the people's routes that then drop their connection (no answer at all) until the next
# good post. The first five drop the POST's own connection too, AFTER the hub has kept the bad status.
D5_STATUS = {
    "radios-not-a-list.json": ["/api/station/status"],
    "radio-row-null.json": [],
    "radio-row-label-not-a-string.json": [],
    "channels-not-an-object.json": [],
    "outbox-not-an-object.json": ["/api/cad/state"],
}
D5_LIVE = {"talker-until-as-a-string.json": ["/api/cad/state"],          # (the POST itself is answered 200)
           "page-until-as-a-string.json": ["/api/cad/state"]}


@pytest.mark.parametrize("file", [*D5_STATUS, *D5_LIVE])
def test_known_deviation_the_hub_raises_on_a_wrong_type_in_a_state_post(live, file):
    """D5. The hub ignores a key it does not know, but a known key of the wrong type is not handled: Hub._state keeps
    the status first and then reads it, raising in the handler (the connection is dropped with no answer), and the
    routes that read the kept status or the live snapshot drop theirs until the next good post. Fixed: the post gets
    an HTTP answer, and people's routes go on answering."""
    good = dict(c.vectors("station-state", "valid"))["with-status.json"]
    bad = dict(c.vectors("station-state", "invalid"))[file]
    assert c.errors("station-state", bad)
    assert live.call("POST", "/api/station/state", dict(good, at=time.time()))[0] == 200 and live.people() == FINE
    code, _ = live.call("POST", "/api/station/state", dict(bad, at=time.time()))
    after = live.people()
    try:
        if D5_HUB_RAISES_ON_A_WRONG_TYPE:
            down = D5_STATUS.get(file, D5_LIVE.get(file))
            assert code == (200 if file in D5_LIVE else 0), (file, code)      # 0: no HTTP answer at all
            assert after == {**FINE, **{p: 0 for p in down}}, (file, after)
            if file in D5_STATUS:                                             # ... and the bad status was kept
                assert c.errors("station-state", {"at": T0, "talking": [], "paging": [], "status": live.hub.app.hub.status})
        else:
            assert code in (200, 400) and after == FINE, (file, code, after)
    finally:                                         # the next good post puts the hub right (and before it is closed)
        assert live.call("POST", "/api/station/state", dict(good, at=time.time()))[0] == 200
        assert live.people() == FINE


@pytest.mark.parametrize("caps", [5, True])
def test_known_deviation_a_caps_that_is_no_list_ends_the_stations_state_thread(tmp_path, monkeypatch, caps):
    """D6. The station ignores a key it does not know, but `caps` of the wrong type raises in StationLink._set_caps,
    outside the thread's own error handling: the hub-state thread ends, and with it the heartbeat, talking now, the
    status and the hub's build (so no update either) until the station restarts. Fixed: the value is ignored."""
    died: list[tuple[str, str]] = []
    monkeypatch.setattr(threading, "excepthook", lambda a: died.append((a.thread.name, type(a.exc_value).__name__)))
    lv = c.Live(tmp_path, answer_with={"caps": caps})
    try:
        st = lv.station()
        state = next(t for t in st.link._threads if t.name == "hub-state")
        assert wait(lambda: len(lv.seen) >= 1, 30)
        state.join(5.0 if D6_STATION_RAISES_ON_A_WRONG_TYPE else 0.5)
        n = len(lv.seen)
        st.link.live_changed()
        if D6_STATION_RAISES_ON_A_WRONG_TYPE:
            assert not state.is_alive() and died == [("hub-state", "TypeError")]
            assert not wait(lambda: len(lv.seen) > n, 0.5)                    # no post ever again
        else:
            assert state.is_alive() and died == [] and wait(lambda: len(lv.seen) > n, 30)
        assert st.link.hub_caps == frozenset()
    finally:
        lv.close()


def test_known_deviation_other_wrong_types_in_the_answer_are_taken_as_they_are(tmp_path):
    """D6. `caps` as a string becomes one capability per character, and a `pool.ready` that is no number makes
    StationLink.pool_has_room raise in whoever asks (the speech-to-text hand-over). Fixed: both are ignored."""
    lv = c.Live(tmp_path, answer_with={"caps": "abc", "pool": {"on": True, "ready": "x", "max_s": 10}})
    try:
        st = lv.station()
        assert wait(lambda: len(lv.seen) >= 1 and st.link.pool.get("on") is True, 30)
        if D6_STATION_RAISES_ON_A_WRONG_TYPE:
            assert st.link.hub_caps == frozenset("abc")
            st.link.hub_caps = frozenset(CAPS)       # (so that the pool counts as taken: what the question needs)
            with pytest.raises(ValueError):
                st.link.pool_has_room(5.0)
        else:
            assert st.link.hub_caps == frozenset()
            st.link.hub_caps = frozenset(CAPS)
            assert st.link.pool_has_room(5.0) is False
    finally:
        lv.close()
