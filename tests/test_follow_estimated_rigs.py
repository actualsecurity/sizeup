"""A rig drawn by an estimate is followed by a click, as a live rig or a plane is (GitHub #473).

The user, on a returning ghost on the map (an engine on its way home, "~AV"): click to follow it, the same as for live rigs and planes, even without
live location. A click on any rig on a map that follows now follows it (and opens its box: its call and, for an admin,
its home station one click away); the camera is kept on the estimate through the frame loop; a trip home followed ends
at quarters with a word; a live fix taking over glides onto the fix; placeholders are never followed.

cadmap.js in node with the stand-in MapLibre (tests/test_unit_follow.py), on states from the real hub code
(tests/test_rig_route_motion.py page_state: the downtown OSM extract, made-up calls and statuses)."""


from tests.test_rig_route_motion import OP, PRE, STATIC, metres, page_state
from tests.test_unit_follow import needs_node, run

FOLLOW = PRE + r"""
const center = () => [rm.map.center.lng, rm.map.center.lat];
const click = (u) => chip(u).el.fire("click");
"""


def released(c, clock, uid, ago=10):
    """E61 on location, then released `ago` s before now: on its way home by road."""
    c.unit_status(uid("E61"), {"status": "OL", **OP})
    clock.t -= ago
    c.unit_status(uid("E61"), {"status": "AV", **OP})
    clock.t += ago
    return c.state()["map"]


@needs_node
def test_every_estimate_state_is_followed_by_a_click(tmp_path):
    c, clock, uid, data = page_state()
    e61 = next(m for m in data["map"]["markers"] if m["unit"] == "E61")
    rt = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    # held at a "~DP" stop: started away from its firehouse (the hub's "enroute", est), dispatched, its move stopped
    held = dict(rt, move=dict(rt["move"], stop=rt["move"]["t0"]))
    data["held"] = dict(data["map"], markers=[dict(e61, place="enroute", group="unit:E61", est=True, look="dispatched", status="DP")
                                              if m is e61 else m for m in data["map"]["markers"]],
                        routes=[held if r is rt else r for r in data["map"]["routes"]])
    # overdue: an hour past its estimated arrival
    late = dict(rt, move=dict(rt["move"], t0=rt["move"]["t0"] - 3600, done_at=rt["move"]["done_at"] - 3600))
    data["late"] = dict(data["map"], routes=[late if r is rt else r for r in data["map"]["routes"]])
    data["home"] = released(c, clock, uid)
    o = run(tmp_path, FOLLOW + r"""
out.r = {};
for (const [k, M] of [["enroute", IN.map], ["held", IN.held], ["late", IN.late], ["home", IN.home]]) {
  if (rm.fol.id) rm.fol.stop();
  L.closePicker();
  setMap(M);
  click("E61");
  out.r[k] = [rm.fol.id, cls("E61").includes("est"), followText(), chip("E61").parts.st.textContent, L.pop.hidden];
}
""", data)
    for k, (fol, est, text, st, hidden) in o["r"].items():
        assert fol == "E61" and est and " · est." in text, (k, text)
        assert hidden is (k == "home"), k                       # a box only with its call in it (on its way home: none)
    assert o["r"]["enroute"][2].startswith("Following E61 · Responding · est.") and o["r"]["enroute"][3] == "~RS"
    assert o["r"]["held"][3] == "~DP" and o["r"]["late"][3] == "~RS overdue"
    assert o["r"]["home"][2].startswith("Following E61 · Returning · est.")


@needs_node
def test_the_camera_tracks_the_estimate_and_the_loop_wakes_while_following(tmp_path):
    *_, data = page_state()
    o = run(tmp_path, FOLLOW + r"""
// not followed: the estimate alone ticks a few times a second, the loop sleeping between
let ticks = 0; const t = L.tick.bind(L); L.tick = () => { ticks++; t(); };
go(63, 16); out.alone = ticks;
click("E61");
ticks = 0; go(63, 16); out.followed = ticks;
out.track = [center(), ll("E61")];
// held (not moving): still frames while followed (the camera kept on it), none once let go
const rt = IN.map.routes.find((r) => r.unit === "E61");
const held = { ...IN.map, routes: IN.map.routes.map((r) => (r === rt ? { ...r, move: { ...r.move, stop: r.move.t0 } } : r)) };
setMap(held); go(5, 16);
out.heldMoving = L.moving(); frames.splice(0); go(3, 16); out.heldFrames = frameRuns;
rm.fol.stop(); go(3, 16); out.after = L.moving();
""", data)
    assert o["followed"] >= 2.5 * o["alone"] and o["followed"] >= 15                 # smooth while followed (~20 Hz)
    assert metres(o["track"][0], o["track"][1]) < 5                                  # the camera on the estimate (mapfollow pans every PAN_MS)
    assert o["heldMoving"] is True and o["after"] is False


