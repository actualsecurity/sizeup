"""#927: a transmission the station's whisper transcribes is on the hub's radio timeline at its close ("Transcribing on
the station (whisper)", status pending, with its clip, talker, radio IDs, emergency flags, call and key-up, late), and its
text fills the same row later as a "retext" with fill: never a second "traffic" (the hub drops a resent id), never
re-ordered. What the text sets off (the text hooks: intel, status hints, possible incoming, the airport; the alarms'
words; ATC; the station's run) happens once, on the fill; the emergency button alarms at the close. Only to a hub that
says it takes it (caps "traffic_pending"); an older hub gets one event after the text, as before. The hub's pool can take
a clip back from the station's whisper (stt_withdraw). Every id, name, unit and word here is made up."""
from __future__ import annotations

import base64
import json
import threading
import time
import types

import numpy as np
import pytest

from sizeup.app import App, LANDED_MAX, PENDING_CAP, STATION_WHISPER, WHISPER_FALLBACK_STATION
from sizeup.cloud import client as wire
from sizeup.cloud import hub as hub_mod
from sizeup.cloud.station import clip_8k
from sizeup.radio.fm import Channel, Segment
from sizeup.radio.p25 import GRANT_CUT, Call, Turn
from sizeup.stt import SHED
from tests.test_app import voice, wait
from tests.test_cloud import HubRun, base, call, hub_cfg

SAID = "Engine 5 on location, nothing showing"
MAYDAY = "mayday mayday mayday Engine 5 trapped"
TG = 307


class Gate:
    """Stands in for whisper-server: the words by the clip's length in seconds, each answer held until `go` is set (the
    station's whisper behind), the clips it was asked for in `asked`."""

    def __init__(self, texts: dict[int, str], held: bool = True):
        self.texts, self.asked, self.go = texts, [], threading.Event()
        if not held:
            self.go.set()

    def transcribe(self, audio, prompt="", **_kw):
        self.asked.append(round(len(audio) / 16000))
        assert self.go.wait(30), "the test never let whisper answer"
        return self.texts[round(len(audio) / 16000)], 0.9


def link(caps, **more) -> types.SimpleNamespace:
    """The station's hub link as the App reads it: the hub's caps (and, given, its pool's word)."""
    return types.SimpleNamespace(hub_caps=frozenset(caps), start=lambda: None, stop=lambda: None,
                                 status_row=lambda: None, **more)


class Station:
    """A station (the all-in-one App, forwarding) whose events are kept in order and posted to a real hub on flush(),
    each with its clip, as the outbox would (8 kHz WAV in base64, the next seq)."""

    def __init__(self, tmp_path, monkeypatch, texts: dict[int, str], caps=(PENDING_CAP,), held: bool = True, **cfg):
        self.app = App(base(clips_dir=str(tmp_path / "st" / "clips"), cad_db=str(tmp_path / "st" / "cad.sqlite"),
                            p25_talkgroups={TG: "Fire/EMS 15"}, **cfg))
        monkeypatch.setattr(self.app, "forwarding", lambda: True)
        self.sent: list[tuple[str, dict, str | None]] = []
        self.app.events = lambda kind, data, clip: self.sent.append((kind, json.loads(wire.dumps(data)), clip))
        self.app.link = link(caps)
        self.whisper = Gate(texts, held)
        self.app.stt.client = self.whisper
        self.app.stt.start()
        self.posted = 0
        self.seq = 0

    def kinds(self, tid: str | None = None) -> list[str]:
        return [k for k, d, _c in self.sent if tid is None or d.get("id") == tid]

    def of(self, kind: str, tid: str) -> list[dict]:
        return [d for k, d, _c in self.sent if k == kind and d.get("id") == tid]

    def flush(self, hub: HubRun) -> None:
        """Every event not posted yet, in order, one post each (as the forwarder would batch them: the same order)."""
        while self.posted < len(self.sent):
            kind, data, clip = self.sent[self.posted]
            self.posted += 1
            self.post(hub, kind, data, clip)

    def post(self, hub: HubRun, kind: str, data: dict, clip: str | None = None) -> None:
        self.seq += 1
        audio = clip_8k(self.app.clip(clip)) if clip else None
        ev = {"seq": self.seq, "at": time.time(), "type": kind, "data": data,
              "audio": base64.b64encode(audio).decode() if audio else None}
        code, out = call(hub.url + "/api/station/events", "POST", {"epoch": "e1", "events": [ev]})
        assert code == 200 and out.get("applied") == 1 and not out.get("rejected"), out

    def stop(self) -> None:
        self.whisper.go.set()
        self.app.stop()


