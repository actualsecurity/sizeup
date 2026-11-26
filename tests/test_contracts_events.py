"""contracts/ (ADR 0004): the body of POST /api/station/events and the hub's answer, as built. A real station with
everything the repo's fakes can give it is put through every path that queues an event (tests/contracts_lane.py
`everything`); what went over the wire to a real hub is checked against contracts/schemas/station-events.schema.json,
event type by event type, and each reader is sent keys it does not know. The cursor's rules are in
tests/test_contracts_events_cursor.py. Every id, name and address here is made up."""
from __future__ import annotations

import ast
import base64
import copy
import inspect
import json
import re
import sqlite3
import types
from pathlib import Path

import numpy as np
import pytest

import sizeup.app as appmod
import sizeup.cloud.station as station_mod
from sizeup.cad.dispatcher import wav_bytes
from sizeup.cloud import hub as hub_mod
from sizeup.radio.page import Page
from sizeup.radio.tones import ToneEvent
from tests import contracts as c
from tests import contracts_lane as lane
from tests.test_app import wait
from tests.test_contracts_events_cursor import E1_A_FAILED_EVENT_IS_ACKED_AND_GONE

ROOT = Path(__file__).resolve().parent.parent
DOC = c.load(lane.EVENTS)
DEFS = DOC["$defs"]
TX = DEFS["transmission"]["properties"]
KINDS = lane.types()

# -- known deviations (contracts/README.md "Known deviations"), pinned as the code is today ------------------------------
# The test below states today's behaviour and the fixed one. When a fix lands, flip its line here from True to False
# (nothing else) and mark the row in contracts/README.md fixed.
E4_THE_HUB_KEEPS_A_KEY_IT_DOES_NOT_KNOW = True    # fixed: the hub takes the keys the schema names and no other

# The recorded base vectors (contracts/vectors/station-events/valid/): each is ONE real event in the envelope it came
# in, cut to that event and scrubbed (tests/contracts_lane.py scrub). name -> (type, which event of the run).
RECORDED = {
    "heard.json": ("heard", lambda d, ids: d["radio_id"] == "7301"),
    "traffic-fm.json": ("traffic", lambda d, ids: d["id"] == ids["fm"]),
    "traffic-fm-cut-at-a-key-up.json": ("traffic", lambda d, ids: d["id"] == ids["fm_cut"]),
    "traffic-fm-emergency.json": ("traffic", lambda d, ids: d["id"] == ids["fm_after_live_alarm"]),
    "traffic-fm-for-amazon-transcribe.json": ("traffic", lambda d, ids: d["id"] == ids["fm_aws"]),
    "traffic-p25.json": ("traffic", lambda d, ids: d["id"] == ids["p25"]),
    "traffic-p25-grant-cut.json": ("traffic", lambda d, ids: d["id"] == ids["p25_grant_cut"]),
    "traffic-p25-emergency.json": ("traffic", lambda d, ids: d["id"] == ids["p25_emergency"]),
    "traffic-p25-stuck-flag.json": ("traffic", lambda d, ids: d["id"] == ids["p25_flag_2"]),
    "traffic-airband.json": ("traffic", lambda d, ids: d["id"] == ids["airband"]),
    "traffic-another-system.json": ("traffic", lambda d, ids: d["id"] == ids["context"]),
    "traffic-another-system-for-the-pool.json": ("traffic", lambda d, ids: d["id"] == ids["context_pool"]),
    "alarm-fm-at-the-key-up.json": ("alarm", lambda d, ids: d["id"] == ids["fm_live_alarm"]),
    "alarm-fm.json": ("alarm", lambda d, ids: d["id"] == ids["fm_after_live_alarm"]),
    "alarm-p25.json": ("alarm", lambda d, ids: d["id"] == ids["p25_emergency"]),
    "alarm-p25-stuck-flag.json": ("alarm", lambda d, ids: d["id"] == ids["p25_flag_2"]),
    "page.json": ("page", lambda d, ids: True),
    "page-text.json": ("page_text", lambda d, ids: True),
    "retext.json": ("retext", lambda d, ids: d["id"] == ids["fm"]),
    "retext-caught-up.json": ("retext", lambda d, ids: d.get("catchup") is True),
    "rerun.json": ("rerun", lambda d, ids: d["id"] == ids["fm"]),
    "alt-text.json": ("alt_text", lambda d, ids: True),
    "activity.json": ("activity", lambda d, ids: True),
    "source-event.json": ("source_event", lambda d, ids: True),
    "source-health.json": ("source_health", lambda d, ids: True),
    "source-seen.json": ("source_seen", lambda d, ids: True),
}
# ... and of the run of a station whose hub takes a transmission at its close (#927, lane.at_the_close: `ran_close`)
RECORDED_AT_CLOSE = {
    "traffic-fm-at-its-close.json": ("traffic", lambda d, ids: d["id"] == ids["fm"]),
    "retext-fill.json": ("retext", lambda d, ids: d["id"] == ids["fm"] and d.get("fill") is True),
    "retext-fill-to-the-pool.json": ("retext", lambda d, ids: d["id"] == ids["fm_pool"] and d.get("fill") is True),
}


_RUN: dict = {}


@pytest.fixture
def ran(tmp_path_factory, monkeypatch):
    """One run of the station with everything against a real hub, for the tests that only read what it left: every
    events post as it came and as it was answered, the clip ids, and what the hub then held. Made by the first test
    that asks, inside that test (so under conftest's guards, which are set per test), and kept for the others: the
    hub and the station are stopped before it is handed out."""
    if "run" not in _RUN:
        tmp = tmp_path_factory.mktemp("events")
        lv = lane.Lane(tmp)                 # (an older hub's caps: its transmissions in one event; see ran_close)
        try:
            st = lv.rich_station(monkeypatch)
            ids = lane.everything(lv, st, monkeypatch)
            hub = lv.hub.app
            held = {t.id: t.as_dict() for t in list(hub.store.loose) + [t for i in hub.store.incidents for t in i.traffic]}
            clips = {ev["data"]["id"]: hub.clip(ev["data"]["id"]) for ev in lv.events() if ev["audio"]}
            _RUN["run"] = types.SimpleNamespace(
                tmp=tmp, ids=ids, seen=list(lv.events_seen), events=lv.events(), held=held, clips=clips,
                errors=(hub.hub.errors, hub.hub.last_error), epoch=st.link.outbox.epoch)
        finally:
            lv.close()
    return _RUN["run"]


