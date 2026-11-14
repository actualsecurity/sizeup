"""Settings never redraws under someone (sizeup/web/static/settings.js sLoad, sRender, sKeep, sMorph, sPushPost).

The CAD's rev moves with every call, unit status and transmission, and the Settings page used to rebuild its whole
section each time: the page jumped, the cards that fill themselves after a read (Push notifications) went empty for a
moment, and text typed but not saved was gone. A refused save rebuilt the section too, with the reason only at the top
of the page.

Two halves: what holds without a browser (the server's side, and the script's shape), and the page itself in headless
Chromium against a real WebServer and CAD (skipped where Playwright isn't installed: `pip install playwright &&
playwright install chromium`)."""
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from sizeup import notify
from sizeup.cad import dispatcher
from sizeup.cad.api import CadApi
from sizeup.notify import ALERTS, STATUS, NotifyError, Pushes
from tests.test_cad import make_cad

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
# Invented names in the shape of real ones (this repository is public).
OUR_ALERTS, OUR_STATUS = "example-alerts-topic-1234", "example-status-topic-1234"
READ_RING, READ_STATUS = "example-ring-topic-5678", "example-heartbeat-topic-5678"
TYPED = "example-new-topic-90ab"


def fn(name: str) -> str:
    s = (STATIC / "settings.js").read_text()
    m = re.search(rf"^(?:async )?function {name}\(.*?^\}}", s, re.S | re.M)
    assert m, name
    return m.group(0)


