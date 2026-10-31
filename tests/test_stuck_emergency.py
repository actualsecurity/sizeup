"""A flag that won't go away (#190, user 2026-10-15: "these were not actual emergencies, so our check for the bus dot
dispatch is wrong, also i cant minimize this"). DOT Dispatch 1 (TG 1325, the buses) showed "Emergency (not ours)" and
300 TX "kept: emergency window" of routine traffic. trunk-recorder 5.2.0 flags a call from the grant that opened it, so
a flag on call after call is the system's (a radio left in emergency mode, or the talkgroup held in emergency): someone
else's flag is raised once, then called stuck (alarms.Windows.flag) and renews nothing; someone else's window keeps at
most WINDOW_MAX_KEPT calls; ours (fire channels, our rigs) is untouched. And the Radio view's Other talkgroups and each
talkgroup in it fold when their caret is clicked and stay folded, per viewer."""

import json
import re
import subprocess
import time

import numpy as np
import pytest

from sizeup.cad.alarms import (ROUTINE_CALL_S, STUCK_REPEATS, STUCK_TG_FLAGS, STUCK_WITHIN_S, WINDOW_MAX_KEPT, WINDOW_S,
                               Windows)
from sizeup.radio.p25 import Call, Turn
from tests.test_alarms import tx
from tests.test_cad import make_cad
from tests.test_radio_emergency import NODE, STATIC, Clock, make, rows_of


@pytest.fixture(autouse=True)
def _notify_stays_local(monkeypatch):
    import sizeup.notify as notify

    def refuse(req, *a, **kw):
        raise AssertionError(f"no pushes in these tests: {req.full_url}")
    monkeypatch.setattr(notify, "urlopen", refuse)


def test_the_constants_say_what_the_issue_asked():
    assert 5 * 60 <= WINDOW_S <= 10 * 60                       # a short time after the last real flag
    assert 0 < WINDOW_MAX_KEPT < 300                            # never the 300 TX of #190
    assert STUCK_REPEATS >= 2 and STUCK_TG_FLAGS > STUCK_REPEATS and STUCK_WITHIN_S >= 5 * 60
    assert 16 < ROUTINE_CALL_S                                   # #190's bus calls (0-16 s) are routine-length


# -- the rule (alarms.Windows.flag) ------------------------------------------------------------------------------


def test_a_radio_flagging_routine_call_after_call_is_stuck_once():
    c = Clock()
    w = Windows(WINDOW_S, c)
    seen = [w.flag(1325, ["21237"], c.t + 60 * k, 2.0, ours=False) for k in range(6)]
    assert [s["stuck"] for s in seen] == [""] * (STUCK_REPEATS - 1) + ["radio"] * (6 - STUCK_REPEATS + 1)
    assert [s["first"] for s in seen].count(True) == 1 and seen[STUCK_REPEATS - 1]["first"]     # said once
    assert seen[-1]["radios"] == ["21237"]
    # stuck while it goes on (a long call too), until it has been quiet STUCK_WITHIN_S
    assert w.flag(1325, ["21237"], c.t + 400, 45.0, ours=False)["stuck"] == "radio"
    assert w.flag(1325, ["21237"], c.t + 400 + STUCK_WITHIN_S + 1, 2.0, ours=False)["stuck"] == ""
    # another radio on the same talkgroup is its own (one flag: real)
    assert Windows(WINDOW_S, c).flag(1325, ["21000"], c.t, 2.0, ours=False)["stuck"] == ""


def test_flags_far_apart_or_held_open_are_real():
    c = Clock()
    w = Windows(WINDOW_S, c)
    far = [w.flag(1325, ["21237"], c.t + (STUCK_WITHIN_S + 5) * k, 2.0, ours=False)["stuck"] for k in range(5)]
    assert far == [""] * 5
    hot = [w.flag(1325, ["21500"], c.t + 30 * k, ROUTINE_CALL_S + 15, ours=False)["stuck"] for k in range(5)]
    assert hot == [""] * 5                                                  # a hot mic held open: never routine


def test_a_talkgroup_flagged_whoever_talks_is_stuck():
    """Every grant on the talkgroup flagged (the system holds it in emergency): many radios, one flag each."""
    c = Clock()
    w = Windows(WINDOW_S, c)
    got = [w.flag(1325, [f"21{k:03d}"], c.t + 20 * k, 3.0, ours=False) for k in range(STUCK_TG_FLAGS + 2)]
    assert [g["stuck"] for g in got] == [""] * (STUCK_TG_FLAGS - 1) + ["talkgroup"] * 3
    assert [g["first"] for g in got].count(True) == 1
    assert w.flag(383, ["21000"], c.t + 200, 3.0, ours=False)["stuck"] == ""   # another talkgroup: its own


