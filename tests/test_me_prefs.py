"""Each person's own console choices on the hub (#823, sizeup/cad/prefs.py, static/me-prefs.js): the map layers' sets and
the Pre-plan layer follow the signed-in person to any browser. Their own only (never another person's, never by
anything the request names), viewers too, validated shapes of booleans, capped, written without a CAD rev, refused on a
fenced task, kept in the replicated cad.sqlite. The page: localStorage first, the hub's copy wins, changes debounced,
this browser's old choices sent once, failures kept and sent again. Every person and value here is invented."""

import json
import subprocess
import urllib.request

import pytest

from sizeup.api.v1.handler import V1
from sizeup.cad import prefs as prefs_mod
from sizeup.cad.store import Store
from tests.test_access import hub, jwks, jwt, keys, req, verifier  # noqa: F401  (fixtures)
from tests.test_cad import make_cad
from tests.test_cloud import HubRun, hub_cfg
from tests.test_roles import CASEY, DEE, JORDAN, VAL, people
from tests.test_settings import post
from tests.test_unit_follow import NODE, STATIC, needs_node

LAYERS = {"overview": {"cameras": False, "signs": True, "gis:hydrants": True}, "incident": {"airport": True},
          "folds": {"Parcels": False, "Utilities & Hydrants": True}, "hideDown": True}
URL = prefs_mod.PATH


# ---- the store -------------------------------------------------------------------------------------------------------

def test_only_booleans_in_the_known_shapes():
    assert json.loads(prefs_mod.validate("map_layers", LAYERS)) == LAYERS
    assert json.loads(prefs_mod.validate("map_layers", {})) == {}
    assert json.loads(prefs_mod.validate("preplan", {"layer": True})) == {"layer": True}
    for ns, bad in (("map_layers", []), ("map_layers", "on"), ("map_layers", {"overview": {"cameras": 1}}),
                    ("map_layers", {"overview": {"cameras": "true"}}), ("map_layers", {"overview": {"Cameras": True}}),
                    ("map_layers", {"overview": {"x" * 49: True}}), ("map_layers", {"overview": {"a b": True}}),
                    ("map_layers", {"overview": {"cameras": {"nested": True}}}), ("map_layers", {"overview": []}),
                    ("map_layers", {"theme": "dark"}), ("map_layers", {"hideDown": None}),
                    ("map_layers", {"folds": {"": True}}), ("map_layers", {"folds": {"x" * 81: True}}),
                    ("map_layers", {"folds": {"Tab\there": True}}), ("map_layers", {"folds": {" Parcels": True}}),
                    ("preplan", {"layer": "1"}), ("preplan", {"layer": True, "zoom": 14}), ("preplan", True)):
        with pytest.raises(prefs_mod.PrefsError) as e:
            prefs_mod.validate(ns, bad)
        assert e.value.code == 400, bad
    with pytest.raises(prefs_mod.PrefsError) as e:
        prefs_mod.validate("theme", {"dark": True})
    assert e.value.code == 404


def test_caps_per_set_namespace_and_person(tmp_path, monkeypatch):
    many = {f"k{i}": True for i in range(prefs_mod.MAX_KEYS + 1)}
    with pytest.raises(prefs_mod.PrefsError) as e:
        prefs_mod.validate("map_layers", {"overview": many})
    assert e.value.code == 413
    long_keys = {"overview": {f"gis:{'a' * 40}{i:03d}": True for i in range(300)},
                 "incident": {f"gis:{'b' * 40}{i:03d}": True for i in range(300)}}       # ~31 KB: over one namespace's cap
    with pytest.raises(prefs_mod.PrefsError) as e:
        prefs_mod.validate("map_layers", long_keys)
    assert e.value.code == 413
    cad = make_cad(tmp_path)
    ids = people(cad)
    p = cad.prefs
    near = {"overview": {f"gis:{'c' * 40}{i:03d}": True for i in range(250)}}          # under 16 KB alone
    p.put(ids["Casey"], "map_layers", near)
    monkeypatch.setattr(prefs_mod, "MAX_PERSON_BYTES", len(prefs_mod.validate("map_layers", near)) + 10)
    with pytest.raises(prefs_mod.PrefsError) as e:                                      # over the person's total
        p.put(ids["Casey"], "preplan", {"layer": True})
    assert e.value.code == 413
    assert "preplan" not in p.all(ids["Casey"])                                         # nothing written
    p.put(ids["Casey"], "map_layers", near)                                             # its own size not counted twice
    p.put(ids["Casey"], "map_layers", {"hideDown": True})
    p.put(ids["Casey"], "preplan", {"layer": True})
    assert p.all(ids["Casey"]) == {"map_layers": {"hideDown": True}, "preplan": {"layer": True}}