@pytest.fixture
def hub(tmp_path):
    h = HubRun(hub_cfg(tmp_path))
    yield h
    h.close()


@pytest.fixture
def made(tmp_path, monkeypatch):
    out: list[Station] = []

    def make(texts, **kw) -> Station:
        st = Station(tmp_path, monkeypatch, texts, **kw)
        out.append(st)
        return st
    yield make
    for st in out:
        st.stop()


def fm(st: Station, at: float, seconds: float = 3.0, bursts=()) -> str:
    """A transmission on FG1 (a fireground: whisper on the station) at `at`. Its clip id."""
    st.app._segment(Channel(453.0375, "FG1"), Segment(at, at + seconds, voice(seconds), 20.0, list(bursts)))
    return f"FG1-{int(at * 1000)}"


def row(hub: HubRun, tid: str) -> dict:
    return hub.app.cad.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,)) or {}


def held(app, tid: str) -> list:
    return [t for t in list(app.store.loose) + [t for i in app.store.incidents for t in i.traffic] if t.id == tid]


def hooks(hub: HubRun) -> dict[str, list]:
    """What the text hooks and the intel engine hear on the hub, by traffic id."""
    heard: dict[str, list] = {"text": [], "late": [], "context": [], "intel": []}
    comms = hub.app.cad.comms
    for name in ("text", "late", "context"):
        real = getattr(comms, f"on_{name}_text" if name != "text" else "on_text")

        def spy(tid, _real=real, _name=name):
            heard[_name].append(tid)
            if _real is not None:
                _real(tid)
        setattr(comms, f"on_{name}_text" if name != "text" else "on_text", spy)
    real_consider = hub.app.cad.intel.consider
    hub.app.cad.intel.consider = lambda tid: (heard["intel"].append(str(tid)), real_consider(tid))[-1]
    return heard


# -- a whisper clip: at its close, then its text in place -----------------------------------------------------------------

def test_a_whisper_clip_is_on_the_hub_at_its_close_and_its_text_fills_the_same_row_in_air_time_order(hub, made):
    st = made({3: SAID, 4: "Ladder 2 on location"})
    t0 = time.time() - 60
    a = fm(st, t0, 3.0)
    b = fm(st, t0 + 10, 4.0)
    # at the close, before whisper said anything: each on its way up as "Transcribing", with its clip
    assert st.kinds() == ["traffic", "traffic"]
    for tid, (_k, d, clip) in zip((a, b), st.sent):
        assert d["id"] == tid and clip == tid and d["status"] == "pending" and d["text"] == "" and d["raw"] == ""
        assert d["model"] == WHISPER_FALLBACK_STATION and d["transcript_pending"] == "station" and d["attach"] is True
    st.flush(hub)
    for tid in (a, b):
        r = row(hub, tid)
        assert (r["status"], r["text"], r["model"]) == ("pending", "", WHISPER_FALLBACK_STATION)
        assert hub.app.has_clip(tid)                                   # its audio from the close: playable now
    rows = hub.get("/api/cad/comms")["rows"]
    assert [x["id"] for x in rows if x["id"] in (a, b)] == [b, a]      # newest first, by air time
    # whisper answers: the same rows fill in place, one "retext" each, never a second "traffic"
    st.whisper.go.set()
    assert wait(lambda: len([k for k in st.kinds() if k == "retext"]) == 2, 15)
    assert st.kinds(a) == ["traffic", "retext"] and st.kinds(b) == ["traffic", "retext"]
    fill = st.of("retext", a)[0]
    assert fill["fill"] is True and fill["text"] == SAID and fill["status"] == "done" and fill["model"] == "m.bin"
    assert fill["engine"] == STATION_WHISPER and isinstance(fill["stt_wait_s"], float) and fill["stt_class"] == "urgent"
    st.flush(hub)
    assert (row(hub, a)["text"], row(hub, a)["status"], row(hub, a)["model"]) == (SAID, "done", "m.bin")
    assert row(hub, b)["text"] == "Ladder 2 on location" and row(hub, a)["text_at"] is not None
    rows = hub.get("/api/cad/comms")["rows"]
    assert [x["id"] for x in rows if x["id"] in (a, b)] == [b, a]      # never re-ordered
    assert [len(held(hub.app, tid)) for tid in (a, b)] == [1, 1]       # one transmission each on the hub
    t = held(hub.app, a)[0]
    assert t.text == SAID and not hub.app.mdb.is_waiting(a) and not getattr(t, "history", None)
    # the station's run, with whisper's wait and run, as when it came in one event
    runs = hub.app.stt_runs.of(a)
    assert [(r["engine"], r["role"], r["text"]) for r in runs] == [(STATION_WHISPER, "text", SAID)]
    assert runs[0]["wait_s"] == fill["stt_wait_s"]


