"""CAD (M1): store, numbering, roster seed, run cards, dispatch text/audio, the bridge from the
monitor, the HTTP API, and that nothing can transmit."""

import io
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher, runcards
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cad.store import SCHEMA_VERSION, TZ, CadError, Store
from sizeup.cad.transmit import AudioPttTransmitter, NullTransmitter, make_transmitter
from sizeup.config import ConfigError, load
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore

ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "data" / "westchester_apparatus.csv"
CALLSIGNS = load(ROOT / "config.example.toml").unit_callsigns
PAGE_TEXT = ("Port Chester Fire Department, respond to an activated fire alarm. 209 West Chester Avenue, "
             "cross of Pearl Street, Port Chester. Time out 1432.")


def local(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_cad(tmp_path=None, clock=None, **kw):
    s = Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else "", callsigns=CALLSIGNS, **kw)
    return Cad(s, log=lambda m: None, **({"clock": clock} if clock else {}))


def names(units):
    return [u["name"] for u in units]


def by_name(cad, name):
    return next(u for u in cad.store.units() if u["name"] == name)


# -- store ------------------------------------------------------------------------------------------

def test_roster_seeded_from_county_csv_once():
    st = Store()
    n = st.seed_units(ROSTER, "Port Chester", CALLSIGNS)
    units = {u["name"]: u for u in st.units()}
    assert n == len(units) == 16
    # Chiefs from the callsigns, apparatus in roster order; reserved numbers are not trucks.
    assert names(st.units())[:5] == ["2391 Chief of Department", "2392 1st Assistant Chief", "2393 2nd Assistant Chief",
                                     "2395 Immediate Past Chief", "2398 Duty Officer"]
    for gone in ("Engine 16", "Engine 58", "Tower Ladder 31"):
        assert gone not in units
    assert units["Engine 59"]["spare"] and units["Rescue 41"]["spare"] and not units["Engine 60"]["spare"]
    assert {u["status"] for u in units.values()} == {"available"}
    assert (units["Ladder 31"]["kind"], units["Tower Ladder 2"]["kind"], units["Marine 40"]["kind"]) == ("ladder", "tower", "boat")
    assert st.seed_units(ROSTER, "Port Chester", CALLSIGNS) == 0       # never re-seeded over edits
    # Another department's callsigns aren't ours.
    assert Store().seed_units(ROSTER, "Port Chester", {"2661": "2661 Chief"}) == 11


def test_numbers_sequential_per_local_year_and_pending_unnumbered(tmp_path):
    clock = Clock(local(2026, 12, 31, 23, 30))       # 04:30 UTC on Jan 1: still 2026 in Port Chester
    st = Store(tmp_path / "cad.sqlite", clock=clock)
    a = st.create_incident(type="Other")
    p = st.create_incident(type="Other", source="60 Control page", source_ref="monitor:x", status="pending")
    b = st.create_incident(type="Other")
    assert st.incident(a)["number"] == "PCFD-2026-0001" and st.incident(b)["number"] == "PCFD-2026-0002"
    assert st.incident(p)["number"] is None and st.incident(p)["status"] == "pending"
    clock.t = local(2027, 1, 1, 0, 30)
    assert st.accept(p)["number"] == "PCFD-2027-0001"                 # numbered when accepted
    assert st.incident(st.create_incident(type="Other"))["number"] == "PCFD-2027-0002"
    with pytest.raises(CadError):
        st.accept(p)                                                   # already active
    # Reopened from disk: same data, same schema version.
    st2 = Store(tmp_path / "cad.sqlite", clock=clock)
    assert st2.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert st2.incident(st2.create_incident(type="Other"))["number"] == "PCFD-2027-0003"
    with pytest.raises(sqlite_error()):
        st2.create_incident(type="Other", source="60 Control page", source_ref="monitor:x", status="pending")


def sqlite_error():
    import sqlite3
    return sqlite3.IntegrityError


