"""Settings > System > Intel lays out as a grid, and still asks the server for exactly what it did (sizeup/web/static/
intel.js IntelSettings, settings.css .in-*).

The card was runs of inputs with unattached labels, and each category a chip with a select taller and wider than it:
the selects overlapped the next chip and the next line. Now: one grid for its lines (what it is, its fields under
their labels, what it reports), a pack's categories in columns of one width, the rules table with fields as wide as
what goes in them.

Two halves, as tests/test_settings_no_redraw.py: the script's shape (run everywhere), and the page itself in headless
Chromium against a real WebServer and CAD (skipped where Playwright isn't installed). The second finds the controls
by their names (aria-label), which the card had before too, so what each control posts can be compared with another
commit's page: SIZEUP_INTEL_REF=<commit> serves that commit's intel.js, settings.js, settings.css and style.css.

    SIZEUP_INTEL_REF=304d3cc python -m pytest tests/test_intel_settings_layout.py -k posts      # passes: the same bodies

(The layout tests read the new grid's own classes: they say nothing about another commit's page.)
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_settings_no_redraw import Site

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
REF = os.environ.get("SIZEUP_INTEL_REF", "")
CARD = "#s-intel"


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


# ---- without a browser -----------------------------------------------------------------------------------------------

def test_the_card_asks_for_what_it_did_and_names_every_field():
    """The script's shape: every change still goes to the intel section with the same bodies, every field it makes
    has a name, and no field is the old 6rem number box."""
    js = (STATIC / "intel.js").read_text()
    settings = js[js.index("window.IntelSettings"):]
    assert 'sPost("intel", body' in settings
    for body in ('{ action: "set", key, value: v }', '{ action: "set", key: "push", value: cb.checked }',
                 '{ action: "set", key: "listen", value: lcb.checked }', '{ action: "set", key: "infer", value: icb.checked }',
                 '{ action: "set", key: "enabled", value: ecb.checked }', '{ action: "set", key, value: i.value }',
                 '{ action: "pack", pack: p.name, [key]: v }', '{ action: "category", pack: p.name, category: c.name, mode: v }',
                 '{ action: "reset", what: "category", pack: p.name, category: c.name }',
                 '{ action: "rule", rule: r.id, mode: v }', '{ action: "rule", rule: r.id, w: v }'):
        assert body in settings, body
    for key in ("per_hour", "per_day", "per_category_hour", "repush_min", "max_age_min", "listen_expiry_min", "listen_before_s",
                "listen_after_s", "listen_kbps", "infer_model", "infer_daily_cap", "infer_budget_usd", "infer_sample_pct",
                "quiet_from", "quiet_to", "mode", "threshold", "window_s"):
        assert f'"{key}"' in settings, key
    assert settings.count('setAttribute("aria-label", label)') == 2          # num() and the time fields; selects: sSelect
    assert "s-num" not in settings and '"chip"' not in settings              # (a category is no longer a chip around a select)
    css = (STATIC / "settings.css").read_text()
    for rule in (".in-rows {", ".in-row {", ".in-fields {", ".in-cats {", ".in-cat {", ".in-stat {"):
        assert rule in css, rule
    # the swap in whole (settings.js sCardMount) is by the card's children: the card itself takes no class or id here
    assert "card.classList" not in settings and "card.id" not in settings


# ---- in the browser --------------------------------------------------------------------------------------------------

def many_categories(route):
    """The pack with three times its categories, a third of them with long names and every fourth of those edited
    (its way back shows). Invented names."""
    r = route.fetch()
    d = r.json()
    for p in d["packs"]:
        cats = p["categories"]
        long = [{**c, "name": f"{c['name']}_long", "label": f"{c['label']} at an example location with a long name {i}",
                 "edited": i % 4 == 0} for i, c in enumerate(cats)]
        p["categories"] = cats + long + [{**c, "name": f"{c['name']}_again"} for c in cats]
    route.fulfill(response=r, body=json.dumps(d))


@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright; playwright install chromium)")
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        yield b
        b.close()


def open_intel(browser, site, width, height=900, touch=False, many=False):
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch)
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())         # nothing off this machine
    if REF:                                                                                  # another commit's page
        for name, ctype in (("intel.js", "text/javascript"), ("settings.js", "text/javascript"),
                            ("settings.css", "text/css"), ("style.css", "text/css")):
            body = subprocess.run(["git", "-C", str(ROOT), "show", f"{REF}:sizeup/web/static/{name}"],
                                  capture_output=True, check=True).stdout
            pg.route(f"**/static/{name}*", (lambda body, ctype: lambda route: route.fulfill(body=body, content_type=ctype))(body, ctype))
    if many:
        pg.route("**/api/cad/intel", many_categories)
    pg.errors = []
    pg.posts = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("request", lambda r: pg.posts.append((r.url.rsplit("/", 1)[1], r.post_data_json))
          if r.method == "POST" and "/api/cad/settings/" in r.url else None)
    pg.goto(site.base + "/cad#settings/system")
    pg.fill("#operator", "JD")
    pg.wait_for_selector(f"{CARD} table")
    pg.wait_for_function("!S.loading")
    return pg


# Every control of the categories (a name, a select, a way back) and every field of the card's lines: [what, the
# pack's or line's number, x, y, right, bottom], and what the page and the card are wide.
BOXES = """() => {
  const card = document.querySelector('#s-intel'), out = [];
  const box = (what, group, n) => { const r = n.getBoundingClientRect(); out.push([what, group, r.left, r.top, r.right, r.bottom]); };
  [...card.querySelectorAll('.in-cats')].forEach((g, i) => {
    for (const cell of g.children) {
      box('cell', i, cell);
      for (const n of cell.children) box(n.tagName.toLowerCase(), i, n);
    }
  });
  [...card.querySelectorAll('.in-row')].forEach((row, i) => { for (const f of row.querySelectorAll('.in-f > :is(input, select)')) box('field', 100 + i, f); });
  const c = card.getBoundingClientRect(), t = card.querySelector('.tscroll');
  return { boxes: out, card: [c.left, c.top, c.right, c.bottom], cardScroll: [card.scrollWidth, card.clientWidth],
    table: [t.scrollWidth, t.clientWidth], page: [document.documentElement.scrollWidth, innerWidth] };
}"""


def overlap(a, b) -> bool:
    return a[2] < b[4] - 0.5 and b[2] < a[4] - 0.5 and a[3] < b[5] - 0.5 and b[3] < a[5] - 0.5


@pytest.mark.parametrize("width, touch, many", [(1024, True, False), (768, True, False), (1024, True, True), (768, True, True),
                                                (1024, False, True), (768, False, True), (1440, False, True), (390, True, True)])
def test_no_category_control_overlaps_another(site, browser, width, touch, many):
    """The report: at an iPad's widths (44 px controls: touch) and a desk's, with the pack as it is and with three
    times its categories and long names, no category's name, select or way back overlaps another's, each select is
    inside its own cell, the cells are in columns of one width with their selects in line, and nothing sticks out of
    the card. The same for the fields of the card's lines."""
    pg = open_intel(browser, site, width, touch=touch, many=many)
    m = pg.evaluate(BOXES)
    cells = [b for b in m["boxes"] if b[0] == "cell"]
    parts = [b for b in m["boxes"] if b[0] in ("span", "select", "button")]
    fields = [b for b in m["boxes"] if b[0] == "field"]
    assert len(cells) >= (33 if many else 11) and len(parts) >= 2 * len(cells) and len(fields) >= 18
    for group in (cells, parts, fields):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                assert not overlap(a, b), (a, b)
    left, _top, right, _bottom = m["card"]
    for b in cells + parts + fields:
        assert b[2] >= left - 0.5 and b[4] <= right + 0.5, b                 # inside the card
        assert b[4] - b[2] > 10 and b[5] - b[3] > 10, b                      # and there to be seen
    for sel in (b for b in parts if b[0] == "select"):
        own = [c for c in cells if c[1] == sel[1] and c[2] - 0.5 <= sel[2] and sel[4] <= c[4] + 0.5 and c[3] - 0.5 <= sel[3] and sel[5] <= c[5] + 0.5]
        assert len(own) == 1, sel                                            # the select fits in its cell
        assert abs(own[0][4] - sel[4]) < 1, sel                              # at the cell's end: the modes are in line
    widths = {round(c[4] - c[2]) for c in cells}
    assert len(widths) == 1, widths                                          # columns of one width
    columns = {round(c[2]) for c in cells}
    assert len(columns) >= (3 if width >= 1024 else 2 if width >= 768 else 1), columns
    assert m["cardScroll"][0] <= m["cardScroll"][1] and m["table"][0] <= m["table"][1], m      # nothing clipped or scrolled sideways
    assert pg.errors == []


