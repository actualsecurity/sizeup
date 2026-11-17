"""When the Settings page reads the settings again (sizeup/web/static/settings.js sHook, S_POLL; Editor.settings_rev).

An open Settings page used to read GET /api/cad/settings on every move of the CAD's rev, which moves with every call,
unit and transmission: about every 1.5 s on the hub, each read building every section (1-2.5 s there), slowing every
other request. Now it reads again at once only when the settings themselves change (the state's settings_rev), and for
what moves with the calls ("used 3×", a unit's status, the audio kept) on a slower clock: Lists and Units often, the
rest seldom. Settings.page() keeps its usage counts between reads and never parses the imported place names."""
import json
import time

import pytest

from sizeup.cad import settings as settings_mod
from tests.test_cad import make_cad
from tests.test_settings_no_redraw import FIELD, Site  # noqa: F401 - the browser site


# ---- the server ------------------------------------------------------------------------------------------------------

def call(cad, n=1):
    for i in range(n):
        cad.create({"type": "SERVICE", "subtype": "LIFT", "address": f"{i + 1} Example Street", "received_via": "PHONE",
                    "operator": "JD"})


def test_settings_rev_moves_with_a_setting_not_with_a_call(tmp_path):
    cad = make_cad(tmp_path)
    r0 = cad.state()["settings_rev"]
    call(cad, 2)
    st = cad.state()
    assert st["settings_rev"] == r0 and st["rev"] > 0
    cad.settings_post("alert_mutes", {"action": "mute", "key": next(
        k["key"] for k in cad.settings.page()["alert_mutes"]["kinds"] if not k["muted"]), "hours": 1, "operator": "KM"})
    r1 = cad.state()["settings_rev"]
    assert r1 != r0
    cad.settings_post("alert_mutes", {"action": "unmute", "key": next(
        k["key"] for k in cad.settings.page()["alert_mutes"]["kinds"] if k["muted"]), "operator": "KM"})
    assert cad.state()["settings_rev"] not in (r0, r1)


def test_settings_rev_moves_with_a_user_added(tmp_path):
    cad = make_cad(tmp_path)
    r0 = cad.state()["settings_rev"]
    cad.settings_post("users", {"action": "add", "name": "Example Person", "operator": "KM"})
    assert cad.state()["settings_rev"] != r0


def test_the_page_never_parses_the_imported_place_names(tmp_path, monkeypatch):
    cad = make_cad(tmp_path)
    with cad.store.tx() as db:
        for i in range(30):
            db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('places', ?, ?, 0)",
                       (f"sample place {i}", json.dumps({"name": f"Sample Place {i}", "address": f"{i} Example Ave"})))
    parsed = []
    real = settings_mod.json.loads

    def loads(s, *a, **kw):
        out = real(s, *a, **kw)
        if isinstance(out, dict) and str(out.get("name", "")).startswith("Sample Place"):
            parsed.append(out["name"])
        return out
    cad.settings.page()                              # (the place names' own cache filled: _places_cached)
    monkeypatch.setattr(settings_mod.json, "loads", loads)
    cad.settings.page()
    assert parsed == []
    assert "places" not in cad.settings.overrides(skip=("places",)) and "places" in cad.settings.overrides()


def test_the_pages_usage_counts_follow_new_records_and_a_delete_still_counts_exactly(tmp_path, monkeypatch):
    cad = make_cad(tmp_path)
    used = lambda: {r["code"]: r["used"] for r in cad.settings.page()["lists"]["event_types"]["items"]}
    n = used().get("SERVICE", 0)
    call(cad)
    assert used()["SERVICE"] == n + 1                # a new incident: counted again at once
    runs = []
    real = settings_mod.Editor.usage
    monkeypatch.setattr(settings_mod.Editor, "usage", lambda self, sec: runs.append(sec) or real(self, sec))
    used()
    assert runs == []                                 # nothing new: kept
    cad.store.db.execute("UPDATE incidents SET type = 'SERVICE'")       # (no new row: the clock)
    monkeypatch.setattr(settings_mod.Editor, "PAGE_USAGE_S", 0.0)
    used()
    assert "event_types" in runs


def test_the_page_reads_the_units_once(tmp_path, monkeypatch):
    cad = make_cad(tmp_path)
    n = []
    real = cad.store.units
    monkeypatch.setattr(cad.store, "units", lambda *a, **kw: n.append(1) or real(*a, **kw))
    cad.settings.page()
    assert len(n) == 1


# ---- the page --------------------------------------------------------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


@pytest.fixture
def browser_page(site):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium: {e}")
        pg = browser.new_context().new_page()
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
        pg.errors, pg.reads = [], []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("request", lambda r: pg.reads.append(time.monotonic())
              if r.method == "GET" and r.url.split("?")[0].endswith("/api/cad/settings") else None)
        yield pg
        assert pg.errors == []
        browser.close()


def open_at(pg, site, section, ready):
    pg.goto(site.base + f"/cad#settings/{section}")
    pg.fill("#operator", "JD")
    pg.wait_for_selector(ready)
    pg.wait_for_function("() => S.data && !S.loading")
    pg.wait_for_timeout(1500)


def test_calls_running_read_the_settings_seldom_in_system(site, browser_page):
    pg = browser_page
    open_at(pg, site, "system", FIELD)
    n0 = len(pg.reads)
    for _ in range(6):
        site.a_call_runs()
        pg.wait_for_timeout(1000)
    assert len(pg.reads) - n0 <= 1                    # was one a rev: six or more


def test_another_admins_change_is_read_at_once(site, browser_page):
    pg = browser_page
    open_at(pg, site, "system", FIELD)
    site.a_call_runs()
    pg.wait_for_timeout(1200)
    n0 = len(pg.reads)
    t0 = time.monotonic()
    site.another_admin_mutes_an_alert()
    pg.wait_for_function("() => /Muted until/.test(document.querySelector('#s-pushes').innerText)", timeout=5000)
    assert len(pg.reads) > n0 and time.monotonic() - t0 < 5


def test_lists_counts_keep_up_with_the_calls(site, browser_page):
    pg = browser_page
    open_at(pg, site, "lists", "#s-lists form.s-add input.s-name")
    used = "() => [...document.querySelectorAll('#s-lists .s-used')].map((n) => n.textContent).join('|')"
    was = pg.evaluate(used)
    n0 = len(pg.reads)
    site.a_call_runs()
    pg.wait_for_function(f"() => ({used})() !== {json.dumps(was)}", timeout=9000)    # within S_POLL.live (5 s)
    for _ in range(4):
        site.a_call_runs()
        pg.wait_for_timeout(1000)
    assert len(pg.reads) - n0 <= 3                    # at most one every 5 s, not one a rev