def test_kept_per_operator_replaced_whole_and_no_cad_rev(tmp_path):
    cad = make_cad(tmp_path)
    ids = people(cad)
    p = cad.prefs
    rev, state_rev = cad.store.rev, cad.store.state_rev
    assert p.put(ids["Casey"], "map_layers", LAYERS) == LAYERS
    p.put(ids["Jordan"], "preplan", {"layer": True})
    assert p.all(ids["Casey"]) == {"map_layers": LAYERS}
    assert p.all(ids["Jordan"]) == {"preplan": {"layer": True}}
    assert p.all(ids["Val"]) == {}
    p.put(ids["Casey"], "map_layers", {"hideDown": False})                     # the whole namespace, replaced
    assert p.all(ids["Casey"]) == {"map_layers": {"hideDown": False}}
    # someone's Layers box is no CAD change: no console's state or per-rev cache rebuilt for it
    assert (cad.store.rev, cad.store.state_rev) == (rev, state_rev)
    # the same value again: nothing changed in the file
    before = cad.store.db.total_changes
    p.put(ids["Casey"], "map_layers", {"hideDown": False})
    assert cad.store.db.total_changes == before


def test_in_the_replicated_database_and_found_by_the_next_task(tmp_path):
    """cad.sqlite is what Litestream replicates; a takeover's task opens the promoted copy: the choices are there, under
    the same operator ids. A renamed person or a changed sign-in email keeps theirs (the row's id)."""
    cad = make_cad(tmp_path)
    ids = people(cad)
    cad.prefs.put(ids["Jordan"], "map_layers", LAYERS)
    cad.store.shutdown()
    again = make_cad(tmp_path)
    assert again.prefs.all(again.prefs.operator_id(JORDAN)) == {"map_layers": LAYERS}
    raw = Store(str(tmp_path / "cad.sqlite")).q("SELECT operator_id, ns FROM person_prefs")
    assert raw == [{"operator_id": ids["Jordan"], "ns": "map_layers"}]
    post(again, "users", action="edit", id=ids["Jordan"], email="jordan.new@example.org")
    assert again.prefs.all(again.prefs.operator_id("jordan.new@example.org")) == {"map_layers": LAYERS}
    assert again.prefs.operator_id(JORDAN) is None