def test_the_stations_own_store_and_radio_log_have_the_row_at_the_close_and_fill_it_the_same_way(made):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    [t] = held(st.app, tid)
    assert (t.status, t.text) == ("pending", "")
    r = st.app.cad.store.one("SELECT status, text, model FROM radio_traffic WHERE id = ?", (tid,))
    assert (r["status"], r["text"], r["model"]) == ("pending", "", WHISPER_FALLBACK_STATION)
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    assert len(held(st.app, tid)) == 1 and (held(st.app, tid)[0].text, held(st.app, tid)[0].status) == (SAID, "done")
    r = st.app.cad.store.one("SELECT status, text, model FROM radio_traffic WHERE id = ?", (tid,))
    assert (r["status"], r["text"], r["model"]) == ("done", SAID, "m.bin")
    assert tid not in st.app._landed                                 # (its fill went up: nothing left to remember)


# -- shed, no speech, the pool --------------------------------------------------------------------------------------------

def behind(st: Station) -> str:
    """Whisper busy with one clip (held), and the next one waiting behind it: that one's id."""
    fm(st, time.time() - 40, 3.0)
    assert wait(lambda: st.whisper.asked == [3], 10)
    return fm(st, time.time() - 30, 4.0)


def test_a_shed_clip_fills_its_row_as_skipped_for_the_catch_up(hub, made):
    st = made({3: SAID})
    tid = behind(st)
    [(_p, item, _a, _c, _q)] = st.app.stt.withdraw(ids={tid})       # (out of the queue, as the queue sheds one)
    st.app._stt_dropped(item, f"{SHED} (waited too long)")
    assert st.kinds(tid) == ["traffic", "retext"]
    fill = st.of("retext", tid)[0]
    assert (fill["status"], fill["text"], fill["fill"]) == ("skipped", "", True) and SHED in fill["model"]
    st.flush(hub)
    r = row(hub, tid)
    assert r["status"] == "skipped" and SHED in r["model"]
    assert [x["id"] for x in hub.app.cad.comms.shed_clips(time.time() - 3600)[0]] == [tid]   # the catch-up's


def test_a_key_up_with_no_speech_fills_its_row_as_no_speech(hub, made):
    st = made({3: ""}, held=False)
    tid = fm(st, time.time() - 30)
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    assert st.of("retext", tid)[0]["status"] == "no_speech"
    heard = hooks(hub)
    st.flush(hub)
    assert (row(hub, tid)["status"], row(hub, tid)["text"]) == ("no_speech", "")
    assert heard["text"] == [tid]                                   # told once: on its text, not its close


