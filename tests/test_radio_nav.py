"""The Radio page's rail layout (GitHub #415, sizeup/web/static/radio-nav.js): what the rail shows (the views, the tree
system > group > channel, the unread counts from the hub's per-minute counts and when this viewer last looked), the
page it keeps (the first page, Older, what changed, one request at a time), the mute and emergency rules, the address,
the Our calls line and what Live is offered, the phone's sheet, and that the page as before is untouched until a viewer
turns the rail on.

The script runs in node against a stand-in page and hub (no browser, no app). Every source, talkgroup number, unit and
count here is invented."""

import json
import re
import subprocess

import pytest

from tests.test_radio_emergency import NODE, STATIC

HARNESS = r"""
const fs = require("fs");
const stored = { v: process.argv[3] === "-" ? null : process.argv[3] };
class El {
  constructor(tag, cls, text) { this.tagName = String(tag || "div").toUpperCase(); this.className = cls || "";
    this.textContent = text === undefined || text === null ? "" : String(text); this.children = []; this.dataset = {}; this.attrs = {};
    this.hidden = false; this.handlers = {}; this.parentElement = null; this.value = ""; this.title = ""; this.tabIndex = 0; this.focused = 0; }
  get childNodes() { return this.children; }
  get classList() { const n = this; const has = () => new Set(n.className.split(/\s+/).filter(Boolean));
    return { toggle(c, on) { const s = has(); if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c); n.className = [...s].join(" "); },
             contains: (c) => has().has(c), add(c) { this.toggle(c, true); }, remove(c) { this.toggle(c, false); } }; }
  append(...ns) { for (const x of ns) if (x && typeof x === "object") { x.parentElement = this; this.children.push(x); } }
  replaceChildren(...ns) { this.children = []; this.append(...ns); }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  fire(type, ev = {}) { for (const f of this.handlers[type] || []) f({ target: this, preventDefault() {}, stopPropagation() {}, ...ev }); }
  closest(sel) { let n = this; while (n) { if (sel === "button" ? n.tagName === "BUTTON" : sel.startsWith("[data-") ? sel.slice(6, -1) in n.dataset
    : n.classList.contains(sel.slice(1))) return n; n = n.parentElement; } return null; }
  querySelector() { return this.children.find((c) => c.tabIndex === 0) || null; }
  focus() { this.focused++; document.activeElement = this; }
  get text() { return [this.textContent, ...this.children.map((c) => c.text)].filter(Boolean).join(" "); }
}
const byId = {};
global.$ = (sel) => (byId[sel] = byId[sel] || new El("div"));
global.el = (tag, cls, text) => new El(tag, cls, text);
global.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
global.window = global; global.addEventListener = () => {}; global.dispatchEvent = () => {};
global.HashChangeEvent = class {};
const docKeys = [];
global.document = { visibilityState: "visible", activeElement: null, addEventListener(t, f) { if (t === "keydown") docKeys.push(f); } };
global.location = { hash: "#radio" };
global.history = { replaceState(a, b, h) { location.hash = h; } };
global.localStorage = { getItem: () => stored.v, setItem: (k, v) => { stored.v = v; } };
global.setTimeout = () => 1; global.clearTimeout = () => {};
global.syncList = (box, items, o) => { box.replaceChildren(...items.map((it) => { const n = o.make(it); n.dataset.key = o.key(it); o.fill(n, it); return n; })); };
global.setText = (n, t) => { n.textContent = t; };
global.show = (n, on) => { n.hidden = !on; };
global.fmtTime = (u) => `t${u}`;
const paged = [];
global.Radio = { fillPaging(box, paging, done) { paged.push([box === $("#rn-paging"), paging, done.size]); } };
const offered = [];
global.RadioLive = { offer(x) { offered.push(x.map((t) => t.id)); } };
global.RadioView = { rowSig: (t) => JSON.stringify([t.text]), row: (t) => { const li = new El("li", "rt"); li.append(new El("div", "txt", t.text)); return li; } };
// police made prominent here (Settings > Radios > Emergencies), so someone else's police emergency stands out; the
// default (fire, EMS) is the quiet scenario's
global.cad = { state: { comms_rev: "1", talking: [], alarms: { windows: [] }, paging: null,
                        radio_emergencies: { prominent: ["fire", "ems", "police"] } }, hooks: [], txRev: 0 };

// ---- the hub: made-up rows and sources; every request waits until the test lets it go -------------------------------
const NOW = 600000;
const row = (id, start, source, main, service) => ({ id, kind: "radio", start, end: start + 2, source, channel: source, main, service, text: `words ${id}`, status: "done", has_audio: true, events: [] });
const OURS = Array.from({ length: 230 }, (_, i) => row(`o${String(i).padStart(3, "0")}`, 1000 + i, "fm:FG1", true, "fire")).reverse();
const PD = Array.from({ length: 5 }, (_, i) => row(`p${i}`, 5000 + i, "p25:metro:9001", false, "police")).reverse();
const hub = { ours: OURS.slice(), pd: PD.slice(), changed: { rows: [], gone: [], reload: false }, cursor: 10 };
let SRC = [
  { key: "fm:FG1", system: "analog", system_label: "Analog", group: "Analog", name: "FG1", service: "fire", tier: "", ours: true, last: NOW - 30, counts: [[0, 2], [40, 5]], emergency: "", stuck: "" },
  { key: "p25:metro:9001", system: "metro", system_label: "Metro", group: "Police", name: "PD Dispatch", service: "police", tier: "alert", ours: false, last: NOW - 100, counts: [[1, 4]], emergency: "", stuck: "" },
  { key: "p25:metro:9003", system: "metro", system_label: "Metro", group: "Police", name: "PD Secure", service: "police", tier: "activity", ours: false, encrypted: true, last: NOW - 20, counts: [[0, 3]], emergency: "", stuck: "" },
  { key: "p25:metro:9100", system: "metro", system_label: "Metro", group: "Transit", name: "Rail Ops", service: "transit", tier: "record", ours: false, last: null, counts: [], emergency: "", stuck: "" },
  { key: "air:Tower", system: "airband", system_label: "Airband", group: "Airband", name: "Tower", service: "air", tier: "", ours: false, last: NOW - 50, counts: [[2, 7]], emergency: "", stuck: "" },
];
const calls = [], waiting = [];
function answer(path) {
  const u = new URL(path, "http://x"), q = u.searchParams;
  if (u.pathname.endsWith("/sources")) return q.get("rev") === "s1" ? { unchanged: true, rev: "s1", live: [] } : { rev: "s1", now: NOW, minutes: 60, live: [], sources: SRC };
  const all = q.get("view") === "ours" ? hub.ours : q.get("view") === "police" ? hub.pd : hub.ours.concat(hub.pd).sort((a, b) => b.start - a.start);
  if (q.get("changed")) return { ...hub.changed, cursor: hub.cursor + 1 };
  const limit = +q.get("limit") || 100;
  let rows = all;
  if (q.get("before")) { const [s, id] = q.get("before").split(","); rows = all.filter((t) => t.start < +s || (t.start === +s && t.id < id)); }
  const page = rows.slice(0, limit), last = page[page.length - 1];
  return { rows: page, older: rows.length > limit, next: last ? `${last.start},${last.id}` : "", cursor: hub.cursor, rev: "1", rigs: ["E71"], events: [] };
}
global.api = (method, path) => { calls.push(path); return new Promise((ok) => waiting.push(() => ok(answer(path)))); };
const tick = () => new Promise((r) => setImmediate(r));
async function drain(n = 50) { for (let i = 0; i < n; i++) { await tick(); const go = waiting.shift(); if (go) go(); else { await tick(); if (!waiting.length) break; } } await tick(); }
const comms = () => calls.filter((c) => c.startsWith("/api/cad/comms?"));
const last = (k) => new URLSearchParams(comms().filter((c) => !c.includes("limit=15")).slice(-1)[0].split("?")[1]).get(k);   // not the Our calls line's own

global.RADIO_NAV_TEST = process.argv[4] !== "plain";
eval(fs.readFileSync(process.argv[2], "utf8"));
if (process.argv[4] === "plain") { console.log(JSON.stringify({ test: "test" in window.RadioNav, keys: Object.keys(window.RadioNav) })); process.exit(0); }
const N = window.RadioNav, T = N.test, st = T.st;
const out = {};
const ids = () => st.rows.map((t) => t.id);

(async () => {
  // ---- pure ----
  const seenAll = Object.fromEntries(SRC.map((s) => [s.key, NOW - 20 * 60]));
  const p = { ...N.prefs(null), seen: seenAll, pins: ["p25:metro:9001"], mutes: ["air:Tower", "fm:FG1", "p25:metro:9003"] };
  const flagged = SRC.map((s) => (s.key === "p25:metro:9003" ? { ...s, emergency: "other", stuck: "CAR 9 (4100012)" } : s));
  const m = N.model(flagged, p, NOW, new Set(["p25:metro:9003"]), { "fm:FG1": 777 });
  const county = N.model(SRC.slice(0, 1), { ...N.prefs(null), seen: seenAll }, NOW);
  const many = Object.fromEntries(Array.from({ length: 1500 }, (_, i) => [`k${i}`, i + 1]));
  Object.assign(out, {
    fresh: N.prefs(null), stored: N.prefs(stored.v), bad: N.prefs("{not json"),
    wants: [N.wants(""), N.wants("ui=2"), N.wants("ui=1"), N.wants("event=26-000001")],
    views: m.views, systems: m.systems.map((s) => [s.label, s.groups.map((g) => [g.name, g.service, g.n, g.sources.map((x) => x.name)])]),
    pinned: m.pinned.map((s) => s.key), rows: Object.fromEntries(m.rows.map((s) => [s.key, [s.n, s.live, s.recent, s.mute, s.code, s.em, s.until]])),
    flaggedKeys: m.flagged.map((s) => s.key), mutesFor: N.mutesFor(p.mutes, SRC),
    unseen: N.unread(SRC[1], undefined, NOW), old: N.unread(SRC[0], NOW - 50 * 60, NOW), later: N.unread(SRC[0], NOW - 30 * 60, NOW),
    countyViews: county.views.map((v) => v.id),
    // nobody signed in: the hub's tree holds none of another system's sources; this browser's pins, mutes and looks at
    // them, a live key or an emergency window for one (the state push) bring none of it back
    hidden: (() => { const h = N.model(SRC.slice(0, 1), { ...N.prefs(null), pins: ["p25:metro:9001"], mutes: ["p25:metro:9003"],
                                                          seen: seenAll }, NOW, new Set(["p25:metro:9001"]), { "p25:metro:9100": 4242 });
                     return { views: h.views.map((v) => v.id), systems: h.systems.map((x) => x.label), rows: h.rows.map((x) => x.key),
                              pinned: h.pinned.map((x) => x.key), flagged: h.flagged.map((x) => x.key) }; })(),
    seenKept: [Object.keys(N.prefs(JSON.stringify({ seen: many })).seen).length, "k1499" in N.prefs(JSON.stringify({ seen: many })).seen, "k0" in N.prefs(JSON.stringify({ seen: many })).seen],
    // the default (fire, EMS): someone else's transit / police emergency is quiet; ours never is
    quiet: (() => { const q = N.model(SRC.map((s) => (s.key === "p25:metro:9100" ? { ...s, emergency: "other" }
                                                      : s.key === "p25:metro:9001" ? { ...s, emergency: "ours" }
                                                      : s.key === "p25:metro:9003" ? { ...s, emergency: "other" } : s)),
                                      N.prefs(null), NOW, new Set(), { "fm:FG1": 777, "air:Tower": 888 }, ["fire", "ems"]);
                    return { rows: Object.fromEntries(q.rows.map((s) => [s.key, [s.em, s.quiet]])), flagged: q.flagged.map((s) => s.key),
                             views: Object.fromEntries(q.views.map((v) => [v.id, v.em])) }; })(),
    merged: N.merged([{ id: "c", start: 3 }, { id: "b", start: 2 }], [{ id: "b", start: 2 }, { id: "a", start: 1 }, { id: "z", start: 9 }]).map((t) => t.id),
  });
  if (process.argv[4] === "pure") return console.log(JSON.stringify(out));

  // ---- the page: first page, Older, the 30 s page keeps what Older loaded ----
  N.open(""); await drain();
  out.first = { n: st.rows.length, top: ids()[0], older: st.older, view: last("view"), hash: location.hash, offered: offered.slice(-1)[0].length,
                listed: $("#rn-list").children.length, olderShown: !$("#rn-older").hidden, navOn: $("#radio-view").className, strip: $("#rn-ours").hidden };
  T.older(); await drain();
  T.older(); await drain();
  out.deep = { n: st.rows.length, older: st.older, bottom: ids().slice(-1)[0] };
  hub.ours.unshift(row("o999", 9000, "fm:FG1", true, "fire"));
  T.page(); await drain();                              // the safety page (every 30 s)
  out.afterSafety = { n: st.rows.length, top: ids()[0], bottom: ids().slice(-1)[0], dup: new Set(ids()).size === ids().length };

  // ---- what changed: patched, gone taken off, reload asks afresh ----
  hub.changed = { rows: [{ ...hub.ours[1], text: "the transcript, arrived" }, row("o998", 9500, "fm:FG1", true, "fire")], gone: ["o000"], reload: false };
  cad.state.comms_rev = "2"; cad.hooks.forEach((h) => h()); await drain();
  out.delta = { asked: last("changed") === null ? comms().some((c) => c.includes("changed=10")) : true, top: ids()[0], n: st.rows.length,
                text: st.rows.find((t) => t.id === hub.ours[1].id).text, gone: ids().includes("o000"), cursor: st.cursor };
  hub.changed = { rows: [], gone: [], reload: true };
  const before = comms().length;
  cad.state.comms_rev = "3"; cad.hooks.forEach((h) => h()); await drain();
  out.reload = comms().slice(before).map((c) => (c.includes("changed=") ? "changed" : c.includes("view=ours&limit=15") ? "strip" : "page"));

  // ---- a view picked while a request is out is never dropped ----
  hub.changed = { rows: [], gone: [], reload: false };
  cad.state.comms_rev = "4"; cad.hooks.forEach((h) => h());   // a delta goes out ...
  T.set({ view: "police", source: "" });                      // ... and a view is picked before it is back
  await drain();
  out.pickedDuringDelta = { view: last("view"), rows: ids(), strip: [!$("#rn-ours").hidden, $("#rn-ours").text] };
  T.set({ view: "ours", source: "" }); await drain();
  T.older();                                                  // Older goes out ...
  T.set({ view: "police", source: "" });                      // ... a view is picked
  await drain();
  out.pickedDuringOlder = { rows: ids(), busy: st.busy };
  // ---- Older clicked while a change request is out: done right after, not dropped ----
  T.set({ view: "ours", source: "" }); await drain();
  cad.state.comms_rev = "5"; cad.hooks.forEach((h) => h());   // a delta goes out ...
  T.older();                                                  // ... Older is clicked
  await drain();
  out.olderDuringDelta = { n: st.rows.length, asked: comms().some((c) => c.includes("before=") && comms().indexOf(c) > comms().findIndex((d) => d.includes("changed=11") || d.includes("changed=12"))) };
  T.set({ view: "police", source: "" }); await drain();

  // ---- Live: only Our calls, whatever is on screen ----
  out.liveInPolice = { last: offered.slice(-1)[0].slice(0, 2), anyPolice: offered.some((x) => x.some((id) => id.startsWith("p"))) };

  // ---- the address: filters kept, the view never; old links; a view change clears an old link's filters ----
  N.open("event=26-000012&channel=Fire%2FEMS+15&emergency=1&since=5"); await drain();
  out.oldLink = { view: st.f.view, f: [st.f.event, st.f.channel, st.f.emergency, st.f.since], hash: location.hash,
                  asked: ["view", "event", "channel", "emergency", "since"].map(last), chips: $("#rn-chips").children.map((c) => c.textContent) };
  $("#rn-chips").children[0].parentElement = $("#rn-chips");
  $("#rn-chips").handlers.click[0]({ target: $("#rn-chips").children[0] }); await drain();
  out.chipCleared = { channel: st.f.channel, emergency: st.f.emergency, hash: location.hash };
  T.set({ view: "police", source: "" }); await drain();
  out.viewChange = { f: [st.f.event, st.f.channel, st.f.emergency, st.f.since], hash: location.hash };
  N.open("view=police"); await drain();
  out.viewLink = { view: st.f.view, hash: location.hash };
  N.close(); N.open(""); await drain();                       // left and back, or a reload: the address holds no view
  out.reopened = st.f.view;

  // ---- mutes: never Our calls; an emergency always shows; the phone's sheet ----
  st.p.mutes = ["fm:FG1", "air:Tower", "p25:metro:9003"]; st.p.seen = Object.fromEntries(SRC.map((s) => [s.key, NOW - 20 * 60]));
  T.set({ view: "all", source: "" }); await drain();
  out.muteSent = last("mute");
  // a county fire talkgroup with an emergency window open and no alarm row (Windows.state's own shape)
  SRC = flagged.concat([{ key: "p25:westchester:4100015", system: "westchester", system_label: "County", group: "Fire", name: "Fire Ops 9",
                          service: "fire", tier: "", ours: false, mutable: false, last: NOW - 5, counts: [], emergency: "", stuck: "" }]);
  st.srcRev = ""; st.srcAt = 0;
  st.p.mutes = st.p.mutes.concat(["p25:westchester:4100015"]);
  cad.state.alarms.windows = [{ talkgroup: 4100015, channel: "Fire Ops 9", since: 4000, until: 4242, radios: [], alarm_id: null, ours: false, kept: 0 }];
  T.sources(true); await drain();
  out.mutesKept = st.p.mutes.slice();
  { const r = N.model(st.src, st.p, NOW, new Set(), { "p25:westchester:4100015": 4242 }).rows.find((x) => x.key === "p25:westchester:4100015");
    out.windowEm = r.em; out.windowMute = r.mute; }
  out.flags = { hidden: $("#rn-flags").hidden, text: $("#rn-flags").children.map((c) => [c.className, c.textContent]) };
  out.viewsEm = $("#rn-views").children.map((b) => [b.dataset.view, b.children.some((c) => c.className.includes("emergency"))]);
  out.sourcesButton = $("#rn-open").children.map((c) => [c.className, c.textContent]);
  $("#rn-open").fire("click");
  out.sheetOpen = [$("#rn-rail").classList.contains("sheet"), $("#rn-fold").textContent];
  for (const f of docKeys) f({ key: "Escape", stopPropagation() {} });
  out.sheetEscaped = $("#rn-rail").classList.contains("sheet");
  $("#rn-open").fire("click");
  const tab = $("#rn-views").children.find((b) => b.dataset.view === "police"); tab.tagName = "BUTTON";
  $("#rn-rail").handlers.click[0]({ target: tab }); await drain();
  out.sheetAfterPick = [$("#rn-rail").classList.contains("sheet"), st.f.view];
  const tabs = $("#rn-views").children;
  $("#rn-views").handlers.keydown[0]({ key: "ArrowDown", target: tabs[0], preventDefault() {} });
  out.arrow = [tabs[1].focused, tabs.map((t) => t.tabIndex)];

  st.p.seen["fm:FG1"] = NOW - 20 * 60; st.railSig = ""; T.render();     // in Police, with new traffic on Our calls
  out.sourcesButton2 = $("#rn-open").children.map((c) => [c.className, c.textContent]);
  SRC = SRC.map((s) => ({ ...s, emergency: "" })).concat([{ key: "p25:westchester:555", system: "westchester", system_label: "County", group: "Public Works", name: "County Other", service: "other", tier: "", ours: false, last: NOW - 5, counts: [], emergency: "ours", stuck: "" }]);
  cad.state.alarms.windows = []; st.srcRev = ""; st.srcAt = 0;
  T.sources(true); await drain();
  out.allOnlyEm = { views: $("#rn-views").children.filter((b) => b.children.some((c) => c.className.includes("emergency"))).map((b) => b.dataset.view),
                    button: $("#rn-open").children.map((c) => c.className) };
  // ---- the default (fire, EMS): a transit emergency, not ours, is a muted EM on its channel and nothing else ----
  cad.state.radio_emergencies = { prominent: ["fire", "ems"] };
  SRC = SRC.map((s) => ({ ...s, emergency: s.key === "p25:metro:9100" ? "other" : "" }));
  st.srcRev = ""; st.srcAt = 0;
  T.set({ view: "all", source: "" }); T.sources(true); await drain();
  const walk = (n) => [n, ...n.children.flatMap(walk)];
  out.quietDom = { flags: [$("#rn-flags").hidden, $("#rn-flags").children.length],
                   tree: walk($("#rn-tree")).filter((c) => /em-quiet|emergency/.test(c.className)).map((c) => [c.className, c.textContent, c.title]),
                   views: $("#rn-views").children.filter((b) => b.children.some((c) => /emergency|em-quiet/.test(c.className))).map((b) => b.dataset.view),
                   button: $("#rn-open").children.map((c) => c.className) };
  cad.state.radio_emergencies = { prominent: ["fire", "ems", "transit"] };     // transit made prominent: as before
  st.railSig = ""; st.flagSig = ""; T.render();
  out.loudDom = { flags: $("#rn-flags").children.map((c) => [c.className, c.textContent]) };
  // ---- a page going out now: over the list here too ----
  cad.state.paging = { channel: "South 2" }; cad.hooks.forEach((h) => h());
  out.paging = paged.slice(-1)[0].slice(0, 2);
  out.stored = N.prefs(stored.v);
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack); process.exit(1); });
"""


