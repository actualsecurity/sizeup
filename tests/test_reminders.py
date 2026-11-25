"""Reminders (sizeup/cad/reminders.py, static/reminders.js): a call left Pending with no unit, a unit left Dispatched,
flash on every console after a set time, by the hub's clock, until the state changes or someone OKs it for everyone.

The request (a department member, through the admin): "if a call sits in pending for longer than 5 minutes it starts
flashing, or if a unit sits DP for longer than 5 minutes it flashes". Invented calls and addresses throughout.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad import api as api_mod
from sizeup.cad import prefs as prefs_mod
from sizeup.cad import reminders as rm
from sizeup.cad import settings as settings_mod
from sizeup.cad.store import CadError
from tests.test_cad import Clock, local, make_cad, uid

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
T0 = local(2026, 11, 20, 10, 0)


@pytest.fixture
def world():
    clock = Clock(T0)
    cad = make_cad(clock=clock, tts_engine="none")
    return cad, clock


def call(cad, address="12 Sample St"):
    return cad.create({"type": "ALARM", "subtype": "CO-RESD", "address": address, "operator": "JD"})["id"]


def items(cad):
    return {i["key"]: i for i in cad.state()["reminders"]["items"]}


def due(cad, now):
    return {k for k, i in items(cad).items() if now >= i["due"]}


# -- the rules -----------------------------------------------------------------------------------------------------

def test_a_pending_call_with_no_unit_is_due_after_five_minutes_and_clears_with_a_unit(world):
    cad, clock = world
    iid = call(cad)
    it = items(cad)[f"pending:{iid}"]
    assert (it["kind"], it["iid"], it["since"], it["due"]) == ("pending", iid, T0, T0 + 300)
    assert due(cad, T0 + 299) == set() and due(cad, T0 + 300) == {f"pending:{iid}"}
    clock.t = T0 + 400
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    assert f"pending:{iid}" not in items(cad)                      # a unit on it: cleared, no OK needed


def test_since_is_the_last_unit_cleared_not_the_accept(world):
    """Active -> Pending when its last unit clears (another branch's transition, or a reopen): counted from then."""
    cad, clock = world
    iid = call(cad)
    clock.t = T0 + 60
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    clock.t = T0 + 900
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})
    cad.store.db.execute("UPDATE incidents SET status = 'pending' WHERE id = ?", (iid,))   # as the transition leaves it
    cad.store.rev += 1
    it = items(cad)[f"pending:{iid}"]
    assert it["since"] == T0 + 900 and it["due"] == T0 + 1200
    assert rm.pending_since({"accepted_at": T0, "created_at": T0, "assignments": [{"cleared_at": T0 + 50}],
                             "events": [{"kind": "reopened", "at": T0 + 70}]}) == T0 + 70


def test_a_non_emergency_or_closed_event_never_reminds(world):
    cad, _ = world
    ne = cad.create({"type": "NOTIF", "address": "3 Example Rd", "emergency": False, "operator": "JD"})["id"]
    iid = call(cad)
    cad.close(iid, {"disposition": "FALSE", "operator": "JD"})
    assert not any(k.endswith(f":{ne}") or k.endswith(f":{iid}") for k in items(cad))


def test_a_unit_dispatched_five_minutes_without_moving_is_due_and_clears_when_it_responds(world):
    cad, clock = world
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60"), uid(cad, "L31")], "operator": "JD"})
    e60, l31 = f"dispatched:{uid(cad, 'E60')}", f"dispatched:{uid(cad, 'L31')}"
    it = items(cad)[e60]
    assert (it["unit"], it["status"], it["iid"], it["since"], it["due"]) == ("E60", "DP", iid, T0, T0 + 300)
    clock.t = T0 + 200
    cad.unit_status(uid(cad, "L31"), {"status": "RS", "operator": "JD"})
    assert l31 not in items(cad) and due(cad, T0 + 301) == {e60}
    clock.t = T0 + 320
    cad.unit_status(uid(cad, "E60"), {"status": "OL", "operator": "JD"})
    assert items(cad) == {}


def test_the_hubs_status_time_counts_not_when_anyone_looked(world):
    """Since is the stored status change: a state built later (a page loaded later) is due at the same moment."""
    cad, clock = world
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    clock.t = T0 + 1000                                            # nobody looked for a while
    cad._state = None
    it = items(cad)[f"dispatched:{uid(cad, 'E60')}"]
    assert it["since"] == T0 and it["due"] == T0 + 300
    assert cad.state()["time"] == T0 + 1000                       # the pages' clock: the hub's, in the same push


