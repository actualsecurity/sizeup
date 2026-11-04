"""Units go back to Available when their call closes, Cancelled ones too (GitHub #232).

2026-11-04: E63 stayed on "Cancelled enroute" (CE) on the right column after its incident was closed; the
other units on the call went back to Available. CE and Cancelled (CN) are free statuses (data/cad_lists.json
_unit_statuses): they clear the unit off its call at once, so Close event, which released only the units
still on the call, never saw it.

    the fix      Close (and Dismiss) also release a unit whose status was set on that call and that has been
                 on no call since, when the status belongs to a call (Lists.call_bound: release_on_close's
                 CN / CE / FR / NR, the working ones); Available, At quarters and Out of service stay
    while open   CE stays as it is: free at once (can be recommended / dispatched again), the board says
                 CE until its call closes
    repair       on startup, a unit left in such a status from a closed (or vanished) call is released, with
                 "released: call 26-000001 was closed" in its history and on the unit log; idempotent
"""
import sqlite3

from sizeup.cad import lists as lists_mod
from sizeup.cad.service import Cad, Settings
from tests.test_cad import write_lists
from tests.test_radios import Clock, _event_with, cad, uid

CLOSE = {"disposition": "", "operator": "JD"}


def close(c, iid):
    return c.close(iid, dict(CLOSE, disposition=c.lists.disposition_codes[0]))


def status(c, name):
    return next(u["status"] for u in c.store.units() if u["name"] == name)


def history(c, name):
    return c.store.q("SELECT old_status, new_status, incident_id, operator, note FROM unit_history WHERE unit_id = ? "
                     "ORDER BY id", (uid(c, name),))


def texts(c, iid):
    return [e["text"] for e in c.full(iid)["events"]]


# -- the reproduction and the close path ---------------------------------------------------------------------------

def test_e63_cancelled_enroute_goes_back_to_available_when_its_call_closes(tmp_path):
    """The 2026-11-04 report: E63 and E60 dispatched, E63 cancelled en route, the call closed. On main E63 stayed CE."""
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60"], clock)
    clock.t += 30
    c.unit_status(uid(c, "E63"), {"status": "RS", "operator": "JD"})
    c.unit_status(uid(c, "E63"), {"status": "CE", "operator": "JD"})
    clock.t += 300
    close(c, a["id"])
    assert status(c, "E63") == "AV" and status(c, "E60") == "AV"
    # The CE record stays in its history, on this call; the release is the same entry as any unit's on close.
    h = history(c, "E63")
    assert [(r["old_status"], r["new_status"]) for r in h] == [("AV", "DP"), ("DP", "RS"), ("RS", "CE"), ("CE", "AV")]
    assert all(r["incident_id"] == a["id"] for r in h)
    assert h[-1]["operator"] == "JD" and h[-1]["note"] == ""
    # The closing line names it with the others; its timer starts again.
    assert texts(c, a["id"])[-1].endswith("; cleared E60, E63")
    assert c.store.unit(uid(c, "E63"))["status_at"] == clock.t
    # A second look changes nothing: the startup repair finds nothing either.
    assert c.store.release_stale() == []


