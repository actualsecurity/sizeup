"""The incident's Size-Up view (GitHub #797, static/sizeup-view.js): Details · Size-Up in the incident's head.

    Details     today's view, unchanged; every selectIncident() opens it
    Size-Up     a two-column grid (#d-size) of the detail's own nodes, moved into slots and back, never copied
    moving      a comment left where each node was; back in reverse order, so every node is home exactly
    empty       a slot with nothing shown says so (.sz-none), kept up as modules flip `hidden`
    narrow      one column in the #797 order: Incident, Address notes, Map, Hazards, Pre-plan, History
    History     folded each time Size-Up opens, not saved (Details keeps its saved state)
The real sizeup-view.js and addr-history.js run in node on a fake document built from cad.html's own #d-body; the
layout is checked in a browser (the PR's screenshots)."""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
SLOTS_LEFT = ["sz-incident", "sz-preplan", "sz-hazards"]
SLOTS_RIGHT = ["sz-building", "sz-map", "sz-notes", "sz-street", "sz-history"]
PLACEHOLDERS = ["sz-building", "sz-street"]                  # later parts of #797 fill these: hidden till then
PHONE_ORDER = ["sz-incident", "sz-notes", "sz-map", "sz-hazards", "sz-preplan", "sz-history"]
MOVED = {
    "sz-incident": ["d-info", "card", "d-cmts"],             # the info column, the Assigned card, the comments
    "sz-preplan": ["d-preplan-form", "d-preplan", "d-docs-form", "d-docs"],
    "sz-hazards": ["d-roads", "d-trains", "d-vessels", "d-cams", "d-tides", "d-airport"],
    "sz-map": ["d-mapbox"],
    "sz-notes": ["d-premise", "d-premise-form"],
    "sz-history": ["d-here"],
}


def text(name):
    return (STATIC / name).read_text()


def plan():
    """[[what, slot]] out of sizeup-view.js's PLAN."""
    src = text("sizeup-view.js")
    body = src[src.index("const PLAN = ["):src.index("];", src.index("const PLAN = ["))]
    return re.findall(r'\["([\w-]+)", "([\w-]+)"\]', body)


# -- the page -------------------------------------------------------------------------------------------------------

def test_the_control_is_a_seg_in_the_incident_head_for_the_console_only():
    html = text("cad.html")
    head = html[html.index('<div id="d-card" class="card inc">'):html.index('<div id="d-queued"')]
    m = re.search(r'<span id="d-tabs" class="seg console-only" role="group" aria-label="Incident view">(.*?)</span>',
                  head, re.S)
    assert m, "the control in #d-card's head"
    btns = re.findall(r'<button type="button" class="sm" data-tab="(\w+)" aria-pressed="(\w+)"[^>]*>([^<]+)</button>',
                      m.group(1))
    assert btns == [("details", "true", "Details"), ("sizeup", "false", "Size-Up")]
    tip = re.search(r'data-tab="sizeup"[^>]*title="([^"]+)"', m.group(1)).group(1)
    for shown in ("incident", "pre-plan", "hazards", "map", "address notes", "history"):
        assert shown in tip.lower(), shown
    assert "building" not in tip.lower() and "street" not in tip.lower()    # not in this part of #797


def test_the_grid_and_its_slots_sit_in_the_incident_hidden():
    html = text("cad.html")
    body = html[html.index('<div id="d-body" hidden>'):html.index('<form id="edit-form"')]
    assert re.search(r'<div id="d-size" class="size-grid" hidden>', body)
    grid = body[body.index('<div id="d-size"'):]
    assert re.findall(r'<section id="(sz-[\w-]+)" class="sz-slot \1"', grid) == SLOTS_LEFT + SLOTS_RIGHT
    assert grid.count('<div class="sz-col">') == 2
    assert grid.index('<div class="sz-col">', grid.index("sz-hazards")) < grid.index("sz-building")   # the right column
    for slot, none in (("sz-hazards", "No live feeds nearby"), ("sz-preplan", "None on file"),
                       ("sz-notes", "None on file")):
        sec = grid[grid.index(f'id="{slot}"'):]
        assert re.search(rf'<div class="sz-none[^"]*">{none}</div>\s*$', sec[:sec.index("</section>")]), slot
    for slot in PLACEHOLDERS:                                # hidden, and claim nothing
        sec = grid[grid.index(f'id="{slot}"'):]
        sec = sec[:sec.index("</section>")]
        assert re.match(r'id="[\w-]+" [^>]*\bhidden>', sec) and "sz-none" not in sec, slot


