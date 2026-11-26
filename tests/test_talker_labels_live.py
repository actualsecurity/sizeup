"""Talker labels follow Settings > Radios > Radio IDs as it is now (#977): another system's rows keep their radio IDs and
the label they were recorded with, and every reader is shown the label the table gives at read time (radios.py
shown_talker / show_talkers, through Comms.talkers): a label added, changed or removed, or an ID range's, shows on the
rows already logged, the Radio pages are told (comms_rev, the change log's reset) and the state push carries the new
radio_labels. The same number on the county and on MRRS is two radios; each split part resolves by its own ID; label >
learned alias > "unknown MRRS radio" stays the order; the hook intel listens on is called after the commit, and what it
raises never breaks the edit.

Every radio ID, label, alias and transcript here is invented (9900xxx); no real ID, unit or roster is in this file."""

from __future__ import annotations

import json

from sizeup import context_bundle
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from tests.test_radios import Clock, cad

A, B, C, D = "9900301", "9900302", "9900303", "9900450"
PD = 9901                          # an invented talkgroup
WHO = Identity(email="")          # a verified sign-in: another system's rows are served
UNKNOWN = "unknown MRRS radio"


def mrrs(c, tid, rid, talker=UNKNOWN, start=1_800_000_000.0, text="unit two on scene"):
    """One MRRS row as recorded: talker the label the registry gave it then."""
    c.record_traffic({"id": tid, "start": start, "end": start + 3, "source": "PD Dispatch", "talkgroup": PD,
                      "system": "mrrs", "source_key": f"p25:mrrs:{PD}", "tier": "alert", "service": "police",
                      "unit": talker, "radio_id": rid, "rig": "", "text": text, "status": "done", "model": "m.bin"})


def shown(api, q="system=mrrs"):
    return {r["id"]: r["talker"] for r in json.loads(api.get("/api/cad/comms", q, WHO)[1])["rows"]}


def post(api, path, body):
    code, out, _ = api.post(path, json.dumps(body).encode(), WHO)
    assert code == 200, out
    return json.loads(out)


