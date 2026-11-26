"""contracts/ (ADR 0004): the jobs the hub hands a station, as built. GET /api/station/jobs?wait= (the long poll) and
POST /api/station/jobs/<id> (a job's result). A real station long-polls a real hub over HTTP (tests/contracts.py Live);
every job is made by the hub's own code (the callers that build the argument lists: Settings, the re-run, the catch-up,
the dispatch voice), and what went over the wire, as bytes, is checked against contracts/schemas/. Each reader is sent
keys it does not know. The station's voice, its whisper and its incident-source adapter are stand-ins; nothing else is.
Every id and name here is made up."""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import re
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sizeup import sourcekeys, stt as stt_mod, stt_terms, sttcatchup
from sizeup.app import App, AwsJob
from sizeup.cad import dispatcher
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cloud import hub as hub_mod
from sizeup.cloud import station as station_mod
from sizeup.cloud.hub import StationOffline
from sizeup.radio.fm import Channel, Segment
from sizeup.sources.runner import SourceRunner
from tests import contracts as c
from tests.test_app import voice, wait
from tests.test_contracts_station_state import ints
from tests.test_stt_catchup import Whisper, shed_one

JOBS, RESULT, ANSWER = "station-jobs", "station-job-result", "station-job-result-answer"
ROUTE = "/api/station/jobs"
CLIP = "Fireground1-1794990000000"           # the one transmission's id in the recorded vectors
FM = Channel(453.1, "Fireground 1")
TERMS = "# county_fire: made up\n# units\nEngine one\nLadder two\n# places\nNorthfield\nExample Street\n"

# -- known deviations (contracts/README.md "Known deviations"), pinned as the code is today ------------------------------
# Each test below states today's behaviour and the fixed one. When a fix lands, flip its line here from True to False
# (nothing else) and mark the row in contracts/README.md fixed. Checked ahead of time: with #782 merged in and J3
# flipped, every test of this file passes.
J1_A_JOB_IS_HANDED_OUT_ONCE_AND_A_LATE_RESULT_IS_404 = True   # redelivered until answered; a late result is taken (200)
J2_A_RESULT_IS_DROPPED_AFTER_THREE_TRIES = True               # kept, and sent again when the hub answers
J3_THE_JOBS_THREAD_ENDS_ON_A_WRONG_TYPE = False               # #782: that answer is left out, the thread goes on
J4_AN_ARGUMENT_TOO_MANY_IS_A_TYPE_ERROR = False                # named arguments; one the station does not know is ignored
J5_NO_ARGUMENTS_MEANS_EVERYTHING = False                       # a call without its arguments is refused
J6_THE_HUB_RAISES_ON_A_WRONG_TYPE_IN_A_RESULT = False          # text only / nothing queued, as for no answer at all


# -- the schemas choose by `if` / `then` -----------------------------------------------------------------------------------

@contextlib.contextmanager
def conditionals():
    """tests/contracts.py walks $ref, allOf, anyOf and oneOf to find what a schema says about an object. These schemas
    choose a job's `args` by its `type` and a call's argument list by its `fn` with `if` / `then`, which it does not
    follow. Inside this block it does (the `if`s here hold no $ref)."""
    real = c._branches

    def more(node, resolver, inst):
        out = real(node, resolver, inst)
        for n, r in list(out):
            if isinstance(n.get("if"), dict) and isinstance(n.get("then"), dict) \
                    and Draft202012Validator(n["if"]).is_valid(inst):
                out += more(n["then"], r, inst)
        return out
    c._branches = more
    try:
        yield
    finally:
        c._branches = real


def undeclared(name: str, doc) -> list[str]:
    with conditionals():
        return c.undeclared(name, doc)


def with_unknown_keys(name: str, doc):
    with conditionals():
        return c.with_unknown_keys(name, doc)


def conforms(name: str, raw: bytes | str | dict):
    """raw is JSON a strict reader takes, valid against the schema, with no key the schema does not name (inside a
    job's arguments too) and no integer a 64-bit float would change. The parsed document."""
    doc = c.loads_strict(raw) if isinstance(raw, (bytes, str)) else raw
    assert c.errors(name, doc) == [], (name, doc)
    assert undeclared(name, doc) == [], f"{name}: the code sends keys the schema does not name"
    assert all(c.float_exact(n) for n in ints(doc))
    return doc


def result_conforms(what: str, result: dict) -> dict:
    """A result that says ok, against the schema of what its job returns: `$defs/result.<fn>` (or sources_reset's)."""
    assert result["ok"] is True and "result" in result, (what, result)
    part = {"$ref": c.load(RESULT)["$id"] + f"#/$defs/result.{what}"}
    v = Draft202012Validator(part, registry=c.registry())
    assert [e.message for e in v.iter_errors(result["result"])] == [], (what, result)
    doc = c.load(RESULT)
    assert c._undeclared(part, c.registry().resolver(doc["$id"]), result["result"], "$.result") == [], what
    return result


def fn_of(job: dict) -> str:
    return job["args"]["fn"] if job["type"] == "call" else job["type"]


# -- the wire ------------------------------------------------------------------------------------------------------------