def test_a_clip_whisper_sheds_to_the_hubs_pool_after_its_close_goes_there(hub, made, monkeypatch):
    st = made({3: SAID})
    st.app.link = link((PENDING_CAP,), pool_has_room=lambda seconds=None: True, pool_takes=lambda: True)
    tid = behind(st)
    st.flush(hub)
    pooled = []
    monkeypatch.setattr(hub.app, "_pool_clip", lambda item, key, wav, cls=None, fresh=True:
                        pooled.append((item.id, key, wav is not None, cls, fresh, item.status, item.stt)))
    [(_p, item, _a, _c, _q)] = st.app.stt.withdraw(ids={tid})
    st.app._stt_dropped(item, f"{SHED} (waited too long)")
    fill = st.of("retext", tid)[0]
    assert (fill["transcript_pending"], fill["status"], fill["model"], fill["fill"]) == ("pool", "remote", "pool", True)
    st.flush(hub)
    assert pooled == [(tid, "FG1", True, fill.get("stt_class"), False, "pending", "pool")]
    assert row(hub, tid)["status"] == "pending"


# -- the alarms: the button at the close, the words on the text, one alarm -------------------------------------------------

def test_the_emergency_button_alarms_at_the_close_and_a_mayday_on_the_text_is_the_same_alarm(hub, made):
    from sizeup.radio.mdc1200 import encode
    st = made({3: MAYDAY})
    tid = fm(st, time.time() - 30, bursts=[encode(0x00, 0x80, 0x6401).astype(np.float32)])     # MDC emergency
    assert st.kinds(tid) == ["alarm", "traffic"]                    # the button first, then the row: both at the close
    st.flush(hub)
    alarms = lambda: hub.app.cad.store.q("SELECT * FROM alarms")    # noqa: E731
    what = lambda: hub.app.cad.alarms.get(alarms()[0]["id"])["what"]    # noqa: E731
    assert wait(lambda: len(alarms()) == 1, 10)
    [a] = alarms()
    assert a["traffic_id"] == tid and a["emergency"] == 1 and what() == "emergency button (MDC)"
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["alarm", "traffic", "retext"], 15)
    st.flush(hub)
    assert wait(lambda: what() == "emergency button (MDC) + mayday", 10)
    assert len(alarms()) == 1 and alarms()[0]["traffic_id"] == tid


def test_a_mayday_with_no_button_alarms_on_the_text_never_at_the_close(hub, made):
    st = made({3: MAYDAY})
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    assert row(hub, tid)["status"] == "pending"
    time.sleep(0.3)
    assert hub.app.cad.store.q("SELECT * FROM alarms") == []
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert wait(lambda: len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1, 10)
    time.sleep(0.3)
    assert len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1


def test_a_p25_emergency_call_alarms_at_the_close_and_once(hub, made):
    st = made({2: MAYDAY})
    now = time.time() - 30
    st.app._call(Call(f"{TG}-1-call_1", TG, "Fire/EMS 15", now, now + 2, ["41001"], voice(2.0), "",
                      [Turn(0.0, "41001", "41001", at=now)], emergency=True))
    tid = f"{TG}-1-call_1"
    assert st.kinds(tid) == ["alarm", "traffic"] and st.of("traffic", tid)[0]["emergency"] is True
    st.flush(hub)
    assert wait(lambda: len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1, 10)
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["alarm", "traffic", "retext"], 15)
    st.flush(hub)
    assert wait(lambda: hub.app.cad.store.q("SELECT text FROM alarms")[0]["text"] == MAYDAY, 10)
    assert len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1


# -- the text hooks (intel, hints, incoming, the airport) never on a row still transcribing --------------------------------

def test_the_text_hooks_and_intel_hear_a_row_once_on_its_text_never_while_it_is_transcribing(hub, made):
    st = made({3: SAID})
    heard = hooks(hub)
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    assert row(hub, tid)["status"] == "pending"
    assert heard == {"text": [], "late": [], "context": [], "intel": []}
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert heard["text"] == [tid] and heard["intel"] == [tid] and heard["late"] == []


# -- late calls (#747) and split parts (#816 / #821) ----------------------------------------------------------------------

def test_a_late_call_is_late_at_its_close_and_on_its_text_and_reaches_only_the_radio_log(hub, made):
    st = made({2: SAID})
    heard = hooks(hub)
    now = time.time() - 600
    call_ = Call(f"{TG}-2-call_2", TG, "Fire/EMS 15", now, now + 2, ["41002"], voice(2.0), "",
                 [Turn(0.0, "41002", "41002", at=now)], late=True)
    st.app._call(call_)
    tid = f"{TG}-2-call_2"
    assert st.of("traffic", tid)[0]["late"] is True and st.of("traffic", tid)[0]["status"] == "pending"
    st.flush(hub)
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    assert st.of("retext", tid)[0]["late"] is True
    st.flush(hub)
    assert row(hub, tid)["text"] == SAID
    assert heard["late"] == [tid] and heard["text"] == [] and heard["intel"] == []