@pytest.fixture
def ran_close(tmp_path_factory, monkeypatch):
    """(#927) One run of a plain station whose hub's caps say "traffic_pending" (lane.at_the_close), as `ran`."""
    if "close" not in _RUN:
        tmp = tmp_path_factory.mktemp("events-close")
        lv = lane.Lane(tmp, answer_with=None)                          # (the hub's own caps: traffic_pending)
        try:
            st = lv.station()
            ids = lane.at_the_close(lv, st, monkeypatch)
            hub = lv.hub.app
            held = {t.id: t.as_dict() for t in list(hub.store.loose) + [t for i in hub.store.incidents for t in i.traffic]}
            _RUN["close"] = types.SimpleNamespace(
                tmp=tmp, ids=ids, seen=list(lv.events_seen), events=lv.events(), held=held,
                errors=(hub.hub.errors, hub.hub.last_error), epoch=st.link.outbox.epoch)
        finally:
            lv.close()
    return _RUN["close"]


def picked(ran, name: str) -> dict:
    """The post of the run that carried the event a recorded vector stands for, cut to that event."""
    kind, pick = RECORDED[name] if name in RECORDED else RECORDED_AT_CLOSE[name]
    for raw, code, _ in ran.seen:
        post = json.loads(raw)
        for ev in post["events"]:
            if code == 200 and ev["type"] == kind and pick(ev["data"], ran.ids):
                return dict(post, events=[ev])
    raise AssertionError(f"{name}: the run sent no such event")


# -- what a real station sends -----------------------------------------------------------------------------------------

def test_every_post_of_a_station_with_everything_is_what_the_schema_says(ran):
    """Every events post on the wire is strict JSON, valid, and names no key the schema does not, in the envelope, in
    each event and in each event's data by its type; every answer is what its schema says; the hub applied them all."""
    assert ran.errors == (0, "") and len(ran.events) >= 40     # (in however many posts: a slower machine batches more)
    for raw, code, answer in ran.seen:
        post = lane.conforms(raw)
        assert code == 200 and post["epoch"] == ran.epoch and re.fullmatch(r"[0-9a-f]{16}", post["epoch"])
        assert post["station"] == c.STATION
        ans = c.loads_strict(answer)
        assert c.errors(lane.ANSWER, ans) == [] and c.undeclared(lane.ANSWER, ans) == []
        assert set(ans) == {"ok", "acked", "applied"}
        assert ans["acked"] == post["events"][-1]["seq"] and ans["applied"] == len(post["events"])
    seqs = [ev["seq"] for ev in ran.events]
    assert seqs == list(range(1, len(seqs) + 1))               # from 1, by one, in the order they were queued
    # (qid: only on a quarantined event sent again, which this run has none of: tests/test_contracts_outbox.py O5)
    assert all(set(ev) == set(DEFS["event"]["properties"]) - {"qid"} for ev in ran.events)


def test_every_event_type_was_sent_and_every_key_of_every_type(ran, ran_close):
    """What makes the sub-schemas complete (`x-complete`): the run sends every event type the schema knows, and of
    each type every key its sub-schema names. For a transmission that is all 51 attributes of the Traffic object, each
    set by the path that sets it in the code (FM, a key-up cut, an emergency packet, P25, a P25 key-up cut at a mid-call
    grant, a P25 emergency with trunk-recorder's record, a stuck flag, the airband, another P25 system, the hub's
    engines)."""
    seen: dict[str, set] = {k: set() for k in KINDS}
    for ev in ran.events:
        seen[ev["type"]] |= set(ev["data"])
    for ev in ran_close.events:                                 # (#927: a fill's keys, and `engine` on every retext)
        if ev["type"] == "retext":
            seen["retext"] |= set(ev["data"])
    assert all(seen[k] for k in KINDS), [k for k in KINDS if not seen[k]]
    assert seen["traffic"] | seen["alarm"] == set(TX) and len(TX) == 51
    assert seen["traffic"] == set(TX) and "attach" not in seen["alarm"]
    assert set(TX) - seen["alarm"] >= {"attach", "raw", "confidence", "model", "stt", "stt_class", "transcript_pending"}
    for kind in ("retext", "rerun", "alt_text", "heard", "activity", "source_event", "source_health", "source_seen"):
        assert seen[kind] == set(DEFS[kind]["properties"]), kind
    always = set(DEFS["page_doc"]["required"])
    assert seen["page"] == seen["page_text"] == always and len(always) == 22
    feed = next(ev["data"]["event"] for ev in ran.events if ev["type"] == "source_event")
    assert set(feed) == set(DEFS["feed_event"]["properties"])
    # what each type always carries (`required`) is in every event of the type
    for ev in ran.events:
        need = DEFS[ev["type"]].get("required") or DEFS["page_doc"]["required"]
        assert set(need) <= set(ev["data"]), (ev["type"], sorted(set(need) - set(ev["data"])))


def test_a_page_document_has_the_schemas_keys_and_no_other():
    """A page's document is built in one place (App._page_doc): with everything a page can carry (the other engine's
    text, a unit's key-up cut from it and who keyed it) it has exactly the sub-schema's 27 keys."""
    page = Page("page-0002", "Paging 1", 460.1, c.T0, c.T0 + 10.2, "page",
                [ToneEvent("two_tone", 716.1, 600.9, 0.0, 4.05, ("Northfield FD",))], 4.2, None, 25.0, "done", lane.PAGED, 0.9)
    page.tone_seq = [{"hz": 716.1, "start": 0.0, "dur": 1.0, "role": "A"}]
    page.history = [{"text": "Northfield Fire", "at": c.T0 + 60, "model": "m.bin"}]
    page.alt_text, page.alt_model = "Northfield Fire, Engine 5", "other.bin"
    page.keyup = {"parent": "page-0001", "prev": "page-0001", "split": "id"}
    page.radio_id, page.unit, page.model, page.vocab_v, page.rerun_at = "7301", "7301", "m.bin", "terms-7", c.T0 + 60
    d = json.loads(json.dumps(appmod.App._page_doc(page)))
    assert set(d) == set(DEFS["page_doc"]["properties"]) and len(d) == 27
    post = {"epoch": lane.EPOCH, "station": c.STATION, "events": [
        {"seq": 1, "at": c.T0, "type": "page_text", "data": d, "audio": None, "audio_dropped": False}]}
    lane.conforms(post)