def test_a_fenced_task_writes_nothing(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    cad.may_write = lambda: False
    from sizeup.cloud.access import Identity
    code, raw, _, headers = prefs_mod.http_put(cad.prefs, Identity(email=CASEY), URL + "/map_layers",
                                               json.dumps(LAYERS).encode())
    assert code == 503 and headers["Retry-After"]
    cad.may_write = lambda: True
    assert cad.store.q("SELECT * FROM person_prefs") == []
    # and the read still works there (the standby follows the replica)
    code, raw, _, _ = prefs_mod.http_get(cad.prefs, Identity(email=CASEY))
    assert code == 200 and json.loads(raw)["prefs"] == {}


# ---- the endpoints, on a hub behind Access ---------------------------------------------------------------------------

def put(url, body, headers, raw=None, method="PUT"):
    data = raw if raw is not None else json.dumps(body).encode()
    r = urllib.request.Request(url, method=method, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b)
        except ValueError:
            return e.code, b


def as_(key, email):
    return {"Cf-Access-Jwt-Assertion": jwt(key, email=email)}


def test_each_person_reads_and_writes_only_their_own(keys, hub):  # noqa: F811
    cad = hub.app.cad
    ids = people(cad)
    casey, jordan, val, dee = (as_(keys[0], e) for e in (CASEY, JORDAN, VAL, DEE))
    assert req(hub.url + URL)[0] == 401                                            # nobody: Access first
    assert put(hub.url + URL + "/map_layers", LAYERS, {})[0] == 401
    code, out = req(hub.url + URL, headers=casey)
    assert code == 200 and out == {"signed_in": True, "id": ids["Casey"], "prefs": {}}
    code, out = put(hub.url + URL + "/map_layers", LAYERS, {**casey, "X-Sizeup-Person": str(ids["Casey"])})
    assert code == 200 and out == {"ok": True, "prefs": {"map_layers": LAYERS}}
    assert req(hub.url + URL, headers=casey)[1]["prefs"] == {"map_layers": LAYERS}
    assert req(hub.url + URL, headers=jordan)[1]["prefs"] == {}                    # never another person's
    # nothing in the request names whose: a query, a body field or a path for someone else changes nothing
    assert req(hub.url + URL + f"?email={CASEY}&id={ids['Casey']}", headers=jordan)[1]["prefs"] == {}
    assert put(hub.url + URL + f"/map_layers?email={CASEY}", {"hideDown": False}, jordan)[0] == 200
    assert put(hub.url + URL + "/map_layers", {"hideDown": False, "email": CASEY}, jordan)[0] == 400
    assert put(hub.url + URL + f"/{ids['Casey']}/map_layers", {"hideDown": False}, jordan)[0] == 404
    assert cad.prefs.all(ids["Casey"]) == {"map_layers": LAYERS}
    assert cad.prefs.all(ids["Jordan"]) == {"map_layers": {"hideDown": False}}
    # the page's id for Casey, sent by Jordan's sign-in (someone else signed in on that browser since): refused
    code, out = put(hub.url + URL + "/map_layers", {"hideDown": True}, {**jordan, "X-Sizeup-Person": str(ids["Casey"])})
    assert code == 409 and cad.prefs.all(ids["Jordan"]) == {"map_layers": {"hideDown": False}}
    # a viewer and a deactivated user: their own screen, theirs to keep
    assert put(hub.url + URL + "/preplan", {"layer": True}, val)[0] == 200
    assert put(hub.url + URL + "/preplan", {"layer": True}, dee)[0] == 200
    assert cad.prefs.all(ids["Val"]) == {"preplan": {"layer": True}}
    # a new person (not on the list yet): no id until whoami adds them; their first PUT adds them as whoami does
    pat = as_(keys[0], "pat@example.org")
    assert req(hub.url + URL, headers=pat)[1] == {"signed_in": True, "id": None, "prefs": {}}
    assert put(hub.url + URL + "/preplan", {"layer": False}, pat)[0] == 200
    pid = cad.prefs.operator_id("pat@example.org")
    assert req(hub.url + URL, headers=pat)[1] == {"signed_in": True, "id": pid, "prefs": {"preplan": {"layer": False}}}


def test_refused_requests_write_nothing(keys, hub):  # noqa: F811
    cad = hub.app.cad
    people(cad)
    casey = as_(keys[0], CASEY)
    for path, body, code in (("/theme", {"dark": True}, 404), ("/map_layers", {"overview": {"cameras": "yes"}}, 400),
                             ("/map_layers", {"overview": {f"k{i}": True for i in range(500)}}, 413),
                             ("/preplan", [True], 400), ("", {}, 501), ("/", {}, 404)):
        assert put(hub.url + URL + path, body, casey)[0] == code, path
    assert put(hub.url + URL + "/map_layers", None, casey, raw=b"{not json")[0] == 400
    assert put(hub.url + URL + "/map_layers", None, casey, raw=b"{" + b" " * prefs_mod.MAX_BODY + b"}")[0] == 413
    assert put(hub.url + URL + "/map_layers", LAYERS, {**casey, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert put(hub.url + URL + "/map_layers", LAYERS, {**casey, "Content-Type": "text/plain"})[0] == 415
    assert put(hub.url + URL + "/map_layers", LAYERS, {**casey, "Content-Type": "application/jsonx"})[0] == 415
    # a device's token is not for the console (#581), whoever is signed in: not here either
    bearer = {**casey, "Authorization": "Bearer dev-token-made-up"}
    assert req(hub.url + URL, headers=bearer)[0] == 401
    assert put(hub.url + URL + "/map_layers", LAYERS, bearer)[0] == 401
    for method in ("PATCH", "DELETE", "POST"):
        assert put(hub.url + URL + "/map_layers", LAYERS, casey, method=method)[0] in (404, 501), method
    # the hub handing over (not the leader, #501): 503, the page sends it again
    cad.may_write = lambda: False
    try:
        code, _ = put(hub.url + URL + "/map_layers", LAYERS, casey)
    finally:
        cad.may_write = lambda: True
    assert code == 503
    # ... and the lease lost between the check and the write (the guard refuses the INSERT): 503, nothing written
    calls = []
    cad.may_write = lambda: (calls.append(1), len(calls) <= 1)[1]
    try:
        code, _ = put(hub.url + URL + "/map_layers", LAYERS, casey)
    finally:
        cad.may_write = lambda: True
    assert code == 503 and len(calls) > 1
    assert cad.store.q("SELECT * FROM person_prefs") == []
    assert not cad.store.db.in_transaction


def test_nobody_signed_in_keeps_them_in_the_browser(tmp_path):
    """All-in-one / no Access: nobody to keep them for: GET says so, a PUT is refused, nothing written."""
    h = HubRun(hub_cfg(tmp_path))
    try:
        assert h.server.access is None
        code, out = req(h.url + URL)
        assert code == 200 and out == {"signed_in": False, "id": None, "prefs": {}}
        assert put(h.url + URL + "/map_layers", LAYERS, {})[0] == 403
        assert h.app.cad.store.q("SELECT * FROM person_prefs") == []
    finally:
        h.close()


def test_not_in_the_apps_api():
    v1 = V1()
    assert not v1.handles(URL) and not v1.handles(URL + "/map_layers")


# ---- the page (node) -------------------------------------------------------------------------------------------------

HARNESS = r"""
global.window = global;
const input = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const ls = Object.assign({}, input.local || {});
global.localStorage = { getItem: (k) => (k in ls ? ls[k] : null), setItem: (k, v) => { ls[k] = String(v); },
                        removeItem: (k) => { delete ls[k]; } };
let now = 0, seq = 0;
const timers = [];
global.setTimeout = (fn, ms) => { const t = { fn, at: now + (ms || 0), id: ++seq }; timers.push(t); return t.id; };
global.clearTimeout = (id) => { const t = timers.find((x) => x.id === id); if (t) t.dead = true; };
const listeners = {};
global.addEventListener = (k, fn) => { (listeners[k] = listeners[k] || []).push(fn); };
global.fire = (k) => { for (const fn of listeners[k] || []) fn(); };
global.document = { addEventListener() {} };
global.$ = () => null;                               // (preplan.js: cad.js's, no page here)
global.performance = { now: () => now, clearMeasures() {}, measure() {} };
const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await tick(); }
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await settle();
    const due = timers.filter((t) => !t.dead && !t.done && t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = due.at; due.done = true; due.fn();
  }
  now = end;
  await settle();
}
// the hub: GET answers input.get (or a status), PUTs answer the next of input.puts (a status, "throw"), then 200
const calls = [];
let gets = (input.gets || [input.get]).slice(), puts = (input.puts || []).slice();
global.fetch = async (url, opts = {}) => {
  const method = opts.method || "GET";
  calls.push({ url, method, body: opts.body ? JSON.parse(opts.body) : null, person: (opts.headers || {})["X-Sizeup-Person"],
               keepalive: !!opts.keepalive, at: now });
  if (url.startsWith("/api/cad/")) return { ok: false, status: 404, json: async () => ({}) };
  const a = method === "GET" ? (gets.length > 1 ? gets.shift() : gets[0]) : (puts.length ? puts.shift() : 200);
  if (a === "throw") throw new TypeError("Failed to fetch");
  if (a && a.delay !== undefined) {                // an answer that takes a while: after a.delay ms
    await new Promise((r) => setTimeout(r, a.delay));
    const status = a.status || 200;
    return { ok: status >= 200 && status < 300, status, type: "basic", json: async () => a.body || {} };
  }
  if (typeof a === "number") return { ok: a >= 200 && a < 300, status: a, type: "basic", json: async () => ({}) };
  return { ok: true, status: 200, json: async () => a };
};
require(input.me);
for (const f of input.load || []) require(f);
const out = { calls };
(async () => {
  out.before = JSON.parse(JSON.stringify(ls));
  out.mode0 = MePrefs._st.mode;
  if (window.MapLayers) out.prefsAtStart = JSON.parse(JSON.stringify(MapLayers.prefs));
  const prefsObj = window.MapLayers ? MapLayers.prefs : null;
  for (const s of input.steps || []) {
    if (s.wait !== undefined) await advance(s.wait);
    if (s.layer) MapLayers.setLayer(s.layer[0], s.layer[1], s.layer[2]);
    if (s.ns) { window.__vals[s.ns] = s.value; MePrefs.changed(s.ns); }
    if (s.register) {
      window.__vals[s.register] = s.value;
      MePrefs.register(s.register, { get: () => window.__vals[s.register], apply: (v) => { window.__applied.push([s.register, v]); window.__vals[s.register] = v === null ? {} : v; },
                                     hadLocal: !!s.hadLocal });
    }
    if (s.fire) fire(s.fire);
    if (s.mark) out[s.mark] = calls.filter((c) => c.method === "PUT").length;
  }
  await advance(input.end === undefined ? 0 : input.end);
  out.mode = MePrefs._st.mode;
  out.after = ls;
  out.applied = window.__applied;
  out.vals = window.__vals;
  if (window.MapLayers) { out.prefs = MapLayers.prefs; out.sameObject = prefsObj === MapLayers.prefs; }
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""
PRELUDE = "window.__vals = {}; window.__applied = [];\n"


def page(tmp_path, data):
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    pre = tmp_path / "pre.js"
    pre.write_text(PRELUDE)
    h.write_text(HARNESS.replace("require(input.me);", f"require({json.dumps(str(pre))});\nrequire(input.me);"))
    d.write_text(json.dumps({"me": str(STATIC / "me-prefs.js"), **data}))
    r = subprocess.run([NODE, str(h), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def puts(out):
    return [c for c in out["calls"] if c["method"] == "PUT"]


HUB = {"signed_in": True, "id": 7, "prefs": {}}


@needs_node
def test_the_hubs_choices_win_over_this_browsers_copy(tmp_path):
    """localStorage is read first (no flash of the defaults); the hub's copy for the person then wins, written back here."""
    local = {"sizeup.mapLayers": json.dumps({"overview": {"cameras": False}}), "sizeup.prefsOwner": "7"}
    hub_layers = {"overview": {"cameras": True, "signs": True}, "folds": {"Parcels": True}}
    out = page(tmp_path, {"local": local, "get": {**HUB, "prefs": {"map_layers": hub_layers}},
                          "load": [str(STATIC / "maplayers.js")]})
    assert out["mode0"] == "loading" and out["prefsAtStart"]["overview"] == {"cameras": False}
    assert out["prefs"]["overview"] == {"cameras": True, "signs": True} and out["prefs"]["folds"] == {"Parcels": True}
    assert out["sameObject"]                                                 # MapLayers.prefs: still the same object
    assert json.loads(out["after"]["sizeup.mapLayers"])["overview"] == {"cameras": True, "signs": True}
    assert puts(out) == [] and out["mode"] == "hub" and out["after"]["sizeup.prefsOwner"] == "7"
    assert [c["url"] for c in out["calls"] if c["method"] == "GET" and c["url"].startswith("/api/me")] == ["/api/me/prefs"]


@needs_node
def test_this_browsers_choices_are_sent_once_when_the_hub_has_none(tmp_path):
    local = {"sizeup.mapLayers": json.dumps({"overview": {"cameras": False}, "hideDown": True,
                                             "folds": {"Tab\there": True, " Padded": True, "\u200bZero": False}}),
             "sizeup.gisLayers": json.dumps({"hydrants": True, "_open:Parcels": False, "_open:": True}),   # (before #111)
             "sizeup.preplanLayer": "1"}
    out = page(tmp_path, {"local": local, "get": HUB, "load": [str(STATIC / "maplayers.js"), str(STATIC / "preplan.js")]})
    sent = {c["url"]: c for c in puts(out)}
    assert set(sent) == {"/api/me/prefs/map_layers", "/api/me/prefs/preplan"}
    assert sent["/api/me/prefs/map_layers"]["body"] == {
        "overview": {"cameras": False, "gis:hydrants": True}, "incident": {"gis:hydrants": True},
        "folds": {"Parcels": False}, "hideDown": True}
    assert sent["/api/me/prefs/preplan"]["body"] == {"layer": True}
    assert all(c["person"] == "7" for c in sent.values())
    assert out["after"]["sizeup.prefsOwner"] == "7"
    # the same person's own copy (the owner marker theirs): sent too; nothing local: nothing sent
    out = page(tmp_path, {"local": {"sizeup.preplanLayer": "0", "sizeup.prefsOwner": "7"}, "get": HUB,
                          "load": [str(STATIC / "maplayers.js"), str(STATIC / "preplan.js")]})
    assert [(c["url"], c["body"]) for c in puts(out)] == [("/api/me/prefs/preplan", {"layer": False})]


@needs_node
def test_someone_elses_copy_on_a_shared_screen_is_not_inherited(tmp_path):
    local = {"sizeup.mapLayers": json.dumps({"overview": {"cameras": False}}), "sizeup.preplanLayer": "1",
             "sizeup.prefsOwner": "3"}
    out = page(tmp_path, {"local": local, "get": HUB, "load": [str(STATIC / "maplayers.js"), str(STATIC / "preplan.js")]})
    assert puts(out) == []
    assert out["prefs"]["overview"] == {} and out["after"]["sizeup.preplanLayer"] == "0"       # the defaults
    assert out["after"]["sizeup.prefsOwner"] == "7"


@needs_node
def test_changes_go_to_the_hub_a_second_later_as_one_write(tmp_path):
    steps = [{"wait": 0}]
    steps += [{"layer": ["signs", i % 2 == 0, "overview"], "wait": 200} for i in range(5)]
    steps += [{"mark": "early"}]
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": HUB, "load": [str(STATIC / "maplayers.js")],
                          "steps": steps, "end": 1000})
    assert out["early"] == 0                                      # each change pushed it back
    (p,) = puts(out)
    assert p["url"] == "/api/me/prefs/map_layers" and p["body"]["overview"] == {"signs": True}
    assert p["at"] == 1000 + 1000                                 # a second after the last change
    assert json.loads(out["after"]["sizeup.mapLayers"])["overview"] == {"signs": True}     # and here at once


@needs_node
def test_a_failed_write_is_kept_and_sent_again(tmp_path):
    """Signed out (an opaque redirect: status 0), offline (a thrown fetch), the hub handing over (503): kept here, sent
    again with backoff; at once when signed in again (#818's event) or online. Never blocks: the value is here."""
    steps = [{"register": "x", "value": {"a": 1}}, {"wait": 0}, {"ns": "x", "value": {"a": 2}}, {"wait": 1000},
             {"mark": "first"}, {"wait": 5000}, {"mark": "second"}, {"wait": 2000}, {"fire": "sizeup:signed-in"},
             {"wait": 0}, {"mark": "third"}]
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": HUB, "puts": [0, "throw", 503], "steps": steps,
                          "end": 60000})
    sent = puts(out)
    assert out["first"] == 1 and out["second"] == 2 and out["third"] == 3      # 5 s backoff, then the event at once
    assert len(sent) == 4 and all(c["body"] == {"a": 2} for c in sent)         # the fourth: after 503's backoff, ok
    assert out["mode"] == "hub"


