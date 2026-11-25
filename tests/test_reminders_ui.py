"""Reminders on the page (static/reminders.js, cad.css): what flashes where, the OK, reduced motion, the wall board.

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI): invented calls
put in the state the hub's way (a call accepted minutes ago, a rig dispatched minutes ago), then the console and /monitor.
SIZEUP_SHOTS=<dir> saves screenshots of each.
"""
import json
import os
import time
from pathlib import Path

import pytest

from tests.test_cad import uid
from tests.test_settings_no_redraw import Site

SHOTS = os.environ.get("SIZEUP_SHOTS", "")
WHO = {"source": "access", "email": "casey@example.org", "name": "Casey", "callsign": "", "role": "dispatcher",
       "may": "dispatcher", "enforced": True, "active": True, "admins": []}


@pytest.fixture
def site(tmp_path, monkeypatch):
    from sizeup.cad.automode import AutoDispatch
    monkeypatch.setattr(AutoDispatch, "hand_dispatch", lambda self: None)
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


def calls(site, pending_age=400, dp_age=430):
    """A call Pending with no unit for pending_age s, another with E60 Dispatched dp_age s ago (the hub's records moved
    back: as if the time had passed)."""
    cad, now = site.cad, time.time()
    a = cad.create({"type": "ALARM", "subtype": "CO-RESD", "address": "12 Sample St", "operator": "JD"})["id"]
    b = cad.create({"type": "STRU", "address": "40 Example Rd", "operator": "JD"})["id"]
    cad.dispatch(b, {"unit_ids": [uid(cad, "E60")], "operator": "JD"})
    db = cad.store.db
    db.execute("UPDATE incidents SET created_at = ?, accepted_at = ? WHERE id = ?", (now - pending_age, now - pending_age, a))
    db.execute("UPDATE units SET status_at = ? WHERE name = 'E60'", (now - dp_age,))
    db.execute("UPDATE assignments SET assigned_at = ?, dispatched_at = ? WHERE incident_id = ?", (now - dp_age, now - dp_age, b))
    cad.store.rev += 1
    return a, b


def page(browser, site, path="/cad", reduced=False, pc_off_s=0, who=None):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, reduced_motion="reduce" if reduced else "no-preference",
                              color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())         # nothing off this machine
    if who:
        pg.route(site.base + "/api/cad/whoami", lambda route: route.fulfill(status=200, body=json.dumps(who),
                                                                             content_type="application/json"))
    if pc_off_s:                                       # this PC's clock is wrong by that much
        pg.add_init_script(f"(() => {{ const n = Date.now.bind(Date); Date.now = () => n() + {pc_off_s * 1000}; }})()")
    pg.errors, pg.posts = [], []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("request", lambda r: pg.posts.append(r.url) if r.method == "POST" else None)
    pg.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'JD'); } catch (e) {}")
    pg.goto(site.base + path)
    pg.wait_for_function("typeof cad !== \"undefined\" && cad.state && cad.state.reminders && window.Reminders")
    return pg


def snoozed(site):
    """The OK's POST answered: the hub has the snooze."""
    deadline = time.time() + 5
    while not site.cad.store.q("SELECT 1 FROM reminder_snoozes") and time.time() < deadline:
        time.sleep(0.05)
    assert site.cad.store.q("SELECT 1 FROM reminder_snoozes")


