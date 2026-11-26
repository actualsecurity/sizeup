"""The Radio rail scrolls to its last row by itself (radio-nav.js fitRail, cad.css .rn-rail): a tree taller than the
window, with and without the live player's strip under the console, at a wide, a laptop and a narrow width. Before,
its height was the window's less two headers, so with the strip (or a banner) its last rows sat below what could be
seen and the page had to be scrolled for them. Headless Chromium; every channel and talkgroup is invented.
SIZEUP_SHOTS=<dir> saves screenshots."""
import json
import os
import time

import pytest

from tests.test_live_player_ui import Air, listen_on, open_console, site  # noqa: F401  (fixture)
from tests.test_reminders_ui import browser  # noqa: F401  (fixture)

SHOTS = os.environ.get("SIZEUP_SHOTS", "")


def tree():
    out = [{"key": "fm:FG1", "system": "analog", "system_label": "Analog", "group": "Analog", "name": "FG1", "service": "fire",
            "tier": "", "ours": True, "mutable": False, "last": 0, "counts": []}]
    for sys, label, base in (("westchester", "County", 1000), ("metro", "Metro", 9000)):
        for g in range(6):
            for i in range(8):
                tg = base + g * 10 + i
                out.append({"key": f"p25:{sys}:{tg}", "system": sys, "system_label": label, "group": f"Group {g + 1}",
                            "name": f"{label} Ch {g + 1}-{i + 1}", "service": "other", "tier": "text", "ours": False,
                            "mutable": True, "talkgroup": tg, "last": 0, "counts": []})
    out[-1]["name"] = "Last Channel"
    return out


SOURCES = {"rev": "s1", "now": 0, "live": [], "folders": None, "services": {}, "sources": tree()}

REACH = """() => {
  const rail = document.getElementById('rn-rail'), col = rail.closest('.detail');
  const rows = [...rail.querySelectorAll('.rn-ch')], last = rows[rows.length - 1];
  rail.scrollTop = rail.scrollHeight;
  const lp = document.getElementById('lp'), strip = lp && !lp.hidden ? lp.getBoundingClientRect().height : 0;
  const page = getComputedStyle(col).overflowY === 'visible';
  const seen = page ? innerHeight - strip : Math.min(innerHeight - strip, col.getBoundingClientRect().bottom);
  return { rows: rows.length, name: last.textContent, bottom: last.getBoundingClientRect().bottom, seen,
           railBottom: rail.getBoundingClientRect().bottom, colScroll: col.scrollTop, winScroll: scrollY,
           railScrolls: rail.scrollHeight > rail.clientHeight, page };
}"""


@pytest.mark.parametrize("width,height", [(1600, 900), (1280, 720), (1000, 700)])
@pytest.mark.parametrize("strip", [False, True])
def test_the_rails_last_row_is_reached_by_scrolling_the_rail_alone(browser, site, width, height, strip):  # noqa: F811
    air = Air()
    pg = open_console(browser, site, air)
    pg.set_viewport_size({"width": width, "height": height})
    pg.route(lambda url: "/api/cad/comms/sources" in url, lambda route: route.fulfill(
        status=200, body=json.dumps({**SOURCES, "now": time.time()}), content_type="application/json"))
    if strip:
        listen_on(pg)
    pg.evaluate("() => { location.hash = '#radio?ui=2'; }")
    pg.wait_for_function("() => document.querySelectorAll('#rn-tree .rn-ch').length > 90")
    pg.wait_for_timeout(300)                         # (a frame for the measure)
    m = pg.evaluate(REACH)
    pg.wait_for_timeout(100)
    m = pg.evaluate(REACH)                           # (the rail's own scroll, measured after it settles)
    assert m["railScrolls"], m                       # the tree is taller than the window: the rail scrolls
    assert "Last Channel" in m["name"]
    assert m["bottom"] <= m["seen"] + 1, m           # its last row in sight, the page not scrolled for it
    assert m["railBottom"] <= m["seen"] + 1, m
    # the column never scrolled for it; on a desk the page doesn't either (under 1101 px the columns stack and the page
    # scrolls down to the Radio view, as it always did: the rail then sticks under the header)
    assert m["colScroll"] == 0 and (m["page"] or m["winScroll"] == 0), m
    if SHOTS and width == 1280:
        pg.screenshot(path=f"{SHOTS}/rail-scroll-{width}{'-strip' if strip else ''}.png")
    assert not pg.errors, pg.errors