@needs_node
def test_a_followed_trip_home_ends_at_quarters_with_a_word_and_no_jump(tmp_path):
    c, clock, uid, data = page_state()
    data["map"] = released(c, clock, uid)
    back = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    clock.t = back["move"]["done_at"] + 1
    data["home"] = c.state()["map"]                                              # the hub has it home: "station"
    clock.t = data["now"]
    oos = [dict(m, look="oos", status="OS") if m["unit"] == "E61" else m for m in data["home"]["markers"]]
    data["oos"] = dict(data["home"], markers=oos)
    o = run(tmp_path, FOLLOW + r"""
click("E61"); go(20, 16);
out.before = center();
setMap(IN.home); go(2, 16);
const toast = host.find("cm-toast")[0];
out.end = [rm.fol.id, toast && toast.textContent, toast && toast.hidden, center()];
go(250, 16); out.gone = toast.hidden;
setMap(IN.map); L.closePicker(); click("E61"); setMap(IN.oos); go(2, 16);
out.oos = [rm.fol.id, toast.textContent];
""", data)
    assert o["end"][0] == "" and o["end"][1] == "E61 in quarters" and o["end"][2] is False
    assert metres(o["end"][3], o["before"]) < 40                                  # stopped where it was: no jump
    assert o["gone"] is True and o["oos"] == ["", "E61 out of service"]


@needs_node
def test_a_live_fix_mid_follow_is_handed_over_smoothly(tmp_path):
    *_, data = page_state()
    e61 = next(m for m in data["map"]["markers"] if m["unit"] == "E61")
    data["fixed"] = dict(data["map"], markers=[dict(e61, place="position", lon=-73.6672, lat=41.0030, at=data["now"] - 1,
                                                    heading=90.0, speed=0.0) if m is e61 else m for m in data["map"]["markers"]])
    o = run(tmp_path, FOLLOW + r"""
click("E61"); go(10, 16); out.was = ll("E61");
setMap(IN.fixed); go(1, 16); out.just = [ll("E61"), rm.fol.id, cls("E61").includes("est")];
go(100, 16); out.at = [ll("E61"), center(), rm.fol.id, followText()];
""", data)
    assert metres(o["just"][0], o["was"]) < 15 and o["just"][1] == "E61" and o["just"][2] is False
    assert o["at"][0] == [-73.6672, 41.003] and metres(o["at"][1], o["at"][0]) < 1 and o["at"][2] == "E61"
    assert "est." not in o["at"][3]


@needs_node
def test_a_placeholder_is_never_followed(tmp_path):
    *_, data = page_state()
    e61 = next(m for m in data["map"]["markers"] if m["unit"] == "E61")
    data["map"]["markers"].append(dict(e61, unit="DPT239", placeholder=True, icon=""))
    o = run(tmp_path, FOLLOW + r"""
out.r = [L.chips.has("DPT239"), CadMap.followUnit("DPT239"), rm.followUnit("DPT239"), rm.fol.id];
""", data)
    assert o["r"] == [False, False, False, ""]


def test_every_rig_is_a_full_size_target():
    css = (STATIC / "cadmap.css").read_text()
    assert "inline-size: max(var(--L), var(--ctl-sm)); block-size: max(var(--L), var(--ctl-sm))" in css   # a vehicle
    assert ".cm-rig.rig-dot::after" not in css                                      # fanned dots: no bigger target
    dot = css[css.index(".cm-rig.rig-dot.solo::after"):]
    dot = dot[:dot.index("}")]
    assert "inline-size: var(--ctl-sm)" in dot and "block-size: var(--ctl-sm)" in dot             # a dot: 24 / 44 px
    est = css[css.index(".cm-rig.rv.est"):css.index(".cm-rig.rv.est.overdue")]
    assert "pointer-events" not in est and "size" not in est.replace("font-style", "")          # an estimate: the same
    style = (STATIC / "style.css").read_text()
    assert "--ctl-sm: calc(24px" in style and "--ctl-sm: 44px" in style