def test_every_node_it_moves_is_on_the_page_once():
    html = text("cad.html")
    p = plan()
    assert [w for w, _ in p] == [n if n != "card" else "assigned" for s in MOVED.values() for n in s]
    for what, slot in p:
        assert html.count(f'id="{slot}"') == 1, slot
        if what != "assigned":
            assert html.count(f'id="{what}"') == 1, what
    assert re.search(r'<div id="d-grid" class="inc-grid">\s*<div id="d-info">', html)   # the info column
    assert {s for _, s in p} == set(SLOTS_LEFT + SLOTS_RIGHT) - set(PLACEHOLDERS)


def test_the_script_is_in_the_bundle_after_the_incident_map():
    html = text("cad.html")
    block = html[html.index("<!-- bundle js cad -->"):]
    block = block[:block.index("<!-- /bundle -->")]
    assert '<script src="/static/sizeup-view.js"></script>' in block
    assert html.index('src="/static/incident-map.js"') < html.index('src="/static/sizeup-view.js"')
    assert html.index('src="/static/addr-history.js"') < html.index('src="/static/sizeup-view.js"')


def test_details_opens_first_on_every_selection():
    js = text("cad.js")
    assert re.search(r'\btab:\s*"details",', js)
    sel = js[js.index("function selectIncident(id) {"):js.index("function renderDetail() {")]
    assert re.search(r'\bcad\.tab\s*=\s*"details";', sel).start() < sel.index("render();")
    src = text("sizeup-view.js")
    assert re.search(r'if \(cad\.sel !== lastSel\) \{\s*lastSel = cad\.sel;\s*cad\.tab = "details";', src)
    assert re.search(r"cad\.hooks\.push\(apply\)", src)


def test_the_public_api_and_no_keys_of_its_own():
    src = text("sizeup-view.js")
    assert "keydown" not in src                              # like every other .seg group
    users = "".join(p.read_text() for p in STATIC.glob("*.js") if p.name != "sizeup-view.js")
    assert not re.search(r"SizeUp\.(show|tab|choose)\b", users)
    assert re.search(r"SizeUp\.on\(\)", users)               # addr-history.js
    assert re.search(r'observe\(size, \{ subtree: true, attributes: true, attributeFilter: \["hidden"\] \}\)', src)


def test_the_css_two_columns_and_the_phone_order():
    css = text("cad.css")
    assert re.search(r"\.size-grid \{ display: grid; "
                     r"grid-template-columns: minmax\(0, 1fr\) minmax\(18rem, 48%\);", css)
    block = css[css.index("@container (width < 44rem) {\n    .size-grid"):]
    block = block[:block.index("\n  }")]
    assert re.search(r"\.sz-col \{ display: contents; \}", block)
    orders = dict(re.findall(r"\.(sz-[\w-]+) \{ order: (\d+); \}", block))
    assert sorted(orders, key=lambda k: int(orders[k])) == PHONE_ORDER
    assert re.search(r"\.size-grid \.dmap-handle \{ display: none; \}", css)
    assert re.search(r"\.size-grid \.mapbox \{ block-size: var\(--map-h\); \}", css)
    assert re.search(r"\.size-grid \.ml-cams-head \{ flex-wrap: wrap;", css)    # the Cameras row fits a phone's slot
    assert not re.search(r"\.sz-head\s*\{[^}]*font-weight", css)      # h3 keeps the design system's weight


# -- the fake document (node) ---------------------------------------------------------------------------------------