def test_each_split_part_of_a_call_has_its_own_row_at_the_close_and_its_own_text(hub, made):
    st = made({2: "Engine 5 to Control", 3: "Control, go ahead"})
    now = time.time() - 60
    st.app._call(Call(f"{TG}-3-call_3", TG, "Fire/EMS 15", now, now + 5, ["41003", "41004"], voice(5.0), "",
                      [Turn(0.0, "41003", "41003", 2.0, at=now),
                       Turn(2.0, "41004", "41004", 3.0, at=now + 2.0, via=GRANT_CUT)]))
    parts = [d["id"] for k, d, _c in st.sent if k == "traffic"]
    assert len(parts) == 2 and all(p.startswith(f"{TG}-3-call_3") for p in parts)
    assert [st.of("traffic", p)[0]["turn"] for p in parts] == [0, 1]
    assert all(st.of("traffic", p)[0]["status"] == "pending" for p in parts)
    st.flush(hub)
    st.whisper.go.set()
    assert wait(lambda: all(st.kinds(p) == ["traffic", "retext"] for p in parts), 15)
    st.flush(hub)
    assert [row(hub, p)["text"] for p in parts] == ["Engine 5 to Control", "Control, go ahead"]
    assert [row(hub, p)["call_id"] for p in parts] == [f"{TG}-3-call_3"] * 2


# -- an older hub; a resent event; a station that never sends the text -----------------------------------------------------

def test_an_older_hub_gets_one_traffic_event_after_the_text_as_before(made):
    st = made({3: SAID}, caps=("p25_systems", "stt_input"))
    tid = fm(st, time.time() - 30)
    assert st.sent == [] and held(st.app, tid) == []                # nothing until whisper says
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic"], 15)
    time.sleep(0.2)
    assert st.kinds(tid) == ["traffic"]
    d = st.of("traffic", tid)[0]
    assert (d["status"], d["text"], d["model"]) == ("done", SAID, "m.bin") and "transcript_pending" not in d


def test_a_resent_event_at_the_close_is_one_row_and_never_undoes_its_text(hub, made):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    _k, pending, clip = st.sent[0]
    st.flush(hub)
    st.post(hub, "traffic", pending, clip)                          # (the outbox sends it again: a quarantine's retry)
    assert len(hub.app.cad.store.q("SELECT id FROM radio_traffic WHERE id = ?", (tid,))) == 1
    assert len(held(hub.app, tid)) == 1
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    st.post(hub, "traffic", pending, clip)                          # ... and once more, after its text
    assert (row(hub, tid)["text"], row(hub, tid)["status"]) == (SAID, "done") and len(held(hub.app, tid)) == 1


def test_a_fill_for_a_row_another_engine_filled_first_is_kept_as_a_run_not_the_text(hub, made):
    st = made({3: SAID})
    heard = hooks(hub)
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    hub.app._retext(tid, "Engine 5 on location", "done", 0.9, "gpu-worker:large", why="pool")    # (another engine first)
    assert heard["text"] == [tid]
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert row(hub, tid)["text"] == "Engine 5 on location"
    assert [(r["engine"], r["role"], r["text"]) for r in hub.app.stt_runs.of(tid)] == [(STATION_WHISPER, "late", SAID)]
    assert heard["text"] == [tid] and heard["intel"] == [tid]       # the hooks heard it once: on the first text


def evidence(hub: HubRun, monkeypatch, boot: str = "boot-a") -> None:
    """The hub has heard a station (connected, its machine's boot) as a state post would say."""
    monkeypatch.setattr(hub.app.hub, "connected", lambda: True)
    hub.app.hub.boot_id = boot


