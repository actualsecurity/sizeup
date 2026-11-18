"""The station link and the hub never break on a wrong-typed field from the other side (D5 / D6 of #774's contracts
harness). Hub (D5): a station status of another shape is left out before it is stored (the last good one stays) and a
talker / page of another shape dropped; the rest of the post is taken and the 200 answer (its build too) says what was
left out ("refused": ["status" | "live"], "reason"). Every reader of a stored status (the CAD state, the station status the deploy
check reads, Health) survives a bad value anyway. Station (D6): a hub answer's field of another type is ignored (the
last good value stays, said once per field) and no link thread ever dies of one; one that raised starts again and
shows on the Hub row and on Health. Invented data only."""

import threading
import time

import pytest

from sizeup.app import make_app
from sizeup.cloud import statusshape
from sizeup.cloud.client import HubError
from tests.test_app import wait
from tests.test_cloud import TOKEN, HubRun, call, free_port, hub_cfg, station, station_cfg

HUB_SHA = "abcdef1234567"                          # (invented)
GOOD = {"id": "t", "name": "Test station", "radios": [{"label": "FG1", "mhz": 453.0375, "state": "ok", "detail": "",
                                                        "last_heard": None}],
        "outbox": {"pending": 2, "oldest": None, "dropped_audio": 0}, "channels": {"sig": "abc123", "prefs": {}},
        "version": {"build": "", "update": {"failed": {}}}, "health": {"cpu_pct": 12.0, "jobs": {}}}

# (name, the bad status' changes): each one a value a reader dereferenced and fell over (or the shape it must have)
BAD = [
    ("radios 5", {"radios": 5}),
    ("radios [5]", {"radios": [5]}),
    ("radios [null]", {"radios": [None]}),
    ("radio label a list", {"radios": [{"label": [], "state": "ok"}]}),
    ("radio last_heard a word", {"radios": [{"label": "FG1", "last_heard": "late"}]}),
    ("channels 5", {"channels": 5}),
    ("channels a list", {"channels": []}),
    ("channels.sig a number", {"channels": {"sig": 5}}),
    ("outbox 5", {"outbox": 5}),
    ("outbox a list", {"outbox": []}),
    ("outbox.pending a word", {"outbox": {"pending": "x"}}),
    ("outbox.pending true", {"outbox": {"pending": True}}),
    ("version 5", {"version": 5}),
    ("version.update a list", {"version": {"build": "", "update": []}}),
    ("version.update.failed a list", {"version": {"build": "1234567", "update": {"failed": [1]}}}),
    ("version.update.since a word", {"version": {"build": "1234567", "update": {"state": "updating", "to": HUB_SHA,
                                                                              "since": "x"}}}),
    ("health.stt_queue.oldest_s a word", {"health": {"stt_queue": {"oldest_s": "late"}}}),
    ("health 5", {"health": 5}),
    ("health.jobs a job not an object", {"health": {"jobs": {"FG1": 5}}}),
    ("sdr 5", {"sdr": 5}),
    ("sdr.sdrs an object", {"sdr": {"sdrs": {}}}),
    ("p25_systems 5", {"p25_systems": 5}),
    ("stt_terms 5", {"stt_terms": 5}),
    ("reception a list", {"reception": []}),
    ("rerun 5", {"rerun": 5}),
    ("stt_alert_rev a list", {"stt_alert_rev": []}),
    ("name 5", {"name": 5}),
    ("talkgroups [5]", {"talkgroups": [5]}),
    ("talkgroup tg a word", {"talkgroups": [{"tg": "x", "record": True}]}),
]

READS = ("/api/cad/state", "/api/state", "/api/station/status", "/api/cad/health")


def read_all(hub) -> dict:
    """Every route that reads the stored status: (code, body) each; the deploy check's with the station token."""
    return {p: call(hub.url + p, token=TOKEN if p == "/api/station/status" else None, timeout=5.0) for p in READS}


def post(hub, status, **kw):
    return call(hub.url + "/api/station/state", "POST", {"at": time.time(), "talking": [], "paging": [],
                                                          "status": status, **kw})


# -- the hub (D5) ------------------------------------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    h.app.hub.build = HUB_SHA                       # (a hub that knows its build: describe() reads the update state)
    try:
        yield h
    finally:
        h.close()