class Site:
    """A real WebServer over a real CAD with push topics, and the calls another console would make."""

    def __init__(self, tmp_path, monkeypatch):
        from sizeup.web.server import WebServer
        monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
        self.cad = make_cad(tmp_path)
        self.push_error = ""
        self.sent = []
        self.cad.pushes = Pushes("http://127.0.0.1:9", OUR_ALERTS, OUR_STATUS, sources={ALERTS: "ssm", STATUS: "ssm"},
                                 settings=lambda: self.cad.settings.section(notify.SECTION), log=lambda m: None,
                                 prealert={"ring": (READ_RING, "config"), "status": (READ_STATUS, "config")})
        self.cad.pushes.publish = self.publish
        self.ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                            lambda iid: False, cad=CadApi(self.cad))
        threading.Thread(target=self.ws.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.ws.httpd.server_address[1]}"

    def publish(self, server, topic, title, body, **kw):
        if self.push_error:
            raise NotifyError(self.push_error)
        self.sent.append(topic)
        return {"id": "m1"}

    def j(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def another_admin_mutes_an_alert(self):
        """A change to the settings themselves from another console: the page has something to redraw."""
        key = self.j("GET", "/api/cad/settings")[1]["alert_mutes"]["kinds"][0]["key"]
        code, out = self.j("POST", "/api/cad/settings/alert_mutes", {"action": "mute", "key": key, "hours": 1, "operator": "KM"})
        assert code == 200, out
        return self.cad.store.rev

    def a_call_runs(self):
        """What moves the CAD's rev all day: a call comes in, a unit goes, its status changes."""
        code, inc = self.j("POST", "/api/cad/incident", {"type": "SERVICE", "subtype": "LIFT", "address": "1 Example Street",
                                                         "received_via": "PHONE", "operator": "JD"})
        assert code == 200, inc
        unit = self.cad.store.units()[0]["id"]
        assert self.j("POST", f"/api/cad/incident/{inc['id']}/dispatch",
                      {"unit_ids": [unit], "operator": "JD", "channel": "PC repeater"})[0] == 200
        assert self.j("POST", f"/api/cad/unit/{unit}/status", {"status": "OL", "operator": "JD"})[0] == 200
        return self.cad.store.rev


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


# ---- without a browser -----------------------------------------------------------------------------------------------

def test_a_running_call_moves_the_rev_but_not_what_settings_shows(site):
    """The premise: GET /api/cad/settings after a call, a dispatch and a status change differs only in its rev and in
    what Lists and Units show of the calls (an item's "used 12×" and "3 now", a unit's status and its call), so the
    other sections have nothing to redraw."""
    def page():
        return site.j("GET", "/api/cad/settings")[1]

    def strip(v, drop):
        if isinstance(v, dict):
            return {k: strip(x, drop) for k, x in v.items() if k not in drop}
        return [strip(x, drop) for x in v] if isinstance(v, list) else v

    def quiet(d):
        return dict(d, rev=0, lists=strip(d["lists"], {"used", "now"}), units=strip(d["units"], {"used", "status", "incident_number"}))

    before = page()
    site.a_call_runs()
    after = page()
    assert after["rev"] != before["rev"]
    assert dict(after, rev=0) != dict(before, rev=0)                                     # what moves with the calls moved
    assert quiet(after) == quiet(before)
    s = (STATIC / "settings.js").read_text()                                             # ... and that is what it compares
    assert 'const S_LIVE_KEYS = { lists: new Set(["used", "now"]), units: new Set(["used", "status", "incident_number"]) };' in s
    assert 'const S_LIVE = new Set(["lists", "units"]);' in s
    load = fn("sLoad")
    assert "const same = !!S.data && sig === S.sig && (live === S.sigLive || !S_LIVE.has(S.section));" in load
    assert "if (!same || opts.must) sRender(false, !opts.fresh);" in load
    assert "if (same || S.deferred || S.pressDeferred) sRefresh();" in load


def test_a_refused_topic_answers_400_with_the_reason_the_page_puts_under_the_field(site):
    code, out = site.j("POST", "/api/cad/settings/notify", {"action": "set", "key": "alerts", "topic": READ_RING, "operator": "JD"})
    assert code == 400 and out["error"].startswith("Alerts topic: that is a protected topic")
    post = fn("sPushPost")
    assert "sRender" not in post and "sPost(" not in post                    # no redraw: the field keeps what was typed
    assert "sPushErr(card, key, `${" in post and "sPushWhy(e, label)" in post
    assert post.index("sPushErr(card, key, `${") < post.index("sMsg(done, true)")
    # every button of the card goes through it (or the test push, which reports the same way): none through sPost,
    # whose refusal is the line at the top of the page
    card = fn("sPushes")
    assert "sPost(" not in card and card.count("sPushPost(card, ") == 8 and card.count("sPushErrLine(") == 4
    test = fn("sPushTest")
    assert "sMsg(e.message)" not in test and test.count("sPushErr(card, k.kind, ") == 3


def test_the_script_redraws_through_skeep_and_updates_the_push_card_in_place():
    s = (STATIC / "settings.js").read_text()
    assert "if (fn) sKeep(panel, () => fn(panel), keep && !(fresh && !force));" in fn("sRender")
    assert "sPressing()" in fn("sRender")                                     # never between a button's press and its click
    assert fn("sPost").count("sRender(true, true)") == 2                      # a refusal: what was typed stays
    card = fn("sPushes")
    assert "if (card.dataset.built) { sMorph(card, next); return; }" in card
    assert "card.dataset.built) return;" in card                              # a refresh that failed leaves the card alone
    system = fn("sSystem")
    for part in ("s-counts", "s-audit", "s-alarms", "s-pushes", "s-intel"):   # the parts that fill themselves keep their node
        assert f'kept("{part}", ' in system, part
    assert "sDeferredNow(false)" in s and "sDeferredNow(true)" in s


# ---- in the browser --------------------------------------------------------------------------------------------------

@pytest.fixture
def page(site):
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs Playwright (pip install playwright; playwright install chromium)")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {str(e).splitlines()[0]}")
        ctx = browser.new_context(viewport={"width": 1366, "height": 800})
        pg = ctx.new_page()
        pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())     # nothing off this machine
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(site.base + "/cad#settings/system")
        pg.fill("#operator", "JD")
        pg.wait_for_selector("#s-pushes[data-built] input[data-key=alerts]")
        pg.wait_for_function("document.querySelector('#s-audit table') && document.querySelector('#s-counts div')")
        yield pg
        assert pg.errors == []
        browser.close()


MARK = """() => {            // remember the nodes on screen: a redraw would replace them
  const sys = document.querySelector("#s-system");
  window._nodes = [sys.firstElementChild, sys.querySelector("#s-pushes"), sys.querySelector("#s-pushes input[data-key=alerts]"),
    sys.querySelector("#s-pushes .row"), sys.lastElementChild];
}"""
SAME = """() => {
  const sys = document.querySelector("#s-system");
  const now = [sys.firstElementChild, sys.querySelector("#s-pushes"), sys.querySelector("#s-pushes input[data-key=alerts]"),
    sys.querySelector("#s-pushes .row"), sys.lastElementChild];
  return now.map((n, i) => n === window._nodes[i]);
}"""
SCROLL = "() => document.querySelector('#settings .s-content').scrollTop"
FIELD = "#s-pushes input[data-key=alerts]"


def scroll_to_pushes(pg) -> int:
    pg.evaluate("""() => { const sc = document.querySelector('#settings .s-content');
        sc.scrollTop = document.querySelector('#s-pushes').offsetTop - 40; }""")
    top = pg.evaluate(SCROLL)
    assert top > 100
    return top


