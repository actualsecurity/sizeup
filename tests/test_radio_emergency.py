"""The emergency in the Radio log (#128, user 2026-10-14: "where is the bus emergency here? it should be easy to see here
in the logs but its not"): every transmission row carries the emergency button and its alarm (comms.with_alarms; rows
logged before the flag was kept light up from their alarm), radio_traffic.emergency is written by the monitor and the
hub alike, the Radio view's Emergency filter, the folded talkgroup that can't hide it; trunk-recorder's own record of an
emergency call is kept for audit. And the emergency window (alarms.Windows, user: "if emergencies bypass that record
switch, it should record for 10 minutes or so after the incident too or until an indication is made that things are
'ok' even if the record switch is off"): the button's talkgroup and radio kept with Record off until All clear."""

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from sizeup import janitor
from sizeup.cad.alarms import Windows
from sizeup.cad.api import needs
from sizeup.cloud import station as station_mod
from sizeup.radio.p25 import Call, Turn, parse_call
from tests.test_alarms import tx
from tests.test_cad import make_cad
from tests.test_cloud import HubRun, base, call as http, hub_cfg
from tests.test_p25 import LABELS, day_dir, tr_call

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or (shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin"))


@pytest.fixture(autouse=True)
def _notify_stays_local(monkeypatch):
    """No push leaves this machine (and never to Tapout's topics)."""
    import sizeup.notify as notify

    def refuse(req, *a, **kw):
        raise AssertionError(f"no pushes in these tests: {req.full_url}")
    monkeypatch.setattr(notify, "urlopen", refuse)


def row(cad, **kw) -> dict:
    """One transmission as the monitor logs it (a P25 one on DOT Dispatch 1 by default: the bus, 2026-10-14)."""
    at = kw.pop("at", time.time() - 30)
    t = {"id": "1325-1792007984.625_476075000.0-call_130", "start": at, "end": at + 15, "source": "DOT Dispatch 1",
         "talkgroup": 1325, "unit": "BUS514", "rig": "", "radio_id": "21514", "text": "", "status": "no_speech", **kw}
    cad.record_traffic(t)
    return t


def rows_of(cad, **q) -> dict[str, dict]:
    out = cad.comms_query({k: [v] for k, v in q.items()})
    return {r["id"]: r for r in out["rows"]}


# -- the row carries its emergency and its alarm --------------------------------------------------------------------


def test_a_row_logged_without_the_flag_lights_up_from_its_alarm(tmp_path):
    """The bus (BUS514, radio 21514, TG 1325) raised the notice, but its row said only "Key-up, no speech": the row
    as the old station logged it (no emergency) shows the alarm, its level and who acknowledged it."""
    cad = make_cad(tmp_path)
    t = row(cad)                                           # logged as before: no "emergency" in it
    plain = row(cad, id="1325-2-call_131", radio_id="21000", unit="BUS100")
    a = cad.alarms.on_traffic(dict(tx(t["id"], source="DOT Dispatch 1", talkgroup=1325, unit="BUS514", radio_id="21514",
                                      emergency=True), start=t["start"], end=t["end"]))
    assert a["level"] == "notice"                          # not our talkgroup or radio: "Emergency button (not ours)"
    got = rows_of(cad)
    r = got[t["id"]]
    assert (r["emergency"], r["alarm_id"], r["alarm_level"], r["keywords"], r["ack_by"], r["ack_at"]) == \
        (1, a["id"], "notice", [], "", None)
    p = got[plain["id"]]
    assert (p["emergency"], p["alarm_id"], p["alarm_level"], p["keywords"]) == (0, None, "", [])
    cad.alarm_ack(a["id"], {"operator": "Corey"})
    r = rows_of(cad)[t["id"]]
    assert r["ack_by"] == "Corey" and r["ack_at"] and r["clear_at"] is None
    # a mayday heard (words, no button) is on its row too, ours: the full alarm
    m = row(cad, id="FG1-9", source="FG1", talkgroup=None, unit="E64 P1", radio_id="6401", rig="E64",
            text="mayday mayday mayday", status="done")
    ma = cad.alarms.on_traffic(tx("FG1-9", text="mayday mayday mayday", rig="E64", radio_id="6401", at=m["start"]))
    r = rows_of(cad)["FG1-9"]
    assert (r["emergency"], r["alarm_id"], r["alarm_level"], r["keywords"]) == (0, ma["id"], "alarm", ["mayday"])


def test_the_alarm_is_on_the_events_radio_card_and_the_feed_too(tmp_path):
    cad = make_cad(tmp_path)
    t = row(cad, id="307-5-call_9", source="Fire/EMS 15", talkgroup=307, unit="E64 P1", radio_id="2391001")
    a = cad.alarms.on_traffic(dict(tx(t["id"], source="Fire/EMS 15", talkgroup=307, radio_id="2391001", emergency=True),
                                   start=t["start"], end=t["end"]))
    feed = {r["id"]: r for r in cad.feed.rows()}
    assert feed[t["id"]]["alarm_id"] == a["id"] and feed[t["id"]]["alarm_level"] == "alarm"


def test_the_monitor_writes_the_flag_and_it_stays(tmp_path):
    cad = make_cad(tmp_path)
    ev = {"talkgroup": 1325, "emergency": 1, "srcList": [{"src": 21514, "emergency": 0, "pos": 0.0}], "file": "x.json"}
    t = row(cad, emergency=True, evidence=ev, kept="emergency")
    r = cad.store.one("SELECT emergency, evidence, kept FROM radio_traffic WHERE id = ?", (t["id"],))
    assert r["emergency"] == 1 and json.loads(r["evidence"]) == ev and r["kept"] == "emergency"
    row(cad, text="BUS514 go ahead", status="done")       # its transcript again, without the flag: still an emergency
    got = rows_of(cad)[t["id"]]
    assert (got["emergency"], got["evidence"], got["kept"], got["text"]) == (1, ev, "emergency", "BUS514 go ahead")
    assert got["alarm_id"] is None                         # (no alarm of its own: the flag alone shows)


def test_an_oversized_record_keeps_its_flags_not_its_lists(tmp_path):
    from sizeup.cad.comms import EVIDENCE_MAX, evidence_json
    ev = {"emergency": 1, "talkgroup": 1325, "srcList": [{"src": i, "tag": "x" * 200} for i in range(1000)]}
    s = evidence_json(ev)
    assert len(s) <= EVIDENCE_MAX and json.loads(s) == {"emergency": 1, "talkgroup": 1325, "trimmed": True}
    assert evidence_json(None) == "" and evidence_json({}) == ""


def test_the_column_is_added_to_an_old_database_and_filled_from_its_alarms(tmp_path):
    cad = make_cad(tmp_path)
    t = row(cad)
    other = row(cad, id="1325-2-call_131", radio_id="21000")
    cad.alarms.on_traffic(dict(tx(t["id"], source="DOT Dispatch 1", talkgroup=1325, radio_id="21514", emergency=True),
                               start=t["start"], end=t["end"]))
    cad.shutdown()
    db = sqlite3.connect(tmp_path / "cad.sqlite")           # the database as the last release left it
    for col in ("emergency", "evidence", "kept"):
        db.execute(f"ALTER TABLE radio_traffic DROP COLUMN {col}")
    db.execute("ALTER TABLE alarms DROP COLUMN clear_by")
    db.execute("ALTER TABLE alarms DROP COLUMN clear_at")
    db.commit()
    db.close()
    cad = make_cad(tmp_path)
    got = {r["id"]: r["emergency"] for r in cad.store.q("SELECT id, emergency FROM radio_traffic")}
    assert got == {t["id"]: 1, other["id"]: 0}
    cols = {r["name"] for r in cad.store.q("PRAGMA table_info(alarms)")}
    assert {"clear_by", "clear_at"} <= cols


def test_the_emergency_filter(tmp_path):
    cad = make_cad(tmp_path)
    t = row(cad, emergency=True)                            # the flag on the row
    row(cad, id="1325-2-call_131", radio_id="21000")        # ordinary traffic
    old = row(cad, id="1325-3-call_132", radio_id="21515")  # the flag only in its alarm (logged before it was kept)
    cad.alarms.on_traffic(dict(tx(old["id"], source="DOT Dispatch 1", talkgroup=1325, radio_id="21515", emergency=True),
                               start=old["start"], end=old["end"]))
    m = row(cad, id="FG1-9", source="FG1", talkgroup=None, unit="E64 P1", radio_id="6401", rig="E64",
            text="mayday mayday mayday", status="done")
    cad.alarms.on_traffic(tx("FG1-9", text="mayday mayday mayday", rig="E64", radio_id="6401", at=m["start"]))
    assert len(rows_of(cad)) == 4
    assert set(rows_of(cad, emergency="1")) == {t["id"], old["id"], "FG1-9"}
    assert set(rows_of(cad, emergency="1", channel="FG1")) == {"FG1-9"}
    assert all(r["kind"] == "radio" for r in rows_of(cad, emergency="1").values())     # no notes


# -- the hub carries it -------------------------------------------------------------------------------------------


def test_the_hub_logs_the_stations_flag_and_record(tmp_path):
    import base64
    from sizeup.cad.dispatcher import wav_bytes
    hub = HubRun(hub_cfg(tmp_path))
    try:
        at = time.time() - 20
        ev = {"talkgroup": 1325, "emergency": 1, "srcList": [{"src": 21514, "emergency": 0}], "file": "c.json"}
        d = {"id": "1325-1792007984.625_476075000.0-call_130", "source": "DOT Dispatch 1", "start": at, "end": at + 15,
             "text": "", "status": "no_speech", "unit": "BUS514", "talkgroup": 1325, "rig": "", "radio_id": "21514",
             "emergency": True, "evidence": ev, "pairs": [["21514", "BUS514"]], "attach": False, "raw": "", "model": ""}
        wav = base64.b64encode(wav_bytes(np.zeros(8000, np.float32), 8000)).decode()
        code, out = http(hub.url + "/api/station/events", "POST",
                         {"epoch": "e1", "events": [{"seq": 1, "at": at, "type": "alarm", "data": d, "audio": wav},
                                                    {"seq": 2, "at": at, "type": "traffic", "data": d, "audio": wav}]})
        assert code == 200 and out["applied"] == 2
        r = rows_of(hub.app.cad)[d["id"]]
        assert (r["emergency"], r["alarm_level"], r["evidence"]) == (1, "notice", ev)
        # an "alarm" event that says it isn't one raises nothing (one without the field still does)
        d2 = dict(d, id="1325-2-call_131", emergency=False, radio_id="21000")
        code, _ = http(hub.url + "/api/station/events", "POST",
                       {"epoch": "e1", "events": [{"seq": 3, "at": at, "type": "alarm", "data": d2}]})
        assert code == 200 and not hub.app.cad.store.q("SELECT 1 FROM alarms WHERE traffic_id = '1325-2-call_131'")
        d3 = {k: v for k, v in d.items() if k != "emergency"} | {"id": "1325-3-call_132", "radio_id": "21516"}
        code, _ = http(hub.url + "/api/station/events", "POST",
                       {"epoch": "e1", "events": [{"seq": 4, "at": at, "type": "alarm", "data": d3}]})
        assert code == 200 and hub.app.cad.store.q("SELECT 1 FROM alarms WHERE traffic_id = '1325-3-call_132'")
    finally:
        hub.close()


# -- trunk-recorder's record of an emergency call is kept ---------------------------------------------------------


def emergency_call(tmp_path, name="1325-1792007984.625_476075000.0-call_130", tg=1325, flag=True):
    d = day_dir(tmp_path / "p25")
    tr_call(d, name, [(21514, 3.0, 0.0, "BUS514")], tg=tg)
    meta = json.loads((d / f"{name}.json").read_text())
    meta["emergency"] = int(flag)
    (d / f"{name}.json").write_text(json.dumps(meta))
    call = parse_call(d / f"{name}.json", {**LABELS, 1325: "DOT Dispatch 1", 383: "WCPD Ch 1"})
    call.audio = np.full(int(3.0 * 16000), 0.2, np.float32)
    return d, call


def make(tmp_path):
    from sizeup.app import App
    app = App(base(clips_dir=str(tmp_path / "captures" / "clips"), cad_db=str(tmp_path / "cad.sqlite")))
    texts = []
    app._stt = lambda item, audio, eng, key: texts.append(item)        # speech-to-text held back
    return app, texts


def test_an_emergency_calls_record_is_kept_and_goes_with_it(tmp_path):
    app, texts = make(tmp_path)
    d, call = emergency_call(tmp_path)
    app._call(call)
    kept = tmp_path / "captures" / janitor.EMERGENCY_DIR / f"{call.id}.json"
    assert kept.is_file() and json.loads(kept.read_text())["emergency"] == 1
    assert not (d / f"{call.id}.json").exists() and not (d / f"{call.id}.wav").exists()
    t = texts[0]
    assert t.emergency and t.evidence["emergency"] == 1 and t.evidence["srcList"][0]["src"] == 21514
    assert t.evidence["file"] == f"{call.id}.json"
    t.raw, t.status, t.confidence = "", "no_speech", None
    app._transcribed(t)
    r = rows_of(app.cad)[t.id]
    assert r["emergency"] == 1 and r["evidence"]["srcList"][0]["src"] == 21514
    # an ordinary call's files go, as before
    d2, plain = emergency_call(tmp_path / "b", name="307-1-call_1", tg=307, flag=False)
    app._call(plain)
    assert not (d2 / "307-1-call_1.json").exists() and not (tmp_path / "captures" / "emergency" / "307-1-call_1.json").exists()


def test_the_janitor_keeps_emergency_records_90_days(tmp_path):
    cap = tmp_path / "captures"
    em = cap / janitor.EMERGENCY_DIR
    em.mkdir(parents=True)
    new, old = em / "a.json", em / "b.json"
    new.write_text("{}")
    old.write_text("{}")
    import os
    now = time.time()
    os.utime(new, (now - 30 * 86400, now - 30 * 86400))
    os.utime(old, (now - 91 * 86400, now - 91 * 86400))
    janitor.clean(cap, cap / "clips", cap, 12 * 3600, 24 * 3600, now)   # (a P25 dir that holds it: still kept)
    assert new.exists() and not old.exists()


# -- the emergency window -----------------------------------------------------------------------------------------


class Clock:
    def __init__(self, t=1_792_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_the_window_opens_extends_follows_the_radio_and_ends():
    c = Clock()
    w = Windows(600, c)
    assert w.keeps(1325, ["21000"], c.t) == ""
    w.open(1325, "DOT Dispatch 1", ["21514"], c.t)
    assert w.keeps(1325, ["21000"], c.t + 60) == "talkgroup"          # anyone on its talkgroup
    assert w.keeps(383, ["21514"], c.t + 60) == "radio"               # the radio that pressed it, anywhere
    assert w.keeps(383, ["21000"], c.t + 60) == ""                    # anyone else elsewhere: no
    assert w.keeps(1325, [], c.t + 601) == ""                         # 10 minutes after: over
    w.open(1325, "DOT Dispatch 1", ["21514"], c.t + 300)              # the button again: extended
    assert w.keeps(1325, [], c.t + 899) == "talkgroup" and w.keeps(1325, [], c.t + 901) == ""
    assert [x["until"] for x in w.state(c.t + 10)] == [c.t + 900]
    assert w.state(c.t + 901) == []                                   # expired: gone from the state
    w.open(1325, "DOT Dispatch 1", ["21514"], c.t + 1000)
    assert w.clear(1325) is True                                      # All clear: talkgroup and its radio
    assert w.keeps(1325, [], c.t + 1001) == "" and w.keeps(383, ["21514"], c.t + 1001) == ""
    assert Windows(0, c).open(1325, "", ["1"], c.t) is None           # emergency_window_min = 0: no window


def test_record_off_keeps_the_talkgroup_and_the_radio_until_all_clear(tmp_path):
    """TG 1325 had Record off: the bus's call was kept (the button) but dispatch's answers were dropped."""
    app, texts = make(tmp_path)
    app.tg.set(1325, record=False)
    app.tg.set(383, record=False)
    now = time.time() - 5
    t0 = Turn(0.0, "BUS514", "21514", "BUS514", 3.0, now)

    def c(cid, tg, src, at, em=False):
        return Call(cid, tg, "DOT Dispatch 1" if tg == 1325 else "WCPD Ch 1", at, at + 3, [src],
                    np.full(48000, 0.2, np.float32), "", [Turn(0.0, src, src, "", 3.0, at)], {}, "westchester", emergency=em)
    # an ordinary call on the switched-off talkgroup: dropped, as before
    app._call(c("1325-0", 1325, "21000", now - 60))
    assert texts == []
    app._call(Call("1325-1", 1325, "DOT Dispatch 1", now, now + 3, ["BUS514"], np.full(48000, 0.2, np.float32), "",
                   [t0], {"BUS514": ("21514", "BUS514")}, "westchester", emergency=True))
    assert [x.id for x in texts] == ["1325-1"] and texts[0].kept == "emergency"
    app._call(c("1325-2", 1325, "1325001", now + 20))                # dispatch answering: kept
    app._call(c("383-1", 383, "21514", now + 30))                    # the bus on another talkgroup: kept
    app._call(c("383-2", 383, "5550001", now + 40))                  # someone else there: dropped
    assert [x.id.split("-t")[0] for x in texts] == ["1325-1", "1325-2", "383-1"]
    assert all(getattr(x, "kept", "") == "emergency" for x in texts)
    # logged with the marker, so a talkgroup that is normally off says why it is showing
    texts[1].raw, texts[1].status, texts[1].confidence = "BUS514 go ahead", "done", 0.9
    app._transcribed(texts[1])
    assert rows_of(app.cad)[texts[1].id]["kept"] == "emergency"
    # the window is on the screens: "Recording until ..."
    win = app.cad.state()["alarms"]["windows"]
    assert [(w["talkgroup"], w["radios"]) for w in win] == [(1325, ["21514"])] and win[0]["alarm_id"]
    # acknowledged (seen) it keeps running ...
    aid = app.cad.state()["alarms"]["notices"][0]["id"]
    app.cad.alarm_ack(aid, {"operator": "Corey"})
    app._call(c("1325-3", 1325, "1325001", now + 60))
    assert texts[-1].id.split("-t")[0] == "1325-3"
    # ... All clear (resolved) ends it: the switch is respected again
    a = app.cad.alarm_clear(aid, {"operator": "Corey"})
    assert a["clear_by"] == "Corey" and a["clear_at"] and app.cad.state()["alarms"]["windows"] == []
    n = len(texts)
    app._call(c("1325-4", 1325, "1325001", now + 70))
    app._call(c("383-3", 383, "21514", now + 80))
    assert len(texts) == n
    with pytest.raises(Exception):
        app.cad.alarm_clear(aid, {"operator": "Corey"})             # once
    # a talkgroup with Record on is untouched by all this
    app.tg.set(1325, record=True)
    app._call(c("1325-5", 1325, "1325001", now + 90))
    assert len(texts) == n + 1 and not getattr(texts[-1], "kept", "")


def test_the_window_ends_on_its_own(tmp_path):
    app, texts = make(tmp_path)
    app.tg.set(1325, record=False)
    now = time.time() - 5
    app._call(Call("1325-1", 1325, "DOT Dispatch 1", now, now + 3, ["21514"], np.full(48000, 0.2, np.float32), "",
                   [Turn(0.0, "21514", "21514", "", 3.0, now)], {}, "westchester", emergency=True))
    late = now + 3 + 10 * 60 + 5                                     # [p25] emergency_window_min (10) after it
    app._call(Call("1325-2", 1325, "DOT Dispatch 1", late, late + 3, ["1325001"], np.full(48000, 0.2, np.float32), "",
                   [Turn(0.0, "1325001", "1325001", "", 3.0, late)], {}, "westchester"))
    assert [x.id.split("-t")[0] for x in texts] == ["1325-1"]


def test_all_clear_goes_from_the_hub_to_the_station(tmp_path):
    """The station decides what is kept (App._call): the hub's All clear closes its window there too."""
    assert "emergency_all_clear" in station_mod.CALLS
    assert needs("/api/cad/alarm/3/clear") == "dispatch"
    hub = HubRun(hub_cfg(tmp_path))
    sent = []
    hub.app._switch = lambda fn, *args: sent.append((fn, *args))
    try:
        at = time.time() - 20
        a = hub.app.cad.alarms.on_traffic(dict(tx("1325-1", source="DOT Dispatch 1", talkgroup=1325, radio_id="21514",
                                                  emergency=True), start=at, end=at + 15))
        assert [w["talkgroup"] for w in hub.app.cad.state()["alarms"]["windows"]] == [1325]
        hub.app.cad.alarm_ack(a["id"], {"operator": "Corey"})
        assert sent == [] and hub.app.cad.state()["alarms"]["windows"]       # seen is not resolved
        hub.app.cad.alarm_clear(a["id"], {"operator": "Corey"})
        deadline = time.time() + 5
        while not sent and time.time() < deadline:
            time.sleep(0.02)
        assert sent == [("emergency_all_clear", 1325, ["21514"])]
        assert hub.app.cad.state()["alarms"]["windows"] == []
        # its transcript arriving after the All clear doesn't open it again
        hub.app.cad.alarms.on_traffic(dict(tx("1325-1", source="DOT Dispatch 1", talkgroup=1325, radio_id="21514",
                                              emergency=True, text="all good"), start=at, end=at + 15))
        assert hub.app.cad.state()["alarms"]["windows"] == []
    finally:
        hub.close()
    # the station's side: its window closed
    app, _texts = make(tmp_path / "st")
    app.cad.alarms.windows.open(1325, "DOT Dispatch 1", ["21514"], time.time())
    assert app.emergency_all_clear(1325, ["21514"]) is True and app.cad.alarms.windows.state() == []


def test_the_window_is_a_setting(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "config.toml"
    p.write_text('[p25]\nemergency_window_min = 5\n')
    assert load(p).p25_emergency_window_min == 5
    p.write_text('[p25]\nemergency_window_min = 500\n')
    with pytest.raises(ConfigError, match="emergency_window_min"):
        load(p)
    app, _ = make(tmp_path)
    assert app.cad.alarms.windows.keep_s == 600


# -- the page --------------------------------------------------------------------------------------------------


def test_the_radio_view_has_the_emergency_filter_and_the_row_its_chip():
    html = (STATIC / "cad.html").read_text()
    assert '<input id="rv-emergency" type="checkbox">' in html and 'id="rv-other-em"' in html
    rv = (STATIC / "radio-view.js").read_text()
    assert '"pcfd", "emergency", "since"' in rv and "emSig(t)" in rv
    cad = (STATIC / "cad.js").read_text()
    for name in ("cad-feed.js", "board.js"):
        assert "emSig(" in (STATIC / name).read_text(), name
    assert "emSig(x.t)" in cad
    alarm = (STATIC / "alarm.js").read_text()
    assert "/api/cad/alarm/${a.id}/clear" in alarm and '"All clear"' in alarm and "Recording until" in alarm


ROW_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("not found: " + re); return m[0]; };
global.el = (tag, cls, text) => ({ tag, cls: cls || "", text: text === undefined ? "" : String(text), title: "" });
global.fmtTime = (t) => (t ? "19:59" : "");
global.cad = { state: {} };
eval(grab(/^const EM_LOUD = .*$/m) + "\n" + grab(/^function emLoud\(t\) \{[\s\S]*?^\}/m) + "\n"
     + grab(/^function emergencyChip\(t\) \{[\s\S]*?^\}/m) + "\n" + grab(/^function alarmDoneText\(t\) \{[\s\S]*?^\}/m) + "\n"
     + grab(/^function alarmDone\(t\) \{[\s\S]*?^\}/m) + "\n"
     + grab(/^function keptChip\(t\) \{[\s\S]*?^\}/m) + "\nglobal.emergencyChip = emergencyChip; global.alarmDone = alarmDone; "
     + "global.keptChip = keptChip; global.emLoud = emLoud;");
const out = {};
const pick = (n) => (n ? { cls: n.cls, text: n.text } : null);
out.notice = pick(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", talkgroup: 1325, service: "fire" }));
out.ours = pick(emergencyChip({ emergency: 1, alarm_id: 8, alarm_level: "alarm", talkgroup: 307 }));
out.mayday = pick(emergencyChip({ emergency: 0, alarm_id: 9, alarm_level: "alarm", keywords: ["mayday"] }));
out.flagOnly = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "ems" }));
out.none = pick(emergencyChip({ emergency: 0, alarm_id: null }));
out.acked = pick(alarmDone({ ack_by: "Corey <c@x>", ack_at: 1 }));
out.cleared = pick(alarmDone({ ack_by: "Corey", ack_at: 1, clear_by: "Val", clear_at: 2 }));
out.kept = pick(keptChip({ kept: "emergency", service: "fire" }));
// Settings > Radios > Emergencies (default fire, EMS): a bus's button (transit, not ours) is a muted EM, the detail in the
// hover; its window's kept rows a dot. Ours, our channel and a scene-safety row stand out whatever the service.
const full = (n) => (n ? { cls: n.cls, text: n.text, title: n.title } : null);
out.bus = full(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", talkgroup: 4100, service: "transit",
                               ack_by: "Dana <d@x>", ack_at: 1 }));
