"""Outside units (mutual aid, sizeup/cad/outside.py) on the page: after ours in Assigned, "MA · <department>", "radio"
when only heard, never a status button, a reminder or a follow; their codes among the Radio card's unit keys.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI). Invented
data only: a made-up mutual-aid board (Sampleville FD: E9901), made-up county apparatus and places.
SIZEUP_SHOTS=<dir> saves a screenshot."""

import time

import pytest

import tests.test_settings_no_redraw as nr
from sizeup.sources.runner import SourceRunner
from tests.fake_feed import FakeFeed
from tests.test_cad import make_cad
from tests.test_outside_units import COUNTY, lists_file
from tests.test_radios import tx
from tests.test_reminders_ui import WHO, browser, page, shot  # noqa: F401 - the module's browser fixture


@pytest.fixture
def site(tmp_path, monkeypatch):
    path = lists_file(tmp_path)
    monkeypatch.setattr(nr, "make_cad", lambda tmp: make_cad(tmp, lists=path))
    s = nr.Site(tmp_path, monkeypatch)
    s.cad.outside._county = COUNTY
    yield s
    s.ws.shutdown()


def test_outside_units_follow_ours_with_their_department_and_no_controls(site, browser):  # noqa: F811
    cad, now = site.cad, time.time()
    feed = FakeFeed(clock=time.time)
    runner = SourceRunner([feed], lambda kind, d: cad.source_event(d) if kind == "source_event" else cad.sources.apply(kind, d),
                          log=lambda m: None, clock=time.time)
    feed.put("A1", type="STRUCTURE FIRE", location="40 Example Rd", units=("E60", "L31", "L9902"),
             times={"dispatched": now - 300}, unit_times={"L9902": {"dispatched": now - 290, "on_location": now - 60}},
             extra={"fireground": "FG1", "group_units": {"OURS": ["E60", "L31", "L9902", "R9903", "9905", "DPT99"]}})
    for r in runner.runs:
        runner.step(r)
    iid = cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'A1'")["incident_id"]
    cad.record_traffic(tx("FG1-1", now - 30, rig="E9901", radio_id="9901", text="Engine 9901 on location"))
    pg = page(browser, site, who=WHO)
    pg.locator(f'#q-active > li[data-key="{iid}"]').click()
    pg.wait_for_function("document.querySelectorAll('#d-units li.ou.other').length === 4")
    rows = pg.locator("#d-units > li.ou")
    names = [rows.nth(i).locator("b").first.inner_text() for i in range(rows.count())]
    assert names[:2] == ["E60", "L31"] and set(names[2:]) == {"L9902", "R9903", "9905", "E9901"}    # ours first
    other = pg.locator("#d-units li.ou.other")
    assert other.locator(".ubtns, .u-follow").count() == 0                         # nothing to press, nothing to follow
    assert all(other.nth(i).get_attribute("data-rem-unit") is None for i in range(4))   # never a reminder
    e9901 = pg.locator("#d-units li.ou.other", has_text="E9901")
    assert e9901.locator(".chip.dept").inner_text() == "MA · Sampleville FD" and e9901.locator(".chip.radio").count() == 1
    assert "OnLoc" in pg.locator("#d-units li.ou.other", has_text="L9902").locator(".times").inner_text()
    keys = pg.locator("#d-radio-units [data-unit-filter]")
    assert "E9901" in [keys.nth(i).get_attribute("data-unit-filter") for i in range(keys.count())]
    assert pg.errors == []
    shot(pg, "outside-units", "#d-units")
    shot(pg, "outside-units-incident")
