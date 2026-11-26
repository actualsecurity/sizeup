"""The console keeps its page small and still (GitHub #887; sizeup/web/static/settings.js sRelease, sUnitsLive;
prealert.js ticking).

A production trace of /cad counted ~145k DOM nodes (only 2-3.5k laid out) and the event listeners swinging 6k -> 21k
-> 6k within seconds, again and again. Measured in headless Chromium against the CAD under a made-up load (45 events,
~250 units, 1,500 transmissions, a unit's status and an event's notes changing every 0.7 s; CDP Memory.getDOMCounters
after a GC, EventTarget.addEventListener counted by its caller):

  Settings > Units open: the section was built again whenever a unit's status, call or record count moved (S_LIVE,
      every S_POLL.live ms): ~250 rows of fields, selects and buttons, about 290 nodes and a dozen listeners each,
      each time. 20 s: 11,874 listeners added, the counters 4.3k -> 7.3k -> 10.3k listeners and 80k -> 228k nodes
      between collections (the trace's swing). Now the rows' status lines are put right in place: 14 listeners added,
      the counters flat.
  Any section once opened stayed in the page, hidden: after a visit to every page, back on the Overview, 112k nodes
      and 6.1k listeners, 73k of the nodes Settings > Units and 14k Settings > System. Now a section sRender builds
      whole is let go when it is left (it was built again on every visit anyway): 24k nodes, 2.5k listeners.
  prealert.js searched the whole page for [data-pa-at] every second; now it ticks the ages it made.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed)."""
import time

import pytest

from tests.test_settings_no_redraw import Site


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


@pytest.fixture
def page(site):
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright; playwright install chromium)")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())     # nothing off this machine
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.cdp = ctx.new_cdp_session(pg)
        yield pg
        assert pg.errors == []
        browser.close()


def counters(pg) -> dict:
    pg.cdp.send("HeapProfiler.collectGarbage")
    return pg.cdp.send("Memory.getDOMCounters")


def go(pg, h: str) -> None:
    pg.evaluate("(h) => { location.hash = h; }", h)


UNITS = "#s-units li.s-row.unit[data-row]"


def open_units(pg, site) -> None:
    pg.goto(site.base + "/cad#settings/units")
    pg.fill("#operator", "JD")
    pg.wait_for_function(f"document.querySelectorAll('{UNITS}').length > 20 && !S.loading")


def test_a_settings_section_left_lets_its_nodes_go_and_is_built_again_on_return(site, page):
    open_units(page, site)
    rows = page.locator(UNITS).count()
    shown = counters(page)["nodes"]
    go(page, "#settings/system")
    page.wait_for_function("document.querySelector('#s-system').children.length > 0 && !S.loading")
    assert page.evaluate("() => document.querySelector('#s-units').children.length") == 0
    page.evaluate("() => { history.pushState(null, '', location.pathname); window.dispatchEvent(new HashChangeEvent('hashchange')); }")
    page.wait_for_function("document.querySelector('#settings').hidden")
    assert page.evaluate("() => [...document.querySelectorAll('#settings .s-panel')].filter((p) => "
                         "['lists', 'units', 'dispatch', 'users', 'system'].includes(p.dataset.s)).map((p) => p.children.length)") \
        == [0, 0, 0, 0, 0]
    # the units' rows were most of the page (about 290 nodes a unit): gone with the section
    assert counters(page)["nodes"] < shown - 150 * rows
    go(page, "#settings/units")                                       # back: built again, as on any visit
    page.wait_for_function(f"document.querySelectorAll('{UNITS}').length === {rows}")


