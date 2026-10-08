"""Radio IDs -> rigs: the registry (seed, pattern guesses, conflicts, edits), resolving MDC and P25
talkers, the live "talking" state from trunk-recorder's log and the FG1 squelch, linking
transmissions to CAD events while a unit is on them, persistence, and the /comms API."""

import json
import sqlite3
import time
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup.app import App
from sizeup.cad.api import CadApi
from sizeup.cad.radios import plan
from sizeup.cad.service import Cad, Settings
from sizeup.cad.store import SCHEMA_VERSION, CadError, Store
from sizeup.radio.fm import FRAME, RATE, Channel, Segment
from sizeup.radio.live import LiveTalk
from sizeup.radio.p25 import Call
from tests.test_app import FakeWhisper, cfg, voice, wait

FIX = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self, t=1_791_400_000.0):
        self.t = t

    def __call__(self):
        return self.t


def cad(tmp_path=None, clock=None, **kw):
    return Cad(Settings(db=str(tmp_path / "cad.sqlite") if tmp_path else "", **kw), log=lambda m: None,
               clock=clock or time.time)


def uid(c, name):
    return next(u["id"] for u in c.store.units() if u["name"] == name)


def read_wav(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


# -- registry ---------------------------------------------------------------------------------------

def test_seed_confirmed_guessed_and_conflicts_left_unmapped():
    c = cad()
    r = c.radios
    assert r.resolve("mdc", "0611") == {"unit": "E61", "label": "Portable 1", "source": "confirmed", "radio_id": "0611"}
    assert r.resolve("mdc", "0615")["label"] == "Portable 5"
    assert r.get("mdc", "9231")["label"] == "South End Firehouse Mobile" and r.resolve("mdc", "9231") is None
    assert r.resolve("mdc", "6301")["unit"] == "E63"                       # heard; E63 by the pattern
    for rid, unit in (("6310", "E63"), ("0631", "E63"), ("3101", "L31"), ("4101", "R41"), ("5905", "E59"),
                      ("0619", "E61"), ("6110", "E61")):
        assert r.resolve("mdc", rid)["unit"] == unit and r.resolve("mdc", rid)["source"] in ("guessed", "confirmed")
    # M40 / R40 and M60 / E60 would share IDs: nobody gets them, and they are listed.
    for rid in ("4001", "4010", "0401", "6001", "0605"):
        assert r.get("mdc", rid) is None and r.resolve("mdc", rid) is None
    pairs = {(tuple(x["units"]), x["ids"]) for x in r.plan["conflicts"]}
    assert (("M40", "R40"), "4001–4010") in pairs and (("M40", "R40"), "0401–0409") in pairs
    assert (("E60", "M60"), "6001–6010") in pairs and (("E60", "M60"), "0601–0609") in pairs
    unfit = {x["unit"] for x in r.plan["unfit"]}
    assert {"TL2", "U239", "U65", "2391", "2398"} <= unfit
    assert not any(row["unit"] in ("E14", "L30", "U65", "TL2") for row in r.rows())   # Rye Brook / no pattern
    assert c.radios.seed() == 0                                            # once per version


def test_edits_win_are_audited_and_survive_reseed_and_restart(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    code, body, _ = api.post("/api/cad/radios", json.dumps({"system": "mdc", "radio_id": "6301", "unit": "E63",
                                                            "label": "Officer portable", "source": "confirmed",
                                                            "operator": "Lt. Ruiz"}).encode())
    assert code == 200 and json.loads(body)["source"] == "confirmed"
    api.post("/api/cad/radios", json.dumps({"system": "mdc", "radio_id": "4001", "unit": "R40", "operator": "Lt. Ruiz"}).encode())
    code, body, _ = api.post("/api/cad/radios", json.dumps({"system": "mdc", "radio_id": "zz", "unit": "E63"}).encode())
    assert code == 400 and "hex" in json.loads(body)["error"]
    code, body, _ = api.post("/api/cad/radios", json.dumps({"system": "mdc", "radio_id": "0001", "unit": "E99"}).encode())
    assert code == 400 and "not on the boards" in json.loads(body)["error"]
    api.post("/api/cad/radios/delete", json.dumps({"system": "mdc", "radio_id": "0612", "operator": "Lt. Ruiz"}).encode())
    c2 = cad(tmp_path)                                                      # restart: kept, not re-seeded over
    r = c2.radios
    assert r.resolve("mdc", "6301")["label"] == "Officer portable" and r.resolve("mdc", "4001")["unit"] == "R40"
    assert r.get("mdc", "0612") is None
    audit = json.loads(CadApi(c2).get("/api/cad/settings/audit", "section=radios")[1])["audit"]
    keys = [(a["operator"], a["key"], a["old"], a["new"]) for a in audit]
    assert ("Lt. Ruiz", "mdc:6301/label", "APX 6000", "Officer portable") in keys
    assert ("Lt. Ruiz", "mdc:6301/source", "guessed", "confirmed") in keys
    assert any(k[1] == "mdc:0612" and k[3] == "" for k in keys)            # removed: old -> ''
    # Heard on the air later with another alias: an edited row is not overwritten.
    r.heard("mdc", "6301")
    assert r.resolve("mdc", "6301")["label"] == "Officer portable"


def test_p25_aliases_name_units_and_radio_ids_are_remembered():
    c = cad()
    r = c.radios
    assert r.resolve("p25", "", "E60")["unit"] == "E60" and r.resolve("p25", "", "R40")["unit"] == "R40"
    assert r.resolve("p25", "", "2398")["unit"] == "2398" and r.resolve("p25", "", "77A4")["unit"] == "77A4"
    assert r.resolve("p25", "", "E60 P2") == {"unit": "E60", "label": "P2", "source": "heard", "radio_id": ""}
    for alias in ("2243 P1", "60 Ctrl OP 6", "BUS527", "E274", "TL57", ""):
        assert r.resolve("p25", "", alias) is None, alias
    r.heard("p25", "41228", "77A4")
    r.heard("p25", "600003", "60 Ctrl OP 3")
    assert r.resolve("p25", "41228", "") == {"unit": "77A4", "label": "77A4", "source": "heard", "radio_id": "41228"}
    assert r.get("p25", "600003")["alias"] == "60 Ctrl OP 3" and r.resolve("p25", "600003") is None
    assert r.rig_p25([("41228", ""), ("600003", "60 Ctrl OP 3"), ("1", "E60")]) == "77A4, E60"
    assert r.rig_for_names("Engine 61 Portable 1, 6301", True, {"0611": "Engine 61 Portable 1"}) == ("E61, E63", "0611, 6301")


def test_fg1_and_p25_rows_carry_the_rig_and_raw_id(tmp_path):
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    app.stt.client = type("W", (), {"transcribe": lambda self, a, prompt="": ("Engine 63 to command", 0.9)})()
    app.stt.start()
    burst = read_wav(FIX / "mdc" / "apx6000_6301_late_burst.wav")
    now = time.time()
    app._segment(Channel(453.0375, "FG1"), Segment(now, now + len(burst) / RATE, burst, 20.0, []))
    app._call(Call("307-1-call_1", 307, "Fire/EMS 15", now + 5, now + 8, ["E60", "60 Ctrl OP 6"],
                   np.concatenate([voice(1.0), voice(2.0, 2)]), "", [(0.0, "E60"), (1.0, "60 Ctrl OP 6")],
                   {"E60": ("1234567", "E60"), "60 Ctrl OP 6": ("600006", "60 Ctrl OP 6")}))
    assert wait(lambda: len(app.store.loose) == 3)
    rows = {t.id: t for t in app.store.loose}
    fg = next(t for t in rows.values() if t.source == "FG1")
    assert (fg.rig, fg.radio_id, fg.unit) == ("E63", "6301", "6301")
    assert (rows["307-1-call_1-t0"].rig, rows["307-1-call_1-t0"].radio_id) == ("E60", "1234567")
    assert (rows["307-1-call_1-t1"].rig, rows["307-1-call_1-t1"].radio_id) == ("", "600006")
    assert app.radios.resolve("p25", "1234567")["unit"] == "E60"          # remembered with the alias
    st = app.state()
    assert {t["rig"] for t in st["loose_traffic"]} == {"E63", "E60", ""}
    assert app.cad.comms_query({})["rows"][0]["id"].startswith("307-")    # logged for /comms


# -- live talking ------------------------------------------------------------------------------

def test_trunk_recorder_log_lines_give_who_is_talking():
    clock = Clock()
    c = cad()
    lt = LiveTalk(resolve=c.radios.resolve, tg_name={307: "Fire/EMS 15", 107: "Fire Dispatch"}.get,
                  learn=lambda src, alias, at: c.radios.heard("p25", src, alias, at), clock=clock)
    lines = (FIX / "trunk-recorder-sample.log").read_text().splitlines()
    by_call = lambda k: [l for l in lines if f"m{k}\x1b" in l]
    # Call 8C (TG 107): E274's alias, then 60 Control keys ("Unit ID set via Control Channel").
    eight = by_call("8C")
    assert lt.feed_line(eight[0]) and lt.state()[0]["radio_id"] == "" and lt.state()[0]["tg"] == 107
    lt.feed_line(eight[1])
    assert (lt.state()[0]["radio_id"], lt.state()[0]["alias"]) == ("36034", "E274")
    lt.feed_line(eight[2])
    s = lt.state()
    assert len(s) == 1 and s[0]["radio_id"] == "600003" and s[0]["channel"] == "Fire Dispatch"
    lt.feed_line(eight[3])
    assert lt.state()[0]["alias"] == "60 Ctrl OP 3" and lt.state()[0]["dispatcher"]
    clock.t += 1
    lt.feed_line(eight[4])                                                  # Concluding Recorded Call
    assert lt.state() == []
    for l in eight[5:]:
        lt.feed_line(l)
    assert lt.state() == []                                                 # both were shown live: no flash
    assert c.radios.get("p25", "600003")["alias"] == "60 Ctrl OP 3"         # learned
    # The parent's example: radio 41228 (77A4) keys on Fire/EMS 15 mid-call -> 77A4 talking.
    head = "[2026-10-07 16:02:11.104221] (info)   [westchester]\t\x1b[0;34m291C\x1b[0m\tTG: \x1b[35m       307\x1b[0m\tFreq: 478.187500 MHz\t"
    lt.feed_line(head + "\x1b[32mStarting P25 Recorder Num [2]\x1b[0m\tTDMA: true\tSlot: 0\tQPSK: true")
    lt.feed_line(head + "Unit ID set via Control Channel, ext: 41228\tcurrent: -1\t samples: 0")
    assert lt.state()[0]["rig"] == "" and lt.state()[0]["channel"] == "Fire/EMS 15"   # alias not known yet
    c.radios.heard("p25", "41228", "77A4")
    assert lt.state()[0]["rig"] == "77A4"
    clock.t += 31                                                           # no word for 30 s: gone
    assert lt.state() == []
    # Call 2C: one transmission, its talker named only after the call (here via a new alias line).
    two = by_call("2C")
    lt.feed_line(two[0])
    assert lt.state()[0]["radio_id"] == ""
    lt.feed_line(two[1])
    assert lt.state()[0]["alias"] == "60 Ctrl OP 7"
    # A first talker never shown live is flashed as "just transmitted" from the src lines.
    lt2 = LiveTalk(resolve=c.radios.resolve, clock=clock)
    four = by_call("4C")
    lt2.feed_line(four[0])
    lt2.feed_line(four[2])                                                  # Unit ID set: 14 (Yonkers OP 2)
    for l in four[5:]:
        lt2.feed_line(l)
    flashed = lt2.state()
    assert [(e["radio_id"], e["ended"]) for e in flashed] == [("21527", True)]
    clock.t += 5
    assert lt2.state() == []
    # trunk-recorder's unit_script (deploy/trunk-recorder/unit-event.sh): the grant names the first talker.
    lt3 = LiveTalk(resolve=c.radios.resolve, clock=clock)
    lt3.feed_line("SIZEUP-UNIT westchester 41228 call 307 ")
    lt3.feed_line(head + "Starting P25 Recorder Num [2]")
    assert lt3.state()[0]["rig"] == "77A4"


UNIT_EVENT = Path(__file__).parent.parent / "deploy" / "trunk-recorder" / "unit-event.sh"


def unit_script(*args: str) -> tuple[list[str], float]:
    """Run deploy/trunk-recorder/unit-event.sh the way trunk-recorder 5.2's unit_script plugin does:
    system("<script> <shortName> <radioID> <event> [<tg> <patches>] &") with trunk-recorder's stdout
    a pipe sizeup reads line by line. Its lines, and how long the first one took to arrive."""
    import subprocess
    t0 = time.time()
    p = subprocess.Popen(["/bin/sh", "-c", f"{UNIT_EVENT} {' '.join(args)} &"], stdout=subprocess.PIPE)
    lines = [raw.decode().rstrip("\n") for raw in iter(p.stdout.readline, b"")]
    took = time.time() - t0
    p.wait()
    return lines, took


def test_unit_script_prints_only_grants_in_trunk_recorders_argument_order():
    assert UNIT_EVENT.is_file() and UNIT_EVENT.stat().st_mode & 0o111
    lines, took = unit_script("westchester", "34701", "call", "307", "")         # no patch: "call 307  &"
    assert lines == ["SIZEUP-UNIT westchester 34701 call 307"] and took < 1.0
    assert unit_script("westchester", "34701", "call", "307", "307,111")[0] == ["SIZEUP-UNIT westchester 34701 call 307"]
    for ev in (("join", "307", ""), ("on",), ("off",), ("ackresp",), ("data",), ("location", "307", "")):
        assert unit_script("westchester", "34701", *ev)[0] == []


def test_a_voice_grant_names_the_talker_at_once():
    # User 2026-10-08: "we are not identifying who is talking on FireEMS15 ... quick enough, it shows
    # up after a few seconds and after, but we really need to on keypress". The grant is the keypress.
    clock = Clock()
    c = cad()
    c.radios.heard("p25", "41228", "77A4")                                    # a radio on a rig
    pokes, notes = [], []
    lt = LiveTalk(resolve=c.radios.resolve, tg_name={307: "Fire/EMS 15", 383: "WCPD Ch 1"}.get, clock=clock,
                  on_change=lambda: pokes.append(clock.t), main=lambda tg: tg == 307, debug=notes.append)
    head = ("[2026-10-08 13:26:29.132061] (info)   [westchester]\t\x1b[0;34m140C\x1b[0m\tTG: \x1b[35m       307"
            "\x1b[0m\tFreq: 476.112500 MHz\t")
    line = unit_script("westchester", "41228", "call", "307", "")[0][0]
    # The grant alone (its "Starting" line not read yet): talking now, pushed before feed_line returns.
    assert lt.feed_line(line) and pokes == [clock.t]
    s = lt.state()
    assert [(e["radio_id"], e["rig"], e["channel"], e["tg"], e["ended"]) for e in s] == [
        ("41228", "77A4", "Fire/EMS 15", 307, False)]
    assert len(notes) == 1 and "radio 41228 (77A4) talking via grant" in notes[0] and "ms after the grant" in notes[0]
    talk_since = s[0]["talk_since"]
    clock.t += 0.01
    # trunk-recorder's own lines for the same call: one entry, the same talker, nothing new to push.
    assert lt.feed_line(head + "\x1b[32mStarting P25 Recorder Num [0]\x1b[0m\tTDMA: true\tSlot: 1\tQPSK: true")
    assert [(e["radio_id"], e["talk_since"]) for e in lt.state()] == [("41228", talk_since)]
    assert not lt.feed_line(line)                                             # the same grant again
    assert not lt.feed_line(head + "Unit ID set via Control Channel, ext: 41228\tcurrent: -1\t samples: 0")
    lt.feed_line(head + "New MotoP25_TDMA alias: 41228 (77A4)")                 # the alias, seconds later
    assert [(e["radio_id"], e["alias"], e["talk_since"]) for e in lt.state()] == [("41228", "77A4", talk_since)]
    assert len(notes) == 1                                                    # not named a second time
    # A re-grant with a new source on the same talkgroup = the next key-up: the talker switches.
    clock.t += 3
    assert lt.feed_line("SIZEUP-UNIT westchester 600003 call 307")
    s = lt.state()
    assert [(e["radio_id"], e["rig"], e["talk_since"]) for e in s] == [("600003", "", clock.t)]   # unknown: the ID
    assert "radio 600003 (unknown radio)" in notes[-1]
    clock.t += 2
    assert lt.feed_line("SIZEUP-UNIT westchester 41228 call 307")             # and back
    assert [e["radio_id"] for e in lt.state()] == ["41228"]
    # The call ends: cleared; the "Transmission src" lines name only talkers already shown: no flash,
    # (the finished call's JSON is the record: radio/p25.py), so nobody is shown twice.
    clock.t += 4
    assert lt.feed_line(head + "Concluding Recorded Call - Last Update: 4s\tRecorder last write:4.48\tCall Elapsed: 9")
    assert lt.state() == []
    for src in ("41228 (77A4)", "600003 (60 Ctrl OP 3)", "41228 (77A4)"):
        lt.feed_line(head + f"- Transmission src: {src} pos:  0.00 length:  2.76")
    assert lt.state() == []
    # Starting first, the grant a few ms later (the script runs in the background): named at the grant.
    head2 = head.replace("140C", "141C")
    lt.feed_line(head2 + "Starting P25 Recorder Num [1]\tTDMA: true\tSlot: 0\tQPSK: true")
    assert [e["radio_id"] for e in lt.state()] == [""]
    assert lt.feed_line("SIZEUP-UNIT westchester 34701 call 307")
    assert [e["radio_id"] for e in lt.state()] == ["34701"] and len(lt.state()) == 1
    # A grant written between a log message and its newline (two writes to the same pipe): both read.
    clock.t += 1
    assert lt.feed_line(head2 + "Unit ID set via Control Channel, ext: 34701\tcurrent: -1\t samples: 0"
                        "SIZEUP-UNIT westchester 41228 call 307")
    assert [e["radio_id"] for e in lt.state()] == ["41228"]
    # Not a talker: a grant with no source, other unit events.
    assert not lt.feed_line("SIZEUP-UNIT westchester -1 call 307")
    assert not lt.feed_line("SIZEUP-UNIT westchester 0 call 307")
    assert not lt.feed_line("SIZEUP-UNIT westchester 41228 join 307")


def test_a_grant_on_a_talkgroup_nothing_records_shows_until_its_timeout():
    from sizeup.radio.live import GRANT_IDLE_S, GRANT_S
    clock = Clock()
    notes = []
    lt = LiveTalk(tg_name={383: "WCPD Ch 1"}.get, clock=clock, main=lambda tg: tg == 307, debug=notes.append)
    assert lt.feed_line("SIZEUP-UNIT westchester 5551 call 383")
    assert [(e["radio_id"], e["channel"], e["main"]) for e in lt.state()] == [("5551", "WCPD Ch 1", False)]
    assert notes == []                                                        # debug only for main talkgroups
    clock.t += GRANT_IDLE_S - 0.5
    assert len(lt.state()) == 1
    clock.t += 1
    assert lt.state() == []
    # A held grant too old for the call that starts later: that call starts with no talker.
    lt.feed_line("SIZEUP-UNIT westchester 5551 call 383")
    clock.t += GRANT_S + 0.5
    lt.feed_line("[westchester]\t9C\tTG:        383\tFreq: 476.112500 MHz\tStarting P25 Recorder Num [3]")
    assert [e["radio_id"] for e in lt.state()] == [""]


def test_fg1_open_squelch_is_talking_and_live_mdc_names_the_rig(tmp_path):
    app = App(cfg(tmp_path))
    app._kind = {"FG1": "traffic"}
    burst = read_wav(FIX / "mdc" / "apx6000_6301_late_burst.wav").astype(np.float64)

    class Sq:
        open, _start = True, time.time() - 0.5
        _frames = []

    class Rx:
        channel = Channel(453.0375, "FG1")
        squelch = Sq()

        def status(self):
            return {"label": "FG1", "mhz": 453.0375, "state": "ok", "detail": "", "open": Sq.open,
                    "quieting_db": None, "restarts": 0, "last_heard": None}
    app.receivers = [Rx()]
    app._watch_fm_once()
    s = app.state()["talking"]
    assert [(e["channel"], e["rig"], e["radio_id"]) for e in s] == [("FG1", "", "")]   # open, radio not known yet
    assert s[0]["id_on_release"] is True and s[0]["system"] == "fm"             # PCFD's MDC post-ID: "FG1 · ID on release"
    Sq._frames = [burst[i:i + FRAME] for i in range(0, len(burst) - FRAME + 1, FRAME)]
    app._watch_fm_once()
    s = app.cad.state()["talking"]
    assert [(e["channel"], e["rig"], e["radio_id"]) for e in s] == [("FG1", "E63", "6301")]
    assert s[0]["id_on_release"] is False                                        # decoded: the unit, as before
    Sq.open = False
    app._watch_fm_once()
    assert app.state()["talking"] == []
    # A post-ID only known when the transmission ends is flashed then.
    app.live.fm_segment("FG1", ["0611"], time.time() - 2, time.time())
    assert [(e["rig"], e["ended"]) for e in app.state()["talking"]] == [("E61", True)]


# -- linking to events ------------------------------------------------------------------------

def _event_with(c, units, clock, operator="Lt. Ruiz"):
    inc = c.create({"type": "STRU", "address": "209 Westchester Ave", "operator": operator})
    c.dispatch(inc["id"], {"unit_ids": [uid(c, u) for u in units], "operator": operator})
    return inc


def tx(tid, start, source="FG1", rig="", unit="", tg=None, text="words", radio_id=""):
    return {"id": tid, "start": start, "end": start + 2, "source": source, "talkgroup": tg, "unit": unit, "rig": rig,
            "radio_id": radio_id, "text": text, "status": "done"}


def test_traffic_links_to_the_event_while_the_unit_is_on_it_and_not_after(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    before = c.record_traffic(tx("FG1-1", clock.t - 5, rig="E63"))
    assert before == []
    a = _event_with(c, ["E63", "E60"], clock)
    clock.t += 10
    assert c.record_traffic(tx("FG1-2", clock.t, rig="E63", radio_id="6301")) == [a["id"]]
    assert c.record_traffic(tx("FG1-3", clock.t + 1)) == [a["id"]]                     # FG1, no ID, one call: that one
    # A known radio that isn't a rig on the call (9231 = South End Firehouse mobile) is never attached.
    assert c.record_traffic(tx("FG1-9231", clock.t + 1.5, radio_id="9231")) == []
    assert c.record_traffic(tx("307-9-call_4-t1", clock.t + 2, "Fire/EMS 15", "E60", "E60", 307)) == [a["id"]]
    assert c.record_traffic(tx("307-9-call_4-t0", clock.t + 1, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == [a["id"]]
    assert c.record_traffic(tx("307-9-call_4-t2", clock.t + 4, "Fire/EMS 15", "", "60 Ctrl OP 6", 307)) == [a["id"]]
    assert c.record_traffic(tx("307-8-call_1", clock.t + 5, "Fire/EMS 15", "", "60 Ctrl OP 2", 307)) == []   # no unit turn
    assert c.record_traffic(tx("301-7-call_2", clock.t + 6, "60 Control", "77A4", "77A4", 301)) == []        # not on it
    b = c.create({"type": "STRU", "address": "1 Main St"})
    c.dispatch(b["id"], {"unit_ids": [uid(c, "L31")], "operator": "x"})
    clock.t += 5
    assert c.record_traffic(tx("FG1-4", clock.t, rig="E63")) == [a["id"]]             # its unit's event only
    assert c.record_traffic(tx("FG1-5", clock.t + 1)) == []                           # unknown radio, two calls: neither
    # E63 clears (back in service): its later traffic is no longer on the event.
    clock.t += 10
    c.unit_status(uid(c, "E63"), {"status": "AV"})
    clock.t += 1
    assert c.record_traffic(tx("FG1-6", clock.t, rig="E63")) == []                    # cleared: on no event
    c.close(a["id"], {"disposition": c.lists.disposition_codes[0]})
    c.close(b["id"], {"disposition": c.lists.disposition_codes[0]})
    clock.t += 1
    assert c.record_traffic(tx("FG1-7", clock.t, rig="E63")) == []
    radio = {r["id"]: r["why"] for r in c.full(a["id"])["radio"]}
    assert radio == {"FG1-2": "unit", "FG1-3": "channel", "307-9-call_4-t1": "unit", "307-9-call_4-t0": "dispatcher",
                     "307-9-call_4-t2": "dispatcher", "FG1-4": "unit"}
    # The dispatcher's turn can arrive first: linked when the unit's turn is linked.
    d = _event_with(c, ["R40"], clock)
    clock.t += 1
    assert c.record_traffic(tx("307-5-call_9-t0", clock.t, "Fire/EMS 15", "", "60 Ctrl OP 1", 307)) == []
    c.record_traffic(tx("307-5-call_9-t1", clock.t + 2, "Fire/EMS 15", "R40", "R40", 307))
    assert {r["id"]: r["why"] for r in c.full(d["id"])["radio"]} == {"307-5-call_9-t0": "dispatcher",
                                                                     "307-5-call_9-t1": "unit"}
    # A speech-to-text re-run updates the copied text.
    assert c.comms.retext("FG1-2", "Engine 63 on location", "done", "m")
    # Restart: links and transcripts are in the CAD database.
    c2 = cad(tmp_path, clock)
    rows = {r["id"]: r for r in c2.full(a["id"])["radio"]}
    assert rows["FG1-2"]["text"] == "Engine 63 on location" and rows["FG1-2"]["rig"] == "E63"
    assert rows["FG1-2"]["radio_id"] == "6301" and len(rows) == 6
    assert c2.state()["incidents"][0]["radio"] or any(i["radio"] for i in c2.state()["incidents"])


def test_comms_api_filters(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    clock.t += 5
    c.record_traffic(tx("FG1-1", clock.t, rig="E63", radio_id="6301"))
    c.record_traffic(tx("307-1-call_1", clock.t + 1, "Fire/EMS 15", "77A4", "77A4", 307, radio_id="41228"))
    c.record_traffic(tx("307-2-call_1", clock.t + 2, "Fire/EMS 15", "E60", "E60", 307))
    c.record_traffic(tx("FG1-2", clock.t + 3, rig="E14"))                  # Rye Brook: not PCFD's
    api = CadApi(c)
    get = lambda q: json.loads(api.get("/api/cad/comms", q)[1])
    all_ = get("")
    assert [r["id"] for r in all_["rows"]] == ["FG1-2", "307-2-call_1", "307-1-call_1", "FG1-1"]   # newest first
    assert all_["rows"][-1]["events"][0]["number"] == a["number"] and all_["rows"][-1]["has_audio"] is False
    assert set(all_["channels"]) == {"FG1", "Fire/EMS 15", "Notes"} and {"E63", "77A4", "E60", "E14"} <= set(all_["rigs"])
    assert [r["id"] for r in get("unit=E63")["rows"]] == ["FG1-1"]
    assert [r["id"] for r in get("channel=Fire%2FEMS+15")["rows"]] == ["307-2-call_1", "307-1-call_1"]
    assert [r["id"] for r in get(f"event={a['number']}")["rows"]] == ["FG1-1"]   # E14 (known radio) is not on it
    assert [r["id"] for r in get("pcfd=1")["rows"]] == ["307-2-call_1", "FG1-1"]
    assert [r["id"] for r in get(f"since={clock.t + 0.5}&until={clock.t + 1.5}")["rows"]] == ["307-1-call_1"]
    assert get(f"rev={all_['rev']}")["unchanged"] is True
    code, body, _ = api.get("/api/cad/comms", "since=yesterday")
    assert code == 400


def test_comms_text_search_and_main_flag_for_the_radio_view(tmp_path):
    """The CAD's Radio view (which replaced /comms): ?q= finds words in the transcript or a note (any
    case, LIKE's own characters taken literally), and each row says whether it goes up front (Main:
    no talkgroup, a talkgroup marked Main, a note) or folds under Other talkgroups."""
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    c.record_traffic(tx("FG1-1", clock.t + 1, rig="E63", text="Engine 63 on location"))
    c.record_traffic(tx("307-1-call_1", clock.t + 2, "Fire/EMS 15", "E60", "E60", 307, text="Engine 60 responding"))
    c.record_traffic(tx("359-1-call_1", clock.t + 3, "WCPD PSD", "", "1234", 359, text="unit 12, 100% copy"))
    c.note(a["id"], {"text": "E63 stated that the engine is in service", "units": ["E63"], "operator": "Corey"})
    get = lambda q: json.loads(CadApi(c).get("/api/cad/comms", q)[1])["rows"]
    found = get("q=ENGINE")
    assert [r["id"] for r in found][:2] == ["307-1-call_1", "FG1-1"] and [r["kind"] for r in found][2:] == ["note"]
    assert [r["id"] for r in get("q=engine+location")] == ["FG1-1"]                # every word
    assert [r["id"] for r in get("q=100%25")] == ["359-1-call_1"] and get("q=1_0") == []
    main = {r["id"]: r["main"] for r in get("")}
    assert main["FG1-1"] and main["307-1-call_1"] and not main["359-1-call_1"]   # 307: an incident talkgroup
    assert all(r["main"] for r in get("type=notes"))
    c.main_tg = lambda tg: tg == 359                                           # the station's Main switch (App sets it)
    main = {r["id"]: r["main"] for r in get("")}
    assert main["359-1-call_1"] and not main["307-1-call_1"] and main["FG1-1"]


def test_v3_database_migrates_to_v4(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    db = sqlite3.connect(tmp_path / "cad.sqlite")
    for t in ("radios", "radio_traffic", "radio_links", "settings_audit", "meta"):
        db.execute(f"DROP TABLE {t}")
    db.execute("ALTER TABLE assignments DROP COLUMN assigned_at")
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()
    c2 = cad(tmp_path, clock)
    assert c2.store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
    assert "schema v3 -> v4 (radio IDs, communications log)" in c2.store.migrated
    assert c2.store.one("SELECT assigned_at FROM assignments")["assigned_at"] is not None   # from dispatched_at
    assert c2.radios.resolve("mdc", "0611")["unit"] == "E61"
    clock.t += 1
    assert c2.record_traffic(tx("FG1-1", clock.t, rig="E63")) == [a["id"]]


def test_plan_is_data_driven():
    units = [{"name": "E7", "kind": "engine", "board": "main", "department": "", "mutual_aid": False},
             {"name": "R7", "kind": "rescue", "board": "main", "department": "", "mutual_aid": False},
             {"name": "E70", "kind": "engine", "board": "main", "department": "", "mutual_aid": False}]
    seed = {"patterns": [{"name": "new", "label": "r{k}", "first": 1, "last": 2, "format": "{n}{k:02d}"}],
            "pattern_kinds": ["engine", "rescue"]}
    p = plan(seed, units)
    assert sorted(p["guesses"]) == ["7001", "7002"] and {x["unit"] for x in p["unfit"]} == {"E7", "R7"}


def test_units_on_an_incident_are_offered_only_the_next_steps():
    # User: "we should only be able to click the 'next up' options" (cad_lists.json status_flow).
    c = cad()
    clock_inc = c.create({"type": "STRU", "address": "1 Main St"})
    c.dispatch(clock_inc["id"], {"unit_ids": [uid(c, "E63")], "operator": "x"})
    nxt = lambda: next(a["next"] for a in c.full(clock_inc["id"])["assignments"] if a["name"] == "E63")
    assert nxt() == ["RS", "OL", "ST", "AV", "CN", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "AK"})
    assert nxt() == ["RS", "OL", "ST", "AV", "CN", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "RS"})
    assert nxt() == ["OL", "ST", "AV", "CE", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "ST"})
    assert nxt() == ["OL", "AV", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "OL"})
    assert nxt() == ["IC", "AV", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "IC"})
    assert nxt() == ["OL", "AV", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "DO"})        # the API still takes any status (the board)
    assert nxt() == ["AV", "OS"]
    for a in c.state()["incidents"][0]["assignments"]:
        assert not {"DP", "DO", "AK", "UNMANNED"} & set(a["next"])
    assert c.state()["lists"]["status_flow"]["RS"] == ["OL", "ST", "AV", "CE", "OS"]
    c.unit_status(uid(c, "E63"), {"status": "AV"})              # clears it off the event
    a = next(a for a in c.full(clock_inc["id"])["assignments"] if a["name"] == "E63")
    assert a["cleared_at"] and a["next"] == []


def test_unit_tag_autocomplete_order_and_prefixes():
    # User: start typing "E..E6...E63" in the comment box and tab-select the unit.
    c = cad()
    inc = c.create({"type": "STRU", "address": "1 Main St"})
    c.dispatch(inc["id"], {"unit_ids": [uid(c, "E63"), uid(c, "L31")], "operator": "x"})
    s = lambda q: c.suggest_units(inc["id"], q)
    assert s("E6") == ["E63", "E60", "E61", "E62", "E64"]                    # on the event first, then board order
    assert s("e6") == s("E6") and s("E63") == ["E63"] and s("") == []
    assert s("E")[:2] == ["E63", "E58"] and "E14" in s("E1")                  # Rye Brook's engine is a fire unit
    assert s("L3") == ["L31", "L30"]
    assert s("23") == ["2391", "2392", "2393", "2394", "2395", "2396", "2397", "2398"]     # 8 at most
    assert s("77A")[:3] == ["77A1", "77A2", "77A3"]                           # EMS: after the fire units
    order = c.tag_order(inc["id"])
    assert order.index("U239") < order.index("77A1") < order.index("PCPD")    # fire, then EMS / police / agencies
    assert s("242_e") == ["242_eNG"] and s("239_c") == ["239_Cascd"]          # codes as written, any case
    assert c.state()["incidents"][0]["tag_units"][:2] == ["E63", "L31"]
    got = json.loads(CadApi(c).get(f"/api/cad/incident/{inc['id']}/units", "q=e6")[1])
    assert got["units"][0] == "E63"


def test_unit_tagged_notes_are_stored_exported_and_merge_with_radio(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "L31"], clock)
    api = CadApi(c)
    clock.t += 5
    c.record_traffic(tx("FG1-1", clock.t, rig="E63", radio_id="6301", text="Engine 63 on location"))
    clock.t += 5
    code, body, _ = api.post(f"/api/cad/incident/{a['id']}/note", json.dumps({
        "text": "E63 stated that the building manager is not able to access the basement",
        "units": ["e63"], "operator": "Lt. Ruiz"}).encode())
    assert code == 200 and json.loads(body)["units"] == ["E63"]
    clock.t += 5
    api.post(f"/api/cad/incident/{a['id']}/note", json.dumps({"text": "E63 and L31 report all clear",
                                                             "units": ["E63", "L31"]}).encode())
    api.post(f"/api/cad/incident/{a['id']}/note", json.dumps({"text": "Con Ed notified"}).encode())
    code, body, _ = api.post(f"/api/cad/incident/{a['id']}/note", json.dumps({"text": "x", "units": ["Q9"]}).encode())
    assert code == 400
    notes = [e for e in c.full(a["id"])["events"] if e["kind"] == "note"]
    assert [(n["units"], n["text"][:8]) for n in notes] == [("E63", "E63 stat"), ("E63,L31", "E63 and "), ("", "Con Ed n")]
    # The event-log CSV: the note's unit tags in the Unit column.
    import csv as _csv, io as _io
    log = list(_csv.DictReader(_io.StringIO(c.export("event-log.csv").decode())))
    units = {r["Event / Comment"][:14]: r["Unit"] for r in log}
    assert units["E63 stated tha"] == "E63" and units["E63 and L31 re"] == "E63, L31" and units["Con Ed notifie"] == ""
    # /comms: E63's typed notes with its transmissions, newest first; type=notes / channel Notes.
    get = lambda q: json.loads(api.get("/api/cad/comms", q)[1])
    rows = get(f"unit=E63&event={a['number']}")["rows"]
    assert [(r["kind"], r["text"][:10]) for r in rows] == [("note", "E63 and L3"), ("note", "E63 stated"),
                                                          ("radio", "Engine 63 ")]
    assert [r["kind"] for r in get("unit=L31")["rows"]] == ["note"]
    assert all(r["kind"] == "note" for r in get("type=notes")["rows"]) and len(get("type=notes")["rows"]) == 2
    assert [r["kind"] for r in get("channel=Notes")["rows"]] == ["note", "note"]
    assert [r["kind"] for r in get("type=radio")["rows"]] == ["radio"]
    assert get("pcfd=1")["rows"][0]["events"][0]["number"] == a["number"]


def test_old_channel_links_of_known_radios_are_dropped(tmp_path):
    # Links made by the earlier rule (a known radio, or one transmission on two calls) go on restart.
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    clock.t += 5
    c.record_traffic(tx("FG1-u", clock.t))                                   # unknown radio, one call: kept
    with c.store.tx() as db:
        db.execute("INSERT INTO radio_traffic (id, start, end, channel, radio_id, rig, text, status, updated_at) "
                   "VALUES ('FG1-k', ?, ?, 'FG1', '9231', '', '', 'no_speech', ?)", (clock.t, clock.t, clock.t))
        db.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES ('FG1-k', ?, 'channel', '')", (a["id"],))
    from sizeup.cad.comms import Comms
    Comms(c.store)
    assert {r["id"] for r in c.full(a["id"])["radio"]} == {"FG1-u"}