@needs_node
def test_a_refused_value_is_not_sent_again_and_someone_else_signed_in_reloads(tmp_path):
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": HUB, "puts": [400],
                          "steps": [{"register": "x", "value": {"a": 1}}, {"wait": 0}, {"ns": "x", "value": {"a": 2}}],
                          "end": 600000})
    assert len(puts(out)) == 1
    # 409: Jordan signed in on Casey's page: Casey's pending change dropped, Jordan's loaded (Casey's copy not inherited)
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"},
                          "gets": [HUB, {"signed_in": True, "id": 9, "prefs": {}}], "puts": [409],
                          "steps": [{"register": "x", "value": {"a": 1}, "hadLocal": True}, {"ns": "x", "value": {"a": 2}}],
                          "end": 600000})
    (p,) = puts(out)
    assert p["body"] == {"a": 2} and p["person"] == "7"
    assert out["applied"] == [["x", None]] and out["after"]["sizeup.prefsOwner"] == "9"


@needs_node
def test_changes_before_the_hub_answers_win_and_nobody_signed_in_stays_local(tmp_path):
    # the GET fails (signed out), a change meanwhile, then it answers: the change is sent, not the hub's older copy
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"},
                          "gets": [0, {**HUB, "prefs": {"x": {"a": 0}}}],
                          "steps": [{"register": "x", "value": {"a": 1}}, {"wait": 0}, {"ns": "x", "value": {"a": 2}},
                                    {"wait": 5000}], "end": 2000})
    assert [c["body"] for c in puts(out)] == [{"a": 2}] and out["applied"] == []
    # nobody signed in (all-in-one): never a PUT
    out = page(tmp_path, {"get": {"signed_in": False, "id": None, "prefs": {}}, "load": [str(STATIC / "maplayers.js")],
                          "steps": [{"wait": 0}, {"layer": ["signs", True, "overview"]}], "end": 600000})
    assert puts(out) == [] and out["mode"] == "local"
    assert json.loads(out["after"]["sizeup.mapLayers"])["overview"] == {"signs": True}
    assert "sizeup.prefsOwner" not in out["after"]