def test_a_status_of_the_shape_the_hub_reads_is_taken(hub):
    code, body = post(hub, dict(GOOD))
    assert code == 200 and body["ok"] is True
    assert hub.app.hub.status["outbox"]["pending"] == 2 and hub.app.hub.status_refused is None
    assert all(c == 200 for c, _ in read_all(hub).values())
    # absent keys (an older station) and unknown ones (a newer one) at every level are fine
    assert statusshape.problem({"id": "t"}) is None
    assert statusshape.problem(dict(GOOD, novel={"x": [1]}, outbox=dict(GOOD["outbox"], novel=1),
                                    radios=[dict(GOOD["radios"][0], novel=[])])) is None
    assert statusshape.problem(dict(GOOD, sdr=None, reception=None, health={"error": "OSError: no /proc"})) is None


@pytest.mark.parametrize("name,bad", BAD, ids=[n for n, _ in BAD])
def test_a_wrong_typed_status_is_left_out_the_rest_of_the_post_taken(hub, name, bad):
    assert post(hub, dict(GOOD))[0] == 200
    good, at = hub.app.hub.status, hub.app.hub.status_at
    t0 = time.time()
    code, body = post(hub, dict(GOOD, **bad), talking=[dict(TALKER, since=t0, talk_since=t0)])
    assert code == 200 and body["ok"] is True and body["build"] == HUB_SHA and body["caps"]   # the normal answer
    assert body["refused"] == ["status"] and body["reason"].startswith("status.")
    assert hub.app.hub.status is good and hub.app.hub.status_at == at        # the last good status kept
    assert hub.app.hub.last_state >= t0 and len(hub.app.hub.live.state()) == 1   # the heartbeat and talkers taken
    assert hub.app.hub.status_refused["why"] == body["reason"]
    for path, (c, b) in read_all(hub).items():
        assert c == 200, (path, c, b)
    code, body = post(hub, dict(GOOD, name="Test station 2"))                # the next good one is taken
    assert code == 200 and "refused" not in body
    assert hub.app.hub.status["name"] == "Test station 2" and hub.app.hub.status_refused is None


def test_a_status_that_is_not_an_object_is_left_out_and_a_post_without_one_is_the_heartbeat(hub):
    assert post(hub, dict(GOOD))[0] == 200
    for st in (5, "x", [GOOD]):
        code, body = post(hub, st)
        assert code == 200 and body["refused"] == ["status"] and body["reason"] == "status: an object", st
    assert hub.app.hub.status["name"] == "Test station"
    code, body = call(hub.url + "/api/station/state", "POST", {"at": time.time(), "talking": [], "paging": []})
    assert code == 200 and "refused" not in body                # no status (the talkers only): as always
    code, body = post(hub, None)
    assert code == 200 and "refused" not in body


def test_a_left_out_status_is_logged_once_per_reason_and_a_health_hint_while_it_lasts(hub, monkeypatch):
    said = []
    monkeypatch.setattr(hub.app.hub, "log", said.append)
    assert post(hub, dict(GOOD))[0] == 200
    for _ in range(3):
        assert post(hub, dict(GOOD, outbox=5))[1]["refused"] == ["status"]
    assert post(hub, dict(GOOD, radios=5))[1]["refused"] == ["status"]
    left = [s for s in said if "station status left out" in s]
    assert len(left) == 2 and "status.outbox: an object" in left[0] and "status.radios: a list" in left[1]
    assert hub.app.hub.status_refused["n"] == 4
    m = hub.app.health.model()
    assert any(x["channel"] == "Station" and "status left out since" in x["text"] and "status.radios: a list" in x["text"]
               for x in m["hints"])
    alerts, _ = hub.app.health.conditions(m, time.time())
    assert not alerts.get("silent")                              # (its heartbeat is taken: not silent)
    assert post(hub, dict(GOOD))[0] == 200
    assert not any("status left out" in x["text"] for x in hub.app.health.model()["hints"])


TALKER = {"key": "fm:FG1:1.00:", "system": "fm", "channel": "FG1", "tg": None, "radio_id": "", "alias": "", "rig": "",
          "label": "", "since": 1.0, "talk_since": 1.0, "ended": False, "until": None, "dispatcher": False, "main": True}