def test_units_setting_fire_ems_by_default_third_party_dispatch_other_with_all(world):
    cad, clock = world
    iid = call(cad)
    cad.attach(iid, {"unit_ids": [uid(cad, "77A1"), uid(cad, "PCPD"), uid(cad, "BAT10"), uid(cad, "KNOX_BOX")], "operator": "JD"})
    cad.unit_status(uid(cad, "PCPD"), {"status": "DO", "operator": "JD", "incident_id": iid})
    names = lambda: sorted(i["unit"] for i in items(cad).values() if i["kind"] == "dispatched")   # noqa: E731
    assert names() == ["77A1"]                                     # EMS yes; another agency and a third party not
    cad.settings.post("reminders", {"action": "set", "key": "units", "value": "agencies", "operator": "KM"})
    assert names() == ["77A1", "BAT10"]
    cad.settings.post("reminders", {"action": "set", "key": "units", "value": "all", "operator": "KM"})
    assert names() == ["77A1", "BAT10", "PCPD"]          # a third party in its Dispatch other; the keyholder's Dispatched never
    pc = next(i for i in items(cad).values() if i["unit"] == "PCPD")
    assert pc["status"] == "DO"


def test_department_placeholders_never_remind(world):
    """DPT239 and the like (a county feed's "department dispatched, no unit yet"): Dispatched by design, never a rig."""
    cad, _ = world
    row = {"id": 50, "name": "DPT239", "status": "DP", "status_at": T0, "incident_id": 1, "incident_number": "26-000001",
           "assigned_at": T0, "response": "fire", "kind": "other", "third_party": False}
    cfg = rm.config(None)
    assert [c["unit"] for c in rm.candidates(cfg, [], [dict(row, placeholder=False)], cad.lists)] == ["DPT239"]
    assert rm.candidates(cfg, [], [dict(row, placeholder=True)], cad.lists) == []


def test_the_optional_rules_are_off_by_default_and_work_when_on(world):
    cad, clock = world
    q = cad.store.create_incident(type="ALARM", address="8 Test Ave", status="queued", source="60 Control page")
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    cad.unit_status(uid(cad, "E60"), {"status": "OL", "operator": "JD"})
    ne = cad.create({"type": "NOTIF", "address": "3 Example Rd", "emergency": False, "until_at": T0 + 600, "operator": "JD"})["id"]
    cad.attach(ne, {"unit_ids": [uid(cad, "U65")], "operator": "JD"})
    assert {i["kind"] for i in items(cad).values()} == set()
    for k in ("queued_on", "onscene_on", "until_on"):
        cad.settings.post("reminders", {"action": "set", "key": k, "value": True, "operator": "KM"})
    got = items(cad)
    assert got[f"queued:{q}"]["due"] == T0 + 180
    assert got[f"onscene:{uid(cad, 'E60')}"]["due"] == T0 + 1800
    assert got[f"until:{ne}"]["since"] == T0 + 600 and got[f"until:{ne}"]["due"] == T0 + 600


# -- OK: shared, expiring, audited ---------------------------------------------------------------------------------

def test_an_ok_snoozes_it_for_everyone_expires_and_is_audited(world):
    cad, clock = world
    iid = call(cad)
    key = f"pending:{iid}"
    with pytest.raises(CadError, match="not due"):
        cad.reminders.ack({"key": key, "operator": "JD"})
    clock.t = T0 + 360
    out = cad.reminders.ack({"key": key, "operator": "Casey <casey@example.org>"})
    assert out["until"] == T0 + 660 and out["text"] == "Pending 6 min"
    it = items(cad)[key]                                           # every console's state: quiet until then
    assert it["due"] == T0 + 660 and it["by"].startswith("Casey")
    assert due(cad, T0 + 659) == set() and due(cad, T0 + 660) == {key}       # and back by itself
    ev = cad.store.full(iid)["events"][-1]
    assert (ev["kind"], ev["text"], ev["operator"]) == ("system", "Reminder OK: Pending 6 min · quiet 5 min",
                                                        "Casey <casey@example.org>")
    clock.t = T0 + 370                                             # a second OK meanwhile: the first one stands
    again = cad.reminders.ack({"key": key, "operator": "JD"})
    assert again["already"] and again["by"].startswith("Casey")
    assert sum(e["text"].startswith("Reminder OK") for e in cad.store.full(iid)["events"]) == 1