def test_ours_is_never_stuck():
    c = Clock()
    w = Windows(WINDOW_S, c)
    assert all(w.flag(307, ["2391001"], c.t + 20 * k, 2.0, ours=True)["stuck"] == "" for k in range(20))


def test_someone_elses_window_keeps_at_most_its_cap_ours_has_none():
    c = Clock()
    w = Windows(WINDOW_S, c)
    w.open(1325, "DOT Dispatch 1", ["21237"], c.t, ours=False)
    kept = [w.keeps(1325, ["1325001"], c.t + 5 * k) for k in range(WINDOW_MAX_KEPT + 10)]
    assert kept == ["talkgroup"] * WINDOW_MAX_KEPT + [""] * 10
    assert w.keeps(383, ["21237"], c.t + 100) == ""                          # its radio elsewhere: the same cap
    w.open(307, "Fire/EMS 15", ["2391001"], c.t, ours=True)
    assert all(w.keeps(307, ["1"], c.t + k) == "talkgroup" for k in range(WINDOW_MAX_KEPT * 3))
    # ours once on the window (the CAD of record says so): no cap from then on
    w.open(1325, "DOT Dispatch 1", ["21237"], c.t + 200, ours=True)
    assert w.keeps(1325, [], c.t + 300) == "talkgroup"


# -- the station (App._call) ---------------------------------------------------------------------------------------


def bus(cid, at, src="21237", tag="BUS237", em=True, length=2.0, tg=1325, label="DOT Dispatch 1"):
    n = int(length * 16000)
    return Call(cid, tg, label, at, at + length, [tag], np.full(n, 0.2, np.float32), "",
                [Turn(0.0, tag, src, tag, length, at)], {tag: (src, tag)}, "westchester", emergency=em)


def done(app, t, text="copy"):
    t.raw, t.status, t.confidence = text, "done", 0.9
    app._transcribed(t)


def test_a_stuck_bus_radio_raises_once_and_stops_renewing_the_window(tmp_path):
    """#190 as it happened: Record off on DOT Dispatch 1, BUS237's radio flagging every routine call."""
    app, texts = make(tmp_path)
    app.tg.set(1325, record=False)
    base = time.time() - 25 * 60
    for k in range(20):                                                     # a flagged call every minute for 20 min
        app._call(bus(f"1325-{k}", base + 60 * k))
    ids = [x.id for x in texts]
    real = [x for x in texts if getattr(x, "emergency", False)]
    stuck = [x for x in texts if getattr(x, "flag_stuck", "")]
    assert [x.id for x in real] == [f"1325-{k}" for k in range(STUCK_REPEATS - 1)]
    assert all(x.flag_stuck == "BUS237 (21237)" and not getattr(x, "emergency", False) for x in stuck)
    # kept only while the window from the last real flag runs, then dropped: not renewed by the stuck flags
    last_real = base + 60 * (STUCK_REPEATS - 2) + 2
    assert ids == [f"1325-{k}" for k in range(20) if base + 60 * k <= last_real + WINDOW_S]
    assert all(x.kept == "emergency" for x in texts)
    for x in texts:
        done(app, x, "alright, happy")
    alarms = app.cad.alarms.history()
    assert len(alarms) == 1 and alarms[0]["level"] == "notice" and alarms[0]["stuck"] == "BUS237 (21237)"
    rows = rows_of(app.cad)
    assert rows["1325-0"]["emergency"] == 1 and rows["1325-0"]["alarm_stuck"] == "BUS237 (21237)"
    assert all(rows[x.id]["emergency"] == 0 for x in stuck)                 # only flagged calls that counted
    # the record of the call that showed it stuck is kept for audit (no WAV / JSON here: nothing to move)
    state = app.cad.state()["alarms"]
    assert [n["stuck"] for n in state["notices"]] == ["BUS237 (21237)"]


