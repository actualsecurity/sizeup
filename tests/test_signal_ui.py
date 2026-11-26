"""The signal metrics' UI (#606), in node over a tiny DOM: the charts (signal-chart.js) draw an empty series as "No data"
and a dense run of events as one tick with its count; Settings > Radios > Signal (signal-panel.js) lays out a channel's
and a P25 system's charts from what the API answers, and a 403 is a plain "Admins only"; the Squelch card
(squelch-panel.js) shows "Suggested N dB" with Apply for an admin on a traffic channel only, and Apply posts the same
squelch edit as typing it (audited the same); Health (health.js) links a shift hint to its chart.

The data is what the hub would answer for synthetic records in the real shapes (tests/test_signal_insight.py): the
store's own query() and the insight's suggest(), run here, serialised to the page's fetch."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cloud import metricsinsight as mi
from sizeup.cloud.metricstore import MetricStore
from tests.test_signal_insight import DAY, NOW, floor_minutes, transmissions

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

DOM = r"""
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
class N {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.className = ""; this.textContent = "";
    this.title = ""; this.dataset = {}; this.style = {}; this.hidden = false; this.value = ""; this.disabled = false;
    this.on = {}; this.isConnected = true; this.offsetParent = {}; this.clientWidth = 640; this.type = "";
    this.classList = { add: (c) => { this.className += " " + c; }, contains: (c) => this.className.split(" ").includes(c) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  append(...c) { for (const x of c) this.children.push(typeof x === "string" ? Object.assign(new N("#text"), { textContent: x }) : x); }
  replaceChildren(...c) { this.children = []; this.append(...c); }
  addEventListener(k, f) { (this.on[k] = this.on[k] || []).push(f); }
  contains() { return false; }
  getBoundingClientRect() { return { left: 0, width: Number(this.attrs.width || 640) }; }
  closest() { return null; } focus() {}
  get tHead() { return this.children.find((c) => c.tag === "thead"); }
  get tBodies() { return this.children.filter((c) => c.tag === "tbody"); }
}
global.Node = N;
const text = (n) => (n.textContent || "") + n.children.map(text).join("");
const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const c of n.children) all(c, pred, out); return out; };
const cls = (n, c) => all(n, (x) => (x.className || "").split(/\s+/).includes(c));
global.document = { createElement: (t) => new N(t), createElementNS: (ns, t) => new N(t),
                    createTextNode: (t) => Object.assign(new N("#text"), { textContent: t }), activeElement: null,
                    visibilityState: "visible", hidden: false, addEventListener() {} };
global.window = global;
global.location = { hash: data.hash || "" };
global.setTimeout = () => 0; global.clearTimeout = () => {};
global.serverNow = () => data.now;
global.fmtTime = (t) => "t" + t;
global.el = (tag, c, t) => { const e = new N(tag); if (c) e.className = c; if (t !== undefined && t !== null) e.textContent = t; return e; };
global.fetched = [];
global.fetch = async (path) => {
  fetched.push(path);
  const key = Object.keys(data.fetch).find((k) => path.startsWith(k)) ;
  const r = key ? data.fetch[key] : { status: 404, body: { error: "nope" } };
  return { ok: r.status === 200, status: r.status, json: async () => r.body };
};
const load = (name) => eval(fs.readFileSync(process.argv[3] + "/" + name, "utf8"));
const flush = () => new Promise((ok) => setImmediate(ok));
"""


def run(tmp_path, body, data):
    h, d = tmp_path / "h.js", tmp_path / "d.json"
    h.write_text(DOM + "(async () => {\n" + body + "\n})().catch((e) => { console.error(e.stack || e); process.exit(1); });")
    d.write_text(json.dumps(data, default=str))
    out = subprocess.run([NODE, str(h), str(d), str(STATIC)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# -- the charts -------------------------------------------------------------------------------------------------------

def test_an_empty_series_is_no_data_and_dense_events_are_one_tick_with_their_count(tmp_path):
    out = run(tmp_path, r"""
load("signal-chart.js");
const C = window.SignalChart;
const empty = C.plot({ x0: 0, x1: 3600, time: true, layers: [{ type: "line", name: "Rate", pts: [] },
  { type: "hline", name: "Suggested", y: 13, ref: true }], label: "nothing" });
const t0 = 1800000000;
const evs = [{ at: t0 + 600, ev: "sdr", text: "sdr: SDR 00000102 -> 00000105" }, { at: t0 + 610, ev: "gain", text: "gain: 36.4 -> 42.1" },
  { at: t0 + 620, ev: "squelch", text: "squelch: open 12" }, { at: t0 + 3000, ev: "start", text: "start" }];