class Wire:
    """The two job routes of this hub, as bytes: seen = [(method, path, query, request, status, answer)], in the order
    the hub answered. newer: a function every poll answer with a job in it goes through first (what a newer hub would
    send). instead: raw answers, each given once in place of the hub's next poll answer. refuse: (status, body) given
    to every result in place of the hub's answer (the hub never sees those results)."""

    def __init__(self, lv):
        self.h = lv.hub.app.hub
        self.seen: list[tuple] = []
        self.newer = None
        self.instead: list[bytes] = []
        self.refuse: tuple[int, dict] | None = None
        real = self.h.handle

        def handle(method, path, query, headers, body):
            if not path.startswith(ROUTE):
                return real(method, path, query, headers, body)
            got: list[bytes] = []

            def read(limit):
                raw = body(limit)
                got.append(raw)
                return raw
            if method == "POST" and self.refuse is not None:
                read(hub_mod.MAX_RESULT_BODY)
                out = (self.refuse[0], json.dumps(self.refuse[1]).encode(), "application/json")
            else:
                out = real(method, path, query, headers, read if body is not None else None)
            if method == "GET" and out[0] == 200 and json.loads(out[1])["jobs"]:
                if self.instead:
                    out = (200, self.instead.pop(0), out[2])
                elif self.newer is not None:
                    out = (200, json.dumps(self.newer(json.loads(out[1])), separators=(",", ":")).encode(), out[2])
            self.seen.append((method, path, query, got[0] if got else None, out[0], out[1]))
            return out
        self.h.handle = handle

    def give(self, raw: bytes | dict) -> None:
        """The station's poll, which is waiting now, is answered with this instead of anything the hub would say."""
        self.instead.append(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
        self.h.jobs.put("call", {"fn": "nothing", "args": []}, 5.0)          # (wakes the poll; never handed out)

    def results(self, since: int = 0) -> list[tuple]:
        return [x for x in self.seen[since:] if x[0] == "POST"]

    def exchanges(self, since: int = 0) -> list[tuple[dict, dict]]:
        """[(a job as the hub handed it out, the result the station posted for it)], each as parsed off the wire."""
        posted = {path.rsplit("/", 1)[1]: json.loads(req) for _m, path, _q, req, code, _a in self.results(since)
                  if code == 200}
        return [(job, posted[job["id"]]) for m, _p, _q, _r, code, ans in self.seen[since:] if m == "GET" and code == 200
                for job in json.loads(ans)["jobs"] if job["id"] in posted]


def exchange(wire: Wire, do, n: int = 1, fns: tuple = ()) -> list[tuple[dict, dict]]:
    """do() makes the hub hand out n job(s); the (job, result) pairs once the station has answered them. fns: only
    jobs of these types or functions count (the hub also sends a station its channel choices, pre-alert sources and
    term lists by itself, whenever a status says they differ)."""
    mark = len(wire.seen)
    do()
    got = lambda: [x for x in wire.exchanges(mark) if not fns or fn_of(x[0]) in fns]   # noqa: E731
    assert wait(lambda: len(got()) >= n, 20), wire.seen[mark:]
    return got()[:n]


def a_voice(monkeypatch) -> bytes:
    """The station's voice is a stand-in (no Kokoro and no `say` in CI): a hundredth of a second of silence as a WAV,
    said to be by the voice the hub asked for. The WAV."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 160)

    def render(text, tones, out_dir, voice_cfg, repeat=2, spoken=None, log=None, local_fallback=True):
        path = Path(out_dir) / "dispatch.wav"
        path.write_bytes(buf.getvalue())
        return path, voice_cfg.label()
    monkeypatch.setattr(dispatcher, "render_dispatch", render)
    return buf.getvalue()


class Feed:
    """An incident-source adapter that reads nothing: a source to poll now and to reset."""
    source, label, rank, poll_s = "east-cad", "East CAD", 1, 30.0

    def poll(self):
        return []

    def reset(self, origin=""):
        pass


def segment(st) -> str:
    """One transmission on the fireground channel, three seconds of voice. Its id."""
    now = time.time()
    st._segment(FM, Segment(now, now + 3.0, voice(3.0), 20.0, []))
    return f"Fireground1-{int(now * 1000)}"


def a_station(lv, monkeypatch):
    """A real station on this hub: its whisper says the same words for every clip, its voice is a_voice()'s, and it
    has one incident source."""
    a_voice(monkeypatch)
    st = lv.station()
    st.sources_runner = SourceRunner([Feed()], lambda kind, data: None, log=lambda m: None, role="station", env={})
    assert wait(lambda: lv.hub.app.hub.connected() and lv.hub.app.hub.status is not None, 30)
    return st


def every_job_the_hub_makes(lv, st, wire, tmp_path, monkeypatch) -> dict[str, tuple[dict, dict]]:
    """Each kind of job and each function of CALLS, in every form the hub sends, made by the hub's own callers and run
    by the station: {form: (the job, its result)} as they went over the wire."""
    app, h = lv.hub.app, lv.hub.app.hub
    out: dict[str, tuple[dict, dict]] = {}

    def one(form: str, do) -> None:
        out[form] = exchange(wire, do, fns=(form.split("-")[0],))[0]

    # the catch-up of a clip that was skipped while whisper was behind (sizeup/sttcatchup.py): the hub's own driver
    tid = shed_one(st, monkeypatch, segment=segment)
    assert wait(lambda: [r["id"] for r in app.cad.comms.shed_clips(time.time() - 3600)[0]] == [tid], 15)
    st.stt.client = Whisper({3: "engine one on scene"})
    monkeypatch.setattr(stt_mod, "CATCHUP_QUIET_S", 0.0)
    st.whisper.state = "ok"
    tick = lambda: (st.link.live_changed(), app.catchup.tick(), app.catchup.stats()["sent"])[2] == 1    # noqa: E731
    one("catch_up", lambda: wait(tick, 30))                 # (a tick or a few: once its status says whisper is idle)
    row = lambda: app.cad.comms.store.one("SELECT status, rerun_result FROM radio_traffic WHERE id = ?",    # noqa: E731
                                          (tid,))
    assert wait(lambda: row()["status"] == "done" and row()["rerun_result"], 20)
    # a person's Re-transcribe on the hub; every recording; the alternative of a channel on both engines; and Amazon
    # Transcribe could not read a live clip: the station's whisper, now. (Each re-run is over before the next is asked.)
    for form, do in (("retranscribe", lambda: app.retranscribe([tid])),
                     ("retranscribe-all", lambda: app.retranscribe(None)),
                     ("retranscribe-as-alt", lambda: app._rerun_station([tid], True)),
                     ("transcribe_now", lambda: app._aws_failed(AwsJob("traffic", tid, FM.label), "AWS said no"))):
        assert wait(lambda: not st._rerun_ids, 20)
        one(form, do)
    # the monitor's switches
    one("set_mode", lambda: app.set_mode("Fireground 1"))
    one("set_mode-confirmed", lambda: app.set_mode("Fireground 1", "00000101", confirm=True))
    one("set_talkgroup", lambda: app.set_talkgroup(4101, True, None))
    one("set_talkgroup-tier", lambda: app.set_talkgroup(7001, None, None, "alert", "east"))
    one("set_talkgroup_group", lambda: app.set_talkgroup_group("Fire", True, None))
    one("emergency_all_clear", lambda: app._all_clear_sent({"id": "4101-1794990000-call_1", "talkgroup": 4101,
                                                            "emergency": True, "radio_id": "990001, 990002"}))
    one("set_adsb_receiver", lambda: app.set_adsb_receiver(None))
    groups = [{"name": "roof", "sdrs": ["00000101", "00000102"], "jobs": ["Paging 1", "Fireground 1"]}]
    one("set_antenna_groups", lambda: app.set_antenna_groups(groups))
    one("set_antenna_groups-confirmed", lambda: app.set_antenna_groups(groups, "Stop Fireground 1 to save this?"))
    one("sources_poll_now", lambda: app._sources_poll_now("radio pre-alert"))
    one("reception_post", lambda: app.reception_post({"action": "start", "system": "east", "jobs": ["East 1"],
                                                      "sdr": "00000105", "minutes": 15}, "admin@example.org"))
    one("reception_sample_b64", lambda: app.reception_sample("0123456789ab"))
    # the pre-alerts' sources, when the station's revision differs from the intel engine's
    # (held for the rest of the run: the cache's 5 s running out would send the engine's own set again, a race)
    monkeypatch.setattr(app, "_stt_alert_source", lambda: (frozenset({"fm:Paging 1", "p25:east:7001"}), 4242))
    one("set_stt_alert_keys", app.alert_keys_check)
    assert wait(lambda: not app._alert_push, 5)
    # the term lists for whisper's prompt, when the station's version differs
    (tmp_path / "terms").mkdir()
    (tmp_path / "terms" / "county_fire.txt").write_text(TERMS, encoding="utf-8")
    app.cfg = replace(app.cfg, stt_terms_whisper=True)
    app.stt_terms = stt_terms.TermStore(stt_terms.DirFiles(tmp_path / "terms"), log=lambda m: None)
    app.stt_terms.refresh()
    one("set_stt_terms", app.stt_terms_check)
    # Settings > Radios > Channels on the hub: a channel's choice, a named tone pair and a squelch; the push that has
    # all three is the last one, once the station reports what the hub wants
    who = {"operator": "admin@example.org"}
    mark = len(wire.seen)
    for body in (("channels", {"action": "set", "key": "Fireground 1", "transcribe": False, **who}),
                 ("tones", {"action": "set", "a_hz": 832.5, "b_hz": 1201.0, "name": "Southfield FD", **who}),
                 ("squelch", {"action": "set", "key": "Fireground 1", "open_db": 13, "min_open_s": 0.5, **who})):
        app.cad.settings.post(*body)
    assert wait(lambda: not app._chan_push["busy"] and not app._chan_push["again"]
                and (h.status or {}).get("channels", {}).get("sig") == app.channel_desired()["sig"], 20)
    out["set_channel_prefs"] = [x for x in wire.exchanges(mark) if fn_of(x[0]) == "set_channel_prefs"][-1]
    # the dispatch voice, and an admin's reset of the incident sources
    one("tts", lambda: app.cad._render_remote("Engine 1 respond to 12 Example Street for a reported alarm",
                                              [[716.1, 1.0], [600.9, 3.0]]))
    one("sources_reset", lambda: h.reset_source("", "admin@example.org"))
    # Health's Dismiss on an alarm the hub took too late (#762): the station drops its late one, the one it still sends
    # again stays
    ob = st.link.outbox
    for k in range(2):
        q = ob.quarantine({"seq": 900001 + k, "at": c.T0, "type": "alarm", "audio": None, "audio_dropped": False,
                           "data": {"id": f"Fireground1-179499000{k}000"}}, "apply_failed", "RuntimeError")
    ob.late(q["id"], "alarm delivered late, not raised (40 min after it ended)")
    one("quarantine_dismiss", lambda: h.dismiss_rejected("alarm", "admin@example.org"))
    assert [(r["kind"], r["state"]) for r in ob.summary()["rows"]] == [("alarm", "held")]
    # the hub's pool takes a clip back from the station's whisper (#927): one whose text went up long ago, so "sent"
    one("stt_withdraw", lambda: app.stt_withdraw_station([tid]))
    return out


FORMS = ("catch_up", "retranscribe", "retranscribe-all", "retranscribe-as-alt", "transcribe_now", "set_mode",
         "set_mode-confirmed", "set_talkgroup", "set_talkgroup-tier", "set_talkgroup_group", "emergency_all_clear",
         "set_adsb_receiver", "set_antenna_groups", "set_antenna_groups-confirmed", "sources_poll_now", "reception_post",
         "reception_sample_b64", "set_stt_alert_keys", "set_stt_terms", "set_channel_prefs", "tts", "sources_reset",
         "quarantine_dismiss", "stt_withdraw")


def scrub(doc, tmp=""):
    """A recorded job or result with what belongs to this run replaced by fixed made-up values: the job's id; the
    clip's id and the times in the catch-up's row; the reception test's id; the units the dispatch voice is told how to
    say (the hub's own list). Then nothing in it may name this machine or run."""
    def words(o):
        if isinstance(o, dict):
            return {words(k): words(v) for k, v in o.items()}
        if isinstance(o, list):
            return [words(v) for v in o]
        return re.sub(r"Fireground1-\d{13}", CLIP, o) if isinstance(o, str) else o
    doc = words(doc)
    for job in doc.get("jobs", []):
        job["id"] = c.HEX16
        if job["type"] == "tts":
            assert job["args"]["spoken"] and all(isinstance(v, str) for v in job["args"]["spoken"].values())
            job["args"]["spoken"] = {"E1": "Engine 1", "L2": "Ladder 2"}
        elif fn_of(job) == "catch_up":
            for spec in job["args"]["args"][0]:
                spec.update(start=c.T0, seconds=round(spec["seconds"], 1))
        elif fn_of(job) == "reception_post":
            assert re.fullmatch(r"[0-9a-f]{12}", job["args"]["args"][2])
            job["args"]["args"][2] = c.HEX16[:12]
    text = json.dumps(doc)
    mine = [str(tmp), "pytest", "/private/", "/Users/", "/home/"]
    if os.environ.get(c.RECORD_ENV, "") not in ("", "0"):
        import getpass
        import socket
        mine += [socket.gethostname(), getpass.getuser()]
    for word in mine:
        assert len(word) < 3 or word not in text, f"the recording still names this machine or run: {word!r}"
    return doc


@pytest.fixture
def live(tmp_path):
    lv = c.Live(tmp_path)
    yield lv
    lv.close()


# -- as built: the hub's own jobs, a real station's results ---------------------------------------------------------------

def test_the_recorded_jobs_and_results_are_what_the_hub_and_the_station_send_now(live, tmp_path, monkeypatch):
    """Every job type and every function of CALLS, in every form the hub sends, goes over the wire: made by the hub's
    own callers, run by a real station. Each poll answer, each result and each answer to a result is strict JSON and
    conforms, with no key the schemas do not name; and it is, to the byte of every value, the recorded vector
    (contracts/vectors/station-jobs/valid/<form>.json and station-job-result/valid/<form>.json, written anew by
    `python -m tests.contracts record`)."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    got = every_job_the_hub_makes(live, st, wire, tmp_path, monkeypatch)
    assert tuple(got) == FORMS
    for method, path, query, req, code, ans in wire.seen:
        if method == "GET":
            assert code == 200 and query == f"wait={station_mod.JOB_WAIT_S}" and req is None
            conforms(JOBS, ans)
        else:
            assert code == 200 and re.fullmatch(ROUTE + "/[0-9a-f]{16}", path)
            conforms(RESULT, req)
            assert conforms(ANSWER, ans) == {"ok": True}
    assert len(wire.exchanges()) == len(wire.results()) >= len(FORMS)       # every job was answered, once
    for form, (job, result) in got.items():
        result_conforms(fn_of(job), result) if job["type"] != "tts" else None
        for name, now in ((JOBS, {"jobs": [job]}), (RESULT, result)):
            now = scrub(now, tmp_path)
            committed = c.recorded(f"{name}/valid/{form}.json", now)
            conforms(name, committed)
            assert committed == now, (name, form)
    # what the station did with them: every type, every function, every length of argument list the schema lists
    jobs = [job for job, _ in got.values()]
    assert {j["type"] for j in jobs} == set(c.load(JOBS)["$defs"]["job"]["properties"]["type"]["x-known"])
    sent: dict[str, set[int]] = {}
    for j in jobs:
        if j["type"] == "call":
            sent.setdefault(j["args"]["fn"], set()).add(len(j["args"]["args"]))
    assert sent == {fn: set(c.load(JOBS)["$defs"][f"call.{fn}"]["x-forms"]) - ({3} if fn == "set_channel_prefs" else set())
                    for fn in station_mod.CALLS}                             # (its 3: an older station, tested below)
    r = {form: result for form, (_, result) in got.items()}
    assert r["catch_up"]["result"] == {"queued": [got["catch_up"][0]["args"]["args"][0][0]["id"]], "refused": {}, "hold": ""}
    assert [r[f]["result"] for f in ("retranscribe", "retranscribe-all", "retranscribe-as-alt", "transcribe_now",
                                     "sources_poll_now")] == [1, 1, 1, 1, 1]
    assert r["set_channel_prefs"]["result"]["squelch"] == {"Fireground 1": {"open_db": 13.0, "min_open_s": 0.5}}
    assert r["set_stt_alert_keys"]["result"] == {"ok": True, "rev": 4242} == {"ok": True, "rev": st.stt_alert_rev}
    assert r["set_stt_terms"]["result"]["v"] == live.hub.app.stt_terms.v != ""
    assert r["tts"]["label"].startswith("Voice: ") and r["sources_reset"]["result"].startswith("east-cad: reset asked")
    assert r["quarantine_dismiss"]["result"] == 1
    assert r["stt_withdraw"]["result"] == {"withdrawn": [], "running": [], "sent": [got["stt_withdraw"][0]["args"]["args"][0][0]],
                                           "unknown": []}               # (its text went up long ago)
    # the hub puts the channels' result in the status it has (and the station's next status says the same)
    assert wait(lambda: live.hub.app.hub.status["channels"] == r["set_channel_prefs"]["result"], 20)


def test_the_dispatch_voice_comes_back_as_a_wav_in_base64(live, monkeypatch):
    """`tts`: the station's WAV goes up as standard base64 in `audio`, whatever its sample rate (nothing of it is
    changed or checked on the way but the first four bytes, RIFF); the hub keeps the bytes as they came."""
    import base64
    wire = Wire(live)
    wav = a_voice(monkeypatch)
    a_station(live, monkeypatch)
    job, result = exchange(wire, lambda: live.hub.app.cad._render_remote("Engine 1 respond", [[716.1, 1.0]]))[0]
    assert base64.b64decode(result["audio"], validate=True) == wav and result["audio"].startswith("UklGR")
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
    assert job["args"]["tones"] == [[[716.1, 1.0]]] and job["args"]["voice"]["engine"] == live.hub.app.cad.s.tts_engine
    path, label = live.hub.app.cad._render_remote("Engine 1 respond", [[716.1, 1.0]])    # (now from the hub's cache)
    assert path.read_bytes() == wav and label == result["label"] and len(wire.results()) == 1


# What a change in the code would put on the wire: each alone must fail the conformance check. These edit a recorded
# job or result, not the code; a real change in the code is caught by the test above, which compares whole bodies.
JOB = "/jobs/0/args"
add = lambda path, value: {"op": "add", "path": path, "value": value}            # noqa: E731
put = lambda path, value: {"op": "replace", "path": path, "value": value}        # noqa: E731
cut = lambda path: {"op": "remove", "path": path}                                # noqa: E731
MUTATIONS = {
    ("set_mode", "a new key on a job"): [add("/jobs/0/priority", 1)],
    ("set_mode", "a new key beside fn and args"): [add(JOB + "/kwargs", {})],
    ("set_mode", "a job without args"): [cut(JOB)],
    ("set_mode", "an id of 32 characters"): [put("/jobs/0/id", c.HEX16 * 2)],
    ("set_adsb_receiver", "a second argument"): [add(JOB + "/args/-", True)],
    ("set_talkgroup", "main as a word"): [put(JOB + "/args/2", "yes")],
    ("set_channel_prefs", "a new key in the word on AWS"): [add(JOB + "/args/1/budget", 1)],
    ("set_channel_prefs", "a new choice of a channel"): [add(JOB + "/args/0/Fireground 1/gain", 1)],
    ("set_channel_prefs", "a new squelch number"): [add(JOB + "/args/3/Fireground 1/hang_s", 1)],
    ("catch_up", "a new key in the catch-up's row"): [add(JOB + "/args/0/0/model", "x")],
    ("catch_up", "a row without its key"): [cut(JOB + "/args/0/0/key")],
    ("set_stt_terms", "a new key in a term list"): [add(JOB + "/args/0/sets/county_fire/lang", "en")],
    ("set_stt_terms", "terms as one string"): [put(JOB + "/args/0/sets/county_fire/terms", "Engine one")],
    ("set_stt_alert_keys", "the revision as a float"): [put(JOB + "/args/1", 4242.5)],
    ("tts", "a new key in the voice"): [add(JOB + "/voice/pitch", 1)],
    ("tts", "a new argument of tts"): [add(JOB + "/lang", "en")],
    ("tts", "repeat as a string"): [put(JOB + "/repeat", "2")],
    ("sources_reset", "a new argument of sources_reset"): [add(JOB + "/origin", "hub")],
}
RESULT_MUTATIONS = {
    ("catch_up", "a new key in the catch-up's result"): [add("/result/later", [])],
    ("catch_up", "refused as a list"): [put("/result/refused", [])],
    ("set_channel_prefs", "a new key in the channels applied"): [add("/result/new_flag", True)],
    ("set_channel_prefs", "channels applied without sig"): [cut("/result/sig")],
    ("set_stt_alert_keys", "rev as a string"): [put("/result/rev", "4242")],
    ("set_stt_terms", "a new key in the terms applied"): [add("/result/places", True)],
    ("retranscribe", "a count as a string"): [put("/result", "1")],
    ("set_mode", "an error as an object"): [put("/result", {"error": "no"})],
    ("emergency_all_clear", "closed as a number"): [put("/result", 1)],
    ("reception_sample_b64", "a sample that is no WAV"): [put("/result", "T2dnUw==")],
    ("sources_reset", "the reset's word as a list"): [put("/result", ["east-cad"])],
    ("tts", "a new key beside label and audio"): [add("/seconds", 4.2)],
}


def test_a_change_in_what_the_code_sends_fails_the_check():
    """The check on itself: a recorded job or result, changed the way a change in the code would change it (a key
    added, a key gone, a type changed, an argument more), no longer conforms. Each alone."""
    jobs, results = dict(c.vectors(JOBS, "valid")), dict(c.vectors(RESULT, "valid"))
    for (form, what), ops in MUTATIONS.items():
        conforms(JOBS, jobs[f"{form}.json"])
        with pytest.raises(AssertionError):
            conforms(JOBS, c.patched(jobs[f"{form}.json"], ops))
            pytest.fail(f"not caught: {what}", pytrace=False)
    for (form, what), ops in RESULT_MUTATIONS.items():
        real = conforms(RESULT, results[f"{form}.json"])
        if form != "tts":
            result_conforms(form, real)
        with pytest.raises(AssertionError):
            changed = conforms(RESULT, c.patched(real, ops))
            result_conforms(form, changed)
            pytest.fail(f"not caught: {what}", pytrace=False)
    for form in FORMS:                                               # (and every recorded result is its function's)
        if form != "tts":
            result_conforms(form.split("-")[0], results[f"{form}.json"])


# -- the schemas against the code's own lists ------------------------------------------------------------------------------

def test_the_functions_and_their_argument_names_are_the_codes():
    """`call` is positional on the wire. The schema names every function of CALLS and, per position, the parameter of
    the App's method it lands on (`x-names`): the names a later form with named arguments would use. Checked against
    the signatures: every parameter, in order; the list is closed at the last one; `minItems` is the number of
    parameters without a default."""
    defs = c.load(JOBS)["$defs"]
    assert defs["call"]["properties"]["fn"]["x-known"] == list(station_mod.CALLS) and len(station_mod.CALLS) == 17
    assert [b["if"]["properties"]["fn"]["const"] for b in defs["call"]["allOf"]] == list(station_mod.CALLS)
    assert {k for k in defs if k.startswith("call.")} == {f"call.{fn}" for fn in station_mod.CALLS}
    results = c.load(RESULT)["$defs"]
    assert set(results) == {f"result.{fn}" for fn in station_mod.CALLS} | {"result.sources_reset"}
    for fn in station_mod.CALLS:
        params = list(inspect.signature(getattr(App, fn)).parameters.values())[1:]
        assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in params), fn
        d = defs[f"call.{fn}"]
        assert d["x-names"] == [p.name for p in params], fn
        assert len(d["prefixItems"]) == d["maxItems"] == len(params), fn
        assert d["minItems"] == sum(p.default is p.empty for p in params), fn
        assert d["x-forms"] == sorted(d["x-forms"]) and d["minItems"] <= d["x-forms"][0] and d["x-forms"][-1] <= d["maxItems"]
        assert all(i.get("description") for i in d["prefixItems"]), fn


def test_the_job_types_and_the_words_in_the_arguments_are_the_codes():
    from sizeup import channels, tiers
    from sizeup.cloud.adapters import SOURCE_ID
    from sizeup.radio import reception
    defs = c.load(JOBS)["$defs"]
    hub_src = inspect.getsource(hub_mod.Hub)
    assert sorted(set(re.findall(r'self\.call\("(\w+)"', hub_src))) == sorted(defs["job"]["properties"]["type"]["x-known"])
    station_src = inspect.getsource(station_mod.StationLink._run_job)
    assert re.findall(r'kind == "(\w+)"', station_src) == defs["job"]["properties"]["type"]["x-known"]
    assert defs["job"]["properties"]["id"]["pattern"] == "^" + hub_mod.JOB_PATH.pattern.split("(")[1].split(")")[0] + "$"
    prefs = defs["call.set_channel_prefs"]["prefixItems"]
    assert prefs[0]["additionalProperties"]["properties"]["engine"]["x-known"] == list(channels.ENGINES)
    assert list(prefs[0]["additionalProperties"]["properties"]) == list(channels.FIELDS)
    assert list(prefs[3]["additionalProperties"]["properties"]) == list(channels.SQUELCH)
    assert defs["call.set_talkgroup"]["prefixItems"][3]["anyOf"][0]["x-known"] == list(tiers.TIERS)
    assert defs["sources_reset"]["properties"]["source"]["pattern"] == f"^({SOURCE_ID.pattern})?$"
    assert defs["call.reception_post"]["prefixItems"][2]["pattern"] == f"^({reception.SAMPLE_ID.pattern})?$"
    assert defs["call.set_stt_terms"]["prefixItems"][0]["properties"]["sets"]["additionalProperties"]["properties"][
        "terms"]["maxItems"] == stt_terms.Pushed.MAX_PUSHED
    assert defs["tts"]["properties"]["text"]["maxLength"] == 4000 and sttcatchup.WHISPER_BATCH == 3
    assert (hub_mod.TTS_WAIT_S, hub_mod.CALL_WAIT_S, hub_mod.MAX_RESULT_BODY) == (20.0, 12.0, 16 * 1024 * 1024)
    assert (station_mod.JOB_WAIT_S, hub_mod.CATCHUP_REFUSED_S) == (25, 600.0)


def test_wait_is_seconds_cut_to_0_to_30_and_anything_else_is_0(live):
    """The query of the long poll: `wait` as a decimal number of seconds, held at most 30; missing, empty or no
    number: 0 (answered at once). And the hub does hold the request that long with nothing to hand out."""
    h = live.hub.app.hub
    asked: list[float] = []
    real = h.jobs.take
    h.jobs.take = lambda wait: asked.append(wait) or []
    for query in ("?wait=25", "?wait=0.25", "?wait=1e9", "?wait=-5", "?wait=soon", "?wait=", "", "?from_the_future=1"):
        assert live.call("GET", ROUTE + query) == (200, {"jobs": []}), query
    assert asked == [25.0, 0.25, 30.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    h.jobs.take = real
    t0 = time.monotonic()
    assert live.call("GET", ROUTE + "?wait=0.4") == (200, {"jobs": []}) and 0.35 <= time.monotonic() - t0 < 5
    assert conforms(JOBS, {"jobs": []}) == dict(c.vectors(JOBS, "valid"))["no-jobs.json"]


def test_what_the_hub_answers_to_a_result(live):
    """200 {ok} for a job that is waiting; 404 when there is none by that id; 400 for a body that is JSON but no
    object, or no JSON; 413 over 16 MiB (the web server hands the hub no body then; a client still sending one that
    large usually sees the connection close before it reads the answer). The refusals are station-error objects with
    `error` alone; an id that is not 16 lower-case hex is no route at all."""
    h = live.hub.app.hub
    job = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 60.0)
    path = f"{ROUTE}/{job['id']}"
    for body, code, error in (([1], 400, "a JSON object"), ("ok", 400, "a JSON object"), (None, 200, None)):
        got = live.call("POST", path, body) if body is not None else live.call("POST", path, {"ok": True, "result": None})
        assert got[0] == code and (got[1] == {"ok": True} if code == 200 else got[1] == {"error": error}), (body, got)
        if code != 200:
            assert c.errors("station-error", got[1]) == [] and "reason" not in got[1]
    assert job["done"].is_set() and job["result"] == {"ok": True, "result": None}
    again = live.call("POST", path, {"ok": True, "result": None})                # the same result once more
    assert again == (404, {"error": "no such job (it timed out)"}) and c.errors("station-error", again[1]) == []
    job = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 60.0)
    path = f"{ROUTE}/{job['id']}"
    asked: list[int] = []
    code, raw, _ctype = h.handle("POST", path, "", {"Authorization": f"Bearer {live.own}"}, lambda limit: asked.append(limit))
    assert (code, json.loads(raw), asked) == (413, {"error": "too large"}, [16 * 1024 * 1024])
    assert c.errors("station-error", json.loads(raw)) == []
    import urllib.request
    req = urllib.request.Request(live.url + path, data=b'{"ok": tru', method="POST",
                                 headers={"Authorization": f"Bearer {live.own}", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    bad = json.loads(e.value.read())
    assert e.value.code == 400 and bad["error"].startswith("bad request: ") and c.errors("station-error", bad) == []
    assert not job["done"].is_set()                                              # none of those reached the caller
    for odd in ("0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0", "None"):
        assert live.call("POST", f"{ROUTE}/{odd}", {"ok": True}) == (404, {"error": "not found"})
    assert live.call("POST", path, {})[0] == 200 and job["result"] == {}        # an empty object is a result: not ok


# -- the compatibility rule on the real readers --------------------------------------------------------------------------

def test_the_station_runs_a_newer_hubs_jobs_the_same(live, monkeypatch):
    """The station as the reader: every recorded job, handed out by the hub with a key from a newer build in the
    answer, in the job, in its `args`, and in every object inside them the schema describes, is run exactly as
    without: the same result. And a newer hub's job type and function are answered, with the texts the hub knows."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    h = live.hub.app.hub
    for own in ("channels_check", "alert_keys_check", "stt_terms_check"):     # (the hub sends nothing by itself here:
        monkeypatch.setattr(live.hub.app, own, lambda *a, **k: None)         # only the recorded jobs change the station)
    added, ran = 0, 0

    def run(args_of: dict) -> dict:
        feed = st.sources_runner.runs[0]
        feed.triggered, feed.triggers = float("-inf"), []            # (its adapter may be woken again at once)
        job = h.jobs.put(args_of["type"], args_of["args"], 30.0)
        assert job["done"].wait(20), args_of
        return job["result"]
    for file, answer in c.vectors(JOBS, "valid"):
        for job in answer["jobs"]:
            wire.newer = None
            plain = run(job)

            def newer(ans):
                nonlocal added
                ans, n = with_unknown_keys(JOBS, ans)
                added += n
                return ans
            wire.newer = newer
            assert run(job) == plain, file
            ran += 1
    assert ran >= len(FORMS) + 5 and added > 100                    # (the answer, the job, its args, and inside them)
    sent = [json.loads(a) for m, _p, _q, _r, _c, a in wire.seen if m == "GET" and b"from_the_future" in a]
    paths = {p for ans in sent for p in undeclared(JOBS, ans)}
    assert {"$.from_the_future", "$.jobs[0].from_the_future", "$.jobs[0].args.from_the_future",
            "$.jobs[0].args.voice.from_the_future", "$.jobs[0].args.args[0].Fireground 1.from_the_future",
            "$.jobs[0].args.args[1].from_the_future", "$.jobs[0].args.args[3].Fireground 1.from_the_future",
            "$.jobs[0].args.args[0][0].from_the_future", "$.jobs[0].args.args[0].from_the_future",
            "$.jobs[0].args.args[0].sets.county_fire.from_the_future"} <= paths, sorted(paths)
    assert all(c.errors(JOBS, ans) == [] for ans in sent)
    wire.newer = None
    # words it does not know: answered, never run, never a thread lost
    with pytest.raises(RuntimeError, match=r"^this station doesn't know job 'from_the_future'$"):
        h.call("from_the_future", {"x": 1}, 10.0)
    with pytest.raises(RuntimeError, match=r"^not allowed from the hub: 'from_the_future'$"):
        h.remote("from_the_future", 1, 2, timeout=10.0)
    assert st.link.last_error == "" and all(t.is_alive() for t in st.link._threads)
    for _m, _p, _q, req, code, _a in wire.results():
        assert code == 200
        conforms(RESULT, req)


class Poller:
    """A station that only long-polls and answers: each job with answer(job) -> a result. jobs: what it was handed."""

    def __init__(self, lv, answer):
        self.lv, self.answer, self.jobs, self.stop = lv, answer, [], threading.Event()
        assert lv.call("GET", ROUTE + "?wait=0") == (200, {"jobs": []})         # (the hub has heard from it)
        self.thread = threading.Thread(target=self._run, name="contracts-poller", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            code, body = self.lv.call("GET", ROUTE + "?wait=0.2")
            for job in (body.get("jobs") or []) if code == 200 else []:
                self.jobs.append(job)
                res = self.answer(job)
                if res is not None:
                    self.lv.call("POST", f"{ROUTE}/{job['id']}", res)

    def close(self):
        self.stop.set()
        self.thread.join(10)


def test_the_hub_takes_a_newer_stations_results(live, tmp_path):
    """The hub as the reader: every recorded result with a key from a newer build at the top and in every object
    inside it, answered to the hub's own callers, is read as without."""
    app, h = live.hub.app, live.hub.app.hub
    results = dict(c.vectors(RESULT, "valid"))
    added = 0

    def answer(job):
        nonlocal added
        res, n = c.with_unknown_keys(RESULT, results[f"{fn_of(job)}.json"])
        if isinstance(res.get("result"), dict):
            res["result"]["from_the_future"] = {"x": [1, {"y": None}]}
            n += 1
        added += n
        assert c.errors(RESULT, res) == []
        return res
    p = Poller(live, answer)
    try:
        wav, label = h.render_voice({"text": "Engine 1 respond"})
        assert wav[:4] == b"RIFF" and label == results["tts.json"]["label"]
        assert app._catchup_whisper([{"id": CLIP}]) == {"queued": [CLIP], "refused": {}, "hold": ""}
        assert app.retranscribe([CLIP]) == 1 and app.set_adsb_receiver("") is None
        assert h.reset_source("", "admin@example.org") == {"ok": True, "result": results["sources_reset.json"]["result"]}
        assert h.remote("set_stt_alert_keys", ["fm:Paging 1"], 4242) == {"ok": True, "rev": 4242,
                                                                         "from_the_future": {"x": [1, {"y": None}]}}
        assert live.call("POST", "/api/station/state", dict(c.vectors("station-state", "valid"))["with-status-plain.json"]
                         | {"at": time.time()})[0] == 200
        app.cad.settings.post("squelch", {"action": "set", "key": "Fireground 1", "open_db": 13, "min_open_s": 0.5,
                                          "operator": "admin@example.org"})
        assert wait(lambda: "from_the_future" in (h.status or {}).get("channels", {}), 10)   # kept whole, as it came
        assert live.people() == {path: 200 for path in (*c.PEOPLE_ROUTES, "/api/station/status")}
        assert added >= 7 and [fn_of(j) for j in p.jobs][:5] == ["tts", "catch_up", "retranscribe", "set_adsb_receiver",
                                                                  "sources_reset"]
    finally:
        p.close()


def test_the_hub_guesses_the_argument_list_from_the_stations_status(live):
    """D2 (contracts/README.md), as it shows in the jobs: nothing says which functions and how many arguments a
    station takes, so the hub reads it off the status. A station whose status has no `p25_systems` is sent the county's
    talkgroup as three arguments and no `p25:` channel key; one without `channels.squelch_ok` three arguments of
    set_channel_prefs; one without `channels.deny_ok` no `aws.deny`; one without `stt_alert_rev` is never sent the
    pre-alerts' sources. Each of those lists is a valid one (`x-forms`)."""
    app, h = live.hub.app, live.hub.app.hub
    p = Poller(live, lambda job: {"ok": True, "result": None})
    base = dict(c.vectors("station-state", "valid"))["with-status-plain.json"]

    def status(**change) -> None:
        st = {**base["status"], **{k: v for k, v in change.items() if v is not None}}
        for k, v in change.items():
            if v is None:
                st.pop(k)
        assert live.call("POST", "/api/station/state", {**base, "at": time.time(), "status": st})[0] == 200

    def args_of(fn: str, do) -> list:
        """The arguments of the first job of that function the hub hands out once do() has run."""
        assert wait(lambda: not app._chan_push["busy"] and not app._alert_push, 10)
        n = len(p.jobs)
        do()
        assert wait(lambda: any(fn_of(j) == fn for j in p.jobs[n:]), 10), (fn, p.jobs[n:])
        job = next(j for j in p.jobs[n:] if fn_of(j) == fn)
        conforms(JOBS, {"jobs": [job]})
        return job["args"]["args"]
    try:
        app._alert_push = False
        app._chan_ov = {"p25:east:7001": {"transcribe": False}, "Fireground 1": {"record": False}}
        app._sq_ov = {"Fireground 1": {"open_db": 13.0}}
        county = sourcekeys.COUNTY
        older = {**base["status"]["channels"], "sig": "0" * 12}
        older.pop("squelch_ok")
        before = {"p25_systems": None, "channels": older, "stt_alert_rev": None}      # a station from before all three
        # (the hub sends the channel choices by itself when a status says they differ: the status is part of do())
        new = args_of("set_channel_prefs", lambda: (status(), app.channels_check(force=True)))
        assert len(new) == 4 and set(new[0]) == {"p25:east:7001", "Fireground 1"}
        assert new[3] == {"Fireground 1": {"open_db": 13.0}}
        talkgroup = lambda: app.set_talkgroup(4101, True, None, None, county)       # noqa: E731
        assert args_of("set_talkgroup", talkgroup) == [4101, True, None, None, county]
        old = args_of("set_channel_prefs", lambda: (status(**before), app.channels_check(force=True)))
        assert len(old) == 3 and set(old[0]) == {"Fireground 1"} and "deny" not in old[1]
        assert {len(old), len(new)} == set(c.load(JOBS)["$defs"]["call.set_channel_prefs"]["x-forms"])
        assert args_of("set_talkgroup", talkgroup) == [4101, True, None]
        app._push_cache = (time.monotonic(), frozenset({"fm:Paging 1"}), 4242)
        n = len(p.jobs)
        app.alert_keys_check()
        time.sleep(0.3)
        assert not [j for j in p.jobs[n:] if fn_of(j) == "set_stt_alert_keys"]
        assert args_of("set_stt_alert_keys", lambda: (status(), app.alert_keys_check())) == [["fm:Paging 1"], 4242]
    finally:
        p.close()


def test_an_older_station_says_not_allowed_and_the_hub_falls_back_on_those_words(live, monkeypatch):
    """D2 again: a station from before a function answers `not allowed from the hub: '<fn>'`, and the hub's two
    fallbacks hang on the words `not allowed` in it: transcribe_now is sent again as retranscribe, and catch_up is not
    asked again for ten minutes."""
    monkeypatch.setattr(station_mod, "CALLS", tuple(x for x in station_mod.CALLS if x not in ("transcribe_now", "catch_up")))
    wire = Wire(live)
    a_station(live, monkeypatch)
    app = live.hub.app
    got = exchange(wire, lambda: app._aws_failed(AwsJob("traffic", CLIP, FM.label), "AWS said no"), 2)
    assert [(fn_of(j), j["args"]["args"]) for j, _ in got] == [("transcribe_now", [[CLIP]]), ("retranscribe", [[CLIP]])]
    assert got[0][1] == {"ok": False, "error": "not allowed from the hub: 'transcribe_now'"}
    assert got[1][1] == {"ok": True, "result": 0}
    assert app._catchup_refused_until == 0.0
    job, result = exchange(wire, lambda: app._catchup_whisper([{"id": CLIP}]))[0]
    assert result == {"ok": False, "error": "not allowed from the hub: 'catch_up'"}
    assert time.time() + 500 < app._catchup_refused_until < time.time() + 700
    for _m, _p, _q, req, _c, _a in wire.results():
        conforms(RESULT, req)
    assert "not allowed" in c.load(RESULT)["properties"]["error"]["description"]


def test_what_the_station_answers_to_arguments_of_the_wrong_type(live, monkeypatch):
    """As built: a job the station cannot run is answered with ok false and why, over the wire, and nothing of the
    station stops. Arguments that are no object (tts, call); `args` of a call that is no list; an argument missing;
    an argument of the wrong type, which the function itself refuses or takes as it can."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    h = live.hub.app.hub

    def run(kind: str, args) -> dict:
        job = h.jobs.put(kind, args, 30.0)
        assert job["done"].wait(20), (kind, args)
        return job["result"]
    assert run("tts", ["x"]) == {"ok": False, "error": "AttributeError: 'list' object has no attribute 'get'"}
    assert run("call", ["x"]) == {"ok": False, "error": "AttributeError: 'list' object has no attribute 'get'"}
    assert run("tts", {"text": 5}) == run("tts", {"text": "  "}) == run("tts", {"text": "x" * 4001}) \
        == {"ok": False, "error": "no dispatch text"}
    assert run("call", {"fn": "set_mode", "args": "Off"}) == {"ok": False, "error": "args: a list or an object"}
    assert run("call", {"fn": ["set_mode"], "args": []}) == {"ok": False, "error": "not allowed from the hub: ['set_mode']"}
    assert run("call", {"args": []}) == {"ok": False, "error": "not allowed from the hub: None"}
    missing = run("call", {"fn": "set_mode", "args": []})
    assert missing["ok"] is False and missing["error"].startswith("TypeError: ") and "'mode'" in missing["error"]
    refused = run("call", {"fn": "set_stt_alert_keys", "args": ["fm:Paging 1", 4242]})
    assert refused == {"ok": False, "error": "ValueError: keys: a list of source keys; rev: a number"}
    assert run("call", {"fn": "set_stt_terms", "args": [{"sets": {"Bad Name": {}}}]})["error"].startswith("ValueError: stt terms")
    assert run("call", {"fn": "set_channel_prefs", "args": [{"Fireground 1": {"engine": "from_the_future"}}]})["error"] \
        == "ValueError: engine: one of local, aws, both"
    assert run("call", {"fn": "set_antenna_groups", "args": [{"name": "roof"}]}) \
        == {"ok": True, "result": "each channel has its own dongle: nothing to group"}
    assert run("call", {"fn": "reception_post", "args": [["start"], "", ""]}) == {"ok": True, "result": "a JSON object"}
    assert run("sources_reset", {"source": "No Such Source", "by": 5}) == {"ok": False, "error": "source: a source id"}
    long = run("call", {"fn": "set_mode", "args": ["x" * 400, {"y": 1}]})
    assert long["ok"] in (True, False) and len(long.get("error", "")) <= 300
    assert st.link.last_error == "" and all(t.is_alive() for t in st.link._threads)
    for _m, _p, _q, req, code, _a in wire.results():
        assert code == 200
        conforms(RESULT, req)


# -- known deviations: pinned (the switches are at the top of this file) -------------------------------------------------

def test_known_deviation_a_job_is_handed_out_once_and_a_late_result_is_refused(live):
    """J1. Delivery is at most once, both ways. A job is marked taken the moment a poll's answer is written: if that
    answer never reaches the station (the connection drops, the station restarts just then), no later poll gets it,
    and the hub's caller waits out its time and says the station didn't answer. And a result that comes after that
    time is answered 404 and thrown away, although the station did the work. Fixed: a job is handed out again until
    its result is in or it expires, and a result for a job the hub has given up on is still taken (200)."""
    h = live.hub.app.hub
    assert live.call("GET", ROUTE + "?wait=0") == (200, {"jobs": []})
    said: list = []

    def caller():
        try:
            said.append(h.remote("set_adsb_receiver", "", timeout=0.6))
        except StationOffline as e:
            said.append(e)
    t = threading.Thread(target=caller, name="contracts-caller", daemon=True)
    t.start()
    assert wait(lambda: h.jobs.pending() == 1, 5)
    code, first = live.call("GET", ROUTE + "?wait=0")         # the poll whose answer is lost on the way
    assert code == 200 and [fn_of(j) for j in first["jobs"]] == ["set_adsb_receiver"]
    code, second = live.call("GET", ROUTE + "?wait=0")        # the station asks again (or has restarted)
    t.join(10)
    late = live.call("POST", f"{ROUTE}/{first['jobs'][0]['id']}", {"ok": True, "result": None})
    if J1_A_JOB_IS_HANDED_OUT_ONCE_AND_A_LATE_RESULT_IS_404:
        assert second == {"jobs": []}
        assert isinstance(said[0], StationOffline) and str(said[0]) == "the station didn't answer in 0.6 s"
        assert late == (404, {"error": "no such job (it timed out)"})
    else:
        assert second == first and late == (200, {"ok": True})


def test_known_deviation_a_result_is_tried_three_times_and_then_dropped(live, monkeypatch):
    """J2. The station posts a job's result at most three times (a 5xx or no answer: again after 1 s, then 2 s), then
    forgets it: nothing is kept for when the hub is back, so the hub's caller never learns that the work was done. A
    4xx is given up at once. Fixed: the result is kept and delivered once the hub answers again."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    h = live.hub.app.hub
    st.link.stop()                                            # (its waits between the tries return at once)
    job = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 60.0)
    handed = h.jobs.take(0.0)
    wire.refuse = (503, {"error": "updating", "updating": True})
    st.link._run_job(handed[0])
    tries = len(wire.results())
    wire.refuse = None
    if J2_A_RESULT_IS_DROPPED_AFTER_THREE_TRIES:
        assert tries == 3 and not job["done"].wait(0.5)
    else:
        assert job["done"].wait(10) and job["result"] == {"ok": True, "result": None}
    wire.refuse = (404, {"error": "no such job (it timed out)"})
    st.link._run_job(handed[0])
    assert len(wire.results()) == tries + 1                   # a 4xx: once, in either case


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")     # (today: the thread's own end)
@pytest.mark.parametrize("answer", [{"jobs": 5}, {"jobs": True}, [], 5, "jobs"], ids=repr)
def test_known_deviation_the_jobs_thread_ends_on_an_answer_of_the_wrong_type(live, monkeypatch, capsys, answer):
    """J3. The station does not handle a wrong type in the poll's answer: `jobs` that is a number or a boolean, or an
    answer that is no object, raises in StationLink._jobs outside its error handling, and the thread that polls ends.
    The station then takes no job (no switch from the hub, no dispatch voice, no re-run) until it restarts, and its
    other threads go on, so it still looks connected. Fixed (#782): that answer is left out and the polling goes on."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    h = live.hub.app.hub
    thread = next(t for t in st.link._threads if t.name == "hub-jobs")
    wire.give(answer)
    assert wait(lambda: any(m == "GET" and a == json.dumps(answer).encode() for m, _p, _q, _r, _c, a in wire.seen), 10)
    job = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 30.0)
    if J3_THE_JOBS_THREAD_ENDS_ON_A_WRONG_TYPE:
        thread.join(10)
        assert not thread.is_alive() and not job["done"].wait(0.4)
        assert all(t.is_alive() for t in st.link._threads if t is not thread) and st.link.connected()
        capsys.readouterr()                                        # (the thread's traceback, on stderr)
    else:
        assert job["done"].wait(20) and job["result"] == {"ok": True, "result": None}
        assert all(t.is_alive() for t in threading.enumerate() if t.name == "hub-jobs")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")     # (the four threads' own ends)
def test_a_job_that_is_no_object_is_skipped_and_the_ones_beside_it_run(live, monkeypatch, capsys):
    """Beside J3, as built: an entry of `jobs` that is no object ends only the thread started for it (an
    AttributeError, on stderr); the jobs beside it in the same answer run, and the polling goes on. A job without an
    `id` is run and its result posted to /api/station/jobs/None, which is no route (404)."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    h = live.hub.app.hub
    job = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 30.0)
    real = {"id": job["id"], "type": "call", "args": {"fn": "set_adsb_receiver", "args": [""]}}
    wire.instead.append(json.dumps({"jobs": [5, "x", None, ["y"], {"type": "call", "args": {"fn": "sources_poll_now"}},
                                             real]}).encode())
    assert job["done"].wait(20) and job["result"] == {"ok": True, "result": None}
    assert wait(lambda: any(p == ROUTE + "/None" and code == 404 for _m, p, _q, _r, code, _a in wire.results()), 10)
    again = h.jobs.put("call", {"fn": "set_adsb_receiver", "args": [""]}, 30.0)
    assert again["done"].wait(20) and all(t.is_alive() for t in st.link._threads)
    capsys.readouterr()


