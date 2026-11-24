"""MRRS radios named the department's way (sizeup/cad/radios.py OTHER_P25): the registry keyed per system (MRRS radio N is
not county radio N), a label per ID and admin ranges of IDs (an ID's own label wins, ranges never overlap), the talker
label on every row and each split part ("unknown MRRS radio" when nothing names it, the raw ID kept in radio_id), the
MRRS IDs heard learned as rows (never a rig), the "likely agency" hint, Settings > Radios > Radio IDs' add / edit /
remove through the API (audited), the hub naming its station's rows from its own registry, and an older database kept
as it was.

Every radio ID, label and talker here is invented (9900xxx); no real ID, unit or roster is in this file."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from sizeup.cad import radios as radios_mod
from sizeup.cloud.access import Identity
from sizeup.cad.api import CadApi
from sizeup.radio.p25 import Call, Turn
from sizeup.incidents import Traffic
from tests.test_app import voice, wait
from tests.test_p25_systems import EMS, PD, PD_SECURE, rows, two  # noqa: F401 (fixture)
from tests.test_radios import Clock, cad

A, B, C, D = "9900101", "9900102", "9900103", "9900250"
WHO = Identity(email="")     # a verified sign-in (Cloudflare Access): another system's radios and rows are served


def post(api, path, body):
    code, out, _ = api.post(path, json.dumps(body).encode())
    return code, json.loads(out)


def call(app, name, tg, turns, n=3.0, encrypted=False, label="PD Dispatch"):
    """A call with these key-ups: [(pos, radio ID, alias tag)], ids as trunk-recorder's srcList gives them."""
    now = time.time()
    ts = [Turn(pos, tag or src, src, tag, at=now + pos) for pos, src, tag in turns]
    c = Call(f"{tg}-{name}", tg, label, now, now + n, [t.talker for t in ts], voice(n, 1), "", ts, system="mrrs",
             encrypted=encrypted)
    c.ids = {t.talker: (t.src, t.tag) for t in ts}
    return c


# -- the registry ---------------------------------------------------------------------------------------------------------

def test_the_same_number_on_the_county_and_on_mrrs_is_two_radios():
    c = cad()
    r = c.radios
    r.save({"system": "p25", "radio_id": A, "unit": "E61", "label": "Portable 1"})
    r.save({"system": "mrrs", "radio_id": A, "label": "PCPD officer"})
    assert r.resolve("p25", A)["unit"] == "E61" and r.name("p25", A) == "Portable 1"
    assert r.resolve("mrrs", A) is None                          # another agency's radio: never one of our rigs
    assert r.name("mrrs", A) == "PCPD officer" and r.talker("mrrs", A) == "PCPD officer"
    assert r.rig_p25([(A, "")]) == "E61"                         # the county's lookups never see the MRRS entry
    assert {(x["system"], x["radio_id"]) for x in r.rows() if x["radio_id"] == A} == {("p25", A), ("mrrs", A)}
    # an MRRS radio is never given a unit
    try:
        r.save({"system": "mrrs", "radio_id": B, "unit": "E61"})
        raise AssertionError("an MRRS radio took a unit")
    except radios_mod.RadioError as e:
        assert "never on a rig" in str(e)