def test_unit_status_times_and_close_clears(tmp_path):
    clock = Clock(local(2026, 10, 7, 14, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "MVA", "address": "Boston Post Rd"})["id"]
    e60 = by_name(cad, "Engine 60")["id"]
    cad.store.assign(iid, [e60], "JD")
    clock.t += 60
    cad.unit_status(e60, {"status": "enroute", "operator": "JD"})
    clock.t += 120
    cad.unit_status(e60, {"status": "onscene"})
    a = cad.store.incidents()[0]["assignments"][0]
    assert (a["enroute_at"] - a["dispatched_at"], a["onscene_at"] - a["dispatched_at"]) == (60, 180)
    with pytest.raises(CadError):
        cad.unit_status(e60, {"status": "lunch"})
    cad.close(iid, {"operator": "JD"})
    u = by_name(cad, "Engine 60")
    assert u["status"] == "available" and u["incident_id"] is None
    events = [e["text"] for e in cad.store.incidents()[0]["events"]]
    assert events[-1] == "Closed; cleared Engine 60" and "Engine 60: En route" in events
    # Off any incident, a status change goes to the unit log.
    cad.unit_status(e60, {"status": "out_of_service", "note": "pump repair"})
    assert cad.store.unit_log()[0]["text"] == "Engine 60: Out of service (pump repair)"


# -- run cards -------------------------------------------------------------------------------------

def test_recommend_follows_card_skipping_busy_and_out_of_service():
    cad = make_cad()
    iid = cad.create({"type": "Structure fire", "address": "209 Westchester Ave"})["id"]
    r = runcards.recommend(cad.store, "Structure fire", iid)
    assert names(r["units"]) == ["Engine 60", "Engine 61", "Engine 62", "Ladder 31", "Rescue 40", "2398 Duty Officer"]
    assert r["unfilled"] == []
    # Ladder 31 out of service, Tower Ladder 2 and Engine 61 committed elsewhere, 2398 too.
    other = cad.create({"type": "Other"})["id"]
    cad.unit_status(by_name(cad, "Ladder 31")["id"], {"status": "out_of_service", "note": "at the shop"})
    cad.store.assign(other, [by_name(cad, n)["id"] for n in ("Tower Ladder 2", "Engine 61", "2398 Duty Officer")])
    r = runcards.recommend(cad.store, "Structure fire", iid)
    assert names(r["units"]) == ["Engine 60", "Engine 62", "Engine 63", "Rescue 40"]
    assert r["unfilled"] == ["no ladder/tower available", "2398 Duty Officer not available (committed to PCFD-2026-0002)"
                             .replace("2026", str(datetime.now(TZ).year))]
    # Units already on this incident fill their slots; spares come after regular apparatus.
    cad.store.assign(iid, [by_name(cad, n)["id"] for n in ("Engine 60", "Engine 62", "Engine 63", "Rescue 40")])
    cad.store.assign(other, [by_name(cad, "Engine 64")["id"]])
    r = runcards.recommend(cad.store, "MVA with entrapment", other)
    assert names(r["units"]) == ["Rescue 41"] and r["unfilled"] == []          # the rest are already on it
    assert r["already"] == ["2398 Duty Officer", "Engine 61", "Engine 64", "Tower Ladder 2"]
    third = cad.create({"type": "Structure fire"})["id"]
    r = runcards.recommend(cad.store, "Structure fire", third)
    assert [(u["name"], u["spare"]) for u in r["units"]] == [("Engine 59", True), ("Rescue 41", True)]   # spares, last
    assert r["unfilled"][:2] == ["only 1 of 3 engine available", "no ladder/tower available"]


def test_run_cards_seeded_as_defaults_and_edits_validated():
    cad = make_cad()
    cards = {c["type"]: c for c in runcards.cards(cad.store)}
    assert len(cards) == 13 and all(c["is_default"] for c in cards.values())
    assert cards["Water / marine rescue"]["slots"][0] == {"unit": "Marine 40"}
    for bad in ([], [{"kind": "helicopter"}], [{"unit": "Engine 99"}], [{"kind": "engine", "count": 0}], "x"):
        with pytest.raises(CadError):
            cad.save_run_card({"type": "CO alarm", "slots": bad})
    c = cad.save_run_card({"type": "CO alarm", "slots": [{"kind": "engine", "count": 2}, {"unit": "2398 Duty Officer"}]})
    assert c["slots"] == [{"kind": "engine", "count": 2}, {"unit": "2398 Duty Officer"}] and not c["is_default"]
    cad.save_run_card({"type": "CO / gas", "rename_from": "CO alarm", "slots": [{"kind": "engine"}]})
    assert runcards.card(cad.store, "CO alarm") is None and runcards.card(cad.store, "CO / gas")
    cad.create({"type": "CO / gas"})
    with pytest.raises(CadError, match="open incident"):
        cad.delete_run_card({"type": "CO / gas"})
    assert runcards.seed(cad.store) == 0                                # never re-seeded over edits