def test_the_mark_is_made_even_when_the_stuck_call_itself_is_dropped(tmp_path):
    """The window already full (WINDOW_MAX_KEPT) when the flag shows stuck: the call goes, its alarm is marked anyway."""
    app, texts = make(tmp_path)
    app.tg.set(1325, record=False)
    base = time.time() - 20 * 60
    for k in range(STUCK_REPEATS - 1):
        app._call(bus(f"1325-{k}", base + 10 * k))
    for k in range(WINDOW_MAX_KEPT):
        app._call(bus(f"1325-r{k}", base + 30 + 2 * k, src="1325001", tag="DOT Disp", em=False))
    n = len(texts)
    app._call(bus("1325-stuck", base + 200))
    assert len(texts) == n                                                   # dropped: Record off, the window full
    assert [a["stuck"] for a in app.cad.alarms.history()] == ["BUS237 (21237)"]


def test_one_flag_then_routine_traffic_is_a_bounded_window(tmp_path):
    app, texts = make(tmp_path)
    app.tg.set(1325, record=False)
    base = time.time() - 20 * 60
    app._call(bus("1325-em", base))                                          # one flagged call: real
    assert texts[0].emergency
    for k in range(WINDOW_MAX_KEPT + 20):                                    # routine calls, every 5 s
        app._call(bus(f"1325-r{k}", base + 10 + 5 * k, src="1325001", tag="DOT Disp", em=False))
    kept = [x.id for x in texts[1:]]
    assert kept == [f"1325-r{k}" for k in range(WINDOW_MAX_KEPT)]           # at most the cap
    assert all(getattr(x, "kept", "") == "emergency" and not getattr(x, "emergency", False) for x in texts[1:])
    # and nothing after the window's time, cap or not
    app2, texts2 = make(tmp_path / "b")
    app2.tg.set(1325, record=False)
    app2._call(bus("1325-em", base))
    app2._call(bus("1325-late", base + 2 + WINDOW_S + 1, src="1325001", tag="DOT Disp", em=False))
    assert [x.id for x in texts2] == ["1325-em"]


def test_ours_is_unchanged_every_flag_counts_and_the_window_has_no_cap(tmp_path):
    """Fire/EMS 15 (TG 307, one of our fire channels) with Record off: every flag an emergency, every call kept."""
    app, texts = make(tmp_path)
    app.tg.set(307, record=False)
    base = time.time() - 20 * 60
    for k in range(STUCK_REPEATS + 3):
        app._call(bus(f"307-{k}", base + 30 * k, src="2391001", tag="E64 P1", tg=307, label="Fire/EMS 15"))
    assert all(getattr(x, "emergency", False) and not getattr(x, "flag_stuck", "") for x in texts)
    n = len(texts)
    for k in range(WINDOW_MAX_KEPT + 10):
        app._call(bus(f"307-r{k}", base + 300 + 2 * k, src="2391002", tag="E62", em=False, tg=307, label="Fire/EMS 15"))
    assert len(texts) == n + WINDOW_MAX_KEPT + 10
    for x in texts[:n]:
        done(app, x, "")
    alarms = app.cad.alarms.history()
    assert alarms and all(a["level"] == "alarm" and not a["stuck"] for a in alarms)
    # our rig on someone else's talkgroup is ours too
    pcfd = app.cad.radios.pcfd[0]
    w = app.cad.alarms
    assert w.counts_as_ours({"talkgroup": 1325, "rig": pcfd}) and not w.counts_as_ours({"talkgroup": 1325, "rig": ""})
    assert w.counts_as_ours({"talkgroup": 307}) and w.counts_as_ours({"talkgroup": None, "source": "FG1"})