const f = C.plot({ x0: t0, x1: t0 + 3600, time: true, label: "FG1", events: evs, layers: [
  { type: "band", name: "Idle q50–q90", pts: [[t0, 7, 9], [t0 + 60, 7.2, 9.1], [t0 + 120, 7.1, 9.3]] },
  { type: "dots", name: "Voice", pts: [[t0 + 60, 21.5, "2.4 s"]] },
  { type: "step", name: "Open", pts: [[t0, 10]], label: "Open 10", ref: true }] });
const svg = f.children[0];
const ticks = cls(svg, "sg-ev");
// hover over the voice dot: the readout lists every layer there
const x = 40 + (60 / 3600) * (Number(svg.attrs.width) - 40 - 56);
svg.on.pointermove[0]({ clientX: x });
const tip = cls(f, "sg-tip")[0];
console.log(JSON.stringify({ empty: [empty.dataset.empty, cls(empty, "sg-none").map(text)],
  full: f.dataset.empty, ticks: ticks.map((t) => [text(t.children.find((c) => c.tag === "text") || { textContent: "", children: [] }),
                                                  text(t.children.find((c) => c.tag === "title"))]),
  tip: tip.hidden ? null : cls(tip, "sg-tip-r").map(text), dl: cls(svg, "sg-dl").map(text),
  spark: [C.spark([1, null, 3]).children.length, C.spark([]).children.length] }));
""", {"now": NOW, "fetch": {}})
    assert out["empty"] == ["1", ["No data"]]                       # a reference line alone is still no data
    assert out["full"] == ""
    assert out["ticks"][0][0] == "3" and out["ticks"][0][1].count("\n") == 2   # three within 8 px: one tick, "3"
    assert "sdr: SDR 00000102 -> 00000105" in out["ticks"][0][1] and out["ticks"][1][0] == ""
    assert out["tip"] == ["7.2–9.1Idle q50–q90", "21.5Voice · 2.4 s", "10Open"]
    assert out["dl"] == ["Open 10"]
    assert out["spark"] == [3, 1]                                    # title, line, the last dot; empty: the title only


# -- Settings > Radios > Signal ---------------------------------------------------------------------------------------

def api_data(tmp_path):
    """What the hub answers: FG1 (traffic, 7 days), FG2 (no data in range), the county P25 system."""
    s = MetricStore(tmp_path / "metrics.sqlite", log=lambda m: None, clock=lambda: NOW)
    fl = [dict(r, s="FG1") for r in floor_minutes(NOW - 2 * DAY, 2 * 1440)]
    tx = [dict(r, s="FG1") for r in transmissions(NOW - DAY, DAY, [15 + (k % 15) for k in range(60)])]
    cc = [{"k": "cc", "s": "p25", "at": a, "n": 20, "state": "ok", "rate": 40.0, "lo": 36.0, "hi": 44.0,
           **({"unc": {"773.68125": 2}} if a % 3600 == 0 else {})} for a in range(int(NOW - DAY), int(NOW), 60)]
    evs = [{"k": "ev", "s": "FG1", "at": NOW - 3 * 3600, "ev": "sdr", "d": "SDR 00000102 -> 00000105"},
           {"k": "ev", "s": "p25", "at": NOW - 5 * 3600, "ev": "cc_lost", "d": ""},
           {"k": "ev", "s": "p25", "at": NOW - 4 * 3600, "ev": "cc_back", "d": ""}]
    old = [{"k": "floor", "s": "FG2", "at": NOW - 20 * DAY, "q90": 4.0, "kind": "traffic", "open": 10, "close": 6}]
    for chunk in (fl, tx, cc, evs, old):
        s.ingest("pcfd-hq", chunk)
    q = {"from": NOW - DAY, "to": NOW, "res": "raw"}
    sugg = mi.suggest(floor_minutes(NOW - 7 * DAY, 7 * 1440), transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212),
                      mi.DEFAULTS, NOW)
    sugg.update(subject="FG1", station="pcfd-hq")
    return {"/api/cad/metrics?view=squelch": {"status": 200, "body": {"channels": [sugg]}},
            "/api/cad/metrics?subject=FG1": {"status": 200, "body": s.query({**q, "subject": "FG1"})},
            "/api/cad/metrics?subject=FG2": {"status": 200, "body": s.query({**q, "subject": "FG2"})},
            "/api/cad/metrics?subject=p25": {"status": 200, "body": s.query({**q, "subject": "p25"})},
            "/api/cad/metrics": {"status": 200, "body": s.index()}}


PANEL = r"""
load("signal-chart.js"); load("signal-panel.js");
const box = new N("div");
SignalPanel.mount(box);
for (let i = 0; i < 20; i++) await flush();
const cards = cls(box, "sg-card");
console.log(JSON.stringify({
  fetched, error: text(box).slice(0, 80),
  cards: cards.map((c) => ({ subject: c.dataset.subject, head: text(c.children[0]),
    sugg: (cls(c.children[0], "small").find((s) => s.title) || {}).title || "",
    figs: cls(c, "sg-fig").map((f) => [f.attrs["aria-label"], f.dataset.empty, cls(f, "sg-ev").length,
                                        all(f, (n) => n.tag === "circle").length, cls(f, "sg-spans").length]) })),
  controls: cls(box, "seg").map((g) => g.children.map((b) => [b.textContent, b.attrs["aria-pressed"], b.disabled])) }));
