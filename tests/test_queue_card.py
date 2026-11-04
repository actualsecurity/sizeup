"""The queue card's head (GitHub #231): the number and the elapsed time on the top row, the time at the end; the
type on its own line under them, the card's width, wrapping as text does; the town's chip (a Rye Brook call) after
the type on that line. Before, all four sat in one flex row: the type (shrinkable to nothing) was squeezed to a word
a line, its text ran under the chip, and the time ran past the card's edge at the queue's narrowest width.

The card is built in node from cad.js's own fillQueueItem with a small stand-in DOM (skipped where node isn't
installed); the stylesheet checks run everywhere. The incidents are made up."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_design import declarations, layer_block, strip_comments

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def rules(selector):
    """{property: value} for one exact selector in cad.css's views layer."""
    css = layer_block(strip_comments((STATIC / "cad.css").read_text()), "views")
    return {p: v for s, p, v in declarations(css) if s == selector}


def test_the_head_holds_the_number_and_the_time_and_the_type_has_its_own_line():
    head, num, time, line = rules(".qh"), rules(".qn"), rules(".qe"), rules(".qtl")
    assert head["display"] == "flex" and head["min-inline-size"] == "0"
    assert num["min-inline-size"] == "0" and num["overflow-wrap"] == "anywhere"      # a long number wraps in its room
    assert "nowrap" not in num.get("white-space", "")
    assert time["flex"] == "none" and time["white-space"] == "nowrap" and time["margin-inline-start"] == "auto"
    assert line["min-inline-size"] == "0" and "overflow-wrap" in line
    css = strip_comments((STATIC / "cad.css").read_text())
    assert ".qh .qt" not in css and ".qh .spacer" not in css              # the type is no flex item of the top row
    # nothing in the card's head has a fixed width to squeeze the type
    for sel in (".qh", ".qn", ".qe", ".qtl", ".qt", ".qtl .chip"):
        for p in rules(sel):
            assert not re.fullmatch(r"(max-|min-)?(width|inline-size)", p) or rules(sel)[p] == "0", (sel, p)


def test_the_styleguide_card_is_built_the_same_way():
    sg = (STATIC / "styleguide.js").read_text()
    assert 'head.append(el("b", "qn", n), el("span", "qe", "02:41"));' in sg
    assert 'el("div", "qtl")' in sg


CARD = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const grab = (name) => { const m = src.match(new RegExp("^function " + name + "\\([^)]*\\) \\{[\\s\\S]*?^\\}", "m"));
                         if (!m) throw new Error("not found: " + name); return m[0]; };
class Node {
  constructor(tag) { this.tag = tag; this.className = ""; this.children = []; this.dataset = {}; this.attrs = {};
                     this.style = { props: {}, setProperty: (k, v) => { this.style.props[k] = v; } }; this._text = ""; }
  get classList() { const n = this; return { add: (c) => { n.className = (n.className + " " + c).trim(); } }; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  append(...xs) { for (const x of xs) this.children.push(typeof x === "string" ? Object.assign(new Node("#text"), { _text: x }) : x); }
  replaceChildren(...xs) { this.children = []; this.append(...xs); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
}
global.document = { createElement: (t) => new Node(t) };
global.cad = { sel: 0, state: { lists: {
  event_types: [{ code: "INVEST", name: "Investigation", color: "#CC4125" }, { code: "ALARM", name: "Alarm", color: "#CC0000" }],
  event_subtypes: [{ code: "CO-NO", name: "Carbon monoxide – no symptoms with odor" }, { code: "COMM", name: "Commercial" }],
}, overview: [], talking: [] } };
global.BOARD = false;
global.since = (n) => { n.textContent = "20:55"; return n; };
global.fmtTime = () => "17:33";
global.airportLine = () => "";
global.unconfirmed = () => false;
global.txNow = () => false;
global.canDo = () => true;
global.statusChip = (code, text) => { const n = new Node("span"); n.className = "chip st"; n.textContent = text; return n; };
eval(["el", "L", "nameOf", "typeLabel", "typeColor", "isOpen", "incTitle", "openAssignments", "callSummary", "callLive",
      "fillQueueItem"].map(grab).join("\n") + "\nglobal.fillQueueItem = fillQueueItem;");
const shape = (n) => ({ cls: n.className, text: n.children.length ? undefined : n._text, c: n.children.map(shape),
                        color: n.style.props["--c"] });
const card = (inc) => { const li = new Node("li"); fillQueueItem(li, inc); return shape(li); };
const base = { id: 1, status: "active", emergency: true, created_at: 1, address: "420 N Ridge St, Rye Brook", assignments: [] };
console.log(JSON.stringify({
  joint: card({ ...base, number: "26-000032-RB-0001", type: "INVEST", subtype: "CO-NO", joint_call: "Rye Brook" }),
  ours: card({ ...base, id: 2, number: "26-000002", type: "ALARM", subtype: "COMM", joint_call: "", address: "1 Gateway Plz" }),
  untyped: card({ ...base, id: 3, number: "26-000003", type: "", subtype: "", joint_call: "" }),
}));
"""


@needs_node
def test_the_card_puts_the_number_and_time_on_top_and_the_type_and_town_under_them(tmp_path):
    h = tmp_path / "card.js"
    h.write_text(CARD)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "cad.js")], capture_output=True, text=True, check=True,
                                    timeout=30).stdout)
    head, line, addr = out["joint"]["c"][:3]
    assert head["cls"] == "qh" and [(c["cls"], c["text"]) for c in head["c"]] == [("qn", "26-000032-RB-0001"), ("qe", "20:55")]
    assert line["cls"] == "qtl"
    assert [(c["cls"], c["text"]) for c in line["c"]] == [("qt", "Investigation / Carbon monoxide – no symptoms with odor"),
                                                         ("chip joint", "Rye Brook")]
    assert line["c"][0]["color"] == "#CC4125"                                  # the type in its color, as before
    assert addr["cls"] == "qa" and addr["text"] == "420 N Ridge St, Rye Brook"
    # no town chip on our own call; the type line still its own
    head, line = out["ours"]["c"][:2]
    assert [c["cls"] for c in head["c"]] == ["qn", "qe"]
    assert [(c["cls"], c["text"]) for c in line["c"]] == [("qt", "Alarm / Commercial")]
    # an event with no type yet: "No type" in the warning color, on the same line
    line = out["untyped"]["c"][1]
    assert line["cls"] == "qtl" and [(c["cls"], c["text"]) for c in line["c"]] == [("qt warn", "No type")]