class Tree(HTMLParser):
    """cad.html's #d-body as nested [tag, attrs, children] (elements only)."""
    VOID = {"input", "br", "img", "hr", "meta", "link", "source", "use", "path", "col", "wbr"}

    def __init__(self):
        super().__init__()
        self.root = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if (self.root is None and a.get("id") != "d-body") or (self.root is not None and not self.stack):
            return                                         # before #d-body, or after it closed
        node = [tag, a, []]
        if self.root is None:
            self.root = node
        else:
            self.stack[-1][2].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.stack:
            self.stack[-1][2].append([tag, {k: v or "" for k, v in attrs}, []])

    def handle_endtag(self, tag):
        if self.stack and tag not in self.VOID:
            while self.stack and self.stack.pop()[0] != tag:
                pass


DOM = r"""
const fs = require("fs");
global.window = global;
const observers = [];
let queued = new Set();
const camel = (s) => s.replace(/-(\w)/g, (_, c) => c.toUpperCase());
function detach(c) { if (c.parentNode) c.parentNode.removeChild(c); }
class Comment {
  constructor(t) { this.data = t; this.parentNode = null; this.ownerDocument = doc; }
}
class El {
  constructor(tag, attrs = {}) {
    this.tag = tag; this.attrs = { ...attrs }; this.childNodes = []; this.parentNode = null; this.ownerDocument = doc;
    this._hidden = "hidden" in attrs; this.on = {}; this.title = ""; this.textContent = "";
    this.dataset = {};
    for (const k in attrs) if (k.startsWith("data-")) this.dataset[camel(k.slice(5))] = attrs[k];
    const cl = () => (this.attrs.class || "").split(/\s+/).filter(Boolean);
    const put = (l) => { this.attrs.class = l.join(" "); };
    this.classList = { contains: (c) => cl().includes(c), add: (c) => put([...new Set([...cl(), c])]),
      remove: (c) => put(cl().filter((x) => x !== c)),
      toggle: (c, on) => (on ?? !cl().includes(c)) ? this.classList.add(c) : this.classList.remove(c) };
  }
  get id() { return this.attrs.id || ""; }
  get hidden() { return this._hidden; }
  set hidden(v) {
    v = !!v;
    if (v === this._hidden) return;
    this._hidden = v;
    for (const o of observers) if (o.opts.attributes && (o.opts.attributeFilter || []).includes("hidden")
      && (o.root === this || (o.opts.subtree && o.root.contains(this)))) queued.add(o.fn);
  }
  get children() { return this.childNodes.filter((c) => c instanceof El); }
  get firstElementChild() { return this.children[0] || null; }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  insertBefore(c, ref) { detach(c); this.childNodes.splice(this.childNodes.indexOf(ref), 0, c); c.parentNode = this; }
  appendChild(c) { detach(c); this.childNodes.push(c); c.parentNode = this; return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(typeof c === "string" ? new El("#text") : c)); }
  removeChild(c) { this.childNodes.splice(this.childNodes.indexOf(c), 1); c.parentNode = null; return c; }
  replaceChildren(...cs) { [...this.childNodes].forEach((c) => this.removeChild(c)); this.append(...cs); }
  addEventListener(k, fn) { (this.on[k] = this.on[k] || []).push(fn); }
  click() { for (const fn of this.on.click || []) fn({ preventDefault() {} }); }
  matches(sel) {
    const m = /^([a-z0-9]*)(?:#([\w-]+))?((?:\.[\w-]+)*)$/.exec(sel);
    return (!m[1] || this.tag === m[1]) && (!m[2] || this.id === m[2])
      && m[3].split(".").filter(Boolean).every((c) => this.classList.contains(c));
  }
  closest(sel) { for (let n = this; n instanceof El; n = n.parentNode) if (n.matches(sel)) return n; return null; }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/), out = [];
    const ok = (n) => {
      if (!n.matches(parts[parts.length - 1])) return false;
      let i = parts.length - 2;
      for (let a = n.parentNode; i >= 0 && a && a !== this.parentNode; a = a.parentNode) if (a.matches(parts[i])) i--;
      return i < 0;
    };
    (function walk(n) { for (const c of n.children) { if (ok(c)) out.push(c); walk(c); } })(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
const doc = { createComment: (t) => new Comment(t) };
const build = ([tag, attrs, kids]) => {
  const n = new El(tag, attrs);
  kids.forEach((k) => n.appendChild(build(k)));
  return n;
};
const root = build(JSON.parse(fs.readFileSync(process.argv[4], "utf8")));
Object.assign(doc, { getElementById: (id) => root.id === id ? root : root.querySelector("#" + id),
  querySelectorAll: (s) => root.querySelectorAll(s), querySelector: (s) => root.querySelector(s) });
global.document = doc;
global.MutationObserver = class {
  constructor(fn) { this.fn = fn; }
  observe(r, opts) { observers.push({ root: r, opts, fn: this.fn }); }
};
const flushObservers = () => { const q = queued; queued = new Set(); q.forEach((fn) => fn([])); };
let frames = [];
global.requestAnimationFrame = (fn) => { frames.push(fn); return 1; };
const flushFrames = () => { const f = frames; frames = []; f.forEach((fn) => fn()); flushObservers(); };
const byId = (id) => doc.getElementById(id);
const dump = (n) => n instanceof Comment ? ["#comment", n.data]
  : [n.tag, n.id, n.attrs.class || "", n.childNodes.map(dump)];
const marks = () => { let k = 0; (function walk(n) { n.childNodes.forEach((c) => c instanceof Comment
  ? k += c.data.startsWith("sz:") : walk(c)); })(root); return k; };
const slotIds = (s) => byId(s).children
  .filter((c) => !c.classList.contains("sz-head") && !c.classList.contains("sz-none"))
  .map((c) => c.id || c.attrs.class);
"""

