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
from tests.test_radios import cad

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
    # the county's row keeps its alias as heard (its rig follows the table through relink), never MRRS's label
    assert got["307-1-call_9"] == "EXAMPLE ALIAS" and got[f"{PD}-1-call_1"] == UNKNOWN
    c.radios.save({"system": "mrrs", "radio_id": A, "label": "Example PD officer"})
    got = shown(api, "")
    assert got["307-1-call_9"] == "EXAMPLE ALIAS" and got[f"{PD}-1-call_1"] == "Example PD officer"
    assert c.radios.shown_talker("p25", A, "EXAMPLE ALIAS") == "EXAMPLE ALIAS"


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