out.busStuck = pick(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", service: "transit", alarm_stuck: "VAN7 (4107)" }));
out.busDrill = pick(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", service: "transit", drill: true }));
out.busKept = full(keptChip({ kept: "emergency", service: "transit" }));
out.oursOnTransit = pick(emergencyChip({ emergency: 1, alarm_id: 8, alarm_level: "alarm", service: "transit" }));
out.ourChannel = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "transit", ours: 1 }));
// unsure is loud: no service on the row; "other" (Trunk Ops, Interop, OEM) by default; one of our rigs on someone
// else's talkgroup (a repeat press: no alarm row of its own); a row on a talkgroup whose window is ours
out.other = pick(emergencyChip({ emergency: 1, alarm_id: 9, alarm_level: "notice", service: "other" }));
out.ourRig = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "transit", em_ours: true }));
cad.state.alarms = { windows: [{ talkgroup: 4100, ours: true, until: 9 }, { talkgroup: 4300, ours: false, until: 9 }] };
out.oursWindow = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "transit", talkgroup: 4100 }));
out.oursWindowKept = pick(keptChip({ kept: "emergency", service: "transit", talkgroup: 4100, system: "westchester" }));
out.othersWindow = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "transit", talkgroup: 4300 }));
out.otherSystemsTg = pick(emergencyChip({ emergency: 1, alarm_id: null, alarm_level: "", service: "transit", talkgroup: 4100, system: "metro" }));
cad.state.alarms = null;
out.safety = pick(emergencyChip({ emergency: 1, alarm_id: 9, alarm_level: "notice", service: "police", safety: "officer" }));
out.dismissed = pick(emergencyChip({ emergency: 1, alarm_id: 9, alarm_level: "notice", service: "police", safety: "dismissed" }));
out.noService = pick(emergencyChip({ emergency: 1, alarm_id: 9, alarm_level: "notice" }));
cad.state.radio_emergencies = { prominent: ["fire", "ems", "transit"] };
out.busLoud = pick(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", service: "transit" }));
out.busKeptLoud = pick(keptChip({ kept: "emergency", service: "transit" }));
cad.state.radio_emergencies = { prominent: [] };
out.fireQuiet = pick(emergencyChip({ emergency: 1, alarm_id: 7, alarm_level: "notice", service: "fire" }));
out.oursStill = pick(emergencyChip({ emergency: 1, alarm_id: 8, alarm_level: "alarm", service: "fire" }));
out.noServiceStill = pick(emergencyChip({ emergency: 1, alarm_id: 9, alarm_level: "notice" }));
out.notKept = pick(keptChip({ kept: "" }));
console.log(JSON.stringify(out));
"""

GROUPS_HARNESS = r"""
const fs = require("fs");
const stubEl = () => new Proxy({ hidden: false, value: "", checked: false, options: [], dataset: {}, children: [],
  classList: { toggle() {}, contains: () => false, add() {} }, addEventListener() {}, setAttribute() {},
  replaceChildren() {}, append() {}, querySelector: () => null, reset() {} }, { get: (t, k) => (k in t ? t[k] : () => stubEl()) });
global.$ = () => stubEl();
global.window = global; global.addEventListener = () => {};
global.document = { visibilityState: "visible", addEventListener() {} };
global.location = { hash: "" }; global.history = { replaceState() {} };
global.localStorage = { getItem: () => null, setItem() {} };
global.Audio = class { addEventListener() {} pause() {} play() { return Promise.resolve(); } };
for (const n of ["el", "syncList", "radioRow", "evChip", "setText", "show", "opSpan", "talkSig", "fmtTime"]) global[n] = () => stubEl();
global.toLocalInput = () => ""; global.fromLocalInput = () => null; global.Radio = { fillPaging() {} };
global.cad = { state: {}, hooks: [], txRev: 0 };
eval(fs.readFileSync(process.argv[2], "utf8"));
const rows = [
  { id: "a", talkgroup: 383, channel: "WCPD Ch 1", start: 300, text: "10-4", status: "done" },
  { id: "b", talkgroup: 1325, channel: "DOT Dispatch 1", start: 200, text: "", status: "no_speech", rig: "", talker: "BUS514",
    radio_id: "21514", emergency: 1, alarm_id: 7, alarm_level: "notice" },
  { id: "c", talkgroup: 1325, channel: "DOT Dispatch 1", start: 250, text: "go ahead", status: "done", kept: "emergency" },
  { id: "d", talkgroup: 359, channel: "WCPD PSD", start: 100, text: "copy", status: "done" },
].sort((x, y) => y.start - x.start);
const g = RadioView.otherGroups(rows, [{ talkgroup: 359, until: 999 }]);
console.log(JSON.stringify(g.map((x) => ({ name: x.name, emergency: x.emergency, until: x.until, snippet: x.snippet }))));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_row_chip_and_the_folded_group_in_the_pages_own_code(tmp_path):
    h = tmp_path / "row.js"
    h.write_text(ROW_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["notice"] == {"cls": "chip emergency em-other", "text": "Emergency (not ours)"}
    assert out["ours"] == {"cls": "chip emergency", "text": "EMERGENCY"}
    assert out["mayday"] == {"cls": "chip emergency", "text": "MAYDAY"}
    assert out["flagOnly"]["text"] == "Emergency button" and out["none"] is None
    assert out["acked"]["text"] == "· acked by Corey 19:59" and out["cleared"]["text"] == "· all clear Val 19:59"
    assert out["kept"]["text"] == "kept: emergency window" and out["notKept"] is None
    # quiet: someone else's on a service that isn't prominent (default fire, EMS)
    assert (out["bus"]["cls"], out["bus"]["text"]) == ("chip em-quiet", "EM")
    assert out["bus"]["title"].startswith("Emergency (not ours). ") and out["bus"]["title"].endswith(". acked by Dana 19:59")
    assert out["busStuck"] == {"cls": "chip em-quiet", "text": "EM"} and out["busDrill"]["text"] == "DRILL · EM"
    assert (out["busKept"]["cls"], out["busKept"]["text"]) == ("em-dot", "•")
    assert out["busKept"]["title"].startswith("Kept: emergency window. ")
    assert out["dismissed"]["cls"] == "chip em-quiet"
    for k in ("noService", "other", "ourRig", "oursWindow"):                      # unsure, or ours: loud
        assert out[k]["cls"] == "chip emergency em-other", k
    assert out["oursWindowKept"]["text"] == "kept: emergency window"
    assert out["othersWindow"]["cls"] == "chip em-quiet" and out["otherSystemsTg"]["cls"] == "chip em-quiet"
    assert out["oursOnTransit"] == {"cls": "chip emergency", "text": "EMERGENCY"}
    assert out["ourChannel"]["cls"] == "chip emergency em-other" and out["safety"]["cls"] == "chip emergency em-other"
    # the setting: transit made prominent stands out as before; none prominent quiets even fire, never ours
    assert out["busLoud"] == {"cls": "chip emergency em-other", "text": "Emergency (not ours)"}
    assert out["busKeptLoud"]["text"] == "kept: emergency window"
    assert out["fireQuiet"]["cls"] == "chip em-quiet" and out["oursStill"]["cls"] == "chip emergency"
    assert out["noServiceStill"]["cls"] == "chip emergency em-other"             # blank: loud whatever the setting
    h = tmp_path / "groups.js"
    h.write_text(GROUPS_HARNESS)
    groups = json.loads(subprocess.run([NODE, str(h), str(STATIC / "radio-view.js")], capture_output=True, text=True,
                                       check=True, timeout=30).stdout)
    # the bus's talkgroup above the quiet ones (its newest row is older than WCPD's), with the one with a window open;
    # then the rest, newest first. Someone else's emergency doesn't take over the snippet (#190: low-key)
    assert [(x["name"], x["emergency"]) for x in groups] == [("DOT Dispatch 1", "other"), ("WCPD PSD", ""), ("WCPD Ch 1", "")]
    assert groups[0]["snippet"] == "go ahead" and groups[1]["until"] == 999


# -- Settings > Radios > Emergencies: whose emergencies (not ours) stand out on the Radio page -----------------------


def test_the_prominent_services_are_a_radios_setting(tmp_path):
    """Default fire and EMS; set and reset by an admin, audited under Radios, in the state push for the Radio page,
    exported and imported again. Presentation only: someone else's button still raises its notice, never a push."""
    cad = make_cad(tmp_path)
    page = cad.settings.page()["radio_emergencies"]
    assert page == {"prominent": ["fire", "ems", "other"], "default": ["fire", "ems", "other"], "edited": False,
                    "choices": ["fire", "police", "ems", "transit", "air", "other"]}
    assert cad.state()["radio_emergencies"] == {"prominent": ["fire", "ems", "other"]}
    assert needs("/api/cad/settings/radio_emergencies") == "admin"
    out = cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": ["transit", "fire"],
                                                  "operator": "Lt. Test"})
    assert out["radio_emergencies"]["prominent"] == ["fire", "transit"] and out["radio_emergencies"]["edited"]
    assert cad.state()["radio_emergencies"] == {"prominent": ["fire", "transit"]}
    def audit():
        return [r for r in cad.settings.audit_log("radios") if r["key"] == "radio_emergencies/prominent"]
    (a,) = audit()
    assert (a["operator"], a["old"], a["new"]) == ("Lt. Test", "fire, ems, other", "fire, transit")
    # the same again: nothing written, nothing audited
    cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": ["fire", "transit"], "operator": "Lt. Test"})
    assert len(audit()) == 1
    for bad in (["bus"], "fire", None):
        with pytest.raises(Exception, match="prominent"):
            cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": bad, "operator": "Lt. Test"})
    with pytest.raises(Exception, match="key: prominent"):
        cad.settings_post("radio_emergencies", {"action": "set", "key": "other", "value": [], "operator": "Lt. Test"})
    exp = cad.settings_export()
    assert exp["overrides"]["radio_emergencies"] == {"prominent": ["fire", "transit"]}
    assert cad.settings_post("import", {"action": "dry_run", "settings": exp, "operator": "Lt. Test"})
    exp["overrides"]["radio_emergencies"] = {"prominent": ["bus"]}
    with pytest.raises(Exception, match="radio_emergencies"):
        cad.settings_post("import", {"action": "dry_run", "settings": exp, "operator": "Lt. Test"})
    cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": [], "operator": "Lt. Test"})
    assert cad.state()["radio_emergencies"] == {"prominent": []}
    cad.settings_post("radio_emergencies", {"action": "reset", "key": "prominent", "operator": "Lt. Test"})
    assert cad.settings.page()["radio_emergencies"]["edited"] is False
    assert cad.state()["radio_emergencies"] == {"prominent": ["fire", "ems", "other"]}
    assert [r["new"] for r in audit()][:2] == ["fire, ems, other", "none"]


