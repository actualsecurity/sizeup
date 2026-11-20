"""The pre-alert cards' action rows in the 60 Control column never cut a button short (static/intel.js, airport-alerts.js,
prealert.js, icons.js Icon.button, cad.css .aa-btns).

The report: in the queue column at its narrowest, a radio pre-alert's row of text buttons (More info, Useful, Noise,
Wrong place, Dismiss) squeezed below their words, which ran over each other. Now the card's actions are icon buttons
(named for a screen reader, "Name: what it does" on hover), More info a quiet link at the row's start, and the row
wraps rather than shrinking a button: the same on the airport and phone pre-alert cards.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed): the cards are drawn by
the page's own scripts into lists in the 60 Control section, with invented events, at the column's widths (220-480 px,
panels.js) by mouse and by touch (44 px controls). SIZEUP_SHOTS=<dir> saves the column at each width.
"""
import os
from pathlib import Path

import pytest

from tests.test_settings_no_redraw import Site

SHOTS = os.environ.get("SIZEUP_SHOTS", "")

# Invented events: the shape of the state's intel / airport_alerts / prealerts "open" items.
INTEL = [
    {"id": 101, "level": "medium", "what": 0.6, "where": 0.2, "precision": "none", "rules": [["pd.alarm", 1, 0.6]],
     "pack_label": "PD", "label": "Alarm", "town": "", "place": "", "confirmed": False, "first_at": 1,
     "traffic_ids": ["c1-t0"]},
    {"id": 102, "level": "high", "what": 0.9, "where": 0.8, "precision": "intersection", "rules": [["pd.mva", 1, 0.9]],
     "pack_label": "PD", "label": "MVA with injuries", "town": "Sampletown", "place": "Example Ave & Test St",
     "confirmed": True, "first_at": 2, "traffic_ids": ["c1-t1", "c1-t2"]},
]
AIRPORT = [
    {"id": 7, "title": "Tower: possible smoke on a runway", "level": "medium", "confidence": 0.7, "source": "atc",
     "source_name": "Tower", "channel": "Ground", "at": 3, "count": 2, "data": {"snippets": [],
                                                                                "alert_level": {"level": 1}}},
    {"id": 8, "title": "Tower: aircraft emergency inbound", "level": "high", "confidence": 0.9, "source": "atc",
     "source_name": "Tower", "channel": "Tower", "at": 4, "count": 1, "data": {"snippets": []}},
]
PHONE = {"open": [{"id": 5, "source": "HQ line", "at": 0, "title": "Ring", "message": "Ring", "ntfy_id": "x"}]}

DRAW = """([intel, airport, phone]) => {
  window.__posts = [];
  const sec = document.querySelector('#q-prealert').parentElement;
  const list = (id) => { let n = document.getElementById(id); if (!n) { n = document.createElement('ol'); n.id = id; n.className = 'pa-list'; sec.append(n); } return n; };
  const deps = { api: async (m, p, b) => { window.__posts.push([m, p, b]); return {}; }, operator: () => 'JD',
                 time: () => '17:08:02', readonly: false, more: true, select: () => {}, alarms: null };
  phone.open[0].at = Date.now() / 1000 - 20;
  IntelCard.column(list('t-intel'), { open: intel }, deps);
  AirportAlerts.column(list('t-airport'), { open: airport }, deps);
  PreAlert.column(list('t-phone'), null, phone, deps);
}"""

