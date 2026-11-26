"""The contract tests' helper for the station's durable lane (contracts/, ADR 0004): POST /api/station/events. A real
hub that keeps every events post as it came and as it was answered (tests/contracts.py Live, plus `events_seen`), the
drivers that put real transmissions through the station's own code paths (an FM transmission, a page, a P25 call, an
airband transmission, a talkgroup of another P25 system, a re-run, an incident source), and the walkers that hold an
event's `data` to the sub-schema of its `type`. Dev-only; nothing here is imported by sizeup/. Every id, name and
address here is made up."""
from __future__ import annotations

import base64
import copy
import io
import json
import re
import time
import wave

import numpy as np

from tests import contracts as c

EVENTS = "station-events"
ANSWER = "station-events-answer"
EPOCH = "0123456789abcdef"          # a recorded post's epoch (the real one is random: made with the outbox file)
SAID = "Engine 5 on location, nothing showing"
PAGED = "Northfield Fire, Engine 5, alarm activation, 12 Example Avenue, cross street Sample Road"
SAID_AGAIN = "Engine 5 on location, investigating"
CLIP = np.zeros(80, "<i2")          # a recorded event's clip: 10 ms of silence at 8 kHz (the real one is seconds long)


class Whisper:
    """Stands in for whisper-server: the same words for every transmission, the page's for a page (its voice is the
    6 s one), and other words once `again` is set (a re-run that changes the text)."""

    def __init__(self):
        self.again = False
        self.prompts: list[str] = []

    def transcribe(self, audio, prompt=""):
        self.prompts.append(prompt)
        if self.again:
            return SAID_AGAIN, 0.8
        return (PAGED if round(len(audio) / 16000) == 6 else SAID), 0.9


# The hub's caps from before #927 (no "traffic_pending"): a Lane's hub answers with these unless told otherwise, so its
# station sends a transmission in one event after its text, as the recorded vectors and the cursor's counts have it;
# at_the_close() is the other way (a Lane made with answer_with=None: the hub's own caps)
OLDER_CAPS = ["p25_systems", "stt_input", "stt_pool"]


class Lane(c.Live):
    """tests/contracts.py Live, with every events post the hub handled kept as it came and as it was answered:
    `events_seen` = [(request bytes, status, answer bytes)]. events_answer_with: keys added to every 200 answer (what a
    newer hub would send). SIZEUP_CONTRACTS_EVENTS_DELAY_S=0.3 in the environment: every events post is handled that
    much later (a slow hub: the station's rows pile up and go in fewer, larger posts; how the tests' own assumptions
    about that are looked for, as SIZEUP_CONTRACTS_STATE_DELAY_S does for the state post)."""

    def __init__(self, tmp_path, events_answer_with: dict | None = None, **kw):
        import os
        if "caps" not in kw.get("answer_without", ()):
            kw.setdefault("answer_with", {"caps": OLDER_CAPS})
        super().__init__(tmp_path, **kw)
        self.events_seen: list[tuple[bytes, int, bytes]] = []
        h = self.hub.app.hub
        real = h._events
        delay = float(os.environ.get("SIZEUP_CONTRACTS_EVENTS_DELAY_S") or 0.0)

        def events(raw, p=None):
            if delay:
                time.sleep(delay)
            try:
                code, body, ctype = real(raw, p)
            except ValueError as e:                  # (Hub.handle answers it 400: kept here as that)
                self.events_seen.append((raw, 400, str(e).encode()))
                raise
            if code == 200 and events_answer_with:
                body = json.dumps(dict(json.loads(body), **events_answer_with), separators=(",", ":")).encode()
            self.events_seen.append((raw, code, body))
            return code, body, ctype
        h._events = events

    def station(self, token: str = "sz1"):
        st = super().station(token)
        st.stt.client = self.whisper = Whisper()
        return st

    def rich_station(self, monkeypatch, token: str = "sz1"):
        st = super().rich_station(monkeypatch, token)
        st.stt.client = self.whisper = Whisper()
        return st

    def events(self, kind: str | None = None) -> list[dict]:
        """Every event the hub took (200), in the order they came; kind: of that type only."""
        out = [ev for raw, code, _ in self.events_seen if code == 200 for ev in json.loads(raw)["events"]]
        return [ev for ev in out if kind is None or ev["type"] == kind]

    def post(self, body, token: str | None = "sz1"):
        return self.call("POST", "/api/station/events", body, token=token)

    def drained(self, st, n: int = 0, s: float = 30.0) -> bool:
        """The station's outbox is empty and the hub has taken at least n events."""
        from tests.test_app import wait
        return wait(lambda: st.link.outbox.pending()[0] == 0 and len(self.events()) >= n, s)