def test_a_snooze_belongs_to_its_spell(world):
    cad, clock = world
    iid = call(cad)
    clock.t = T0 + 300
    cad.reminders.ack({"key": f"pending:{iid}", "operator": "JD"})
    clock.t = T0 + 320
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    e60 = f"dispatched:{uid(cad, 'E60')}"
    clock.t = T0 + 640
    cad.reminders.ack({"key": e60, "operator": "JD"})
    clock.t = T0 + 700
    cad.unit_status(uid(cad, "E60"), {"status": "AV", "operator": "JD"})                    # cleared ...
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})                    # ... and dispatched again
    it = items(cad)[e60]
    assert it["since"] == T0 + 700 and it["due"] == T0 + 1000 and "by" not in it      # a new spell: not snoozed
    with pytest.raises(CadError, match="cleared"):
        cad.reminders.ack({"key": f"pending:{iid}", "operator": "JD"})


def test_the_ok_minutes_come_from_settings_and_bad_bodies_are_refused(world):
    cad, clock = world
    iid = call(cad)
    cad.settings.post("reminders", {"action": "set", "key": "snooze_min", "value": 10, "operator": "KM"})
    clock.t = T0 + 300
    assert cad.reminders.ack({"key": f"pending:{iid}", "operator": "JD"})["until"] == T0 + 900
    for body, why in (({"key": f"pending:{iid}"}, "operator"), ({"key": "", "operator": "JD"}, "key"),
                      ({"key": "pending:99999", "operator": "JD"}, "cleared")):
        with pytest.raises(CadError, match=why):
            cad.reminders.ack(body)


def test_the_route_is_for_dispatchers_and_admins_and_in_the_activity():
    assert api_mod.needs("/api/cad/reminders/ack") == "dispatch"
    assert api_mod.MAY["viewer"] == ("read",)
    m = api_mod.REMINDER_ACK.fullmatch("/api/cad/reminders/ack")
    said = next(fn for rx, fn in api_mod.DOES if rx is api_mod.REMINDER_ACK)(m, {}, {"text": "E60 DP 6 min",
                                                                                   "incident_number": "26-000001"}, None)
    assert said == ("Reminder OK: E60 DP 6 min", "26-000001")
    assert next(fn for rx, fn in api_mod.DOES if rx is api_mod.REMINDER_ACK)(m, {}, {"already": True, "text": "x"}, None) is None


# -- settings -----------------------------------------------------------------------------------------------------