"""


def test_the_signal_tab_draws_each_channel_and_p25_system_from_the_api(tmp_path):
    out = run(tmp_path, PANEL, {"now": NOW, "fetch": api_data(tmp_path)})
    assert out["fetched"][:2] == ["/api/cad/metrics", "/api/cad/metrics?view=squelch"]
    assert any("subject=FG1" in f and "res=raw" in f for f in out["fetched"])       # 24 h: raw
    by = {c["subject"]: c for c in out["cards"]}
    assert list(by) == ["FG1", "FG2", "p25"]                                          # channels, then P25
    fg1 = by["FG1"]
    assert fg1["head"].startswith("FG1FM · traffic") and "Suggested 13 / 9 dB · now 10 / 6" in fg1["head"]
    assert fg1["sugg"].startswith("noise p90 ")                                       # the numbers on hover
    labels = [f[0] for f in fg1["figs"]]
    assert labels == ["FG1: idle quieting, squelch, transmissions", "FG1: noise opens a minute", "FG1: transmissions by kind",
                      "FG1: quieting distribution, 7 d"]
    assert fg1["figs"][0][1] == "" and fg1["figs"][0][2] == 1 and fg1["figs"][0][3] == 60    # the sdr tick, 60 dots
    assert all(f[1] == "1" for f in by["FG2"]["figs"][:3])                          # nothing in range: "No data"
    p25 = by["p25"]
    assert [f[0] for f in p25["figs"]] == ["p25: control channel decode rate", "p25: grants on uncovered frequencies"]
    assert p25["figs"][0][4] == 1 and p25["figs"][1][1] == ""                         # cc_lost shaded; grants drawn
    assert out["controls"][0] == [["6 h", "false", False], ["24 h", "true", False], ["7 d", "false", False], ["30 d", "false", False]]


def test_a_health_hint_link_focuses_its_channel_and_time(tmp_path):
    data = {"now": NOW, "fetch": api_data(tmp_path), "hash": f"#settings/radios/signal?s=FG1&at={int(NOW - 3 * 3600)}"}
    out = run(tmp_path, PANEL, data)
    assert [c["subject"] for c in out["cards"]] == ["FG1"]
    f = next(f for f in out["fetched"] if "subject=FG1" in f)
    assert f"to={int(NOW)}" in f and f"from={int(NOW) - 86400}" in f                 # the 24 h around it (up to now)


def test_not_an_admin_is_a_plain_admins_only(tmp_path):
    out = run(tmp_path, PANEL, {"now": NOW, "fetch": {"/api/cad/metrics": {"status": 403, "body": {"error": "Admins only"}}}})
    assert out["error"] == "Admins only" and out["cards"] == []
    out = run(tmp_path, PANEL, {"now": NOW, "fetch": {}})
    assert out["error"] == "On the hub only"


# -- the Squelch card's suggestion ------------------------------------------------------------------------------------

SQUELCH = r"""
load("squelch-panel.js");
global.canDo = () => data.admin;
global.operator = () => "KM";
global.posted = [];
global.api = async (m, p, b) => { if (m === "POST") { posted.push([p, b]); return { ok: true }; } return data.squelch; };
global.UI = { confirm: async (t) => { global.asked = t; return true; } };
const box = new N("div");
SquelchPanel.mount(box);
for (let i = 0; i < 10; i++) await flush();
const rows = all(box, (n) => n.tag === "tr");
const head = rows[0].children.map((th) => th.textContent);
const cells = rows.slice(1).map((r) => cls(r, "sq-sugg").map((td) => [text(td), (td.children[0] || {}).title || td.title]));
const apply = all(box, (n) => n.tag === "button" && n.textContent === "Apply");
if (apply.length) { apply[0].on.click[0](); for (let i = 0; i < 10; i++) await flush(); }
console.log(JSON.stringify({ head, cells, applies: apply.length, posted, asked: global.asked || "", fetched }));
"""


def squelch_data(admin=True, status=200):
    sugg = mi.suggest(floor_minutes(NOW - 7 * DAY, 7 * 1440), transmissions(NOW - 7 * DAY, 7 * DAY, [20.0] * 212),
                      mi.DEFAULTS, NOW)
    page = mi.suggest(floor_minutes(NOW - 7 * DAY, 7 * 1440, kind="paging"),
                      transmissions(NOW - 7 * DAY, 7 * DAY, [22.0] * 30, kind="page"), mi.DEFAULTS, NOW)
    row = lambda key, kind, run=(10.0, 6.0): {"key": key, "kind": kind, "editable": kind in ("traffic", "paging"), "set": {},
                             "state": "ok",
                             "default": {"open_db": 10.0, "close_db": 6.0, "min_open_s": 0.3},
                             "running": {"open_db": run[0], "close_db": run[1], "min_open_s": 0.3}, "hiss_db": 2.4,
                             "quieting_db": 3.1, "short_per_min": 0.2, "keyups": None}
    return {"now": NOW, "admin": admin,
            "squelch": {"channels": [row("FG1", "traffic"), row("South 2", "paging"), row("FG2", "traffic", (13.0, 9.0))],
                        "ranges": {}, "sync": {}},
            "fetch": {"/api/cad/metrics?view=squelch": {"status": status, "body": {"channels": [
                {**sugg, "subject": "FG1", "station": "pcfd-hq"}, {**page, "subject": "South 2", "station": "pcfd-hq"},
                {**sugg, "subject": "FG2", "station": "pcfd-hq"}]}
                if status == 200 else {"error": "Admins only"}}}}


def test_suggested_with_apply_for_an_admin_on_a_traffic_channel_and_it_posts_the_audited_edit(tmp_path):
    out = run(tmp_path, SQUELCH, squelch_data())
    assert "Suggested" in out["head"]
    fg1, south, fg2 = out["cells"]
    assert fg2[0][0] == "13 dB = now"                                   # already running it (an Apply seconds ago)
    assert fg1[0][0] == "13 dB Apply" and fg1[0][1].startswith("Suggested Open 13 / Close 9 dB\nnoise p90 ")
    assert south[0][0] == "10 dB info"                                 # paging: the numbers, never Apply
    assert out["applies"] == 1
    assert out["asked"] == "FG1: Open 10 → 13 dB, Close 6 → 9 dB? 0 of 212 real transmissions under 13 dB"
    # what it was worked out for goes with it: the server refuses it if the squelch running now is another
    assert out["posted"] == [["/api/cad/settings/squelch", {"action": "set", "key": "FG1", "open_db": 13, "close_db": 9,
                                                            "expect": {"open_db": 10, "close_db": 6}, "operator": "KM"}]]


def test_no_apply_when_in_doubt_weak_traffic_settling_or_a_squelch_that_moved(tmp_path):
    data = squelch_data()
    chans = data["fetch"]["/api/cad/metrics?view=squelch"]["body"]["channels"]
    weak = mi.suggest(floor_minutes(NOW - 7 * DAY, 7 * 1440), transmissions(NOW - 7 * DAY, 7 * DAY, [12.2] * 30 + [20.0] * 200),
                      mi.DEFAULTS, NOW)
    chans[0] = {**weak, "subject": "FG1", "station": "pcfd-hq"}
    data["squelch"]["channels"].append({**data["squelch"]["channels"][0], "key": "FG3",
                                        "running": {"open_db": 11.0, "close_db": 7.0, "min_open_s": 0.3}})
    chans.append({**chans[2], "subject": "FG3"})                 # worked out for 10 / 6; 11 / 7 runs now
    out = run(tmp_path, SQUELCH, data)
    fg1, _south, _fg2, fg3 = out["cells"]
    assert fg1[0][0] == "10 dB weak traffic overlaps the noise" and out["applies"] == 0
    assert fg3[0][0] == "13 dB settling"


def test_no_suggestions_for_someone_who_isnt_an_admin_or_off_the_hub(tmp_path):
    out = run(tmp_path, SQUELCH, squelch_data(admin=False))
    assert "Suggested" not in out["head"] and out["applies"] == 0 and out["fetched"] == []    # never even asked
    out = run(tmp_path, SQUELCH, squelch_data(status=403))
    assert "Suggested" not in out["head"] and out["applies"] == 0


PAGING_ROW = r"""
load("squelch-panel.js");
global.canDo = () => data.admin;
global.operator = () => "KM";
global.posted = [];
global.api = async (m, p, b) => { if (m === "POST") { posted.push([p, b]); return { ok: true }; } return data.squelch; };
const box = new N("div");
SquelchPanel.mount(box);
for (let i = 0; i < 10; i++) await flush();
const south = all(box, (n) => n.tag === "input" && (n.attrs["aria-label"] || "").startsWith("South 2"));
const inputs = south.map((n) => ({ l: n.attrs["aria-label"], disabled: n.disabled, warn: n.className.includes("warn"),
                                   title: n.title }));