def test_page_types_map_to_run_cards():
    assert runcards.from_page("Fire alarm", "fire", PAGE_TEXT) == "Fire alarm – commercial"
    assert runcards.from_page("Fire alarm", "fire", "residential fire alarm") == "Fire alarm – residential"
    assert runcards.from_page("MVA", "rescue", "mva with entrapment") == "MVA with entrapment"
    assert runcards.from_page("Working fire", "fire", "") == "Structure fire"
    assert runcards.from_page("Breathing problem", "ems", "") == "EMS assist"
    assert runcards.from_page("Unknown", "unknown", "") == "Other"


# -- dispatch text and audio --------------------------------------------------------------------------

def test_dispatch_text_as_spoken():
    inc = {"address": "209 Westchester Ave, Port Chester", "cross_street": "Pearl St", "type": "Structure fire"}
    card = {"spoken": "a structure fire"}
    t = dispatcher.build_text("Port Chester Fire", inc, ["Engine 60", "Ladder 31", "Rescue 40"], card,
                              at=local(2026, 10, 7, 14, 32))
    assert t == ("Port Chester Fire, Engine 60, Ladder 31, Rescue 40, respond to 209 Westchester Avenue, "
                 "cross of Pearl Street, for a structure fire. Time out 14:32.")
    assert dispatcher.speech(t + " 2398 Duty Officer") == (
        "Port Chester Fire, Engine 60, Ladder 31, Rescue 40, respond to 2 0 9 Westchester Avenue, "
        "cross of Pearl Street, for a structure fire. Time out 14 32. 23 98 Duty Officer")
    assert dispatcher.spoken_street("St Marys Pl, Rye Brook") == "St Marys Place, Rye Brook"   # other towns said
    assert dispatcher.spoken_street("12 N Main St") == "12 North Main Street"
    t = dispatcher.build_text("Port Chester Fire", {"type": "Other"}, ["2398 Duty Officer"], None, at=local(2026, 1, 2, 9, 5))
    assert t == "Port Chester Fire, 2398 Duty Officer, respond for an other. Time out 09:05."


def peak_hz(x, rate=16000):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec)]


@pytest.mark.skipif(not (shutil.which("say") and shutil.which("afconvert")), reason="macOS say/afconvert not here")
def test_dispatch_audio_tones_then_voice_twice(tmp_path):
    tones = [(349.1, 1.0), (311.1, 3.0)]
    path = dispatcher.render_audio("Port Chester Fire, Engine 60, respond to 209 Westchester Avenue.", tones, tmp_path,
                                   voice="Samantha")
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    assert abs(peak_hz(x[1000:15000]) - 349.1) < 2 and abs(peak_hz(x[17000:63000]) - 311.1) < 2
    voice = x[int((4 + dispatcher.GAP_S) * 16000):]
    assert len(voice) / 16000 > 4 and np.sqrt(np.mean(voice ** 2)) > 0.02      # the voice, read twice
    assert np.max(np.abs(x[64500:int(4.5 * 16000)])) < 0.01                    # the gap is quiet
    again = dispatcher.render_audio("Port Chester Fire, Engine 60, respond to 209 Westchester Avenue.", tones, tmp_path,
                                    voice="Samantha")
    assert again == path                                                        # cached: preview = dispatch


