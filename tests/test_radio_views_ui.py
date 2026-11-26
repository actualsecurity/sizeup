"""The Radio rail's views edited in the browser (static/radio-views.js, radio-nav.js): the rail's ✎ (show / hide, a view
of one's own) and Settings > Radios > Views (the department's, saved for everyone and audited), the rail following both;
every view hidden takes the VIEWS away.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI). Invented
transmissions only. SIZEUP_SHOTS=<dir> saves screenshots."""
import os
import time
from pathlib import Path

import pytest

from tests.test_comms_views import other
from tests.test_radios import tx
from tests.test_reminders_ui import browser  # noqa: F401  (the fixture)
from tests.test_settings_no_redraw import Site

SHOTS = os.environ.get("SIZEUP_SHOTS", "")


@pytest.fixture
def site(tmp_path, monkeypatch):
    from sizeup.cad.automode import AutoDispatch
    monkeypatch.setattr(AutoDispatch, "hand_dispatch", lambda self: None)
    s = Site(tmp_path, monkeypatch)
    t = time.time()
    s.cad.record_traffic(tx("fg-1", t - 50, rig="E63", text="Engine 63 on location, nothing showing"))
    s.cad.record_traffic(other("pd-1", t - 20, text="Car 7 out at the made-up plaza"))
    s.cad.record_traffic(other("tr-1", t - 5, 9002, "Rail Ops", "transit", "Transit", "record", text="Train 12 holding"))
    yield s
    s.ws.shutdown()


def shot(pg, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def open_page(browser, site, path):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'JD'); localStorage.setItem('sizeup.cad.radio.nav', "
                       "JSON.stringify({ on: true })); } catch (e) {}")
    pg.goto(site.base + path)
    pg.wait_for_function("typeof cad !== 'undefined' && cad.state && window.RadioViews")
    return pg


def tabs(pg):
    return pg.eval_on_selector_all("#rn-views [data-view]", "bs => bs.map((b) => b.dataset.view)")


def test_views_edited_in_the_rail_and_in_settings(site, browser):  # noqa: F811
    pg = open_page(browser, site, "/cad#radio?ui=2")
    pg.wait_for_function("document.querySelectorAll('#rn-views [data-view]').length >= 3")
    assert tabs(pg) == ["ours", "police", "transit", "all"]                   # the coded ones (no EMS, no Air source)
    shot(pg, "views-rail")

    # the rail's ✎: Police hidden, a view of one's own (units E63) added
    pg.click("#rn-vedit")
    assert pg.get_attribute("#rn-vedit", "aria-expanded") == "true" and pg.is_hidden("#rn-views")
    pg.uncheck('#rn-vbox [data-show="police"]')
    pg.click('#rn-vbox [data-act="add"]')
    pg.fill('#rn-vbox [data-f="name"]', "Engine 63")
    pg.fill('#rn-vbox [data-f="mark"]', "6")
    pg.fill('#rn-vbox [data-f="units"]', "E63")
    shot(pg, "views-rail-form")
    pg.click('#rn-vbox [data-act="save"]')
    pg.wait_for_selector('#rn-vbox .rv-ed-row:has-text("Engine 63")')
    shot(pg, "views-rail-edit")
    pg.click('#rn-vbox [data-act="done"]')
    pg.wait_for_function("!document.querySelector('#rn-views').hidden")
    got = tabs(pg)
    assert "police" not in got and got[-1].startswith("p:")
    pg.click(f'#rn-views [data-view="{got[-1]}"]')
    pg.wait_for_function("document.querySelector('#rn-crumb').textContent === 'Engine 63'")
    pg.wait_for_function("[...document.querySelectorAll('#rn-list li')].length === 1")
    assert "Engine 63 on location" in pg.inner_text("#rn-list")

    # Settings > Radios > Views: a department view, saved for everyone and audited
    pg.goto(site.base + "/cad#settings/radios/views")
    pg.wait_for_selector('#settings-views [data-act="add"]')
    shot(pg, "views-settings")
    pg.click('#settings-views [data-act="add"]')
    pg.fill('#settings-views [data-f="name"]', "Metro")
    pg.fill('#settings-views [data-f="mark"]', "m")
    pg.check('#settings-views [data-list="systems"][data-v="metro"]')
    pg.click('#settings-views [data-act="save"]')
    pg.wait_for_selector('#settings-views .rv-ed-row:has-text("Metro")')
    views = site.cad.radio_views
    assert [v["name"] for v in views][-1] == "Metro" and views[-1]["mark"] == "M" and views[-1]["filter"] == {"systems": ["metro"]}
    assert [r for r in site.cad.settings.audit_log("radios") if r["key"] == "radio_views/views"]
    # ... and up one: the department's order
    pg.click('#settings-views [data-act="up"][data-id="%s"]' % views[-1]["id"])
    pg.wait_for_function("document.querySelector('#settings-views .rv-ed-row:nth-last-child(2)').textContent.includes('Metro')")

    # back on the rail: the department's new view, its traffic
    pg.goto(site.base + "/cad#radio")
    pg.wait_for_function("[...document.querySelectorAll('#rn-views [data-view]')].some((b) => b.textContent.includes('Metro'))")
    pg.click('#rn-views [data-view^="u:"]')
    pg.wait_for_function("[...document.querySelectorAll('#rn-list li')].length === 2")
    shot(pg, "views-rail-dept")

    # every view hidden: no VIEWS header, everything on screen
    pg.click("#rn-vedit")
    while pg.locator("#rn-vbox [data-show]:checked").count():      # (each one draws the list again)
        pg.locator("#rn-vbox [data-show]:checked").first.click()
    pg.click('#rn-vbox [data-act="done"]')
    pg.wait_for_function("document.querySelector('#rn-views-h').hidden && document.querySelector('#rn-views').hidden")
    pg.wait_for_function("[...document.querySelectorAll('#rn-list li')].length === 3")
    shot(pg, "views-all-hidden")
    assert pg.errors == []