PAGE = {"key": "page:South 2:1.00", "channel": "South 2", "started": 1.0, "phase": "tones", "ours": True,
        "ended": False, "until": None, "page_id": "", "pairs": [{"a": 349.1, "b": 311.1, "names": ["Test FD"], "ours": True}]}
BAD_LIVE = [
    ("talker until a string", {"talking": [dict(TALKER, ended=True, until="soon")]}),
    ("talker since a string", {"talking": [dict(TALKER, since="x")]}),
    ("talker tg a word", {"talking": [dict(TALKER, tg="x")]}),
    ("page until a string", {"paging": [dict(PAGE, ended=True, until="soon")]}),
    ("page pair a word", {"paging": [dict(PAGE, pairs=[{"a": "x", "b": 311.1}])]}),
    ("page pair not an object", {"paging": [dict(PAGE, pairs=[5])]}),
]


@pytest.mark.parametrize("name,bad", BAD_LIVE, ids=[n for n, _ in BAD_LIVE])
def test_a_wrong_typed_talker_or_page_is_left_out_the_others_and_the_rest_taken(hub, name, bad):
    now = time.time()
    good = {"talking": [dict(TALKER, since=now, talk_since=now, key="a")], "paging": [dict(PAGE, started=now, key="p")]}
    assert post(hub, dict(GOOD), **good)[0] == 200
    which = next(iter(bad))
    mixed = dict(good, **{which: good[which] + bad[which]})       # one good entry and the bad one
    code, body = post(hub, dict(GOOD, name="Test station 2"), **mixed)
    assert code == 200 and body["refused"] == ["live"] and body["reason"].startswith(f"{which}[1].")
    assert body["build"] == HUB_SHA
    assert hub.app.hub.status["name"] == "Test station 2"                   # the status taken
    assert [e["key"] for e in hub.app.hub.live.state()] == ["a"]           # only the bad entry left out
    assert [p["key"] for p in hub.app.hub.live.paging_state()] == ["p"]
    assert hub.app.hub.live_refused["why"] == body["reason"]
    for path, (c, b) in read_all(hub).items():
        assert c == 200, (path, c, b)
    ended = {"talking": [dict(TALKER, ended=True, until=now + 30)], "paging": [dict(PAGE, ended=True, until=now + 30)]}
    code, body = post(hub, dict(GOOD), **ended)
    assert code == 200 and "refused" not in body and len(hub.app.hub.live.state()) == 1


def test_a_bad_status_and_a_bad_talker_in_one_post_both_said(hub):
    code, body = post(hub, dict(GOOD, outbox=5), talking=[dict(TALKER, until="x")])
    assert code == 200 and body["refused"] == ["status", "live"]
    assert body["reason"] == "status.outbox: an object; talking[0].until: a number or null"


def test_what_the_live_state_always_dropped_is_still_dropped_not_refused(hub):
    now = time.time()
    for extra in ({"talking": 5}, {"talking": [5, "x"]}, {"paging": 5}, {"paging": [dict(PAGE, started=now, pairs=5)]}):
        code, _ = post(hub, dict(GOOD), **{"talking": [], "paging": [], **extra})
        assert code == 200 and "refused" not in _, extra
        assert hub.app.hub.live.state() == [] and hub.app.hub.live.paging_state() == []


def test_a_stored_talker_or_page_with_a_wrong_typed_until_never_breaks_the_cad_state(hub):
    assert post(hub, dict(GOOD))[0] == 200
    hub.app.hub.live.update([dict(TALKER, ended=True, until="soon")], time.time(),
                            [dict(PAGE, ended=True, until=["x"])])            # behind the check's back
    assert hub.app.hub.live.state() == [] and hub.app.hub.live.paging_state() == []
    for path, (c, b) in read_all(hub).items():
        assert c == 200, (path, c, b)


# a stored value of another type (set behind the check's back: an older hub's, a bug): every reader survives it
STORED_BAD = [{"radios": 5}, {"radios": [5, None, {"label": []}]}, {"outbox": 5}, {"outbox": {"pending": "x"}},
              {"channels": 5}, {"channels": []}, {"version": 5}, {"health": 5}, {"health": {"jobs": 5}},
              {"health": {"stt_queue": 5}}, {"health": {"stt_queue": {"oldest_s": "late", "queued": "x"}}},
              {"version": {"build": "1234567", "update": {"failed": [1]}}},
              {"version": {"build": "1234567", "update": {"state": "updating", "to": HUB_SHA, "since": "x"}}},
              {"sdr": 5}, {"p25_systems": 5}, {"stt_terms": 5}, {"reception": 5},
              {"rerun": 5}, {"name": 5}, {"name": []}]