def test_every_control_has_a_name_and_every_label_a_hover(site, browser):
    """Every input, select and button in the card has an accessible name; every terse label over a field says in its
    hover what the field is (and the field says the same)."""
    pg = open_intel(browser, site, 1024, many=True)
    rows = pg.evaluate("""() => [...document.querySelectorAll('#s-intel :is(input, select, textarea, button)')].map((n) => {
      const lab = n.closest('label');
      const name = n.getAttribute('aria-label') || (n.tagName === 'BUTTON' && n.textContent.trim().length > 1 ? n.textContent.trim() : '')
        || (lab && n.type === 'checkbox' ? lab.textContent.trim() : '');
      return [n.tagName + ':' + (n.type || ''), name, n.title || (lab && lab.title) || (n.classList.contains('help') ? 'help.js' : '')];
    })""")
    assert len(rows) > 60
    for kind, name, title in rows:
        assert name.strip(), (kind, name, title)
        assert title.strip(), (kind, name, title)                            # (the "?" is help.js's own: it opens its text)
    labels = pg.evaluate("() => [...document.querySelectorAll('#s-intel .in-f')].map((l) => [l.querySelector('.in-l').textContent, l.title, l.querySelector('input, select').title])")
    packs = len(site.j("GET", "/api/cad/intel")[1]["packs"])
    assert packs == 4                                                        # the first pack and the three log-only ones
    assert [t for t, _, _ in labels] == ["/h", "/day", "/cat/h", "repush min", "max age min", "expiry min", "before s", "after s", "kbps",
                                         "model", "/day", "$/month", "sample %", "quiet from", "to"] + ["mode", "threshold", "window s"] * packs
    for text, title, field_title in labels:
        assert len(title) > len(text) and title == field_title, (text, title)
    hovers = {t: h for t, h, _ in labels}
    for text, word in (("/h", "hour"), ("/cat/h", "category"), ("repush min", "Minutes"), ("max age min", "Minutes"), ("expiry min", "Minutes"),
                       ("before s", "Seconds"), ("after s", "Seconds"), ("kbps", "kbit/s"), ("$/month", "dollars"), ("sample %", "Percent"),
                       ("quiet from", "local time"), ("threshold", "0 to 1"), ("window s", "Seconds")):
        assert word in hovers[text], (text, hovers[text])
    assert pg.errors == []


