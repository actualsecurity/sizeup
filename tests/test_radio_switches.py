"""One set of switches on every talkgroup in Settings > Radios: Record, Transcribe, Alerts, Main.

The admin asked where the Record / Transcribe / Main switches were for another P25 system's talkgroups (MRRS, #409):
the county's had switches, the others a tier dropdown. The stored models are kept as they were: a tier for another
system's talkgroup (sizeup/tiers.py, talkgroups_systems.json), the switches for the county's (talkgroups.json and the
channel choices). The page and the API show both as the same switches, and a switch on another system's talkgroup is
written as the tier it makes (tiers.from_switches):

    tier        Record  Transcribe  Alerts   Activity only
    alert       on      on          on       -
    text        on      on          off      -
    record      on      off         off      -
    off         off     off         off      (encrypted: off)
    activity    off     off         off      on (encrypted only; the switches disabled)

    the chain   Alerts on: Transcribe and Record on; Transcribe on: Record on;
                Record off: Transcribe and Alerts off; Transcribe off: Alerts off
    Main        another system's: never (disabled, "Another system: never Main")
    county      Record / Main / Transcribe as before; no Alerts of its own (`alerts` null, the switch disabled): its
                pre-alerts are the intel packs' (scope by service and source), never a per-talkgroup choice

Every reader of the tier (speech-to-text's class and lane, the retention, the catch-up's order) sees the same tier
whether a switch or the tier itself set it. No radios, no network; every talkgroup here is invented or MRRS's shipped
file.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from sizeup import sourcekeys, tiers
from sizeup.web.server import WebServer
from tests.test_p25_systems import EMS, PD, PD_SECURE, PD_TAC, two  # noqa: F401 (fixture)
from tests.test_radio_emergency import NODE, STATIC

HARRISON_TAC = 9564                         # MRRS's file: record

# ---- the mapping ---------------------------------------------------------------------------------------------------

TABLE = {   # tier -> (record, transcribe, alerts, activity)
    "alert": (True, True, True, False),
    "text": (True, True, False, False),
    "record": (True, False, False, False),
    "off": (False, False, False, False),
    "activity": (False, False, False, True),
}


@pytest.mark.parametrize("tier", tiers.TIERS)
def test_every_tier_is_a_set_of_switches_and_back(tier):
    sw = tiers.switches(tier)
    assert (sw["record"], sw["transcribe"], sw["alerts"], sw["activity"]) == TABLE[tier]
    if tier == "activity":
        # encrypted: the Activity only switch is the tier itself (activity / off); the others never change it
        assert tiers.from_switches("activity", True) == ("activity", None)
        assert tiers.from_switches("activity", True, None, False, False) == ("activity", None)
        return
    # the switches as they are make the same tier again, from any tier the talkgroup could have
    for start in ("alert", "text", "record", "off"):
        got, why = tiers.from_switches(start, False, sw["record"], sw["transcribe"], sw["alerts"])
        assert why is None and got == tier, (start, tier)
    assert tiers.switches("transcribe") == tiers.switches("text") and tiers.switches(None)["record"] is False


@pytest.mark.parametrize("start,given,tier", [
    ("off", {"alerts": True}, "alert"), ("record", {"alerts": True}, "alert"), ("text", {"alerts": True}, "alert"),
    ("off", {"transcribe": True}, "text"), ("record", {"transcribe": True}, "text"), ("alert", {"transcribe": True}, "alert"),
    ("off", {"record": True}, "record"), ("text", {"record": True}, "text"),
    ("alert", {"record": False}, "off"), ("text", {"record": False}, "off"), ("record", {"record": False}, "off"),
    ("alert", {"transcribe": False}, "record"), ("text", {"transcribe": False}, "record"),
    ("alert", {"alerts": False}, "text"), ("text", {"alerts": False}, "text"),
    ("off", {"record": True, "transcribe": True, "alerts": False}, "text"),
    ("alert", {"record": True, "transcribe": False}, "record"),
])
def test_the_chain(start, given, tier):
    assert tiers.from_switches(start, False, **given) == (tier, None)


def test_the_chain_both_ways_at_once_is_refused():
    for given in ({"record": False, "alerts": True}, {"record": False, "transcribe": True},
                  {"transcribe": False, "alerts": True}):
        got, why = tiers.from_switches("text", False, **given)
        assert got is None and "needs" in why


def test_an_encrypted_talkgroup_is_activity_only():
    for given in ({"record": True}, {"transcribe": True}, {"alerts": True}):
        assert tiers.from_switches("activity", True, **given) == (None, tiers.ENCRYPTED)
    assert tiers.from_switches("activity", True, record=False) == ("off", None)
    assert tiers.from_switches("activity", True, alerts=False) == ("activity", None)


# ---- the station: the API and the rows -------------------------------------------------------------------------------


def files(app) -> tuple[dict, dict]:
    d = app.clips_dir.parent
    read = (lambda p: json.loads(p.read_text()) if p.is_file() else {})
    return read(d / "talkgroups.json"), read(d / "talkgroups_systems.json")


def readers(app, tg: int) -> dict:
    """What the tier's readers make of a talkgroup now: its tier, Record, the channel's Transcribe, speech-to-text's
    class, priority and input lane, the tier's retention and its rank in the catch-up / storage (record 0, text 1,
    alert 2: Comms.context_clips)."""
    from sizeup.incidents import Traffic
    key = sourcekeys.p25("mrrs", tg)
    item = Traffic("i", "x", 0, 1, "", talkgroup=tg)
    tier = app.tg.tier(tg, "mrrs")
    return {"tier": tier, "record": app.tg.record(tg, "mrrs"), "main": app.tg.main(tg, "mrrs"),
            "transcribe": app.channel_pref(key)["transcribe"], "class": app._stt_class(item, key),
            "priority": app._stt_priority(item, key), "lane": app._stt_lane(item, key),
            "keep_days": tiers.KEEP_DAYS.get(tier), "rank": {"record": 0, "text": 1, "alert": 2}.get(tier)}


@pytest.mark.parametrize("tier", ["alert", "text", "record", "off"])
def test_a_switch_writes_the_tier_the_readers_already_read(two, tier):
    app, _said, _logs = two
    # by the tier, as before
    assert app.set_talkgroup(HARRISON_TAC, None, None, tier, "mrrs") is None
    before, stored = readers(app, HARRISON_TAC), files(app)
    assert app.set_talkgroup(HARRISON_TAC, None, None, "text" if tier != "text" else "off", "mrrs") is None
    # by the switches: every switch as the tier has it
    r, t, a, _act = TABLE[tier]
    assert app.set_talkgroup(HARRISON_TAC, r, None, None, "mrrs", transcribe=t, alerts=a) is None
    assert readers(app, HARRISON_TAC) == before and files(app) == stored
    assert stored[1][f"p25:mrrs:{HARRISON_TAC}"] == {"tier": tier}
    row = next(x for x in app.talkgroups_state() if x["source"] == f"p25:mrrs:{HARRISON_TAC}")
    assert (row["tier"], row["tier_set"], row["record"], row["transcribe"], row["alerts"], row["main"]) == (
        tier, True, r, t, a, False)


def test_the_switches_round_trip_through_the_station(two):
    app, _said, _logs = two
    row = lambda tg: next(x for x in app.talkgroups_state() if x["source"] == f"p25:mrrs:{tg}")   # noqa: E731
    assert (row(PD)["tier"], row(PD)["alerts"], row(PD_TAC)["alerts"], row(PD_TAC)["transcribe"]) == ("alert", True, False, True)
    assert app.set_talkgroup(PD_TAC, None, None, None, "mrrs", alerts=True) is None and app.tg.tier(PD_TAC, "mrrs") == "alert"
    assert app.set_talkgroup(PD, None, None, None, "mrrs", transcribe=False) is None and app.tg.tier(PD, "mrrs") == "record"
    assert app.set_talkgroup(PD, False, None, None, "mrrs") is None and app.tg.tier(PD, "mrrs") == "off"
    # the page's Record on (Transcribe off with it): Record only, never its file's alert behind the tap
    assert app.set_talkgroup(PD, True, None, None, "mrrs", transcribe=False) is None and app.tg.tier(PD, "mrrs") == "record"
    assert (row(PD)["record"], row(PD)["transcribe"], row(PD)["alerts"]) == (True, False, False)
    assert app.set_talkgroup(PD, False, None, None, "mrrs") is None and app.tg.tier(PD, "mrrs") == "off"
    # Record alone (the API, a whole section), as before: its file's tier when that records (PD dispatch: alert)
    assert app.set_talkgroup(PD, True, None, None, "mrrs") is None and app.tg.tier(PD, "mrrs") == "alert"
    # a tier and the switches at once: refused, nothing changes (never the tier silently over the switches)
    assert app.set_talkgroup(PD, None, None, "off", "mrrs", alerts=True) == tiers.BOTH and app.tg.tier(PD, "mrrs") == "alert"
    # never Main (as before: accepted, nothing changes)
    assert app.set_talkgroup(PD, None, True, None, "mrrs") is None and not row(PD)["main"]
    # encrypted: activity only
    assert row(PD_SECURE)["tier"] == "activity" and not any(row(PD_SECURE)[k] for k in ("record", "transcribe", "alerts"))
    assert app.set_talkgroup(PD_SECURE, None, None, None, "mrrs", transcribe=True) == tiers.ENCRYPTED
    assert app.set_talkgroup(PD_SECURE, None, None, None, "mrrs", alerts=True) == tiers.ENCRYPTED
    assert app.tg.tier(PD_SECURE, "mrrs") == "activity"
    assert "needs" in app.set_talkgroup(EMS, False, None, None, "mrrs", alerts=True) and app.tg.tier(EMS, "mrrs") == "alert"


def test_the_county_is_as_before(two):
    app, _said, _logs = two
    county = lambda: next(x for x in app.talkgroups_state() if x["source"] == "p25:westchester:307")   # noqa: E731
    r = county()
    assert (r["tier_set"], r["tier"], r["record"], r["main"], r["transcribe"], r["alerts"]) == (False, "text", True, True, True, None)
    before = files(app)
    # its Transcribe is its channel choice and it has no Alerts: refused here, nothing written
    assert "channel" in app.set_talkgroup(307, None, None, None, "", transcribe=False)
    assert app.set_talkgroup(307, None, None, None, sourcekeys.COUNTY, alerts=True) == tiers.COUNTY_ALERTS
    assert files(app) == before and county() == r
    # Record / Main as always
    assert app.set_talkgroup(307, False, None) is None and app.set_talkgroup(307, None, False) is None
    assert files(app)[0] == {"307": {"record": False, "main": False}}
    r = county()
    assert (r["tier"], r["record"], r["main"], r["transcribe"], r["alerts"]) == ("off", False, False, True, None)


# ---- the endpoint and the hub ------------------------------------------------------------------------------------------


def test_the_endpoint_sends_the_switches_only_when_given():
    calls = []

    def spy(*a, **k):
        calls.append((a, k))
        return None
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, set_talkgroup=spy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/talkgroup"

    def post(body):
        r = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=10) as res:
                return res.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert post({"source": f"p25:mrrs:{PD}", "alerts": True}) == 200
        assert post({"source": f"p25:mrrs:{PD}", "record": False, "transcribe": False}) == 200
        assert post({"tg": 307, "transcribe": True}) == 200
        assert post({"tg": 307, "main": True}) == 200
        assert post({"source": f"p25:mrrs:{PD}", "alerts": "yes"}) == 400
        assert post({"source": f"p25:mrrs:{PD}", "tier": "off", "alerts": True}) == 400       # one or the other
    finally:
        srv.shutdown()
    assert calls == [((PD, None, None, None, "mrrs"), {"alerts": True}),
                     ((PD, False, None, None, "mrrs"), {"transcribe": False}),
                     ((307, None, None), {"tier": None, "system": "", "transcribe": True}),
                     ((307, None, True), {})]


def test_the_hub_writes_the_switches_as_the_stations_tier(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_channels_by_system import tg_row
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    sent = []
    hub._switch = lambda *a: sent.append(a)
    hub.hub.status = {"id": "st", "p25_systems": []}
    # a station from before the switches: its rows get them from the tier
    hub.hub._take_talkgroups([tg_row(307, "Fire/EMS 15", "Fire/EMS", "westchester", main=True),
                              tg_row(PD_TAC, "PD Tac", "Police", "mrrs", tier="text"),
                              tg_row(PD_SECURE, "PD Secure", "Police", "mrrs", tier="activity", encrypted=True)])
    got = {r["source"]: (r["tier_set"], r["transcribe"], r["alerts"]) for r in hub.hub.talkgroups}
    assert got == {"p25:westchester:307": (False, False, None), f"p25:mrrs:{PD_TAC}": (True, True, False),
                   f"p25:mrrs:{PD_SECURE}": (True, False, False)}
    assert hub.set_talkgroup(PD_TAC, None, None, None, "mrrs", alerts=True) is None
    assert hub.set_talkgroup(PD_TAC, False, None, None, "mrrs", transcribe=False) is None
    assert hub.set_talkgroup(PD_SECURE, None, None, None, "mrrs", transcribe=True) == tiers.ENCRYPTED
    assert hub.set_talkgroup(307, None, None, None, sourcekeys.COUNTY, alerts=True) == tiers.COUNTY_ALERTS
    assert "no talkgroup" in hub.set_talkgroup(4242, None, None, None, "mrrs", alerts=True)
    assert hub.set_talkgroup(PD_TAC, None, None, "off", "mrrs", transcribe=True) == tiers.BOTH
    assert hub.set_talkgroup(PD_TAC, False, None, None, "mrrs") is None             # Record alone: as before
    assert sent == [("set_talkgroup", PD_TAC, None, None, "alert", "mrrs"),
                    ("set_talkgroup", PD_TAC, None, None, "off", "mrrs"),
                    ("set_talkgroup", PD_TAC, False, None, None, "mrrs")]
    # a station that sends them: its own word stays
    hub.hub._take_talkgroups([dict(tg_row(307, "Fire/EMS 15", "Fire/EMS", "westchester"), tier_set=False,
                                   transcribe=True, alerts=None)])
    assert hub.hub.talkgroups[0]["transcribe"] is True


def test_the_activity_line_says_the_switch():
    from sizeup.cad.api import DOES
    said = next(f for rx, f in DOES if rx.pattern == "/api/talkgroup")
    assert said(None, {"source": f"p25:mrrs:{PD}", "alerts": True}, None, None)[0] == f"Talkgroup p25:mrrs:{PD}: alerts on"
    assert said(None, {"tg": 307, "main": False}, None, None)[0] == "Talkgroup 307: main off"
    assert said(None, {"source": f"p25:mrrs:{PD}", "tier": "off"}, None, None)[0] == f"Talkgroup p25:mrrs:{PD}: tier off"


# ---- the page (node) ---------------------------------------------------------------------------------------------------

HARNESS = r"""
const [CADJS, RC, INF] = process.argv.slice(2);
const cadjs = fs.readFileSync(CADJS, "utf8");
eval(cadjs.slice(cadjs.indexOf("function syncList("), cadjs.indexOf("\nfunction flash(")).replace("function syncList(", "global.syncList = function ("));
Object.defineProperty(N.prototype, "options", { get() { return this.children; } });
const _el = global.el;
global.el = (...a) => { const e = _el(...a); e.style.setProperty = () => {}; return e; };
const IN = JSON.parse(fs.readFileSync(INF, "utf8"));
const asked = [];
// the server: what a POST makes of the state the next poll reads (App.set_talkgroup, tiers.from_switches; Record alone
// restores the file's tier, TalkgroupPrefs.set), so a tap the server would turn into more than it showed is caught
const SERVER = JSON.parse(JSON.stringify(IN.state));
const FILE = { "p25:mrrs:9211": "alert", "p25:mrrs:9212": "text", "p25:mrrs:9570": "alert", "p25:mrrs:9564": "record" };
const REC = ["alert", "text", "record"], TX = ["alert", "text"];
function serve(b) {
  const r = SERVER.talkgroups.find((x) => x.source === b.source || (b.tg !== undefined && x.tg === b.tg && x.system === "westchester"));
  if (!r) return;
  if (b.main !== undefined) r.main = b.main;
  if (!r.tier) { if (b.record !== undefined) r.record = b.record; return; }          // the county's
  let t = r.tier;
  if (b.tier) t = b.tier;
  else if (b.transcribe !== undefined || b.alerts !== undefined) {
    let rec = REC.includes(t), tx = TX.includes(t), al = t === "alert";
    if (b.record !== undefined) rec = b.record;
    if (b.transcribe !== undefined) tx = b.transcribe;
    if (b.alerts !== undefined) al = b.alerts;
    if (b.alerts) tx = rec = true;
    if (b.transcribe) rec = true;
    if (b.record === false) tx = al = false;
    if (b.transcribe === false) al = false;
    t = al ? "alert" : tx ? "text" : rec ? "record" : "off";
  } else if (b.record === true) t = REC.includes(FILE[r.source]) ? FILE[r.source] : "record";
  else if (b.record === false) t = "off";
  r.tier = t;
  r.record = REC.includes(t);
}
global.fetch = async (url, o = {}) => {
  asked.push([url, o.body ? JSON.parse(o.body) : null]);
  if (url === "/api/talkgroup") { serve(JSON.parse(o.body)); return { ok: true, status: 200, json: async () => ({ ok: true }) }; }
  return { ok: false, status: 503, headers: { get: () => "" }, json: async () => ({}) };
};
eval(fs.readFileSync(RC, "utf8"));
const tick = () => new Promise((ok) => setImmediate(ok));
(async () => {
  const out = {};
  const box = new N("div");
  RadioControls.mount(box, { reception: true, tab: "channels" });
  await tick(); await tick();
  const st = RadioControls._rx.st;
  st.state = IN.state;
  RadioControls._tg.render();
  const groups = box.querySelector("div.tg-groups");
  const vis = (n) => { while (n) { if (n.hidden) return false; n = n.parentNode; } return true; };
  const row = (src) => groups.querySelectorAll("li.tg-row").find((li) => li.dataset.source === src || li.dataset.nkey === src);
  const sw = (li, f) => li.querySelector(`button[data-field="${f}"]`);
  const look = (li) => Object.fromEntries(["record", "transcribe", "alerts", "main", "activity"].map((f) => {
    const b = sw(li, f);
    return [f, vis(b) ? `${b.classList.contains("on") ? "on" : "off"}${b.disabled ? " disabled" : ""}` : "-"];
  }));
  out.rows = Object.fromEntries(groups.querySelectorAll("li.tg-row").map((li) => [li.dataset.source || li.dataset.nkey, look(li)]));
  out.countyAlertsNa = sw(row("p25:westchester:307"), "alerts").classList.contains("na");
  out.otherAlertsNa = sw(row("p25:mrrs:9212"), "alerts").classList.contains("na");
  out.order = row("p25:mrrs:9212").querySelector("div.tg-sw").children.map((c) => c.dataset.field || c.className);
  out.tierSelects = groups.querySelectorAll("select.tg-tier").length;
  out.titles = { otherMain: sw(row("p25:mrrs:9212"), "main").title, countyAlerts: sw(row("p25:westchester:307"), "alerts").title,
                 sealed: sw(row("p25:mrrs:9213"), "transcribe").title, activity: sw(row("p25:mrrs:9213"), "activity").title,
                 record: sw(row("p25:mrrs:9212"), "record").title };
  // a tap: what is sent and shown at once (`now`, `pending`), then what the next poll shows from the server (`look`)
  const click = async (src, f) => { asked.length = 0; sw(row(src), f).click(); await tick(); await tick();
    const got = { posted: asked.filter(([u]) => u === "/api/talkgroup").map(([, b]) => b),
                  pending: { ...(st.pending.get(src) || { vals: {} }).vals }, now: look(row(src)) };
    st.state = JSON.parse(JSON.stringify(SERVER));                // the next poll (poll(): settle, render)
    RadioControls._tg.settle();
    RadioControls._tg.render();
    return { ...got, look: look(row(src)) }; };
  out.alertsOn = await click("p25:mrrs:9212", "alerts");          // text -> alert
  out.recordOff = await click("p25:mrrs:9211", "record");         // alert -> off: the others off with it
  out.recordOn = await click("p25:mrrs:9211", "record");          // off -> record only (its file says alert)
  await click("p25:mrrs:9211", "record");                         // record -> off again
  out.alertsFromOff = await click("p25:mrrs:9211", "alerts");     // off -> alert: Transcribe and Record with it
  out.transcribeOn = await click("p25:mrrs:9564", "record") && await click("p25:mrrs:9564", "transcribe");
  out.activity = await click("p25:mrrs:9213", "activity");        // activity -> off
  out.sealedRecord = await click("p25:mrrs:9213", "record");      // disabled: nothing sent
  out.otherMain = await click("p25:mrrs:9570", "main");           // disabled: nothing sent
  out.countyAlerts = await click("p25:westchester:307", "alerts");  // disabled: nothing sent
  out.countyMain = await click("p25:westchester:307", "main");    // as before
  // a viewer: every switch locked
  global.canDo = () => false;
  RadioControls._tg.render();
  out.viewer = look(row("p25:mrrs:9570"));
  process.stdout.write(JSON.stringify(out) + "\n", () => process.exit(0));
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
"""


def state():
    from tests.test_channels_by_system import tg_row
    return {
        "talkgroups": [tg_row(307, "Fire/EMS 15", "Fire/EMS", "westchester", main=True, count=4),
                       dict(tg_row(9999, "Made-up Secure", "Fire/EMS", "westchester", encrypted=True), record=False),
                       tg_row(PD, "PD Dispatch", "Police", "mrrs", tier="alert", count=2),
                       tg_row(PD_TAC, "PD Tac", "Police", "mrrs", tier="text"),
                       dict(tg_row(PD_SECURE, "PD Secure", "Police", "mrrs", tier="activity", encrypted=True), record=False),
                       tg_row(EMS, "PC-Rye EMS", "EMS", "mrrs", tier="alert"),
                       dict(tg_row(HARRISON_TAC, "Harrison PD Tac", "Police", "mrrs", tier="off"), record=False)],
        "p25_systems": [{"name": "mrrs", "label": "MRRS", "state": "running", "jobs": [], "title": "", "unknown": []}],
        "channels": {"fm": [{"key": "FG1", "kind": "traffic", "mhz": 453.0375}, {"key": "Tower", "kind": "airband", "mhz": 118.575}],
                     "prefs": {}, "aws_channels": [], "record_only": [], "aws_ok": False, "sync": {"state": "local"}},
    }


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    from tests.test_reception_ui import DOM
    d = tmp_path_factory.mktemp("sw")
    (d / "h.js").write_text(DOM.split("const IN = JSON.parse(")[0] + HARNESS)
    (d / "in.json").write_text(json.dumps({"state": state()}))
    p = subprocess.run([NODE, str(d / "h.js"), str(STATIC / "cad.js"), str(STATIC / "radio-controls.js"), str(d / "in.json")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_every_talkgroup_has_the_same_switches(page):
    rows = page["rows"]
    assert page["tierSelects"] == 0
    assert page["order"] == ["record", "transcribe", "alerts", "main", "activity", "tg-eng sm"]     # the engine: its own slot
    assert rows["p25:mrrs:9211"] == {"record": "on", "transcribe": "on", "alerts": "on", "main": "off disabled", "activity": "-"}
    assert rows["p25:mrrs:9212"] == {"record": "on", "transcribe": "on", "alerts": "off", "main": "off disabled", "activity": "-"}
    assert rows["p25:mrrs:9564"] == {"record": "off", "transcribe": "off", "alerts": "off", "main": "off disabled", "activity": "-"}
    # encrypted on another system: activity only, the rest disabled
    assert rows["p25:mrrs:9213"] == {"record": "off disabled", "transcribe": "off disabled", "alerts": "off disabled",
                                     "main": "off disabled", "activity": "on"}
    # the county's: as before, and Alerts disabled (the intel packs')
    assert rows["p25:westchester:307"] == {"record": "on", "transcribe": "on", "alerts": "off disabled", "main": "on",
                                           "activity": "-"}
    assert rows["p25:westchester:9999"] == {"record": "off", "transcribe": "on disabled", "alerts": "off disabled",
                                            "main": "off", "activity": "-"}
    # FM and airband: Record and Transcribe, as before
    for k in ("fm:FG1", "air:Tower"):
        assert rows[k] == {"record": "on", "transcribe": "on", "alerts": "-", "main": "-", "activity": "-"}, k


def test_the_hovers_say_why(page):
    t = page["titles"]
    assert t["otherMain"] == "Another system: never Main"
    assert t["countyAlerts"] == "County: pre-alerts by the intel packs (Settings > System > Intel)"
    assert t["sealed"] == "Encrypted: never decoded" and t["activity"].startswith("Logged")
    assert t["record"].endswith("Tier: Transcribe")


def test_the_chain_shows_at_once_and_is_sent_as_one_switch(page):
    assert page["alertsOn"]["posted"] == [{"source": "p25:mrrs:9212", "alerts": True}]
    assert page["alertsOn"]["look"]["alerts"] == "on"
    assert page["recordOff"]["posted"] == [{"source": "p25:mrrs:9211", "record": False}]
    assert page["recordOff"]["pending"] == {"record": False, "transcribe": False, "alerts": False}
    assert {k: v for k, v in page["recordOff"]["look"].items() if k != "main"} == {
        "record": "off", "transcribe": "off", "alerts": "off", "activity": "-"}
    # Record on, then Transcribe on: each by itself (Transcribe has Record already)
    assert page["transcribeOn"]["posted"] == [{"source": "p25:mrrs:9564", "transcribe": True}]
    assert page["transcribeOn"]["pending"] == {"transcribe": True}
    assert page["transcribeOn"]["look"] == {"record": "on", "transcribe": "on", "alerts": "off", "main": "off disabled",
                                            "activity": "-"}
    assert page["activity"]["posted"] == [{"source": "p25:mrrs:9213", "tier": "off"}]
    assert page["activity"]["look"]["activity"] == "off"
    for k in ("sealedRecord", "otherMain", "countyAlerts"):
        assert page[k]["posted"] == [], k
    assert page["countyMain"]["posted"] == [{"source": "p25:westchester:307", "main": False}]


def test_record_on_from_off_is_record_only_never_the_files_tier(page):
    # PD Dispatch's file says alert: Record alone would bring Transcribe and Alerts back a poll later (TalkgroupPrefs.set)
    r = page["recordOn"]
    assert r["posted"] == [{"source": "p25:mrrs:9211", "record": True, "transcribe": False}]
    want = {"record": "on", "transcribe": "off", "alerts": "off", "main": "off disabled", "activity": "-"}
    assert r["now"] == want and r["look"] == want          # shown at once, and what the server made of it


def test_alerts_on_from_off_shows_the_chain_at_once(page):
    r = page["alertsFromOff"]
    assert r["posted"] == [{"source": "p25:mrrs:9211", "alerts": True}]
    assert r["pending"] == {"alerts": True, "transcribe": True, "record": True}
    want = {"record": "on", "transcribe": "on", "alerts": "on", "main": "off disabled", "activity": "-"}
    assert r["now"] == want and r["look"] == want


def test_the_countys_alerts_has_no_knob(page):
    assert page["countyAlertsNa"] and not page["otherAlertsNa"]


def test_a_viewer_changes_nothing(page):
    assert all(v.endswith("disabled") for v in page["viewer"].values() if v != "-")