ROUND = DOM + r"""
require(process.argv[2]);
const S = window.SizeUp;
const named = { assigned: byId("d-units").closest(".card") };
const items = S.PLAN.map(([w, s]) => [named[w] || byId(w), byId(s)]);
const before = JSON.stringify(dump(root));
const out = { slots: {}, same: [] };
for (let i = 0; i < 2; i++) {
  const parked = S.moveIn(items);
  for (const [, s] of S.PLAN) out.slots[s] = slotIds(s);
  out.marks = marks();
  S.moveOut(parked);
  out.same.push(JSON.stringify(dump(root)) === before);
}
// a node moved twice (into one slot, then from there into another): only the reverse order brings it home
const home = new El("div"), a = new El("div", { id: "a" }), s1 = new El("div"), s2 = new El("div");
home.append(new El("i"), a, new El("b"));
const was = JSON.stringify(dump(home));
S.moveOut(S.moveIn([[a, s1], [a, s2]]));
out.reverse = JSON.stringify(dump(home)) === was && !s1.childNodes.length;
const p = S.moveIn([[a, s1], [a, s2]]);
p.forEach(([n, m]) => S.unpark(n, m));                     // first in, first out: lands in the first slot
out.forward = a.parentNode === s1;
console.log(JSON.stringify(out));
"""

