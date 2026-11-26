"""One name per radio source in a browser (static/radio-names.js): a transmission's channel chip shows the admins' short
label, its hover the system and the full name, as Settings > Radios > Channels and the rail's Edit set them; before the
names come (or with none set) the real label, as before.

Headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI): the names are
set through the hub's own settings; the radio log's answer is this test's (invented rows). SIZEUP_SHOTS=<dir> saves
before / after screenshots."""
import json
import os
import time

import pytest

from tests.test_reminders_ui import WHO, browser  # noqa: F401  (fixture)
from tests.test_settings_no_redraw import Site

SHOTS = os.environ.get("SIZEUP_SHOTS", "")


def rows(now):
    base = {"kind": "radio", "has_audio": False, "status": "", "events": [], "main": True, "service": "police"}
    return [
        {**base, "id": "t1", "start": now - 30, "end": now - 26, "source": "p25:metro:9001", "system": "metro", "talkgroup": 9001,
         "channel": "PD Dispatch", "talker": "Car 12", "text": "Car 12 en route to the north lot"},
        {**base, "id": "t2", "start": now - 60, "end": now - 57, "source": "p25:metro:9002", "system": "metro", "talkgroup": 9002,
         "channel": "PD Dispatch", "talker": "Car 4", "text": "Car 4 clear"},
        {**base, "id": "t3", "start": now - 90, "end": now - 86, "source": "fm:FG1", "system": "analog", "talkgroup": None,
         "channel": "FG1", "talker": "E60", "service": "fire", "text": "E60 on location"},
    ]


@pytest.fixture
def site(tmp_path, monkeypatch):
    from sizeup.cad.automode import AutoDispatch
    monkeypatch.setattr(AutoDispatch, "hand_dispatch", lambda self: None)
    s = Site(tmp_path, monkeypatch)
    s.cad.source_systems = lambda: {"westchester": "County", "metro": "Metro"}
    yield s
    s.ws.shutdown()


def open_radio(browser, site, names_off=False):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1400, "height": 800}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
    pg.route(site.base + "/api/cad/whoami", lambda route: route.fulfill(status=200, body=json.dumps(WHO), content_type="application/json"))
    now = time.time()
    body = {"rows": rows(now), "rev": "1", "channels": ["FG1", "PD Dispatch"], "rigs": [], "pcfd": [], "events": [],
            "counts": {"main": 3, "other": 0}, "limits": {"main": 200, "other": 300}}
    pg.route(lambda url: "/api/cad/comms?" in url or url.endswith("/api/cad/comms"),
             lambda route: route.fulfill(status=200, body=json.dumps(body), content_type="application/json"))
    if names_off:                                      # as before this change: no resolver answer, the real labels
        pg.route(lambda url: "/api/cad/radio-names" in url, lambda route: route.fulfill(status=404, body="{}"))
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'JD'); localStorage.setItem('sizeup.cad.radio.nav', '{\"on\":false}'); } catch (e) {}")
    pg.goto(site.base + "/cad#radio?ui=1")
    pg.wait_for_function("document.querySelectorAll('#rv-main .chip.src').length === 3")
    return pg


def chips(pg):
    return pg.eval_on_selector_all("#rv-main .chip.src", "(ns) => ns.map((n) => [n.textContent, n.title])")


def test_a_chip_shows_the_one_name_and_its_system_on_hover(browser, site):  # noqa: F811
    cad = site.cad
    for key, name, short in (("p25:metro:9001", "Town PD Dispatch North", "PD North"),
                             ("p25:metro:9002", "Town PD Dispatch South", "")):
        cad.settings_post("radio_folders", {"action": "name", "key": key, "name": name, "operator": "Lt. Test"})
        if short:
            cad.settings_post("radio_folders", {"action": "short", "key": key, "short": short, "operator": "Lt. Test"})
    before = open_radio(browser, site, names_off=True)
    # before: two different talkgroups read the same ("PD Dispatch")
    assert [c[0] for c in chips(before)] == ["PD Dispatch", "PD Dispatch", "FG1"]
    if SHOTS:
        before.screenshot(path=f"{SHOTS}/radio-names-before.png", clip={"x": 0, "y": 0, "width": 1400, "height": 420})
    after = open_radio(browser, site)
    after.wait_for_function("document.querySelector('#rv-main .chip.src').textContent === 'PD North'")
    assert chips(after) == [
        ["PD North", "Metro · Town PD Dispatch North (PD Dispatch, TG 9001)"],
        ["Town PD Dispatch South", "Metro · Town PD Dispatch South (PD Dispatch, TG 9002)"],
        ["FG1", "Analog · FG1"],
    ]
    if SHOTS:
        after.hover("#rv-main .chip.src")
        after.screenshot(path=f"{SHOTS}/radio-names-after.png", clip={"x": 0, "y": 0, "width": 1400, "height": 420})
    # a rename from Settings reaches the open page (the push's radio_names rev moves)
    cad.settings_post("radio_folders", {"action": "short", "key": "p25:metro:9002", "short": "PD South", "operator": "Lt. Test"})
    after.wait_for_function("[...document.querySelectorAll('#rv-main .chip.src')].map((n) => n.textContent).includes('PD South')")
    assert not before.errors and not after.errors, before.errors + after.errors