def test_someone_elses_button_is_still_a_notice_and_never_pushed(tmp_path):
    """The setting changes only how the Radio page draws it: the notice, its row and its window are as before, and a
    notice is never pushed (only our alarms are)."""
    cad = make_cad(tmp_path)
    pushed = []
    cad.alarms._push = lambda a: pushed.append(a)
    t = row(cad, id="4100-1-call_1", source="Metro Transit 1", talkgroup=4100, unit="VAN7", radio_id="4107")
    a = cad.alarms.on_traffic(dict(tx(t["id"], source="Metro Transit 1", talkgroup=4100, unit="VAN7", radio_id="4107",
                                      emergency=True), start=t["start"], end=t["end"]))
    assert a["level"] == "notice" and pushed == []
    r = rows_of(cad)[t["id"]]
    assert (r["emergency"], r["alarm_level"]) == (1, "notice") and "service" in r and "ours" in r and "safety" in r


QUIET_GROUPS = r"""
global.emLoud = (t) => t.alarm_level === "alarm" || !!t.ours || !t.service || ["fire", "ems", "other"].includes(t.service);
const rows2 = [
  { id: "q1", talkgroup: 4100, channel: "Metro Transit 1", start: 260, text: "", status: "no_speech", service: "transit",
    emergency: 1, alarm_id: 7, alarm_level: "notice" },
  { id: "q2", talkgroup: 4100, channel: "Metro Transit 1", start: 270, text: "copy", status: "done", service: "transit", kept: "emergency" },
  { id: "f1", talkgroup: 4200, channel: "Other Town Fire", start: 150, text: "", status: "no_speech", service: "fire",
    emergency: 1, alarm_id: 8, alarm_level: "notice" },
  { id: "p1", talkgroup: 383, channel: "WCPD Ch 1", start: 300, text: "10-4", status: "done", service: "police" },
].sort((x, y) => y.start - x.start);
const g2 = RadioView.otherGroups(rows2, [{ talkgroup: 4100, until: 999 }]);
console.log(JSON.stringify(g2.map((x) => ({ name: x.name, emergency: x.emergency, quiet: !!x.quiet }))));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_quiet_emergency_doesnt_move_its_folded_talkgroup_up(tmp_path):
    """Someone else's on a service that isn't prominent: its folded talkgroup keeps its place (newest first) and is
    marked quiet (a muted EM, its window a dot); another town's fire emergency still goes above the quiet ones."""
    h = tmp_path / "groups.js"
    head, _ = GROUPS_HARNESS.split("const rows = [", 1)       # the same stand-in page, these rows
    h.write_text(head + QUIET_GROUPS)
    got = json.loads(subprocess.run([NODE, str(h), str(STATIC / "radio-view.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert got == [{"name": "Other Town Fire", "emergency": "other", "quiet": False},
                   {"name": "WCPD Ch 1", "emergency": "", "quiet": False},
                   {"name": "Metro Transit 1", "emergency": "other", "quiet": True}]


# -- the CAD-wide notice strip and toast (alarm.js) follow the same rule -------------------------------------------------


def _button(cad, tid, source, tg, service, unit, rid):
    t = row(cad, id=tid, source=source, talkgroup=tg, unit=unit, radio_id=rid, service=service)
    return cad.alarms.on_traffic(dict(tx(tid, source=source, talkgroup=tg, unit=unit, radio_id=rid, emergency=True),
                                      start=t["start"], end=t["end"]))


def test_the_state_says_which_notices_and_windows_are_quiet(tmp_path):
    """Someone else's button on transit: its notice and its window are in the state as before (still raised, still
    kept, still acknowledged and cleared by id), marked quiet for the screens. Another town's fire: not quiet. The
    setting moves it; All clear from the row's menu is the same POST."""
    cad = make_cad(tmp_path)
    bus = _button(cad, "4100-1-call_1", "Metro Transit 1", 4100, "transit", "VAN7", "4107")
    fire = _button(cad, "4200-1-call_1", "Other Town Fire", 4200, "fire", "E901", "5901")
    assert (bus["level"], fire["level"]) == ("notice", "notice")
    s = cad.alarms.state()
    assert {a["id"]: a["quiet"] for a in s["notices"]} == {bus["id"]: True, fire["id"]: False}
    assert {w["talkgroup"]: w["quiet"] for w in s["windows"]} == {4100: True, 4200: False}
    cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": ["fire", "ems", "transit"],
                                            "operator": "Lt. Test"})
    assert [a["quiet"] for a in cad.alarms.state()["notices"]] == [False, False]
    cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": [], "operator": "Lt. Test"})
    s = cad.alarms.state()
    assert [a["quiet"] for a in s["notices"]] == [True, True] and [w["quiet"] for w in s["windows"]] == [True, True]
    cad.settings_post("radio_emergencies", {"action": "reset", "key": "prominent", "operator": "Lt. Test"})
    # ours is never quiet, whatever its row's service: an alarm, and its window ours
    ours = _button(cad, "FG1-7", "FG1", None, "other", "E64 P1", "6401")
    assert ours["level"] == "alarm" and all(a["id"] != ours["id"] for a in cad.alarms.state()["notices"])
    # the quiet one is acknowledged and cleared like any (the row's menu on the Radio page posts these)
    cad.alarm_ack(bus["id"], {"operator": "Lt. Test"})
    assert rows_of(cad)["4100-1-call_1"]["ack_by"] == "Lt. Test"
    cad.alarm_clear(bus["id"], {"operator": "Lt. Test"})
    assert rows_of(cad)["4100-1-call_1"]["clear_at"] and 4100 not in {w["talkgroup"] for w in cad.alarms.state()["windows"]}


STRIP_HARNESS = r"""
const fs = require("fs");
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.className = ""; this.textContent = ""; this.children = []; this.hidden = false;
    this.title = ""; this.attrs = {}; }
  get classList() { return { toggle() {}, add() {}, remove() {}, contains: () => false }; }
  append(...ns) { for (const n of ns) if (n && typeof n === "object") this.children.push(n); }
  replaceChildren(...ns) { this.children = []; this.append(...ns); }
  setAttribute(k, v) { this.attrs[k] = v; } addEventListener() {} after() {}
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].filter(Boolean).join(""); }
}
const body = new El("body");
global.window = global;
global.document = { createElement: (t) => new El(t), body, addEventListener() {}, querySelector: () => null };
const toasts = [];
global.UI = { toast: (...a) => toasts.push(a) };
global.cad = { state: { alarms: { open: [], notices: [], recent: [], windows: [] } }, hooks: [] };
eval(fs.readFileSync(process.argv[2], "utf8"));
const strip = () => body.children.find((n) => n.className === "alarm-notices");
const notice = (id, who, channel, quiet) => ({ id, who, channel, at: 1000, text: "", emergency: 1, talkgroup: id * 100, quiet });
const win = (id, quiet) => ({ talkgroup: id * 100, channel: `TG ${id * 100}`, until: 2000, alarm_id: id, radios: [], quiet });
const out = {};
const push = (alarms) => { cad.state.alarms = { open: [], recent: [], ...alarms }; cad.hooks.forEach((h) => h());
                           return { hidden: strip().hidden, rows: strip().hidden ? [] : strip().children.map((r) => r.text), toasts: toasts.splice(0) }; };
out.first = push({ notices: [], windows: [] });
out.quiet = push({ notices: [notice(1, "VAN7", "Metro Transit 1", true)], windows: [win(1, true)] });
out.quietAcked = push({ notices: [], recent: [{ ...notice(1, "VAN7", "Metro Transit 1", true), ack_by: "Dana", what: "emergency button" }], windows: [win(1, true)] });
out.loud = push({ notices: [notice(1, "VAN7", "Metro Transit 1", true), notice(2, "E901", "Other Town Fire", false)], windows: [win(1, true), win(2, false)] });
out.loudWindowOnly = push({ notices: [], windows: [win(1, true), win(2, false)] });
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_quiet_notice_has_no_strip_and_no_toast_a_prominent_one_does(tmp_path):
    h = tmp_path / "strip.js"
    h.write_text(STRIP_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "alarm.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["first"] == {"hidden": True, "rows": [], "toasts": []}
    assert out["quiet"] == {"hidden": True, "rows": [], "toasts": []}            # nothing, on any page; its window neither
    assert out["quietAcked"] == {"hidden": True, "rows": [], "toasts": []}       # nor "Alarm acknowledged by"
    loud = out["loud"]
    assert loud["hidden"] is False and len(loud["rows"]) == 1
    assert loud["rows"][0].startswith("Emergency button (not ours) E901 on Other Town Fire") and "Recording until" in loud["rows"][0]
    assert loud["toasts"] == [["Emergency button: E901 on Other Town Fire", "alert", "Not ours · notice"]]
    w = out["loudWindowOnly"]                                                     # acknowledged: the prominent window's own line
    assert w["hidden"] is False and len(w["rows"]) == 1 and "TG 200" in w["rows"][0] and w["toasts"] == []


MENU_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (re) => { const m = src.match(re); if (!m) throw new Error("not found: " + re); return m[0]; };
global.cad = { state: {} };
global.pageMenuItems = () => []; global.rerunWaiting = () => false; global.addToPage = () => [];
let may = "dispatch";
global.canDo = (x) => x === "dispatch" && may === "dispatch";
eval(grab(/^const EM_LOUD = .*$/m) + "\n" + grab(/^function emLoud\(t\) \{[\s\S]*?^\}/m) + "\n"
     + grab(/^function txMenuItems\(t\) \{[\s\S]*?^\}/m) + "\nglobal.txMenuItems = txMenuItems;");
const labels = (t) => txMenuItems(t).filter((i) => i.label).map((i) => i.label);
const bus = { id: "x", alarm_id: 7, alarm_level: "notice", emergency: 1, service: "transit", text: "" };
const out = { bus: labels(bus), acked: labels({ ...bus, ack_at: 1 }), cleared: labels({ ...bus, ack_at: 1, clear_at: 2 }),
              fire: labels({ ...bus, service: "fire" }), plain: labels({ id: "y", service: "transit" }) };
may = "viewer";
out.viewer = labels(bus);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_quiet_emergency_is_acknowledged_and_cleared_from_its_rows_menu(tmp_path):
    """No strip for it, so its row's menu has Acknowledge and All clear (dispatchers); a prominent one keeps the strip's."""
    h = tmp_path / "menu.js"
    h.write_text(MENU_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["bus"][-3:] == ["Acknowledge emergency", "All clear", "Copy text"]
    assert "Acknowledge emergency" not in out["acked"] and "All clear" in out["acked"]
    for k in ("cleared", "fire", "plain", "viewer"):
        assert "All clear" not in out[k] and "Acknowledge emergency" not in out[k], k
    assert "/api/cad/alarm/${t.alarm_id}/${what}" in (STATIC / "cad.js").read_text()


# -- unsure is loud; quiet only for a service that is known and not prominent -------------------------------------------


def test_the_countys_bus_talkgroups_are_transit_and_the_shared_ones_other():
    """The county file's tags as the classifier reads them (deploy/trunk-recorder/talkgroups.csv): DOT (the buses'
    dispatch, supervisors, maintenance) is transit, so it goes quiet; Operations, Interop/Events and OEM (where a
    mutual-aid firefighter or an unmapped portable of ours could be) are "other": prominent by default."""
    import csv

    from sizeup import sourcekeys
    path = Path(__file__).resolve().parent.parent / "deploy" / "trunk-recorder" / "talkgroups.csv"
    tags = {r["Tag"] for r in csv.DictReader(path.open())}
    got = {t: sourcekeys.service(t) for t in tags}
    assert got["DOT"] == "transit" and got["Fire/EMS"] == "fire" and got["County Police"] == "police"
    assert got["Hospital"] == "ems"
    assert {got[t] for t in ("Operations", "Interop/Events", "OEM")} == {"other"}
    for words in ("Bee-Line", "Bus Dispatch", "County Transit", "Paratransit", "Metro Buses"):
        assert sourcekeys.service(words) == "transit", words


def test_an_unknown_or_other_service_is_never_quiet_by_default(tmp_path):
    cad = make_cad(tmp_path)
    blank = _button(cad, "5100-1-call_1", "Ops 1", 5100, "", "P901", "5901")
    other = _button(cad, "5200-1-call_1", "Interop 1", 5200, "other", "P902", "5902")
    bus = _button(cad, "4100-1-call_1", "Metro Transit 1", 4100, "transit", "VAN7", "4107")
    quiet = {a["id"]: a["quiet"] for a in cad.alarms.state()["notices"]}
    assert quiet == {blank["id"]: False, other["id"]: False, bus["id"]: True}
    assert {w["talkgroup"]: w["quiet"] for w in cad.alarms.state()["windows"]}[5200] is False
    # a row with no service at all, whatever the setting: never quiet
    assert cad.store.one("SELECT service FROM radio_traffic WHERE id = '5100-1-call_1'")["service"] == ""
    cad.settings_post("radio_emergencies", {"action": "set", "key": "prominent", "value": [], "operator": "Lt. Test"})
    quiet = {a["id"]: a["quiet"] for a in cad.alarms.state()["notices"]}
    assert quiet == {blank["id"]: False, other["id"]: True, bus["id"]: True}
    assert cad.alarms._quiet("no-such-row") is False                            # not in the log: shown as before


def test_a_window_on_a_full_alarm_is_never_quiet(tmp_path):
    """"Other agency's button: the full alarm": the bus's button is an alarm, so its window is not quiet (All clear
    stays on the screens) though its service is transit."""
    cad = make_cad(tmp_path)
    cad.settings_post("alarms", {"action": "set", "key": "other", "value": "alarm", "operator": "Lt. Test"})
    a = _button(cad, "4100-1-call_1", "Metro Transit 1", 4100, "transit", "VAN7", "4107")
    assert a["level"] == "alarm"
    s = cad.alarms.state()
    (w,) = s["windows"]
    assert (w["alarm_id"], w["ours"], w["quiet"]) == (a["id"], False, False) and [x["id"] for x in s["open"]] == [a["id"]]
    cad.alarm_ack(a["id"], {"operator": "Lt. Test"})                            # seen: the window's own line, All clear on it
    (w,) = cad.alarms.state()["windows"]
    assert w["quiet"] is False
    cad.alarm_clear(a["id"], {"operator": "Lt. Test"})
    assert cad.alarms.state()["windows"] == []


def test_our_rig_on_another_agencys_talkgroup_is_loud_repeat_presses_too(tmp_path):
    """One of our rigs presses the button on a transit talkgroup, twice: the alarm, its window ours; the second press
    has no alarm row of its own, and its row still says one of ours keyed it (em_ours: cad.js emLoud)."""
    cad = make_cad(tmp_path)
    kw = dict(source="Metro Transit 1", talkgroup=4100, unit="E64 P1", radio_id="6401", rig="E64", service="transit")
    t1 = row(cad, id="4100-1-call_1", emergency=True, **kw)
    a = cad.alarms.on_traffic(dict(tx(t1["id"], emergency=True, **{k: v for k, v in kw.items() if k != "service"}),
                                   start=t1["start"], end=t1["end"]))
    t2 = row(cad, id="4100-2-call_2", emergency=True, at=t1["start"] + 20, **kw)
    again = cad.alarms.on_traffic(dict(tx(t2["id"], emergency=True, **{k: v for k, v in kw.items() if k != "service"}),
                                       start=t2["start"], end=t2["end"]))
    assert a["level"] == "alarm" and again["id"] == a["id"]
    bus = row(cad, id="4100-3-call_3", unit="VAN7", radio_id="4107", source="Metro Transit 1", talkgroup=4100,
              service="transit", emergency=True, at=t1["start"] + 25)
    got = rows_of(cad)
    assert (got[t2["id"]]["alarm_id"], got[t2["id"]]["em_ours"], got[t1["id"]]["em_ours"]) == (None, True, True)
    assert got[bus["id"]]["em_ours"] is False
    (w,) = cad.alarms.state()["windows"]
    assert w["ours"] is True and w["quiet"] is False


def test_a_windows_talkgroup_is_looked_up_on_the_county_system_only(tmp_path):
    """A window with no alarm (notices off): quiet by the newest row on its talkgroup of the county system; another
    system's row with the same talkgroup number says nothing about it."""
    cad = make_cad(tmp_path)
    cad.settings_post("alarms", {"action": "set", "key": "other", "value": "off", "operator": "Lt. Test"})
    assert _button(cad, "4100-1-call_1", "Metro Transit 1", 4100, "transit", "VAN7", "4107") is None
    (w,) = cad.alarms.state()["windows"]
    assert w["alarm_id"] is None and w["quiet"] is True
    cad.store.db.execute("UPDATE radio_traffic SET system = 'metro'")
    assert cad.alarms.state()["windows"][0]["quiet"] is False                   # nothing of the county's on it: shown
