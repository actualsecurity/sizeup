"""A non-emergency event's Until enforced on the hub (Store.expire_until): a notification, a road closure, a hydrant
out of service with an Until closes by itself, as Expired, once it passes; from the stored until_at alone (a restart
or a takeover in between only makes it later), checked by the incident-sources ticker at its start and every few
seconds, on the leader only; said on its timeline. Before, the Until was only shown: nothing ever closed the event.

A new event's Until in the past is refused (it would expire at once: a wrong day), an edit to before the event was
created too; Reopen clears a passed one. The console shows the day when the Until isn't today or the day the event
began, and flags one before the event was created, or passed while it is still open. All data here is made up."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sizeup.cad.store import EXPIRED, UNTIL_OPERATOR, CadError
from tests.test_cad import Clock, by_name, local, make_cad, monitor_incident, texts, uid

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


def notif(cad, until, **kw):
    return cad.create({"type": "NOTIF", "address": "40 Example Rd", "remarks": "street closed for a film shoot",
                       "until_at": until, "operator": "JD", **kw})["id"]


def closing(cad, iid):
    return next(e for e in cad.store.full(iid)["events"] if e["kind"] == "closed")


def test_a_passed_until_closes_the_event_as_expired_once():
    clock = Clock(local(2026, 10, 7, 12, 37))
    logs = []
    cad = make_cad(clock=clock)
    cad.log = logs.append
    iid = notif(cad, local(2026, 10, 7, 13, 37))
    other = notif(cad, None)                                   # no Until: open until a person closes it
    clock.t = local(2026, 10, 7, 13, 36)
    assert cad.expire_until() == [] and cad.store.incident(iid)["status"] == "pending"
    clock.t = local(2026, 10, 7, 13, 37) + 4                   # the ticker's next look
    done = cad.expire_until()
    assert [(d["id"], d["closed"]) for d in done] == [(iid, True)]
    inc = cad.store.incident(iid)
    assert (inc["status"], inc["disposition"], inc["closed_at"]) == ("closed", EXPIRED, clock.t)
    ev = closing(cad, iid)
    assert ev["text"] == "Event closed: Expired at 13:37 (until 13:37)" and ev["operator"] == UNTIL_OPERATOR
    assert cad.store.lists.name("dispositions", EXPIRED) == "Expired"
    assert logs == [f"CAD: {inc['number']}: until 10/07 13:37 passed: closed (Expired)"]
    assert cad.store.incident(other)["status"] == "pending"
    clock.t += 3600
    assert cad.expire_until() == [] and texts(cad, iid).count(ev["text"]) == 1     # once


def test_a_restart_in_between_closes_it_at_the_tickers_start(tmp_path):
    clock = Clock(local(2026, 10, 7, 12, 37))
    cad = make_cad(tmp_path, clock=clock)
    iid = notif(cad, local(2026, 10, 7, 13, 37))
    cad.shutdown()
    clock.t = local(2026, 10, 7, 13, 0)                        # a deploy before it: still open
    cad = make_cad(tmp_path, clock=clock)
    assert cad.store.incident(iid)["status"] == "pending"
    cad.shutdown()
    clock.t = local(2026, 10, 7, 15, 9)                        # down across the Until: the next start closes it
    cad = make_cad(tmp_path, clock=clock)
    try:
        assert cad.store.incident(iid)["status"] == "pending"  # nothing at construction (a takeover writes nothing)
        cad.sources.start(backfill_delay_s=None)
        end = time.monotonic() + 10
        while cad.store.incident(iid)["status"] != "closed" and time.monotonic() < end:
            time.sleep(0.02)
        assert cad.store.incident(iid)["disposition"] == EXPIRED
        assert closing(cad, iid)["text"] == "Event closed: Expired at 15:09 (until 13:37)"
    finally:
        cad.shutdown()


def test_an_until_past_midnight_waits_for_that_day(tmp_path):
    clock = Clock(local(2026, 10, 7, 18, 32))
    cad = make_cad(tmp_path, clock=clock)
    iid = notif(cad, local(2026, 10, 8, 13, 37))               # 13:37 tomorrow, entered in the evening
    for t in (local(2026, 10, 7, 23, 59), local(2026, 10, 8, 0, 1), local(2026, 10, 8, 13, 36)):
        clock.t = t
        assert cad.expire_until() == []
    assert cad.store.incident(iid)["status"] == "pending"
    cad.shutdown()
    clock.t = local(2026, 10, 9, 8, 0)                         # the hub was down past it: the day said too
    cad = make_cad(tmp_path, clock=clock)
    try:
        assert [d["id"] for d in cad.expire_until()] == [iid]
        assert closing(cad, iid)["text"] == "Event closed: Expired at 08:00 (until 10/08 13:37)"
    finally:
        cad.shutdown()


def test_an_until_in_the_past_at_creation_is_refused():
    clock = Clock(local(2026, 10, 7, 18, 32))
    cad = make_cad(clock=clock)
    with pytest.raises(CadError, match="Until is in the past"):
        notif(cad, local(2026, 10, 7, 13, 37))                 # the morning's time on an evening call: a wrong day
    assert cad.store.incidents() == []
    iid = notif(cad, clock.t - 30)                             # the minute being typed: taken, and over at once
    assert [d["id"] for d in cad.expire_until()] == [iid]
    assert cad.create({"type": "STRU", "emergency": True, "until_at": None})["number"]


def test_an_edit_to_a_past_until_is_refused():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    iid = notif(cad, local(2026, 10, 7, 18, 0))
    clock.t = local(2026, 10, 7, 14, 0)
    for t in (local(2026, 10, 7, 11, 0), local(2026, 10, 7, 13, 0)):   # before it began; since, but gone by
        with pytest.raises(CadError, match=r"Until is in the past: check the day \(or Close it\)"):
            cad.update(iid, {"until_at": t, "operator": "JD"})
    assert cad.store.incident(iid)["until_at"] == local(2026, 10, 7, 18, 0)
    assert cad.expire_until() == [] and cad.store.incident(iid)["status"] == "pending"
    cad.update(iid, {"until_at": local(2026, 10, 8, 9, 0), "operator": "JD"})      # a later day: taken
    assert texts(cad, iid)[-1] == "Edited until: 10/08 09:00"
    cad.update(iid, {"until_at": clock.t - 30, "operator": "JD"})                   # the minute being typed
    assert [d["id"] for d in cad.expire_until()] == [iid]


def test_an_old_until_before_creation_never_blocks_other_edits_and_expires():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    iid = notif(cad, None)
    with cad.store.tx() as db:                                 # a row from before the check
        db.execute("UPDATE incidents SET until_at = ? WHERE id = ?", (local(2026, 10, 7, 9, 0), iid))
    # the console sends the Until with every edit: the same one back is no change
    cad.update(iid, {"remarks": "detour posted", "until_at": local(2026, 10, 7, 9, 0), "operator": "JD"})
    assert cad.store.incident(iid)["remarks"] == "detour posted"
    assert [d["id"] for d in cad.expire_until()] == [iid]


def test_a_unit_on_it_keeps_it_open_until_it_clears():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    iid = notif(cad, local(2026, 10, 7, 13, 0), subtype="WATER")
    e60 = uid(cad, "E60")
    cad.attach(iid, {"unit_ids": [e60], "operator": "JD"})
    cad.unit_status(e60, {"status": "OL", "operator": "JD"})
    clock.t = local(2026, 10, 7, 13, 0) + 5
    done = cad.expire_until()
    assert [(d["id"], d["closed"], d["units"]) for d in done] == [(iid, False, ["E60"])]
    assert cad.store.incident(iid)["status"] == "active" and by_name(cad, "E60")["status"] == "OL"
    assert texts(cad, iid)[-1] == "Until 13:00 passed: open while E60 is on it"
    clock.t += 60
    assert cad.expire_until() == [] and texts(cad, iid).count("Until 13:00 passed: open while E60 is on it") == 1
    cad.unit_status(e60, {"status": "AV", "operator": "JD"})   # the last one clears: the next look closes it
    clock.t += 5
    assert [d["closed"] for d in cad.expire_until()] == [True]
    assert cad.store.incident(iid)["disposition"] == EXPIRED and by_name(cad, "E60")["incident_id"] is None


def test_emergency_events_queued_pages_and_a_standby_are_left_alone():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    emerg = cad.create({"type": "STRU", "address": "40 Example Rd"})["id"]
    cad.update(emerg, {"until_at": local(2026, 10, 7, 12, 30), "operator": "JD"})
    _, page = monitor_incident("Port Chester Fire, be advised, hydrant out of service at 10 Example St")
    q = cad.from_monitor(page)["id"]
    with cad.store.tx() as db:
        db.execute("UPDATE incidents SET emergency = 0, until_at = ? WHERE id = ?", (local(2026, 10, 7, 12, 30), q))
    iid = notif(cad, local(2026, 10, 7, 12, 30))
    clock.t = local(2026, 10, 7, 13, 0)
    cad.may_write = lambda: False                              # not the leader: nothing written, nothing raised
    assert cad.expire_until() == [] and cad.store.incident(iid)["status"] == "pending"
    cad.may_write = lambda: True
    assert [d["id"] for d in cad.expire_until()] == [iid]
    assert cad.store.incident(emerg)["status"] == "pending" and cad.store.incident(q)["status"] == "queued"


def test_reopening_an_expired_event_clears_its_passed_until():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    iid = notif(cad, local(2026, 10, 7, 13, 0))
    clock.t = local(2026, 10, 7, 13, 1)
    cad.expire_until()
    clock.t = local(2026, 10, 7, 13, 5)
    inc = cad.reopen(iid, {"operator": "JD"})
    assert (inc["status"], inc["until_at"]) == ("pending", None)
    assert texts(cad, iid)[-1] == "Reopened by JD; was Expired at 13:01; until 13:00 (passed) cleared"
    clock.t += 600
    assert cad.expire_until() == [] and cad.store.incident(iid)["status"] == "pending"


def test_a_held_or_idle_sweep_commits_nothing():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    held = notif(cad, local(2026, 10, 7, 13, 0))
    notif(cad, local(2026, 10, 7, 20, 0))                      # not yet
    cad.attach(held, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    rev = cad.store.rev
    for _ in range(3):                                         # idle: nothing due
        assert cad.expire_until() == []
    assert cad.store.rev == rev
    clock.t = local(2026, 10, 7, 13, 1)
    assert [d["closed"] for d in cad.expire_until()] == [False]    # said once: one commit
    rev = cad.store.rev
    for _ in range(3):                                         # held, already said: reads only
        clock.t += 5
        assert cad.expire_until() == []
    assert cad.store.rev == rev


def test_the_look_uses_its_index_and_a_standby_never_looks(tmp_path):
    cad = make_cad(tmp_path)
    try:
        idx = cad.store.q("SELECT sql FROM sqlite_master WHERE name = 'incidents_until_open'")
        assert idx and "WHERE until_at IS NOT NULL AND status IN ('pending', 'active')" in idx[0]["sql"]
        seen = []
        q = cad.store.q
        cad.store.q = lambda sql, args=(): (seen.append(sql), q(sql, args))[1]
        cad.store.expire_until()
        plan = q("EXPLAIN QUERY PLAN " + seen[0], (1e9,))
        assert any("incidents_until_open" in r["detail"] for r in plan), plan
        seen.clear()
        cad.may_write = lambda: False
        assert cad.store.expire_until() == [] and seen == []
    finally:
        cad.store.q = q
        cad.may_write = lambda: True
        cad.shutdown()


def test_the_lease_lost_part_way_still_logs_what_was_closed():
    clock = Clock(local(2026, 10, 7, 12, 0))
    logs = []
    cad = make_cad(clock=clock)
    cad.log = logs.append
    first, second = notif(cad, local(2026, 10, 7, 12, 30)), notif(cad, local(2026, 10, 7, 12, 40))
    clock.t = local(2026, 10, 7, 13, 0)
    one = cad.store._expire_one

    def then_lost(iid, now):
        out = one(iid, now)
        cad.may_write = lambda: False                          # the lease runs out after the first commit
        return out
    cad.store._expire_one = then_lost
    done = cad.expire_until()
    cad.may_write = lambda: True
    assert [d["id"] for d in done] == [first]
    assert logs == [f"CAD: {cad.store.incident(first)['number']}: until 10/07 12:30 passed: closed (Expired)"]
    assert cad.store.incident(first)["status"] == "closed" and cad.store.incident(second)["status"] == "pending"
    cad.store._expire_one = one                                # the next leader's look
    assert [d["id"] for d in cad.expire_until()] == [second]


def test_an_event_made_non_emergency_drops_a_passed_until():
    clock = Clock(local(2026, 10, 7, 12, 0))
    cad = make_cad(clock=clock)
    a = cad.create({"type": "STRU", "address": "40 Example Rd", "operator": "JD"})["id"]
    b = cad.create({"type": "STRU", "address": "42 Example Rd", "operator": "JD"})["id"]
    c = cad.create({"type": "STRU", "address": "44 Example Rd", "operator": "JD"})["id"]
    for iid in (a, b):
        cad.update(iid, {"until_at": local(2026, 10, 7, 12, 30), "operator": "JD"})
    cad.update(c, {"until_at": local(2026, 10, 7, 18, 0), "operator": "JD"})
    clock.t = local(2026, 10, 7, 13, 0)
    cad.update(a, {"emergency": False, "operator": "JD"})        # unticked
    cad.update(b, {"type": "NOTIF", "operator": "JD"})           # a type that is non-emergency
    cad.update(c, {"emergency": False, "operator": "JD"})        # still to come: kept
    for iid in (a, b):
        inc = cad.store.incident(iid)
        assert (inc["emergency"], inc["until_at"]) == (0, None)
        assert texts(cad, iid)[-1].endswith("now a non-emergency event; until 12:30 (passed) cleared")
    assert cad.store.incident(c)["until_at"] == local(2026, 10, 7, 18, 0)
    assert cad.expire_until() == [] and {cad.store.incident(i)["status"] for i in (a, b, c)} == {"pending"}


def test_expired_is_the_hubs_own_disposition():
    cad = make_cad()
    L = cad.store.lists
    assert "EXPIRED" not in L.disposition_codes and L.name("dispositions", EXPIRED) == "Expired"
    assert next(x for x in L.dispositions if x["code"] == EXPIRED)["system"] is True
    iid = notif(cad, None)
    with pytest.raises(CadError, match="not one of the dispositions"):
        cad.close(iid, {"disposition": EXPIRED, "operator": "JD"})
    js = (STATIC / "cad.js").read_text()
    assert "const live = (x) => !x.retired && !x.system;" in js       # off the Close picker


# -- the console: the day, and the flags ---------------------------------------------------------------------------

UNTIL = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[process.argv.length - 2], "utf8");
const grab = (name) => { const m = src.match(new RegExp("^function " + name + "\\(.*\\) \\{[\\s\\S]*?^\\}", "m"));
                         if (!m) throw new Error("not found: " + name); return m[0]; };
global.cad = { scene: { timezone: "America/New_York" }, skew: 0 };
const fmts = {};
const pad = (n) => String(n).padStart(2, "0");
eval(["fmt", "fmtRecv", "toLocalInput", "isOpen", "untilInfo"].map(grab).join("\n") + "\nglobal.untilInfo = untilInfo;");
const cases = JSON.parse(process.argv[process.argv.length - 1]);
console.log(JSON.stringify(cases.map(([inc, now]) => untilInfo(inc, now))));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_console_says_the_day_and_flags_a_wrong_or_passed_until():
    c, now = local(2026, 10, 7, 12, 37), local(2026, 10, 7, 12, 40)
    base = {"status": "pending", "emergency": False, "created_at": c}
    cases = [
        [dict(base, until_at=local(2026, 10, 7, 13, 37)), now],              # today, the day it began: the time
        [dict(base, until_at=local(2026, 10, 8, 13, 37)), now],              # tomorrow: the day too
        [dict(base, until_at=local(2026, 10, 7, 13, 37), created_at=local(2026, 10, 6, 18, 32)), now],  # began yesterday
        [dict(base, until_at=local(2026, 10, 7, 9, 0)), now],                # before it was created
        [dict(base, until_at=c - 30, status="closed"), now],                  # inside the minute's grace: no flag
        [dict(base, until_at=local(2026, 10, 7, 12, 38)), now],              # passed, still open (a unit on it)
        [dict(base, until_at=local(2026, 10, 7, 12, 38), status="closed"), now],
        [dict(base, until_at=None), now],
    ]
    r = subprocess.run([NODE, "-e", UNTIL, str(STATIC / "cad.js"), json.dumps(cases)], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert [(o or {}).get("text") for o in out] == ["13:37", "Oct 8 13:37", "Oct 7 13:37", "09:00 · before created",
                                                    "12:36", "12:38 · passed", "12:38", None]
    assert [(o or {}).get("warn") for o in out] == [False, False, False, True, False, True, False, None]
    assert out[0]["tip"] == "10/07 13:37:00: closes by itself then (Expired)"
    assert out[3]["tip"].endswith("check the day") and "closes once no unit is on it" in out[5]["tip"]


def test_the_queue_card_and_the_facts_use_it():
    js = (STATIC / "cad.js").read_text()
    assert "const u = untilInfo(inc);" in js and '`Until ${u.text}`' in js
    assert 'setText($("#d-until"), until ? until.text : "");' in js
    assert "untilInfo(i)?.text" in js                          # a card redrawn when its flag changes
    # the edit form's Until only on a non-emergency event, and as the Emergency box is ticked
    assert 'show($("#e-until-w"), !inc.emergency);' in js
    assert '$("#e-emergency").addEventListener("change", () => show($("#e-until-w"), !$("#e-emergency").checked));' in js
    assert '<label id="e-until-w"' in (STATIC / "cad.html").read_text()
