"""A pre-alert's More info page (sizeup/context_bundle.py, GET /api/cad/prealerts/<kind>/<id>, static/prealert-info.js).

The admin: "I'd also like the pre-alerts to offer a click to more info option, to a page that provides whatever data is
needed, like maybe the audio recordings related?" One bundle per pre-alert (the inference layer reads the same one):
why it fired, the transmissions with their audio (or "audio expired"), the channels around it, the place, site
history, what is open near it now, the outcome.

  the API      signed in only (403 from the CAD API without a person, 401 from the server behind Access); shadow / log
               radio pre-alerts and rule phrases are the admins'; 404 for one that isn't there
  the bundle   both kinds from the real paths (HPN ATC through atc.AtcWatch on the test_atc transcripts, a radio
               pre-alert through the intel engine on test_intel's), the radio log, site history, expired audio
  the page     the cards' More info link and the page itself in node with a minimal DOM; rendering again changes
               only what changed

Synthetic: example.org people, Example / Sample / Test streets, fictional tails."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sizeup import atc, context_bundle
from sizeup.cad.api import CadApi
from sizeup.cad.service import Cad, Settings
from sizeup.cloud.access import Identity
from tests.test_atc import PLANES, T0, Clock
from tests.test_cad import make_cad
from tests.test_intel import Clock as IntelClock
from tests.test_intel import FakeNotifier, ctx_row, example_locate
from tests.test_roles import CASEY, JORDAN, VAL, people

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def get(api, path, who=CASEY):
    code, raw, *_ = api.get(path, "", Identity(email=who) if who else None)
    return code, json.loads(raw) if raw[:1] == b"{" else {}


# -- the airport: HPN ATC on the real watch --------------------------------------------------------------------------

@pytest.fixture
def airport(tmp_path):
    clock = Clock()
    cad = Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=lambda m: None, clock=clock)
    people(cad)
    cad.airport_alerts.notifier = FakeNotifier()
    w = atc.AtcWatch(cad.airport_alerts, ["HPN Tower"], aircraft=lambda: PLANES, log=lambda m: None, clock=clock)
    kept = set()
    cad.has_clip = lambda cid: cid in kept

    def say(n, text, at, record=True):
        clock.t = T0 + at
        t = {"id": f"HPNTower-{n}", "source": "HPN Tower", "start": clock.t - 2, "end": clock.t, "text": text,
             "mhz": 118.575, "status": "done", "model": "whisper"}
        if record:
            cad.comms.record(t)                       # the radio log has it, as the hub's does
        return w.heard(t)
    return cad, say, clock, kept


def test_an_airport_prealert_bundle_has_its_transmissions_audio_place_site_and_outcome(airport):
    cad, say, clock, kept = airport
    # an earlier airport event (its comment and units are site history)
    old = cad.create({"type": "AIRPORT", "operator": "Lt. Example", "remarks": "Alert 2 drill"})
    cad.note(old["id"], {"text": "units staged at the north gate", "operator": "Lt. Example"})
    cad.close(old["id"], {"disposition": "COMPLETE", "operator": "Lt. Example"})
    say(1, "Delta 45, runway one six, line up and wait", -400)               # long before: outside the window
    say(2, "JetBlue 1234, runway one six, cleared for takeoff", 0)
    a = say(3, "Westchester tower, JetBlue 1234, we see what looks like a brush fire off the departure end, maybe a mile", 5)
    b = say(4, "JetBlue 1234, thanks, we'll roll the trucks", 10)
    say(5, "November one two three alfa bravo, contact departure", 60)       # after: in the window, not evidence
    assert a["id"] == b["id"]
    kept.update({"HPNTower-3", "HPNTower-4"})                                 # HPNTower-2's audio has gone (retention)
    api = CadApi(cad)
    code, d = get(api, f"/api/cad/prealerts/airport/{a['id']}", VAL)          # a viewer: anyone signed in
    assert code == 200, d
    assert d["schema"] == context_bundle.SCHEMA_VERSION and d["event"] == {"kind": "airport", "id": a["id"]}
    assert d["url"] == f"/cad#prealert/airport/{a['id']}" and d["cursor"] and d["generated_at"] == clock.t
    assert d["summary"]["title"] == "HPN ATC: possible brush fire reported off runway 16 departure"
    assert d["why"]["matched"] == ["brush fire", "roll the trucks"]
    trig = {t["id"]: t for t in d["trigger"]["items"]}
    assert set(trig) == {"HPNTower-2", "HPNTower-3", "HPNTower-4"}
    assert trig["HPNTower-2"]["context"] and not trig["HPNTower-2"]["has_audio"]          # audio expired
    assert trig["HPNTower-3"]["has_audio"] and trig["HPNTower-3"]["matched"] == ["brush fire"]
    assert trig["HPNTower-3"]["text"] == ("Westchester tower, JetBlue 1234, we see what looks like a brush fire off the "
                                          "departure end, maybe a mile")             # as transcribed (the row's hover)
    assert trig["HPNTower-3"].get("shown", trig["HPNTower-3"]["text"]) == (trig["HPNTower-3"].get("atc_text")
                                                                         or trig["HPNTower-3"]["text"])
    assert trig["HPNTower-3"]["channel"] == "HPN Tower" and trig["HPNTower-3"]["transcript_kept"]
    ba = d["before_after"]
    assert [t["id"] for t in ba["items"]] == ["HPNTower-2", "HPNTower-3", "HPNTower-4", "HPNTower-5"]   # oldest first
    assert [t["evidence"] for t in ba["items"]] == [True, True, True, False] and ba["channels"] == ["HPN Tower"]
    assert d["place"]["point"]["approx"] and "off the departure end" in d["place"]["phrases"]
    calls = d["site_history"]["calls"]["items"]
    assert [c["id"] for c in calls] == [old["id"]] and calls[0]["disposition"] == "COMPLETE"
    assert calls[0]["comments"][0]["text"] == "units staged at the north gate"
    assert d["now"]["aircraft"]["items"][0]["callsign"] == "JBU1234" and d["now"]["aircraft"]["as_recorded"]
    assert d["outcome"]["status"] == "open" and d["outcome"]["incident"] is None
    # dismissed: who and when, and the cursor moves; the same data again: the same cursor
    again = context_bundle.build(cad, "airport", a["id"])
    assert again["cursor"] == context_bundle.build(cad, "airport", a["id"])["cursor"]
    cad.airport_alert(a["id"], "dismiss", {"operator": "Lt. Example", "reason": "controlled burn"})
    after = context_bundle.build(cad, "airport", a["id"])
    assert after["outcome"]["status"] == "dismissed" and after["outcome"]["by"] == "Lt. Example"
    assert after["outcome"]["note"] == "controlled burn" and after["outcome"]["at"] == clock.t
    assert after["cursor"] != again["cursor"]
    json.dumps(after)                                                          # plain, serialisable data


def test_the_airports_approximate_point_is_never_a_building(airport):
    """ATC's point is a guess from the words: no address key, notes, pre-plan points or calls near it as site history,
    only the AIRPORT events (before it); "now" still measures from it."""
    cad, say, clock, kept = airport
    a = say(3, "Westchester tower, JetBlue 1234, we see what looks like a brush fire off the departure end, maybe a mile", 5)
    loc = a["data"]["location"]
    clock.t = T0 - 3600                          # an unrelated call right on the guessed point, with a note, earlier
    near = cad.create({"type": "MVA", "address": "1 Example Rd", "lat": loc["lat"], "lon": loc["lon"], "operator": "Lt. Example"})
    cad.premise.create({"incident_id": near["id"], "text": "Gate code on file", "operator": "Lt. Example"})
    clock.t = T0 + 60
    later = cad.create({"type": "AIRPORT", "operator": "Lt. Example"})                     # after it: not its history
    d = context_bundle.build(cad, "airport", a["id"])
    assert d["place"]["point"]["approx"] and d["place"]["key"] == {} and d["place"]["nearby"] == {}
    assert d["site_history"]["key"] == {} and d["site_history"]["calls"]["items"] == []
    assert d["site_history"]["notes"]["items"] == [] and "approximate" in d["site_history"]["notes"]["empty"]
    assert {c["id"] for c in d["now"]["calls"]["items"]} == {near["id"], later["id"]}    # now: what is open near it


def test_the_window_keeps_the_nearest_each_side_of_the_trigger(airport):
    cad, say, clock, kept = airport
    for i in range(40):                          # 40 before and 40 after, 5 s apart: more than the caps each side
        cad.comms.record({"id": f"HPNTower-b{i}", "source": "HPN Tower", "start": T0 - 5 * (i + 1), "end": T0 - 5 * (i + 1) + 2,
                          "text": f"before {i}", "mhz": 118.575, "status": "done", "model": "whisper"})
        cad.comms.record({"id": f"HPNTower-a{i}", "source": "HPN Tower", "start": T0 + 10 + 5 * (i + 1),
                          "end": T0 + 12 + 5 * (i + 1), "text": f"after {i}", "mhz": 118.575, "status": "done", "model": "whisper"})
    a = say(1, "Westchester tower, JetBlue 1234, we see what looks like a brush fire off the departure end, maybe a mile", 2)
    ba = context_bundle.build(cad, "airport", a["id"])["before_after"]
    ids = [t["id"] for t in ba["items"]]
    assert ids[:30] == [f"HPNTower-b{i}" for i in reversed(range(30))]                    # the nearest 30, oldest first
    assert ids[30] == "HPNTower-1" and ba["items"][30]["evidence"]
    assert ids[31:] == [f"HPNTower-a{i}" for i in range(30)]
    assert ba["capped"] == {"before": True, "after": True}
    assert [t["start"] for t in ba["items"]] == sorted(t["start"] for t in ba["items"])
    # oldest first through comms' own fetch (one context gate), never anything but ASC / DESC in the SQL
    asc = cad.comms._fetch(["t.channel = ?"], ["HPN Tower"], 3, order="ASC")
    assert [r["id"] for r in asc] == ["HPNTower-b39", "HPNTower-b38", "HPNTower-b37"]
    with pytest.raises(ValueError):
        cad.comms._fetch([], [], 1, order="start; DROP TABLE x")


def test_a_transmission_gone_from_the_radio_log_keeps_the_prealerts_own_copy(airport):
    cad, say, clock, kept = airport
    a = say(1, "Westchester tower, November one two three alfa bravo, mayday mayday mayday, engine failure", 0, record=False)
    d = context_bundle.build(cad, "airport", a["id"])
    [t] = d["trigger"]["items"]
    assert not t["transcript_kept"] and "mayday" in t["text"].lower() and not t["has_audio"] and t["kept"]
    assert d["before_after"]["items"] == [] and d["before_after"]["empty"]
    for sec in ("calls", "prealerts"):
        assert d["site_history"][sec]["items"] == [] and d["site_history"][sec]["empty"]


def test_an_unknown_prealert_or_kind_is_a_404(airport):
    cad, *_ = airport
    api = CadApi(cad)
    assert get(api, "/api/cad/prealerts/airport/999")[0] == 404
    assert get(api, "/api/cad/prealerts/intel/999")[0] == 404
    assert get(api, "/api/cad/prealerts/phone/1")[0] == 404
    with pytest.raises(context_bundle.NotFound):
        context_bundle.build(cad, "phone", 1)


# -- the radio pre-alerts: the intel engine ----------------------------------------------------------------------------

@pytest.fixture
def intel(tmp_path):
    clock = IntelClock(T0)
    cad = make_cad(tmp_path, clock=clock)
    people(cad)
    cad.intel.notifier, cad.intel.locate = FakeNotifier(), example_locate
    # an earlier call at that corner, with a comment, a day before
    clock.t = T0 - 86400
    old = cad.create({"type": "MVA", "address": "Sample St & Test Rd", "lat": 41.01, "lon": -73.67, "operator": "Lt. Example"})
    cad.note(old["id"], {"text": "two cars, no injuries", "operator": "Lt. Example"})
    cad.close(old["id"], {"disposition": "COMPLETE", "operator": "Lt. Example"})
    cad.premise.create({"incident_id": old["id"], "text": "Blind corner: stage on Sample St", "operator": "Lt. Example"})
    clock.t = t0 = T0
    cad.comms.record(ctx_row("c0-t0", "unit 3 on patrol", t0 - 120))
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", t0))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    cad.comms.record(ctx_row("c2-t0", "copy, units responding", t0 + 30))
    [ev] = cad.intel.events.open_events()
    clock.t = T0 + 60
    return cad, ev, old


def test_the_api_is_anyone_signed_in_and_fails_closed(intel):
    cad, ev, _old = intel
    api = CadApi(cad)
    for kind, pid in (("intel", ev.id), ("airport", 1)):
        assert get(api, f"/api/cad/prealerts/{kind}/{pid}", None) == (403, {"error": "pre-alert details need a sign-in (Cloudflare Access)"})
    for who in (CASEY, JORDAN, VAL):
        assert get(api, f"/api/cad/prealerts/intel/{ev.id}", who)[0] == 200


def test_the_server_refuses_without_access(intel):
    cad, ev, _old = intel
    from sizeup.web.server import WebServer

    class Access:
        def identify(self, headers):
            e = headers.get("X-Test-User")
            return Identity(email=e) if e else None
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, cad=CadApi(cad), access=Access(),
                    access_log=lambda m: None, role="hub")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/cad/prealerts/intel/{ev.id}"
    try:
        def code(hdr):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        assert code({})[0] == 401
        status, d = code({"X-Test-User": VAL})
        assert status == 200 and d["event"] == {"kind": "intel", "id": ev.id}
    finally:
        srv.shutdown()


def test_an_intel_bundle_has_the_full_transcripts_hits_place_site_history_and_votes(intel):
    cad, ev, old = intel
    cad.has_clip = lambda cid: cid == "c1-t0"
    api = CadApi(cad)
    code, d = get(api, f"/api/cad/prealerts/intel/{ev.id}", JORDAN)
    assert code == 200, d
    assert d["summary"]["category"] == "weapons" and d["why"]["rules"][0]["id"] == "pd.shots"
    [t] = d["trigger"]["items"]
    assert t["text"] == "shots fired at Sample St and Test Rd"                 # the whole transcript (not scrubbed)
    assert t["has_audio"] and t["rules"][0]["id"] == "pd.shots"
    assert "matched" not in t and d["why"]["phrases_hidden"]                   # rule phrases: admins only
    ids = [x["id"] for x in d["before_after"]["items"]]
    assert ids == ["c0-t0", "c1-t0", "c2-t0"] and [x["has_audio"] for x in d["before_after"]["items"]] == [False, True, False]
    assert d["place"]["label"] == "Sample St & Test Rd, Rye" and d["place"]["point"]["lat"] == 41.01
    calls = d["site_history"]["calls"]["items"]
    assert [c["id"] for c in calls] == [old["id"]] and calls[0]["comments"][0]["text"] == "two cars, no injuries"
    assert calls[0]["disposition"] == "COMPLETE" and calls[0]["type"] == "MVA"
    assert [n["text"] for n in d["site_history"]["notes"]["items"]] == ["Blind corner: stage on Sample St"]
    for k in ("caller_name", "phone", "caller_address"):
        assert k not in json.dumps(calls)
    # an admin sees the rule's phrases that matched
    code, a = get(api, f"/api/cad/prealerts/intel/{ev.id}", CASEY)
    assert a["trigger"]["items"][0]["matched"] and not a["why"]["phrases_hidden"]
    # a vote and a dismissal: the outcome
    cad.intel_admin.feedback(ev.id, {"verdict": "useful", "operator": "Val Example"})
    cad.intel_admin.dismiss(ev.id, {"operator": "Jordan Example"})
    o = get(api, f"/api/cad/prealerts/intel/{ev.id}", VAL)[1]["outcome"]
    assert o["status"] == "dismissed" and o["by"] == "Jordan Example"
    assert [f["verdict"] for f in o["feedback"]] == ["noise", "useful"] and all("operator" not in f for f in o["feedback"])
    o = get(api, f"/api/cad/prealerts/intel/{ev.id}", CASEY)[1]["outcome"]      # who voted: admins
    assert [(f["verdict"], f["operator"]) for f in o["feedback"]] == [("noise", "Jordan Example"), ("useful", "Val Example")]


def test_shadow_and_log_radio_prealerts_are_the_admins(intel):
    cad, ev, _old = intel
    with cad.store.tx() as db:
        db.execute("UPDATE intel_events SET mode = 'shadow' WHERE id = ?", (ev.id,))
    api = CadApi(cad)
    assert get(api, f"/api/cad/prealerts/intel/{ev.id}", VAL)[0] == 404
    assert get(api, f"/api/cad/prealerts/intel/{ev.id}", JORDAN)[0] == 404
    assert get(api, f"/api/cad/prealerts/intel/{ev.id}", CASEY)[0] == 200


def test_the_page_url_for_the_push_is_only_the_kind_and_the_id():
    assert context_bundle.page_url("https://cad.example.org/", "intel", 12) == "https://cad.example.org/cad#prealert/intel/12"
    assert context_bundle.page_url("", "airport", 3) == ""
    with pytest.raises(ValueError):
        context_bundle.page_path("phone", 1)


# -- the page, in node -------------------------------------------------------------------------------------------------

DOM = r"""
const fs = require("fs");
global.window = global;
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
class CL {
  constructor(e) { this.e = e; }
  _s() { return new Set((this.e.className || "").split(/\s+/).filter(Boolean)); }
  add(c) { const s = this._s(); s.add(c); this.e.className = [...s].join(" "); }
  remove(c) { const s = this._s(); s.delete(c); this.e.className = [...s].join(" "); }
  contains(c) { return this._s().has(c); }
  toggle(c, on) { if (on === undefined ? !this.contains(c) : on) this.add(c); else this.remove(c); }
}
class El {
  constructor(tag, id) { this.tagName = tag.toUpperCase(); this.id = id || ""; this.children = []; this.dataset = {}; this.on = {};
                         this.attrs = {}; this.className = ""; this._t = ""; this.hidden = false; this.title = ""; this.href = "";
                         this.classList = new CL(this); }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  append(...xs) { for (const x of xs) this.children.push(x); }
  prepend(...xs) { this.children.unshift(...xs); }
  replaceChildren(...xs) { this.children = [...xs]; this._t = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  all() { const r = [this]; for (const c of this.children) if (c && c.all) r.push(...c.all()); return r; }
  find(cls) { return this.all().filter((e) => e.classList.contains(cls)); }
  tag(t) { return this.all().filter((e) => e.tagName === t.toUpperCase()); }
}
const ids = {};
global.document = { createElement: (t) => new El(t), getElementById: (id) => ids[id] || null };
global.$ = (sel) => { const id = sel.slice(1); return ids[id] || (ids[id] = new El("div", id)); };
global.el = (tag, cls, text) => { const n = new El(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = text; return n; };
global.show = (n, on) => { n.hidden = !on; };
global.UI = { toast() {} };
global.fmtTime = (u) => `T${Math.round(u)}`;
global.cad = { scene: null };
// the CAD's row (cad.js radioRow), as far as the page relies on it: the play button or "Audio expired", the text
global.radioRow = (t, ctx) => { const li = el("li", "rt"); li.dataset.scope = (ctx || {}).scope; const b = el("button", "play", t.has_audio === false ? "–" : "▶");
  b.title = t.has_audio === false ? "Audio expired" : "Play"; li.append(b, el("div", "txt", t.text)); return li; };
"""


def run_node(script: str, data: dict) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.json"
        inp.write_text(json.dumps(data))
        js = Path(td) / "t.js"
        js.write_text(DOM + script)
        p = subprocess.run([NODE, str(js), str(inp)], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout.strip().splitlines()[-1])


@needs_node
def test_the_cards_have_more_info_for_every_viewer_but_not_on_the_wall_board():
    script = f"""
{(STATIC / "airport-alerts.js").read_text()}
{(STATIC / "intel.js").read_text()}
{(STATIC / "prealert-info.js").read_text()}
const out = {{}};
const deps = (ro, more) => ({{ api: async () => ({{}}), operator: () => "", time: (t) => String(t), readonly: ro, more }});
const links = (list) => list.all().filter((e) => e.tagName === "A" && e.classList.contains("aa-more")).map((a) => [a.href, a.textContent]);
for (const [name, ro, more] of [["console", false, true], ["viewer", true, true], ["board", true, false]]) {{
  const la = new El("ol"), li = new El("ol");
  AirportAlerts.column(la, {{ open: [IN.airport] }}, deps(ro, more));
  IntelCard.column(li, {{ open: [IN.intel] }}, deps(ro, more));
  out[name] = {{ airport: links(la), intel: links(li) }};
}}
console.log(JSON.stringify(out));
"""
    airport = {"id": 7, "title": "HPN ATC: possible brush fire", "level": "high", "confidence": 0.9, "source": "atc",
               "source_name": "HPN ATC", "channel": "HPN Tower", "at": 1, "count": 1, "data": {"snippets": []}}
    intel = {"id": 12, "level": "medium", "what": 0.75, "where": 0.9, "precision": "intersection", "rules": [["pd.shots", 1, 0.75]],
             "pack_label": "PD", "label": "Weapon / shots", "town": "Rye", "place": "Sample St & Test Rd, Rye", "confirmed": True,
             "first_at": 1, "traffic_ids": ["c1-t0"]}
    out = run_node(script, {"airport": airport, "intel": intel})
    for who in ("console", "viewer"):
        assert out[who] == {"airport": [["#prealert/airport/7", "More info"]], "intel": [["#prealert/intel/12", "More info"]]}, who
    assert out["board"] == {"airport": [], "intel": []}


def bundle_for_page(intel_fixture):
    from sizeup import sourcekeys
    cad, ev, _old = intel_fixture
    cad.has_clip = lambda cid: cid == "c1-t0"
    with sourcekeys.request_context(Identity(email=CASEY)):          # a signed-in reader, as the API's
        return context_bundle.build(cad, "intel", ev.id, admin=True)


def test_build_fails_closed_for_a_caller_that_set_no_reader(intel):
    """build() called directly (the push, inference) with no reader who may see another system's traffic: no radio
    pre-alert at all, never one with the words left out silently."""
    from sizeup import sourcekeys
    cad, ev, _old = intel
    with pytest.raises(context_bundle.NotFound):
        context_bundle.build(cad, "intel", ev.id)
    with sourcekeys.request_context(None), pytest.raises(context_bundle.NotFound):
        context_bundle.build(cad, "intel", ev.id, admin=True)
    with sourcekeys.request_context(Identity(email=VAL)):
        assert context_bundle.build(cad, "intel", ev.id)["trigger"]["items"][0]["text"].startswith("shots fired")


def signed(cad, kind, pid, admin=False):
    from sizeup import sourcekeys
    with sourcekeys.request_context(Identity(email=CASEY if admin else VAL)):
        return context_bundle.build(cad, kind, pid, admin=admin)


def test_non_admins_see_only_the_rules_the_event_fired_on_or_that_map_or_push(intel):
    """A transmission's hits include rules being tuned (shadow, below threshold, an admin's local.* ones): an admin
    sees them all, anyone else only the ones the event fired on or that map or push now."""
    cad, ev, _old = intel
    fired = {r[0] for r in ev.rules}
    with cad.store.tx() as db:
        for rid in ("local.secret_words", "pd.retired_rule"):
            db.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, event_id, at) "
                       "VALUES ('c1-t0', ?, 1, 0.3, 'weapons', 'police', NULL, ?)", (rid, cad.clock()))
    shadowed = next(r for r in cad.intel.pack("police").rules if r.id not in fired)
    shadowed.mode = "shadow"
    with cad.store.tx() as db:
        db.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, event_id, at) "
                   "VALUES ('c1-t0', ?, 1, 0.2, 'weapons', 'police', NULL, ?)", (shadowed.id, cad.clock()))
    seen = {h["id"] for h in signed(cad, "intel", ev.id)["trigger"]["items"][0]["rules"]}
    assert seen == fired
    every = {h["id"] for h in signed(cad, "intel", ev.id, admin=True)["trigger"]["items"][0]["rules"]}
    assert every == fired | {"local.secret_words", "pd.retired_rule", shadowed.id}


def test_a_category_in_shadow_hides_its_rules_from_non_admins(intel):
    """The rung a hit's rule really has is the engine's: the lowest of the pack's, its category's and its own mode."""
    cad, ev, _old = intel
    pack = cad.intel.pack("police")
    fired = {r[0] for r in ev.rules}
    other = next(r for r in pack.rules if r.id not in fired and r.enabled and r.mode in ("map", "push")
                 and not r.id.startswith("local.") and pack.category(r.category).get("mode", "push") in ("map", "push"))
    with cad.store.tx() as db:
        db.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, event_id, at) "
                   "VALUES ('c1-t0', ?, 1, 0.2, ?, 'police', NULL, ?)", (other.id, other.category, cad.clock()))
    seen = lambda: {h["id"] for h in signed(cad, "intel", ev.id)["trigger"]["items"][0]["rules"]}     # noqa: E731
    assert other.id in seen()                                     # it maps or pushes: anyone sees it
    pack.categories[other.category]["mode"] = "shadow"            # its category being tuned
    assert other.id not in seen() and seen() >= fired
    assert other.id in {h["id"] for h in signed(cad, "intel", ev.id, admin=True)["trigger"]["items"][0]["rules"]}


@pytest.mark.parametrize("precision, confirmed, where, alts, site, unconfirmed", [
    ("intersection", True, 0.9, [], True, False),
    ("address", False, 0.8, [], True, True),            # "respond to 120 Example Ave": sure, the only reading
    ("landmark", False, 0.75, [], True, True),
    ("address", False, 0.7, [], False, False),          # not sure enough
    ("address", False, 0.9, [{"label": "120 Example Ave, Rye", "lat": 41.0, "lon": -73.68}], False, False),   # two readings
    ("block", True, 0.9, [], False, False),             # a stretch of street, never a building
    ("street", True, 0.9, [], False, False),
])
def test_a_site_is_a_confirmed_place_or_a_sure_unconfirmed_one(intel, precision, confirmed, where, alts, site, unconfirmed):
    cad, ev, old = intel
    with cad.store.tx() as db:
        db.execute("UPDATE intel_events SET precision = ?, confirmed = ?, where_score = ?, alts = ? WHERE id = ?",
                   (precision, int(confirmed), where, json.dumps(alts), ev.id))
    d = signed(cad, "intel", ev.id)
    sh = d["site_history"]
    if site:
        assert [c["id"] for c in sh["calls"]["items"]] == [old["id"]] and sh["notes"]["items"]
        assert sh["unconfirmed"] is unconfirmed and d["place"]["site_unconfirmed"] is unconfirmed
    else:
        assert sh["calls"]["items"] == [] and sh["notes"]["items"] == [] and sh["key"] == {} and not sh["unconfirmed"]
        assert sh["calls"]["empty"]


def test_recomputed_phrases_say_they_are_the_current_rules_and_flag_a_change(intel):
    cad, ev, _old = intel
    w = signed(cad, "intel", ev.id, admin=True)["why"]
    assert w["matched_from"] == "current rules" and w["rules_changed"] is False and w["matched"]
    with cad.store.tx() as db:
        db.execute("UPDATE intel_events SET ruleset_hash = 'feedfacecafe' WHERE id = ?", (ev.id,))
    assert signed(cad, "intel", ev.id, admin=True)["why"]["rules_changed"] is True


def test_earlier_lists_are_strictly_before_the_prealert(intel):
    """Site history and the earlier pre-alerts are the ones before this pre-alert, not before the page is read; an
    earlier shadow one only for an admin."""
    from sizeup.intel.model import Event
    cad, ev, old = intel
    later = cad.create({"type": "MVA", "address": "Sample St & Test Rd", "lat": 41.01, "lon": -73.67, "operator": "Lt. Example"})
    place = dict(pack="police", category="weapons", label_full="Sample St & Test Rd, Rye", lat=41.01, lon=-73.67,
                 precision="intersection", confirmed=True, status="ended", level="medium")
    before = cad.intel.events.save(Event(**place, first_at=ev.first_at - 3600, last_at=ev.first_at - 3600, mode="push",
                                         updated_at=ev.first_at - 3600))
    tuned = cad.intel.events.save(Event(**place, first_at=ev.first_at - 1800, last_at=ev.first_at - 1800, mode="shadow",
                                        updated_at=ev.first_at - 1800))
    after = cad.intel.events.save(Event(**place, first_at=ev.first_at + 30, last_at=ev.first_at + 30, mode="push",
                                        updated_at=ev.first_at + 30))
    d = signed(cad, "intel", ev.id)
    assert [c["id"] for c in d["site_history"]["calls"]["items"]] == [old["id"]] and later["id"] != old["id"]
    assert [p["id"] for p in d["site_history"]["prealerts"]["items"]] == [before.id]
    a = signed(cad, "intel", ev.id, admin=True)
    assert [p["id"] for p in a["site_history"]["prealerts"]["items"]] == [tuned.id, before.id] and after.id not in {
        p["id"] for p in a["site_history"]["prealerts"]["items"]}


@needs_node
def test_the_page_renders_the_bundle_and_again_changes_only_what_changed(intel):
    d = bundle_for_page(intel)
    script = f"""
{(STATIC / "prealert-info.js").read_text()}
global.api = async () => IN;
(async () => {{
  await PrealertInfo.open("intel/{d['event']['id']}");
  const S = ["pi-why", "pi-trigger", "pi-around", "pi-site", "pi-place", "pi-now", "pi-outcome"];
  const text = Object.fromEntries(S.map((id) => [id, $("#" + id).textContent]));
  const shown = Object.fromEntries(S.map((id) => [id, !$("#" + id).hidden]));
  const plays = $("#pi-around").all().filter((e) => e.tagName === "BUTTON" && e.classList.contains("play")).map((b) => b.title);
  const ev = $("#pi-around").find("pi-ev").length;
  const scopes = ["pi-trigger", "pi-around"].map((id) => [...new Set($("#" + id).find("rt").map((li) => li.dataset.scope))]);
  const before = Object.fromEntries(S.map((id) => [id, $("#" + id).children[0]]));
  PrealertInfo.render(JSON.parse(JSON.stringify(IN)));                    // the same again (a poll): nothing redrawn
  const same = S.filter((id) => $("#" + id).children[0] === before[id]);
  const changed = JSON.parse(JSON.stringify(IN));
  changed.outcome.status = "dismissed"; changed.outcome.by = "Jordan Example"; changed.now.as_of += 60;
  PrealertInfo.render(changed);
  const redrawn = S.filter((id) => $("#" + id).children[0] !== before[id]);
  const unsure = JSON.parse(JSON.stringify(changed)); unsure.site_history.unconfirmed = true;
  PrealertInfo.render(unsure);
  const siteHead = $("#pi-site").children[0];
  const unconfirmed = [siteHead.textContent, siteHead.children[0].title];
  console.log(JSON.stringify({{ text, shown, plays, ev, scopes, same, redrawn, unconfirmed, title: $("#pi-title").textContent,
                               outcome: $("#pi-outcome").textContent, bad: PrealertInfo.parse("phone/1"), ok: PrealertInfo.parse("intel/12"),
                               href: PrealertInfo.href("airport", 3) }}));
}})();
"""
    out = run_node(script, d)
    assert all(out["shown"].values()), out["shown"]
    assert out["title"] == "PD · Weapon / shots · Rye"
    assert "shots fired at Sample St and Test Rd" in out["text"]["pi-trigger"]           # the full transcript
    assert "copy, units responding" in out["text"]["pi-around"]
    assert out["plays"] == ["Audio expired", "Play", "Audio expired"] and out["ev"] == 1
    assert out["scopes"] == [["pi-heard"], ["pi-around"]]                                  # a menu each (txMenu's key)
    assert "two cars, no injuries" in out["text"]["pi-site"] and "COMPLETE" in out["text"]["pi-site"]
    assert "Sample St & Test Rd, Rye" in out["text"]["pi-place"]
    assert "pd.shots" in out["text"]["pi-why"] and "Matched" in out["text"]["pi-why"]
    assert len(out["same"]) == 7 and out["redrawn"] == ["pi-outcome"]                  # idempotent; "now"'s clock ignored
    assert "Dismissed · Jordan Example" in out["outcome"]
    assert "unconfirmed place" in out["unconfirmed"][0] and "Unconfirmed place" in out["unconfirmed"][1]
    assert out["bad"] is None and out["ok"] == {"kind": "intel", "id": 12} and out["href"] == "#prealert/airport/3"