@pytest.mark.parametrize("bad", STORED_BAD, ids=[next(iter(b)) + ":" + repr(next(iter(b.values()))) for b in STORED_BAD])
def test_every_reader_survives_a_bad_value_already_stored(hub, bad):
    assert post(hub, dict(GOOD))[0] == 200
    hub.app.hub.status = dict(GOOD, **bad)
    for path, (c, b) in read_all(hub).items():
        assert c == 200, (path, c, b)
    hub.app.health.observe(hub.app.hub.status)
    hub.app.health.tick()
    hub.app.channels_check()
    hub.app.stt_terms_check()
    assert hub.app.channels_sync()["state"]
    assert hub.app.hub.health()["state"] == "ok"


def test_a_report_health_cannot_read_is_the_model_of_no_report_said_once(hub, monkeypatch):
    said = []
    monkeypatch.setattr(hub.app.health, "log", said.append)
    assert post(hub, dict(GOOD))[0] == 200
    # a job whose row is not an object, behind the check's back: the model's dongle row reads it
    hub.app.hub.status = dict(GOOD, sdr={"sdrs": [{"serial": "00000001", "mode": "FG1", "present": True}]},
                              health={"jobs": {"FG1": 5}})
    m = hub.app.health.model()
    assert m["reported"] is False and m["unreadable"] == "AttributeError"
    assert any(x["channel"] == "Station" and "unreadable" in x["text"] for x in m["hints"])
    for _ in range(3):
        hub.app.health.model()
    assert len([s for s in said if "report not read" in s]) == 1
    code, body = call(hub.url + "/api/station/status", token=TOKEN)
    assert code == 200 and body["station"]["reported"] is False


def test_a_channel_push_answer_of_another_shape_is_not_stored(hub, monkeypatch):
    assert post(hub, dict(GOOD))[0] == 200
    monkeypatch.setattr(hub.app.hub, "connected", lambda: True)
    for res, stored in (({"sig": 5}, False), ({"sig": "fffeee", "prefs": {}}, True)):
        monkeypatch.setattr(hub.app.hub, "remote", lambda *a, res=res, **k: res)
        hub.app._chan_push.update(busy=False, at=0.0)
        hub.app.channels_check(force=True)
        assert wait(lambda: not hub.app._chan_push["busy"], 5)
        assert (hub.app.hub.status["channels"] == res) is stored