def test_close_releases_cancelled_and_cancelled_enroute_and_not_the_others(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60", "E61", "E62", "E63", "E64", "L31"], clock)
    clock.t += 10
    for name, st in (("E60", "CN"), ("E61", "CE"), ("E62", "AQ"), ("E63", "AV"), ("E64", "FR")):
        c.unit_status(uid(c, name), {"status": st, "operator": "JD"})
    c.unit_status(uid(c, "L31"), {"status": "OS", "note": "pump", "operator": "JD"})
    clock.t += 10
    close(c, a["id"])
    got = {n: status(c, n) for n in ("E60", "E61", "E62", "E63", "E64", "L31")}
    assert got == {"E60": "AV", "E61": "AV", "E62": "AQ", "E63": "AV", "E64": "AV", "L31": "OS"}
    assert c.store.unit(uid(c, "L31"))["out_of_service"] == "pump"
    assert [r["new_status"] for r in history(c, "E62")] == ["DP", "AQ"]     # At quarters says where it is: kept
    assert texts(c, a["id"])[-1].endswith("; cleared E60, E61, E64")


def test_a_cancelled_unit_is_free_at_once_while_the_call_is_open(tmp_path):
    """CE while the call is open: off the call (free, can go again), the board says CE, its history keeps it."""
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60"], clock)
    e63 = uid(c, "E63")
    c.unit_status(e63, {"status": "CE", "operator": "JD"})
    u = c.store.unit(e63)
    assert u["status"] == "CE" and u["incident_id"] is None                 # the board: CE, on no call
    row = next(x for x in c.full(a["id"])["assignments"] if x["unit_id"] == e63)
    assert row["cleared_at"] and row["return"]                              # can be sent back to it
    b = c.create({"type": "STRU", "address": "1 Main St"})
    assert "CE" in c.lists.free                                             # free: the recommendation may pick it
    c.dispatch(b["id"], {"unit_ids": [e63], "operator": "JD"})              # and it can go straight to another call
    assert c.store.unit(e63)["incident_id"] == b["id"]


def test_units_moved_to_another_call_stay(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60", "L31"], clock)
    b = c.create({"type": "STRU", "address": "1 Main St"})
    e63, l31 = uid(c, "E63"), uid(c, "L31")
    c.unit_status(e63, {"status": "CE", "operator": "JD"})
    clock.t += 10
    c.dispatch(b["id"], {"unit_ids": [e63], "operator": "JD"})              # cancelled here, sent there
    c.unit_status(l31, {"status": "CE", "operator": "JD"})
    clock.t += 10
    c.store.set_unit_status_on(l31, "RS", b["id"], "JD")                    # the board: responding to the other call
    clock.t += 10
    close(c, a["id"])
    assert status(c, "E63") == "DP" and status(c, "L31") == "RS"
    assert {x["name"] for x in c.full(b["id"])["assignments"] if not x["cleared_at"]} == {"E63", "L31"}
    assert texts(c, a["id"])[-1].endswith("; cleared E60")
    # A CE set off any call (the board, nothing open) is nobody's to release.
    close(c, b["id"])
    c.unit_status(e63, {"status": "CE", "operator": "JD"})
    d = _event_with(c, ["E60"], clock)
    close(c, d["id"])
    assert status(c, "E63") == "CE"


def test_out_of_service_and_a_later_status_are_untouched(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60"], clock)
    e63, e60 = uid(c, "E63"), uid(c, "E60")
    c.unit_status(e63, {"status": "CE", "operator": "JD"})
    c.unit_status(e63, {"status": "OS", "note": "flat tire", "operator": "JD"})   # off any call now
    c.unit_status(e60, {"status": "OS", "note": "pump", "operator": "JD"})        # straight from the call
    close(c, a["id"])
    assert status(c, "E63") == "OS" and status(c, "E60") == "OS"
    assert c.store.unit(e60)["out_of_service"] == "pump"


def test_reopened_call_then_closed_again(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60"], clock)
    close(c, a["id"])
    c.reopen(a["id"], {"operator": "JD"})
    c.store.assign(a["id"], [uid(c, "E63")], "JD")
    c.unit_status(uid(c, "E63"), {"status": "CE", "operator": "JD"})
    close(c, a["id"])
    assert status(c, "E63") == "AV"


# -- other agencies' units (#219, #223) ----------------------------------------------------------------------------

def test_other_agency_units_cancelled_go_back_to_their_free_status(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60"], clock)
    names = {"RBPD": "CE", "PCPD": "CN"}
    for n in names:
        assert c.lists.agency_set(n) == "responder"
    ems = next(u["name"] for u in c.store.units() if u["status_set"] == "full")
    key = next(u["name"] for u in c.store.units() if u["status_set"] == "contact")
    names[ems] = "CE"
    c.attach(a["id"], {"unit_ids": [uid(c, n) for n in [*names, key]], "operator": "JD"})
    for n, st in names.items():
        c.unit_status(uid(c, n), {"status": "RS", "operator": "JD"})
        c.unit_status(uid(c, n), {"status": st, "operator": "JD"})
    c.unit_status(uid(c, key), {"status": "AV", "operator": "JD"})          # the keyholder: Done (AV)
    close(c, a["id"])
    for n in names:
        assert status(c, n) == "AV", n
        assert history(c, n)[-1]["incident_id"] == a["id"]
    assert status(c, key) == "AV" and [r["new_status"] for r in history(c, key)] == ["DP", "AV"]   # Done: left as is


def test_free_for_an_agency_set_without_available(tmp_path):
    """A set whose free status isn't AV: its own free one."""
    d = lists_mod.load()
    sets = dict(d.agency_status_sets)
    sets["sets"] = dict(sets["sets"], contact={"statuses": ("DP", "RS", "OL", "AQ"), "labels": {}})
    d.agency_status_sets = sets
    key = next(c for c, b in d._board_of.items() if b == "res-keyholder")
    assert d.free_for(key) == "AQ" and d.free_for("E63") == "AV"
    assert d.call_bound("CE") and d.call_bound("CN") and d.call_bound("RS") and d.call_bound("DO")
    assert not d.call_bound("AV") and not d.call_bound("AQ") and not d.call_bound("OS") and not d.call_bound("UNMANNED")
    assert not d.call_bound("NOPE")


def test_release_on_close_in_the_data_file(tmp_path):
    d = lists_mod.load()
    assert d.release_on_close == ("CN", "CE", "FR", "NR")
    assert all(d.role(s) == "free" for s in d.release_on_close)
    # Lenient: a code not free (or gone) drops out; a wrong shape is refused.
    p = write_lists(tmp_path, release_on_close=["CE", "OS", "GONE", "AV"])
    assert lists_mod.load(p).release_on_close == ("CE",)
    p = write_lists(tmp_path, release_on_close="CE")
    try:
        lists_mod.load(p)
    except lists_mod.ListsError as e:
        assert "release_on_close" in str(e)
    else:
        raise AssertionError("a string is not a list")


# -- the startup repair ----------------------------------------------------------------------------------------------

def start(tmp_path, clock, logs):
    """The hub starting on that database: Cad() runs the repair."""
    return Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=logs.append, clock=clock)


def _stuck_db(tmp_path, clock):
    """A database as main left it: E63 on CE from a closed call, plus the cases the repair must leave alone."""
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60", "E61", "E62", "E64", "L31"], clock)
    b = c.create({"type": "STRU", "address": "1 Main St"})
    clock.t += 10
    c.unit_status(uid(c, "E63"), {"status": "CE", "operator": "JD"})       # stuck: the bug
    c.unit_status(uid(c, "E60"), {"status": "CN", "operator": "JD"})       # stuck too
    c.unit_status(uid(c, "E61"), {"status": "AQ", "operator": "JD"})       # at quarters: stays
    c.unit_status(uid(c, "E62"), {"status": "OS", "note": "pump", "operator": "JD"})   # out of service: stays
    c.unit_status(uid(c, "E64"), {"status": "CE", "operator": "JD"})
    c.dispatch(b["id"], {"unit_ids": [uid(c, "E64")], "operator": "JD"})  # moved to an open call: stays
    c.unit_status(uid(c, "L31"), {"status": "CE", "operator": "JD"})
    c.unit_status(uid(c, "L31"), {"status": "ST", "operator": "JD"})       # a later status off any call: stays
    # Close as main did it: only the units still on the call (none here), the rest left where they were.
    with c.store.tx() as db:
        db.execute("UPDATE assignments SET cleared_at = ? WHERE incident_id = ? AND cleared_at IS NULL", (clock.t, a["id"]))
        db.execute("UPDATE incidents SET status = 'closed', closed_at = ?, disposition = ? WHERE id = ?",
                   (clock.t, c.lists.disposition_codes[0], a["id"]))
    c.store.db.close()
    return a, b


def test_startup_repair_releases_units_stuck_on_a_closed_call(tmp_path):
    clock = Clock()
    a, b = _stuck_db(tmp_path, clock)
    clock.t += 3600
    logs = []
    c = start(tmp_path, clock, logs)
    got = {n: status(c, n) for n in ("E63", "E60", "E61", "E62", "E64", "L31")}
    assert got == {"E63": "AV", "E60": "AV", "E61": "AQ", "E62": "OS", "E64": "DP", "L31": "ST"}
    number = c.full(a["id"])["number"]
    h = history(c, "E63")[-1]
    assert (h["old_status"], h["new_status"], h["incident_id"], h["operator"]) == ("CE", "AV", a["id"], "")
    assert h["note"] == f"released: call {number} was closed"
    assert history(c, "E60")[-1]["note"] == f"released: call {number} was closed"
    assert c.store.unit(uid(c, "E63"))["status_at"] == clock.t
    log = [m for m in logs if "released" in m]
    assert len(log) == 1 and log[0].startswith("CAD: released 2 unit(s)") and "E63 CE -> AV" in log[0]
    assert any(e["text"] == f"E63 available (released: call {number} was closed)" for e in c.store.unit_log())
    # The closed call's chronology is not rewritten.
    assert not any("released" in t for t in texts(c, a["id"]))
    # Idempotent: a second start changes nothing and logs nothing.
    c.store.db.close()
    logs.clear()
    rev_rows = sqlite3.connect(tmp_path / "cad.sqlite").execute("SELECT COUNT(*) FROM unit_history").fetchone()[0]
    c = start(tmp_path, clock, logs)
    assert not [m for m in logs if "released" in m]
    assert c.store.release_stale() == []
    assert c.store.q("SELECT COUNT(*) AS n FROM unit_history")[0]["n"] == rev_rows


def test_startup_repair_leaves_a_clean_database_alone(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60"], clock)
    c.unit_status(uid(c, "E63"), {"status": "CE", "operator": "JD"})      # open call: CE stays while it's open
    rev = c.store.rev
    assert c.store.release_stale() == [] and c.store.rev == rev
    assert status(c, "E63") == "CE"
    close(c, a["id"])
    assert c.store.release_stale() == []


def test_startup_repair_dismissed_vanished_and_dangling(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63", "E60", "E61"], clock)
    e63, e60, e61 = uid(c, "E63"), uid(c, "E60"), uid(c, "E61")
    c.unit_status(e63, {"status": "CE", "operator": "JD"})
    # E60 still on the call (a dangling assignment) and E61 CE from an incident row that is gone.
    with c.store.tx() as db:
        db.execute("UPDATE incidents SET status = 'dismissed', closed_at = ? WHERE id = ?", (clock.t, a["id"]))
    c.unit_status(e61, {"status": "CE", "operator": "JD"})
    c.store.db.execute("PRAGMA foreign_keys = OFF")
    c.store.db.execute("UPDATE unit_history SET incident_id = 9999 WHERE id = (SELECT MAX(id) FROM unit_history "
                       "WHERE unit_id = ?)", (e61,))
    c.store.db.execute("PRAGMA foreign_keys = ON")
    out = c.store.release_stale()
    assert {r["unit"]: (r["from"], r["to"]) for r in out} == {"E63": ("CE", "AV"), "E60": ("DP", "AV"), "E61": ("CE", "AV")}
    num = c.full(a["id"])["number"]
    assert history(c, "E63")[-1]["note"] == f"released: call {num} was dismissed"
    assert history(c, "E61")[-1]["note"] == "released: its call is no longer there"
    assert history(c, "E61")[-1]["incident_id"] is None
    assert c.store.q("SELECT COUNT(*) AS n FROM assignments WHERE cleared_at IS NULL")[0]["n"] == 0
    assert c.store.release_stale() == []