# -- real transmissions through the station's own code paths -------------------------------------------------------------

def fm(st, label: str = "Fireground 1", mhz: float = 453.1, seconds: float = 3.0, radio: int | None = 0x7301,
       emergency: bool = False, at: float | None = None, cut: bool = False) -> str:
    """A transmission on an FM traffic channel, with its radio's MDC-1200 ID burst (emergency: the button's packet);
    cut: a piece of a recording cut at a key-up (sizeup/radio/fm.py). Its clip id."""
    from sizeup.radio.fm import Channel, Segment
    from sizeup.radio.mdc1200 import encode
    from tests.test_app import voice
    at = time.time() if at is None else at
    bursts = [] if radio is None else [encode(0x00 if emergency else 0x01, 0x80 if emergency else 0x00, radio)
                                       .astype(np.float32)]
    seg = Segment(at, at + seconds, voice(seconds), 20.0, bursts)
    if cut:
        seg.split, seg.prev_start, seg.first_start = "gap", at - 4.0, at - 9.0
    st._segment(Channel(mhz, label), seg)
    return f"{label.replace(' ', '')}-{int(at * 1000)}"


def page(st, label: str = "Paging 1", mhz: float = 460.1, at: float | None = None) -> None:
    """A two-tone page of the test's department and six seconds of voice on the paging channel."""
    from sizeup.radio.fm import RATE, Channel, Segment
    from tests.test_app import tone, voice
    at = time.time() if at is None else at
    audio = np.concatenate([tone(716.1, 1.0), tone(600.9, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    st._segment(Channel(mhz, label), Segment(at, at + len(audio) / RATE, audio, 25.0))


def airband(st, label: str = "Tower", mhz: float = 118.5, seconds: float = 4.0, at: float | None = None,
            weak: bool = False) -> str:
    """A transmission on the AM airband channel, with the levels sizeup/radio/am.py measures on each."""
    from sizeup.radio.fm import Channel, Segment
    from tests.test_app import voice
    at = time.time() if at is None else at
    seg = Segment(at, at + seconds, voice(seconds), 2.0 if weak else 14.0, hiss_db=-52.3)
    seg.weak, seg.marginal, seg.tones = weak, False, [1004.0]
    seg.carrier_dbfs, seg.snr_db, seg.opened = -38.3, 11.5, "carrier"
    st._segment(Channel(mhz, label), seg)
    return f"{label.replace(' ', '')}-{int(at * 1000)}"


def p25_call(st, name: str = "4101-1794990000_476075000.0-call_1", tg: int = 4101, radio: str = "990001",
             seconds: float = 2.0, at: float | None = None, emergency: bool = False, alias: str = ""):
    """A finished call on a talkgroup of the county system, as sizeup/radio/p25.py hands it over (one key-up)."""
    from sizeup.radio.p25 import Call, Turn
    from tests.test_app import voice
    at = time.time() if at is None else at
    return Call(name, tg, c.TALKGROUPS.get(tg, "East Dispatch"), at, at + seconds, [alias or radio], voice(seconds, 1), "",
                [Turn(0.0, alias or radio, radio, alias, at=at, emergency=emergency)],
                ids={alias or radio: (radio, alias)}, system=st.tg.county, emergency=emergency)


def p25_grant_cut_call(st, name: str = "4101-1794990060_476075000.0-call_2", at: float | None = None):
    """A county call of two talkers in one transmission, the second cut where the control channel granted it (#819:
    the grant split "on", sizeup/radio/p25.py apply_grant_cuts)."""
    from sizeup.radio.p25 import GRANT_CUT, Call, Turn
    from tests.test_app import voice
    at = time.time() if at is None else at
    return Call(name, 4101, c.TALKGROUPS.get(4101, "East Dispatch"), at, at + 4.0, ["990001", "990004"], voice(4.0, 1), "",
                [Turn(0.0, "990001", "990001", "", 2.0, at=at), Turn(2.0, "990004", "990004", "", 2.0, at=at + 2.0,
                                                                      via=GRANT_CUT)],
                ids={"990001": ("990001", "")}, system=st.tg.county)


def context_call(st, name: str = "7001-1794990000_770181250.0-call_2", tg: int = 7001, radio: str = "880001",
                 seconds: float = 2.0, at: float | None = None, late: bool = False) -> None:
    """A finished call on a talkgroup of the other P25 system ("east"): context, by its talkgroup's tier. late: written
    while sizeup was down, ingested at its next start (#747)."""
    from sizeup.radio.p25 import Call, Turn
    from tests.test_app import voice
    at = time.time() if at is None else at
    rt = st.systems["east"]
    call = Call(name, tg, rt.system.talkgroups[tg]["name"], at, at + seconds, [radio], voice(seconds, 1), "",
                [Turn(0.0, radio, radio, "", at=at)], system="east")
    call.late = late
    st._call_system(rt, call)


def at_the_close(lv: Lane, st, monkeypatch) -> dict[str, str]:
    """(#927) A plain station of a hub whose caps say "traffic_pending", through each way the text of a transmission sent
    at its close fills it: whisper's words; whisper behind, the next clip handed to the hub's pool (its fill says
    "pool", with its class); a channel on both engines. The clip ids by what they are. What stands in: whisper (Whisper,
    held while the clips queue), the hub's word that its pool has room, and the channel map it would push back."""
    import threading
    from sizeup.stt import SHED
    from tests.test_app import wait
    assert wait(lambda: "traffic_pending" in st.link.hub_caps, 30)
    gate, then, real = threading.Event(), threading.Event(), st.stt.client
    calls = []

    class Held:
        def transcribe(self, audio, prompt=""):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(30)                                 # (the first clip held while the others queue)
            elif round(len(audio) / 16000) == 4:
                then.wait(30)                                 # (the one the hub withdraws while whisper has it)
            return real.transcribe(audio, prompt)
    st.stt.client = Held()
    ids: dict[str, str] = {}
    t = time.time() - 300.0
    ids["fm"] = fm(st, at=t)                                                    # transcript_pending station; fill
    assert wait(lambda: st.stt.current() is not None, 10)                       # (whisper on it, held)
    ids["fm_pool"] = fm(st, at=t + 10, radio=0x7306)                            # behind it: shed, to the pool
    for name, fn in (("pool_has_room", lambda seconds=None: True), ("pool_takes", lambda: True)):
        monkeypatch.setattr(st.link, name, fn)
    [(_p, item, _a, _c, _q)] = st.stt.withdraw(ids={ids["fm_pool"]})
    st._stt_dropped(item, f"{SHED} (the contract run)")                         # transcript_pending pool, stt_class
    ids["fm_withdrawn"] = fm(st, at=t + 15, seconds=4.0, radio=0x7307)          # the hub's pool takes it: authoritative
    monkeypatch.setattr(lv.hub.app, "channels_check", lambda force=False: None)   # (the hub's map: not pushed back)
    st.set_channel_prefs({"Fireground 1": {"engine": "both"}}, {"ok": True, "why": "", "reserve": []})
    ids["fm_both"] = fm(st, at=t + 20, radio=0x7305)                            # stt both
    gate.set()
    assert wait(lambda: getattr((st.stt.current() or (0, None))[1], "id", "") == ids["fm_withdrawn"], 10)
    got = st.stt_withdraw([ids["fm_withdrawn"]])
    assert got["running"] == [ids["fm_withdrawn"]], got                         # (whisper began it)
    then.set()

    def filled() -> bool:
        got = {ev["data"]["id"] for ev in lv.events("retext") if ev["data"].get("fill")}
        return set(ids.values()) <= got and st.link.outbox.pending()[0] == 0
    assert wait(filled, 60), "the fills did not reach the hub"
    return ids


# -- an event's data against the sub-schema of its type ------------------------------------------------------------------

def types() -> list[str]:
    """The event types the schema knows: the item's `type` x-known."""
    return list(c.load(EVENTS)["$defs"]["event"]["properties"]["type"]["x-known"])


def data_schema(kind: str) -> dict | None:
    """The sub-schema of an event type's `data` ({"$ref": ...}), None for a type the schema does not know."""
    doc = c.load(EVENTS)
    return {"$ref": f"{doc['$id']}#/$defs/{kind}"} if kind in types() else None


def _resolver():
    doc = c.load(EVENTS)
    return c.registry().resolver(doc["$id"])


def undeclared(body: dict) -> list[str]:
    """The keys of an events post the schema does not name, wherever it lists them all: the envelope's and each
    event's (tests/contracts.py undeclared), and inside each event's `data` by its type (the schema picks the
    sub-schema with if / then, which that walker does not follow)."""
    out = c.undeclared(EVENTS, body)
    for i, ev in enumerate(body.get("events") or []):
        node = data_schema(str(ev.get("type"))) if isinstance(ev, dict) else None
        if node is not None and isinstance(ev.get("data"), dict):
            out += c._undeclared(node, _resolver(), ev["data"], f"$.events[{i}].data")
    return out


def with_unknown_keys(body: dict, key: str = "from_the_future") -> tuple[dict, int]:
    """(a copy of an events post with a key no schema names in every object the schema describes field by field: the
    envelope, each event, and each event's `data` at every depth, by its type; how many were added)."""
    out, n = c.with_unknown_keys(EVENTS, body, key)
    for ev in out.get("events") or []:
        node = data_schema(str(ev.get("type"))) if isinstance(ev, dict) else None
        if node is not None and isinstance(ev.get("data"), dict):
            n += c._inject(node, _resolver(), ev["data"], key)
    return out, n


def conforms(raw: bytes | str | dict) -> dict:
    """An events post is JSON a strict reader takes, valid against station-events, names no key the schema does not
    (the envelope, each event, each event's data by its type) and has no integer a 64-bit float would change."""
    from tests.test_contracts_station_state import ints
    doc = c.loads_strict(raw) if isinstance(raw, (bytes, str)) else raw
    assert c.errors(EVENTS, doc) == []
    assert undeclared(doc) == [], "station-events: the code sends keys the schema does not name"
    assert all(c.float_exact(n) for n in ints(doc))
    return doc


def wav_of(audio_b64: str) -> tuple[int, int, int, int]:
    """(channels, bytes per sample, frames per second, frames) of an event's `audio`: base64 (RFC 4648, with padding)
    of a RIFF / WAVE file."""
    raw = base64.b64decode(audio_b64, validate=True)
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    with wave.open(io.BytesIO(raw)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def tiny_clip() -> str:
    """The clip a recorded event carries instead of its own: 10 ms of silence, mono 16-bit 8 kHz, as base64."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(CLIP.tobytes())
    return base64.b64encode(buf.getvalue()).decode()


# -- recording ------------------------------------------------------------------------------------------------------------

IDS = re.compile(r"\b(Fireground1|Tower|Paging1)-(live-)?(\d{12,})\b")
ANCHORS = ("start", "at", "seen_at")


def scrub(body: dict, tmp="") -> dict:
    """A recorded events post with everything of this run replaced by fixed made-up values (tests/contracts.py scrub
    does the same for a state post), so that a vector says nothing about the machine or the moment it was recorded on
    and a new recording differs only where the code does: the epoch is EPOCH; `seq` counts from 1; each event's times
    are moved so that its own first time (`start`, else `at`, `seen_at`, its minute, the time in its clip id) is T0, the times inside its clip
    ids with them, and it was queued a second later; an alarm raised at a key-up ends a second after it started (its
    end is the moment it was sent); a clip is the 10 ms one; a page's random id is `page-0001`; the speech-to-text
    timings are fixed; the county system's own name becomes `countynet`."""
    import sizeup.app as appmod
    from sizeup import sourcekeys
    body = copy.deepcopy(body)
    doc = c.load(EVENTS)
    resolver = c.registry().resolver(doc["$id"])
    pages: dict[str, str] = {}
    for k, ev in enumerate(body["events"]):
        d = ev["data"]
        anchor = next((d[a] for a in ANCHORS if isinstance(d.get(a), (int, float))), None)
        if anchor is None:
            own = IDS.fullmatch(str(d.get("id")))                 # (new words for a clip: the time in the clip's id)
            anchor = d["minute"] * 60.0 if isinstance(d.get("minute"), int) else int(own[3]) / 1000 if own else ev["at"]
        move = lambda t, anchor=anchor: round(c.T0 + (t - anchor), 2)             # noqa: E731

        def words(o, move=move):
            if isinstance(o, dict):
                return {key: words(v) for key, v in o.items()}
            if isinstance(o, list):
                return [words(v) for v in o]
            if isinstance(o, str):
                o = o.replace(sourcekeys.COUNTY, "countynet").replace(appmod.P25_MODE, "County P25")
                o = IDS.sub(lambda m: f"{m[1]}-{m[2] or ''}{int(round(move(int(m[3]) / 1000) * 1000))}", o)
            return o
        node = data_schema(ev["type"])
        if node is not None:
            d = c._retime(node, resolver, d, move)
        d = words(d)
        if isinstance(d.get("minute"), int):
            d["minute"] = int(c.T0 // 60)
        if "-live-" in str(d.get("id")):
            d["end"] = d["start"] + 1.0
        if ev["type"] == "source_event":
            d["event"]["comments"] = [[move(at), text] for at, text in d["event"]["comments"]]
        if ev["type"] in ("page", "page_text"):
            d["id"] = pages.setdefault(d["id"], f"page-{len(pages) + 1:04d}")
        for key, fixed in (("stt_wait_s", 0.2), ("stt_run_s", 0.4)):
            if key in d:
                d[key] = fixed
        ev.update(seq=k + 1, at=c.T0 + 1.0, data=d)
        if ev.get("audio"):
            ev["audio"] = tiny_clip()
    body["epoch"] = EPOCH
    text = json.dumps(body)
    for word in [str(tmp), "pytest", "/private/", "/Users/", "/home/"]:
        assert len(word) < 3 or word not in text, f"the recording still names this machine or run: {word!r}"
    return body


def shape(o):
    """A document's keys at every depth, its values left out: what a recorded vector and the code must share."""
    if isinstance(o, dict):
        return {k: shape(v) for k, v in sorted(o.items())}
    if isinstance(o, list):
        return [shape(v) for v in o]
    return type(o).__name__ if o is None or isinstance(o, (bool, str)) else "number"


# -- everything a station sends, in one run --------------------------------------------------------------------------------

def everything(lv: Lane, st, monkeypatch) -> dict[str, str]:
    """Put the station with everything (Live.rich_station) through every path that queues an event, and wait until the
    hub has them all. The clip ids by what they are. What is real: the app's own handlers from the finished segment or
    call on (App._segment, _call, _call_system, _p25_activity, retranscribe, _retext, the incident sources' runner),
    speech-to-text's queue, the outbox, the link and the hub. What stands in: whisper (Whisper above), the live tap's
    memory of an alarm raised at the key-up, whisper's queue length for the hand-over to the pool, and the hub's word
    that Amazon Transcribe is up (as its set_channel_prefs job gives it)."""
    from sizeup.cad.alarms import STUCK_REPEATS
    from sizeup.sources.runner import SourceRunner
    from tests.fake_feed import FakeFeed
    from tests.test_app import wait
    assert wait(lambda: "p25_systems" in st.link.hub_caps, 30)
    ids: dict[str, str] = {}
    t = time.time() - 300.0
    ids["fm"] = fm(st, at=t)
    ids["fm_cut"] = fm(st, at=t + 20, cut=True, radio=None)                     # keyup, id_note
    ids["fm_emergency"] = fm(st, at=t + 30, emergency=True, radio=0x7302)       # emergency, emergency_via
    key = st._fm_emergency("Fireground 1", t + 40, [("7303", True)])            # the alarm at the key-up ...
    monkeypatch.setattr(st.tap, "alarm_key", lambda label, start, end: key if abs(start - (t + 40)) < 1 else "")
    ids["fm_live_alarm"], ids["fm_after_live_alarm"] = key, fm(st, at=t + 40, emergency=True, radio=0x7303)   # alarm_key
    page(st, at=t + 50)
    st._call(p25_call(st, at=t + 70))                                           # fm, call_id, turn, pairs, ours
    ids["p25"] = "4101-1794990000_476075000.0-call_1"
    st._call(p25_grant_cut_call(st, at=t + 75))                                # cut (#819)
    ids["p25_grant_cut"] = "4101-1794990060_476075000.0-call_2-t1"
    rec = lv.tmp / "tr" / "4101-1794990050_476075000.0-call_3.json"             # trunk-recorder's own record of the call
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"talkgroup": 4101, "emergency": 1, "srcList": [{"src": 990002, "time": 1794990050,
                                                                              "pos": 0.0, "emergency": 1, "tag": "Car 2"}],
                               "freqList": [{"freq": 476075000, "time": 1794990050, "pos": 0.0, "len": 2.0}]}))
    em = p25_call(st, name=rec.stem, at=t + 80, emergency=True, radio="990002", alias="Car 2")
    em.wav_path = str(rec.with_suffix(".wav"))
    st._call(em)                                                                # emergency, evidence
    ids["p25_emergency"] = rec.stem
    st.tg.set(4102, record=False)
    monkeypatch.setattr(st, "_p25_ours", lambda call, turns: False)             # (someone else's: it may be found stuck)
    for k in range(STUCK_REPEATS):                                              # the third in a row: stuck
        name = f"4102-17949901{k}0_476075000.0-call_{5 + k}"
        st._call(p25_call(st, name=name, tg=4102, at=t + 100 + 20 * k, emergency=True, radio="990003"))   # kept, flag_stuck
        ids[f"p25_flag_{k}"] = name
    ids["airband"] = airband(st, at=t + 170)
    ids["airband_weak"] = airband(st, at=t + 180, weak=True)
    context_call(st, at=t + 190)
    ids["context"] = "7001-1794990000_770181250.0-call_2"
    context_call(st, name="7002-1794990000_770181250.0-call_4", tg=7002, at=t + 195)   # record only: status off
    ids["context_record_only"] = "7002-1794990000_770181250.0-call_4"
    context_call(st, name="7001-1794990050_770181250.0-call_8", at=t + 197, late=True)  # late (#747)
    ids["context_late"] = "7001-1794990050_770181250.0-call_8"
    st._p25_activity("p25:east:7003", int((t + 200) // 60), 3, ["880001", "880002"])
    def all_there() -> bool:                 # every transmission so far is on the hub, with its words or without
        got = {ev["data"]["id"] for ev in lv.events("traffic")}
        return all(v in got for k, v in ids.items() if k != "fm_live_alarm") and st.link.outbox.pending()[0] == 0
    assert wait(all_there, 60), "the first events did not reach the hub"
    # the hub transcribes: Amazon Transcribe for one channel (as the hub's set_channel_prefs job says), the pool for a
    # context clip while this station's whisper is behind
    st.set_channel_prefs({"Fireground 1": {"engine": "aws"}}, {"ok": True, "why": "", "reserve": []})
    ids["fm_aws"] = fm(st, at=t + 210, radio=0x7304)                            # stt aws, transcript_pending aws
    st.set_channel_prefs({"Fireground 1": {"engine": "both"}}, {"ok": True, "why": "", "reserve": []})
    ids["fm_both"] = fm(st, at=t + 220, radio=0x7305)                           # stt both
    st.set_channel_prefs({}, {"ok": False, "why": "", "reserve": []})
    monkeypatch.setattr(st.link, "pool_has_room", lambda seconds=None: True)
    monkeypatch.setattr(st.link, "pool_gpu_idle", lambda: True)
    monkeypatch.setattr(st.link, "pool_takes", lambda: True)
    monkeypatch.setattr(st.link, "pool_takes_bg", lambda: True)       # (#896: Auto · background, its whisper behind)
    real_stats = st.stt.stats
    monkeypatch.setattr(st.stt, "stats", lambda: dict(real_stats(), queued=50, low=50, context=50, service_s=16.0))
    context_call(st, name="7001-1794990100_770181250.0-call_6", at=t + 230)     # transcript_pending pool, stt_class
    ids["context_pool"] = "7001-1794990100_770181250.0-call_6"
    monkeypatch.setattr(st.stt, "stats", real_stats)
    monkeypatch.setattr(st.link, "pool_takes_bg", lambda: False)      # (the rest on this station's whisper)
    assert wait(all_there, 60), "the hub-transcribed events did not reach the hub"
    # new words: a re-run that changes the text, one for the alternative, and the forms App._retext's callers send
    lv.whisper.again = True
    assert st.retranscribe([ids["fm"]]) == 1                                    # retext, rerun
    assert st.retranscribe([ids["airband"]], True) == 1                         # alt_text
    assert wait(lambda: len(lv.events("rerun")) >= 1 and len(lv.events("alt_text")) >= 1, 30)
    # stt_prompt_time, alt, catchup, vocab_v, late
    st._retext(ids["fm_cut"], SAID_AGAIN, "done", 0.8, "m.bin", alt={"text": SAID, "model": "other.bin"},
               quiet=True, vocab_v="terms-7", prompted=True, late=True)
    # an incident source's adapter on the station
    feed = FakeFeed(source="demo", label="Demo feed")
    st.sources_runner = SourceRunner([feed], st._source_out, log=lambda m: None)
    now = st.cad.clock()
    feed.put("S1", type="ALARM", location="12 Example Ave", units=("E5",), times={"dispatched": now - 20},
             unit_times={"E5": {"dispatched": now - 20, "responding": now - 5}},
             comments=((now - 10, "caller reports smoke"),), extra={"jurisdiction": "Northfield"}, aliases=("S0",))
    st.sources_runner.step(st.sources_runner.runs[0])                           # source_event, source_seen, source_health
    assert wait(lambda: all(lv.events(k) for k in ("source_event", "source_seen", "source_health")), 30)
    assert wait(lambda: len(lv.events("retext")) >= 2 and all_there(), 30)
    return ids