const close = south.find((n) => n.attrs["aria-label"] === "South 2 Close");
close.value = "13"; close.on.change[0]();
for (let i = 0; i < 10; i++) await flush();
console.log(JSON.stringify({ inputs, posted }));
"""


def test_paging_open_and_close_are_editable_for_an_admin_with_the_guard_on_hover(tmp_path):
    """Paging (sizeup/cad/settings.py _paging_guard): Open / Close inputs for an admin, Min stays the receiver's; an Open
    within 6 dB of the pages is amber; a Close goes up with the Open it sits under."""
    data = squelch_data()
    data["squelch"]["channels"][1]["running"] = {"open_db": 17.0, "close_db": 13.0, "min_open_s": 0.3}   # pages at 22
    out = run(tmp_path, PAGING_ROW, data)
    o, c, m = out["inputs"]
    assert (o["l"], o["disabled"], c["disabled"], m["disabled"]) == ("South 2 Open", False, False, True)
    assert o["warn"] and "Guard: idle max" in o["title"] and "pages 22 − 6 dB" in o["title"]
    assert "pages could be missed" in o["title"] and not c["warn"]
    assert out["posted"] == [["/api/cad/settings/squelch", {"action": "set", "key": "South 2", "close_db": 13,
                                                            "open_db": 17, "operator": "KM"}]]
    out = run(tmp_path, PAGING_ROW, squelch_data(admin=False))
    assert all(i["disabled"] for i in out["inputs"])                  # not an admin: read only


# -- Health -----------------------------------------------------------------------------------------------------------

def test_health_links_a_shift_hint_to_its_chart_and_draws_the_sparks(tmp_path):
    spark = {"subjects": [{"subject": "FG1", "am": False, "level": [5.0] * 47 + [9.0], "opens": [0.1] * 48},
                          {"subject": "p25", "rate": [40.0] * 48}]}
    data = {"now": NOW, "fetch": {"/api/cad/metrics?view=spark": {"status": 200, "body": spark},
                                  "/api/cad/health": {"status": 200, "body": {
                                      "health": {"reported": True, "health_reported": True, "channels": [{"label": "FG1", "state": "ok"}],
                                                 "trunk": {"last_call": NOW - 60, "quiet_s": 60, "window_s": 900, "gaps": 3},
                                                 "hints": [{"channel": "FG1", "level": "warn", "kind": "metrics_shift",
                                                            "text": "idle quieting +4.2 dB since 14:05 (SDR change)",
                                                            "link": "#settings/radios/signal?s=FG1&at=1799989200"},
                                                           {"channel": "FG2", "level": "info", "kind": "metrics_shift",
                                                            "text": "level not checked: too little traffic since 14:05 "
                                                                    "(station restart)",
                                                            "title": "kept voice in 4 half hours before, 2 after",
                                                            "link": "#settings/radios/signal?s=FG2&at=1799989200"}]},
                                      "alerts": [], "notify": {}, "thresholds": {}, "history": []}},
                                  "/api/cad/perf": {"status": 403, "body": {}}}}
    out = run(tmp_path, r"""
