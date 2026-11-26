"""The console's live player (sizeup/web/static/live-player.js), its pure part ListenCore in node: which level a
transmission gets from a person's listen choices (the most specific rule first, the quick toggles, an emergency), and
the queue (Priority first and never dropped, Normal oldest first and dropped once too old or too many, interrupt / duck /
queue, an interrupted one resumed unless too old, choices changed while clips wait). Every source, talkgroup and time
here is invented."""

import json
import subprocess

import pytest

from tests.test_radio_emergency import NODE, STATIC

HARNESS = r"""
global.window = global;
global.document = { getElementById: () => null };
const [FILE, CODE] = process.argv.slice(-2);
require(FILE);
const C = window.ListenCore;
const out = (v) => process.stdout.write(JSON.stringify(v));
const p = (o) => C.norm(o);
eval(CODE);
"""

# invented rows in the hub's shape (GET /api/cad/comms?view=all)
FG1 = {"id": "a1", "source": "fm:FG1", "system": "analog", "service": "fire", "ours": 1, "start": 1000}
DISP = {"id": "a2", "source": "p25:westchester:1111", "system": "westchester", "service": "fire", "ours": 1, "start": 1001}
PD = {"id": "a3", "source": "p25:westchester:2222", "system": "westchester", "service": "police", "ours": 0, "start": 1002}
MTA = {"id": "a4", "source": "p25:metro:3333", "system": "metro", "service": "transit", "ours": 0, "start": 1003}
TWR = {"id": "a5", "source": "air:Tower", "system": "airband", "service": "air", "ours": 0, "mhz": 118.6, "start": 1004}