@needs_node
def test_a_click_before_the_hub_answers_is_only_that_click(tmp_path):
    """Never this browser's whole copy over a newer one: another device's change on the hub stays, the click goes on top;
    on a shared screen the last person's set is not carried over by an early click."""
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": {**HUB, "prefs": {"x": {"o": {"a": False, "b": True}}}},
                          "steps": [{"register": "x", "value": {"o": {"a": False, "b": False, "c": True}}},
                                    {"ns": "x", "value": {"o": {"a": True, "b": False, "c": True}}}], "end": 2000})
    assert [c["body"] for c in puts(out)] == [{"o": {"a": True, "b": True}}]
    assert out["applied"] == [["x", {"o": {"a": True, "b": True}}]]
    # someone else's copy here (owner 3), and a click before the hub answers: the click alone, on the defaults
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "3"}, "get": HUB,
                          "steps": [{"register": "x", "value": {"o": {"a": False, "b": False}}, "hadLocal": True},
                                    {"ns": "x", "value": {"o": {"a": True, "b": False}}}], "end": 2000})
    assert [c["body"] for c in puts(out)] == [{"o": {"a": True}}] and out["applied"] == [["x", {"o": {"a": True}}]]
    # the same with maplayers.js: Casey's copy here, Jordan signs in, turns Signs on before the hub answers
    local = {"sizeup.mapLayers": json.dumps({"overview": {"cameras": False}, "hideDown": True}), "sizeup.prefsOwner": "3"}
    out = page(tmp_path, {"local": local, "get": HUB, "load": [str(STATIC / "maplayers.js")],
                          "steps": [{"layer": ["signs", True, "overview"]}], "end": 2000})
    (p,) = puts(out)
    assert p["body"] == out["prefs"] == {"overview": {"signs": True}, "incident": {}, "folds": {}, "hideDown": False}


