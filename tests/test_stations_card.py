"""Settings > Station > Stations in a browser (sizeup/web/static/stations.js): an admin adds a station and sees its token
once with Copy buttons, Done forgets it, Rotate and Revoke ask first, and nothing of a token is left on the page or in
storage afterwards. Playwright + Chromium; skipped without them. Every id and name is made up.

SIZEUP_SHOTS=<dir> also saves screenshots there (the PR's)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.cloud.hubstore import MonitorDb
from sizeup.cloud.stations import StationRegistry
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_roles import CASEY, JORDAN, people

SHARED = "shared-station-token-0123456789"
SHOTS = os.environ.get("SIZEUP_SHOTS", "")


class Access:
    def identify(self, headers):
        e = headers.get("X-Test-User")
        return Identity(email=e) if e else None


@pytest.fixture
def site(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg = StationRegistry(MonitorDb(tmp_path / "hub.sqlite"), shared=SHARED, log=lambda m: None)
    reg.claim(reg.resolve(SHARED), "st-hq")            # the existing station, on the shared token
    reg.touch(reg.resolve(SHARED))
    cad.stations = reg
    from sizeup.cloud.sttworkers import WorkerRegistry
    cad.stt_workers = WorkerRegistry(reg.mdb, log=lambda m: None)    # (each station's whisper in the STT pool, #639)
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad), access=Access(), role="hub")
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    yield type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "reg": reg, "cad": cad})()
    ws.shutdown()


def browser_page(site, who):
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
    ctx = browser.new_context(viewport={"width": 1280, "height": 760}, extra_http_headers={"X-Test-User": who})
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    return pw, browser, pg


def shot(pg, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.locator("#s-station-stations").screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def test_an_admin_adds_a_station_sees_the_token_once_then_rotates_and_revokes(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/stations")
        card = pg.locator("#s-station-stations .card")
        card.wait_for()
        assert card.locator("tr", has_text="Shared token").locator("td.mono").inner_text() == "st-hq"
        assert card.get_by_text("One station until multi-station").is_visible()
        assert card.locator("label", has_text="Id").locator("input").get_attribute("placeholder") == "st-hq"
        shot(pg, "stations-shared")
        card.locator("label", has_text="Id").locator("input").fill("st-hq")
        card.locator("label", has_text="Name").locator("input").fill("Headquarters")
        card.get_by_role("button", name="Add").click()
        tok = card.locator(".notice.ok p.mono").first
        tok.wait_for()
        token = tok.inner_text()
        assert token.startswith("sz1.st-hq.") and site.reg.resolve(token).id == "st-hq"
        assert card.get_by_role("button", name="Copy token").is_visible()
        shot(pg, "stations-new-token")
        card.get_by_role("button", name="Done").click()
        pg.wait_for_function("!document.querySelector('#s-station-stations .notice.ok')")
        assert token not in pg.content()
        row = card.locator("tr", has_text="Headquarters")
        row.get_by_role("button", name="Rotate").click()
        pg.get_by_role("alertdialog").get_by_role("button", name="Rotate").click()
        card.locator(".notice.ok p.mono").first.wait_for()
        rotated = card.locator(".notice.ok p.mono").first.inner_text()
        assert rotated != token and site.reg.resolve(rotated).id == "st-hq"
        assert card.get_by_text("update GitHub SIZEUP_STATUS_TOKEN within 10 min").is_visible()   # a new status token too
        shot(pg, "stations-rotated")
        card.get_by_role("button", name="Done").click()
        row.get_by_role("button", name="Revoke").click()
        pg.get_by_role("alertdialog").get_by_role("button", name="Revoke").click()
        card.locator("td", has_text="revoked").wait_for()
        shot(pg, "stations-revoked")
        assert pg.evaluate("() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)").count("sz1.") == 0
        assert rotated not in pg.content() and token not in pg.content()
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_a_dispatcher_never_sees_the_tab(site):
    pw, browser, pg = browser_page(site, JORDAN)
    try:
        pg.goto(site.base + "/cad#settings/station/stations")
        pg.wait_for_function("typeof cad !== 'undefined' && !!cad.state && !!cad.who")
        pg.wait_for_timeout(1000)
        assert not pg.locator("#s-station-stations .card").is_visible()
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_an_admin_puts_a_stations_whisper_in_the_stt_pool_from_its_row(site):
    """#639 phase 2: off by default, per station; the button flips it and the Settings audit says so."""
    pw, browser, pg = browser_page(site, CASEY)
    try:
        site.reg.add("st-north", "North")
        pg.goto(site.base + "/cad#settings/station/stations")
        card = pg.locator("#s-station-stations .card")
        card.wait_for()
        row = card.locator("tr", has_text="North")
        row.get_by_role("button", name="Whisper in pool: off").click()
        row.get_by_role("button", name="Whisper in pool: on").wait_for()
        assert site.cad.stt_workers.stations_on("st-north")
        shot(pg, "stations-whisper-in-pool")
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()