def test_no_tts_means_text_only_dispatch(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cad = make_cad()
    iid = cad.create({"type": "CO alarm", "address": "1 King St"})["id"]
    r = cad.dispatch(iid, {"unit_ids": [by_name(cad, "Engine 60")["id"]], "operator": "JD"})
    assert r["audio_url"] is None and "say" in r["audio_note"] and r["transmitted"] is False
    assert cad.store.incidents()[0]["dispatches"][0]["audio_note"] == r["audio_note"]


# -- the bridge from the monitor ------------------------------------------------------------------------

def monitor_incident(text=PAGE_TEXT, depts=("Port Chester FD",), geo=None):
    store = IncidentStore({"Port Chester FD", "Rye Brook FD"}, geocode=lambda d: geo)
    return store, store.on_page("p1", list(depts), parse(text), at=local(2026, 10, 7, 14, 32))


def test_page_for_port_chester_becomes_one_pending_incident():
    cad = make_cad()
    geo = {"lat": 41.0025, "lon": -73.6669, "label": "209 Westchester Ave, Port Chester", "kind": "address",
           "confidence": 0.91}
    store, inc = monitor_incident(geo=geo)
    p = cad.from_monitor(inc)
    assert (p["status"], p["number"], p["source"], p["source_ref"]) == ("pending", None, "60 Control page", f"monitor:{inc.id}")
    assert (p["type"], p["address"], p["cross_street"]) == ("Fire alarm – commercial", "209 Westchester Ave, Port Chester", "Pearl Street")
    assert (p["lat"], p["lon"]) == (41.0025, -73.6669)
    assert "60 Control page 14:32: " + PAGE_TEXT in p["narrative"] and "location as heard: 209 West Chester Avenue" in p["narrative"]
    # The repeat (same monitor incident) doesn't open a second one.
    inc2 = store.on_page("p2", ["Port Chester FD"], parse(PAGE_TEXT), at=local(2026, 10, 7, 14, 33))
    assert inc2 is inc
    cad.from_monitor(inc2)
    pending = [i for i in cad.store.incidents() if i["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["narrative"].count(PAGE_TEXT) == 2
    with pytest.raises(CadError, match="accept"):
        cad.dispatch(p["id"], {"unit_ids": [1], "operator": "JD"})
    cad.accept(p["id"], {"operator": "JD"})
    assert cad.from_monitor(inc)["status"] == "active"
    # Units named on the page are noted, not assigned.
    _, inc3 = monitor_incident("Port Chester Fire, Engine 61, 2398, respond to a structure fire at 5 King Street")
    inc3.units = ["Engine 61", "2398 Duty Officer"]
    p3 = cad.from_monitor(inc3)
    assert "units named on the page: Engine 61, 2398 Duty Officer" in p3["narrative"] and p3["type"] == "Structure fire"
    assert by_name(cad, "Engine 61")["incident_id"] is None and p3["lat"] is None


def test_rye_brook_only_page_is_not_ours_to_dispatch():
    cad = make_cad()
    _, inc = monitor_incident("Rye Brook Fire, respond to an activated fire alarm, 940 King Street", depts=("Rye Brook FD",))
    assert cad.from_monitor(inc) is None and cad.store.incidents() == []


def test_app_page_opens_pending_cad_incident(tmp_path):
    from tests.test_app import FakeWhisper, cfg, tone, voice, wait
    from sizeup.app import App
    from sizeup.radio.fm import RATE, Channel, Segment
    app = App(replace(cfg(tmp_path), cad_db=str(tmp_path / "state" / "cad.sqlite"), unit_callsigns=CALLSIGNS))
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: PAGE_TEXT})
    app.stt.start()
    audio = np.concatenate([tone(349.1, 1.0), tone(311.1, 3.0), np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    assert wait(lambda: any(i["status"] == "pending" for i in app.cad.store.incidents()))
    p = app.cad.store.incidents()[0]
    assert p["type"] == "Fire alarm – commercial" and p["source_ref"] == f"monitor:{app.store.active().id}"
    assert (tmp_path / "state" / "cad.sqlite").is_file() and isinstance(app.cad.transmitter, NullTransmitter)


# -- HTTP API -----------------------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    from sizeup.web.server import WebServer
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    geocoder = lambda q: [{"label": "209 Westchester Ave, Port Chester", "lat": 41.0025, "lon": -73.6669,
                           "kind": "address", "municipality": "Port Chester", "precision": "exact", "confidence": 0.93}]
    cad = Cad(Settings(callsigns=CALLSIGNS), geocoder=geocoder, log=lambda m: None)
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"

    def call(method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type")

    def j(method, path, body=None):
        code, data, _ = call(method, path, body)
        return code, json.loads(data)
    yield type("S", (), {"call": staticmethod(call), "j": staticmethod(j), "cad": cad, "base": base})
    ws.shutdown()


def test_api_incident_lifecycle(server):
    code, page, ctype = server.call("GET", "/cad")
    assert code == 200 and b"cad.js" in page and ctype.startswith("text/html")
    code, st = server.j("GET", "/api/cad/state")
    assert code == 200 and len(st["units"]) == 16 and st["incidents"] == [] and st["run_cards_default"]
    assert st["transmit"] == {"enabled": False, "transmitter": "null", "channels": ["FG1", "PC repeater"],
                              "note": "Transmit is off: logged and played here only"}
    code, g = server.j("GET", "/api/cad/geocode?q=209+westchester")
    assert code == 200 and g["matches"][0]["label"] == "209 Westchester Ave, Port Chester"
    assert server.j("POST", "/api/cad/incident", {"type": "Volcano"}) == (400, {"error": "type: no run card 'Volcano'"})
    assert server.j("POST", "/api/cad/incident", {"type": "Other", "lat": 41.0})[0] == 400
    assert server.call("POST", "/api/cad/incident", raw=b"{nope")[0] == 400
    code, inc = server.j("POST", "/api/cad/incident", {"type": "Structure fire", "address": "209 Westchester Avenue",
                                                       "cross_street": "Pearl Street", "operator": "JD"})
    assert code == 200 and inc["status"] == "active" and re.fullmatch(r"PCFD-\d{4}-0001", inc["number"])
    assert inc["lat"] == 41.0025 and inc["geo_label"] == "209 Westchester Ave, Port Chester"   # placed: gazetteer sure
    iid = inc["id"]
    assert server.j("POST", f"/api/cad/incident/{iid}/accept", {})[0] == 400                 # not pending
    code, rec = server.j("POST", f"/api/cad/incident/{iid}/recommend", {})
    assert code == 200 and names(rec["units"])[:3] == ["Engine 60", "Engine 61", "Engine 62"]
    ids = [u["id"] for u in rec["units"]]
    code, pv = server.j("POST", f"/api/cad/incident/{iid}/preview", {"unit_ids": ids})
    assert code == 200 and pv["audio_url"].startswith("/api/cad/audio/")
    assert server.call("GET", pv["audio_url"])[1][:4] == b"RIFF"
    assert server.cad.store.incidents()[0]["assignments"] == []                              # preview assigns nothing
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids})[1] == {"error": "operator: required"}
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ["x"], "operator": "JD"})[0] == 400
    assert server.j("POST", f"/api/cad/incident/{iid}/dispatch",
                    {"unit_ids": ids, "operator": "JD", "channel": "Ch 9"})[0] == 400
    code, d = server.j("POST", f"/api/cad/incident/{iid}/dispatch", {"unit_ids": ids, "operator": "JD",
                                                                     "channel": "PC repeater"})
    assert code == 200 and d["transmitted"] is False and d["channel"] == "PC repeater"
    assert d["text"].startswith("Port Chester Fire, Engine 60, Engine 61, Engine 62, Ladder 31, Rescue 40, 2398 Duty Officer, "
                                "respond to 209 Westchester Avenue, cross of Pearl Street, for a structure fire.")
    code, wav, ctype = server.call("GET", d["audio_url"])
    assert code == 200 and ctype == "audio/wav" and wave.open(io.BytesIO(wav)).getframerate() == 16000
    assert server.call("GET", "/api/cad/dispatch/999.wav")[0] == 404
    e60 = ids[0]
    assert server.j("POST", f"/api/cad/unit/{e60}/status", {"status": "napping"})[0] == 400
    code, u = server.j("POST", f"/api/cad/unit/{e60}/status", {"status": "onscene", "operator": "JD"})
    assert code == 200 and u["status"] == "onscene"
    assert server.j("POST", f"/api/cad/incident/{iid}/note", {"text": ""})[0] == 400
    assert server.j("POST", f"/api/cad/incident/{iid}/note", {"text": "Smoke showing", "operator": "JD"})[0] == 200
    assert server.j("POST", f"/api/cad/incident/{iid}", {"cross_street": "Pearl St and Gilbert Pl"})[0] == 200
    assert server.j("POST", f"/api/cad/incident/{iid}/close", {"operator": "JD"})[0] == 200
    code, st = server.j("GET", "/api/cad/state")
    inc = st["incidents"][0]
    kinds = [e["kind"] for e in inc["events"]]
    assert inc["status"] == "closed" and kinds[0] == "created" and kinds[-1] == "closed"
    assert {"dispatch", "status", "note", "edit"} <= set(kinds)
    assert inc["dispatches"][0]["transmitted"] is False and "NOT transmitted" in next(
        e["text"] for e in inc["events"] if e["kind"] == "dispatch")
    assert all(u["incident_id"] is None for u in st["units"])
    code, rc = server.j("POST", "/api/cad/runcards", {"type": "Lockout", "slots": [{"kind": "engine", "count": 1}]})
    assert code == 200 and rc["type"] == "Lockout"
    assert server.j("POST", "/api/cad/runcards/delete", {"type": "Lockout"})[0] == 200
    assert server.call("POST", "/api/cad/nope", {})[0] == 404