def test_not_ours_is_a_notice_raised_once_ours_an_alarm_each_time(tmp_path):
    cad = make_cad(tmp_path)
    at = time.time() - 600
    a1 = cad.alarms.on_traffic(dict(tx("1325-1", source="DOT Dispatch 1", talkgroup=1325, radio_id="21237",
                                       emergency=True), start=at, end=at + 2))
    cad.alarm_ack(a1["id"], {"operator": "Corey"})                       # seen ...
    a2 = cad.alarms.on_traffic(dict(tx("1325-2", source="DOT Dispatch 1", talkgroup=1325, radio_id="21237",
                                       emergency=True), start=at + 240, end=at + 242))
    assert a1["level"] == "notice" and a2["id"] == a1["id"]               # ... and the same radio again: still one
    o1 = cad.alarms.on_traffic(dict(tx("307-1", source="Fire/EMS 15", talkgroup=307, radio_id="2391001",
                                       emergency=True), start=at, end=at + 2))
    cad.alarm_ack(o1["id"], {"operator": "Corey"})
    o2 = cad.alarms.on_traffic(dict(tx("307-2", source="Fire/EMS 15", talkgroup=307, radio_id="2391001",
                                       emergency=True), start=at + 240, end=at + 242))
    assert o1["level"] == o2["level"] == "alarm" and o2["id"] != o1["id"]  # ours: as before (#128)
    win = {w["talkgroup"]: w for w in cad.state()["alarms"]["windows"]}
    assert win[1325]["ours"] is False and win[307]["ours"] is True
    # the stuck mark: once, on the radio's alarm; a talkgroup's on the newest there
    m = cad.alarms.stuck(dict(tx("1325-3", source="DOT Dispatch 1", talkgroup=1325, radio_id="21237"),
                              stuck="BUS237 (21237)"))
    assert m["id"] == a1["id"] and m["stuck"] == "BUS237 (21237)"
    again = cad.alarms.stuck(dict(tx("1325-4", talkgroup=1325, radio_id="21237"), stuck="DOT Dispatch 1 (TG 1325)"))
    assert again["stuck"] == "BUS237 (21237)"
    assert cad.alarms.stuck(dict(tx("383-1", talkgroup=383, radio_id="1"), stuck="x")) is None   # no alarm there
    # one of our rigs on that talkgroup: its alarm is never the one called stuck
    pcfd = cad.radios.pcfd[0]
    ours = cad.alarms.on_traffic(dict(tx("359-1", source="WCPD PSD", talkgroup=359, radio_id="2391009", rig=pcfd,
                                         emergency=True), start=at, end=at + 2))
    assert ours["level"] == "alarm"
    assert cad.alarms.stuck(dict(tx("359-2", talkgroup=359, radio_id="21999"), stuck="WCPD PSD (TG 359)")) is None


def test_the_stuck_mark_reaches_the_hub_with_the_transcript(tmp_path):
    """A station forwarding to a hub: the stuck call's transmission carries flag_stuck, the hub marks the alarm."""
    from tests.test_cloud import HubRun, call as http, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        at = time.time() - 120
        d = {"id": "1325-1-call_1", "source": "DOT Dispatch 1", "start": at, "end": at + 2, "text": "", "status": "no_speech",
             "unit": "BUS237", "talkgroup": 1325, "rig": "", "radio_id": "21237", "emergency": True,
             "pairs": [["21237", "BUS237"]], "attach": False, "raw": "", "model": ""}
        s = dict(d, id="1325-3-call_3", start=at + 60, end=at + 62, emergency=False, flag_stuck="BUS237 (21237)",
                 text="alright, happy", raw="alright, happy", status="done")
        code, out = http(hub.url + "/api/station/events", "POST",
                         {"epoch": "e1", "events": [{"seq": 1, "at": at, "type": "alarm", "data": d},
                                                    {"seq": 2, "at": at, "type": "traffic", "data": d},
                                                    {"seq": 3, "at": at + 60, "type": "traffic", "data": s}]})
        assert code == 200 and out["applied"] == 3
        rows = rows_of(hub.app.cad)
        assert rows["1325-1-call_1"]["alarm_stuck"] == "BUS237 (21237)" and rows["1325-3-call_3"]["emergency"] == 0
        assert len(hub.app.cad.alarms.history()) == 1
        # the station's mark at the flag itself: an "alarm" event that says it isn't one (the call may be dropped)
        hub.app.cad.store.db.execute("UPDATE alarms SET stuck = ''")
        m = {k: v for k, v in s.items() if k not in ("text", "raw")} | {"id": "1325-4-call_4", "text": ""}
        code, out = http(hub.url + "/api/station/events", "POST",
                         {"epoch": "e1", "events": [{"seq": 4, "at": at + 90, "type": "alarm", "data": m}]})
        assert code == 200 and [a["stuck"] for a in hub.app.cad.alarms.history()] == ["BUS237 (21237)"]
        assert len(hub.app.cad.alarms.history()) == 1
    finally:
        hub.close()


# -- the page: someone else's emergency low-key, and the carets fold for good ---------------------------------------


def test_someone_elses_group_is_not_marked_like_ours():
    css = (STATIC / "cad.css").read_text()
    assert re.search(r"\.og\.emergency\.em-other > \.og-head \{ box-shadow: none; \}", css)
    assert ".og.emergency.em-ours > .og-head { box-shadow: inset" in css
    assert ".chip.emergency.em-other.em-stuck {" in css and ".alarm-stuck {" in css
    rv = (STATIC / "radio-view.js").read_text()
    assert "Emergency flag stuck on ${g.stuck}" in rv and 'g.emergency === "ours" ? 2' in rv
    assert "a.stuck" in (STATIC / "alarm.js").read_text() and "t.alarm_stuck" in (STATIC / "cad.js").read_text()