def literals_emitted() -> set[str]:
    """Every event type the code queues, read from its source: the first argument of each App._emit(...) call in
    sizeup/ (a name there fails: it could not be read), and the incident sources' three, which reach _emit as a
    variable (App._source_out) from the runner's sink(...) calls. App._emit is the one place that queues an event:
    a second call of the queue itself (`self.events(...)` in sizeup/app.py) would be a type this cannot read."""
    queued = [fn.name for fn in ast.walk(ast.parse((ROOT / "sizeup/app.py").read_text(encoding="utf-8")))
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(fn)
              if isinstance(n, ast.Call) and ast.unparse(n.func) == "self.events"]
    assert queued == ["_emit"], f"sizeup/app.py queues an event outside _emit: {queued}"
    out: set[str] = set()
    for f in sorted((ROOT / "sizeup").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = ("_emit", "sink") if f.relative_to(ROOT).as_posix() == "sizeup/sources/runner.py" else ("_emit",)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in names and n.args:
                first = n.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    out.add(first.value)
                else:
                    src = ast.unparse(n)
                    assert n.func.attr == "_emit" and src == "self._emit(kind, data)" and f.name == "app.py", \
                        f"{f.name}: {src}: an event type the contract tests cannot read; name it in station-events"
    return out


def test_the_event_types_are_the_ones_the_code_emits_and_the_hub_applies():
    """`type` x-known is the code's own list, both ends: every type some call in sizeup/ queues, and every type
    HubApp.apply acts on. A type added to either fails here until the schema has its sub-schema."""
    assert literals_emitted() == set(KINDS)
    tree = ast.parse(inspect.getsource(hub_mod.HubApp.apply).lstrip())
    applied = {x.value for n in ast.walk(tree) if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
               and n.left.id == "kind" for comp in n.comparators for x in ast.walk(comp)
               if isinstance(x, ast.Constant) and isinstance(x.value, str)}
    assert applied | set(hub_mod.SOURCE_KINDS) == set(KINDS)
    assert set(hub_mod.SOURCE_KINDS) == {"source_event", "source_health", "source_seen"}
    assert set(DEFS) >= set(KINDS) and DEFS["event"]["properties"]["type"]["x-known"] == KINDS
    assert [b["if"]["properties"]["type"]["const"] for b in DEFS["event"]["allOf"]] == KINDS


def test_no_attribute_is_set_on_a_transmission_that_the_schema_does_not_name():
    """The wider net under the run above: Traffic.as_dict sends every attribute, so an attribute a later change sets
    on a path the run does not reach would be on the wire unnamed. Every attribute assigned on a transmission or an
    item of speech-to-text anywhere in sizeup/app.py and sizeup/stt.py (`t.x = ...`, `item.x = ...`, `mark.x = ...`,
    setattr with the name spelled out, there or in the tuple a loop goes over) is one the schema names, or one of the
    few that never reach a transmission's event (a page's or a re-run's own, and the two that are taken off before it
    is sent). A setattr whose name cannot be read, and a write into `__dict__`, fail here: spell the name out. What
    this cannot see is the same object under another name (`tr = t; tr.x = ...`): the run above is the check for
    that, on the paths it drives."""
    held = ("t", "item", "mark")
    never = {"raw_audio": "taken off before it is sent (Traffic.as_dict)",
             "catchup": "a re-run's own (Rerun)", "history": "a page's", "dispatch": "a page's", "shown": "a page's",
             "rerun_at": "a page's", "rerun_result": "a page's", "alt_text": "a page's", "alt_model": "a page's",
             "page": "the tap's carrier",
             # set only on the CAD of record (App._screen: never while forwarding to a hub, which screens itself)
             "quality": "the CAD of record's (sizeup/stt_quality.py)", "unclear_text": "the CAD of record's"}
    found: dict[str, str] = {}
    for rel in ("sizeup/app.py", "sizeup/stt.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        looped: set[int] = set()
        for n in ast.walk(tree):
            targets = []
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                raw = n.targets if isinstance(n, ast.Assign) else [n.target]
                targets = [x for t in raw for x in (t.elts if isinstance(t, ast.Tuple) else [t])]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in held:
                    found[t.attr] = f"{rel}:{t.lineno}"
            if isinstance(n, ast.For) and isinstance(n.iter, ast.Tuple) and any(
                    isinstance(x, ast.Call) and getattr(x.func, "id", "") == "setattr" and isinstance(x.args[0], ast.Name)
                    and x.args[0].id in held for x in ast.walk(n)):
                found.update({e.value: f"{rel}:{n.lineno}" for e in n.iter.elts if isinstance(e, ast.Constant)})
                looped |= {id(x) for x in ast.walk(n)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "setattr" and len(n.args) >= 2 \
                    and isinstance(n.args[0], ast.Name) and n.args[0].id in held:
                if isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str):
                    found[n.args[1].value] = f"{rel}:{n.lineno}"
                else:
                    assert id(n) in looped, f"{rel}:{n.lineno}: {ast.unparse(n)}: a name the contract tests cannot read"
            own = lambda x: (isinstance(x, ast.Attribute) and x.attr == "__dict__"            # noqa: E731
                             and isinstance(x.value, ast.Name) and x.value.id in held)
            wrote = (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and own(n.func.value)
                     and n.func.attr in ("update", "setdefault", "__setitem__")) \
                or (isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                    and any(isinstance(x, ast.Subscript) and own(x.value)
                            for x in (n.targets if isinstance(n, ast.Assign) else [n.target])))
            assert not wrote, f"{rel}:{n.lineno}: {ast.unparse(n)}: a write into __dict__ the contract tests cannot read"
    assert len(found) >= 40, found
    unnamed = {k: v for k, v in found.items() if k not in TX and k not in never}
    assert unnamed == {}, f"set on a transmission and not in station-events' transmission: {unnamed}"
    assert {"carrier_dbfs", "snr_db", "opened", "flag_stuck", "transcript_pending", "stt_prompt_time"} <= set(found)


def test_the_words_are_the_codes_own():
    """Each `x-known` list that the code has as a list of its own is that list."""
    from sizeup import sourcekeys, stt, tiers
    from sizeup.sources import HEALTH_STATES, SOURCE_ID, TIME_KEYS
    assert TX["stt_class"]["x-known"] == list(stt.CLASS_NAMES.values())
    assert TX["tier"]["x-known"] == list(tiers.RECORDED)
    assert TX["service"]["x-known"] == TX["service_auto"]["x-known"] == list(sourcekeys.SERVICES)
    assert DEFS["source_health"]["properties"]["state"]["x-known"] == list(HEALTH_STATES)
    for kind in ("source_event", "source_health", "source_seen", "feed_event"):
        assert DEFS[kind]["properties"]["source"]["pattern"] == f"^{SOURCE_ID.pattern}$"
    assert all(f"`{k}`" in DEFS["feed_event"]["properties"]["times"]["description"] for k in TIME_KEYS)
    assert DEFS["clip_id"]["pattern"] == f"^(?!.*\\.\\.){appmod.CLIP_ID.pattern}$"
    src = inspect.getsource(hub_mod.HubApp.apply)
    assert all(f'"{w}"' in src for w in DEFS["heard"]["properties"]["system"]["x-known"])
    assert TX["emergency_via"]["x-known"] == ["MDC", "FleetSync"] and '"FleetSync"' in inspect.getsource(appmod.App._via)


# -- the clip on the wire ------------------------------------------------------------------------------------------------

def test_a_clip_is_a_base64_wav_mono_16_bit_8_khz_and_the_hub_stores_it_as_it_came(ran):
    """`audio` as the station sends it: base64 (the standard alphabet, padded, no line breaks) of a whole RIFF / WAVE
    file, PCM mono 16-bit at 8000 frames a second, half the frames of the station's 16 kHz clip. Only a page, a
    transmission and an alarm carry one. The hub stores the decoded bytes as they are."""
    with_clip = [ev for ev in ran.events if ev["audio"] is not None]
    assert {ev["type"] for ev in with_clip} == {"page", "traffic", "alarm"} and len(with_clip) >= 20
    assert all(ev["audio"] is None for ev in ran.events if ev["type"] not in ("page", "traffic", "alarm"))
    assert all(ev["audio_dropped"] is False for ev in ran.events)
    for ev in with_clip:
        d = ev["data"]
        ch, width, rate, frames = lane.wav_of(ev["audio"])
        assert (ch, width, rate) == (1, 2, 8000) and "\n" not in ev["audio"] and ev["audio"].startswith("UklGR")
        assert abs(frames / 8000 - (d["end"] - d["start"])) < 0.01, (ev["type"], d["id"])
        assert ran.clips[d["id"]] == base64.b64decode(ev["audio"], validate=True)
    live = next(ev for ev in ran.events if ev["data"].get("id") == ran.ids["fm_live_alarm"])
    assert live["type"] == "alarm" and live["audio"] is None       # raised at the key-up: no clip yet


def test_what_is_and_is_not_checked_about_a_clip(tmp_path):
    """As built. The station halves a 16 kHz mono 16-bit WAV and sends anything else as it is (another rate, bytes
    that are no WAV); nothing for no clip. The hub checks nothing: bytes that are no WAV are stored, base64 with
    characters outside the alphabet is decoded with them left out, and only base64 it cannot decode at all fails the
    event (acked and skipped: E1)."""
    pcm = (np.sin(np.arange(1600) / 5) * 8000).astype(np.int16)
    out = station_mod.clip_8k(wav_bytes(pcm, 16000))
    assert lane.wav_of(base64.b64encode(out).decode()) == (1, 2, 8000, 800)
    at_8k = wav_bytes(pcm, 8000)
    assert station_mod.clip_8k(at_8k) == at_8k and station_mod.clip_8k(b"not a wav") == b"not a wav"
    assert station_mod.clip_8k(b"") is None and station_mod.clip_8k(None) is None
    lv = lane.Lane(tmp_path)
    try:
        base = dict(c.vectors(lane.EVENTS, "valid"))["traffic-fm.json"]
        hub = lv.hub.app

        def post(seq: int, tid: str, audio):
            ev = dict(base["events"][0], seq=seq, audio=audio, data=dict(base["events"][0]["data"], id=tid))
            return lv.post(dict(base, events=[ev]))[1]
        assert post(1, "clip-1", base64.b64encode(b"not a wav").decode())["applied"] == 1
        assert hub.clip("clip-1") == b"not a wav"                                   # no WAV check
        good = base["events"][0]["audio"]
        assert post(2, "clip-2", good[:20] + " \n!*" + good[20:])["applied"] == 1
        assert hub.clip("clip-2") == base64.b64decode(good)                         # not strict: the junk is left out
        assert post(3, "clip-3", "")["applied"] == 1 and not hub.has_clip("clip-3")  # "": no clip
        listed = {} if E1_A_FAILED_EVENT_IS_ACKED_AND_GONE else {"rejected": [                # (#762 lists it)
            {"i": 0, "seq": 4, "type": "traffic", "reason": "apply_failed", "error": "Error: Incorrect padding"}]}
        assert post(4, "clip-4", "abc") == {"ok": True, "acked": 4, "applied": 0, **listed}   # not base64: it fails
        assert hub.hub.errors == 1 and hub.hub.last_error.startswith("traffic #4: Error")
        assert c.errors(lane.EVENTS, dict(base, events=[dict(base["events"][0], audio="abc")])) != []
    finally:
        lv.close()


# -- the recorded vectors ---------------------------------------------------------------------------------------------------

def test_the_recorded_event_vectors_are_what_the_station_sends_now(ran):
    """The base vectors of contracts/vectors/station-events/valid/ are recordings of this run, one real event each,
    scrubbed. They and the code must not drift apart: each has the keys, at every depth, of what the station sends
    now. `python -m tests.contracts record` writes them anew from this very run."""
    for name in RECORDED:
        now = lane.scrub(picked(ran, name), ran.tmp)
        committed = c.recorded(f"{lane.EVENTS}/valid/{name}", now)
        lane.conforms(committed)
        assert lane.shape(now) == lane.shape(committed), name
        assert committed["epoch"] == lane.EPOCH and committed["events"][0]["at"] == c.T0 + 1.0
    answer = json.loads(ran.seen[0][2])                         # (its counts fixed: how many events the first post
    ans = c.recorded(f"{lane.ANSWER}/valid/answer.json", dict(answer, acked=1, applied=1))   # had depends on the machine)
    assert set(ans) == set(answer) and c.errors(lane.ANSWER, ans) == []
    # together the recorded transmissions carry every key of the transmission: the vectors are complete too
    keys = set()
    for name, (kind, _) in RECORDED.items():
        if kind in ("traffic", "alarm"):
            keys |= set(dict(c.vectors(lane.EVENTS, "valid"))[name]["events"][0]["data"])
    assert keys == set(TX)


def test_the_recorded_at_close_vectors_and_a_station_that_sends_a_transmission_at_its_close_then_its_fill(ran_close):
    """(#927) Every post of that run conforms and was applied; each transmission went up once as "traffic" at its close
    (`transcript_pending` `station`, status pending, no words, its clip) and once as a "retext" with `fill`; the hub
    holds each once, filled. The recorded vectors of it are what the station sends now."""
    assert ran_close.errors == (0, "")
    for raw, code, _answer in ran_close.seen:
        lane.conforms(raw)
        assert code == 200
    for tid in ran_close.ids.values():
        tr = [ev for ev in ran_close.events if ev["type"] == "traffic" and ev["data"]["id"] == tid]
        fills = [ev for ev in ran_close.events if ev["type"] == "retext" and ev["data"]["id"] == tid and ev["data"].get("fill")]
        assert len(tr) == 1 and len(fills) == 1 and tr[0]["seq"] < fills[0]["seq"], tid
        d = tr[0]["data"]
        assert (d["status"], d["text"], d["transcript_pending"]) == ("pending", "", "station") and tr[0]["audio"]
        assert fills[0]["audio"] is None and fills[0]["data"]["engine"] == "station-whisper"
        assert fills[0]["data"].get("authoritative", True) is (tid != ran_close.ids["fm_withdrawn"])
    pool = next(ev["data"] for ev in ran_close.events if ev["type"] == "retext" and ev["data"].get("fill")
                and ev["data"]["id"] == ran_close.ids["fm_pool"])
    assert (pool["transcript_pending"], pool["status"], pool["model"]) == ("pool", "remote", "pool")
    both = next(ev["data"] for ev in ran_close.events if ev["type"] == "retext" and ev["data"].get("fill")
                and ev["data"]["id"] == ran_close.ids["fm_both"])
    assert both["stt"] == "both" and both["status"] == "done"
    assert ran_close.held[ran_close.ids["fm"]]["text"] == lane.SAID
    for name in RECORDED_AT_CLOSE:
        now = lane.scrub(picked(ran_close, name), ran_close.tmp)
        committed = c.recorded(f"{lane.EVENTS}/valid/{name}", now)
        lane.conforms(committed)
        assert lane.shape(now) == lane.shape(committed), name


# What a change in the code would put on the wire: each alone must fail the conformance check. These edit a recorded
# event, not the code; a real change in the code is caught by the live tests above, which is what they are for.
MUTATIONS = {
    "traffic-fm.json": {
        "a new attribute on a transmission": lambda e: e["data"].update(new_flag=True),
        "a transmission without attach": lambda e: e["data"].pop("attach"),
        "talkgroup as a string": lambda e: e["data"].update(talkgroup="4101"),
        "start in milliseconds": lambda e: e["data"].update(start=int(e["data"]["start"] * 1000)),
        "pairs as an object": lambda e: e["data"].update(pairs={"990001": ""}),
        "a new key on the event": lambda e: e.update(new_key=1),
        "seq as a string": lambda e: e.update(seq="1"),
        "audio as raw bytes in a list": lambda e: e.update(audio=[82, 73, 70, 70]),
        "audio that is no WAV": lambda e: e.update(audio=base64.b64encode(b"OggS....").decode()),
    },
    "traffic-fm-cut-at-a-key-up.json": {"a new key in keyup": lambda e: e["data"]["keyup"].update(new_key=1)},
    "page.json": {
        "a page without kind": lambda e: e["data"].pop("kind"),
        "a new key on a page": lambda e: e["data"].update(new_key=1),
        "a new key on a tone pair": lambda e: e["data"]["tones"][0].update(new_key=1),
        "confidence as a string": lambda e: e["data"].update(confidence="0.9"),
    },
    "page-text.json": {"a new key in the parsed dispatch": lambda e: e["data"]["dispatch"].update(new_key=1)},
    "alarm-p25.json": {"an alarm without emergency": lambda e: e["data"].pop("emergency"),
                       "an alarm's pairs null": lambda e: e["data"].update(pairs=None)},
    "retext.json": {"a new key on a retext": lambda e: e["data"].update(new_key=1),
                    "catchup false": lambda e: e["data"].update(catchup=False)},
    "retext-fill.json": {"fill false": lambda e: e["data"].update(fill=False),
                         "stt as aws": lambda e: e["data"].update(stt="aws"),
                         "stt_run_s as a string": lambda e: e["data"].update(stt_run_s="1.2")},
    "heard.json": {"at in milliseconds": lambda e: e["data"].update(at=int(c.T0 * 1000)),
                   "a heard without channel": lambda e: e["data"].pop("channel")},
    "activity.json": {"minute as a time in seconds": lambda e: e["data"].update(minute=int(c.T0)),
                      "radios as numbers": lambda e: e["data"].update(radios=[880001])},
    "source-event.json": {"a new key beside the event": lambda e: e["data"].update(new_key=1),
                          "a new key in the feed event": lambda e: e["data"]["event"].update(new_key=1),
                          "a comment as an object": lambda e: e["data"]["event"].update(comments=[{"at": c.T0, "text": "x"}])},
    "source-health.json": {"stopped as a word": lambda e: e["data"].update(stopped="no")},
    "source-seen.json": {"ids as an object": lambda e: e["data"].update(ids={"S1": True})},
}


def test_a_change_in_what_the_code_sends_fails_the_check():
    """The check on itself: a recorded event changed the way a change in the code would change it (a key gone, a key
    added, a type changed) no longer conforms. Each alone."""
    vectors = dict(c.vectors(lane.EVENTS, "valid"))
    n = 0
    for name, changes in MUTATIONS.items():
        lane.conforms(vectors[name])
        for what, change in changes.items():
            post = copy.deepcopy(vectors[name])
            change(post["events"][0])
            n += 1
            try:
                lane.conforms(post)
            except AssertionError:
                continue
            pytest.fail(f"not caught: {name}: {what}")
    assert n >= 25


def test_the_walkers_reach_inside_each_events_data_by_its_type():
    """The helper on itself: the schema picks an event's sub-schema by its `type` (if / then), which the walkers of
    tests/contracts.py do not follow, so tests/contracts_lane.py does. A key added inside a transmission or inside a
    feed event is found; an event of a type the schema does not know is left alone."""
    vectors = dict(c.vectors(lane.EVENTS, "valid"))
    post = copy.deepcopy(vectors["source-event.json"])
    post["events"][0]["data"]["event"]["new_key"] = 1
    post["events"][0]["data"]["event"]["extra"]["anything"] = {"goes": "here"}       # (open: the adapter's own)
    post["events"].append(dict(copy.deepcopy(vectors["traffic-p25-emergency.json"]["events"][0]), seq=2))
    post["events"][1]["data"]["new_key"] = 1
    post["events"][1]["data"]["evidence"]["anything"] = 1                             # (open: trunk-recorder's own)
    post["events"].append({"seq": 3, "at": c.T0, "type": "from_the_future", "data": {"x": 1}, "audio": None,
                           "audio_dropped": False})
    assert lane.undeclared(post) == ["$.events[0].data.event.new_key", "$.events[1].data.new_key"]
    assert c.errors(lane.EVENTS, post) == []
    out, n = lane.with_unknown_keys(vectors["page.json"])
    assert n == 6 and sorted(lane.undeclared(out)) == [
        "$.events[0].data.from_the_future", "$.events[0].data.tone_seq[0].from_the_future",
        "$.events[0].data.tone_seq[1].from_the_future", "$.events[0].data.tones[0].from_the_future",
        "$.events[0].from_the_future", "$.from_the_future"]
    assert c.errors(lane.EVENTS, out) == []


# -- the compatibility rule on the real readers ------------------------------------------------------------------------

def test_the_hub_takes_a_key_it_does_not_know_in_the_envelope_in_an_event_and_in_every_types_data(tmp_path):
    """The hub as the reader: every valid vector, with a key from a newer build added to the envelope, to each event
    and to every object of each event's data at every depth, is applied like today's: no event fails."""
    lv = lane.Lane(tmp_path)
    try:
        hub = lv.hub.app
        added, kinds = 0, set()
        for k, (file, post) in enumerate(c.vectors(lane.EVENTS, "valid")):
            post, n = lane.with_unknown_keys(post)
            added += n
            assert n >= 1 and c.errors(lane.EVENTS, post) == [], file
            post["epoch"] = f"{k:016x}"                               # (each vector is its own outbox: seq from 1)
            for ev in post["events"]:
                kinds.add(ev["type"])
                if ev["type"] in ("page", "traffic", "alarm"):
                    ev["data"]["id"] = f"{ev['data']['id']}.{k}"     # (a clip the hub has not seen yet)
            code, ans = lv.post(post)
            last = post["events"][-1]["seq"] if post["events"] else 0
            assert code == 200 and (ans["applied"], ans["acked"]) == (len(post["events"]), last), (file, ans)
            assert hub.hub.errors == 0, (file, hub.hub.last_error)
        assert added > 100 and kinds >= set(KINDS)
        assert len(hub.store.loose) + sum(len(i.traffic) for i in hub.store.incidents) >= 10 and len(hub.pages) >= 2
        assert lv.people()["/api/cad/state"] == 200 and lv.people()["/api/state"] == 200
    finally:
        lv.close()


def test_the_hub_acks_a_type_it_does_not_know_and_applies_nothing(tmp_path):
    """A newer station's event type: logged, acked, no error. So a new type needs a capability string from the hub
    first (as `activity` and another P25 system's rows wait for `p25_systems`): an older hub would take it and drop
    it, and the station would delete it."""
    lv = lane.Lane(tmp_path)
    said: list[str] = []
    try:
        lv.hub.app.hub.log = said.append
        post = dict(c.vectors(lane.EVENTS, "valid"))["a-type-from-the-future.json"]
        assert c.errors(lane.EVENTS, post) == [] and post["events"][0]["type"] not in KINDS
        assert lv.post(post)[1] == {"ok": True, "acked": 1, "applied": 1}
        assert lv.hub.app.hub.errors == 0 and any("ignoring event type" in m for m in said)
    finally:
        lv.close()


def test_the_station_holds_context_events_back_from_a_hub_that_has_not_said_p25_systems(tmp_path):
    """Rule 2 as the events lane has it today: `activity`, and a transmission or an alarm with `system`, are queued
    only while the hub's `caps` have `p25_systems`; without it they are not queued at all (dropped, not kept for
    later)."""
    lv = lane.Lane(tmp_path, answer_without=("caps",))
    try:
        st = lv.station()
        assert wait(lambda: len(lv.seen) >= 1, 30) and "p25_systems" not in st.link.hub_caps
        tx = dict(c.vectors(lane.EVENTS, "valid"))["traffic-another-system.json"]["events"][0]["data"]
        st.link.emit("activity", {"source": "p25:east:7003", "minute": 29916500, "calls": 1, "radios": []})
        st.link.emit("traffic", dict(tx))
        st.link.emit("alarm", dict(tx, pairs=[]))
        st.link.emit("heard", {"system": "p25", "radio_id": "880001", "alias": "", "at": c.T0, "channel": "East Dispatch"})
        assert lv.drained(st, 1) and [ev["type"] for ev in lv.events()] == ["heard"]
    finally:
        lv.close()


def test_the_station_ignores_the_keys_it_does_not_know_in_the_answer(tmp_path):
    """The station as the reader: a newer hub's answer (keys it has never heard of, and #762's `rejected`, which
    today's station does not know either) is read like today's: `acked`, nothing else."""
    extra = {"from_the_future": {"x": [1, 2]}, "rejected": [], "cursor": {"epoch": "x", "seq": 1}}
    lv = lane.Lane(tmp_path, events_answer_with=extra)
    try:
        st = lv.station()
        lane.fm(st)
        assert lv.drained(st, 2) and wait(lambda: st.link.sent == 2, 10) and st.link.last_error == ""
        assert all(json.loads(a)["from_the_future"] == {"x": [1, 2]} for _, _, a in lv.events_seen)
        ans = json.loads(lv.events_seen[0][2])
        assert c.errors(lane.ANSWER, ans) == [] and len(c.undeclared(lane.ANSWER, ans)) == 2
        assert all(t.is_alive() for t in st.link._threads)
    finally:
        lv.close()


def test_known_deviation_the_hub_keeps_a_key_it_does_not_know_on_a_transmission(tmp_path):
    """E4. For `traffic` and `alarm` the hub does not ignore a key it does not know: every key whose name is a
    lower-case letter and up to 30 lower-case letters, digits or underscores becomes an attribute of its own copy of
    the transmission, and from there a key of the row every console gets (GET /api/state and the stream). So a key a
    station adds changes what the consoles receive without the hub having said it understands it, and one named like
    an attribute the hub's own code sets (here `flagged`) lands on that attribute. Any other name, and `has_audio`,
    is dropped; so is an unknown key of every other event type. Fixed: the hub takes the keys the schema names."""
    lv = lane.Lane(tmp_path)
    try:
        hub = lv.hub.app
        post = copy.deepcopy(dict(c.vectors(lane.EVENTS, "valid"))["traffic-fm.json"])
        d = post["events"][0]["data"]
        d.update({"from_the_future": {"x": 1}, "flagged": "by the station", "has_audio": "no", "From_The_Future": 1,
                  "_private": 1, "a" * 32: 1, "with-a-dash": 1})
        heard = dict(c.vectors(lane.EVENTS, "valid"))["heard.json"]["events"][0]
        post["events"].append(dict(heard, seq=2, data=dict(heard["data"], from_the_future=1)))
        assert lv.post(post)[1] == {"ok": True, "acked": 2, "applied": 2}
        t = next(x for x in hub.store.loose if x.id == d["id"])
        rows = lambda: [x for x in lv.call("GET", "/api/state", token=None)[1]["loose_traffic"] if x["id"] == d["id"]]   # noqa: E731
        assert wait(lambda: bool(rows()), 15)                           # (the consoles' state is pushed once a second)
        row = rows()[0]
        dropped = {"From_The_Future", "_private", "a" * 32, "with-a-dash"}
        assert not dropped & set(vars(t)) and not dropped & set(row)
        assert row["has_audio"] is True                                 # the hub's own word, not the station's "no"
        assert hub.radios.get("mdc", heard["data"]["radio_id"]) is not None
        if E4_THE_HUB_KEEPS_A_KEY_IT_DOES_NOT_KNOW:
            assert t.from_the_future == {"x": 1} and t.flagged == "by the station"
            assert row["from_the_future"] == {"x": 1} and row["flagged"] == "by the station"
        else:
            assert not hasattr(t, "from_the_future") and "from_the_future" not in row and "flagged" not in row
        assert hub_mod.EXTRA.pattern == "[a-z][a-z0-9_]{0,30}"
        # the names it takes today are exactly the names the schema's transmission has: none would be dropped
        assert all(hub_mod.EXTRA.fullmatch(k) for k in TX)
    finally:
        lv.close()


# -- which keys of a transmission the hub reads ------------------------------------------------------------------------

# Another value for a key, for the keys where "the value plus one" or "not the value" would not be one the code takes.
OTHER = {"status": "no_speech", "stt": "both", "stt_class": "urgent", "tier": "text", "service": "fire", "service_auto": "fire",
         "opened": "sustained", "emergency_via": "FleetSync", "id_note": "undecoded", "kept": "", "system": "west",
         "source_key": "p25:east:7002", "group": "Fire", "dept": "East PD", "unit": "Car 9", "radio_id": "990009",
         "source": "Fire Ops 2", "transcript_pending": "pool", "talkgroup": 4102, "raw": "Ladder 2 responding",
         "text": "Ladder 2 responding", "pairs": [["990009", "Car 9"]], "evidence": {"emergency": 0}, "notched_hz": [],
         "keyup": {"parent": "another-clip", "prev": "", "split": ""}, "turn": 3, "call_id": "another-call",
         "model": "other.bin", "vocab_v": "terms-8", "alarm_key": "Fireground1-live-1", "flag_stuck": "Car 9 (990009)",
         "rig": "E9"}
# The keys the hub reads by name where the comparison below cannot see it (it compares what the hub stores, and these
# act on something else or only together with another key): where, in the code as of b8cba174.
READ_BY_READING = {
    "emergency_via": "sizeup/app.py:1857 (App._alarm: the alarm's `via`)",
    "alarm_key": "sizeup/app.py:1858, sizeup/cad/alarms.py:668 (the alarm raised at the key-up is taken over)",
    "flag_stuck": "sizeup/app.py:1858 (App._alarm: the alarm's `stuck`)",
    "stt_prompt_time": "sizeup/cloud/hub.py:1859 (the run is left out of the trailing-time check)",
    "stt_class": "sizeup/cloud/hub.py:1241 (the pool's class, with transcript_pending `pool`)",
    "transcript_pending": "sizeup/cloud/hub.py:1238, 1240 (the hub transcribes it: Amazon Transcribe, or the pool)",
    "stt": "sizeup/cloud/hub.py:1247 (`both`: Amazon Transcribe's text is added, when the clip came)",
    "pairs": "sizeup/cloud/hub.py:1206-1212 (the rig, when the hub's registry knows the radio)",
    "fm": "sizeup/app.py:2353, 2607 (de-emphasis when the clip is transcribed again)",
    "mhz": "sizeup/cad/comms.py:287",
    "carrier_db": "sizeup/app.py:1271 (the airport pre-alerts' weak-signal mark)",
    "service": "sizeup/app.py:2189 (only when service_auto is missing)",
    "late": "sizeup/app.py:2229, 1910 (#747: the radio log's row and the alarm say late; no pre-alert hears it)",
}


# The two recordings the comparison leaves out: on a transmission it is to transcribe itself the hub starts work of
# its own (a job for the pool, the way round a clip that did not come), so what it holds depends on when one looks.
HUB_TRANSCRIBES = ("traffic-fm-for-amazon-transcribe.json", "traffic-another-system-for-the-pool.json")
WATCHED = {"cad.sqlite": ("radio_traffic",), "stt.sqlite": ("stt_runs",)}
OF_THE_ROW = re.compile(r"\.(id|rerun_at|[a-z_]+_at)$")      # a row's own number, and when the hub wrote it


def snapshot(lv, tid: str) -> dict:
    """What the hub holds of this clip: its row in the radio log, its speech-to-text runs, and the hub's own copy of
    the transmission (what the consoles get), the id itself replaced. Not the alarms: one alarm takes in the next
    transmission of the same radio, so two transmissions cannot be compared there."""
    out: dict = {}
    for name, tables in WATCHED.items():
        db = sqlite3.connect(f"file:{lv.tmp / 'hub' / name}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            for table in tables:
                rows = [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')]
                hit = [r for r in rows if any(isinstance(v, str) and tid in v for v in r.values())]
                out[table] = json.loads(json.dumps(hit, default=repr).replace(tid, "ID"))
        finally:
            db.close()
    app = lv.hub.app
    mine = [t.as_dict() for t in list(app.store.loose) + [t for i in app.store.incidents for t in i.traffic] if t.id == tid]
    out["copy"] = json.loads(json.dumps(mine, default=repr).replace(tid, "ID"))
    return out


def changed(a, b, path: str = "") -> set[str]:
    """The paths at which two snapshots differ."""
    if type(a) is not type(b):
        return {path}
    if isinstance(a, dict):
        return {x for k in set(a) | set(b) for x in (changed(a[k], b[k], f"{path}.{k}") if k in a and k in b
                                                    else {f"{path}.{k}"})}
    if isinstance(a, list):
        return {f"{path}[]"} if len(a) != len(b) else {x for i, (u, v) in enumerate(zip(a, b))
                                                       for x in changed(u, v, f"{path}[{i}]")}
    return set() if a == b else {path}


def test_which_keys_of_a_transmission_the_hub_reads(tmp_path):
    """`x-hub` on every key of the transmission, held to the hub itself. Each recorded transmission is applied on a
    real hub as it is, then once without each key and once with each key changed, under a new clip id each time:
    `required` = the event fails without the key; `read` = changing it changes something the hub stores beyond its own
    copy of that key (or the code reads it where that cannot show: READ_BY_READING, by file and line); `replaced` =
    the hub's copy has the hub's value whatever was sent; `kept` = changing it changes the hub's copy of that key and
    nothing else."""
    lv = lane.Lane(tmp_path)
    try:
        app = lv.hub.app
        marks = {k: v["x-hub"] for k, v in TX.items()}
        assert set(marks.values()) == {"required", "read", "replaced", "kept"}
        n = [0]

        def apply(kind: str, data: dict, without: str = "", **change):
            n[0] += 1
            d = copy.deepcopy(data)
            d["id"] = tid = f"probe-{n[0]:04d}"
            if "call_id" in d:
                d["call_id"] = tid
            d.pop(without, None)
            d.update(change)
            try:
                app.apply(kind, d, None)
            except Exception as e:  # noqa: BLE001 - the event fails: what `required` means
                return type(e).__name__
            return snapshot(lv, tid)

        def other(key: str, v):
            if key in OTHER:
                return OTHER[key]
            return (not v) if isinstance(v, bool) else v + 1.5 if isinstance(v, (int, float)) else v + " x"

        found: dict[str, set] = {k: set() for k in TX}
        seen_at: dict[str, list] = {}                               # (where a `read` showed: for the failure's message)
        for name, (kind, _) in RECORDED.items():
            if kind not in ("traffic", "alarm") or name in HUB_TRANSCRIBES:
                continue
            data = dict(c.vectors(lane.EVENTS, "valid"))[name]["events"][0]["data"]
            assert "transcript_pending" not in data
            base = apply(kind, data)
            assert isinstance(base, dict), (name, base)
            noise = changed(base, apply(kind, data))                # (the same event twice: only the rows' own numbers)
            assert all(OF_THE_ROW.search(p) for p in noise), (name, noise)
            for key, value in data.items():
                if key == "id":
                    continue
                gone = apply(kind, data, without=key)
                if isinstance(gone, str):
                    found[key].add("required")
                    continue
                if value is None:
                    continue                                        # (null here: another recording has a value)
                got = apply(kind, data, **{key: other(key, value)})
                assert isinstance(got, dict), (name, key, got)
                strip = lambda s: dict(s, copy=[{k: v for k, v in t.items() if k != key} for t in s["copy"]])   # noqa: E731
                where = sorted(p for p in changed(strip(base), strip(got)) if not OF_THE_ROW.search(p))
                if where:
                    found[key].add("read")
                    seen_at.setdefault(key, where)
                elif kind == "traffic" and [t.get(key) for t in got["copy"]] == [t.get(key) for t in base["copy"]]:
                    found[key].add("replaced")                      # (changed on the wire, the same on the hub)
                else:
                    found[key].add("kept")
        required = {k for k, v in found.items() if "required" in v} | {"id"}
        assert required == {k for k, m in marks.items() if m == "required"} == {"id", "source", "start", "end", "text"}
        seen_read = {k for k, v in found.items() if "read" in v} - required
        by_mark = lambda m: {k for k, x in marks.items() if x == m}                 # noqa: E731
        assert seen_read <= by_mark("read"), f"the hub reads these and the schema does not say so: {seen_read - by_mark('read')}"
        assert by_mark("read") - seen_read == set(READ_BY_READING), (
            "marked `read`, not seen by the comparison, and not listed with its file and line (or the reverse)",
            {k: seen_at[k] for k in seen_read & set(READ_BY_READING)})
        assert by_mark("replaced") == {"rig", "ours"} and all("read" not in found[k] for k in ("rig", "ours"))
        assert "replaced" in found["rig"]
        assert by_mark("kept") == {"low_energy", "carrier_dbfs", "snr_db", "opened", "notched_hz", "floor_dbfs"}
        assert all(found[k] == {"kept"} for k in by_mark("kept")), {k: found[k] for k in by_mark("kept")}
        untried = [k for k in TX if not found[k] and k != "id"]
        assert untried == ["transcript_pending"], untried            # (only on the two recordings left out)
    finally:
        lv.close()


def test_the_hub_reads_neither_at_nor_audio_dropped_and_writes_raw_over_text(tmp_path):
    """As built: an event's `at` and `audio_dropped` are sent and never read; a transmission's `text` must be there
    and is then replaced by `raw`."""
    lv = lane.Lane(tmp_path)
    try:
        base = dict(c.vectors(lane.EVENTS, "valid"))["traffic-fm.json"]
        ev = base["events"][0]
        a = dict(ev, data=dict(ev["data"], id="one", text="words the hub never shows"))
        b = dict(ev, seq=2, at="yesterday", audio_dropped="perhaps", data=dict(ev["data"], id="two"))
        assert lv.post(dict(base, events=[a, b]))[1] == {"ok": True, "acked": 2, "applied": 2}
        rows = {t.id: t for t in lv.hub.app.store.loose}
        assert rows["one"].text == rows["two"].text == ev["data"]["raw"]
        src = inspect.getsource(hub_mod.Hub._events)
        assert 'get("at")' not in src and "audio_dropped" not in src and "audio_dropped" not in inspect.getsource(hub_mod.HubApp.apply)
    finally:
        lv.close()


# -- the deviations of the durable lane (events, jobs, the outbox) and the README ---------------------------------------------

LANE_TESTS = ("test_contracts_events.py", "test_contracts_events_cursor.py", "test_contracts_jobs.py",
              "test_contracts_outbox.py")


def test_each_deviation_of_the_durable_lane_is_pinned_by_a_test_or_says_it_was_read():
    """As tests/test_contracts_schemas.py does for D1 to D6 (pinned in tests/test_contracts_station_state.py): every
    E (events), J (jobs) and O (outbox) row of the README's deviations table has a file:line and is pinned by a
    `test_known_deviation_*` in one of this lane's test files, behind a switch, unless its row says it was read."""
    text = (c.ROOT / "README.md").read_text(encoding="utf-8")
    rows = {m[1]: m[0] for m in re.finditer(r"^\| ([EJO]\d+) \|.*$", text, re.M)}
    assert {"E1", "E2", "E3", "E4", "E5"} <= set(rows)
    pinned: set[str] = set()
    for name in LANE_TESTS:
        src = (ROOT / "tests" / name).read_text(encoding="utf-8")
        here = set(re.findall(r'def test_known_deviation_\w+\(.*\):\n    \"\"\"([EJO]\d+)\. ', src))
        for d in here:
            assert re.search(rf"^{d}_[A-Z0-9_]+ = (True|False)\b", src, re.M), f"{name}: {d} has no switch at the top"
        pinned |= here
    for d, row in rows.items():
        assert (d in pinned) != ("Found by reading" in row), f"{d}: pinned by a test, or found by reading"
        assert re.search(r"`[a-z/_.]+\.(py|md|sql):\d+", row), f"{d}: file:line"
        assert re.search(r"^\| [EJO]\d+ \| (low|medium|high) \|", row), f"{d}: a severity"
    assert pinned <= set(rows), f"a test pins a deviation the README does not list: {pinned - set(rows)}"