def test_api_stream_pushes_cad_state(server):
    with urllib.request.urlopen(server.base + "/api/cad/stream", timeout=5) as r:
        for _ in range(6):                     # the first push can be the empty one from before the first tick
            line = r.readline()
            if line.startswith(b"data: ") and b"department" in line:
                break
    st = json.loads(line[len(b"data: "):])
    assert st["department"] == "Port Chester Fire" and "units" in st


# -- nothing transmits ---------------------------------------------------------------------------------

def test_only_the_null_transmitter_exists_in_m1(tmp_path):
    assert isinstance(make_transmitter(False), NullTransmitter)
    with pytest.raises(ValueError):
        make_transmitter(True)
    with pytest.raises(NotImplementedError):
        AudioPttTransmitter()
    with pytest.raises(ValueError):
        Cad(Settings(transmit=True), log=lambda m: None)
    toml = (ROOT / "config.example.toml").read_text().replace("transmit = false", "transmit = true")
    (tmp_path / "c.toml").write_text(toml)
    with pytest.raises(ConfigError, match="transmit"):
        load(tmp_path / "c.toml")
    # No code that could open a radio interface: serial ports, GPIO/HID, audio output.
    src = "\n".join(p.read_text() for p in (ROOT / "sizeup").rglob("*.py"))
    for mod in ("serial", "sounddevice", "pyaudio", "hid", "gpiozero", "RPi", "simpleaudio", "pygame"):
        assert not re.search(rf"^\s*(import|from)\s+{mod}\b", src, re.M), mod
    assert "afplay" not in src
    # The app wires the null one in.
    cad = make_cad()
    iid = cad.create({"type": "CO alarm"})["id"]
    cad.s.tones = ()
    r = cad.dispatch(iid, {"unit_ids": [by_name(cad, "Engine 60")["id"]], "operator": "JD"})
    assert r["transmitted"] is False and cad.transmitter.sent[-1][1] == r["text"]