def change(pg, selector, value) -> tuple:
    """Set a field or a select, or press a button or a checkbox (value None), and return what the page then asked the
    server to change."""
    n = len(pg.posts)
    loc = pg.locator(selector).first
    kind = loc.evaluate("(n) => n.tagName === 'SELECT' ? 'select' : n.tagName === 'BUTTON' || n.type === 'checkbox' ? 'press' : 'fill'")
    if kind == "select":
        loc.select_option(value)
    elif kind == "press":
        loc.click()
    else:
        loc.fill(value)
        loc.blur()
    for _ in range(100):
        if len(pg.posts) > n:
            break
        pg.wait_for_timeout(50)
    assert len(pg.posts) == n + 1, (selector, pg.posts[n:])
    pg.wait_for_function("!S.loading")
    pg.wait_for_timeout(300)                                                 # its answer, and the card drawn again
    return pg.posts[n]


def test_every_control_posts_what_it_did(site, browser):
    """Every control of the card, found by its name: the section and the body it posts are those of the card before
    the layout (run against that commit with SIZEUP_INTEL_REF: the same list passes)."""
    pg = open_intel(browser, site, 1366)
    d = site.j("GET", "/api/cad/intel")[1]
    pack = d["packs"][0]
    cat, rule = pack["categories"][2], pack["rules"][1]
    at = lambda label: f"{CARD} [aria-label='{label}']"                       # noqa: E731
    check = lambda text: f"{CARD} label.check:has(span:text-is('{text}')) input"      # noqa: E731
    op = {"operator": "JD"}
    steps = [
        (at("Pushes an hour"), "31", {"action": "set", "key": "per_hour", "value": 31}),
        (at("Pushes a day"), "201", {"action": "set", "key": "per_day", "value": 201}),
        (at("Pushes an hour, one category"), "11", {"action": "set", "key": "per_category_hour", "value": 11}),
        (at("Minutes between pushes of one event"), "12", {"action": "set", "key": "repush_min", "value": 12}),
        (f"{CARD} [aria-label^='A transmission older than this']", "16", {"action": "set", "key": "max_age_min", "value": 16}),
        (at("Minutes the link plays"), "25", {"action": "set", "key": "listen_expiry_min", "value": 25}),
        (f"{CARD} [aria-label^='Seconds of the same source before']", "40", {"action": "set", "key": "listen_before_s", "value": 40}),
        (f"{CARD} [aria-label^='Seconds after the last']", "70", {"action": "set", "key": "listen_after_s", "value": 70}),
        (f"{CARD} [aria-label^='Bitrate']", "32", {"action": "set", "key": "listen_kbps", "value": 32}),
        (at("Model id"), " example.model-id-1 ", {"action": "set", "key": "infer_model", "value": "example.model-id-1"}),
        (at("Calls a day at most"), "50", {"action": "set", "key": "infer_daily_cap", "value": 50}),
        (f"{CARD} [aria-label^='Budget a month']", "7", {"action": "set", "key": "infer_budget_usd", "value": 7}),
        (f"{CARD} [aria-label^='Of rows with a hit']", "5", {"action": "set", "key": "infer_sample_pct", "value": 5}),
        (f"{CARD} [aria-label^='Quiet from']", "22:30", {"action": "set", "key": "quiet_from", "value": "22:30"}),
        (at("Quiet until"), "06:15", {"action": "set", "key": "quiet_to", "value": "06:15"}),
        (at(f"{pack['label']} pack mode"), "map", {"action": "pack", "pack": pack["name"], "mode": "map"}),
        (f"{CARD} [aria-label^='Threshold']", "0.55", {"action": "pack", "pack": pack["name"], "threshold": 0.55}),
        (f"{CARD} [aria-label^='Seconds of one source merged']", "90", {"action": "pack", "pack": pack["name"], "window_s": 90}),
        (f"{CARD} select[aria-label='{cat['label']}']", "log", {"action": "category", "pack": pack["name"], "category": cat["name"], "mode": "log"}),
        (f"{CARD} button[title='Back to the default']", None, {"action": "reset", "what": "category", "pack": pack["name"], "category": cat["name"]}),
        (f"{CARD} select[aria-label='{rule['id']}']", "shadow", {"action": "rule", "rule": rule["id"], "mode": "shadow"}),
        (at(f"{rule['id']} weight"), "0.35", {"action": "rule", "rule": rule["id"], "w": 0.35}),
        (check("Listen link"), None, {"action": "set", "key": "listen", "value": not d["settings"]["listen"]}),
        (check("Inference"), None, {"action": "set", "key": "infer", "value": not d["settings"].get("infer")}),
        (check("Push"), None, {"action": "set", "key": "push", "value": not d["settings"]["push"]}),
        (check("Detection"), None, {"action": "set", "key": "enabled", "value": not d["settings"]["enabled"]}),
    ]
    assert pg.locator(f"{CARD} button[title='Back to the default']").count() == 0          # (nothing edited yet)
    for selector, value, body in steps:
        assert change(pg, selector, value) == ("intel", {**body, **op}), selector
    assert len(pg.posts) == len(steps)
    # every control the card has was one of those, or is another row's of the same kind (a category's or a rule's)
    kinds = pg.evaluate("""() => { const c = document.querySelector('#s-intel');
      return [c.querySelectorAll('input[type=checkbox]').length, c.querySelectorAll('.in-fields :is(input, select)').length,
        c.querySelectorAll('.in-cats select').length, c.querySelectorAll('.in-rules select').length, c.querySelectorAll('.in-rules input').length,
        c.querySelectorAll('input, select, button:not(.help)').length]; }""") if not REF else None
    if kinds:
        ncat, nrule = sum(len(p["categories"]) for p in d["packs"]), sum(len(p["rules"]) for p in d["packs"])
        assert len(d["packs"]) == 4 and nrule > 80                           # every pack's rows, each with its own controls
        assert kinds[:5] == [4, 15 + 3 * len(d["packs"]), ncat, nrule, nrule] and kinds[5] == sum(kinds[:5]), kinds
        names = pg.evaluate("() => [...document.querySelectorAll('#s-intel :is(.in-cats, .in-rules) select')].map((n) => n.getAttribute('aria-label'))")
        assert len(set(names)) == len(names) == ncat + nrule                 # no two controls of the card share a name
    # and the server took them: the card shows what was saved
    now = site.j("GET", "/api/cad/intel")[1]
    assert now["settings"]["per_hour"] == 31 and now["settings"]["quiet_from"] == "22:30" and now["packs"][0]["window_s"] == 90
    assert pg.input_value(at("Pushes an hour")) == "31" and pg.input_value(at(f"{rule['id']} weight")) == "0.35"
    assert pg.errors == []