def test_a_row_waiting_behind_a_two_hour_backlog_stays_transcribing(hub, made, monkeypatch):
    st = made({3: SAID})
    evidence(hub, monkeypatch)
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    assert hub.app.mdb.is_waiting(tid)
    assert hub.app._flag_waiting(time.time() + 2 * 3600) == 0       # (an URGENT clip may wait that long: no evidence)
    assert row(hub, tid)["status"] == "pending"
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert (row(hub, tid)["status"], row(hub, tid)["text"]) == ("done", SAID)


@pytest.mark.parametrize("how", ["reboot", "offline", "too old"])
def test_a_row_whose_station_rebooted_is_shed_for_the_catch_up_and_its_text_still_fills_it(hub, made, monkeypatch, how):
    from sizeup.stt import MAX_WAIT_S
    st = made({3: SAID})
    evidence(hub, monkeypatch)
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    later = time.time() + 60
    if how == "reboot":
        hub.app.hub.boot_id = "boot-b"                              # (#795: the station's machine came up again)
    elif how == "offline":
        monkeypatch.setattr(hub.app.hub, "connected", lambda: False)
        hub.app.hub.last_seen = time.time()
        later = time.time() + hub_mod.STATION_OFFLINE_S + 1
    else:
        later = time.time() + MAX_WAIT_S + hub_mod.STATION_GRACE_S + 1
    assert hub.app._flag_waiting(later) == 1
    r = row(hub, tid)
    assert r["status"] == "skipped" and SHED in r["model"] and r["text"] == ""
    assert [x["id"] for x in hub.app.cad.comms.shed_clips(time.time() - 3600)[0]] == [tid]   # the catch-up's now
    assert hub.app._flag_waiting(later + 60) == 0                   # (once)
    st.whisper.go.set()                                             # (its text after all: late, but the text)
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert (row(hub, tid)["status"], row(hub, tid)["text"]) == ("done", SAID) and not hub.app.mdb.is_waiting(tid)


def test_a_fill_for_a_row_no_longer_in_memory_fills_the_radio_log_and_alarms_on_a_mayday(hub, made):
    from sizeup.incidents import Traffic
    st = made({3: MAYDAY})
    heard = hooks(hub)
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    for k in range(1000):                                           # (an evening's newer rows: it leaves memory)
        hub.app.store.loose.append(Traffic(f"FG1-{k}", "FG1", 1.0 + k, 2.0 + k, "", status="done"))
    assert held(hub.app, tid) == [] and row(hub, tid)["status"] == "pending"
    st.post(hub, "traffic", st.sent[0][1], st.sent[0][2])           # (sent again now: never a second row)
    assert held(hub.app, tid) == [] and row(hub, tid)["status"] == "pending"
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.flush(hub)
    assert (row(hub, tid)["text"], row(hub, tid)["status"]) == (MAYDAY, "done") and not hub.app.mdb.is_waiting(tid)
    assert wait(lambda: len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1, 10)
    assert heard["text"] == [tid]
    st.post(hub, "traffic", st.sent[0][1], st.sent[0][2])           # ... and after its text: it never blanks it
    assert (row(hub, tid)["text"], row(hub, tid)["status"]) == (MAYDAY, "done")


def test_a_fill_that_comes_before_its_transmission_waits_for_it(hub, made):
    st = made({2: MAYDAY})
    heard = hooks(hub)
    now = time.time() - 30
    st.app._call(Call(f"{TG}-4-call_4", TG, "Fire/EMS 15", now, now + 2, ["41005"], voice(2.0), "",
                      [Turn(0.0, "41005", "41005", at=now)], emergency=True))
    tid = f"{TG}-4-call_4"
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["alarm", "traffic", "retext"], 15)
    alarm, pending, fill = (x for x in st.sent if x[1].get("id") == tid)
    st.post(hub, *alarm[:2], alarm[2])
    st.post(hub, "retext", fill[1])                                 # (its traffic held back: quarantined, sent later)
    assert row(hub, tid) == {} and heard["text"] == []
    st.post(hub, "traffic", pending[1], pending[2])
    r = row(hub, tid)
    assert (r["text"], r["status"]) == (MAYDAY, "done") and not hub.app.mdb.is_waiting(tid)
    assert hub.app.mdb.early_take(tid) is None
    assert wait(lambda: hub.app.cad.store.q("SELECT text FROM alarms")[0]["text"] == MAYDAY, 10)
    assert len(hub.app.cad.store.q("SELECT * FROM alarms")) == 1 and heard["text"] == [tid]