load("signal-chart.js");
global.fmtDur = (s) => String(s); global.sHead = (t) => [el("h2", "", t)];
global.Help = { info: () => el("span") };
load("health.js");
const box = new N("div");
StationHealth.mount(box);
for (let i = 0; i < 20; i++) await flush();
const hints = cls(box, "hl-hints")[0];
const links = all(hints, (n) => n.tag === "a").map((a) => [a.textContent, a.href]);
const sparks = cls(box, "hl-spark").map((a) => [a.href, cls(a, "sg-spark").map((s) => s.attrs["aria-label"])]);
const notes = hints.children.map((p) => [p.className, p.title]);
console.log(JSON.stringify({ hint: text(hints), links, sparks, notes }));
""", data)
    assert out["hint"].startswith("FG1: idle quieting +4.2 dB since 14:05 (SDR change)")
    assert out["links"] == [["Chart", "#settings/radios/signal?s=FG1&at=1799989200"],
                            ["Chart", "#settings/radios/signal?s=FG2&at=1799989200"]]
    # #686: a level that wasn't checked is an info line, why on hover
    assert out["notes"] == [["notice", ""], ["notice info", "kept voice in 4 half hours before, 2 after"]]
    assert out["sparks"] == [["#settings/radios/signal?s=p25", ["p25 decode rate 24 h · now 40 msg/s"]],
                             ["#settings/radios/signal?s=FG1", ["Idle q90 24 h · now 9 dB", "Noise opens/min 24 h · now 0.1"]]]


def test_seven_days_tick_on_local_midnights(tmp_path):
    out = run(tmp_path, r"""