# Every action in a row: [card, row, tag, name, left, top, right, bottom, scrollWidth, clientWidth], and each card's box.
BOXES = """() => {
  const out = [], cards = [];
  [...document.querySelectorAll('#t-intel > li, #t-airport > li, #t-phone > li')].forEach((li, c) => {
    const r = li.getBoundingClientRect();
    cards.push([c, r.left, r.top, r.right, r.bottom, li.scrollWidth, li.clientWidth]);
    const rows = li.matches('.pa') ? [li] : [...li.querySelectorAll('.aa-btns')];
    rows.forEach((row, ri) => {
      for (const n of row.querySelectorAll(':scope > :is(button, a), :scope > .aa-acts > :is(button, a)')) {
        if (n.hidden) continue;
        const b = n.getBoundingClientRect();
        out.push([c, ri, n.tagName, n.getAttribute('aria-label') || n.textContent.trim(), b.left, b.top, b.right, b.bottom,
                  n.scrollWidth, n.clientWidth]);
      }
    });
  });
  return { boxes: out, cards, page: [document.documentElement.scrollWidth, innerWidth] };
}"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright; playwright install chromium)")
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        yield b
        b.close()


def open_column(browser, site, column, touch=False, scheme="dark"):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1600}, has_touch=touch, color_scheme=scheme)
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())         # nothing off this machine
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(site.base + "/cad")
    pg.wait_for_function("window.IntelCard && window.AirportAlerts && window.PreAlert && window.Icon")
    pg.evaluate("(w) => document.querySelector('.cad-main').style.setProperty('--rail-w', w + 'px')", column)
    pg.evaluate(DRAW, [INTEL, AIRPORT, PHONE])
    pg.wait_for_function("document.querySelectorAll('#t-intel .aa-btns button').length === 8")
    return pg


def overlap(a, b) -> bool:
    return a[4] < b[6] - 0.5 and b[4] < a[6] - 0.5 and a[5] < b[7] - 0.5 and b[5] < a[7] - 0.5


@pytest.mark.parametrize("column, touch", [(220, False), (260, False), (320, False), (480, False), (220, True), (320, True)])
def test_no_action_is_cut_short_or_runs_over_another(site, browser, column, touch):
    """At the queue column's widths (panels.js: 220-480 px), by mouse and by touch: every action of every pre-alert
    card shows all of itself (scrollWidth <= clientWidth), none overlaps another in its row, each is inside its card,
    and nothing scrolls sideways."""
    pg = open_column(browser, site, column, touch)
    m = pg.evaluate(BOXES)
    boxes, cards = m["boxes"], {c[0]: c for c in m["cards"]}
    names = [b[3] for b in boxes]
    assert names.count("More info") == 4 and names.count("Dismiss") == 5, names            # 2 radio, 2 airport, 1 phone
    for n in ("Useful", "Noise", "Wrong place", "Acknowledge", "Accept"):
        assert n in names, n
    for b in boxes:
        assert b[8] <= b[9], b                                                   # all of it shows
        least = 44 if touch else 20                                              # (touch: 44 px targets)
        assert b[6] - b[4] >= least - 0.5 and b[7] - b[5] >= least - 0.5, b      # and is there to be pressed
        _, left, _top, right, _bottom, sw, cw = cards[b[0]]
        assert left - 0.5 <= b[4] and b[6] <= right + 0.5, b                     # inside its card
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if a[0] == b[0]:
                assert not overlap(a, b), (a, b)
    for c in m["cards"]:
        assert c[5] <= c[6], c                                                   # no card scrolls sideways
    assert m["page"][0] <= m["page"][1], m
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        q = pg.locator(".queue > section").first.bounding_box()          # (the page's own pixels: the queue scrolls)
        pg.screenshot(path=str(Path(SHOTS) / f"prealert-actions-{column}{'-touch' if touch else ''}.png"),
                      clip={"x": 0, "y": q["y"] - 8, "width": column, "height": q["height"] + 16})
    assert pg.errors == []


def test_each_action_is_named_hovered_focused_in_order_and_posts_as_before(site, browser):
    """The icon buttons: a name (aria-label) and a hover that starts with it; Tab goes More info, Useful, Noise,
    Wrong place, Dismiss, each with a visible focus ring; and each posts what its text button did, with its toast.
    Every Dismiss keeps .pa-dismiss (cad.js RO_DISPATCH locks it for a viewer)."""
    pg = open_column(browser, site, 220)
    acts = pg.evaluate("""() => [...document.querySelectorAll('#t-intel > li:first-child .aa-btns :is(a, button)')].map((n) =>
      [n.tagName, n.getAttribute('aria-label') || n.textContent.trim(), n.title, !!n.querySelector('svg.ico use')])""")
    assert [a[1] for a in acts] == ["More info", "Useful", "Noise", "Wrong place", "Dismiss"]
    for tag, name, title, icon in acts[1:]:
        assert tag == "BUTTON" and icon and title.startswith(f"{name}: ") and len(title) > len(name) + 4, (name, title)
        assert title[len(name) + 2].islower() and title.count(":") == 1, title          # "Noise: nothing to it ..."
    assert pg.get_attribute("#t-intel > li:first-child .aa-more", "href") == "#prealert/intel/101"
    # keyboard: from the play button, Tab walks the row in order with a visible ring on each
    pg.focus("#t-intel > li:first-child .aa-src button")
    seen = []
    for _ in range(5):
        pg.keyboard.press("Tab")
        seen.append(pg.evaluate("""() => { const n = document.activeElement, s = getComputedStyle(n);
          return [n.getAttribute('aria-label') || n.textContent.trim(), s.outlineStyle, parseFloat(s.outlineWidth), n.matches(':focus-visible')]; }"""))
    assert [s[0] for s in seen] == ["More info", "Useful", "Noise", "Wrong place", "Dismiss"], seen
    for name, style, width, fv in seen:
        assert fv and style != "none" and width >= 1, (name, style, width)
    # each posts as before (the same path and body) and says so
    for name in ("Useful", "Noise", "Wrong place", "Dismiss"):
        pg.click(f"#t-intel > li:first-child .aa-btns button[aria-label='{name}']")
    pg.click("#t-airport > li:nth-child(2) button[aria-label='Dismiss']")
    pg.click("#t-phone > li button[aria-label='Dismiss']")
    assert pg.evaluate("[...document.querySelectorAll('#t-intel, #t-airport, #t-phone')].flatMap((l) => [...l.querySelectorAll("
                       "'button[aria-label=Dismiss]')]).every((b) => b.classList.contains('pa-dismiss'))")
    pg.wait_for_function("window.__posts.length === 6")
    posts = pg.evaluate("window.__posts")
    assert posts == [
        ["POST", "/api/cad/intel/101/feedback", {"verdict": "useful", "operator": "JD"}],
        ["POST", "/api/cad/intel/101/feedback", {"verdict": "noise", "operator": "JD"}],
        ["POST", "/api/cad/intel/101/feedback", {"verdict": "wrong_place", "operator": "JD"}],
        ["POST", "/api/cad/intel/101/dismiss", {"operator": "JD"}],
        ["POST", "/api/cad/airport-alert/8/dismiss", {"operator": "JD"}],
        ["POST", "/api/cad/prealert/5/dismiss", {"operator": "JD"}],
    ], posts
    toasts = pg.locator(".toasts").inner_text()
    for said in ("Noted: useful", "Noted: noise", "Noted: wrong place"):
        assert said in toasts, toasts
    assert pg.errors == []


def test_the_icons_are_in_the_sprite():
    svg = (Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static" / "icons.svg").read_text()
    for name in ("thumbs-up", "thumbs-down", "map-pin-off", "x"):
        assert f'id="i-{name}"' in svg, name