def test_a_real_stations_status_has_the_shape_the_hub_reads(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        assert wait(lambda: hub.app.hub.status is not None, 10)
        assert statusshape.problem(st.link.status()) is None
        assert hub.app.hub.status_refused is None
    finally:
        st.link.stop()
        hub.close()


# -- the station (D6) --------------------------------------------------------------------------------------------------


class FakeHub:
    """The hub as the station's HubClient sees it: answers[path] (a value, or a callable of the body), every call."""

    def __init__(self):
        self.answers = {"/api/station/state": {"ok": True, "build": "", "caps": ["p25_systems", "stt_input"],
                                               "pool": {"on": True, "worker": False, "ready": 2, "max_s": 30.0},
                                               "services": {}, "station": "t"},
                        "/api/station/events": lambda body: {"acked": max([e["seq"] for e in body["events"]] or [0])},
                        "/api/station/jobs": {"jobs": []}}
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def client(self, timeout):
        return self

    def request(self, method, path, body=None, timeout=None, **kw):
        path = path.split("?")[0]
        with self.lock:
            self.calls.append((method, path))
        if path == "/api/station/jobs":
            time.sleep(0.05)                        # (the long poll)
        if path not in self.answers:
            raise HubError("hub said 404: not found", 404)
        a = self.answers[path]
        return a(body) if callable(a) else a

    def n(self, path):
        with self.lock:
            return sum(1 for _, p in self.calls if p == path)

    def close(self):
        pass


@pytest.fixture
def linked(tmp_path, monkeypatch):
    """A station whose hub is FakeHub (its answers set per test), the link started; (app, hub, log lines)."""
    monkeypatch.setattr("sizeup.cloud.station.STATUS_S", 0.2)
    monkeypatch.setattr("sizeup.cloud.station.RESTART_S", (0.05, 0.2))
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    fake, said = FakeHub(), []
    app.link._client = fake.client
    app.link.log = said.append
    app.link.start()
    try:
        assert wait(lambda: app.link.hub_caps == {"p25_systems", "stt_input"} and app.link.pool.get("ready") == 2, 5)
        yield app, fake, said
    finally:
        app.link.stop()


JOBS_JOIN_S = 5.0


def posts_go_on(fake, path="/api/station/state", n=3, s=5.0) -> bool:
    k = fake.n(path)
    return wait(lambda: fake.n(path) >= k + n, s)


@pytest.mark.parametrize("caps", [5, True, "abc", [1, "stt_input"], {"p25_systems": True}])
def test_a_wrong_typed_caps_is_ignored_and_the_state_thread_goes_on(linked, caps):
    app, fake, said = linked
    fake.answers["/api/station/state"] = dict(fake.answers["/api/station/state"], caps=caps)
    assert posts_go_on(fake)                      # the heartbeat keeps going
    assert app.link.hub_caps == {"p25_systems", "stt_input"}       # the last good word stays ("abc" never {a, b, c})
    assert app.link._threads[1].is_alive() and not app.link.restarts
    assert len([s for s in said if "the hub's caps is not a list of strings" in s]) == 1       # said once
    fake.answers["/api/station/state"] = dict(fake.answers["/api/station/state"], caps=["p25_systems"])
    assert wait(lambda: app.link.hub_caps == {"p25_systems"}, 5)
    fake.answers["/api/station/state"] = dict(fake.answers["/api/station/state"], caps=None)   # a hub without: none
    assert wait(lambda: app.link.hub_caps == frozenset(), 5)


@pytest.mark.parametrize("field,value", [("ready", "x"), ("ready", True), ("ready", 1.5), ("max_s", "y"),
                                         ("on", "yes"), ("gpu_idle_n", "2")])
def test_a_wrong_typed_pool_field_keeps_its_last_value_and_never_raises(linked, monkeypatch, field, value):
    app, fake, said = linked
    fake.answers["/api/station/state"]["caps"].append("stt_pool")
    assert wait(lambda: "stt_pool" in app.link.hub_caps, 5)
    before = dict(app.link.pool)
    fake.answers["/api/station/state"] = dict(fake.answers["/api/station/state"],
                                              pool=dict(fake.answers["/api/station/state"]["pool"], **{field: value}))
    assert posts_go_on(fake)
    assert app.link.pool.get(field) == before.get(field)
    assert app.link.pool_has_room(5.0) is True and app.link.pool_has_room() is True
    assert app.link.pool_gpu_idle_n() == 0 and app.link.pool_takes() is True
    assert len([s for s in said if f"the hub's pool.{field} is not" in s]) == 1
    # a stored value of another type (set behind the check's back): the readers never raise either
    app.link.pool = {"on": True, "ready": "x", "max_s": "y", "gpu_idle_n": "2", "gpu_idle": "yes"}
    assert app.link.pool_has_room(5.0) is False and app.link.pool_gpu_idle_n() == 0


@pytest.mark.parametrize("answer", [{"pool": 5}, {"pool": []}, {"services": 5}, {"services": ["fire"]},
                                    {"station": 5}, {"station": ["t2"]}, {"build": 5}, {"build": ["abc1234"]}])
def test_a_wrong_typed_answer_field_is_ignored_the_rest_applied(linked, answer):
    app, fake, said = linked
    field = next(iter(answer))
    fake.answers["/api/station/state"] = dict(fake.answers["/api/station/state"], caps=["p25_systems"], **answer)
    assert wait(lambda: app.link.hub_caps == {"p25_systems"}, 5)       # the others still applied
    assert posts_go_on(fake)
    assert app.link.pool.get("ready") == 2 and app.link.station_id == "t"
    assert len([s for s in said if f"the hub's {field} is not" in s]) == 1
    assert app.link._threads[1].is_alive() and not app.link.restarts


@pytest.mark.parametrize("answer", [[], "ok", 5, None, [{"caps": ["x"]}]])
def test_an_answer_that_is_not_an_object_changes_nothing(linked, answer):
    app, fake, said = linked
    fake.answers["/api/station/state"] = answer
    assert posts_go_on(fake)
    assert app.link.hub_caps == {"p25_systems", "stt_input"} and app.link.pool.get("ready") == 2
    assert app.link._threads[1].is_alive() and not app.link.restarts and app.link.connected()


@pytest.mark.parametrize("answer", [{"jobs": 5}, {"jobs": "x"}, {"jobs": [5, "x", None]}, [], "jobs", 5])
def test_a_wrong_typed_jobs_answer_never_kills_the_jobs_thread(linked, answer):
    app, fake, said = linked
    fake.answers["/api/station/jobs"] = answer
    assert posts_go_on(fake, "/api/station/jobs")
    assert app.link._threads[2].is_alive() and not app.link.restarts
    ran = []
    fake.answers["/api/station/jobs/0123456789abcdef"] = lambda body: ran.append(body) or {"ok": True}
    fake.answers["/api/station/jobs"] = {"jobs": [{"id": "0123456789abcdef", "type": "call",
                                                   "args": {"fn": "nothing", "args": []}}]}
    assert wait(lambda: ran, 5) and ran[0]["ok"] is False and "not allowed" in ran[0]["error"]


@pytest.mark.parametrize("acked", [True, "3", 2.5, None, [1]])
def test_a_wrong_typed_acked_acks_nothing_and_the_forwarder_goes_on(linked, acked):
    app, fake, said = linked
    fake.answers["/api/station/events"] = {"acked": acked}
    app.link.emit("heard", {"radio_id": "1234567", "channel": "FG1", "at": time.time()})
    assert posts_go_on(fake, "/api/station/events", n=2, s=10.0)
    assert app.link.outbox.pending()[0] == 1 and app.link._threads[0].is_alive() and not app.link.restarts
    fake.answers["/api/station/events"] = lambda body: {"acked": max(e["seq"] for e in body["events"])}
    assert wait(lambda: app.link.outbox.pending()[0] == 0, 15)


def test_a_link_thread_that_raises_starts_again_and_shows_on_the_hub_row_and_health(linked, monkeypatch):
    app, fake, said = linked
    real, n = app.live.state, {"raised": 0}

    def flaky():
        if n["raised"] < 2:
            n["raised"] += 1
            raise TypeError("unexpected")
        return real()
    monkeypatch.setattr(app.live, "state", flaky)
    assert wait(lambda: n["raised"] == 2, 5) and posts_go_on(fake)
    r = app.link.restarts["hub-state"]
    assert r["n"] == 2 and r["why"] == "TypeError"
    assert len([s for s in said if "hub-state raised TypeError" in s]) == 1        # said once per error type
    assert "restarted: hub-state 2× (TypeError" in app.link.status_row()["detail"]
    st = app.link.status()
    assert st["health"]["link_restarts"]["hub-state"]["n"] == 2
    assert statusshape.problem(st) is None


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")   # (the death it stages)
def test_the_watchdog_starts_a_dead_link_thread_again_and_never_one_not_started_yet(linked, monkeypatch):
    app, fake, said = linked
    a = app.link.aircraft
    first = a._thread

    def die(now=None):
        raise SystemExit                            # (past its catch-all: the thread really ends)
    monkeypatch.setattr(a, "step", die)
    first.join(5.0)
    assert not first.is_alive()
    monkeypatch.undo()
    assert app.link.watchdog() == ["hub-aircraft"]
    assert a._thread is not first and a._thread.is_alive()
    assert sum(1 for t in threading.enumerate() if t.name == "hub-aircraft" and t.is_alive()) >= 1
    assert app.link.restarts["hub-aircraft"]["why"] == "RuntimeError"
    assert app.link.watchdog() == []                 # nothing dead now
    pending = threading.Thread(target=lambda: None, name="hub-logs")
    app.link.logs._thread = pending                  # made, not started yet (start() in progress): not dead
    assert app.link.watchdog() == [] and app.link.logs._thread is pending
    # one of the link's own loops that ends while the link runs: started again
    jobs, answer = app.link._threads[2], fake.answers["/api/station/jobs"]

    def end(body):
        raise SystemExit                            # (past the supervisor too: the loop really ends)
    fake.answers["/api/station/jobs"] = end
    jobs.join(JOBS_JOIN_S)
    fake.answers["/api/station/jobs"] = answer
    assert not jobs.is_alive() and all(t.is_alive() for i, t in enumerate(app.link._threads) if i != 2)
    assert app.link.watchdog() == ["hub-jobs"] and app.link._threads[2].is_alive()
    assert posts_go_on(fake, "/api/station/jobs")


def test_health_hints_a_station_link_thread_that_started_again(tmp_path):
    hub = HubRun(hub_cfg(tmp_path))
    try:
        now = time.time()
        lr = {"hub-state": {"n": 3, "why": "TypeError", "at": now - 60}, "hub-jobs": {"n": 1, "why": "KeyError",
                                                                                       "at": now - 7200}}
        assert post(hub, dict(GOOD, health={"jobs": {}, "link_restarts": lr}))[0] == 200
        hints = [x for x in hub.app.health.model()["hints"] if x["channel"] == "Station link"]
        assert len(hints) == 1 and "hub-state 3× (TypeError" in hints[0]["text"] and "hub-jobs" not in hints[0]["text"]
        assert post(hub, dict(GOOD, health={"link_restarts": 5}))[1]["refused"] == ["status"]
    finally:
        hub.close()


def test_a_caps_file_of_another_shape_is_as_if_none(tmp_path):
    cfg = station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}")
    (tmp_path / "st").mkdir(parents=True)
    (tmp_path / "st" / "hub_caps.json").write_text('"abc"')
    app = make_app(cfg)
    assert app.link.hub_caps == frozenset()
    (tmp_path / "st" / "hub_caps.json").write_text('["p25_systems"]')
    assert make_app(cfg).link.hub_caps == {"p25_systems"}