def run(tmp_path, stored="-", scenario="all"):
    h = tmp_path / "nav.js"
    h.write_text(HARNESS)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-nav.js"), stored, scenario], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    return run(tmp_path_factory.mktemp("nav"))


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_rail_is_off_until_a_viewer_turns_it_on_and_remembers(tmp_path):
    out = run(tmp_path, scenario="pure")
    assert out["fresh"] == {"on": False, "rail": True, "pins": [], "mutes": [], "seen": {}} == out["bad"]
    assert out["wants"] == [False, True, False, False]            # the page as before unless asked: ?ui=2
    stored = json.dumps({"on": True, "rail": False, "pins": ["fm:FG1"], "mutes": ["air:Tower"], "seen": {"fm:FG1": 5}})
    again = run(tmp_path, stored, "pure")
    assert again["stored"] == {"on": True, "rail": False, "pins": ["fm:FG1"], "mutes": ["air:Tower"], "seen": {"fm:FG1": 5}}
    assert again["wants"] == [True, True, False, True]            # on: an old link opens in it too; ?ui=1: as before
    assert out["seenKept"] == [1000, True, False]                 # the newest thousand: a busy tree never reads as unseen


def test_views_tree_and_unread_counts(out):
    views = {v["id"]: v for v in out["views"]}
    assert list(views) == ["ours", "police", "transit", "air", "all"]          # no EMS source: no EMS view
    assert out["countyViews"] == ["ours", "all"]                               # the county's fire alone: nothing new to pick
    # Our calls: 2 new this minute; FG1's 5 from 40 minutes ago are older than the last look (20 minutes ago)
    assert (views["ours"]["n"], views["ours"]["mark"]) == (2, "F")
    assert (views["police"]["n"], views["police"]["live"]) == (4, False)       # PD Secure is muted: no count, no live dot
    assert views["air"]["n"] == 0 and views["all"]["n"] == 0                   # muted: no count; All: never a count
    assert out["systems"] == [["Analog", [["Analog", "fire", 2, ["FG1"]]]],
                              ["Metro", [["Police", "police", 4, ["PD Dispatch", "PD Secure"]], ["Transit", "transit", 0, ["Rail Ops"]]]],
                              ["Airband", [["Airband", "air", 0, ["Tower"]]]]]
    assert out["pinned"] == ["p25:metro:9001"]
    rows = out["rows"]
    assert rows["p25:metro:9001"] == [4, False, True, False, "PA", "", None]
    assert rows["air:Tower"][:4] == [0, False, True, True]
    assert rows["p25:metro:9100"][:3] == [0, False, False]
    assert (out["unseen"], out["old"], out["later"]) == (0, 7, 2)   # never seen: nothing new; counted only after the look
    assert out["merged"] == ["c", "b", "a"]                         # the fresh page, then only what is older than it


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_rail_shows_no_other_system_the_hub_did_not_send(tmp_path):
    """#419's gate on the rail: the tree is only what GET /api/cad/comms/sources sent (nothing of another P25 system
    to a reader who is not signed in); what this browser remembers of such a source never puts it, or its view, back."""
    hidden = run(tmp_path, scenario="pure")["hidden"]
    assert hidden == {"views": ["ours", "all"], "systems": ["Analog"], "rows": ["fm:FG1"], "pinned": [], "flagged": []}