BEHAVIOUR = DOM + r"""
const writes = [];
let stored = null;
global.localStorage = { getItem: () => stored, setItem: (k, v) => { writes.push([k, v]); stored = v; } };
global.BOARD = false;
global.cad = { hooks: [], sel: 7, mode: "view", tab: "details", radio: false, search: false, pinfo: false,
  scene: null, extra: new Map() };
const inc = (id) => ({ id, address: "12 Elm St", premise: [], status: "active" });
global.selected = () => (cad.sel == null ? null : inc(cad.sel));
global.show = (node, on) => { if (node.hidden === on) node.hidden = !on; };
global.setText = (node, t) => { node.textContent = t ?? ""; };
global.el = (tag, cls, t) => { const n = new El(tag, cls ? { class: cls } : {}); n.textContent = t || ""; return n; };
global.$ = (s) => doc.querySelector(s);
global.api = () => new Promise(() => {});                 // History here's fetch: still loading
let resized = 0, mapApplied = 0;
global.Panels = { resizeMaps: () => resized++ };
global.IncidentMap = { apply: () => mapApplied++ };
require(process.argv[3]);                                  // addr-history.js, before sizeup-view.js as on the page
require(process.argv[2]);
const S = window.SizeUp, H = window.AddrHistory;
const render = () => {                                    // cad.js render(): renderDetail, then the hooks
  if (selected()) H.card(selected(), { readonly: false });
  cad.hooks.forEach((fn) => fn());
  flushObservers();
};
const grid = byId("d-grid"), size = byId("d-size"), tog = byId("d-here-toggle");
const [bDetails, bSize] = doc.querySelectorAll("#d-tabs button");
const pressed = () => [bDetails.getAttribute("aria-pressed"), bSize.getAttribute("aria-pressed")];
const view = () => ({ on: S.on(), tab: cad.tab, pressed: pressed(), grid: !grid.hidden, size: !size.hidden,
  marks: marks() });
const out = { observe: observers.map((o) => o.opts) };

render();
const before = JSON.stringify(dump(root));
render();
out.untouched = { ...view(), same: JSON.stringify(dump(root)) === before, frames: frames.length };

// History here in Details: the toggle opens it and saves that
tog.click();
out.detailsHistory = { open: tog.getAttribute("aria-expanded"), writes: writes.splice(0) };

bSize.click(); flushObservers();
const slots = Object.keys(JSON.parse(process.argv[5]));
out.sizeup = { ...view(), slots: Object.fromEntries(slots.map((s) => [s, slotIds(s)])),
  frames: frames.length };
flushFrames();
out.sizeup.resized = resized; out.sizeup.mapApplied = mapApplied;
out.sizeup.history = tog.getAttribute("aria-expanded");   // folded in Size-Up, though open in Details
const sig = JSON.stringify(dump(root));
render(); render();
out.again = { ...view(), same: JSON.stringify(dump(root)) === sig, frames: frames.length };

// empty slots
const none = (s) => !byId(s).querySelector(".sz-none").hidden;
const head = (s) => !byId(s).querySelector(".sz-head").hidden;
out.empty = { hazards: none("sz-hazards"), notes: none("sz-notes"), preplan: [head("sz-preplan"), none("sz-preplan")] };
byId("d-roads").hidden = false; flushObservers();
out.empty.roadsShown = none("sz-hazards");
byId("d-roads").hidden = true; flushObservers();
out.empty.roadsHidden = none("sz-hazards");
byId("d-preplan").hidden = false; flushObservers();
out.empty.preplanShown = [head("sz-preplan"), none("sz-preplan")];
byId("d-preplan").hidden = true; flushObservers();
out.empty.preplanHidden = [head("sz-preplan"), none("sz-preplan")];

// History here in Size-Up: opens and folds, never saved
tog.click();
out.sizeHistory = { open: tog.getAttribute("aria-expanded"), writes: writes.splice(0) };
H.refold();
out.sizeHistory.refolded = tog.getAttribute("aria-expanded");
tog.click();

// a new selection: Details again, every node home
cad.sel = 8; render(); flushFrames();
out.newSel = { ...view(), same: JSON.stringify(dump(root)) === before, history: tog.getAttribute("aria-expanded"),
  writes: writes.splice(0) };

// what keeps Details: the board, a new call, the Radio / Search / place-info pages, nothing selected
out.kept = {};
const blockers = { board: () => { BOARD = true; }, newCall: () => { cad.mode = "new"; },
  radio: () => { cad.radio = true; },
  search: () => { cad.search = true; }, pinfo: () => { cad.pinfo = true; }, none: () => { cad.sel = null; } };
for (const [name, set] of Object.entries(blockers)) {
  set(); render();
  bSize.click(); flushFrames();
  out.kept[name] = { ...view(), same: JSON.stringify(dump(root)) === before };
  BOARD = false;
  Object.assign(cad, { mode: "view", radio: false, search: false, pinfo: false, sel: 8, tab: "details" });
  render();
}
// and one of them coming up while in Size-Up puts everything back
bSize.click(); flushFrames();
const went = S.on();
cad.radio = true; render(); flushFrames();
out.leave = { went, ...view(), same: JSON.stringify(dump(root)) === before };
console.log(JSON.stringify(out));
"""