@needs_node
def test_signed_in_again_as_someone_else(tmp_path):
    """#818's re-sign-in: asked who again first; another person: theirs (Casey's set not inherited); a click during the
    reload after a 409 is the new person's alone."""
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"},
                          "gets": [HUB, {"signed_in": True, "id": 9, "prefs": {}}],
                          "steps": [{"register": "x", "value": {"a": True}}, {"wait": 0}, {"fire": "sizeup:signed-in"},
                                    {"wait": 0}], "end": 600000})
    assert puts(out) == [] and out["applied"] == [["x", None]] and out["after"]["sizeup.prefsOwner"] == "9"
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"},
                          "gets": [HUB, {"delay": 500, "body": {"signed_in": True, "id": 9, "prefs": {}}}], "puts": [409],
                          "steps": [{"register": "x", "value": {"a": True, "b": False}}, {"wait": 0},
                                    {"ns": "x", "value": {"a": False, "b": False}}, {"wait": 1000},
                                    {"ns": "x", "value": {"a": False, "b": True}}], "end": 600000})
    sent = puts(out)
    assert [(c["person"], c["body"]) for c in sent] == [("7", {"a": False, "b": False}), ("9", {"b": True})]


@needs_node
def test_leaving_while_a_write_is_out_still_sends_the_newest(tmp_path):
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": HUB, "puts": [{"delay": 5000, "status": 200}],
                          "steps": [{"register": "x", "value": {"a": 1}}, {"wait": 0}, {"ns": "x", "value": {"a": 2}},
                                    {"wait": 1000}, {"ns": "x", "value": {"a": 3}}, {"fire": "pagehide"}, {"wait": 0}],
                          "end": 0})
    assert [(c["body"], c["keepalive"]) for c in puts(out)] == [({"a": 2}, False), ({"a": 3}, True)]


@needs_node
def test_a_change_in_the_last_second_goes_with_the_page(tmp_path):
    out = page(tmp_path, {"local": {"sizeup.prefsOwner": "7"}, "get": HUB,
                          "steps": [{"register": "x", "value": {"a": 1}}, {"wait": 0}, {"ns": "x", "value": {"a": 2}},
                                    {"wait": 100}, {"fire": "pagehide"}, {"wait": 0}], "end": 0})
    (p,) = puts(out)
    assert p["keepalive"] and p["body"] == {"a": 2} and p["at"] == 100


def test_the_page_loads_it_before_the_map_scripts():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/me-prefs.js"') < html.index('src="/static/maplayers.js"') < html.index(
        'src="/static/preplan.js"')
    js = (STATIC / "me-prefs.js").read_text()
    assert '"/api/me/prefs"' in js and "localStorage" in js
