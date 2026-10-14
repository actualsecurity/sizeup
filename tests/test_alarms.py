"""The emergency alarm (GitHub #128, sizeup/cad/alarms.py): the P25 emergency button from trunk-recorder's call JSON,
and a mayday heard on our fire channels; one alarm per transmission, on every console until a dispatcher
acknowledges it (in the event's chronology), a drill label, and the push through our own ntfy topic."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import alarms as alarms_mod
from sizeup.cad.alarms import KEYWORDS, find
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.config import ConfigError, load
from sizeup.incidents import Traffic
from sizeup.notify import Notifier
from sizeup.radio.p25 import parse_call, split_call
from tests.test_cad import make_cad, uid
from tests.test_cloud import HubRun, TOKEN, base, call as http, hub_cfg
from tests.test_p25 import LABELS, T0, day_dir, tr_call
from tests.test_roles import COREY, JULIAN, VAL, people

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _notify_stays_local(monkeypatch):
    """Pushes only ever reach a local fake ntfy, never a Tapout topic (they page the fire crew)."""
    from urllib.parse import urlsplit

    import sizeup.notify as notify
    real = notify.urlopen

    def local_only(req, *a, **kw):
        assert urlsplit(req.full_url).hostname in ("127.0.0.1", "localhost"), req.full_url
        assert json.loads(req.data)["topic"] not in ("pcfd-phone-prealert", "pcfd-status")
        return real(req, *a, **kw)
    monkeypatch.setattr(notify, "urlopen", local_only)


class FakeNtfy:
    """A local ntfy: keeps every JSON publish."""

    def __init__(self):
        self.got: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"id": f"m{len(outer.got)}", "time": int(time.time())}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def ntfy():
    f = FakeNtfy()
    yield f
    f.close()


def tx(tid="FG1-1", text="", source="FG1", talkgroup=None, rig="", unit="", radio_id="", emergency=False, at=None):
    at = time.time() - 5 if at is None else at
    return {"id": tid, "start": at, "end": at + 3, "source": source, "talkgroup": talkgroup, "unit": unit, "rig": rig,
            "radio_id": radio_id, "text": text, "emergency": emergency}


# -- the P25 emergency flag in trunk-recorder 5.2.0's call JSON ------------------------------------------------


def test_the_calls_emergency_flag_marks_its_first_key_up(tmp_path):
    d = day_dir(tmp_path)
    tr_call(d, "307-1_476-call_1", [(2391001, 3.0, 0.5, "E64 P1"), (600008, 2.0, 0.0, "60 Ctrl OP 8")])
    meta = json.loads((d / "307-1_476-call_1.json").read_text())
    meta["emergency"] = 1                       # the grant that set the call up was an emergency (5.2.0: call level)
    (d / "307-1_476-call_1.json").write_text(json.dumps(meta))
    call = parse_call(d / "307-1_476-call_1.json", LABELS)
    assert call.emergency and not any(t.emergency for t in call.turns)    # 5.2.0 writes srcList emergency 0
    call.audio = np.full(int(5.0 * 16000), 0.2, np.float32)
    pieces = split_call(call)
    assert [(p.talker, p.emergency) for p in pieces] == [("E64 P1", True), ("60 Ctrl OP 8", False)]


def test_a_srclist_entrys_emergency_flag_marks_that_key_up(tmp_path):
    d = day_dir(tmp_path)
    tr_call(d, "307-2_476-call_2", [(600008, 2.0, 0.5, "60 Ctrl OP 8"), (2391001, 3.0, 0.0, "E64 P1")])
    meta = json.loads((d / "307-2_476-call_2.json").read_text())
    meta["srcList"][1]["emergency"] = 1         # a later trunk-recorder that fills it per transmission
    (d / "307-2_476-call_2.json").write_text(json.dumps(meta))
    call = parse_call(d / "307-2_476-call_2.json", LABELS)
    assert not call.emergency and [t.emergency for t in call.turns] == [False, True]
    call.audio = np.full(int(5.0 * 16000), 0.2, np.float32)
    assert [(p.talker, p.emergency) for p in split_call(call)] == [("60 Ctrl OP 8", False), ("E64 P1", True)]


def test_no_flag_no_emergency(tmp_path):
    d = day_dir(tmp_path)
    tr_call(d, "307-3_476-call_3", [(2391001, 2.0, 0.0, "E64 P1")])
    call = parse_call(d / "307-3_476-call_3.json", LABELS)
    call.audio = np.full(32000, 0.2, np.float32)
    assert not call.emergency and not any(p.emergency for p in split_call(call))


def test_the_unit_script_gets_no_emergency_flag_in_5_2_0():
    """trunk-recorder 5.2.0's unit_script plugin passes "<sys> <radio> call <tg> <patches>" only: the button is
    known when the call JSON is written, not at the grant (the alarm comes at the end of the call)."""
    script = (ROOT / "deploy" / "trunk-recorder" / "unit-event.sh").read_text()
    assert 'echo "SIZEUP-UNIT $1 $2 call $4"' in script


# -- the words ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, heard", [
    ("Mayday mayday mayday, Engine 64 P1, I'm trapped", ["mayday"]),
    ("MAYDAY-MAYDAY, Ladder 31", ["mayday"]),
    ("may day, may day, ladder 31 roof", ["may day may day"]),
    ("Command, emergency traffic, all units evacuate", ["emergency traffic"]),
    ("Firefighter down, second floor rear", ["firefighter down"]),
    ("we have a fire fighter down", ["fire fighter down"]),
    ("Member down on the first floor", ["member down"]),
    ("Lost firefighter, division 2", ["lost firefighter"]),
    ("PAR on Engine 64, one member missing", ["par ... missing"]),
    ("P.A.R. Ladder 31: two unaccounted for", ["par ... unaccounted"]),
])
def test_the_alarm_words(text, heard):
    got, spans = find(text, KEYWORDS)
    assert got == heard
    assert spans and all(text[a:b].strip() for a, b in spans)


@pytest.mark.parametrize("text", [
    "We'll set up the parade on May Day.",            # "may day" once: a date
    "Emergency on the third floor, Engine 64 responding",     # "emergency" alone
    "Mayday's parade is cancelled",                   # not the word itself
    "maydays",
    "PAR complete, nobody missing",                   # a "no" between: not an alarm
    "PAR on all units, none missing",
    "the paramedic is missing the address",           # "par" inside a longer word
    "emergency, traffic on 287 is heavy",             # the two words, but... see below
])
def test_not_the_alarm_words(text):
    got, _ = find(text, KEYWORDS)
    if text.startswith("emergency, traffic"):
        assert got == ["emergency traffic"]           # punctuation doesn't split a phrase: accepted, said here
    else:
        assert got == [], text


def test_the_words_are_marked_where_they_are():
    text = "Engine 64 to command, mayday mayday mayday"
    _, spans = find(text, KEYWORDS)
    assert [text[a:b] for a, b in spans] == ["mayday mayday mayday"]


def test_phrases_are_checked():
    assert alarms_mod.clean("keywords", ["Mayday", "PAR … missing", "mayday"]) == ["mayday", "par ... missing"]
    for bad in (["..."], ["... missing"], ["par ... ... missing"], [""], ["x " * 20], "mayday"):
        with pytest.raises(Exception):
            alarms_mod.clean("keywords", bad)
    assert alarms_mod.clean("channels", ["FG1", "307", "tg:333"]) == ["FG1", "tg:307", "tg:333"]


# -- raising, one per transmission, notices, age -----------------------------------------------------------------


def test_a_mayday_on_fg1_is_one_alarm_per_transmission(tmp_path):
    cad = make_cad(tmp_path)
    a = cad.alarms.on_traffic(tx(text="Engine 64 mayday mayday mayday", unit="", rig="E64", radio_id="6401"))
    assert a and a["level"] == "alarm" and a["keywords"] == ["mayday"] and a["named"] == ["E64"]
    again = cad.alarms.on_traffic(tx(text="Engine 64 mayday mayday mayday, emergency traffic", rig="E64", radio_id="6401"))
    assert again["id"] == a["id"] and again["keywords"] == ["mayday", "emergency traffic"]
    st = cad.state()["alarms"]
    assert [x["id"] for x in st["open"]] == [a["id"]] and st["open"][0]["what"] == "mayday, emergency traffic"
    assert st["open"][0]["talker"]["units"] == [["E64", "Engine 64"]] or st["open"][0]["talker"]["units"] == [("E64", "Engine 64")]
    assert len(cad.store.q("SELECT * FROM alarms")) == 1


def test_words_only_on_our_fire_channels(tmp_path):
    cad = make_cad(tmp_path)
    assert cad.alarms.on_traffic(tx("tg-1", "mayday mayday", source="WCPD Ch 1", talkgroup=383)) is None
    assert cad.alarms.on_traffic(tx("tg-2", "mayday mayday", source="Fire/EMS 15", talkgroup=307))["level"] == "alarm"
    assert cad.alarms.on_traffic(tx("tg-3", "mayday mayday", source="Airport Incident", talkgroup=333))["level"] == "alarm"
    assert cad.alarms.on_traffic(tx("fg-4", "all quiet, nothing showing")) is None


def test_the_emergency_button_ours_or_on_our_channel_alarms_elsewhere_a_notice(tmp_path):
    cad = make_cad(tmp_path)
    ours = cad.alarms.on_traffic(tx("p-1", source="WCPD Ch 1", talkgroup=383, rig="E64", unit="E64 P1",
                                    radio_id="2391001", emergency=True))
    assert ours["level"] == "alarm" and ours["what"] == "emergency button"
    chan = cad.alarms.on_traffic(tx("p-2", source="Fire/EMS 15", talkgroup=307, radio_id="7077003", unit="77M3",
                                    emergency=True))
    assert chan["level"] == "alarm"
    pd = cad.alarms.on_traffic(tx("p-3", source="WCPD Ch 1", talkgroup=383, radio_id="5550001", emergency=True))
    assert pd["level"] == "notice"
    st = cad.state()["alarms"]
    assert [x["id"] for x in st["notices"]] == [pd["id"]] and pd["id"] not in [x["id"] for x in st["open"]]
    # configurable: "alarm" makes it the full alarm, "off" nothing
    cad.settings_post("alarms", {"action": "set", "key": "other", "value": "off", "operator": "Corey"})
    assert cad.alarms.on_traffic(tx("p-4", source="WCPD Ch 1", talkgroup=383, radio_id="5550002", emergency=True)) is None
    cad.settings_post("alarms", {"action": "set", "key": "other", "value": "alarm", "operator": "Corey"})
    assert cad.alarms.on_traffic(tx("p-5", source="WCPD Ch 1", talkgroup=383, radio_id="5550003",
                                    emergency=True))["level"] == "alarm"


def test_an_old_transmission_raises_nothing(tmp_path):
    cad = make_cad(tmp_path)
    assert cad.alarms.on_traffic(tx(text="mayday mayday", at=time.time() - 3 * 3600)) is None


def test_the_fire_channels_and_words_are_settings(tmp_path):
    cad = make_cad(tmp_path)
    cad.settings_post("alarms", {"action": "set", "key": "keywords", "value": ["code red"], "operator": "Corey"})
    assert cad.alarms.on_traffic(tx("k-1", "mayday mayday")) is None
    assert cad.alarms.on_traffic(tx("k-2", "Code Red on the roof"))["keywords"] == ["code red"]
    cad.settings_post("alarms", {"action": "set", "key": "channels", "value": ["FG2"], "operator": "Corey"})
    assert cad.alarms.on_traffic(tx("k-3", "code red", source="FG1")) is None
    assert cad.alarms.on_traffic(tx("k-4", "code red", source="FG2"))
    audit = cad.settings.audit_log("system")
    assert {r["key"] for r in audit} >= {"alarms/keywords", "alarms/channels"}
    cad.settings_post("alarms", {"action": "reset", "key": "keywords", "operator": "Corey"})
    assert cad.alarms.config()["keywords"] == list(KEYWORDS)
    # the settings export imports again
    exp = cad.settings_export()
    assert exp["overrides"]["alarms"]["channels"] == ["FG2"]
    assert cad.settings_post("import", {"action": "dry_run", "settings": exp, "operator": "Corey"})


# -- the event, acknowledge, roles, drill -------------------------------------------------------------------------


def test_linked_to_the_open_event_of_the_rig_and_acknowledged_in_its_chronology(tmp_path):
    cad = make_cad(tmp_path)
    inc = cad.create({"type": "STRU", "address": "100 S Ridge St", "operator": "Lt. Ruiz"})
    cad.dispatch(inc["id"], {"unit_ids": [uid(cad, "E64"), uid(cad, "L31")], "operator": "Lt. Ruiz"})
    t = tx("FG1-9", "Engine 64 mayday mayday mayday, Ladder 31 we're trapped", rig="E64", unit="E64 P1", radio_id="6401", at=time.time() + 0.01)
    cad.record_traffic(t)
    a = cad.alarms.on_traffic(t)
    num = cad.store.incident(inc["id"])["number"]
    assert a["incident_id"] == inc["id"] and a["incident_number"] == num and a["named"] == ["E64", "L31"]
    lines = [e["text"] for e in cad.full(inc["id"])["events"] if e["kind"] == "alarm"]
    assert lines == [f"EMERGENCY: E64 P1 mayday on FG1 at {alarms_mod._hms(t['start'])}"]
    got = cad.alarm_ack(a["id"], {"operator": "Corey <corey@example.org>"})
    assert got["ack_by"] == "Corey <corey@example.org>" and got["ack_at"]
    lines = [e["text"] for e in cad.full(inc["id"])["events"] if e["kind"] == "alarm"]
    assert lines[-1] == (f"EMERGENCY: E64 P1 mayday on FG1 at {alarms_mod._hms(t['start'])}; acknowledged by Corey "
                         f"at {alarms_mod._hms(got['ack_at'])}")
    st = cad.state()["alarms"]
    assert not st["open"] and [x["id"] for x in st["recent"]] == [a["id"]]
    with pytest.raises(Exception, match="already acknowledged"):
        cad.alarm_ack(a["id"], {"operator": "Julian"})
    with pytest.raises(Exception, match="operator"):
        cad.alarm_ack(cad.alarms.on_traffic(tx("FG1-10", "mayday mayday"))["id"], {"operator": ""})


def test_the_button_before_the_transcript_links_by_rig_then_takes_the_words(tmp_path):
    cad = make_cad(tmp_path)
    inc = cad.create({"type": "STRU", "address": "100 S Ridge St", "operator": "Lt. Ruiz"})
    cad.dispatch(inc["id"], {"unit_ids": [uid(cad, "E64")], "operator": "Lt. Ruiz"})
    t = tx("307-5-t0", "", source="Fire/EMS 15", talkgroup=307, rig="E64", unit="E64 P1", radio_id="2391001",
           emergency=True, at=time.time() + 0.01)
    a = cad.alarms.on_traffic(t)
    assert a["what"] == "emergency button" and a["incident_id"] == inc["id"] and a["text"] == ""
    b = cad.alarms.on_traffic(dict(t, text="E64 mayday, mayday, mayday"))
    assert b["id"] == a["id"] and b["what"] == "emergency button + mayday" and b["text"].startswith("E64 mayday")
    assert len([e for e in cad.full(inc["id"])["events"] if e["kind"] == "alarm"]) == 1


def test_acknowledge_is_for_dispatchers_and_admins(tmp_path):
    api = CadApi(make_cad(tmp_path))
    people(api.cad)
    assert needs("/api/cad/alarm/1/ack") == "dispatch" and needs("/api/cad/settings/alarms") == "admin"
    a = api.cad.alarms.on_traffic(tx(text="mayday mayday"))

    def post(path, body, email):
        code, raw, _ = api.post(path, json.dumps(body).encode(), Identity(email=email))
        return code, json.loads(raw)
    code, out = post(f"/api/cad/alarm/{a['id']}/ack", {}, VAL)                # a viewer sees it, can't acknowledge
    assert code == 403 and api.cad.alarms.get(a["id"])["ack_at"] is None
    code, out = post("/api/cad/settings/alarms", {"action": "set", "key": "drill", "value": True}, JULIAN)
    assert code == 403                                                         # drill mode: admins
    code, out = post(f"/api/cad/alarm/{a['id']}/ack", {"operator": "typed name"}, JULIAN)
    assert code == 200 and out["ack_by"].startswith("Julian") and "julian@example.org" in out["ack_by"]
    acts = api.cad.activity.rows(JULIAN, "action", None, 10)
    assert acts and acts[0]["summary"].startswith("Acknowledged the emergency alarm")
    code, out = post("/api/cad/settings/alarms", {"action": "set", "key": "drill", "value": True}, COREY)
    assert code == 200 and out["alarms"]["drill"] is True
    code, raw, _ = api.get("/api/cad/alarms", "", Identity(email=VAL))
    assert code == 200 and json.loads(raw)["drill"] is True


def test_drill_mode_labels_the_alarm_and_its_chronology(tmp_path):
    cad = make_cad(tmp_path)
    cad.settings_post("alarms", {"action": "set", "key": "drill", "value": True, "operator": "Corey"})
    a = cad.alarms.on_traffic(tx(text="mayday mayday", rig="E64", radio_id="6401"))
    assert a["drill"] and alarms_mod.line(a).startswith("DRILL — EMERGENCY: E64 mayday on FG1")
    assert cad.state()["alarms"]["drill"] is True
    cad.settings_post("alarms", {"action": "set", "key": "drill", "value": False, "operator": "Corey"})
    assert not cad.alarms.on_traffic(tx("FG1-2", text="mayday mayday"))["drill"]


# -- the push --------------------------------------------------------------------------------------------------


def test_each_alarm_and_its_acknowledgement_is_pushed_with_who_and_where(tmp_path, ntfy):
    cad = make_cad(tmp_path)
    cad.alarms.notifier = Notifier(ntfy.url, "sizeup-test-alarms-0123", lambda m: None, sleep=lambda s: None)
    inc = cad.create({"type": "STRU", "address": "100 S Ridge St", "operator": "Lt. Ruiz"})
    cad.dispatch(inc["id"], {"unit_ids": [uid(cad, "E64")], "operator": "Lt. Ruiz"})
    num = cad.store.incident(inc["id"])["number"]
    t = tx("FG1-7", "mayday mayday mayday, Engine 64, Ladder 31", rig="E64", unit="E64 P1", radio_id="6401", at=time.time() + 0.01)
    cad.record_traffic(t)
    a = cad.alarms.on_traffic(t)
    cad.alarms.on_traffic(dict(t, text=t["text"] + " roof"))       # the same transmission again: no second push
    assert cad.alarms.notifier.flush(5)
    assert len(ntfy.got) == 1
    m = ntfy.got[0]
    assert m["topic"] == "sizeup-test-alarms-0123" and m["priority"] == 5
    assert m["title"].startswith("MAYDAY") and "E64 P1 (Engine 64)" in m["title"] and "on FG1" in m["title"]
    assert num in m["title"] and "100 S Ridge St" in m["title"]
    assert "heard: “mayday mayday mayday" in m["message"] and "units named: E64, L31" in m["message"]
    cad.alarm_ack(a["id"], {"operator": "Corey"})
    assert cad.alarms.notifier.flush(5) and len(ntfy.got) == 2
    assert ntfy.got[1]["title"].startswith("Acknowledged: MAYDAY") and "acknowledged by Corey" in ntfy.got[1]["message"]
    # a notice is not pushed
    cad.alarms.on_traffic(tx("p-9", source="WCPD Ch 1", talkgroup=383, radio_id="5550001", emergency=True))
    assert cad.alarms.notifier.flush(5) and len(ntfy.got) == 2


def test_the_push_topic_is_config_and_never_tapouts(tmp_path, monkeypatch):
    for k in ("SIZEUP_ALERT_TOPIC", "SIZEUP_ALERT_SERVER"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[alerts]\nntfy_topic = "sizeup-test-alarms-0123"\n')
    assert load(cfg).alerts_ntfy_topic == "sizeup-test-alarms-0123"
    cfg.write_text("")
    assert load(cfg).alerts_ntfy_topic == ""                       # empty by default: no pushes
    for topic in ("pcfd-phone-prealert", "pcfd-status"):
        cfg.write_text(f'[alerts]\nntfy_topic = "{topic}"\n')
        with pytest.raises(ConfigError, match="Tapout"):
            load(cfg)


# -- all-in-one and the hub ----------------------------------------------------------------------------------


def test_all_in_one_the_button_alarms_before_speech_to_text_and_the_words_join_it(tmp_path):
    from sizeup.app import App
    from sizeup.radio.p25 import Call, Turn
    app = App(base(clips_dir=str(tmp_path / "clips"), cad_db=str(tmp_path / "cad.sqlite")))
    texts = []
    app._stt = lambda item, audio, eng, key: texts.append(item)       # speech-to-text held back: the alarm is up first
    now = time.time() - 4
    call = Call("307-1", 307, "Fire/EMS 15", now, now + 3, ["E64 P1"], np.full(48000, 0.2, np.float32), "",
                [Turn(0.0, "E64 P1", "2391001", "E64 P1", 3.0, now)], {"E64 P1": ("2391001", "E64 P1")}, "westchester",
                emergency=True)
    app._call(call)
    st = app.cad.state()["alarms"]
    assert [(a["traffic_id"], a["what"], a["text"]) for a in st["open"]] == [("307-1", "emergency button", "")]
    t = texts[0]
    assert getattr(t, "emergency", False)
    t.raw, t.status, t.confidence = "mayday mayday mayday, E64", "done", 0.9
    app._transcribed(t)
    st = app.cad.state()["alarms"]
    assert len(st["open"]) == 1 and st["open"][0]["what"] == "emergency button + mayday"
    # a talkgroup switched off (Record off) still keeps an emergency call; an ordinary one is dropped
    app.tg.set(383, record=False)
    plain = Call("383-1", 383, "WCPD Ch 1", now, now + 3, ["5550001"], np.full(48000, 0.2, np.float32), "",
                 [Turn(0.0, "5550001", "5550001", "", 3.0, now)], {}, "westchester")
    app._call(plain)
    assert not any(x.id == "383-1" for x in texts)
    app._call(Call("383-2", 383, "WCPD Ch 1", now, now + 3, ["5550001"], np.full(48000, 0.2, np.float32), "",
                   [Turn(0.0, "5550001", "5550001", "", 3.0, now)], {}, "westchester", emergency=True))
    assert any(x.id == "383-2" for x in texts)
    assert [a["traffic_id"] for a in app.cad.state()["alarms"]["notices"]] == ["383-2"]


def test_the_hub_raises_it_from_the_stations_alarm_event_then_the_transcript(tmp_path):
    import base64
    from sizeup.cad.dispatcher import wav_bytes
    hub = HubRun(hub_cfg(tmp_path))
    try:
        at = time.time() - 4
        d = {"id": "307-77-t0", "source": "Fire/EMS 15", "start": at, "end": at + 3, "text": "", "status": "pending",
             "unit": "E64 P1", "talkgroup": 307, "rig": "", "radio_id": "2391001", "emergency": True,
             "pairs": [["2391001", "E64 P1"]]}
        wav = base64.b64encode(wav_bytes(np.zeros(8000, np.float32), 8000)).decode()
        code, out = http(hub.url + "/api/station/events", "POST",
                         {"epoch": "e1", "events": [{"seq": 1, "at": at, "type": "alarm", "data": d, "audio": wav}]})
        assert code == 200 and out["applied"] == 1
        st = hub.get("/api/cad/state")["alarms"]
        assert [(a["traffic_id"], a["what"], a["has_clip"]) for a in st["open"]] == [("307-77-t0", "emergency button", True)]
        tr = dict(d, text="mayday mayday mayday", status="done", raw="mayday mayday mayday", model="m.bin", attach=True)
        code, out = http(hub.url + "/api/station/events", "POST",
                         {"epoch": "e1", "events": [{"seq": 2, "at": at, "type": "traffic", "data": tr, "audio": wav}]})
        assert code == 200
        st = hub.get("/api/cad/state")["alarms"]
        assert len(st["open"]) == 1 and st["open"][0]["what"] == "emergency button + mayday"
        assert st["open"][0]["spans"] and st["open"][0]["text"] == "mayday mayday mayday"
    finally:
        hub.close()