def shot(pg, name, sel=None):
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    (pg.locator(sel).first if sel else pg).screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def test_the_console_flashes_the_pending_call_and_the_dispatched_rig_and_an_ok_quiets_it(site, browser):
    a, b = calls(site)
    pg = page(browser, site)
    card = pg.locator(f'#q-pending > li[data-key="{a}"]')
    card.wait_for()
    pg.wait_for_function(f'document.querySelector(\'#q-pending > li[data-key="{a}"]\').dataset.rem === "pending"')
    assert card.locator(".rem-chip").inner_text() == "Pending 6 min"
    style = pg.evaluate(f'(() => {{ const s = getComputedStyle(document.querySelector(\'#q-pending > li[data-key="{a}"]\'));'
                        f' return [s.animationName, s.animationDuration]; }})()')
    assert style == ["rempulse", "2s"]                                   # 0.5 Hz
    row = pg.locator('#units li.urow[data-rem-unit="E60"]')
    pg.wait_for_function('document.querySelector(\'#units li.urow[data-rem-unit="E60"]\')?.dataset.rem === "dispatched"')
    assert row.locator(".rem-chip").inner_text() == "DP 7 min"
    chip = pg.locator(f'#q-active > li[data-key="{b}"] .chip[data-rem-unit="E60"]')
    assert chip.get_attribute("data-rem") == "dispatched"                 # its chip on the call's card too
    # a map's rig (cadmap.js: a button, its code's label the slot): the label flashes, "7 min" after its status, no
    # button inside the rig's own button (the maps need tiles this test doesn't load: the rig as cadmap.js makes it)
    pg.evaluate("""() => { const e = document.createElement('button'); e.className = 'cm-rig rv'; e.id = 't-rig';
      e.dataset.remUnit = 'E60'; e.dataset.remChip = 'age';
      const l = document.createElement('span'); l.className = 'lbl rem-slot'; l.innerHTML = '<span class="n">E60</span><span class="st">DP</span>';
      e.append(l); document.body.append(e); Reminders.dirty(); }""")
    pg.wait_for_function("document.querySelector('#t-rig .rem-chip')?.textContent === '7 min'")
    assert pg.evaluate("[document.querySelector('#t-rig').dataset.rem, document.querySelector('#t-rig .rem-chip').tagName,"
                       " getComputedStyle(document.querySelector('#t-rig .lbl')).animationName]") == ["dispatched", "SPAN", "rempulse"]
    pg.evaluate("document.querySelector('#t-rig').remove()")
    shot(pg, "reminders-console")
    shot(pg, "reminders-queue", ".queue")
    pg.locator(f'#q-active > li[data-key="{b}"]').click()                # the call open: its unit row flashes too
    pg.wait_for_function('document.querySelector(\'#d-units li.ou[data-rem-unit="E60"]\')?.dataset.rem === "dispatched"')
    shot(pg, "reminders-incident", "#detail")
    # the OK: the chip's button, for everyone, in the timeline
    card.locator("button.rem-chip").click()
    pg.wait_for_function(f'!("rem" in document.querySelector(\'#q-pending > li[data-key="{a}"]\').dataset)')
    assert any(u.endswith("/api/cad/reminders/ack") for u in pg.posts)
    deadline = time.time() + 5
    while not site.cad.store.q("SELECT 1 FROM reminder_snoozes") and time.time() < deadline:
        time.sleep(0.05)
    snz = site.cad.store.q("SELECT key, by FROM reminder_snoozes")
    assert [s["key"] for s in snz] == [f"pending:{a}"]
    assert any(e["text"].startswith("Reminder OK: Pending 6 min") for e in site.cad.store.full(a)["events"])
    other = page(browser, site)                                         # another console: quiet as well
    other.wait_for_function('document.querySelector(\'#units li.urow[data-rem-unit="E60"]\')?.dataset.rem === "dispatched"')
    assert other.locator(f'#q-pending > li[data-key="{a}"]').get_attribute("data-rem") is None
    # the state changes: it stops by itself
    site.cad.unit_status(uid(site.cad, "E60"), {"status": "RS", "operator": "JD"})
    pg.wait_for_function('!("rem" in document.querySelector(\'#units li.urow[data-rem-unit="E60"]\').dataset)')
    assert pg.locator('#units li.urow[data-rem-unit="E60"] .rem-chip').count() == 0
    assert pg.errors == [] and other.errors == []


def test_reduced_motion_is_a_steady_highlight(site, browser):
    a, _ = calls(site)
    pg = page(browser, site, reduced=True)
    sel = f'#q-pending > li[data-key="{a}"]'
    pg.wait_for_function(f'document.querySelector(\'{sel}\')?.dataset.rem === "pending"')
    s = pg.evaluate(f'(() => {{ const s = getComputedStyle(document.querySelector(\'{sel}\'));'
                    ' return [s.animationName, s.outlineStyle, s.backgroundColor]; })()')
    assert s[0] == "none" and s[1] == "solid" and s[2] not in ("", "rgba(0, 0, 0, 0)")
    shot(pg, "reminders-reduced-motion", ".queue")
    assert pg.errors == []


@pytest.mark.parametrize("pc_off_s", [3 * 3600, -3 * 3600])
def test_a_wrong_pc_clock_neither_starts_nor_stops_them(site, browser, pc_off_s):
    a, _ = calls(site, pending_age=120, dp_age=400)          # the call: 2 min (not due); E60: 6 min (due)
    pg = page(browser, site, pc_off_s=pc_off_s)
    pg.wait_for_function('document.querySelector(\'#units li.urow[data-rem-unit="E60"]\')?.dataset.rem === "dispatched"')
    pg.wait_for_timeout(1500)                                # a tick or two
    assert pg.locator(f'#q-pending > li[data-key="{a}"]').get_attribute("data-rem") is None
    assert pg.errors == []