def test_a_units_status_puts_its_line_right_without_building_the_section_again(site, page):
    u = next(u for u in site.cad.store.units() if u["status"] == "AV" and u["kind"] == "engine")
    site.cad.store.set_unit_status(u["id"], "AQ", "JD")             # a unit with history (no history: Delete, then
    site.cad.store.set_unit_status(u["id"], "AV", "JD")             # its first record takes it away: built again)
    open_units(page, site)
    page.evaluate("() => { S_POLL.live = 0; }")                     # read again on the next rev, not 5 s later
    row = f"{UNITS}[data-row='{u['name']}']"
    page.evaluate(f"""() => {{ const li = document.querySelector("{row}");
        window._kept = [li, li.querySelector('input'), document.querySelector('#s-units').firstElementChild]; }}""")
    before = page.locator(f"{row} .s-meta .chip.st").inner_text()
    page.evaluate("() => { window._adds = 0; const add = EventTarget.prototype.addEventListener;"
                  " EventTarget.prototype.addEventListener = function (...a) { window._adds++; return add.apply(this, a); }; }")
    listeners = counters(page)["jsEventListeners"]
    for i, st in enumerate(("AQ", "AV", "AQ")):
        site.cad.store.set_unit_status(u["id"], st, "JD")
        site.cad.update(site.cad.create({"type": "ALARM", "address": f"{10 + i} Example St"})["id"],
                        {"notes": "moves the rev", "operator": "JD"})
        time.sleep(0.3)
    page.wait_for_function(f"""(before) => {{ const c = document.querySelector("{row} .s-meta .chip.st");
        return c && c.textContent !== before && !S.loading; }}""", arg=before)
    page.wait_for_timeout(800)
    # the same row, the same field, the same section: only its status line drawn again
    assert page.evaluate(f"""() => {{ const li = document.querySelector("{row}");
        return [li === window._kept[0], li.querySelector('input') === window._kept[1],
                document.querySelector('#s-units').firstElementChild === window._kept[2]]; }}""") == [True, True, True]
    assert page.evaluate("() => window._adds") < 50                 # was the whole section: ~3,000 a read
    assert counters(page)["jsEventListeners"] - listeners < 50


def test_a_change_to_the_units_settings_still_builds_the_section(site, page):
    open_units(page, site)
    page.evaluate("() => { window._first = document.querySelector('#s-units li.s-row.unit[data-row]'); }")
    code = page.evaluate("() => window._first.dataset.row")
    status, body = site.j("POST", "/api/cad/settings/units", {"action": "edit", "code": code, "unit": {"title": "Example title"},
                                                               "operator": "Another admin"})
    assert status == 200, body
    page.wait_for_function(f"""() => {{ const li = document.querySelector("{UNITS}[data-row='{code}']");
        return li && li !== window._first && li.querySelector('input.s-title').value === 'Example title'; }}""", timeout=40000)


def test_the_pre_alert_ages_tick_without_searching_the_page(site, page):
    page.add_init_script("""(() => { window._paSearches = 0; const q = Document.prototype.querySelectorAll;
        Document.prototype.querySelectorAll = function (s) { if (String(s).includes('data-pa-at')) window._paSearches++;
            return q.call(this, s); }; })()""")
    page.goto(site.base + "/cad")
    page.wait_for_function("window.PreAlert && window.Icon")
    page.evaluate("""() => {
        const at = Date.now() / 1000 - 20;
        const list = document.createElement('ol'); list.id = 't-phone'; document.body.append(list);
        const deps = { api: async () => ({}), operator: () => 'JD', time: () => '17:08:02', readonly: false };
        PreAlert.column(list, null, { open: [{ id: 5, source: 'Example line', at, title: 'Ring', message: 'Ring', ntfy_id: 'x' }] }, deps);
        const gone = document.createElement('ol'); document.body.append(gone);
        PreAlert.column(gone, null, { open: [{ id: 6, source: 'Example line', at, title: 'Ring', message: 'Ring', ntfy_id: 'y' }] }, deps);
        gone.remove();
        window._age = list.querySelector('.pa-age'); window._first = window._age.textContent; window._gone = gone.querySelector('.pa-age').textContent;
        window._goneNode = gone.querySelector('.pa-age');
    }""")
    page.wait_for_function("window._age.textContent !== window._first", timeout=3000)          # it ticks
    page.wait_for_timeout(2200)
    assert page.evaluate("() => window._paSearches") == 0
    assert page.evaluate("() => window._goneNode.textContent === window._gone")              # off the page: let go
