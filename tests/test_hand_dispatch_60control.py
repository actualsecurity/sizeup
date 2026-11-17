"""In 60 Control dispatching (#311, the default) the county dispatches: no dispatch from this CAD. The page hides the
Dispatch card (run card, Preview, Dispatch, Upgrade) and shows it again in PCFD mode, live; the server refuses a
dispatch or an upgrade (automode.hand_dispatch), failing closed. Everything else that works a call stays. Invented
units, addresses and people only."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.automode import NOT_HERE
from sizeup.cloud.access import Identity
from tests.test_cad import make_cad, uid
from tests.test_fast_dispatch import js_function

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
JORDAN = "jordan@example.org"

gate = pytest.mark.hand_dispatch_gate


@pytest.fixture
def api(tmp_path):
    from tests.test_roles import people
    a = CadApi(make_cad(tmp_path))
    people(a.cad)
    return a


def call(api, path, body=None):
    code, raw, _ = api.post(path, json.dumps(body or {}).encode(), Identity(email=JORDAN))
    return code, json.loads(raw)


def emergency(api, address="12 Example Ave"):
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": address})
    assert code == 200
    return inc["id"]


def dispatches(api, iid):
    return api.cad.store.full(iid)["dispatches"]


@gate
def test_60_control_refuses_a_dispatch_and_an_upgrade_and_pcfd_sends_them(api):
    c = api.cad
    assert c.auto.mode() == "60control"
    iid, e60, e61 = emergency(api), uid(c, "E60"), uid(c, "E61")
    code, out = call(api, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e60]})
    assert (code, out["error"]) == (400, NOT_HERE)
    assert dispatches(api, iid) == [] and not c.store.full(iid)["assignments"] and c.store.unit(e60)["status"] == "AV"
    code, out = call(api, f"/api/cad/incident/{iid}/upgrade", {"info": "smoke showing", "unit_ids": [e61]})
    assert (code, out["error"]) == (400, NOT_HERE)
    code, out = call(api, f"/api/cad/incident/{iid}/upgrade", {"info": "smoke showing", "preview": True})
    assert (code, out["error"]) == (400, NOT_HERE)                  # the upgrade form is the card's: hidden too

    assert call(api, "/api/cad/dispatch-mode", {"mode": "pcfd"})[0] == 200
    code, out = call(api, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [e60]})
    assert code == 200 and out["dispatch_id"] and [d["id"] for d in dispatches(api, iid)] == [out["dispatch_id"]]
    assert c.store.unit(e60)["status"] == "DP"
    code, out = call(api, f"/api/cad/incident/{iid}/upgrade", {"info": "smoke showing", "unit_ids": [e61]})
    assert code == 200 and len(dispatches(api, iid)) == 2

    assert call(api, "/api/cad/dispatch-mode", {"mode": "60control"})[0] == 200    # back: refused again (a stale page)
    code, out = call(api, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [uid(c, "L31")]})
    assert (code, out["error"]) == (400, NOT_HERE) and len(dispatches(api, iid)) == 2


@gate
def test_the_service_itself_refuses_and_a_mode_it_cannot_read_is_60_controls(api):
    from sizeup.cad.store import CadError
    c = api.cad
    iid, e60 = emergency(api), uid(api.cad, "E60")
    with pytest.raises(CadError, match="switch to PCFD"):
        c.dispatch(iid, {"unit_ids": [e60], "operator": "Lt. Test"})
    with c.store.tx() as db:                                        # a value nobody wrote: not PCFD
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) "
                   "VALUES ('dispatch_mode', 'mode', '\"PCFD \"', 0, '')")
    with pytest.raises(CadError, match="switch to PCFD"):
        c.dispatch(iid, {"unit_ids": [e60], "operator": "Lt. Test"})
    assert dispatches(api, iid) == []


@gate
def test_working_the_call_stays_in_60_control(api):
    c = api.cad
    iid, e60, e61 = emergency(api), uid(c, "E60"), uid(c, "E61")
    ok = [(f"/api/cad/incident/{iid}", {"cross_street": "Sample St"}),
          (f"/api/cad/incident/{iid}/note", {"text": "Smoke showing"}),
          (f"/api/cad/incident/{iid}/attach", {"unit_ids": [e60], "status": "RS"}),     # Assigned > Add unit
          (f"/api/cad/unit/{e60}/status", {"status": "OL"}),
          (f"/api/cad/unit/{e61}/status", {"status": "RS", "incident_id": iid}),         # the board's status on a call
          (f"/api/cad/incident/{iid}/recommend", {}),                                    # sends and logs nothing
          (f"/api/cad/incident/{iid}/close", {"disposition": "COMPLETE"})]
    for path, body in ok:
        code, out = call(api, path, body)
        assert code == 200, (path, out)
    assert c.store.incident(iid)["status"] == "closed" and dispatches(api, iid) == []


@gate
def test_60_control_automation_still_puts_the_countys_units_on(tmp_path):
    from tests.test_dispatch_modes import AutoRig
    rig = AutoRig(tmp_path)
    rig.created()                                                   # the feed's report: E60 and L31 dispatched
    assert {a["name"] for a in rig.inc()["assignments"]} == {"E60", "L31"} and rig.inc()["dispatches"] == []


def test_the_suites_other_tests_dispatch_as_pcfd(api):
    """conftest: without the marker a test's dispatch goes out whatever the mode (the default, 60 Control, refuses)."""
    iid = emergency(api)
    code, out = call(api, f"/api/cad/incident/{iid}/dispatch", {"unit_ids": [uid(api.cad, "E60")]})
    assert code == 200 and api.cad.auto.mode() == "60control"