def test_config_cad_sections(tmp_path):
    c = load(ROOT / "config.example.toml")
    assert c.cad_db == str((ROOT / "state" / "cad.sqlite").resolve()) and c.dispatch_tones == ((349.1, 1.0), (311.1, 3.0))
    assert c.dispatch_channels == ("FG1", "PC repeater") and c.dispatch_transmit is False
    assert c.apparatus_path.endswith("westchester_apparatus.csv")
    for bad in ('[dispatch]\nvolume = 3\n', '[dispatch]\ntones = [[10, 1]]\n', '[cad]\nnope = 1\n'):
        (tmp_path / "c.toml").write_text(bad)
        with pytest.raises(ConfigError):
            load(tmp_path / "c.toml")
    (tmp_path / "c.toml").write_text("")                       # old configs keep working
    assert load(tmp_path / "c.toml").cad_department == "Port Chester Fire"


def test_cad_page_buttons_have_handlers():
    html = (ROOT / "sizeup" / "web" / "static" / "cad.html").read_text()
    js = (ROOT / "sizeup" / "web" / "static" / "cad.js").read_text()
    for bid, rest in re.findall(r'<button id="([\w-]+)"([^>]*)>', html):
        assert f'$("#{bid}").addEventListener' in js or 'type="submit"' in rest, f"#{bid} has no handler"
    for fid in re.findall(r'<form id="([\w-]+)"', html):
        assert f'$("#{fid}").addEventListener("submit"' in js, f"#{fid} has no submit handler"
    called = set(re.findall(r"^\s+([A-Za-z_]\w*)\(\);?$", re.search(r"^function render\(\) \{(.*?)^\}", js, re.S | re.M).group(1), re.M))
    defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", js, re.M))
    assert called and not (called - defined)