def test_known_deviation_an_argument_too_many_is_refused_with_a_type_error(live, monkeypatch):
    """J4. The arguments of `call` are positional, so the station cannot ignore one it does not know: a list one
    longer than the function takes is answered ok false with Python's TypeError, and nothing is done. That breaks
    rule 1 (what a reader does not know, it ignores), and it is why the hub has to guess a station's build from its
    status before it adds an argument (D2). Fixed: arguments by name (the names are in the schema, `x-names`), and a
    name the station does not know is ignored."""
    wire = Wire(live)
    a_station(live, monkeypatch)
    h = live.hub.app.hub
    assert h.remote("set_adsb_receiver", "", timeout=20.0) is None
    try:
        got = h.remote("set_adsb_receiver", "", "from_the_future", timeout=20.0)
    except RuntimeError as e:
        got = e
    job, result = wire.exchanges()[-1]
    assert len(job["args"]["args"]) == 2 > c.load(JOBS)["$defs"]["call.set_adsb_receiver"]["maxItems"]
    assert len(c.errors(JOBS, {"jobs": [job]})) == 1              # the schema refuses the list for that one rule
    if J4_AN_ARGUMENT_TOO_MANY_IS_A_TYPE_ERROR:
        assert isinstance(got, RuntimeError) and re.fullmatch(
            r"TypeError: (App\.)?set_adsb_receiver\(\) takes 2 positional arguments but 3 were given", str(got))
        assert result == {"ok": False, "error": str(got)}
    else:
        assert got is None and result == {"ok": True, "result": None}