def test_the_card_is_still_swapped_in_whole_and_never_under_a_field(site, browser):
    """With #528: the card keeps its node across another admin's change; a field with the focus is not replaced under
    the person, and catches up once it is left."""
    pg = open_intel(browser, site, 1024)
    field = f"{CARD} [aria-label='Pushes an hour']"
    pg.evaluate("() => { window._card = document.querySelector('#s-intel'); window._rows = window._card.querySelector('.in-rows'); }")
    was = int(pg.input_value(field))

    def elsewhere(key, value):
        code, out = site.j("POST", "/api/cad/settings/intel", {"action": "set", "key": key, "value": value, "operator": "KM"})
        assert code == 200, out
        pg.wait_for_function("(rev) => S.data && S.data.rev >= rev && !S.loading", arg=site.cad.store.rev)
        pg.wait_for_timeout(500)

    elsewhere("per_day", 321)
    pg.wait_for_function("document.querySelector(\"#s-intel [aria-label='Pushes a day']\").value === '321'")
    assert pg.evaluate("[window._card === document.querySelector('#s-intel'), window._card.className, window._rows.isConnected]") == [True, "card s-card", False]
    pg.focus(field)
    pg.keyboard.type("9")                                                    # typed, not saved
    typed = pg.input_value(field)
    pg.evaluate("() => { window._field = document.activeElement; }")
    elsewhere("per_day", 322)
    assert pg.evaluate("window._field === document.activeElement && window._field.isConnected") is True
    assert pg.input_value(field) == typed and pg.input_value(f"{CARD} [aria-label='Pushes a day']") == "321"
    pg.fill(field, str(was))                                                 # as it was: nothing unsaved; and the focus leaves
    n = len(pg.posts)
    pg.locator(f"{CARD} h2").click()
    elsewhere("per_day", 323)
    pg.wait_for_function("document.querySelector(\"#s-intel [aria-label='Pushes a day']\").value === '323'")
    assert pg.posts[n:] == [] and pg.input_value(field) == str(was)
    assert pg.errors == []