load("signal-chart.js");
const t0 = 1800000000 - 7 * 86400;
const ticks = window.SignalChart.timeTicks(t0, 1800000000, 7);
const hm = (t) => new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit",
                                                      hourCycle: "h23" }).format(new Date(t * 1000));
console.log(JSON.stringify(ticks.map(hm)));
""", {"now": NOW, "fetch": {}})
    assert len(out) == 7 and set(out) == {"00:00"}                    # the console's midnights, not UTC's


# -- the re-review's follow-ups (#686) --------------------------------------------------------------------------------

def test_apply_sends_the_squelch_the_suggestion_was_for_not_what_the_card_shows(tmp_path):
    data = squelch_data()
    data["squelch"]["channels"][0]["running"] = {"open_db": 10.1, "close_db": 6.1, "min_open_s": 0.3}   # (near: Apply)
    out = run(tmp_path, SQUELCH, data)
    assert out["applies"] == 1
    # the suggestion's own cur_open / cur_close: the server compares them with what runs and refuses 10.1 / 6.1 itself
    assert out["posted"][0][1]["expect"] == {"open_db": 10, "close_db": 6}


def test_a_subject_two_stations_report_shows_the_one_with_data_in_its_window(tmp_path):
    data = squelch_data()
    chans = data["fetch"]["/api/cad/metrics?view=squelch"]["body"]["channels"]
    old = mi.suggest([], [], mi.DEFAULTS, NOW)                            # an old station id: no floor data in 7 days
    chans.append({**old, "subject": "FG1", "station": "old-id"})
    out = run(tmp_path, SQUELCH, data)
    assert out["cells"][0][0][0] == "13 dB Apply"                       # not blanked by the old id
    chans.append({**chans[0], "station": "pcfd-annex"})                 # two that both have data: which would Apply set?
    out = run(tmp_path, SQUELCH, data)
    assert out["cells"][0][0][0] == "—"


TICKS = r"""
load("signal-chart.js");
const fmt = (t) => new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(t * 1000));
console.log(JSON.stringify(data.spans.map(([a, b, n]) => window.SignalChart.timeTicks(a, b, n).map(fmt))));
"""


def test_ticks_stay_on_local_midnights_and_hours_across_a_dst_change(tmp_path):
    fall, spring = 1793509200, 1772953200        # 2026-11-01 05:00 UTC (01:00 EDT, the first 01:00), 2026-03-08 07:00 UTC (03:00 EDT)
    spans = [[fall - 4 * DAY, fall + 3 * DAY, 7], [spring - 3 * DAY, spring + 4 * DAY, 7],
             [fall - 12 * 3600, fall + 12 * 3600, 8], [spring - 12 * 3600, spring + 12 * 3600, 8]]
    week_f, week_s, day_f, day_s = run(tmp_path, TICKS, {"now": NOW, "fetch": {}, "spans": spans})
    for week, first in ((week_f, "Oct 29"), (week_s, "Mar 6")):
        assert len(week) == 7 and {w.split(", ")[1] for w in week} == {"00:00"}      # never 23:00 after the change
        assert week[0].startswith(first) and len({w.split(", ")[0] for w in week}) == 7   # a date each, none twice
    for day in (day_f, day_s):
        assert all(w.split(", ")[1].endswith(":00") and int(w.split(", ")[1][:2]) % 3 == 0 for w in day), day