def test_mute_never_hides_our_calls_or_an_emergency(out):
    rows, views = out["rows"], {v["id"]: v for v in out["views"]}
    assert rows["fm:FG1"][3] is False and out["mutesFor"] == ["air:Tower", "p25:metro:9003"]     # one of Our calls: never muted
    assert rows["p25:metro:9003"][3] is True and rows["p25:metro:9003"][5] == "other"          # muted, and its emergency known
    assert views["police"]["em"] == "other" and views["all"]["em"] == "other"                  # ... shows on its views
    assert rows["fm:FG1"][5:] == ["", 777] and views["ours"]["em"] == ""                       # a window alone: no mark
    assert out["flaggedKeys"] == ["fm:FG1", "p25:metro:9003"]                                  # (its Recording until chip)
    # in the page: the hub is never asked to mute one of Our calls, and the mute of one is dropped
    assert out["muteSent"] == "air:Tower,p25:metro:9003" and out["mutesKept"] == ["air:Tower", "p25:metro:9003"]
    # the emergency over the list in every view, with its window; on the views; on the phone's Sources button
    assert out["flags"]["hidden"] is False
    # the window (no alarm row): its Recording until chip, never an emergency mark; a fire channel of ours: never muted
    assert out["flags"]["text"] == [["chip emergency em-other em-stuck", "Emergency flag stuck on CAR 9 (4100012): PD Secure"],
                                    ["chip kept", "Recording until t4242 (emergency)"]]
    assert out["windowEm"] == "" and out["windowMute"] is False and "p25:westchester:4100015" not in out["mutesKept"]
    assert dict(out["viewsEm"])["police"] is True and dict(out["viewsEm"])["all"] is True and dict(out["viewsEm"])["ours"] is False
    assert not any("emergency" in c for c, _ in out["sourcesButton"])     # in All it is on screen: Sources marks what isn't