def caught_up(pg, rev) -> None:
    """The page has read the settings at this rev (sHook -> sLoad), and anything it started after that has landed."""
    pg.wait_for_function("(rev) => S.data && S.data.rev === rev && !S.loading", arg=rev)
    pg.wait_for_timeout(300)


def test_calls_running_redraw_nothing_under_a_field_with_the_focus(site, page):
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    page.click(FIELD)
    page.keyboard.type(TYPED)
    page.evaluate("() => document.activeElement.setSelectionRange(3, 7)")
    caught_up(page, site.a_call_runs())
    assert page.evaluate(SAME) == [True] * 5
    assert page.input_value(FIELD) == TYPED
    assert page.evaluate("() => { const a = document.activeElement; return [a.dataset.key, a.selectionStart, a.selectionEnd]; }") == ["alerts", 3, 7]
    assert page.evaluate(SCROLL) == top
    assert page.evaluate("() => S.deferred") is False                         # nothing is waiting to redraw on blur either


def test_calls_running_redraw_nothing_while_reading_with_text_typed_and_the_focus_elsewhere(site, page):
    """What was reported: in the section, not in a field (nothing held the redraw back), the page jumped."""
    page.fill(FIELD, TYPED)
    page.evaluate("() => document.activeElement.blur()")
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    tops = page.evaluate("""() => { window._tops = []; const sc = document.querySelector('#settings .s-content');
        const card = document.querySelector('#s-pushes');
        new MutationObserver(() => window._tops.push([sc.scrollTop, card.childElementCount])).observe(
          document.querySelector('#s-system'), { childList: true, subtree: true }); }""")
    for _ in range(3):
        caught_up(page, site.a_call_runs())
    assert page.evaluate(SAME) == [True] * 5
    assert page.input_value(FIELD) == TYPED
    assert page.evaluate(SCROLL) == top
    seen = page.evaluate("() => window._tops")
    assert all(t == top and n > 5 for t, n in seen), seen                     # never moved, the card never emptied


def test_a_push_elsewhere_updates_its_line_in_place(site, page):
    """The card still reports: another console's test push shows on its line, the field beside it untouched."""
    page.fill(FIELD, TYPED)
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    assert "Test " not in page.inner_text("#s-pushes")
    assert site.j("POST", "/api/cad/settings/notify", {"action": "test", "key": "alerts", "operator": "KM"})[0] == 200
    assert site.sent == [OUR_ALERTS]
    caught_up(page, site.cad.store.rev)
    page.wait_for_function(r"() => /Test \d\d:\d\d(:\d\d)? sent/.test(document.querySelector('#s-pushes').innerText)")
    assert page.evaluate(SAME) == [True] * 5 and page.input_value(FIELD) == TYPED and page.evaluate(SCROLL) == top


def test_someone_elses_settings_change_redraws_but_keeps_scroll_focus_and_typed_text(site, page):
    top = scroll_to_pushes(page)
    page.click(FIELD)
    page.keyboard.type(TYPED)
    page.evaluate(MARK)
    code, out = site.j("POST", "/api/cad/settings/notify", {"action": "set", "key": "status", "topic": "example-other-topic-cdef", "operator": "KM"})
    assert code == 200, out
    caught_up(page, site.another_admin_mutes_an_alert())
    # in the field: the section waits (as before), the card shows their topic in place meanwhile
    assert page.evaluate("() => S.deferred") is True
    assert "example-other-topic-cdef" in page.inner_text("#s-pushes")
    assert page.evaluate(SAME) == [True] * 5 and page.input_value(FIELD) == TYPED
    assert page.evaluate("() => document.activeElement.dataset.key") == "alerts" and page.evaluate(SCROLL) == top
    # out of the field: the section is drawn again (their mute shows), around the card, the typed text and the place
    page.evaluate("() => document.activeElement.blur()")
    page.wait_for_function("() => !S.deferred && /Muted until/.test(document.querySelector('#s-pushes').innerText)")
    page.wait_for_timeout(300)
    same = page.evaluate(SAME)
    assert same[0] is False and same[1:3] == [True, True]                      # redrawn; the card and the field: the same nodes
    assert page.input_value(FIELD) == TYPED and page.evaluate(SCROLL) == top