# -- a left-out part never cuts a station off: the heartbeat and the hub's build (auto-update) keep coming ---------------


def test_a_station_whose_status_the_hub_leaves_out_keeps_posting_and_says_so_until_it_is_taken(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path))
    hub.app.hub.build = HUB_SHA
    st = station(station_cfg(tmp_path, hub.url), {})
    said, builds = [], []
    try:
        assert wait(lambda: hub.app.hub.status is not None, 10)
        real = st.link.status
        monkeypatch.setattr(st.link, "status", lambda talkgroups=True: dict(real(talkgroups), radios=5))
        monkeypatch.setattr(st.link, "log", said.append)
        monkeypatch.setattr(st.link.updates, "hub", builds.append)
        st.link._status_now = True
        st.link.live_changed()
        assert wait(lambda: hub.app.hub.status_refused is not None, 10)
        kept = hub.app.hub.status_at
        n, t0 = len(builds), time.time()
        assert wait(lambda: len(builds) >= n + 2 and (hub.app.hub.last_state or 0) > t0, 15)   # heard, and answered
        assert set(builds) == {HUB_SHA}
        assert hub.app.hub.status_at == kept and isinstance(hub.app.hub.status["radios"], list)
        assert st.link.connected() and "left out this station's status: status.radios: a list" in st.link.refused
        assert "status.radios: a list" in st.link.status_row()["detail"]
        assert len([s for s in said if "left out this station's status" in s]) == 1
        monkeypatch.setattr(st.link, "status", real)                     # a good status again: taken, the word gone
        assert wait(lambda: hub.app.hub.status_refused is None and hub.app.hub.status_at > kept, 15)
        assert wait(lambda: st.link.refused == "", 10)
    finally:
        st.link.stop()
        hub.close()