def test_the_page_keeps_what_older_loaded_and_patches_what_changed(out):
    f = out["first"]
    assert (f["n"], f["top"], f["older"], f["view"], f["listed"], f["olderShown"]) == (100, "o229", True, "ours", 100, True)
    assert f["hash"] == "#radio" and "nav-on" in f["navOn"] and f["strip"] is True       # Our calls: no line about itself
    assert out["deep"] == {"n": 230, "older": False, "bottom": "o000"}
    # the page asked again (every 30 s): the new row on top, and the 130 older rows Older loaded are still there
    assert out["afterSafety"] == {"n": 231, "top": "o999", "bottom": "o000", "dup": True}
    d = out["delta"]
    assert d["asked"] and d["top"] == "o998" and d["text"] == "the transcript, arrived" and d["gone"] is False
    assert d["n"] == 231 and d["cursor"] == 11                               # one in, one gone
    assert out["reload"][:2] == ["changed", "page"]                          # reload: the page is asked afresh


def test_a_view_picked_while_a_request_is_out_is_not_dropped(out):
    a, b = out["pickedDuringDelta"], out["pickedDuringOlder"]
    assert a["view"] == "police" and a["rows"] == ["p4", "p3", "p2", "p1", "p0"]
    assert b["rows"] == ["p4", "p3", "p2", "p1", "p0"] and b["busy"] is False   # Older's answer for Our calls is not mixed in
    c = out["olderDuringDelta"]
    assert c["asked"] and c["n"] > 100                                          # Older clicked meanwhile: asked right after