# -- the page ---------------------------------------------------------------------------------------------------------

PAGE = r"""
// The Dispatch card driven as the page drives it: a render per state push, the mode switched between pushes.
const nodes = {};
const $ = (id) => nodes[id] || (nodes[id] = { id, hidden: false, textContent: "", value: "", checked: false, disabled: false });
const show = (n, on) => { n.hidden = !on; };
const posts = [], timers = [];
const api = async (method, path, body) => { posts.push(path); if (refuse && !path.endsWith("/recommend")) throw new Error(refuse); return { units: [], unfilled: [], already: [], card: null, dispatch_id: 1, moved: [] }; };
globalThis.setTimeout = (f) => { timers.push(f); };
const flush = async () => { while (timers.length) await timers.shift()(); };
const isOpen = (i) => i.status === "pending" || i.status === "active";
const inc = { id: 3, status: "active", emergency: true, dispatches: [] };
const cad = { state: null, sel: 3, rec: null, hand: null };
const toasts = [];
const UI = { toast: (m, k) => toasts.push([m, k]) };
let refuse = "";                                                        // the server's answer to a dispatch
const selected = () => inc;
let boxes = 0, lists = 0;
const renderDispatchBox = () => { boxes++; };
const syncList = () => { lists++; };
const flash = () => {}, needOperator = () => "Lt. Test", checkedIds = () => [7], upgradeBody = () => ({});
const busy = async (b, l, fn) => fn();
const showSent = () => {}, showPreviewThenAudio = async (t, path) => api("POST", path), syncFull = () => {};
const placeholderUnit = () => false;
eval(process.argv[2].replace(/^(const|let) /gm, "var "));
const mode = (m) => { cad.state = m ? { dispatch_mode: { mode: m } } : {}; };
const look = () => ({ card: !$("#d-dispatch").hidden, log: !$("#d-dispatches-card").hidden, upgrade: !$("#upgrade-form").hidden });
const press = async () => { posts.length = 0; await dispatch(); await preview(); await upgrade(null, false); await upgrade(null, true); return posts.slice(); };
(async () => {
  const out = {};
  mode("pcfd"); renderDispatchCard(inc, true); await flush();          // a page loaded in PCFD, an incident opened:
  out.loaded = posts.slice();                                           // selectIncident asks for its run card, not this
  cad.hand = null; posts.length = 0;
  mode("60control"); renderDispatchCard(inc, true); await flush();
  out.sixty = { ...look(), posts: posts.slice(), rec: cad.rec, pressed: await press() };
  await recommend(); out.sixtyRec = posts.slice();
  mode("pcfd"); renderDispatchCard(inc, true); await flush();          // the switch, as the next state push brings it
  out.pcfd = { ...look(), posts: posts.slice() };
  renderDispatchCard(inc, true); await flush();
  out.again = posts.slice();                                            // no second recommendation on the next push
  out.pcfdPressed = await press();
  refuse = "60 Control dispatching: switch to PCFD to dispatch";       // switched on the hub, this page not told yet
  await dispatch(); await upgrade(null, false);
  out.toasts = toasts.slice(); refuse = "";
  $("#upgrade-form").hidden = false;                                    // the upgrade form open when it switches back
  mode("60control"); renderDispatchCard(inc, true); await flush();
  out.back = { ...look(), pressed: await press() };
  inc.dispatches = [{ id: 1 }]; renderDispatchCard(inc, true);
  out.logged = look();                                                  // dispatches sent in PCFD mode stay readable
  inc.dispatches = []; mode(null); renderDispatchCard(inc, true);
  out.none = look();                                                    // no mode in the state: hidden (fails closed)
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_hides_the_dispatch_card_in_60_control_and_shows_it_in_pcfd_live(tmp_path):
    js = (STATIC / "cad.js").read_text()
    code = "\n".join(js_function(js, n) for n in ("handDispatch", "renderDispatchCard", "renderDispatches", "recommend",
                                                   "handRefused", "preview", "dispatch", "upgrade"))
    h = tmp_path / "page.js"
    h.write_text(PAGE)
    out = json.loads(subprocess.run([NODE, str(h), code], capture_output=True, text=True, check=True, timeout=30).stdout)
    # 60 Control: no card, no empty log, no run card asked for; Dispatch / Preview / Upgrade send nothing
    assert out["loaded"] == []
    assert out["sixty"] == {"card": False, "log": False, "upgrade": False, "posts": [], "rec": None, "pressed": []}
    assert out["sixtyRec"] == []
    # PCFD: the card and its log, the run card asked for once, at the switch
    assert out["pcfd"] == {"card": True, "log": True, "upgrade": False, "posts": ["/api/cad/incident/3/recommend"]}
    assert out["again"] == ["/api/cad/incident/3/recommend"]
    assert out["pcfdPressed"] == ["/api/cad/incident/3/dispatch", "/api/cad/incident/3/preview",
                                  "/api/cad/incident/3/upgrade", "/api/cad/incident/3/upgrade"]
    # refused by the server (a stale page): said in a toast, since the card hides with the next push
    assert out["toasts"] == [["60 Control dispatching: switch to PCFD to dispatch", "err"]] * 2
    # back to 60 Control: hidden again, the open upgrade form too, nothing sent
    assert out["back"] == {"card": False, "log": False, "upgrade": False, "pressed": []}
    assert out["logged"] == {"card": False, "log": True, "upgrade": False}
    assert out["none"] == {"card": False, "log": False, "upgrade": False}


def test_the_card_is_drawn_on_every_render_and_holds_every_dispatch_control():
    js = (STATIC / "cad.js").read_text()
    assert "renderDispatchCard(inc, open);" in js_function(js, "renderDetail")
    sel = js_function(js, "selectIncident")
    assert sel.index("cad.hand = null;") < sel.index("render();") < sel.index("recommend();")   # one run card asked for
    assert 'show($("#d-dispatch")' not in js_function(js, "renderDetail")
    html = (STATIC / "cad.html").read_text()
    card = html[html.index('<div id="d-dispatch" class="card">'):html.index('<div id="d-dispatches-card"')]
    for ctl in ("r-refresh", "r-add-btn", "r-tickall", "r-joint", "r-preview", "r-dispatch", "u-open", "upgrade-form", "u-send"):
        assert f'id="{ctl}"' in card, ctl                             # inside the card: hidden with it
    for fn in ("dispatch", "preview", "upgrade", "recommend", "renderDispatchBox"):
        assert "handDispatch()" in js_function(js, fn), fn
