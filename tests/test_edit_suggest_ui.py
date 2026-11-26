"""The Edit form of an open event with no map point pre-selects the gazetteer's one sure reading of its address (static/
cad.js addressBox suggest): an exact address at 90% or more, 30 points ahead of the next, so one Save places it. Seen
live: a mutual-aid call's address in the next town over read 97% (the street alone 37%), yet the form said "No match
picked". A reading that isn't sure picks nothing, as before.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI). Invented
streets and points only (tests/test_feed_place_name_pin.py's gazetteer). SIZEUP_SHOTS=<dir> saves a screenshot."""

from __future__ import annotations

import time

from tests.test_feed_place_name_pin import WEST, as_geocoder, fixture_gazetteer
from tests.test_reminders_ui import browser, page, shot, site  # noqa: F401 - fixtures


def edit(pg, iid: int):
    pg.wait_for_function(f"incidents().some((i) => i.id === {iid})")
    pg.evaluate(f"selectIncident({iid})")
    pg.click("#d-edit")
    pg.wait_for_selector("#edit-form:not([hidden])")


def note(pg) -> str:
    return pg.locator("#e-geo").inner_text()


def test_a_sure_reading_is_pre_selected_and_save_places_the_event(site, browser, tmp_path):  # noqa: F811
    cad = site.cad
    cad.geocoder = None                                       # made with no point (as a page's unconfirmed one)
    iid = cad.create({"type": "STRU", "address": "45 W Sample Greens Dr", "operator": "JD"})["id"]
    other = cad.create({"type": "STRU", "address": "Sample Greens Dr W", "operator": "JD"})["id"]    # a street: not sure
    assert cad.store.incident(iid)["lat"] is None
    cad.geocoder = as_geocoder(fixture_gazetteer(tmp_path / "gaz.sqlite"))
    cad.store.rev += 1
    pg = page(browser, site)
    edit(pg, other)
    pg.wait_for_timeout(600)
    assert note(pg) == "No match picked: saved as typed"
    pg.click("#edit-cancel")
    edit(pg, iid)
    pg.wait_for_function("document.querySelector('#e-geo').textContent.includes('suggested')")
    assert note(pg).startswith("Map: 45 Sample Greens Dr W") and "suggested: Save to place it" in note(pg)
    assert pg.locator("#e-address").input_value() == "45 W Sample Greens Dr"          # the address as it is
    shot(pg, "edit-suggested", "#edit-form")
    pg.click("#edit-save")
    deadline = time.time() + 5
    while cad.store.incident(iid)["lat"] is None and time.time() < deadline:
        time.sleep(0.05)
    inc = cad.store.incident(iid)
    assert (inc["lat"], inc["lon"]) == WEST and inc["address"] == "45 W Sample Greens Dr"
    assert not pg.errors