def test_a_hub_restart_between_the_close_and_the_fill_still_fills_the_row(tmp_path, made):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    first = HubRun(hub_cfg(tmp_path))
    try:
        st.flush(first)
    finally:
        first.close()
    again = HubRun(hub_cfg(tmp_path))                               # (a redeploy: from hub.sqlite and cad.sqlite)
    try:
        assert again.app.mdb.is_waiting(tid) and row(again, tid)["status"] == "pending"
        st.whisper.go.set()
        assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
        st.flush(again)
        assert (row(again, tid)["text"], row(again, tid)["status"]) == (SAID, "done")
        assert not again.app.mdb.is_waiting(tid)
    finally:
        again.close()


def test_a_row_still_transcribing_reaches_the_radio_logs_rules_that_need_no_words(hub, made, monkeypatch):
    st = made({3: SAID})
    seen = []
    real = hub.app.cad.radiolog.consider
    monkeypatch.setattr(hub.app.cad.radiolog, "consider", lambda tid: (seen.append(tid), real(tid))[1])
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    assert seen == [tid]                                            # (a fireground's links: at its close)


def test_a_second_answer_after_the_fill_sends_nothing_and_adds_no_row(made):
    st = made({3: SAID}, held=False)
    tid = fm(st, time.time() - 30)
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    [item] = held(st.app, tid)
    item.status = "done"
    st.app._transcribed(item)                                       # (a stop handing on a clip whisper just finished)
    assert st.kinds(tid) == ["traffic", "retext"] and len(held(st.app, tid)) == 1


def test_a_loose_row_at_its_close_is_written_with_its_fill_one_write(made):
    st = made({3: SAID})
    n = st.app.store.writes
    tid = fm(st, time.time() - 30)
    assert held(st.app, tid) and st.app.store.writes == n          # (its close: in memory, not on disk yet)
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    assert st.app.store.writes == n + 1


def test_a_stations_saves_are_coalesced(tmp_path):
    from sizeup.incidents import IncidentStore, Traffic
    store = IncidentStore(set(), path=tmp_path / "incidents.json")
    store.save_delay = 0.3
    for k in range(5):                                              # (a close and its fill, and a few more)
        store.on_traffic(Traffic(f"FG1-{k}", "FG1", 1.0 + k, 2.0 + k, ""), attach=False)
        store.retext(f"FG1-{k}", "Engine 5 on location", "done")
    assert store.writes == 0
    assert wait(lambda: store.writes == 1, 5)
    assert len(json.loads((tmp_path / "incidents.json").read_text())["loose"]) == 5
    store.save()
    store.flush()                                                   # (a stop: written at once)
    assert store.writes == 2


def test_a_station_restart_keeps_what_is_on_the_hub_a_fill(tmp_path, monkeypatch, made):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    assert tid in st.app._landed
    n = st.app._stt_handoff(wait_s=0.0, mark_s=5.0)
    assert n["saved"] == 1
    saved = json.loads((st.app._stt_queue_file()).read_text())
    assert saved[0]["id"] == tid and saved[0]["landed"] is True
    st.app._landed.clear()                                          # (the next start: nothing in memory)
    again = made({3: SAID}, held=False)
    again.app.clips_dir = st.app.clips_dir
    (again.app._stt_queue_file()).write_text(json.dumps(saved))
    assert again.app._stt_resume() == 1
    assert wait(lambda: again.kinds(tid) == ["retext"], 15)          # its text only: the hub has its row
    assert again.of("retext", tid)[0]["fill"] is True


def test_the_landed_memory_is_bounded():
    assert LANDED_MAX >= 10 * 2000                                  # (whisper's queue holds QUEUE_MAX at most)


# -- the hub's pool takes clips back from the station's whisper (stt_withdraw) --------------------------------------------