def test_a_station_whose_talker_the_hub_leaves_out_keeps_the_others_and_the_word_clears(tmp_path, monkeypatch):
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        assert wait(lambda: hub.app.hub.status is not None, 10)
        now = time.time()
        talkers = [dict(TALKER, key="ok", since=now, talk_since=now), dict(TALKER, key="bad", ended=True, until="soon")]
        monkeypatch.setattr(st.live, "state", lambda: talkers)
        st.link.live_changed()
        assert wait(lambda: [e["key"] for e in hub.app.hub.live.state()] == ["ok"], 10)
        assert wait(lambda: "left out this station's talkers / pages: talking[1].until" in st.link.refused, 10)
        assert st.link.connected()
        monkeypatch.setattr(st.live, "state", lambda: talkers[:1])
        st.link.live_changed()
        assert wait(lambda: st.link.refused == "", 10)                  # (any post taken without the word clears it)
    finally:
        st.link.stop()
        hub.close()


def test_a_station_from_before_this_still_hears_the_hubs_build_with_a_status_the_hub_leaves_out(tmp_path):
    """A release before this one, posting a status the hub leaves out, as its StationLink._state did: every post is
    answered 200 with the build (Watch.hub: it auto-updates away), caps and pool; the hub hears it (not silent). Its
    answer handling, as it was: updates.hub(r.get("build")), then caps / services / pool / station from the dict."""
    from sizeup.cloud.client import HubClient
    hub = HubRun(hub_cfg(tmp_path))
    hub.app.hub.build = HUB_SHA
    try:
        c = HubClient(hub.url, TOKEN, timeout=5.0)
        assert c.request("POST", "/api/station/state", {"at": time.time(), "talking": [], "paging": [],
                                                        "status": dict(GOOD)})["ok"] is True
        kept = hub.app.hub.status_at
        for _ in range(3):
            t0 = time.time()
            r = c.request("POST", "/api/station/state",                 # (HubError on anything but a 2xx)
                          {"at": t0, "talking": [], "paging": [], "status": dict(GOOD, radios=5)}, timeout=5.0)
            build = r.get("build") if isinstance(r, dict) else None     # (the old station's read)
            assert build == HUB_SHA and isinstance(r.get("caps"), list) and isinstance(r.get("pool"), dict)
            assert hub.app.hub.last_state >= t0 and hub.app.hub.status_at == kept
        c.close()
    finally:
        hub.close()