def test_the_fold_is_remembered_per_viewer_in_the_source():
    rv = (STATIC / "radio-view.js").read_text()
    # what the viewer folded wins over what a filter would open; kept in the existing per-viewer key
    assert "const otherOpen = (rest) => !st.other.shut && (st.other.open || (active() && rest.length > 0));" in rv
    assert "const groupOpen = (g, few) => !st.other.shutTgs.has(g.key) && (st.other.tgs.has(g.key) || few);" in rv
    assert "const open = otherOpen(rest);" in rv and "const isOpen = groupOpen(g, few);" in rv
    assert 'OTHER_KEY = "sizeup.cad.radio.other"' in rv and "shut_tgs: last(st.other.shutTgs)" in rv
    assert 'head.getAttribute("aria-expanded") === "true"' in rv
    assert "st.other.tgs.has(g.key) || few;" not in rv.replace("(st.other.tgs.has(g.key) || few);", "")
    lines = rv.splitlines()
    for i, ln in enumerate(lines):                                   # storage only ever inside a try
        if "localStorage." in ln:
            assert any("try {" in lines[j] for j in range(max(0, i - 3), i + 1)), ln.strip()
    # nothing opens it for an emergency
    assert not re.search(r"emergency[^\n]*st\.other\.(open|tgs\.add)", rv)


