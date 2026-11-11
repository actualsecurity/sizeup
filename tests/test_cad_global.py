"""The map modules read cad.js's state through the script-global `cad`, never `window.cad`.

cad.js declares `const cad = {...}` at the top of a classic script: every later script sees `cad`, but a top-level
const is no property of window, so `window.cad` is always undefined. aircraft.js, gislayers.js, maplayers.js and
trains.js guarded with `window.cad && cad.X`: always false, so the hub-clock skew, the "ATC talker on the map"
setting, the console's timezone and who is signed in were never read. Here `cad` is a top-level const too (as on the
page; made-up values) and the modules run in node."""

import re

from tests.test_aircraft_follow import LOADED, MAP, STATIC, needs_node, run_node

CAD = "const cad = { skew: 30, scene: { timezone: 'Asia/Tokyo' }, state: { atc: { map: true } }, who: null };\n"
HEX = "a1b2c3"


def ring(tmp_path, until_in, setup=""):
    """aircraft.js's ring for an aircraft the hub says is talking until `until_in` seconds from the viewer's clock."""
    d = {"aircraft": [{"hex": HEX, "lat": 41.07, "lon": -73.7, "alt_baro": 1500, "trk": 340}], "atc": {"talking": [], "tx": {}}}
    o = run_node(tmp_path, ["motion.js", "glide.js", "mapfollow.js", "aircraft.js"], LOADED + r"""
(async () => {
  """ + setup + r"""
  IN.d.atc.talking = [{ hex: IN.hex, role: "speaking", until: Date.now() / 1000 + IN.until_in }];
  await AircraftLayer.load();
  out.ring = AircraftLayer.ringFor(IN.hex);
  console.log(JSON.stringify(out));
})();
""", {"d": d, "hex": HEX, "until_in": until_in}, pre=CAD + MAP)
    return o["ring"]


@needs_node
def test_the_talking_ring_runs_on_the_hubs_clock(tmp_path):
    # the viewer's clock is 30 s behind the hub's: a hold the hub ended 20 s ago is over, one with 10 s to go is not
    assert ring(tmp_path, 10) == ""
    assert ring(tmp_path, 40) == "talking"


@needs_node
def test_atc_talker_off_takes_the_ring_off(tmp_path):
    assert ring(tmp_path, 100) == "talking"
    assert ring(tmp_path, 100, "cad.state.atc.map = false;") == ""


def line(name, pat):
    (m,) = [x for x in (STATIC / name).read_text().splitlines() if re.search(pat, x)]
    return m.strip()


@needs_node
def test_times_are_in_the_consoles_timezone(tmp_path):
    for name in ("aircraft.js", "gislayers.js", "maplayers.js", "trains.js"):
        src = line(name, r"^\s*const tz = ")
        o = run_node(tmp_path, [], CAD + src + "\nout.tz = tz();\n")
        assert o["tz"] == "Asia/Tokyo", name


@needs_node
def test_a_viewer_is_not_offered_keep_on_a_camera(tmp_path):
    # the DVR Keep button is hidden from a signed-in viewer (the hub refuses them: sizeup/cad/api.py WRITES)
    who, hide = line("maplayers.js", r"const w = .*cad\.who"), line("maplayers.js", r"el\.keep\.hidden = ")
    for may, hidden in (("viewer", True), ("dispatcher", False), ("admin", False)):
        body = CAD + f"cad.who = {{ enforced: true, may: '{may}' }};\nconst el = {{ keep: {{}} }};\n{who}\n{hide}\nout.hidden = el.keep.hidden;\n"
        assert run_node(tmp_path, [], body)["hidden"] is hidden, may
