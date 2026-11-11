"""The Radio page's rail layout (GitHub #415, sizeup/web/static/radio-nav.js): what the rail shows (the views, the tree
system > group > channel, the unread counts from the hub's per-minute counts and when this viewer last looked), what is
remembered per viewer, and that the page as before is untouched until a viewer turns the rail on.

The script's pure part runs in node with no page. Every source, talkgroup number and count here is invented."""

import json
import re
import subprocess

import pytest

from tests.test_radio_emergency import NODE, STATIC

HARNESS = r"""
const fs = require("fs");
global.window = global;
global.localStorage = { getItem: () => process.argv[3] === "-" ? null : process.argv[3], setItem() {} };
eval(fs.readFileSync(process.argv[2], "utf8"));
const N = window.RadioNav, now = 600000;
const counts = (at, n) => { const c = new Array(60).fill(0); c[59 - at] = n; return c; };   // n calls `at` minutes ago
const src = [
  { key: "p25:westchester:307", system: "westchester", system_label: "County", group: "Fire", name: "Fire Main", service: "fire", tier: "text", ours: true, last: now - 30, counts: counts(0, 2) },
  { key: "fm:FG1", system: "analog", system_label: "Analog", group: "Analog", name: "FG1", service: "fire", tier: "", ours: true, last: now - 4000, counts: counts(40, 5) },
  { key: "p25:metro:9001", system: "metro", system_label: "Metro", group: "Police", name: "PD Dispatch", service: "police", tier: "alert", ours: false, last: now - 100, counts: counts(1, 4) },
  { key: "p25:metro:9003", system: "metro", system_label: "Metro", group: "Police", name: "PD Secure", service: "police", tier: "activity", ours: false, encrypted: true, last: now - 20, counts: counts(0, 3) },
  { key: "p25:metro:9100", system: "metro", system_label: "Metro", group: "Transit", name: "Rail Ops", service: "transit", tier: "record", ours: false, last: null, counts: [] },
  { key: "air:Tower", system: "airband", system_label: "Airband", group: "Airband", name: "Tower", service: "air", tier: "", ours: false, last: now - 50, counts: counts(2, 7) },
];
const seenAll = Object.fromEntries(src.map((s) => [s.key, now - 20 * 60]));     // looked 20 minutes ago
const p = { ...N.prefs(null), seen: seenAll, pins: ["p25:metro:9001"], mutes: ["air:Tower", "fm:FG1"] };
const m = N.model(src, p, now, new Set(["p25:metro:9003"]), new Set(["p25:westchester:307"]));
const county = N.model(src.slice(0, 2), { ...N.prefs(null), seen: seenAll }, now);
console.log(JSON.stringify({
  fresh: N.prefs(null), stored: N.prefs(process.argv[3] === "-" ? null : process.argv[3]), bad: N.prefs("{not json"),
  wants: [N.wants(""), N.wants("ui=2"), N.wants("ui=1"), N.wants("event=26-000001")],
  views: m.views, systems: m.systems.map((s) => [s.label, s.groups.map((g) => [g.name, g.service, g.n, g.sources.map((x) => x.name)])]),
  pinned: m.pinned.map((s) => s.key), rows: Object.fromEntries(m.rows.map((s) => [s.key, [s.n, s.live, s.recent, s.mute, s.code, s.em]])),
  unseen: N.unread(src[2], undefined, now), old: N.unread(src[1], now - 50 * 60, now), later: N.unread(src[1], now - 30 * 60, now),
  countyViews: county.views.map((v) => v.id),
  inView: [N.inView(src[0], "ours"), N.inView(src[2], "ours"), N.inView(src[2], "police"), N.inView(src[0], "fire"), N.inView(src[5], "all")],
}));
"""