def test_our_calls_stay_on_top_and_live_plays_only_them(out):
    shown, text = out["pickedDuringDelta"]["strip"]
    assert shown and "Our calls" in text and "words o999" in text           # the newest of Our calls, in one line
    assert out["first"]["offered"] == 15
    assert out["liveInPolice"] == {"last": ["o999", "o229"], "anyPolice": False}


def test_the_address_keeps_filters_never_the_view(out):
    o = out["oldLink"]
    assert o["view"] == "all" and o["f"] == ["26-000012", "Fire/EMS 15", "1", "5"]
    assert o["asked"] == ["all", "26-000012", "Fire/EMS 15", "1", "5"]       # every filter of an old link reaches the hub
    assert "view=" not in o["hash"] and "event=26-000012" in o["hash"] and "channel=" in o["hash"]
    assert o["chips"] == ["Fire/EMS 15 ✕", "Emergency ✕", "From t5 ✕"]
    assert out["chipCleared"]["channel"] == "" and out["chipCleared"]["emergency"] == "1" and "channel" not in out["chipCleared"]["hash"]
    v = out["viewChange"]
    assert v["f"] == ["26-000012", "", "", ""] and v["hash"] == "#radio?event=26-000012"    # the old link's own: gone with the view
    assert out["viewLink"] == {"view": "police", "hash": "#radio"}           # a link may name a view; it is never written back
    assert out["reopened"] == "ours"                                         # back on the Radio page, a reload: Our calls