def test_a_refused_save_says_why_under_the_field_and_keeps_what_was_typed(site, page):
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    page.fill(FIELD, READ_RING)                                               # the topic sizeup reads: never published to
    with page.expect_response(lambda r: r.url.endswith("/api/cad/settings/notify")) as got:
        page.click("#s-pushes .row:has(input[data-key=alerts]) button:text-is('Save')")
    assert got.value.status == 400
    err = page.locator("#s-pushes [data-err=alerts]")
    err.wait_for(state="visible")
    assert err.inner_text().startswith("Not saved: that is a protected topic")
    assert err.get_attribute("role") == "alert" and page.get_attribute(FIELD, "aria-invalid") == "true"
    assert page.input_value(FIELD) == READ_RING and page.evaluate(SAME) == [True] * 5 and page.evaluate(SCROLL) == top
    box, row = err.bounding_box(), page.locator("#s-pushes .row:has(input[data-key=alerts])").bounding_box()
    assert 0 <= box["y"] - (row["y"] + row["height"]) < 24 and box["y"] < 800  # right under its row, on screen
    assert page.inner_text("#s-msg") == ""                                    # (not also at the top of the page)
    caught_up(page, site.a_call_runs())                                       # it stays through the refreshes ...
    assert err.inner_text().startswith("Not saved: that is a protected topic") and page.input_value(FIELD) == READ_RING
    page.click(FIELD)
    page.keyboard.press("End")
    page.keyboard.type("-x")                                                  # ... until the field is edited
    assert err.inner_text() == "" and not err.is_visible() and page.get_attribute(FIELD, "aria-invalid") is None
    assert site.cad.pushes.effective(ALERTS) == {"topic": OUR_ALERTS, "source": "ssm"}


def test_a_save_that_takes_updates_the_card_in_place(site, page):
    top = scroll_to_pushes(page)
    page.fill("#s-pushes input[data-key=status]", "half-typed-elsewhere")
    page.fill(FIELD, f"  {TYPED} ")
    page.click("#s-pushes .row:has(input[data-key=alerts]) button:text-is('Save')")
    page.wait_for_function("(t) => document.querySelector('#s-pushes').innerText.includes('In effect: ' + t)", arg=TYPED)
    caught_up(page, site.cad.store.rev)
    assert site.cad.pushes.effective(ALERTS) == {"topic": TYPED, "source": "settings"}
    assert page.input_value(FIELD) == TYPED                                    # as saved
    assert page.input_value("#s-pushes input[data-key=status]") == "half-typed-elsewhere"
    assert page.locator("#s-pushes .row:has(input[data-key=alerts]) button:text-is('Use default')").count() == 1
    assert page.evaluate(SCROLL) == top


def test_a_test_push_that_fails_and_a_server_that_does_not_answer_say_so_under_the_field(site, page):
    scroll_to_pushes(page)
    row = "#s-pushes .row:has(input[data-key=alerts])"
    site.push_error = "ntfy answered 507"
    page.click(f"{row} button:text-is('Send test push')")
    err = page.locator("#s-pushes [data-err=alerts]")
    err.wait_for(state="visible")
    assert err.inner_text() == "Test push not sent: ntfy answered 507"
    site.push_error = ""
    page.click(f"{row} button:text-is('Send test push')")
    page.wait_for_function("() => document.querySelector('#s-pushes [data-err=alerts]').textContent === ''")
    # the hub gone (a 502 that isn't JSON), then the network
    page.fill(FIELD, TYPED)
    page.route("**/api/cad/settings/notify", lambda route: route.fulfill(status=502, body="Bad Gateway"))
    page.click(f"{row} button:text-is('Save')")
    err.wait_for(state="visible")
    assert err.inner_text().startswith("Not saved: 502") and page.input_value(FIELD) == TYPED
    page.unroute("**/api/cad/settings/notify")
    page.route("**/api/cad/settings/notify", lambda route: route.abort())
    page.click(f"{row} button:text-is('Save')")
    page.wait_for_function("() => document.querySelector('#s-pushes [data-err=alerts]').textContent === 'Not saved: no answer from the server'")
    assert page.input_value(FIELD) == TYPED
    page.click(f"{row} button:text-is('Turn off')")
    page.click("[role=alertdialog] button:text-is('Turn off')")
    page.wait_for_function("() => document.querySelector('#s-pushes [data-err=alerts]').textContent === 'Not changed: no answer from the server'")
    assert site.cad.pushes.effective(ALERTS) == {"topic": OUR_ALERTS, "source": "ssm"}