@needs_node
def test_a_follow_let_go_carries_nothing_into_the_next_one(tmp_path):
    """Follow a rig on its way home, let go (a drag), it gets home, click it: followed, no "in quarters" ending."""
    c, clock, uid, data = page_state()
    data["map"] = released(c, clock, uid)
    back = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    clock.t = back["move"]["done_at"] + 1
    data["home"] = c.state()["map"]
    o = run(tmp_path, FOLLOW + r"""
click("E61"); go(5, 16);
rm.map.emit("dragstart", { originalEvent: { touches: [{}] } }); out.dragged = rm.fol.id;
setMap(IN.home); go(5, 16);
click("E61"); go(5, 16);
const toast = host.find("cm-toast")[0];
out.after = [rm.fol.id, toast ? toast.hidden : true];
// the same through the chip's ✕ and Recenter
rm.fol.stop(); setMap(IN.map); click("E61"); follow().find("mini")[0].fire("click"); setMap(IN.home); click("E61"); go(3, 16);
out.x = rm.fol.id;
rm.fol.stop(); setMap(IN.map); click("E61"); rm.map.emit("sizeup:recenter"); setMap(IN.home); click("E61"); go(3, 16);
out.recenter = rm.fol.id;
""", data)
    assert o["dragged"] == "" and o["after"] == ["E61", True] and o["x"] == "E61" and o["recenter"] == "E61"


@needs_node
def test_dots_round_a_firehouse_or_a_call_never_cover_its_taps(tmp_path):
    """Below the detail zoom an idle rig at a firehouse, and below the active zoom the rigs at a call, are dots fanned
    round the firehouse's icon or the call's pin: their targets stay their own size, clear of the icon or pin; only a
    dot on its own (on the road) gets the full target."""
    c, clock, uid, data = page_state()
    c.unit_status(uid("E60"), {"status": "OS", **OP})                            # one idle rig left at HQ (no count chip)
    c.unit_status(uid("E62"), {"status": "OS", **OP})
    data["map"] = released(c, clock, uid)                                        # E61 on its way home: a dot on its own
    o = run(tmp_path, FOLLOW + r"""
zoomTo(9.5);
out.dots = [...L.chips.values()].filter((r) => r.size === "dot").map((r) => [r.m.unit, r.m.place, r.off,
  r.el.className.includes("solo"), r.el.className.includes(" act") ? 6 : r.el.className.includes("look-oos") ? 3 : 4]);
""", data)
    css = (STATIC / "cadmap.css").read_text()
    fanned = [d for d in o["dots"] if d[1] in ("station", "incident")]
    assert fanned and all(not d[3] for d in fanned)
    for unit, place, off, solo, r in fanned:                                     # r: the dot's own radius (its target)
        assert (off[0] ** 2 + off[1] ** 2) ** 0.5 - r >= 4, unit                   # a tap at the icon's or pin's centre: theirs
    assert any(d[3] for d in o["dots"] if d[1] in ("route", "returning"))
    assert ".cm-rig.rig-dot.solo::after" in css and ".cm-rig.rig-dot::after" not in css


@needs_node
def test_a_followed_rig_standing_still_lets_the_loop_sleep(tmp_path):
    *_, data = page_state()
    rt = next(r for r in data["map"]["routes"] if r["unit"] == "E61")
    data["held"] = dict(data["map"], routes=[dict(r, move=dict(r["move"], stop=r["move"]["t0"])) if r is rt else r
                                             for r in data["map"]["routes"]])
    o = run(tmp_path, FOLLOW + r"""
click("E61"); go(10, 16);
out.moving = [L.smooth(), L.frameMs()];
setMap(IN.held); go(100, 16);                  // let the glide and the pans finish
frameRuns = 0; go(63, 16);
out.held = [L.moving(), L.smooth(), L.frameMs(), frameRuns];
""", data)
    assert o["moving"][0] is True and o["moving"][1] == 50                       # moving: every frame (FRAME_MS)
    moving, smooth, ms, frames = o["held"]
    assert moving is True and smooth is False and ms == 250 and frames <= 8      # still: looked at every PAN_MS


@needs_node
def test_the_box_says_what_the_chip_says_and_the_zoom_is_kept_on_screen(tmp_path):
    c, clock, uid, data = page_state()
    data["map"] = released(c, clock, uid)
    data["admin"] = True
    o = run(tmp_path, FOLLOW + r"""
rm.map.zoom = 14; click("E61");
const pop = host.find("cm-pop")[0];
out.box = [pop.hidden, pop.textContent, followText()];
out.zoom14 = rec.eases[rec.eases.length - 1].zoom;
rm.fol.stop(); rm.map.zoom = 12; click("E61"); out.zoom12 = rec.eases[rec.eases.length - 1].zoom;
""", data)
    hidden, text, chip = o["box"]
    assert hidden is False and "Home station" in text and "Returning · est." in text and "Available" not in text
    assert chip.startswith("Following E61 · Returning · est.")
    assert o["zoom14"] == 14 and o["zoom12"] == 15                              # kept on screen; zoomed in from far out