def test_the_phone_sheet_the_keys_and_the_page_going_out(out):
    assert out["sheetOpen"] == [True, "✕"] and out["sheetEscaped"] is False     # Escape closes it
    assert out["sheetAfterPick"] == [False, "police"]                           # a view picked: the sheet is put away
    assert out["arrow"][0] == 1 and out["arrow"][1].count(0) == 1               # the arrows move between the views: one tab stop
    assert out["paging"] == [True, {"channel": "South 2"}]                      # the page going out now, over this list too
    assert ["badge rn-n", "2"] in out["sourcesButton2"]                         # the other views' new traffic on Sources
    # an emergency on a channel that is in no view but All: on All, and on the phone's Sources button all the same
    assert out["allOnlyEm"]["views"] == ["all"] and "chip emergency" in out["allOnlyEm"]["button"]


def test_the_page_as_before_is_untouched_until_the_rail_is_on():
    html = (STATIC / "cad.html").read_text()
    assert re.search(r'<div id="rn" class="rn" hidden>', html), "the rail layout is in the page, hidden"
    assert html.index('src="/static/radio-view.js"') < html.index('src="/static/radio-nav.js"')
    for bid in ("rn-flags", "rn-paging", "rn-chips", "rn-ours", "rn-older"):
        assert f'id="{bid}"' in html
    css = (STATIC / "cad.css").read_text()
    assert ".radio-view.nav-on > :is(.rv-filters, .rv-sec) { display: none; }" in css     # only then is the old page put away
    view = (STATIC / "radio-view.js").read_text()
    assert "if (window.RadioNav && RadioNav.wants(qs)) { close(true); return RadioNav.open(qs); }" in view
    assert "if (!b || (window.RadioNav && RadioNav.on)) return;" in view                    # an incident chip: handled once
    assert 'window.RadioNav && RadioNav.on ? $("#rn-q") : $("#rv-q")' in (STATIC / "cad.js").read_text()   # "/": the box on screen
    nav = (STATIC / "radio-nav.js").read_text()
    assert "/api/cad/comms?view=ours&limit=15" in nav and "redacted" not in nav


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_page_never_carries_the_scripts_insides(tmp_path):
    out = run(tmp_path, scenario="plain")
    assert out["test"] is False and "test" not in out["keys"]
    assert "RADIO_NAV_TEST" not in "".join((STATIC / f).read_text() for f in ("cad.html", "cad.js", "radio-view.js"))


def test_someone_elses_emergency_off_the_prominent_services_is_quiet(out):
    """Settings > Radios > Emergencies (default fire, EMS): a transit or police emergency that isn't ours is a muted EM
    on its channel only: no banner over the list, no mark on its view or Sources. Ours always stands out; a service
    made prominent stands out as before."""
    q = out["quiet"]
    assert q["rows"]["p25:metro:9100"] == ["other", True] and q["rows"]["p25:metro:9003"] == ["other", True]
    assert q["rows"]["p25:metro:9001"] == ["ours", False] and q["rows"]["fm:FG1"] == ["", False]   # ours; our window
    assert q["rows"]["air:Tower"] == ["", True]                     # a window alone on a quiet service: quiet too
    assert q["flagged"] == ["fm:FG1", "p25:metro:9001"]
    assert q["views"]["transit"] == "" and q["views"]["police"] == "ours"
    d = out["quietDom"]
    assert d["flags"] == [True, 0] and d["views"] == [] and d["button"] == [""]
    assert d["tree"] == [["chip em-quiet", "EM", "Emergency (not ours)"]]          # the detail in the hover
    assert out["loudDom"]["flags"] == [["chip emergency em-other", "Emergency (not ours): Rail Ops"]]
