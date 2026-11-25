"""History here's entries peek at a past call over the current view; Quiet edit and the labelled lines on the page.

The admin, 2026-11-25: "clicking an entry under 'History here' ... should open it in a separate window or overlay. You
read it and close it, and you're back on the incident you were on, without the main view switching to the closed one."

    peek        a click: the past call read-only in #peek-overlay (summary, location, units, comments, timeline); Close,
                Esc or the scrim back to the same History entry (focus), the selected call never changes; "Open" is
                the one way to switch to it
    new tab     the entry is a link to <page>#peek/<id>: ⌘ / Ctrl / middle click is the browser's (a new tab); the deep
                link opens the overlay over the console, and closing it drops the hash
    quiet edit  a closed call's Edit is "Quiet edit": saved quietly, it stays closed, its line marked "after close"

In headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed), invented calls.
History here's list (GET .../here) is routed to name the past call: the index itself is tests/test_address_history.py's.
SIZEUP_SHOTS=<dir> saves screenshots.
"""
import json
import os
import time
from pathlib import Path

import pytest

from tests.test_cad import uid
from tests.test_prealert_action_row import browser, site  # noqa: F401  (fixtures)

SHOTS = os.environ.get("SIZEUP_SHOTS", "")
ADDRESS = "12 Example Ave"


def calls(site):
    """A past Service call at the address (closed, then a quiet edit after the close) and the call open there now."""
    cad = site.cad
    past = cad.create({"type": "SERVICE", "address": ADDRESS, "remarks": "Invented: stuck lift, one inside",
                       "operator": "Casey Example"})["id"]
    cad.dispatch(past, {"unit_ids": [uid(cad, "E60")], "operator": "Casey Example"})
    cad.note(past, {"text": "E60 on location", "operator": "Casey Example"})
    cad.close(past, {"disposition": "COMPLETE", "operator": "Casey Example"})
    cad.update(past, {"cross_street": "Sample St", "quiet": True, "operator": "Robin Example"})
    now = cad.create({"type": "ALARM", "subtype": "COMM", "address": ADDRESS, "operator": "Casey Example"})["id"]
    return past, now


def here_body(site, past):
    p = site.cad.store.incident(past)
    return {"key": {"bldg_key": "k1", "addr_key": "k1", "addr_how": "", "addr_label": ADDRESS}, "total": 1,
            "recent": [{"id": past, "number": p["number"], "created_at": p["created_at"], "type": p["type"],
                        "subtype": p["subtype"], "disposition": p["disposition"], "status": p["status"], "units": ["E60"]}],
            "year": {"n": 1, "by_type": [{"type": p["type"], "n": 1}]}, "false_alarms": {"n": 0, "calls": []}, "notes": []}


def open_page(browser, site, past, path="/cad"):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
    body = json.dumps(here_body(site, past))
    pg.route(lambda url: url.startswith(site.base) and "/here" in url and "/api/cad/incident/" in url,
             lambda route: route.fulfill(status=200, content_type="application/json", body=body))
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(site.base + path)
    pg.wait_for_function("typeof cad !== 'undefined' && cad.state && cad.state.incidents && cad.state.incidents.length >= 2")
    return ctx, pg


def on_current_call(pg, now):
    pg.evaluate(f"selectIncident({now})")
    pg.wait_for_function("!document.querySelector('#d-here').hidden")
    if pg.get_attribute("#d-here-toggle", "aria-expanded") != "true":
        pg.click("#d-here-toggle")
    pg.wait_for_selector("#d-here-body .here-call")


def test_a_click_peeks_read_only_and_esc_comes_back_to_the_same_entry(site, browser):  # noqa: F811
    past, now = calls(site)
    number, current = site.cad.store.incident(past)["number"], site.cad.store.incident(now)["number"]
    ctx, pg = open_page(browser, site, past)
    on_current_call(pg, now)
    entry = f"#d-here-body .here-call[data-iid='{past}']"
    assert pg.get_attribute(entry, "href") == f"/cad#peek/{past}"                     # a link: ⌘-click, a new tab
    pg.focus(entry)
    pg.keyboard.press("Enter")                                                     # (a keyboard click)
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden && document.querySelector('#peek-body .pk-sec')")
    assert pg.evaluate("cad.sel") == now and pg.inner_text("#d-number") == current   # the main view never moved
    assert pg.evaluate("location.hash") == ""
    title = pg.inner_text("#peek-title")
    assert number in title and pg.inner_text("#peek-status").strip()
    body = pg.inner_text("#peek-body")
    for said in (ADDRESS, "Sample St", "E60", "Invented: stuck lift, one inside", "E60 on location", "Event closed"):
        assert said in body, said
    # read-only: no field, no action but Close / Open / New tab
    assert pg.evaluate("document.querySelectorAll('#peek-overlay :is(input, select, textarea)').length") == 0
    assert pg.evaluate("[...document.querySelectorAll('#peek-overlay :is(button, a)')].filter((n) => !n.hidden)"
                       ".map((n) => n.id)") == ["peek-new", "peek-open", "peek-close"]
    assert pg.get_attribute("#peek-new", "href") == f"/cad#peek/{past}"
    # the line made after the close: marked, muted, who in its hover
    chip = pg.locator("#peek-body .chip.ev-label").first
    assert chip.inner_text() == "after close" and "Robin Example" in chip.get_attribute("title")
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.screenshot(path=str(Path(SHOTS) / "history-peek.png"))
    pg.keyboard.press("Escape")
    pg.wait_for_function("document.querySelector('#peek-overlay').hidden")
    assert pg.evaluate(f"document.activeElement === document.querySelector(\"{entry}\")")
    assert pg.evaluate("cad.sel") == now and pg.inner_text("#d-number") == current
    # the Close button and the scrim do the same
    pg.click(entry)
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden")
    pg.click("#peek-close")
    pg.wait_for_function("document.querySelector('#peek-overlay').hidden")
    assert pg.evaluate(f"document.activeElement === document.querySelector(\"{entry}\")") and pg.evaluate("cad.sel") == now
    pg.click(entry)
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden")
    pg.mouse.click(5, 500)                                                         # the scrim
    pg.wait_for_function("document.querySelector('#peek-overlay').hidden")
    assert pg.evaluate("cad.sel") == now
    assert pg.errors == []
    ctx.close()