def test_known_deviation_a_call_without_its_arguments_runs_on_everything(live, monkeypatch):
    """J5. A reader takes every key as optional, and here a missing one has a meaning of its own: a `call` whose
    `args` is absent, null or empty runs the function with its defaults, and retranscribe's default is every recording
    on the station's disk; a `sources_reset` whose `args` is no object is taken as {}: every source. A hub that
    dropped or mistyped the list would start both. Fixed: such a job is refused (ok false) and nothing runs."""
    wire = Wire(live)
    st = a_station(live, monkeypatch)
    st.stt.client.texts = {3: "engine one on scene"}
    h = live.hub.app.hub
    tid = segment(st)
    assert wait(lambda: any(t.id == tid and t.status == "done" for t in st.store.loose), 15)
    assert h.remote("retranscribe", ["Fireground1-1"], timeout=20.0) == 0       # (a clip it does not have: none)

    def run(kind: str, args) -> dict:
        job = h.jobs.put(kind, args, 30.0)
        assert job["done"].wait(20), (kind, args)
        return job["result"]
    got = []
    for args in ({"fn": "retranscribe"}, {"fn": "retranscribe", "args": None}, {"fn": "retranscribe", "args": []}):
        assert wait(lambda: not st._rerun_ids, 20)                   # (the re-run before is over)
        got.append(run("call", args))
    got.append(run("sources_reset", ["east-cad"]))
    if J5_NO_ARGUMENTS_MEANS_EVERYTHING:
        assert got[:3] == [{"ok": True, "result": 1}] * 3            # the one recording on its disk, each time
        assert got[3] == {"ok": True, "result": "east-cad: reset asked (runs before its next poll)"}
    else:
        assert all(r["ok"] is False and r["error"] for r in got)
    assert len(wire.results()) == 5