def test_the_wall_board_flashes_without_an_ok_unless_a_dispatcher_is_signed_in(site, browser):
    a, b = calls(site)
    pg = page(browser, site, path="/monitor")
    pg.wait_for_function(f'document.querySelector(\'#ov-cards > li[data-key="{a}"]\')?.dataset.rem === "pending"')
    card = pg.locator(f'#ov-cards > li[data-key="{a}"]')
    assert card.locator(".rem-chip").evaluate("n => n.tagName") == "SPAN"          # a plain chip: nothing to press
    assert pg.locator('#b-units [data-rem-unit="E60"]').get_attribute("data-rem") == "dispatched"
    shot(pg, "reminders-monitor")
    card.click(force=True)
    pg.wait_for_timeout(500)
    assert not any(u.endswith("/api/cad/reminders/ack") for u in pg.posts)
    assert site.cad.store.q("SELECT 1 FROM reminder_snoozes") == []
    signed = page(browser, site, path="/monitor", who=WHO)
    signed.wait_for_function(f'document.querySelector(\'#ov-cards > li[data-key="{a}"] .rem-chip\')?.tagName === "BUTTON"')
    assert pg.errors == [] and signed.errors == []


def test_the_settings_card_edits_the_rules(site, browser):
    pg = page(browser, site, path="/cad#settings/dispatch")
    card = pg.locator(".s-card", has=pg.locator("h2", has_text="Reminders"))
    card.wait_for()
    assert card.locator(".s-eta-l").all_inner_texts() == ["Pending, no unit", "Unit dispatched, not moving", "Page not accepted",
                                                         "On scene, no status change", "Past its Until", "Units", "OK quiets"]
    assert "null" not in card.inner_text()
    card.get_by_label("Page not accepted: on").check()
    deadline = time.time() + 5
    while site.cad.settings.section("reminders") != {"queued_on": True} and time.time() < deadline:
        time.sleep(0.05)
    assert site.cad.settings.section("reminders") == {"queued_on": True}
    pg.wait_for_function("[...document.querySelectorAll('.s-card h2')].some((h) => h.textContent === 'Reminders' "
                         "&& h.parentElement.textContent.includes('3 on'))")
    shot(pg, "reminders-settings", ".s-card:has(h2:text-is('Reminders'))")
    assert pg.errors == []


def test_keyboard_ok_keeps_the_focus_and_a_closed_call_stops_flashing(site, browser):
    a, _ = calls(site)
    pg = page(browser, site)
    sel = f'#q-pending > li[data-key="{a}"]'
    pg.wait_for_function(f'document.querySelector(\'{sel}\')?.dataset.rem === "pending"')
    hb = pg.evaluate(f'(() => {{ const c = document.querySelector(\'{sel} .rem-chip\'), t = document.querySelector(\'{sel} .qt\');'
                     ' return [c.getBoundingClientRect().height, t.getBoundingClientRect().height]; })()')
    assert hb[0] <= hb[1] + 1                                             # no taller than its line: the card doesn't grow
    pg.locator(sel).click()                                              # opened: its head flashes too (and that click OKs)
    pg.wait_for_function('!("rem" in document.querySelector(\'#d-card > .card-head\').dataset)')
    snoozed(site)
    site.cad.store.db.execute("DELETE FROM reminder_snoozes")
    site.cad.store.rev += 1
    pg.wait_for_function('document.querySelector(\'#d-card > .card-head\').dataset.rem === "pending"')
    pg.locator(f"{sel} button.rem-chip").focus()
    pg.keyboard.press("Enter")
    pg.wait_for_function(f'!("rem" in document.querySelector(\'{sel}\').dataset)')
    assert pg.evaluate(f"document.activeElement === document.querySelector('{sel}')")
    snoozed(site)
    site.cad.store.db.execute("DELETE FROM reminder_snoozes")
    site.cad.store.rev += 1
    pg.wait_for_function('document.querySelector(\'#d-card > .card-head\').dataset.rem === "pending"')
    site.cad.close(a, {"disposition": "FALSE", "operator": "JD"})
    pg.wait_for_function('!("rem" in document.querySelector(\'#d-card > .card-head\').dataset)'
                         ' && !document.querySelector("#d-rem .rem-chip")')
    assert pg.errors == []