def test_open_is_the_one_way_to_switch_and_cmd_click_is_a_new_tab(site, browser):  # noqa: F811
    past, now = calls(site)
    ctx, pg = open_page(browser, site, past)
    on_current_call(pg, now)
    entry = f"#d-here-body .here-call[data-iid='{past}']"
    with ctx.expect_page() as popup:                                               # ⌘ / Ctrl click: the browser's tab
        pg.click(entry, modifiers=["ControlOrMeta"])
    tab = popup.value
    tab.wait_for_load_state()
    assert tab.url.endswith(f"/cad#peek/{past}")
    assert pg.evaluate("document.querySelector('#peek-overlay').hidden") and pg.evaluate("cad.sel") == now
    tab.close()
    pg.click(entry)
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden && document.querySelector('#peek-body .pk-sec')")
    pg.click("#peek-open")
    pg.wait_for_function(f"document.querySelector('#peek-overlay').hidden && cad.sel === {past}")
    assert pg.errors == []
    ctx.close()


def test_the_deep_link_opens_over_the_console_and_closing_drops_it(site, browser):  # noqa: F811
    past, now = calls(site)
    ctx, pg = open_page(browser, site, past, f"/cad#peek/{past}")
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden && document.querySelector('#peek-body .pk-sec')")
    assert site.cad.store.incident(past)["number"] in pg.inner_text("#peek-title")
    pg.wait_for_function("document.querySelector('#peek-title').textContent.includes('Service')")   # drawn again, named
    pg.keyboard.press("Escape")
    pg.wait_for_function("document.querySelector('#peek-overlay').hidden")
    assert pg.evaluate("location.hash") == "" and pg.evaluate("cad.sel") is None
    assert pg.errors == []
    ctx.close()
    # the wall board too (no History list there, the link works; no Open: it only shows)
    ctx, pg = open_page(browser, site, past, f"/monitor#peek/{past}")
    pg.wait_for_function("!document.querySelector('#peek-overlay').hidden && document.querySelector('#peek-body .pk-sec')")
    assert pg.evaluate("document.querySelector('#peek-open').hidden")
    assert pg.errors == []
    ctx.close()


def test_a_closed_call_is_quiet_edited_and_stays_closed(site, browser):  # noqa: F811
    past, now = calls(site)
    ctx, pg = open_page(browser, site, past)
    pg.evaluate(f"openFromRecords({past})")
    pg.wait_for_function(f"cad.sel === {past} && !document.querySelector('#d-edit').hidden")
    assert pg.inner_text("#d-edit") == "Quiet edit" and pg.evaluate("document.querySelector('#d-reopen').hidden") is False
    pg.click("#d-edit")
    assert pg.evaluate("document.querySelector('#e-quiet-w').hidden") and not pg.evaluate("document.querySelector('#e-closed').hidden")
    pg.fill("#e-cross", "Test Pl")
    pg.click("#edit-save")
    deadline = time.time() + 10
    while site.cad.store.incident(past)["cross_street"] != "Test Pl" and time.time() < deadline:
        time.sleep(0.1)
    inc = site.cad.store.full(past)
    assert inc["status"] == "closed" and inc["cross_street"] == "Test Pl"
    assert (inc["events"][-1]["label"], inc["events"][-1]["kind"]) == ("after close", "edit")
    # an open call's form: the Quiet edit tick, unticked; ticked, its line is "quiet edit" and the status stays
    pg.evaluate(f"selectIncident({now})")
    pg.wait_for_function(f"cad.sel === {now}")
    assert pg.inner_text("#d-edit") == "Edit"
    pg.click("#d-edit")
    assert not pg.evaluate("document.querySelector('#e-quiet-w').hidden") and not pg.is_checked("#e-quiet")
    pg.check("#e-quiet")
    pg.fill("#e-cross", "Sample St")
    pg.click("#edit-save")
    deadline = time.time() + 10
    while site.cad.store.incident(now)["cross_street"] != "Sample St" and time.time() < deadline:
        time.sleep(0.1)
    inc = site.cad.store.full(now)
    assert inc["status"] == "pending" and inc["events"][-1]["label"] == "quiet edit"
    # the comments box's Everything / the Timeline mark it, muted
    pg.wait_for_function("document.querySelector('#d-events .chip.ev-label, #d-cmts-list .chip.ev-label') || "
                         "(document.querySelector('#d-events-toggle').click(), false)", polling=500)
    if SHOTS:
        pg.locator("#d-timeline").scroll_into_view_if_needed()
        pg.screenshot(path=str(Path(SHOTS) / "quiet-edit-timeline.png"))
    assert pg.errors == []
    ctx.close()