COLLAPSE_HARNESS = r"""
const fs = require("fs");
const [src, storeIn, scenario] = [process.argv[2], process.argv[3], process.argv[4]];
const store = { v: storeIn === "-" ? null : storeIn };
class El {
  constructor(tag, cls, text) { this.tagName = String(tag || "div").toUpperCase(); this.className = cls || "";
    this.textContent = text === undefined ? "" : String(text); this.children = []; this.dataset = {}; this.attrs = {};
    this.hidden = false; this.handlers = {}; this.parentElement = null; this.value = ""; this.checked = false;
    this.options = []; this.title = ""; }
  get childNodes() { return this.children; }
  get classList() { const n = this; const has = () => new Set(n.className.split(/\s+/).filter(Boolean));
    return { toggle(c, on) { const s = has(); if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c); n.className = [...s].join(" "); },
             contains: (c) => has().has(c), add(c) { this.toggle(c, true); }, remove(c) { this.toggle(c, false); } }; }
  append(...ns) { for (const x of ns) if (x && typeof x === "object") { x.parentElement = this; this.children.push(x); } }
  replaceChildren(...ns) { this.children = []; this.append(...ns); }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); }
  fire(target) { for (const f of this.handlers.click || []) f({ target: target || this, preventDefault() {} }); }
  closest(sel) { let n = this; while (n) { if (n.classList.contains(sel.slice(1))) return n; n = n.parentElement; } return null; }
  querySelector() { return new El("span"); }
  reset() {}
}
const byId = {};
global.$ = (sel) => (byId[sel] = byId[sel] || new El("div"));
global.el = (tag, cls, text) => new El(tag, cls, text);
global.Option = class { constructor(t, v) { this.text = t; this.value = v; } };
global.window = global; global.addEventListener = () => {};
global.document = { visibilityState: "visible", addEventListener() {} };
global.location = { hash: "#radio" }; global.history = { replaceState() {} };
global.localStorage = { getItem: () => store.v, setItem: (k, v) => { store.v = v; } };
global.Audio = class { addEventListener() {} pause() {} play() { return Promise.resolve(); } };
global.syncList = (box, items, o) => {
  const old = new Map(box.children.map((c) => [c.dataset.key, c]));
  box.replaceChildren(...items.map((it) => { const k = o.key(it); let n = old.get(k);
    if (!n) { n = o.make(it); n.dataset.key = k; } o.fill(n, it); return n; }));
};
global.radioRow = () => new El("li", "rt");
global.setText = (n, t) => { n.textContent = t; };
global.show = (n, on) => { n.hidden = !on; };
for (const n of ["evChip", "opSpan"]) global[n] = () => new El("span");
global.talkSig = () => ""; global.emSig = () => []; global.fmtTime = () => "13:02";
global.toLocalInput = () => ""; global.fromLocalInput = () => null; global.Radio = { fillPaging() {} };
global.cad = { state: {}, hooks: [], txRev: 0 };
let rows = [
  { id: "c", kind: "radio", main: false, talkgroup: 1325, channel: "DOT Dispatch 1", start: 300, text: "Alright, happy.", status: "done", kept: "emergency" },
  { id: "b", kind: "radio", main: false, talkgroup: 1325, channel: "DOT Dispatch 1", start: 200, text: "", status: "no_speech",
    emergency: 1, alarm_id: 7, alarm_level: "notice", alarm_stuck: "BUS237 (21237)" },
];
let rev = 1;
global.api = async () => ({ rows, rev: String(rev) });
eval(fs.readFileSync(src, "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 0));
const sec = $("#rv-other-sec"), box = $("#rv-other");
const snap = () => { const li = box.children[0]; return { sec: sec.classList.contains("open"), box: !box.hidden,
  group: li ? li.className : null, expanded: li ? li.children[0].getAttribute("aria-expanded") : null,
  chip: li ? li.children[0].children[3].children.map((c) => [c.className, c.textContent]) : null,
  badge: $("#rv-other-em").textContent }; };
const live = async () => {                    // a live update: a new transmission, the push, the hooks
  rows = [{ id: "n" + rev, kind: "radio", main: false, talkgroup: 1325, channel: "DOT Dispatch 1", start: 400 + rev,
            text: "block 12", status: "done", kept: "emergency" }, ...rows];
  rev += 1; cad.txRev += 1; cad.state = { comms_rev: String(rev) };
  for (const h of cad.hooks) h();
  await tick(); await tick();
};
(async () => {
  const out = {};
  const filtered = scenario !== "plain";
  if (filtered) { location.hash = "#radio?channel=DOT%20Dispatch%201"; }
  RadioView.open(filtered ? "channel=DOT+Dispatch+1" : "");
  if (filtered) $("#rv-channel").value = "DOT Dispatch 1";
  RadioView.render(); await tick(); await tick();
  out.start = snap();
  if (scenario === "fold") {
    const head = box.children[0].children[0];
    box.fire(head);                          // the talkgroup's caret
    out.groupFolded = snap();
    await live(); RadioView.render();
    out.groupAfterLive = snap();
    $("#rv-other-toggle").fire();             // Other talkgroups' caret
    out.sectionFolded = snap();
    await live(); RadioView.render();
    out.sectionAfterLive = snap();
  }
  if (scenario === "reopen") {
    $("#rv-other-toggle").fire();
    out.opened = snap();
    box.fire(box.children[0].children[0]);
    out.groupOpened = snap();
  }
  out.stored = store.v;
  RadioView.close();
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def run_collapse(tmp_path, scenario, stored=None):
    h = tmp_path / "collapse.js"
    h.write_text(COLLAPSE_HARNESS)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-view.js"), stored or "-", scenario], capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_carets_fold_and_stay_folded_through_live_updates(tmp_path):
    out = run_collapse(tmp_path, "fold")
    s = out["start"]                                          # the filter opens the section and its one talkgroup
    assert s["sec"] and s["box"] and s["expanded"] == "true" and "open" in s["group"].split()
    # someone else's (stuck) emergency: its chip, low-key, and the head not marked like ours
    assert s["chip"] == [["chip emergency em-other em-stuck", "Emergency flag stuck on BUS237 (21237)"]]
    assert "em-other" in s["group"] and s["badge"] == "Emergency flag stuck on BUS237 (21237): DOT Dispatch 1"
    g = out["groupFolded"]
    assert g["expanded"] == "false" and "open" not in g["group"].split() and g["sec"]
    assert out["groupAfterLive"]["expanded"] == "false"       # a new transmission: still folded
    f = out["sectionFolded"]
    assert not f["sec"] and not f["box"]
    assert not out["sectionAfterLive"]["sec"]                 # the filter doesn't open it again
    stored = json.loads(out["stored"])
    assert stored["shut"] is True and stored["open"] is False and stored["shut_tgs"] == ["1325"]
    # the next visit (a reload): still folded, the emergency chip still on the folded head
    again = run_collapse(tmp_path, "reopen", out["stored"])
    assert not again["start"]["sec"] and again["start"]["badge"].startswith("Emergency flag stuck")
    assert again["opened"]["sec"] and again["opened"]["expanded"] == "false"      # opened: its talkgroup still folded
    assert again["groupOpened"]["expanded"] == "true"
    stored = json.loads(again["stored"])
    assert stored["shut"] is False and stored["open"] is True and stored["tgs"] == ["1325"] and stored["shut_tgs"] == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_emergency_doesnt_open_the_section_by_itself(tmp_path):
    out = run_collapse(tmp_path, "plain")
    assert not out["start"]["sec"] and out["start"]["badge"]          # folded, the chip on its head says it
