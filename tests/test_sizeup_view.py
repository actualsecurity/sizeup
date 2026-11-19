"""The incident's Size-Up view (GitHub #797, static/sizeup-view.js): Details · Size-Up in the incident's head.

    Details     today's view, unchanged; every selectIncident() opens it
    Size-Up     a two-column grid (#d-size) of the detail's own nodes, moved into slots and back, never copied
    moving      a comment left where each node was; back in reverse order, so every node is home exactly
    narrow      one column in the #797 order: Incident, Address notes, Map, Hazards, Pre-plan, Building, Street View, History
    History     folded each time Size-Up opens, not remembered (Details keeps its remembered state)
The round trip runs in node on a tree built from cad.html's own #d-body; the layout is checked in a browser (the PR's
screenshots)."""
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
PHONE_ORDER = ["sz-incident", "sz-notes", "sz-map", "sz-hazards", "sz-preplan", "sz-building", "sz-street", "sz-history"]


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
    m = re.search(r'<span id="d-tabs" class="seg console-only" role="group" aria-label="Incident view">(.*?)</span>', head, re.S)
    assert m, "the control in #d-card's head"
    btns = re.findall(r'<button type="button" class="sm" data-tab="(\w+)" aria-pressed="(\w+)"[^>]*>([^<]+)</button>', m.group(1))
    assert btns == [("details", "true", "Details"), ("sizeup", "false", "Size-Up")]


def test_the_grid_and_its_slots_sit_in_the_incident_hidden():
    html = text("cad.html")
    body = html[html.index('<div id="d-body" hidden>'):html.index('<form id="edit-form"')]
    assert '<div id="d-size" class="size-grid" hidden>' in body
    grid = body[body.index('<div id="d-size"'):]
    ids = re.findall(r'<section id="(sz-[\w-]+)" class="sz-slot \1"', grid)
    assert ids == SLOTS_LEFT + SLOTS_RIGHT
    assert grid.count('<div class="sz-col">') == 2
    assert grid.index('<div class="sz-col">', grid.index("sz-hazards")) < grid.index("sz-building")   # the right column
    for slot, none in (("sz-building", "Not on file"), ("sz-street", "Not loaded"), ("sz-hazards", "None found"),
                       ("sz-preplan", "None on file"), ("sz-notes", "None on file")):
        sec = grid[grid.index(f'id="{slot}"'):]
        assert sec[:sec.index("</section>")].endswith(f'<div class="sz-none dim small">{none}</div>'), slot


def test_every_node_it_moves_is_on_the_page_once():
    html = text("cad.html")
    p = plan()
    assert [w for w, _ in p] == ["info", "assigned", "d-cmts", "d-preplan-form", "d-preplan", "d-docs-form", "d-docs",
                                 "d-roads", "d-trains", "d-vessels", "d-cams", "d-tides", "d-airport", "d-mapbox",
                                 "d-premise", "d-premise-form", "d-here"]
    for what, slot in p:
        assert html.count(f'id="{slot}"') == 1, slot
        if what not in ("info", "assigned"):
            assert html.count(f'id="{what}"') == 1, what
    assert {s for _, s in p} == set(SLOTS_LEFT + SLOTS_RIGHT) - {"sz-building", "sz-street"}   # PR 2 and 5 fill these


def test_the_script_is_in_the_bundle_after_the_incident_map():
    html = text("cad.html")
    block = html[html.index("<!-- bundle js cad -->"):]
    block = block[:block.index("<!-- /bundle -->")]
    assert '<script src="/static/sizeup-view.js"></script>' in block
    assert html.index('src="/static/incident-map.js"') < html.index('src="/static/sizeup-view.js"')
    assert html.index('src="/static/addr-history.js"') < html.index('src="/static/sizeup-view.js"')


def test_details_opens_first_on_every_selection():
    js = text("cad.js")
    assert '  tab: "details",            // the incident\'s view: "details" or "sizeup" (sizeup-view.js, #797)' in js
    sel = js[js.index("function selectIncident(id) {"):js.index("function renderDetail() {")]
    assert sel.index('cad.tab = "details";') < sel.index("render();")
    src = text("sizeup-view.js")
    assert 'if (cad.sel !== lastSel) { lastSel = cad.sel; cad.tab = "details"; }' in src   # any other way in, too
    assert "!BOARD && cad.tab === \"sizeup\"" in src                                        # the board: Details only
    assert "cad.hooks.push(apply);" in src


def test_the_css_two_columns_and_the_phone_order():
    css = text("cad.css")
    assert ".size-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(18rem, 48%);" in css
    block = css[css.index("@container (width < 44rem) {\n    .size-grid"):]
    block = block[:block.index("\n  }")]
    assert ".sz-col { display: contents; }" in block
    orders = dict(re.findall(r"\.(sz-[\w-]+) \{ order: (\d+); \}", block))
    assert sorted(orders, key=lambda k: int(orders[k])) == PHONE_ORDER
    assert ".size-grid .dmap-handle { display: none; }" in css
    assert ".size-grid .mapbox { block-size: var(--map-h); }" in css