def test_an_ids_label_beats_its_range_and_ranges_never_overlap():
    c = cad()
    r = c.radios
    g = r.save_range({"system": "mrrs", "lo": "9900100", "hi": "9900199", "label": "Rye PD officer"}, "Lt. Example")
    r.save({"system": "mrrs", "radio_id": A, "label": "PD console"})
    assert r.talker("mrrs", A) == "PD console"                  # its own label first
    assert r.talker("mrrs", B) == "Rye PD officer"              # the range's
    assert r.talker("mrrs", D) == "unknown MRRS radio"          # nothing names it
    assert r.talker("mrrs", D, "SYS ALIAS 1") == "SYS ALIAS 1"  # ... unless the system sent an alias
    assert r.talker("mrrs", "") == ""                           # a key-up with no ID is no radio
    assert r.talker("p25", B) != "Rye PD officer" and r.range_of("p25", B) is None   # MRRS ranges only
    # a heard row with no label is no name: the range still names it
    r.heard("mrrs", B, "", channel="PD Dispatch")
    assert r.talker("mrrs", B) == "Rye PD officer"
    for bad, why in (({"lo": "9900150", "hi": "9900300"}, "overlaps"), ({"lo": "9900199", "hi": "9900199"}, "overlaps"),
                     ({"lo": "9900300", "hi": "9900200"}, "hi at least lo"), ({"lo": "x", "hi": "9"}, "digits"),
                     ({"lo": "9900300", "hi": "9900301", "label": ""}, "label"),
                     ({"system": "p25", "lo": "1", "hi": "2"}, "mrrs only")):
        try:
            r.save_range({"system": "mrrs", "label": "x", **bad})
            raise AssertionError(f"accepted {bad}")
        except radios_mod.RadioError as e:
            assert why in str(e), (bad, str(e))
    # edited in place (moving it over itself is no overlap), then removed: the IDs go back to unknown
    r.save_range({"system": "mrrs", "id": g["id"], "lo": "9900100", "hi": "9900149", "label": "RB PD officer"})
    assert r.talker("mrrs", B) == "RB PD officer" and r.talker("mrrs", "9900150") == "unknown MRRS radio"
    r.delete_range(g["id"], "Lt. Example")
    assert r.talker("mrrs", B) == "unknown MRRS radio" and r.ranges() == []


def test_mrrs_rows_heard_are_learned_without_a_rig_and_throttled():
    clock = Clock()
    c = cad(clock=clock)
    r = c.radios
    r.heard("mrrs", A, "E61", clock.t, "PD Dispatch")           # an alias that is one of OUR board codes
    row = r.get("mrrs", A)
    assert (row["unit"], row["label"], row["alias"], row["source"], row["last_channel"], row["heard"]) == (
        "", "", "E61", "heard", "PD Dispatch", 1)
    assert r.resolve("mrrs", A, "E61") is None and r.talker("mrrs", A, "E61") == "E61"
    clock.t += 5
    r.heard("mrrs", A, "E61", clock.t, "PD Tac")                 # within HEARD_WRITE_S: no write
    assert c.store.one("SELECT heard, last_channel FROM radios WHERE system = 'mrrs' AND radio_id = ?", (A,)) == {
        "heard": 1, "last_channel": "PD Dispatch"}
    clock.t += radios_mod.HEARD_WRITE_S
    r.heard("mrrs", A, "E61", clock.t, "PD Tac")
    assert c.store.one("SELECT heard, last_channel, unit FROM radios WHERE system = 'mrrs' AND radio_id = ?", (A,)) == {
        "heard": 2, "last_channel": "PD Tac", "unit": ""}
    # a person's label wins over what is heard after it
    r.save({"system": "mrrs", "radio_id": A, "label": "MTA PD"})
    clock.t += radios_mod.HEARD_WRITE_S
    r.heard("mrrs", A, "OTHER ALIAS", clock.t, "PD Dispatch")
    assert r.get("mrrs", A)["label"] == "MTA PD" and r.talker("mrrs", A, "OTHER ALIAS") == "MTA PD"


def test_the_likely_agency_hint_is_the_talkgroup_heard_most_and_a_shared_one_says_so():
    counts = [{"radio_id": A, "talkgroup": 9563, "channel": "Harrison PD-EMS", "dept": "Harrison PD", "n": 8},
              {"radio_id": A, "talkgroup": PD, "channel": "PD Dispatch", "dept": "", "n": 2},
              {"radio_id": B, "talkgroup": PD, "channel": "PD Dispatch", "dept": "", "n": 9},
              {"radio_id": B, "talkgroup": 9563, "channel": "Harrison PD-EMS", "dept": "Harrison PD", "n": 1},
              {"radio_id": C, "talkgroup": PD, "channel": "PD Dispatch", "dept": "", "n": 1}]
    h = radios_mod.agency_hints(counts)
    assert h[A] == {"agency": "Harrison PD", "channel": "Harrison PD-EMS", "share": 0.8, "n": 10, "shared": False}
    assert h[B] == {"agency": "", "channel": "PD Dispatch", "share": 0.9, "n": 10, "shared": True}
    assert C not in h                                            # too few to say anything