def run(tmp_path, stored="-"):
    h = tmp_path / "nav.js"
    h.write_text(HARNESS)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-nav.js"), stored], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_rail_is_off_until_a_viewer_turns_it_on_and_remembers(tmp_path):
    out = run(tmp_path)
    assert out["fresh"] == {"on": False, "rail": True, "pins": [], "mutes": [], "seen": {}} == out["bad"]
    assert out["wants"] == [False, True, False, False]            # the page as before unless asked: ?ui=2
    stored = json.dumps({"on": True, "rail": False, "pins": ["fm:FG1"], "mutes": ["air:Tower"], "seen": {"fm:FG1": 5}})
    again = run(tmp_path, stored)
    assert again["stored"] == {"on": True, "rail": False, "pins": ["fm:FG1"], "mutes": ["air:Tower"], "seen": {"fm:FG1": 5}}
    assert again["wants"] == [True, True, False, True]            # on: an old link opens in it too; ?ui=1: as before


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_views_tree_and_unread_counts(tmp_path):
    out = run(tmp_path)
    views = {v["id"]: v for v in out["views"]}
    assert list(views) == ["ours", "police", "transit", "air", "all"]          # no EMS source: no EMS view
    assert out["countyViews"] == ["ours", "all"]                               # the county's fire alone: nothing new to pick
    # Our calls: 2 new on the main talkgroup + FG1's 5 from 40 minutes ago are older than the last look (20 minutes)
    assert (views["ours"]["n"], views["ours"]["em"], views["ours"]["mark"]) == (2, True, "F")
    assert (views["police"]["n"], views["police"]["live"]) == (7, True)
    assert views["air"]["n"] == 0 and views["all"]["n"] == 0                    # muted: no count; All: never a count
    assert out["systems"] == [["County", [["Fire", "fire", 2, ["Fire Main"]]]], ["Analog", [["Analog", "fire", 0, ["FG1"]]]],
                              ["Metro", [["Police", "police", 7, ["PD Dispatch", "PD Secure"]], ["Transit", "transit", 0, ["Rail Ops"]]]],
                              ["Airband", [["Airband", "air", 0, ["Tower"]]]]]
    assert out["pinned"] == ["p25:metro:9001"]
    rows = out["rows"]
    assert rows["p25:metro:9001"] == [4, False, True, False, "PA", False]
    assert rows["p25:metro:9003"] == [3, True, True, False, "ENC", False]
    assert rows["p25:westchester:307"][5] is True and rows["p25:westchester:307"][4] == "TX"
    assert rows["air:Tower"][:4] == [0, False, True, True]
    assert rows["fm:FG1"][3] is False                               # one of Our calls is never muted
    assert rows["p25:metro:9100"][:3] == [0, False, False]
    assert (out["unseen"], out["old"], out["later"]) == (0, 5, 0)   # never seen: nothing new; counted only after the look
    assert out["inView"] == [True, False, True, False, True]


def test_the_page_as_before_is_untouched_until_the_rail_is_on():
    html = (STATIC / "cad.html").read_text()
    rn = re.search(r'<div id="rn" class="rn" hidden>', html)
    assert rn, "the rail layout is in the page, hidden"
    assert html.index('src="/static/radio-view.js"') < html.index('src="/static/radio-nav.js"')
    css = (STATIC / "cad.css").read_text()
    assert ".radio-view.nav-on > :is(.rv-filters, .rv-sec) { display: none; }" in css     # only then is the old page put away
    view = (STATIC / "radio-view.js").read_text()
    assert "if (window.RadioNav && RadioNav.wants(qs)) { close(true); return RadioNav.open(qs); }" in view
    nav = (STATIC / "radio-nav.js").read_text()
    assert 'f.view = VIEWS.some(([id]) => id === v) ? v : f.event || f.unit || f.channel || f.q ? "all" : "ours";' in nav
    assert "/api/cad/comms?view=ours&limit=15" in nav and "RadioLive.offer(st.ours" in nav    # Live: Our calls only
    assert "localStorage" in nav and "view:" not in re.search(r"return \{ on: .*?\};", nav).group(0)   # the view isn't kept