def test_settings_defaults_edits_resets_and_audit(world):
    cad, _ = world
    page = cad.settings.page()["reminders"]
    rules = {r["rule"]: (r["on"]["value"], r["min"]["value"]) for r in page["rules"]}
    assert rules == {"pending": (True, 5), "dispatched": (True, 5), "queued": (False, 3), "onscene": (False, 30),
                     "until": (False, 0)}
    assert (page["units"]["value"], page["snooze_min"]["value"]) == ("fire_ems", 5)
    cad.settings.post("reminders", {"action": "set", "key": "pending_min", "value": 8, "operator": "KM"})
    cad.settings.post("reminders", {"action": "set", "key": "dispatched_on", "value": False, "operator": "KM"})
    iid = call(cad)
    cad.dispatch(iid, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    cad.store.db.execute("UPDATE incidents SET status = 'pending' WHERE id = ?", (iid,))
    assert not items(cad)                                          # E60 on it: no pending; dispatched off
    page = cad.settings.page()["reminders"]
    assert next(r for r in page["rules"] if r["rule"] == "pending")["min"] == {
        "key": "pending_min", "value": 8, "default": 5, "edited": True, "lo": 1, "hi": 240}
    audit = cad.store.q("SELECT section, key, old, new FROM settings_audit WHERE key LIKE 'reminders/%' ORDER BY id")
    assert [(a["section"], a["key"], a["old"], a["new"]) for a in audit] == [
        ("dispatch", "reminders/pending_min", "5", "8"), ("dispatch", "reminders/dispatched_on", "true", "false")]
    cad.settings.post("reminders", {"action": "set", "key": "pending_min", "value": 8, "operator": "KM"})   # as it is
    assert len(cad.store.q("SELECT 1 FROM settings_audit WHERE key LIKE 'reminders/%'")) == 2
    cad.settings.post("reminders", {"action": "reset_section", "operator": "KM"})
    assert cad.settings.section("reminders") == {}
    for body, why in (({"action": "set", "key": "pending_min", "value": 0}, "1 to 240"),
                      ({"action": "set", "key": "pending_min", "value": 2.5}, "whole"),
                      ({"action": "set", "key": "pending_min", "value": float("inf")}, "whole"),
                      ({"action": "set", "key": "pending_on", "value": "yes"}, "true or false"),
                      ({"action": "set", "key": "units", "value": "pd"}, "fire_ems"),
                      ({"action": "set", "key": "snooze_min", "value": 61}, "1 to 60"),
                      ({"action": "set", "key": "other", "value": 1}, "key"),
                      ({"action": "flip", "key": "pending_on"}, "action")):
        with pytest.raises(CadError, match=why):
            cad.settings.post("reminders", body)
    assert settings_mod.PAGE_SECTION["reminders"] == "dispatch" and "reminders" in settings_mod.__doc__


def test_the_settings_import_checks_the_reminders(world):
    cad, _ = world
    doc = cad.settings.export()
    doc["overrides"]["reminders"] = {"pending_min": 500}
    with pytest.raises(CadError, match="reminders"):
        cad.settings._check_import(doc)
    doc["overrides"]["reminders"] = {"pending_min": 7, "units": "all"}
    cad.settings._check_import(doc)


def test_the_sound_choice_is_a_persons_own_pref():
    assert prefs_mod.validate("reminders", {"sound": True, "board_sound": False}) == '{"board_sound":false,"sound":true}'
    for bad in ({"sound": "on"}, {"volume": 3}, []):
        with pytest.raises(prefs_mod.PrefsError):
            prefs_mod.validate("reminders", bad)


# -- the page -----------------------------------------------------------------------------------------------------

NODE_DUE = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const [state, skew, browserNow] = JSON.parse(process.argv[2]);
Date.now = () => browserNow * 1000;                       // the PC's clock: wrong
global.window = global; global.BOARD = false;
global.cad = { state, skew, who: null };
global.serverNow = () => Date.now() / 1000 + cad.skew;    // cad.js: the hub's clock from the state's time
global.canDo = () => true;
global.document = { addEventListener() {}, querySelectorAll: () => [], getElementById: () => null };
global.fmtTime = () => "10:00:00";
eval(src);
const now = serverNow();
const due = Reminders.dueNow(now);
console.log(JSON.stringify([...due].map(([k, it]) => [k, it.key, Reminders.label(it, now), Reminders.label(it, now, true)])));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("pc_off", [0, 3 * 3600, -3 * 3600])
def test_the_page_times_them_by_the_hubs_clock_whatever_the_pcs_says(pc_off):
    """The state's time (the hub's) makes the skew; the PC's clock hours out changes nothing."""
    hub = T0 + 361
    state = {"time": hub, "reminders": {"snooze_min": 5, "items": [
        {"key": "pending:4", "kind": "pending", "iid": 4, "unit": "", "status": "", "since": T0, "due": T0 + 300},
        {"key": "dispatched:9", "kind": "dispatched", "iid": 4, "unit": "E60", "status": "DP", "since": T0 + 100, "due": T0 + 400},
        {"key": "queued:5", "kind": "queued", "iid": 4, "unit": "", "status": "", "since": T0, "due": T0 + 180}]}}
    browser = hub + pc_off
    r = subprocess.run([NODE, "-e", NODE_DUE, str(STATIC / "reminders.js"), json.dumps([state, hub - browser, browser])],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    # the pending one is due (6 min); the queued one too, but an event shows its most pressing; E60 not yet (since + 5 min)
    assert json.loads(r.stdout) == [["i:4", "pending:4", "Pending 6 min", "Pending 6 min"]]


def test_the_page_marks_every_place_and_never_redraws_for_it():
    js = (STATIC / "cad.js").read_text()
    assert 'li.dataset.remInc = String(inc.id)' in js and 'c.dataset.remUnit = a.name' in js
    assert 'li.dataset.remUnit = u.name' in js and 'li.dataset.remUnit = a.name' in js and "#d-card > .card-head" in js
    assert "if (window.Reminders) Reminders.tick();" in js
    assert 'e.dataset.remUnit = m.unit' in (STATIC / "cadmap.js").read_text()
    assert 'node.dataset.remUnit = u.name' in (STATIC / "board.js").read_text()
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/profile.js"') < html.index('src="/static/reminders.js"') < html.rindex("<!-- /bundle -->")
    rem = (STATIC / "reminders.js").read_text()
    assert rem.startswith("// Reminders") and '"use strict";' in rem
    assert "render()" not in rem and "renderQueue" not in rem               # touches attributes, never redraws


def test_the_flash_is_slow_and_steady_with_reduced_motion():
    css = (STATIC / "cad.css").read_text()
    m = re.findall(r"animation: rempulse (\d+(?:\.\d+)?)s", css)
    assert m and all(float(s) >= 1.0 for s in m)                          # one cycle a second at most (here 0.5 Hz)
    reduce = css[css.index("@media (prefers-reduced-motion: reduce) {\n    [data-rem]"):]
    assert "animation: none !important" in reduce and "background-color" in reduce   # a steady highlight instead