def test_a_clip_withdrawn_while_it_waits_is_taken_out_and_nothing_goes_up_for_it(made):
    st = made({3: SAID, 4: "Ladder 2 on location"})
    t0 = time.time() - 60
    a = fm(st, t0, 3.0)                                             # on whisper now (held)
    assert wait(lambda: st.whisper.asked == [3], 10)
    b = fm(st, t0 + 10, 4.0)                                        # waiting behind it
    got = st.app.stt_withdraw([b, "FG1-1"])
    assert got == {"withdrawn": [b], "running": [], "sent": [], "unknown": ["FG1-1"]}
    st.whisper.go.set()
    assert wait(lambda: st.kinds(a) == ["traffic", "retext"], 15)
    time.sleep(0.3)
    assert st.kinds(b) == ["traffic"] and st.whisper.asked == [3]    # never transcribed, no "skipped" over the pool's text
    r = st.app.cad.store.one("SELECT status, model FROM radio_traffic WHERE id = ?", (b,))
    assert (r["status"], r["model"]) == ("remote", "pool")


def test_a_clip_withdrawn_once_whisper_began_sends_its_text_as_a_run_only(hub, made):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    assert wait(lambda: st.whisper.asked == [3], 10)
    assert st.app.stt_withdraw([tid]) == {"withdrawn": [], "running": [tid], "sent": [], "unknown": []}
    st.whisper.go.set()
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    fill = st.of("retext", tid)[0]
    assert fill["fill"] is True and fill["authoritative"] is False and fill["text"] == SAID
    st.flush(hub)
    assert row(hub, tid)["status"] == "pending"                    # (the pool's to say: never this text by itself)
    assert [(r["engine"], r["role"]) for r in hub.app.stt_runs.of(tid)] == [(STATION_WHISPER, "late")]


def test_a_withdraw_after_the_text_went_up_says_sent_and_one_never_landed_unknown(made):
    st = made({3: SAID}, held=False)
    tid = fm(st, time.time() - 30)
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    assert st.app.stt_withdraw([tid, "FG1-1", "../x"]) == {"withdrawn": [], "running": [], "sent": [tid],
                                                            "unknown": ["FG1-1", "../x"]}
    assert st.kinds(tid) == ["traffic", "retext"]


def test_a_withdraw_is_refused_for_what_is_not_a_list_of_ids(made):
    st = made({3: SAID})
    for bad in ("FG1-1", [1], ["x"] * 501):
        with pytest.raises(ValueError):
            st.app.stt_withdraw(bad)


def test_the_hub_asks_only_a_connected_station_that_says_it_takes_a_withdraw(hub, made, monkeypatch):
    st = made({3: SAID})
    tid = fm(st, time.time() - 30)
    st.flush(hub)
    asked = []
    monkeypatch.setattr(hub.app.hub, "remote", lambda fn, *a, **k: asked.append((fn, a)) or
                        {"withdrawn": list(a[0]), "running": [], "sent": [], "unknown": []})
    assert hub.app.stt_withdraw_station([tid]) is None and asked == []      # no station connected
    monkeypatch.setattr(hub.app.hub, "connected", lambda: True)
    hub.app.hub.status = {"caps": []}
    assert hub.app.stt_withdraw_station([tid]) is None and asked == []      # an older station: transcribes as ever
    hub.app.hub.status = {"caps": ["stt_withdraw"]}
    assert hub.app.mdb.is_waiting(tid)
    assert hub.app.stt_withdraw_station([tid, "../x"]) == {"withdrawn": [tid], "running": [], "sent": [], "unknown": []}
    assert asked == [("stt_withdraw", ([tid],))]
    assert not hub.app.mdb.is_waiting(tid)                          # (the pool's now: no sweep, no AWS resend for it)


def test_every_retext_of_a_station_says_its_engine(made):
    st = made({3: SAID}, held=False)
    tid = fm(st, time.time() - 30)
    assert wait(lambda: st.kinds(tid) == ["traffic", "retext"], 15)
    st.app._retext(tid, "Engine 5 on location, investigating", "done", 0.9, "m2.bin")
    st.app._retext(tid, "Engine 5 on location", "done", 0.9, "aws-transcribe")
    assert [d["engine"] for d in st.of("retext", tid)] == [STATION_WHISPER, STATION_WHISPER, "station-aws"]