def test_a_redraw_that_waited_for_the_field_never_swallows_the_click_on_save(site, page):
    """Another admin's change while typing waits for the field to lose the focus; the press on Save is what takes it
    away, and the redraw used to replace the button before its click."""
    scroll_to_pushes(page)
    page.click(FIELD)
    page.keyboard.type(TYPED)
    caught_up(page, site.another_admin_mutes_an_alert())
    assert page.evaluate("() => S.deferred") is True
    # a browser that doesn't focus a button on its press (Safari): the focus goes to the page
    page.evaluate("""() => { for (const b of document.querySelectorAll('#s-pushes button'))
        b.addEventListener('mousedown', (e) => { e.preventDefault(); document.activeElement.blur(); }); }""")
    page.click("#s-pushes .row:has(input[data-key=alerts]) button:text-is('Save')")
    page.wait_for_function("(t) => document.querySelector('#s-pushes').innerText.includes('In effect: ' + t)", arg=TYPED)
    assert site.cad.pushes.effective(ALERTS) == {"topic": TYPED, "source": "settings"}


def test_other_sections_keep_typed_text_and_scroll_through_a_refused_save(site, page):
    """The same rule outside Push notifications (sPost): a refusal puts the switches back, not the text."""
    page.fill("#s-pushes input[data-key=status]", "half-typed-elsewhere")
    scroll_to_pushes(page)
    where = "() => Math.round(document.querySelector('#s-pushes input[data-key=status]').getBoundingClientRect().top)"
    top = page.evaluate(where)               # (on screen: the line at the top of the page takes room of its own)
    page.route("**/api/cad/settings/alert_mutes", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": "example refusal"})))
    box = page.locator("#s-pushes label.check input[type=checkbox]").first
    box.click()
    page.wait_for_function("() => document.querySelector('#s-msg').textContent === 'example refusal'")
    page.wait_for_timeout(300)
    # ... and its reason is readable: over the section's head, which is sticky at the same place
    assert page.evaluate("""() => { const m = document.querySelector('#s-msg'), r = m.getBoundingClientRect();
        return m.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)); }""") is True
    assert box.is_checked() is False                                           # the switch is back to what is saved
    assert page.input_value("#s-pushes input[data-key=status]") == "half-typed-elsewhere"
    assert abs(page.evaluate(where) - top) <= 1


def test_lists_redraws_for_its_counts_and_keeps_a_half_filled_add_form(site, page):
    """Lists shows what moves with the calls ("used 2×"), so a call does redraw it: around what is typed in its add form.
    A refused add keeps the form as typed; one that takes clears it."""
    page.goto(site.base + "/cad#settings/lists")
    page.wait_for_selector("#s-lists form.s-add input.s-name")
    used = "() => [...document.querySelectorAll('#s-lists .s-used')].map((n) => n.textContent).join('|')"
    was = page.evaluate(used)
    page.fill("#s-lists form.s-add input.s-name", "Example type")
    page.evaluate("() => { document.activeElement.blur(); window._form = document.querySelector('#s-lists form.s-add'); }")
    caught_up(page, site.a_call_runs())
    assert page.evaluate(used) != was                                          # the count moved on the page
    assert page.evaluate("() => document.querySelector('#s-lists form.s-add') !== window._form")     # (a redraw)
    assert page.input_value("#s-lists form.s-add input.s-name") == "Example type"
    page.fill("#s-lists form.s-add input.s-code-in", "SERVICE")               # taken: the server refuses
    with page.expect_response(lambda r: r.url.endswith("/api/cad/settings/event_types")) as got:
        page.click("#s-lists form.s-add button:text-is('Add')")
    assert got.value.status == 400
    page.wait_for_function("() => /SERVICE is already in/.test(document.querySelector('#s-msg').textContent)")
    page.wait_for_timeout(200)
    assert page.input_value("#s-lists form.s-add input.s-code-in") == "SERVICE"
    assert page.input_value("#s-lists form.s-add input.s-name") == "Example type"
    page.fill("#s-lists form.s-add input.s-code-in", "EXAMPLE")
    page.click("#s-lists form.s-add button:text-is('Add')")
    page.wait_for_function("() => [...document.querySelectorAll('#s-lists li')].some((li) => /Example type/.test(li.textContent))")
    page.evaluate("() => document.activeElement.blur()")
    page.wait_for_function("() => document.querySelector('#s-lists form.s-add input.s-name').value === ''")
    assert page.input_value("#s-lists form.s-add input.s-code-in") == ""       # saved: the form starts clean


def test_every_section_opens_and_sits_through_a_call_without_an_error(site, page):
    for sec in ("lists", "runcards", "units", "dispatch", "users", "radios", "data", "system"):
        page.goto(site.base + f"/cad#settings/{sec}")
        page.wait_for_function("(sec) => S.section === sec && S.data && !S.loading", arg=sec)
        caught_up(page, site.a_call_runs())
        assert page.evaluate(f"() => !document.querySelector('#s-{sec}').hidden"), sec