def test_settings_adds_edits_and_removes_mrrs_names_and_ranges_audited(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    code, row = post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": A, "label": "PCPD officer", "operator": "Lt. Example"})
    assert code == 200 and (row["system"], row["label"], row["unit"], row["source"]) == ("mrrs", "PCPD officer", "", "confirmed")
    code, row = post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": A, "label": "Rye PD officer", "operator": "Lt. Example"})
    assert code == 200 and row["label"] == "Rye PD officer"
    code, err = post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": "99-1", "label": "x"})
    assert code == 400 and "digits" in err["error"]
    code, err = post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": B, "unit": "E61"})
    assert code == 400 and "never on a rig" in err["error"]
    code, g = post(api, "/api/cad/radios/range", {"system": "mrrs", "lo": "9900200", "hi": "9900299", "label": "MTA PD",
                                                  "operator": "Lt. Example"})
    assert code == 200 and (g["lo"], g["hi"], g["label"]) == (9900200, 9900299, "MTA PD")
    code, err = post(api, "/api/cad/radios/range", {"system": "mrrs", "lo": "9900250", "hi": "9900400", "label": "x"})
    assert code == 400 and "overlaps 9900200–9900299" in err["error"]
    code, g = post(api, "/api/cad/radios/range", {"system": "mrrs", "id": g["id"], "lo": "9900200", "hi": "9900299",
                                                  "label": "RB PD officer", "operator": "Lt. Example"})
    assert code == 200 and g["label"] == "RB PD officer"
    # with no sign-in, another system's radios are not served at all (like its traffic)
    got = json.loads(api.get("/api/cad/radios", "")[1])
    assert got["context"] is False and not [x for x in got["radios"] if x["system"] == "mrrs"]
    assert got["ranges"] == [] and got["hints"] == {} and not [a for a in got["log"] if a["key"].startswith("mrrs:")]
    got = json.loads(api.get("/api/cad/radios", "", WHO)[1])
    assert got["context"] is True and [x["label"] for x in got["radios"] if x["system"] == "mrrs"] == ["Rye PD officer"]
    assert "mrrs" in got["systems"] and got["other_systems"] == {"mrrs": "MRRS"}
    assert [(x["lo"], x["label"]) for x in got["ranges"]] == [(9900200, "RB PD officer")]
    assert got["hints"] == {"mrrs": {}}
    assert post(api, "/api/cad/radios/delete", {"system": "mrrs", "radio_id": A, "operator": "Lt. Example"})[0] == 200
    assert post(api, "/api/cad/radios/range/delete", {"id": g["id"], "operator": "Lt. Example"})[0] == 200
    assert c.radios.get("mrrs", A) is None and c.radios.ranges() == []
    # every change in the settings log, kept across a restart
    c2 = cad(tmp_path)
    # with no sign-in (the station's LAN, an all-in-one box without Access) none of it is in the settings log at all
    c2.radios.save({"system": "p25", "radio_id": B, "unit": "E61"}, "Lt. Example")
    for q in ("section=radios", ""):
        code, raw, _ = CadApi(c2).get("/api/cad/settings/audit", q)
        assert code == 200 and "mrrs" not in raw.decode() and "officer" not in raw.decode() and "9900200" not in raw.decode()
        assert f"p25:{B}" in raw.decode()                         # the county's rows stay
    audit = json.loads(CadApi(c2).get("/api/cad/settings/audit", "section=radios", WHO)[1])["audit"]
    keys = [(a["operator"], a["key"], a["old"], a["new"]) for a in audit]
    assert ("Lt. Example", f"mrrs:{A}", "", "no unit · PCPD officer · confirmed") in keys
    assert ("Lt. Example", f"mrrs:{A}/label", "PCPD officer", "Rye PD officer") in keys
    assert ("Lt. Example", "mrrs:9900200-9900299", "", "range · MTA PD") in keys
    assert ("Lt. Example", "mrrs:9900200-9900299/label", "MTA PD", "RB PD officer") in keys
    assert ("Lt. Example", "mrrs:9900200-9900299", "range · RB PD officer", "") in keys
    assert any(k[1] == f"mrrs:{A}" and k[3] == "" for k in keys)


def test_an_older_database_keeps_its_entries_as_they_were(tmp_path):
    c = cad(tmp_path)
    c.radios.save({"system": "p25", "radio_id": A, "unit": "E61", "label": "Portable 1"})
    c.radios.save({"system": "mdc", "radio_id": "0611", "unit": "E61", "label": "Portable 1"})
    c.radios.save({"system": "fleetsync", "radio_id": "167-1002", "label": "Mutual aid portable"})
    c.store.db.close()
    db = sqlite3.connect(tmp_path / "cad.sqlite")
    db.execute("DROP TABLE radio_ranges")                         # a file from before this
    db.commit()
    db.close()
    c2 = cad(tmp_path)
    r = c2.radios
    # the county's entry is still the county's, the fireground's still FG1's; nothing moved to MRRS
    assert r.resolve("p25", A)["unit"] == "E61" and r.get("mrrs", A) is None and r.talker("mrrs", A) == "unknown MRRS radio"
    assert r.resolve("mdc", "0611")["unit"] == "E61" and r.get("fleetsync", "167-1002")["label"] == "Mutual aid portable"
    assert r.ranges() == [] and c2.store.q("SELECT * FROM radio_ranges") == []


# -- the app: rows, split parts, heard rows ------------------------------------------------------------------------------

def test_mrrs_rows_and_each_split_part_carry_their_own_name(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    app.radios.save({"system": "p25", "radio_id": A, "unit": "E61"})          # the county's radio A: one of our rigs
    app.radios.save({"system": "mrrs", "radio_id": A, "label": "PCPD officer"})
    app.radios.save_range({"system": "mrrs", "lo": "9900200", "hi": "9900299", "label": "Rye PD officer"})
    # one MRRS call, three key-ups (a split call, #816 / #821): each part looked up by its own radio ID
    app._call_system(rt, call(app, "1-call_1", PD, [(0.0, A, ""), (1.0, D, ""), (2.0, C, "")]))
    assert wait(lambda: len([r for r in rows(app).values() if r["status"] != "pending"]) == 3)
    got = {r["id"]: r for r in rows(app).values()}
    parts = [got[f"{PD}-1-call_1-t{k}"] for k in range(3)]
    assert [(p["talker"], p["radio_id"], p["rig"]) for p in parts] == [
        ("PCPD officer", A, ""), ("Rye PD officer", D, ""), ("unknown MRRS radio", C, "")]
    # the radio log the console reads says the same
    q = json.loads(CadApi(app.cad).get("/api/cad/comms", "system=mrrs", WHO)[1])
    assert {x["talker"] for x in q["rows"]} >= {"PCPD officer", "Rye PD officer", "unknown MRRS radio"}
    # heard rows under MRRS (never the county's), no rig
    for rid in (A, D, C):
        row = app.radios.get("mrrs", rid)
        assert row is not None and row["unit"] == "" and row["last_channel"] == "PD Dispatch"
    assert app.radios.get("p25", D) is None and app.radios.get("p25", C) is None


def test_an_encrypted_mrrs_call_learns_no_radio(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    app._call_system(rt, call(app, "2-call_2", PD_SECURE, [(0.0, B, "")], label="PD Secure"))
    app._call_system(rt, call(app, "3-call_3", PD, [(0.0, C, "")], encrypted=True))
    assert app.radios.get("mrrs", B) is None and app.radios.get("mrrs", C) is None


def test_the_hint_comes_from_the_radio_log_and_is_never_applied(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    for k in range(3):
        app._call_system(rt, call(app, f"h{k}-call_h{k}", 9563, [(0.0, A, "")], label="Harrison PD-EMS", n=1.0))
    assert wait(lambda: len([r for r in rows(app).values() if r["status"] != "pending"]) == 3)
    got = json.loads(CadApi(app.cad).get("/api/cad/radios", "", WHO)[1])
    assert got["hints"]["mrrs"][A] == {"agency": "Harrison PD", "channel": "Harrison PD-EMS", "share": 1.0, "n": 3,
                                       "shared": False}
    assert app.radios.get("mrrs", A)["label"] == "" and app.radios.talker("mrrs", A) == "unknown MRRS radio"


# -- the hub ---------------------------------------------------------------------------------------------------------------

def test_the_hub_names_its_stations_mrrs_rows_from_its_own_registry(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    hub.radios.save({"system": "mrrs", "radio_id": A, "label": "PCPD officer"})
    hub.radios.save({"system": "p25", "radio_id": A, "unit": "E61"})
    now = time.time()
    base = {"start": now, "end": now + 2, "text": "", "raw": "words", "status": "done", "model": "m.bin",
            "source": "PD Dispatch", "talkgroup": PD, "system": "mrrs", "source_key": f"p25:mrrs:{PD}", "tier": "alert",
            "group": "Police", "service": "police"}
    # the station named it "unknown MRRS radio" (its registry has no names): the hub's own label wins
    t = hub._traffic_of({**base, "id": f"{PD}-h1", "unit": "unknown MRRS radio", "radio_id": A, "pairs": [[A, A]]})
    assert (t.unit, t.rig, t.radio_id) == ("PCPD officer", "", A)
    t = hub._traffic_of({**base, "id": f"{PD}-h2", "unit": "unknown MRRS radio", "radio_id": D, "pairs": [[D, D]]})
    assert t.unit == "unknown MRRS radio"
    # a station from before pairs on MRRS rows: one radio ID, named when the hub knows it, else as sent
    assert hub._traffic_of({**base, "id": f"{PD}-h3", "unit": A, "radio_id": A}).unit == "PCPD officer"
    assert hub._traffic_of({**base, "id": f"{PD}-h4", "unit": D, "radio_id": D}).unit == D
    # the county's row with the same number is still the county's radio
    t = hub._traffic_of({**base, "id": "307-h5", "talkgroup": 307, "system": "", "source": "Fire/EMS 15",
                         "source_key": "", "unit": A, "radio_id": A, "pairs": [[A, A]]})
    assert t.rig == "E61" and t.unit == A
    # an MRRS radio heard at the station: an MRRS heard row on the hub
    hub.apply("heard", {"system": "mrrs", "radio_id": B, "alias": "", "at": now, "channel": "PD Dispatch"}, None)
    row = hub.radios.get("mrrs", B)
    assert (row["source"], row["unit"], row["last_channel"]) == ("heard", "", "PD Dispatch")
    hub.apply("traffic", {**base, "id": f"{PD}-h6", "unit": "unknown MRRS radio", "radio_id": A, "pairs": [[A, A]],
                          "attach": True}, None)
    assert hub.cad.store.one(f"SELECT talker, rig FROM radio_traffic WHERE id = '{PD}-h6'") == {
        "talker": "PCPD officer", "rig": ""}


# -- review follow-ups: bounds, the backfill, the hint cache, overlap under concurrency, the gate, the heard event ----

def test_a_range_end_is_ascii_digits_within_a_p25_id_else_400(tmp_path):
    api = CadApi(cad(tmp_path))
    for lo, hi in (("9" * 40, "9" * 41), ("1", "16777216"), ("\u0661\u0662", "\u0661\u0663"), ("+5", "9"), (" 12 ", "20"),
                   ("1e3", "2000"), (True, 5), ("", "5"), (None, "5"), ([1], "5")):
        code, err = post(api, "/api/cad/radios/range", {"system": "mrrs", "lo": lo, "hi": hi, "label": "x"})
        assert code == 400 and "lo, hi" in err["error"], (lo, hi, code, err)
    code, g = post(api, "/api/cad/radios/range", {"system": "mrrs", "lo": 16777200, "hi": "16777215", "label": "Top"})
    assert code == 200 and (g["lo"], g["hi"]) == (16777200, 16777215)


def test_the_startup_backfill_never_gives_another_systems_row_one_of_our_rigs(two):
    app, _said, _logs = two
    now = time.time()
    mine = Traffic("307-b1", "Fire/EMS 15", now, now + 1, "", status="done", unit="E61", talkgroup=307)
    theirs = Traffic(f"{PD}-b2", "PD Dispatch", now, now + 1, "", status="done", unit="E61", talkgroup=PD)
    theirs.system = "mrrs"
    old = Traffic(f"{PD}-b3", "PD Dispatch", now, now + 1, "", status="done", unit="E61", talkgroup=PD)   # from before
    app.store.loose.extend([mine, theirs, old])                                                           # rows said so
    app._backfill()
    assert mine.rig == "E61" and theirs.rig == "" and old.rig == ""


def test_the_hints_are_cached_and_read_off_the_stores_lock(tmp_path, monkeypatch):
    clock = Clock()
    c = cad(tmp_path, clock)
    calls = []
    real = c.comms.radio_tg_counts
    monkeypatch.setattr(c.comms, "radio_tg_counts", lambda *a: calls.append(a) or real(*a))
    api = CadApi(c)
    api.get("/api/cad/radios", "", WHO)
    api.get("/api/cad/radios", "", WHO)
    assert len(calls) == 1                                       # one scan per HINT_CACHE_S, not one per look
    clock.t += radios_mod.HINT_CACHE_S + 1
    api.get("/api/cad/radios", "", WHO)
    assert len(calls) == 2
    # the scan runs on its own read connection: it finishes while another thread holds the store's lock
    got = []
    with c.store.lock:
        t = threading.Thread(target=lambda: got.append(real("mrrs", 0.0)))
        t.start()
        t.join(5)
    assert got == [[]]


def test_two_admins_saving_overlapping_ranges_at_once_store_one(tmp_path):
    c = cad(tmp_path)
    go = threading.Barrier(8)
    out = []

    def save(k):
        go.wait()
        try:
            c.radios.save_range({"system": "mrrs", "lo": str(9900100 + k), "hi": str(9900200 + k), "label": f"x{k}"})
            out.append("ok")
        except radios_mod.RadioError:
            out.append("refused")
    ts = [threading.Thread(target=save, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert out.count("ok") == 1 and out.count("refused") == 7
    assert len(c.store.q("SELECT * FROM radio_ranges")) == 1


def test_a_cad_that_broadcasts_context_shows_the_mrrs_radios_like_its_traffic(tmp_path):
    c = cad(tmp_path)
    c.radios.save({"system": "mrrs", "radio_id": A, "label": "PCPD officer"})
    # outside a request (a state built for every listener), the same gate as that system's rows (Comms._sees_context)
    assert c.radio_registry()["context"] is False
    c.comms.context_broadcast = True            # (Access enforced: every listener verified)
    got = c.radio_registry()
    assert got["context"] is True and [r["label"] for r in got["radios"] if r["system"] == "mrrs"] == ["PCPD officer"]
    # a request from nobody verified never sees them, broadcast or not (as with the rows)
    assert json.loads(CadApi(c).get("/api/cad/radios", "")[1])["context"] is False


def test_the_station_sends_the_hub_each_mrrs_radio_heard(two):
    app, _said, _logs = two
    rt = app.systems["mrrs"]
    sent = []
    app.events = lambda kind, data, clip: sent.append((kind, data))
    app._call_system(rt, call(app, "5-call_5", PD, [(0.0, A, ""), (1.0, B, "SYS ALIAS 2")]))
    heard = [d for k, d in sent if k == "heard"]
    assert [(d["system"], d["radio_id"], d["alias"], d["channel"]) for d in heard] == [
        ("mrrs", A, "", "PD Dispatch"), ("mrrs", B, "SYS ALIAS 2", "PD Dispatch")]