def test_known_deviation_the_hub_raises_on_a_wrong_type_in_a_result(live):
    """J6. The hub does not handle a wrong type in a job's result in two of its callers. `audio` of a `tts` result
    that is no string raises TypeError out of Hub.render_voice and out of Cad._render_remote, whose callers expect
    the dispatch to go out as text when the voice fails: a person's preview of a dispatch with audio gets no answer
    at all (the connection is dropped). `queued` of a catch_up result that is no list raises out of the catch-up's
    hand-over. Every other caller takes a wrong type as no answer. Fixed: both are taken as a station that sent
    nothing (text only; nothing queued)."""
    app, h = live.hub.app, live.hub.app.hub
    inc = app.cad.create({"type": "STRU", "address": "12 Example Street", "operator": "Lt. Example"})
    preview = lambda: live.call("POST", f"/api/cad/incident/{inc['id']}/preview",                # noqa: E731
                                {"unit_ids": [app.cad.store.units()[0]["id"]], "audio": True}, token=None)
    bad = {"tts": {"ok": True, "label": 5, "audio": 5},
           "catch_up": {"ok": True, "result": {"queued": 5, "refused": [], "hold": 5}}}
    p = Poller(live, lambda job: bad.get(fn_of(job), {"ok": True, "result": {"from_the_future": 1}}))
    try:
        assert c.errors(RESULT, bad["tts"]) != [] and h.connected()
        if J6_THE_HUB_RAISES_ON_A_WRONG_TYPE_IN_A_RESULT:
            with pytest.raises(TypeError):
                h.render_voice({"text": "Engine 1 respond"})
            with pytest.raises(TypeError):
                app.cad._render_remote("Engine 1 respond to 12 Example Street", [[716.1, 1.0]])
            with pytest.raises(TypeError):
                app._catchup_whisper([{"id": CLIP}])
            assert preview()[0] == 0                             # no answer: the request's connection is dropped
        else:
            with pytest.raises(AudioUnavailable):
                h.render_voice({"text": "Engine 1 respond"})
            code, answer = preview()
            assert code == 200 and answer["audio_url"] is None and "text only" in answer["audio_note"]
            assert app.cad._render_remote("Engine 1 respond to 12 Example Street", [[716.1, 1.0]])[0] is None
            assert app._catchup_whisper([{"id": CLIP}]) in (None, {"queued": [], "refused": {}, "hold": "5"})
        # the other callers, today and fixed: a wrong type is no answer, never an error of their own
        with pytest.raises(ValueError, match="speech-to-text runs on the station"):
            app.retranscribe([CLIP])
        assert app.reception_sample("0123456789ab") is None
        assert h.reset_source("", "admin@example.org")["ok"] is True
        bad["catch_up"] = {"ok": True, "result": [CLIP]}
        assert app._catchup_whisper([{"id": CLIP}]) is None
        bad["tts"] = {"ok": True, "label": {"x": 1}, "audio": "not base64!"}
        with pytest.raises(AudioUnavailable, match="the station sent no audio"):
            h.render_voice({"text": "Engine 1 respond"})
        code, answer = preview()                                 # (a wrong value of the right type: text only, said)
        assert code == 200 and answer["audio_url"] is None and "the station sent no audio" in answer["audio_note"]
    finally:
        p.close()


def test_each_job_deviation_is_listed_in_the_readme_and_pinned_here():
    """The J rows of contracts/README.md "Known deviations" and the tests above are the same set; each row has a
    severity and a file:line."""
    text = (c.ROOT / "README.md").read_text(encoding="utf-8")
    rows = {m[1]: m[0] for m in re.finditer(r"^\| (J\d+) \|.*$", text, re.M)}
    here = Path(__file__).read_text(encoding="utf-8")
    pinned = set(re.findall(r'def test_known_deviation_\w+\(.*\):\n    \"\"\"(J\d+)\. ', here))
    switches = set(re.findall(r"^(J\d+)_[A-Z0-9_]+ = (?:True|False) ", here, re.M))
    assert pinned == switches == {f"J{n}" for n in range(1, 7)}
    for d, row in rows.items():
        assert re.search(r"^\| J\d+ \| (low|medium|high) \| ", row), d
        assert re.search(r"`[a-z/_.]+\.py:\d+", row), f"{d}: file:line"
    assert set(rows) in (set(), pinned), "the README lists other job deviations than the tests pin"