def run(code: str):
    if NODE is None:
        pytest.skip("node not installed")
    r = subprocess.run([NODE, "-e", HARNESS, "--", str(STATIC / "live-player.js"), code], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def rows() -> str:
    return f"const FG1 = {json.dumps(FG1)}, DISP = {json.dumps(DISP)}, PD = {json.dumps(PD)}, MTA = {json.dumps(MTA)}, " \
           f"TWR = {json.dumps(TWR)};"


def test_a_new_person_hears_our_calls_as_priority_and_nothing_else():
    got = run(rows() + "const d = p(null); out([d, [FG1, DISP, PD, MTA, TWR].map((t) => C.level(t, d))]);")
    assert got[0] == {"rules": {"ours": "priority"}, "mode": "interrupt", "max_age": 60, "ours_only": False, "mute_police": False}
    assert got[1] == ["priority", "priority", "off", "off", "off"]


def test_the_most_specific_rule_wins():
    got = run(rows() + """
      const q = p({ rules: { ours: "priority", "svc:police": "normal", "sys:metro": "normal", "svc:transit": "off",
                             "src:fm:FG1": "normal", "mhz:118.6": "priority", "sys:airband": "off" } });
      out([[FG1, DISP, PD, MTA, TWR].map((t) => C.level(t, q)), C.rule(TWR, q), C.rule(MTA, q), C.rule(FG1, q)]);
    """)
    assert got[0] == ["normal", "priority", "normal", "normal", "priority"]
    assert got[1] == ["mhz:118.6", "priority"]      # a frequency over its system
    assert got[2] == ["sys:metro", "normal"]        # a system over a service
    assert got[3] == ["src:fm:FG1", "normal"]       # a channel over Our calls


def test_quick_toggles_and_an_emergency():
    got = run(rows() + """
      const base = { rules: { ours: "priority", "svc:police": "priority", "sys:metro": "normal" } };
      const em = { ...MTA, emergency: 1 }, emOff = { ...TWR, emergency: 1 };
      out([[FG1, PD, MTA].map((t) => C.level(t, p({ ...base, ours_only: true }))),
           [FG1, PD, MTA].map((t) => C.level(t, p({ ...base, mute_police: true }))),
           C.level(em, p(base)), C.level(emOff, p(base))]);
    """)
    assert got[0] == ["priority", "off", "off"]
    assert got[1] == ["priority", "off", "normal"]
    assert got[2] == "priority"        # an emergency on a channel listened to: Priority
    assert got[3] == "off"             # ... never one you turned off


def test_a_channels_own_level_and_its_default():
    got = run("""
      const s = { key: "p25:westchester:2222", system: "westchester", service: "police", ours: false };
      const a = p({ rules: { ours: "priority", "svc:police": "normal" } });
      const b = C.setRule(a, "src:" + s.key, "off");
      out([C.sourceLevel(s, a), C.sourceLevel(s, b), C.setRule(b, "src:" + s.key, "").rules]);
    """)
    assert got[0] == {"own": "", "inherited": "normal", "effective": "normal"}
    assert got[1] == {"own": "off", "inherited": "normal", "effective": "off"}
    assert got[2] == {"ours": "priority", "svc:police": "normal"}


def test_priority_plays_first_and_normal_ones_oldest_first():
    got = run("""
      const pr = p({}), q = C.make();
      const it = (id, start, lvl) => ({ id, start, lvl, row: {} });
      const acts = [C.add(q, it("n1", 100, "normal"), 101, pr)];
      q.playing = C.next(q, 101, pr);
      acts.push(C.add(q, it("n3", 103, "normal"), 104, pr), C.add(q, it("n2", 102, "normal"), 104, pr),
                C.add(q, it("p1", 104, "priority"), 105, pr), C.add(q, it("n2", 102, "normal"), 105, pr),
                C.add(q, it("x", 105, "off"), 105, pr));
      const order = [];
      for (let x; (x = C.next(q, 106, pr)); ) order.push(x.id);
      out([acts, q.playing.id, order]);
    """)
    assert got[0] == ["play", "queue", "queue", "interrupt", "none", "none"]     # the same one twice: once
    assert got[1] == "n1"
    assert got[2] == ["p1", "n2", "n3"]


def test_normal_ones_too_old_are_dropped_priority_never():
    got = run("""
      const pr = p({ max_age: 30 }), q = C.make();
      const it = (id, start, lvl) => ({ id, start, lvl, row: {} });
      q.playing = it("long", 0, "normal");
      C.add(q, it("n1", 100, "normal"), 101, pr);
      C.add(q, it("p1", 100, "priority"), 101, pr);
      const late = C.add(q, it("n0", 10, "normal"), 101, pr);     // already 91 s old when it came
      const order = [];
      for (let x; (x = C.next(q, 200, pr)); ) order.push(x.id);  // 100 s later: n1 too old, p1 still played
      out([late, order, q.dropped]);
    """)
    assert got == ["none", ["p1"], 2]


def test_too_many_normal_ones_drop_the_oldest():
    got = run("""
      const pr = p({}), q = C.make();
      q.playing = { id: "x", start: 0, lvl: "priority" };
      for (let i = 0; i < C.MAX_NORMAL + 3; i++) C.add(q, { id: "n" + i, start: 100 + i, lvl: "normal", row: {} }, 120, pr);
      for (let i = 0; i < 30; i++) C.add(q, { id: "p" + i, start: 100 + i, lvl: "priority", row: {} }, 120, pr);
      out([C.waiting(q), q.nq[0].id, q.dropped]);
    """)
    assert got == [{"priority": 30, "normal": 15}, "n3", 3]


def test_interrupt_duck_or_queue_as_chosen():
    got = run("""
      const it = (id, lvl) => ({ id, start: 100, lvl, row: {} });
      const one = (mode, playing) => { const pr = p({ mode }), q = C.make(); q.playing = playing; return C.add(q, it("p", "priority"), 101, pr); };
      out([one("interrupt", it("n", "normal")), one("duck", it("n", "normal")), one("queue", it("n", "normal")),
           one("interrupt", it("p0", "priority")), one("duck", null)]);
    """)
    # a Priority one never interrupts another Priority one; nothing playing: just play
    assert got == ["interrupt", "duck", "queue", "queue", "play"]


def test_an_interrupted_one_resumes_unless_too_old():
    got = run("""
      const pr = p({ max_age: 60 }), q = C.make();
      q.paused = { id: "n", start: 100, lvl: "normal", row: {}, at: 4.2 };
      C.add(q, { id: "n2", start: 101, lvl: "normal", row: {} }, 102, pr);
      const a = C.next(q, 120, pr);
      q.paused = { id: "m", start: 100, lvl: "normal", row: {}, at: 1 };
      const b = C.next(q, 170, pr);                                    // m (70 s old) dropped, n2 (69 s) dropped too
      out([a.id, a.at, b, q.dropped]);
    """)
    assert got == ["n", 4.2, None, 2]


def test_a_changed_choice_judges_what_waits_again():
    got = run(rows() + """
      let pr = p({ rules: { ours: "priority", "svc:police": "normal", "sys:metro": "normal" } });
      const q = C.make();
      q.playing = { id: "x", start: 0, lvl: "priority" };
      for (const t of [PD, MTA, FG1]) C.add(q, { id: t.id, start: t.start, lvl: C.level(t, pr), row: t }, 1005, pr);
      q.paused = { id: "pp", start: 1000, lvl: "normal", row: PD };
      const before = C.waiting(q);
      pr = { ...pr, mute_police: true, rules: { ...pr.rules, "sys:metro": "priority" } };
      C.rejudge(q, pr, (row) => C.level(row, pr));
      out([before, C.waiting(q), q.pq.map((x) => x.id), q.paused]);
    """)
    assert got == [{"priority": 1, "normal": 3}, {"priority": 2, "normal": 0}, ["a1", "a4"], None]


def test_bad_stored_choices_fall_back_to_the_defaults():
    got = run("""out([p({ rules: { ours: "loud", "svc:fire": "normal" }, mode: "x", max_age: 5, ours_only: "yes" }), p([]), p("x")]);""")
    assert got[0] == {"rules": {"svc:fire": "normal"}, "mode": "interrupt", "max_age": 60, "ours_only": False, "mute_police": False}
    assert got[1]["rules"] == {"ours": "priority"} and got[2]["rules"] == {"ours": "priority"}


def test_the_page_loads_it_after_the_radio_scripts_and_the_board_has_none():
    html = (STATIC / "cad.html").read_text()
    order = [html.index(f'src="/static/{n}"') for n in ("me-prefs.js", "cad.js", "radio-nav.js", "live-player.js")]
    assert order == sorted(order)
    assert 'id="lp-on" class="nav live-btn console-only"' in html and 'id="lp" class="lp console-only"' in html
    js = (STATIC / "live-player.js").read_text()
    assert "if (typeof BOARD !== \"undefined\" && BOARD) return null;" in js
    # its own feed rides the state push: no interval of its own
    assert "setInterval" not in js