def test_an_older_updaters_state_file_never_gets_the_status_refused(tmp_path):
    import json as _json
    from sizeup.cloud.autoupdate import Watch
    p = tmp_path / "state.json"
    p.write_text(_json.dumps({"state": 5, "since": "x", "to": ["abcdef1"], "last": "rolled back", "retry": [],
                              "equivalent": "none", "hold": 0, "failed": ["abcdef1"]}))
    st = Watch(own="1234567", run_dir="", state_path=str(p), log=lambda m: None).state()
    assert st == {"last": {}, "retry": {}, "equivalent": {}, "hold": {}, "failed": {}}
    assert statusshape.problem(dict(GOOD, version={"build": "1234567", "update": st})) is None


def test_a_left_out_status_still_gives_its_good_talkgroups_and_the_station_sends_them_again(hub, tmp_path, monkeypatch):
    rows = [{"tg": 307, "record": True, "main": True, "system": "county", "tier": None, "name": "Test TG"}]
    assert post(hub, dict(GOOD))[0] == 200
    code, body = post(hub, dict(GOOD, outbox=5, talkgroups=rows))
    assert code == 200 and body["refused"] == ["status"]
    assert [r["tg"] for r in hub.app.hub.talkgroups] == [307] and hub.app.hub.talkgroups[0]["tier_set"] is False
    code, body = post(hub, dict(GOOD, outbox=5, talkgroups=[{"tg": "x"}]))     # (a bad list: not taken either)
    assert body["refused"] == ["status"] and [r["tg"] for r in hub.app.hub.talkgroups] == [307]
    # the station: a status the hub left out never counts its talkgroups as sent (they go again with the next status)
    app = make_app(station_cfg(tmp_path, hub.url))
    sent = []
    real = app.link._client

    def client(timeout):
        c = real(timeout)
        req = c.request

        def request(method, path, body=None, **kw):
            if path == "/api/station/state" and isinstance(body, dict) and "status" in body:
                sent.append("talkgroups" in body["status"])
            return req(method, path, body, **kw)
        c.request = request
        return c
    app.link._client = client
    monkeypatch.setattr("sizeup.cloud.station.STATUS_S", 0.2)
    # (a station without P25 sends its talkgroups as [] the same way: the first status, then on a change or 30 s)
    real_status = app.link.status
    monkeypatch.setattr(app.link, "status", lambda talkgroups=True: dict(real_status(talkgroups), outbox=5))
    app.link.start()
    try:
        assert wait(lambda: len(sent) >= 4, 10), (sent, app.link.last_error)
        assert all(sent[:4])                         # each status went with its talkgroups (none counted as sent)
        monkeypatch.setattr(app.link, "status", real_status)
        n = len(sent)
        assert wait(lambda: len(sent) >= n + 3, 10)
        assert not all(sent[n + 1:n + 3])            # taken once: not sent again until they change or 30 s
    finally:
        app.link.stop()