def test_a_label_added_changed_and_removed_shows_on_the_rows_already_logged(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    mrrs(c, f"{PD}-1-call_1", A)
    mrrs(c, f"{PD}-2-call_2", A, start=1_800_000_010.0)
    assert set(shown(api).values()) == {UNKNOWN}
    page = json.loads(api.get("/api/cad/comms", "system=mrrs", WHO)[1])
    st0 = c.state()
    labels0, comms0 = st0["radio_labels"], st0["comms_rev"]

    post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    assert set(shown(api).values()) == {"Example PD officer"}           # both rows, no reload of anything stored
    st1 = c.state()
    assert st1["radio_labels"] != labels0 and st1["comms_rev"] != comms0   # the push says so
    # the Radio page asking for what changed since its cursor is told to reload (the rows came again with the label)
    got = json.loads(api.get("/api/cad/comms", f"system=mrrs&changed={page['cursor']}", WHO)[1])
    assert got["reload"] is True
    # nothing stored was rewritten: the label is read through the table
    assert {r["talker"] for r in c.store.q("SELECT talker FROM radio_traffic")} == {UNKNOWN}

    post(api, "/api/cad/radios", {"system": "mrrs", "radio_id": A, "label": "Example PD sergeant"})
    assert set(shown(api).values()) == {"Example PD sergeant"}
    assert c.state()["radio_labels"] != st1["radio_labels"]

    post(api, "/api/cad/radios/delete", {"system": "mrrs", "radio_id": A})
    assert set(shown(api).values()) == {UNKNOWN}                         # back to "unknown MRRS radio" (the ID beside it)
    assert {r["radio_id"] for r in json.loads(api.get("/api/cad/comms", "system=mrrs", WHO)[1])["rows"]} == {A}


def test_a_range_label_names_rows_already_logged_and_moves_the_rev(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    mrrs(c, f"{PD}-1-call_1", D)
    rev0, labels0 = c.radios.rev, c.radios.labels_rev
    g = post(api, "/api/cad/radios/range", {"system": "mrrs", "lo": "9900400", "hi": "9900499", "label": "Example HQ"})
    assert c.radios.rev != rev0 and c.radios.labels_rev != labels0
    assert shown(api) == {f"{PD}-1-call_1": "Example HQ"}
    rev1 = c.radios.rev
    post(api, "/api/cad/radios/range", {"system": "mrrs", "id": g["id"], "lo": "9900400", "hi": "9900499",
                                        "label": "Example HQ desk"})
    assert c.radios.rev != rev1 and shown(api) == {f"{PD}-1-call_1": "Example HQ desk"}
    post(api, "/api/cad/radios/range/delete", {"id": g["id"]})
    assert shown(api) == {f"{PD}-1-call_1": UNKNOWN}


def test_the_same_number_on_the_county_and_on_mrrs_resolves_separately(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    mrrs(c, f"{PD}-1-call_1", A)
    c.record_traffic({"id": "307-1-call_9", "start": 1_800_000_020.0, "end": 1_800_000_023.0, "source": "Fire/EMS 15",
                      "talkgroup": 307, "unit": "EXAMPLE ALIAS", "radio_id": A, "rig": "", "text": "copy",
                      "status": "done", "model": "m.bin"})
    c.radios.save({"system": "p25", "radio_id": A, "label": "County portable"})
    got = shown(api, "")
    # the county's label names the county's row (no rig), never MRRS's row with the same number
    assert got["307-1-call_9"] == "County portable" and got[f"{PD}-1-call_1"] == UNKNOWN
    c.radios.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    got = shown(api, "")
    assert got["307-1-call_9"] == "County portable" and got[f"{PD}-1-call_1"] == "Example PD officer"
    assert c.radios.shown_talker("p25", A, "EXAMPLE ALIAS") == "County portable"
    assert c.radios.shown_talker("mrrs", A, UNKNOWN) == "Example PD officer"


def test_each_split_part_resolves_by_its_own_radio_id(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    # one call cut into three key-ups (#816 / #821): each part its own row and radio ID
    for k, rid in enumerate((A, B, D)):
        mrrs(c, f"{PD}-1-call_1-t{k}", rid, start=1_800_000_000.0 + k)
    c.radios.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    c.radios.save_range({"system": "mrrs", "lo": "9900400", "hi": "9900499", "label": "Example HQ"})
    assert shown(api) == {f"{PD}-1-call_1-t0": "Example PD officer", f"{PD}-1-call_1-t1": UNKNOWN,
                          f"{PD}-1-call_1-t2": "Example HQ"}
    # a row with two radio IDs ("a, b"): each named, joined
    mrrs(c, f"{PD}-2-call_2", f"{A}, {D}", start=1_800_000_100.0)
    assert shown(api)[f"{PD}-2-call_2"] == "Example PD officer, Example HQ"


def test_label_then_learned_alias_then_unknown(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    c.radios.heard("mrrs", B, "SYS ALIAS 7", channel="PD Dispatch")
    mrrs(c, f"{PD}-1-call_1", B, talker="SYS ALIAS 7")
    assert shown(api) == {f"{PD}-1-call_1": "SYS ALIAS 7"}                   # the alias the system sent
    c.radios.save({"system": "mrrs", "radio_id": B, "label": "Example PD officer"})
    assert shown(api) == {f"{PD}-1-call_1": "Example PD officer"}            # a label beats it
    c.radios.save({"system": "mrrs", "radio_id": B, "label": ""})
    assert shown(api) == {f"{PD}-1-call_1": "SYS ALIAS 7"}                   # blank again: the alias it learned
    # a row with no radio ID, and an ID the registry never keeps: as recorded
    mrrs(c, f"{PD}-2-call_2", "", talker="SOME ALIAS", start=1_800_000_100.0)
    mrrs(c, f"{PD}-3-call_3", "0", talker="ZERO ALIAS", start=1_800_000_200.0)
    got = shown(api)
    assert got[f"{PD}-2-call_2"] == "SOME ALIAS" and got[f"{PD}-3-call_3"] == "ZERO ALIAS"


def test_incident_cards_the_prealert_bundle_and_the_corrections_export_read_the_current_label(tmp_path):
    from sizeup import sourcekeys
    c = cad(tmp_path)
    tid = f"{PD}-1-call_1"
    mrrs(c, tid, A)
    inc = c.create({"type": "STRU", "address": "1 Example St"})
    c.comms.link(tid, inc["id"], "JD")
    c.radios.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    with sourcekeys.request_context(WHO):
        # the incident's Radio card (the state's incidents, built again on the edit)
        card = next(i for i in c.state()["incidents"] if i["id"] == inc["id"])
        assert [r["talker"] for r in card["radio"]] == ["Example PD officer"]
        # the pre-alert's "More info" / Heard (context_bundle: the inference layer reads the same)
        assert context_bundle._by_ids(c, [tid])[tid]["talker"] == "Example PD officer"
        # the transcript corrections' editor and their JSONL export
        c.stt_fix.save(tid, {"corrected": "Unit two on scene", "operator": "JD"})
        assert c.stt_fix.get(tid)["talker"] == "Example PD officer"
        lines = [json.loads(x) for x in c.stt_fix.export_jsonl({})[0].decode().splitlines()]
        assert [x["talker"] for x in lines] == ["Example PD officer"]
        # its radio ID and the label it was recorded with: a snapshot of the export stays reproducible
        assert [(x["radio_id"], x["talker_recorded"]) for x in lines] == [(A, UNKNOWN)]
        c.radios.save({"system": "mrrs", "radio_id": A, "label": "Example PD sergeant"})
        lines = [json.loads(x) for x in c.stt_fix.export_jsonl({})[0].decode().splitlines()]
        assert [x["talker"] for x in lines] == ["Example PD sergeant"]
        card = next(i for i in c.state()["incidents"] if i["id"] == inc["id"])
        assert [r["talker"] for r in card["radio"]] == ["Example PD sergeant"]


def test_the_change_hook_after_the_commit_and_a_failing_hook_never_breaks_the_edit(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    logs: list[str] = []
    r.say = logs.append
    seen: list = []

    def hook(keys):
        seen.append((keys, r.name("mrrs", A), r.rev))         # committed and in memory by the time it is called

    def broken(keys):
        raise RuntimeError("boom")

    r.on_change(broken)
    r.on_change(hook)
    rev0 = r.rev
    r.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    assert seen[-1][0] == {("mrrs", A)} and seen[-1][1] == "Example PD officer" and seen[-1][2] > rev0
    assert r.name("mrrs", A) == "Example PD officer" and any("boom" in m for m in logs)
    g = r.save_range({"system": "mrrs", "lo": "9900400", "hi": "9900499", "label": "Example HQ"})
    assert seen[-1][0] is None                                # a range: "look again at all"
    n = len(seen)
    r.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})          # nothing changed: no call
    r.save_range({"system": "mrrs", "id": g["id"], "lo": "9900400", "hi": "9900499", "label": "Example HQ"})
    assert len(seen) == n
    r.delete("mrrs", A)
    r.delete_range(g["id"])
    assert [k for k, _n, _r in seen[n:]] == [{("mrrs", A)}, None]
    # a radio heard on the air is no person's edit: no hook (its rev still moves for a new row)
    r.heard("mrrs", B, "", channel="PD Dispatch")
    assert len(seen) == n + 2


def test_the_resolver_is_cached_per_rev(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    r.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    calls = []
    real = r.talker
    r.talker = lambda *a, **k: calls.append(a) or real(*a, **k)
    for _ in range(5):
        assert r.shown_talker("mrrs", A, UNKNOWN) == "Example PD officer"
    assert len(calls) == 1
    r.save({"system": "mrrs", "radio_id": A, "label": "Example PD sergeant"})
    assert r.shown_talker("mrrs", A, UNKNOWN) == "Example PD sergeant" and len(calls) == 2


def test_the_pages_redraw_a_row_whose_talker_changed():
    """The rows come again on the change (comms_rev, reload); a row is drawn again only when its signature moves, so the
    talker is in it: the Radio view's and the rail's (RadioView.rowSig), the incident's Radio card's; the live player
    takes the new label onto what is playing or waiting."""
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
    view = (static / "radio-view.js").read_text()
    sig = view[view.index("const rowSig = (t) =>"):view.index("const fillRow")]
    assert "t.talker" in sig and "t.radio_id" in sig
    card = (static / "cad.js").read_text()
    sig = card[card.index('sig: (x) => x.kind === "radio"'):card.index("fill: (li, x) =>")]
    assert "x.t.talker" in sig and "x.t.radio_id" in sig
    lp = (static / "live-player.js").read_text()
    assert "function relabel(rows)" in lp and lp.count("relabel(") >= 3      # defined, and on both answers (reload, delta)
    assert "st.q.paused, ...st.q.pq" in lp                                   # a clip paused under a Priority one too


# -- the review's follow-ups (#978): the county and the fireground, the stored alias kept, the alias's rev, races --------

def unit(c, name):
    """An invented unit on the main board (a rig a radio can be on)."""
    with c.store.tx() as db:
        db.execute("INSERT INTO units (name, kind, status, status_at, board) VALUES (?, 'engine', 'AVAILABLE', 0, 'main')",
                   (name,))
    c.radios.refresh_units()
    return next(u["id"] for u in c.store.units() if u["name"] == name)


def county(c, tid, rid, talker="EXAMPLE ALIAS", rig="", start=1_800_000_000.0):
    c.record_traffic({"id": tid, "start": start, "end": start + 2, "source": "Fire/EMS 15", "talkgroup": 307,
                      "unit": talker, "radio_id": rid, "rig": rig, "text": "copy", "status": "done", "model": "m.bin"})


def fg(c, tid, rid, talker="", rig="", start=1_800_000_000.0):
    c.record_traffic({"id": tid, "start": start, "end": start + 2, "source": "FG1", "talkgroup": None, "unit": talker,
                      "radio_id": rid, "rig": rig, "text": "copy", "status": "done", "model": "m.bin"})


def test_a_county_or_fireground_label_with_no_unit_names_rows_old_and_new(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    unit(c, "Z91")
    county(c, "307-1-call_1", "9900601")
    county(c, "307-2-call_2", "9900601", rig="Z91", start=1_800_000_010.0)     # on a rig: the rig says who it is
    fg(c, "FG1-1", "9A03", talker="9A03", start=1_800_000_020.0)
    page = json.loads(api.get("/api/cad/comms", "view=all")[1])                 # a reader with no sign-in
    c.save_radio({"system": "p25", "radio_id": "9900601", "label": "Example portable"})
    c.save_radio({"system": "mdc", "radio_id": "9a03", "label": "Example chief portable"})
    got = shown(api, "")
    assert got["307-1-call_1"] == "Example portable" and got["307-2-call_2"] == "EXAMPLE ALIAS"
    assert got["FG1-1"] == "Example chief portable"
    # a new row is shown the same; nothing stored was rewritten
    county(c, "307-3-call_3", "9900601", start=1_800_000_030.0)
    assert shown(api, "")["307-3-call_3"] == "Example portable"
    assert c.store.one("SELECT talker FROM radio_traffic WHERE id = '307-1-call_1'")["talker"] == "EXAMPLE ALIAS"
    # our rows' readers (no sign-in) are told to reload too
    assert json.loads(api.get("/api/cad/comms", f"view=all&changed={page['cursor']}")[1])["reload"] is True
    # a county radio's label from what was heard (no person set it) is never shown in place of what each row heard
    c.radios.heard("p25", "9900602", "HEARD ALIAS")
    assert c.radios.get("p25", "9900602")["label"] == "HEARD ALIAS"
    county(c, "307-4-call_4", "9900602", talker="OTHER ALIAS", start=1_800_000_040.0)
    assert shown(api, "")["307-4-call_4"] == "OTHER ALIAS"
    # a label taken off: the row as it heard it again
    c.save_radio({"system": "p25", "radio_id": "9900601", "label": ""})
    assert shown(api, "")["307-1-call_1"] == "EXAMPLE ALIAS"


def test_a_unit_changed_with_past_transmissions_moves_their_rig_and_links_and_off_leaves_them(tmp_path):
    clock = Clock(1_800_000_000.0)
    c = cad(tmp_path, clock)
    z1, z2 = unit(c, "Z91"), unit(c, "Z92")
    old_ev = c.create({"type": "STRU", "address": "1 Example St"})
    c.store.assign(old_ev["id"], [z1], "JD")           # (invented units: on the event as the store puts them)
    new_ev = c.create({"type": "STRU", "address": "2 Example St"})
    c.store.assign(new_ev["id"], [z2], "JD")
    clock.t += 10
    c.save_radio({"system": "mdc", "radio_id": "9A01", "unit": "Z91"})
    c.save_radio({"system": "mdc", "radio_id": "9A02", "unit": "Z91"})
    c.save_radio({"system": "p25", "radio_id": "9900611", "unit": "Z91"})
    fg(c, "FG1-a", "9A01", rig="Z91", start=clock.t)
    fg(c, "FG1-b", "9A02", rig="Z91", start=clock.t + 1)
    county(c, "307-1-call_1", "9900611", rig="Z91", start=clock.t + 2)
    links = lambda tid: {(r["incident_id"], r["why"], r["unit"]) for r in c.store.q(       # noqa: E731
        "SELECT incident_id, why, unit FROM radio_links WHERE traffic_id = ?", (tid,))}
    assert links("FG1-a") == {(old_ev["id"], "unit", "Z91")}
    clock.t += 60
    # on (the default): the rows on the old unit move, their unit links with them, audited with the count
    got = c.save_radio({"system": "mdc", "radio_id": "9A01", "unit": "Z92"})
    assert got["past"] == 1
    assert c.store.one("SELECT rig FROM radio_traffic WHERE id = 'FG1-a'")["rig"] == "Z92"
    assert links("FG1-a") == {(new_ev["id"], "unit", "Z92")}
    audit = c.store.q("SELECT key, old, new FROM settings_audit WHERE key = 'mdc:9A01/past'")
    assert len(audit) == 1 and audit[0]["old"] == "Z91" and audit[0]["new"].startswith("Z92: 1 transmission")
    # off: today's behaviour, the rows keep the unit they were logged with
    got = c.save_radio({"system": "mdc", "radio_id": "9A02", "unit": "Z92", "fix_past": False})
    assert got["past"] == 0
    assert c.store.one("SELECT rig FROM radio_traffic WHERE id = 'FG1-b'")["rig"] == "Z91"
    assert links("FG1-b") == {(old_ev["id"], "unit", "Z91")}
    # cleared, with it on: the rig goes, and so does its unit link (the county's rows the same way)
    got = c.save_radio({"system": "p25", "radio_id": "9900611", "unit": ""})
    assert got["past"] == 1
    assert c.store.one("SELECT rig FROM radio_traffic WHERE id = '307-1-call_1'")["rig"] == ""
    assert links("307-1-call_1") == set()
    try:
        c.save_radio({"system": "mdc", "radio_id": "9A02", "unit": "Z91", "fix_past": "yes"})
        raise AssertionError("fix_past took a word")
    except Exception as e:  # noqa: BLE001
        assert "fix_past" in str(e)


def test_a_radio_the_registry_never_kept_keeps_its_stored_alias_and_a_heard_radios_delete_keeps_its_alias(tmp_path):
    c = cad(tmp_path)
    api = CadApi(c)
    # a valid ID with no entry and no range (a heard event lost, an older row): as recorded, never "unknown"
    mrrs(c, f"{PD}-1-call_1", "9900350", talker="KEPT ALIAS")
    assert shown(api) == {f"{PD}-1-call_1": "KEPT ALIAS"}
    # a heard radio, named, then its name removed: its learned alias again, never "unknown"
    c.radios.heard("mrrs", B, "SYS ALIAS 8", channel="PD Dispatch")
    c.save_radio({"system": "mrrs", "radio_id": B, "label": "Example PD officer"})
    mrrs(c, f"{PD}-2-call_2", B, talker="Example PD officer", start=1_800_000_100.0)
    c.delete_radio({"system": "mrrs", "radio_id": B})
    row = c.radios.get("mrrs", B)
    assert row is not None and row["label"] == "" and row["alias"] == "SYS ALIAS 8" and row["source"] == "heard"
    assert shown(api)[f"{PD}-2-call_2"] == "SYS ALIAS 8"
    # never heard (an admin's entry alone): removed outright, as before
    c.save_radio({"system": "mrrs", "radio_id": C, "label": "Example HQ"})
    c.delete_radio({"system": "mrrs", "radio_id": C})
    assert c.radios.get("mrrs", C) is None


def test_another_systems_alias_change_moves_the_rev_and_the_label_shown(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    r.heard("mrrs", B, "ALIAS ONE", at=1_800_000_000.0)
    assert r.shown_talker("mrrs", B, "ALIAS ONE") == "ALIAS ONE"           # (cached for this rev)
    rev = r.rev
    r.heard("mrrs", B, "ALIAS TWO", at=1_800_000_010.0)                    # within the write throttle: still written
    assert r.rev != rev and r.shown_talker("mrrs", B, "ALIAS ONE") == "ALIAS TWO"
    # a county radio's alias is not another system's talker label: its rev stays (the heard write churn)
    r.heard("p25", "9900621", "COUNTY ONE", at=1_800_000_000.0)
    rev = r.rev
    r.heard("p25", "9900621", "COUNTY TWO", at=1_800_000_010.0)
    assert r.rev == rev


def test_the_state_key_has_the_labels_rev_and_the_hook_writes_under_the_stores_lock(tmp_path):
    c = cad(tmp_path)
    c.state()
    assert c._state[0][-1] == c.radios.labels_rev
    held = []
    real = c.comms.note_change
    c.comms.note_change = lambda *a, **k: held.append(c.store.lock._is_owned()) or real(*a, **k)
    c.save_radio({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    c.save_radio({"system": "p25", "radio_id": "9900601", "label": "Example portable"})
    assert held == [True, True]
    assert c._state is not None                       # (the hook no longer drops it from the writer's thread) ...
    st = c.state()                                    # ... the key moved instead
    assert c._state[0][-1] == c.radios.labels_rev and st["radio_labels"] == c.radios.labels_rev


def test_an_edit_of_notes_or_source_alone_moves_no_label(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    seen = []
    r.on_change(seen.append)
    r.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    g = r.save_range({"system": "mrrs", "lo": "9900400", "hi": "9900499", "label": "Example HQ"})
    n, labels = len(seen), r.labels_rev
    r.save({"system": "mrrs", "radio_id": A, "notes": "a note"})
    r.save({"system": "mrrs", "radio_id": A, "source": "guessed"})
    r.save_range({"system": "mrrs", "id": g["id"], "lo": "9900400", "hi": "9900499", "label": "Example HQ",
                  "notes": "a note"})
    assert len(seen) == n and r.labels_rev == labels
    r.save_range({"system": "mrrs", "id": g["id"], "lo": "9900400", "hi": "9900450", "label": "Example HQ"})
    assert len(seen) == n + 1                         # its IDs moved: rows may be named differently