def test_history_is_folded_in_size_up_and_not_remembered():
    src = text("addr-history.js")
    assert "const open = () => (window.SizeUp && SizeUp.on() ? st.sizeOpen : st.open);" in src
    click = src[src.index('$("#d-here-toggle").addEventListener("click"'):]
    click = click[:click.index("});")]
    assert click.index("st.sizeOpen = !st.sizeOpen") < click.index("else") < click.index("remember(st.open)")
    assert "function refold() {" in src and "st.sizeOpen = false;" in src and "refold };" in src
    assert "AddrHistory.refold()" in text("sizeup-view.js")


# -- the round trip (node) ------------------------------------------------------------------------------------------

class Tree(HTMLParser):
    """cad.html's #d-body as nested [tag, id, cls, children] (elements only)."""
    VOID = {"input", "br", "img", "hr", "meta", "link", "source", "use", "path", "col", "wbr"}

    def __init__(self):
        super().__init__()
        self.root = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if (self.root is None and a.get("id") != "d-body") or (self.root is not None and not self.stack):
            return                                         # before #d-body, or after it closed
        node = [tag, a.get("id") or "", a.get("class") or "", []]
        if self.root is None:
            self.root = node
        else:
            self.stack[-1][3].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self.stack:
            a = dict(attrs)
            self.stack[-1][3].append([tag, a.get("id") or "", a.get("class") or "", []])

    def handle_endtag(self, tag):
        if self.stack and tag not in self.VOID:
            while self.stack and self.stack.pop()[0] != tag:
                pass


ROUND = r"""
const fs = require("fs");
global.window = {};
eval(fs.readFileSync(process.argv[2], "utf8"));
const S = window.SizeUp;
const tree = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const doc = { createComment: (t) => mk("#comment", t, "", []) };
function mk(tag, id, cls, kids) {
  const n = { tag, id, cls, childNodes: [], parentNode: null, ownerDocument: doc,
    insertBefore(c, ref) { detach(c); this.childNodes.splice(this.childNodes.indexOf(ref), 0, c); c.parentNode = this; return c; },
    appendChild(c) { detach(c); this.childNodes.push(c); c.parentNode = this; return c; },
    removeChild(c) { this.childNodes.splice(this.childNodes.indexOf(c), 1); c.parentNode = null; return c; } };
  for (const k of kids) n.appendChild(mk(...k));
  return n;
}
function detach(c) { if (c.parentNode) c.parentNode.removeChild(c); }
const root = mk(...tree);
const all = [];
(function walk(n) { all.push(n); n.childNodes.forEach(walk); })(root);
const byId = (id) => all.find((n) => n.id === id);
const named = { info: byId("d-grid").childNodes.find((c) => c.tag !== "#comment"),
                assigned: all.find((n) => n.cls.split(" ").includes("card") && n.childNodes.some((c) => c.id === "d-units")) };
const items = S.PLAN.map(([w, s]) => [named[w] || byId(w), byId(s)]);
const dump = (n) => [n.tag, n.id, n.childNodes.map(dump)];
const before = JSON.stringify(dump(root));
const out = { slots: {}, same: [] };
for (let i = 0; i < 2; i++) {
  const parked = S.moveIn(items);
  for (const [, s] of S.PLAN) out.slots[s] = byId(s).childNodes.filter((c) => c.tag !== "h3" && !c.cls.includes("sz-none")).map((c) => c.id || c.cls);
  S.moveOut(parked);
  out.same.push(JSON.stringify(dump(root)) === before);
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_round_trip_puts_every_node_back_where_it_was(tmp_path):
    t = Tree()
    t.feed(text("cad.html"))
    assert t.root and t.root[1] == "d-body"
    (tmp_path / "tree.json").write_text(json.dumps(t.root))
    (tmp_path / "round.js").write_text(ROUND)
    out = json.loads(subprocess.run([NODE, str(tmp_path / "round.js"), str(STATIC / "sizeup-view.js"), str(tmp_path / "tree.json")],
                                    capture_output=True, text=True, check=True, timeout=30).stdout)
    assert out["same"] == [True, True]                     # exact, twice: same parents, same order, no marks left
    assert out["slots"]["sz-incident"] == ["", "card", "d-cmts"]       # the info column, the Assigned card, the comments
    assert out["slots"]["sz-preplan"] == ["d-preplan-form", "d-preplan", "d-docs-form", "d-docs"]
    assert out["slots"]["sz-hazards"] == ["d-roads", "d-trains", "d-vessels", "d-cams", "d-tides", "d-airport"]
    assert out["slots"]["sz-map"] == ["d-mapbox"]
    assert out["slots"]["sz-notes"] == ["d-premise", "d-premise-form"]
    assert out["slots"]["sz-history"] == ["d-here"]