def run(tmp_path, script, *extra):
    t = Tree()
    t.feed(text("cad.html"))
    assert t.root and t.root[1].get("id") == "d-body"
    (tmp_path / "tree.json").write_text(json.dumps(t.root))
    (tmp_path / "harness.js").write_text(script)
    r = subprocess.run([NODE, str(tmp_path / "harness.js"), str(STATIC / "sizeup-view.js"),
                        str(STATIC / "addr-history.js"), str(tmp_path / "tree.json"), *extra],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_round_trip_puts_every_node_back_where_it_was(tmp_path):
    out = run(tmp_path, ROUND)
    assert out["same"] == [True, True]                     # exact, twice: same parents, same order, no marks left
    assert out["slots"] == MOVED
    assert out["marks"] == sum(map(len, MOVED.values()))
    assert out["reverse"] is True and out["forward"] is True   # why moveOut goes last first


@pytest.fixture(scope="module")
def behaviour(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    return run(tmp_path_factory.mktemp("sizeup"), BEHAVIOUR, json.dumps(MOVED))


DETAILS = {"on": False, "tab": "details", "pressed": ["true", "false"], "grid": True, "size": False, "marks": 0}


def test_details_untouched_until_size_up_is_chosen(behaviour):
    assert behaviour["untouched"] == {**DETAILS, "same": True, "frames": 0}


def test_size_up_moves_every_node_into_its_slot_once(behaviour):
    s = behaviour["sizeup"]
    assert {k: s[k] for k in DETAILS} == {"on": True, "tab": "sizeup", "pressed": ["false", "true"], "grid": False,
                                          "size": True, "marks": sum(map(len, MOVED.values()))}
    assert s["slots"] == MOVED
    assert s["frames"] == 1 and s["resized"] == 1 and s["mapApplied"] == 1      # the maps sized to the new column
    a = behaviour["again"]                                 # re-rendered: nothing moves twice
    assert a["same"] is True and a["marks"] == s["marks"] and a["frames"] == 0 and a["on"] is True


def test_a_new_selection_is_details_with_every_node_home(behaviour):
    n = behaviour["newSel"]
    assert {k: n[k] for k in DETAILS} == DETAILS and n["same"] is True


@pytest.mark.parametrize("why", ["board", "newCall", "radio", "search", "pinfo", "none"])
def test_what_keeps_details(behaviour, why):
    assert behaviour["kept"][why] == {**DETAILS, "tab": "sizeup", "same": True}


def test_leaving_for_another_page_puts_everything_back(behaviour):
    lv = behaviour["leave"]
    assert lv["went"] is True and lv["same"] is True and lv["marks"] == 0 and lv["grid"] and not lv["size"]


def test_empty_slots_say_so_as_modules_flip_hidden(behaviour):
    assert behaviour["observe"] == [{"subtree": True, "attributes": True, "attributeFilter": ["hidden"]}]
    e = behaviour["empty"]
    assert e["hazards"] is True and e["notes"] is True     # every feed hidden: "No live feeds nearby"
    assert e["roadsShown"] is False and e["roadsHidden"] is True
    assert e["preplan"] == [True, True]                    # its own head only over "None on file"
    assert e["preplanShown"] == [False, False] and e["preplanHidden"] == [True, True]


def test_history_folds_in_size_up_without_saving(behaviour):
    assert behaviour["detailsHistory"] == {"open": "true", "writes": [["sizeup.cad.hereOpen", "1"]]}
    assert behaviour["sizeup"]["history"] == "false"       # folded on the way in
    sh = behaviour["sizeHistory"]
    assert sh["open"] == "true" and sh["writes"] == [] and sh["refolded"] == "false"
    assert behaviour["newSel"]["history"] == "true" and behaviour["newSel"]["writes"] == []   # Details' saved state
