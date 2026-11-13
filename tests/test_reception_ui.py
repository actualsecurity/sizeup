"""The reception test's card (GitHub #436, sizeup/web/static/radio-controls.js): where it is and how it reads.

    where       Settings > Radios > Receivers only: settings.js mounts RadioControls with { reception: true, tab }, the
                card is shown (and /api/reception fetched) only while the tab is Receivers; the station's own page
                mounts it without, so it has no reception UI at all (the hub drives the test through the station)
    targets     one select: the county's site, each side of each system, "both sides"; never an FM or airband channel
    stopped     a test that was stopped, failed or interrupted is a grey "stopped at 3m" chip, no verdict colour, the
                reason in its hover; a finished side that heard nothing is the red "None"; "—" for no decode rate
    TG hits/h   the system's alert talkgroups' grants per hour, which talkgroups in the hover
    the note    one input, on the start form; in the history read-only text with a pencil that edits it in place
                (Enter or leaving the field saves through {"action": "note"}, Escape puts it back)

radio-controls.js runs in node with a small fake DOM (no browser); every value is invented.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
NOW = 1_800_000_000.0


def text(name):
    return (STATIC / name).read_text()


# -- where it is ------------------------------------------------------------------------------------------------------


def test_only_settings_mounts_the_card_and_the_station_page_has_none():
    st = text("settings.js")
    assert "RadioControls.mount(rc, { reception: true, tab: S.rtab })" in st
    assert re.search(r'RadioControls\.mount\(\$\("#radio-controls"\)\);', text("station.js"))   # no reception option
    assert "reception" not in re.search(r"RadioControls\.mount\([^)]*\)", text("station.js")).group(0)
    rc = text("radio-controls.js")
    assert "if (opts.reception) {" in rc and 'n.rxCard = tab("receivers", card("Reception test"' in rc
    assert rc.count('card("Reception test"') == 1 and rc.count('"/api/reception", { cache') == 1
    assert 'st.n.rxCard && st.tab === "receivers" && !ro()' in rc        # fetched only while the card is shown
    assert "Reception" not in text("station.html").split("<main id=\"v-radios\"")[1].split("</main>")[0]


# -- the DOM ----------------------------------------------------------------------------------------------------------

DOM = r"""
const fs = require("fs");
let focused = null;
class Cls {
  constructor(n) { this.n = n; }
  get list() { return this.n.className.split(/\s+/).filter(Boolean); }
  add(...c) { this.n.className = [...new Set([...this.list, ...c])].join(" "); }
  remove(...c) { this.n.className = this.list.filter((x) => !c.includes(x)).join(" "); }
  toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); return on; }
  contains(c) { return this.list.includes(c); }
}
function match(n, sel) {
  return sel.split(",").some((one) => {
    const m = one.trim().match(/^([a-z0-9]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:="[^"]*")?\])*)$/i);
    if (!m) throw new Error("selector " + one);
    if (m[1] && n.tagName !== m[1].toUpperCase()) return false;
    for (const c of m[2].split(".").filter(Boolean)) if (!n.classList.contains(c)) return false;
    for (const a of m[3].match(/\[[^\]]+\]/g) || []) {
      const [, k, v] = a.match(/\[([\w-]+)(?:="([^"]*)")?\]/);
      const d = k.startsWith("data-") ? n.dataset[k.slice(5).replace(/-(\w)/g, (_, x) => x.toUpperCase())] : n.attrs[k];
      if (d === undefined || (v !== undefined && String(d) !== v)) return false;
    }
    return true;
  });
}
class N {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentNode = null; this.className = "";
    this._t = ""; this.title = ""; this.hidden = false; this.dataset = {}; this.attrs = {}; this.on = {}; this.style = {};
    this.value = ""; this.disabled = false; this.classList = new Cls(this);
  }
  get isConnected() { return true; }
  get offsetParent() { return this.hidden ? null : {}; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((k) => k.textContent).join(""); }
  append(...xs) { for (const x of xs) { const k = typeof x === "string" ? Object.assign(new N("#text"), { _t: x }) : x; if (k.parentNode) k.remove(); k.parentNode = this; this.children.push(k); } }
  prepend(...xs) { const old = this.children; this.children = []; this.append(...xs); this.children.push(...old); }
  replaceChildren(...xs) { for (const k of this.children) k.parentNode = null; this.children = []; this.append(...xs); }
  insertBefore(x, ref) { if (x.parentNode) x.remove(); x.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; i < 0 ? this.children.push(x) : this.children.splice(i, 0, x); }
  remove() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter((k) => k !== this); this.parentNode = null; } }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  removeEventListener() {}
  fire(t, ev = {}) { const e = Object.assign({ type: t, target: this, preventDefault() { this.prevented = true; } }, ev); let n = this; while (n) { for (const f of n.on[t] || []) f(e); n = ev.bubbles ? n.parentNode : null; } return e; }
  click() { if (this.disabled) return; if (this.tagName === "BUTTON" && focused !== this) { const was = focused; focused = this; if (was) was.fire("blur"); } this.fire("click", { bubbles: true }); }   // a browser focuses a clicked button
  focus() { focused = this; }
  blur() { if (focused === this) { focused = null; this.fire("blur"); } }
  select() {}
  all() { return this.children.flatMap((k) => [k, ...k.all()]); }
  querySelectorAll(sel) { return this.all().filter((k) => k.tagName !== "#TEXT" && match(k, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { let n = this; while (n) { if (n.tagName !== "#TEXT" && match(n, sel)) return n; n = n.parentNode; } return null; }
  contains(x) { while (x) { if (x === this) return true; x = x.parentNode; } return false; }
}
global.window = global;
global.document = { createElement: (t) => new N(t), createElementNS: (_ns, t) => new N(t), visibilityState: "visible",
                    get activeElement() { return focused; }, addEventListener() {} };
global.Option = class extends N { constructor(t, v) { super("option"); this._t = t; this.value = v; } };
global.el = (tag, cls, txt) => { const e = new N(tag); if (cls) e.className = cls; if (txt !== undefined && txt !== null) e.textContent = txt; return e; };
global.setText = (n, t) => { t = t ?? ""; if (n.textContent !== t) n.textContent = t; };
global.show = (n, on) => { n.hidden = !on; };
global.fmtTime = (t) => `T${t}`;
global.syncList = () => {};
global.liveMerge = (a, b) => b;
global.canDo = () => true;
global.Help = { button: (t) => Object.assign(new N("button"), { title: t, className: "help" }) };
global.UI = { confirm: async () => true };
global.api = async () => { throw new Error("no api here"); };
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const asked = [];
global.fetch = async (url, o = {}) => {
  asked.push([url, o.body ? JSON.parse(o.body) : null]);
  if (url.startsWith("/api/reception")) return { ok: true, status: 200, json: async () => ({ reception: IN.rx }) };
  return { ok: false, status: 503, json: async () => ({}) };
};
global.setTimeout = (f, ms) => 0;
global.clearTimeout = () => {};
eval(fs.readFileSync(process.argv[2], "utf8"));
const tick = () => new Promise((ok) => setImmediate(ok));
const hrows = (c) => c.querySelector("table.rx-history").querySelectorAll("tr");
const card = (box) => box.children.find((c) => (c.textContent || "").startsWith("Reception test"));
(async () => {
  const out = {};
  // the station's page: mounted without the reception option
  const station = new N("div");
  RadioControls.mount(station);
  await tick(); await tick();
  out.station = { card: !!card(station), asked: asked.some(([u]) => u.startsWith("/api/reception")) };
  // Settings > Radios > Channels, then Receivers
  asked.length = 0;
  const box = new N("div");
  RadioControls.mount(box, { reception: true, tab: "channels" });
  await tick(); await tick();
  out.channels = { card: !!card(box), hidden: card(box).hidden, asked: asked.some(([u]) => u.startsWith("/api/reception")) };
  RadioControls.mount(box, { reception: true, tab: "receivers" });
  await tick(); await tick(); await tick();
  const c = card(box);
  out.receivers = { hidden: c.hidden, asked: asked.some(([u]) => u === "/api/reception"),
                    tab: c.dataset.rtab, cards: box.children.filter((k) => (k.textContent || "").startsWith("Reception test")).length };
  // the target select: every trunked P25 target, nothing else
  out.targets = c.querySelector("select.rx-target").children.map((o) => [o.value, o.textContent]);
  out.targetTips = c.querySelector("select.rx-target").children.map((o) => o.title);
  out.notes = c.querySelectorAll("input").filter((i) => /rx-note/.test(i.className)).map((i) => i.className);
  // the history
  const rows = hrows(c).filter((r) => r.dataset.id);
  out.rows = rows.map((r) => {
    const tds = r.children;
    const res = r.querySelector("span.rx-result");
    return { id: r.dataset.id, cls: r.className, target: tds[1].textContent,
             result: res.children.map((p) => ({ text: p.textContent, cls: p.className, title: p.title })),
             decoded: tds[6].textContent, hits: tds[7].textContent, hitsTitle: tds[7].title,
             note: tds[8].querySelector("span.rx-note-text").textContent, pen: !!tds[8].querySelector("button.rx-note-pen"),
             inputs: tds[8].querySelectorAll("input").length };
  });
  // the note: the pencil, Enter saves through {"action": "note"}
  asked.length = 0;
  let row = hrows(c).find((r) => r.dataset.id === IN.edit);
  row.querySelector("button.rx-note-pen").click();
  row = hrows(c).find((r) => r.dataset.id === IN.edit);
  const inp = row.querySelector("input.rx-note-edit");
  out.editing = { value: inp.value, focused: document.activeElement === inp };
  // a poll while typing never redraws the field away
  RadioControls._rx.renderReception();
  out.typingKept = c.querySelector("input.rx-note-edit") === inp;
  inp.value = "  yagi on the mast, north  ";
  const e = inp.fire("keydown", { key: "Enter" });
  out.enter = { prevented: !!e.prevented };
  await tick(); await tick();
  out.posted = asked.filter(([u, b]) => u === "/api/reception" && b).map(([, b]) => b);
  // Escape puts it back, nothing sent
  asked.length = 0;
  row = hrows(c).find((r) => r.dataset.id === IN.edit);
  row.querySelector("button.rx-note-pen").click();
  row = hrows(c).find((r) => r.dataset.id === IN.edit);
  const inp2 = row.querySelector("input.rx-note-edit");
  inp2.value = "nope";
  inp2.fire("keydown", { key: "Escape" });
  await tick();
  out.escape = { posted: asked.filter(([u, b]) => b).length, input: !!c.querySelector("input.rx-note-edit") };
  // leaving the field saves too
  asked.length = 0;
  row = hrows(c).find((r) => r.dataset.id === IN.edit);
  row.querySelector("button.rx-note-pen").click();
  const inp3 = c.querySelector("input.rx-note-edit");
  inp3.value = "on the roof";
  inp3.blur();
  await tick(); await tick();
  out.blur = asked.filter(([u, b]) => b).map(([, b]) => b);
  // the duration in a chip
  out.dur = [5, 59, 180, 3599, 3900].map(RadioControls._rx.rxDur);
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
"""


def half(job, secs, windows, pct, rate, grade, hint="", per_h=None, grants=0, alert=None):
    return {"job": job, "seconds": secs, "grade": grade, "hint": hint, "cc_mhz": 770.18125 if windows else None,
            "rate": rate, "decoded_pct": pct if windows else None, "windows": windows, "dropout_s": 0, "retunes": 0,
            "site": None, "cc_over_db": None, "voice_snr_db": None, "watch": [], "calls": 0, "err_per_s": None,
            "encrypted_grants": 0, "alert": {"tgs": alert or [], "grants": grants, "per_h": per_h} if alert else None}


COUNTY_ALERT = [{"tg": 307, "name": "Fire/EMS 15"}, {"tg": 333, "name": "TG 333"}]
MRRS_ALERT = [{"tg": 9211, "name": "PD Dispatch"}]
RX = {
    "systems": [{"name": "probe-westchester", "label": "County", "kind": "county",
                 "jobs": [{"job": "County probe", "mhz": 477.19375, "cc": [476.075, 476.1125]}], "watch": [307, 333], "alert": COUNTY_ALERT, "caveat": ""},
                {"name": "mrrs", "label": "MRRS", "kind": "system",
                 "jobs": [{"job": "MRRS 770", "mhz": 770.3125, "cc": [769.90625, 770.18125]},
                          {"job": "MRRS 773", "mhz": 773.08125, "cc": [772.48125]}],
                 "watch": [9211], "alert": MRRS_ALERT, "caveat": ""}],
    "durations": [15, 60, 240, 0], "keep_min": 10.0,
    "dongles": [{"serial": "spare", "name": "spare", "job": "Off", "refuse": "", "protected": ""},
                {"serial": "trunk", "name": "trunk", "job": "Fire/EMS 15", "refuse": "protected: not lendable",
                 "protected": "county fire dispatch (the P25 trunk)"}],
    "active": None, "alert": None, "samples": [],
    "history": [
        {"id": "a00000000001", "system": "mrrs", "label": "MRRS", "sdr": "spare", "sdr_name": "spare", "was": "Off",
         "plan": ["MRRS 770"], "minutes": 60, "started": NOW - 400, "ended": NOW - 220, "status": "stopped",
         "why": "stopped by a person", "note": "", "restored": "Off again", "restored_ok": True, "verdict": None,
         "halves": [half("MRRS 770", 180, 0, 0.0, None, None, alert=MRRS_ALERT)]},
        {"id": "a00000000002", "system": "mrrs", "label": "MRRS", "sdr": "spare", "sdr_name": "spare", "was": "Off",
         "plan": ["MRRS 770"], "minutes": 15, "started": NOW - 2000, "ended": NOW - 1100, "status": "done", "why": "",
         "note": "indoor whip", "restored": "Off again", "restored_ok": True, "verdict": {"grade": "none"},
         "halves": [half("MRRS 770", 900, 300, 0.0, 0.4, "none", "No control channel on the 770 side",
                         per_h=0.0, alert=MRRS_ALERT)]},
        {"id": "a00000000003", "system": "probe-westchester", "label": "County", "sdr": "spare", "sdr_name": "spare",
         "was": "Off", "plan": ["County probe"], "minutes": 60, "started": NOW - 9000, "ended": NOW - 5400,
         "status": "done", "why": "", "note": "whip on the roof", "restored": "Off again", "restored_ok": True,
         "verdict": {"grade": "good"},
         "halves": [half("County probe", 3600, 1200, 98.2, 39.1, "good", per_h=4.0, grants=4, alert=COUNTY_ALERT)]},
        {"id": "a00000000004", "system": "mrrs", "label": "MRRS", "sdr": "spare", "sdr_name": "spare", "was": "Off",
         "plan": ["MRRS 773"], "minutes": 60, "started": NOW - 20000, "ended": NOW - 19280, "status": "interrupted",
         "why": "sizeup stopped without ending the test (a crash, a kill, the power)", "note": "",
         "restored": "Off set back at the restart", "restored_ok": True, "verdict": {"grade": "marginal"},
         "halves": [half("MRRS 773", 720, 240, 40.0, 18.0, "marginal", "Control channel decodes 40% of the time",
                         per_h=5.0, grants=1, alert=MRRS_ALERT)]},
        {"id": "a00000000005", "system": "probe-westchester", "label": "County", "sdr": "spare", "sdr_name": "spare",
         "was": "Off", "plan": ["County probe"], "minutes": 240, "started": NOW - 30000, "ended": NOW - 29960,
         "status": "failed", "why": "trunk-recorder absent", "note": "", "restored": "Off again",
         "restored_ok": True, "verdict": None, "halves": [half("County probe", 40, 0, 0.0, None, None, alert=COUNTY_ALERT)]},
    ],
}


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    d = tmp_path_factory.mktemp("rxui")
    (d / "h.js").write_text(DOM)
    (d / "in.json").write_text(json.dumps({"rx": RX, "edit": "a00000000003"}))
    r = subprocess.run([NODE, str(d / "h.js"), str(STATIC / "radio-controls.js"), str(d / "in.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_card_is_on_receivers_only_and_never_on_the_station_page(ui):
    assert ui["station"] == {"card": False, "asked": False}                   # no card, no /api/reception
    assert ui["channels"] == {"card": True, "hidden": True, "asked": False}   # Settings > Radios > Channels: hidden
    assert ui["receivers"] == {"hidden": False, "asked": True, "tab": "receivers", "cards": 1}


def test_the_targets_are_the_county_and_each_side_of_each_system(ui):
    # each by its first control channel (not the dongle's centre); the others in the hover
    assert ui["targets"] == [["probe-westchester|County probe", "County · 476.075"],
                             ["mrrs|MRRS 770", "MRRS 770 · 769.90625"], ["mrrs|MRRS 773", "MRRS 773 · 772.48125"],
                             ["mrrs|MRRS 770,MRRS 773", "MRRS both sides"]]
    assert ui["targetTips"][0] == "Control channels 476.075, 476.1125 MHz (dongle centred on 477.19375)"
    assert ui["targetTips"][3] == "MRRS 770: 769.90625, 770.18125\nMRRS 773: 772.48125"
    for fm in ("FG1", "South 2", "HPN Tower", "Fire/EMS 15"):
        assert not any(fm in t for t, _ in ui["targets"])


def test_one_antenna_note_input_on_the_start_form_and_none_in_the_history(ui):
    assert ui["notes"] == ["sm rx-note"]
    assert all(r["inputs"] == 0 and r["pen"] for r in ui["rows"])


def test_a_stopped_run_is_a_grey_chip_and_a_finished_one_that_heard_nothing_is_a_red_none(ui):
    rows = {r["id"]: r for r in ui["rows"]}
    stopped, none, good, crash, failed = (rows[f"a0000000000{i}"] for i in range(1, 6))
    assert stopped["result"] == [{"text": "stopped at 3m", "cls": "pill rx-ended", "title": "Stopped: stopped by a person"}]
    assert stopped["decoded"] == "—" and stopped["hits"] == "—" and stopped["cls"] == "rx-unfinished"
    assert none["result"][0]["text"] == "None" and none["result"][0]["cls"].split()[-1] == "err"
    assert none["result"][0]["title"] == "No control channel on the 770 side" and none["decoded"] == "0%"
    assert good["result"][0]["cls"].split()[-1] == "ok" and good["decoded"] == "98.2%" and good["cls"] == ""
    # an interrupted one keeps what it measured, in the hover, with no verdict colour
    assert crash["result"][0]["text"] == "interrupted at 12m" and crash["result"][0]["cls"] == "pill rx-ended"
    assert crash["result"][0]["title"].startswith("Restored after a crash or restart: sizeup stopped without ending")
    assert "MRRS 773: marginal. Control channel decodes 40% of the time" in crash["result"][0]["title"]
    assert failed["result"][0]["text"] == "failed at 40s" and failed["result"][0]["title"] == "Failed: trunk-recorder absent"
    assert not any(c in p["cls"].split() for r in (stopped, crash, failed) for p in r["result"] for c in ("ok", "err", "warn"))
    assert ui["dur"] == ["5s", "59s", "3m", "59m", "1h05"]


def test_tg_hits_per_hour_is_the_systems_alert_talkgroups(ui):
    rows = {r["id"]: r for r in ui["rows"]}
    county, mrrs = rows["a00000000003"], rows["a00000000004"]
    assert county["hits"] == "4" and county["hitsTitle"] == "Grants per hour on 307 Fire/EMS 15, 333 TG 333"
    assert mrrs["hits"] == "5" and mrrs["hitsTitle"] == "Grants per hour on 9211 PD Dispatch"
    assert county["target"] == "County" and mrrs["target"] == "MRRS 773"
    js = text("radio-controls.js")
    assert "9211 /h" not in js and "${watch} /h" not in js and '["TG hits/h",' in js


def test_the_note_is_edited_in_place_and_saved_by_the_note_action(ui):
    rows = {r["id"]: r for r in ui["rows"]}
    assert rows["a00000000003"]["note"] == "whip on the roof" and rows["a00000000001"]["note"] == "—"
    assert ui["editing"] == {"value": "whip on the roof", "focused": True} and ui["typingKept"]
    assert ui["enter"] == {"prevented": True}
    assert ui["posted"] == [{"action": "note", "id": "a00000000003", "note": "yagi on the mast, north"}]
    assert ui["escape"] == {"posted": 0, "input": False}
    assert ui["blur"] == [{"action": "note", "id": "a00000000003", "note": "on the roof"}]


def test_the_dongle_rows_wrap_inside_the_card():
    css = text("settings.css")
    rule = re.search(r"\.sdrs \.sdr-row \{(.*?)\}", css, re.S).group(1)
    assert "display: grid" in rule and "minmax(0, 1fr)" in rule
    assert re.search(r"\.sdr-btns \{[^}]*flex-wrap: wrap[^}]*min-inline-size: 0", css)
    assert ".pill.rx-ended { color: var(--dim)" in css
