"""Settings never redraws under someone (sizeup/web/static/settings.js sLoad, sRender, sKeep, sMorph, sCardSwap,
sPushPost).

The CAD's rev moves with every call, unit status and transmission, and the Settings page used to rebuild its whole
section each time: the page jumped, the cards that fill themselves after a read (Push notifications) went empty for a
moment, and text typed but not saved was gone. A refused save rebuilt the section too, with the reason only at the top
of the page, behind the section's head.

Two halves: what holds without a browser (the server's side, and the script's shape), and the page itself in headless
Chromium against a real WebServer and CAD (skipped where Playwright isn't installed: `pip install playwright &&
playwright install chromium`).

The browser half finds the page's parts by what was there before this work too (labels, the cards' ids), so it can be
run against another commit's page: SIZEUP_SETTINGS_REF=<commit> serves that commit's settings.js and settings.css.
Against the commit before this work, the tests that state the report (typed text kept, the card never emptied, the
refusal readable where it happened) fail on their assertions:

    SIZEUP_SETTINGS_REF=7e051b8 python -m pytest tests/test_settings_no_redraw.py -k "reading or says_why or other_sections"
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from sizeup import notify, sourcekeys
from sizeup.cad import dispatcher
from sizeup.cad.api import CadApi
from sizeup.cloud.hubstore import AudioDb
from sizeup.notify import ALERTS, STATUS, NotifyError, Pushes
from tests.test_cad import make_cad

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
REF = os.environ.get("SIZEUP_SETTINGS_REF", "")
# Invented names in the shape of real ones (this repository is public).
OUR_ALERTS, OUR_STATUS = "example-alerts-topic-1234", "example-status-topic-1234"
READ_RING, READ_STATUS = "example-ring-topic-5678", "example-heartbeat-topic-5678"
TYPED = "example-new-topic-90ab"
CLIP = b"RIFF" + b"\0" * 996


def fn(name: str) -> str:
    s = (STATIC / "settings.js").read_text()
    m = re.search(rf"^(?:async )?function {name}\(.*?^\}}", s, re.S | re.M)
    assert m, name
    return m.group(0)


class Site:
    """A real WebServer over a real CAD with push topics and, like the hub, the audio it keeps (its counts move with
    every transmission); and the calls another console would make."""

    def __init__(self, tmp_path, monkeypatch):
        from sizeup.web.server import WebServer
        monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
        # as on the hub, where everyone is signed in (Settings > System > Intel is read only then)
        monkeypatch.setattr(sourcekeys, "may_see_context", lambda broadcast_ok=False: True)
        self.cad = make_cad(tmp_path)
        self.push_error = ""
        self.sent = []
        self.clips = 0
        self.cad.pushes = Pushes("http://127.0.0.1:9", OUR_ALERTS, OUR_STATUS, sources={ALERTS: "ssm", STATUS: "ssm"},
                                 settings=lambda: self.cad.settings.section(notify.SECTION), log=lambda m: None,
                                 prealert={"ring": (READ_RING, "config"), "status": (READ_STATUS, "config")})
        self.cad.pushes.publish = self.publish
        self.audio = self.cad.audio_blobs = AudioDb(str(tmp_path / "audio.sqlite"))
        for ch in ("FG1", "PC repeater"):
            self.audio.put(f"clip-{ch}", CLIP, at=time.time(), channel=ch)
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

    def transmission(self):
        """A recorded clip: FG1's twice, then the repeater's larger one (the retention rows change order). The counts
        per channel are worked out at most every 5 minutes (AudioDb.channels): here that time has passed."""
        self.clips += 1
        big = self.clips % 3 == 0
        self.audio.put(f"clip-{self.clips}", CLIP * (5 if big else 1), at=time.time(), channel="PC repeater" if big else "FG1")
        self.audio._channels = (float("-inf"), [])

    def another_admin_mutes_an_alert(self):
        """A change to the settings themselves from another console: the page has something to redraw."""
        kinds = self.j("GET", "/api/cad/settings")[1]["alert_mutes"]["kinds"]
        key = next(k["key"] for k in kinds if not k["muted"])
        code, out = self.j("POST", "/api/cad/settings/alert_mutes", {"action": "mute", "key": key, "hours": 1, "operator": "KM"})
        assert code == 200, out
        return self.cad.store.rev

    def a_call_runs(self):
        """What moves the CAD's rev all day: a transmission is recorded, a call comes in, a unit goes, its status changes."""
        self.transmission()
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
    """The premise: GET /api/cad/settings after a transmission, a call, a dispatch and a status change differs only in
    its rev and in what moves with the calls (a list item's "used 12×" and "3 now", a unit's status and its call, the
    clips and bytes kept and the order of their rows), so there is nothing to redraw. The script compares the same."""
    def page():
        return site.j("GET", "/api/cad/settings")[1]

    def strip(v, drop):
        if isinstance(v, dict):
            return {k: strip(x, drop) for k, x in v.items() if k not in drop}
        return [strip(x, drop) for x in v] if isinstance(v, list) else v

    def quiet(d):
        kept = strip(d["audio_retention"], {"clips", "bytes", "totals"})
        kept["rows"].sort(key=lambda r: r["key"])
        return dict(d, rev=0, lists=strip(d["lists"], {"used", "now"}), units=strip(d["units"], {"used", "status", "incident_number"}),
                    audio_retention=kept, camera_wall=strip(d["camera_wall"], {"n", "streams"}))

    before = page()
    assert before["audio_retention"]["totals"]["audio"]["clips"] == 2
    assert [r["key"] for r in before["audio_retention"]["rows"]] == ["FG1", "PC repeater"]
    for _ in range(3):
        site.a_call_runs()
    after = page()
    assert after["rev"] != before["rev"]
    assert after["audio_retention"]["totals"]["audio"]["clips"] == 5
    assert [r["key"] for r in after["audio_retention"]["rows"]] == ["PC repeater", "FG1"]          # largest first: it moved
    for part in ("lists", "units", "audio_retention"):
        assert after[part] != before[part], part                                                    # what moves moved
    assert quiet(after) == quiet(before)
    s = (STATIC / "settings.js").read_text()
    assert ('const S_LIVE_KEYS = { lists: new Set(["used", "now"]), units: new Set(["used", "status", "incident_number"]),\n'
            '  audio_retention: new Set(["clips", "bytes", "totals"]), camera_wall: new Set(["n", "streams"]) };') in s
    assert 'const S_LIVE = new Set(["lists", "units"]);' in s
    # every part of the page is either one section's or everyone's: a new part is everyone's until it is named
    owner = dict(re.findall(r'(\w+): "(\w+)"', re.search(r"const S_PART_OF = \{(.*?)\};", s, re.S).group(1)))
    assert set(owner) <= set(after) and owner["audio_retention"] == owner["camera_wall"] == "system" and owner["tones"] == "radios"
    load = fn("sLoad")
    assert "const same = !!S.data && S.sigSec === S.section && sig === S.sig && (live === S.sigLive || !S_LIVE.has(S.section));" in load
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
    assert card.count('act("set", ') == 2 and card.count('act("reset", ') == 2 and card.count('act("off", ') == 2
    test = fn("sPushTest")
    assert "sMsg(e.message)" not in test and test.count("sPushErr(card, k.kind, ") == 3


def test_the_script_redraws_through_skeep_and_updates_the_cards_in_place():
    s = (STATIC / "settings.js").read_text()
    assert "if (fn) sKeep(panel, () => fn(panel), keep && !(fresh && !force));" in fn("sRender")
    assert "sPressing()" in fn("sRender")                                     # never between a button's press and its click
    assert fn("sPost").count("sRender(true, true)") == 2                      # a refusal: what was typed stays
    card = fn("sPushes")
    assert "if (card.dataset.built) { sMorph(card, next); return; }" in card
    assert "card.dataset.built) return;" in card                              # a refresh that failed leaves the card alone
    assert "c.dataset.act !== n.dataset.act" in fn("sMorph") and "c.textContent !== n.textContent" in fn("sMorph")
    system = fn("sSystem")
    for part in ("s-counts", "s-audit", "s-alarms", "s-pushes", "s-intel"):   # the parts that fill themselves keep their node
        assert f'kept("{part}", ' in system, part
    assert 'sCardMount(alarms, Alarm.mountSettings, S.saved === "alarms")' in system
    assert 'sCardMount(intel, IntelSettings.mount, S.saved === "intel")' in system
    refresh = fn("sRefresh")
    for read in ("sCounts();", "sAudit();", "sPushes(card)", "Alarm.mountSettings", "IntelSettings.mount", "sAudioRetention(", "sCameraWall("):
        assert read in refresh, read
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
        pg.set_default_timeout(15000)
        pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())     # nothing off this machine
        if REF:                                                                              # another commit's page
            for name, ctype in (("settings.js", "text/javascript"), ("settings.css", "text/css")):
                body = subprocess.run(["git", "-C", str(ROOT), "show", f"{REF}:sizeup/web/static/{name}"],
                                      capture_output=True, check=True).stdout
                pg.route(f"**/static/{name}*", (lambda body, ctype: lambda route: route.fulfill(body=body, content_type=ctype))(body, ctype))
        pg.errors = []
        pg.posts = []                                                                        # what the page asked to change
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("request", lambda r: pg.posts.append((r.url.rsplit("/", 1)[1], r.post_data_json))
              if r.method == "POST" and "/api/cad/settings/" in r.url else None)
        pg.goto(site.base + "/cad#settings/system")
        if not REF:
            # what moves with the calls read again on every rev, as before the slower clock (sHook S_POLL): these tests
            # are about how a reread lands; test_settings_reload.py is about when one happens
            pg.evaluate("() => { S_POLL.live = 0; S_POLL.other = 0; }")
        pg.fill("#operator", "JD")
        pg.wait_for_selector(FIELD)
        pg.wait_for_function("document.querySelector('#s-audit table') && document.querySelector('#s-counts div')"
                             " && document.querySelector('#s-alarms textarea') && !S.loading")
        yield pg
        assert pg.errors == []
        browser.close()


FIELD = "#s-pushes input[aria-label^='Alerts topic']"
STATUS_FIELD = "#s-pushes input[aria-label^='Status & health topic']"
RING_FIELD = "#s-pushes input[aria-label^='Pre-alert ring topic']"
ROW = "#s-pushes .row:has(input[aria-label^='Alerts topic'])"
RING_ROW = "#s-pushes .row:has(input[aria-label^='Pre-alert ring topic'])"
NODES = """() => { const sys = document.querySelector("#s-system");
  return [sys.firstElementChild, sys.querySelector("#s-pushes"), sys.querySelector("#s-pushes input[aria-label^='Alerts topic']"),
    sys.querySelector("#s-pushes .row"), sys.lastElementChild]; }"""
MARK = f"() => {{ window._nodes = ({NODES})(); }}"                 # remember the nodes on screen: a redraw would replace them
SAME = f"() => ({NODES})().map((n, i) => n === window._nodes[i])"
SCROLL = "() => document.querySelector('#settings .s-content').scrollTop"
# A message that can be read: on screen, and not behind something else.
READABLE = """(text) => { for (const n of document.querySelectorAll('#settings .error, #settings [role=alert]')) {
    if (!n.textContent.includes(text)) continue;
    const r = n.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > innerHeight) continue;
    const hit = document.elementFromPoint(r.left + Math.min(r.width / 2, 200), r.top + r.height / 2);
    if (hit && n.contains(hit)) return true;
  } return false; }"""


def scroll_to_pushes(pg) -> int:
    pg.evaluate("""() => { const sc = document.querySelector('#settings .s-content');
        sc.scrollTop = document.querySelector('#s-pushes').offsetTop - 40; }""")
    top = pg.evaluate(SCROLL)
    assert top > 100
    return top


def caught_up(pg, rev) -> None:
    """The page has read the settings at this rev or a later one (sHook -> sLoad), and anything it started after that
    has landed. Later: a dispatch's voice is made after its answer and moves the rev once more, so waiting for this
    very rev can wait for ever."""
    pg.wait_for_function("(rev) => S.data && S.data.rev >= rev && !S.loading", arg=rev)
    pg.wait_for_timeout(400)


def press(pg, selector, confirm="") -> tuple:
    """Click a button and return what it asked the server to change: (the section, the body). confirm: the dialog's
    button, which must come up; without it none may."""
    n = len(pg.posts)
    pg.click(selector)
    if confirm:
        pg.click(f"[role=alertdialog]:visible button:text-is('{confirm}')")
    for _ in range(100):
        if len(pg.posts) > n:
            break
        pg.wait_for_timeout(50)
    assert len(pg.posts) == n + 1, pg.posts[n:]
    pg.wait_for_timeout(500)                                                  # its answer, and the card catching up
    assert pg.locator("[role=alertdialog]:visible").count() == 0
    return pg.posts[n]


def buttons(pg, row) -> list:
    return [b.strip() for b in pg.locator(f"{row} button").all_inner_texts() if b.strip() != "?"]


def test_calls_running_redraw_nothing_under_a_field_with_the_focus(site, page):
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    page.click(FIELD)
    page.keyboard.type(TYPED)
    page.evaluate("() => document.activeElement.setSelectionRange(3, 7)")
    caught_up(page, site.a_call_runs())
    assert page.evaluate(SAME) == [True] * 5
    assert page.input_value(FIELD) == TYPED
    assert page.evaluate("() => { const a = document.activeElement; return [a.getAttribute('aria-label').slice(0, 12), a.selectionStart, a.selectionEnd]; }") == ["Alerts topic", 3, 7]
    assert page.evaluate(SCROLL) == top
    assert page.evaluate("() => S.deferred") is False                         # nothing is waiting to redraw on blur either


def test_calls_running_redraw_nothing_while_reading_with_text_typed_and_the_focus_elsewhere(site, page):
    """What was reported: in the section, not in a field (nothing held the redraw back), the page jumped."""
    page.fill(FIELD, TYPED)
    page.evaluate("() => document.activeElement.blur()")
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    page.evaluate("""() => { window._tops = []; const sc = document.querySelector('#settings .s-content');
        new MutationObserver(() => window._tops.push([sc.scrollTop, document.querySelector('#s-pushes').childElementCount])).observe(
          document.querySelector('#s-system'), { childList: true, subtree: true }); }""")
    for _ in range(3):
        caught_up(page, site.a_call_runs())
    assert page.input_value(FIELD) == TYPED                                    # the typed text is still there
    seen = page.evaluate("() => window._tops")
    assert all(n > 5 for t, n in seen), seen                                   # the card never went empty
    assert all(t == top for t, n in seen) and page.evaluate(SCROLL) == top     # the page never moved
    assert page.evaluate(SAME) == [True] * 5                                   # nothing was rebuilt


def test_the_audio_kept_moves_with_every_transmission_in_place_and_typed_alarm_words_stay(site, page):
    """On the hub the settings carry the clips and bytes kept per channel, which move with every transmission (and
    their rows change order): the section isn't redrawn for them, the Audio retention card shows them in place, and
    text typed in Emergency alarms and not saved is still there, through that and through a real redraw."""
    total = "() => document.querySelector('#s-audio-retention').innerText.match(/Audio (\\d+) ·/)[1]"
    rows = "() => [...document.querySelectorAll('#s-audio-retention .row .s-eta-l')].map((n) => n.textContent)"
    assert page.evaluate(total) == "2" and page.evaluate(rows) == ["Default", "FG1", "PC repeater", "Format"]
    words = page.input_value("#s-alarms textarea")
    page.fill("#s-alarms textarea", words + "\nexample phrase being typed")
    page.fill("#s-alarms input[aria-label='Fire channels']", "FG1, Example channel")
    page.evaluate("() => document.activeElement.blur()")
    top = scroll_to_pushes(page)
    page.evaluate(MARK)
    for _ in range(3):
        caught_up(page, site.a_call_runs())
    assert page.evaluate(SAME) == [True] * 5 and page.evaluate(SCROLL) == top  # no redraw for it
    assert page.evaluate(total) == "5"                                         # ... and the card kept up, largest first
    assert page.evaluate(rows) == ["Default", "PC repeater", "FG1", "Format"]
    assert page.input_value("#s-alarms textarea").endswith("example phrase being typed")
    # a retention field being typed in is left alone, and catches up once it isn't
    page.click("#s-audio-retention input[aria-label='FG1: audio days kept']")
    page.keyboard.type("9")
    caught_up(page, site.a_call_runs())
    assert page.evaluate(total) == "5" and page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "FG1: audio days kept"
    page.keyboard.press("Backspace")
    page.evaluate("() => document.activeElement.blur()")
    caught_up(page, site.a_call_runs())
    assert page.evaluate(total) == "7"
    # another admin's change to the settings: the section is drawn again, around what is typed in Emergency alarms
    top = page.evaluate(SCROLL)               # (the click on the retention field brought it into view)
    page.evaluate(MARK)
    caught_up(page, site.another_admin_mutes_an_alert())
    page.wait_for_function("() => /Muted until/.test(document.querySelector('#s-pushes').innerText)")
    assert page.evaluate(SAME)[0] is False
    assert page.input_value("#s-alarms textarea").endswith("example phrase being typed")
    assert page.input_value("#s-alarms input[aria-label='Fire channels']") == "FG1, Example channel"
    assert page.evaluate(SCROLL) == top
    # ... and saving them works on what is typed, then starts the card clean
    assert press(page, "#s-alarms button:text-is('Save words')")[1]["value"][-1] == "example phrase being typed"
    page.wait_for_function("() => document.querySelector(\"#s-alarms input[aria-label='Fire channels']\").value !== 'FG1, Example channel'")


def test_another_admins_changes_to_alarms_intel_and_topics_show_with_their_audit_rows(site, page):
    """What is read apart from the settings (Emergency alarms, Intel, the audit log): another admin's change shows,
    the second one to the same key too (the settings only count the keys changed)."""
    audit = "() => [...document.querySelectorAll('#s-audit tbody tr')].map((tr) => tr.children[2].textContent + ' ' + tr.children[3].textContent)"
    words = site.j("GET", "/api/cad/alarms")[1]["settings"]["keywords"]
    page.evaluate(MARK)
    for n, phrase in enumerate(("example phrase one", "example phrase two"), 1):
        code, out = site.j("POST", "/api/cad/settings/alarms", {"action": "set", "key": "keywords", "value": words + [phrase], "operator": "KM"})
        assert code == 200, out
        caught_up(page, site.cad.store.rev)
        page.wait_for_function("(p) => document.querySelector('#s-alarms textarea').value.endsWith(p)", arg=phrase)
        assert sum("keywords" in row for row in page.evaluate(audit)) == n
    intel = site.j("GET", "/api/cad/intel")[1]["settings"]
    for n in (intel["per_hour"] + 3, intel["per_hour"] + 5):
        code, out = site.j("POST", "/api/cad/settings/intel", {"action": "set", "key": "per_hour", "value": n, "operator": "KM"})
        assert code == 200, out
        caught_up(page, site.cad.store.rev)
        page.wait_for_function("(n) => document.querySelector(\"#s-intel input[aria-label='Pushes an hour']\").value === String(n)", arg=n)
    rows = len(page.evaluate(audit))
    assert site.j("POST", "/api/cad/settings/notify", {"action": "set", "key": "status", "topic": "example-other-topic-cdef", "operator": "KM"})[0] == 200
    caught_up(page, site.cad.store.rev)
    page.wait_for_function("() => document.querySelector('#s-pushes').innerText.includes('example-other-topic-cdef')")
    assert len(page.evaluate(audit)) == rows + 1 and "notify/status" in page.evaluate(audit)[0]
    assert page.evaluate(SAME)[1:4] == [True] * 3


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
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label').slice(0, 12)") == "Alerts topic" and page.evaluate(SCROLL) == top
    # out of the field: the section is drawn again (their mute shows), around the card, the typed text and the place
    page.evaluate("() => document.activeElement.blur()")
    page.wait_for_function("() => !S.deferred && /Muted until/.test(document.querySelector('#s-pushes').innerText)")
    page.wait_for_timeout(300)
    same = page.evaluate(SAME)
    assert same[0] is False and same[1:3] == [True, True]                      # redrawn; the card and the field: the same nodes
    assert page.input_value(FIELD) == TYPED and page.evaluate(SCROLL) == top


def test_a_refused_save_says_why_under_the_field_and_keeps_what_was_typed(site, page):
    scroll_to_pushes(page)
    page.evaluate(MARK)
    page.fill(FIELD, READ_RING)                                               # the topic sizeup reads: never published to
    with page.expect_response(lambda r: r.url.endswith("/api/cad/settings/notify")) as got:
        page.click(f"{ROW} button:text-is('Save')")
    assert got.value.status == 400
    page.wait_for_timeout(500)
    where = "() => Math.round(document.querySelector(\"#s-pushes input[aria-label^='Alerts topic']\").getBoundingClientRect().top)"
    assert page.input_value(FIELD) == READ_RING                               # what was typed is still there
    assert page.evaluate(READABLE, "that is a protected topic") is True       # and why it was refused can be read
    # ... under its own row, tied to the field, and nowhere else
    err = page.locator("#s-pushes [data-err=alerts]")
    assert err.inner_text().startswith("Not saved: that is a protected topic")
    assert err.get_attribute("role") == "alert" and page.get_attribute(FIELD, "aria-invalid") == "true"
    assert page.get_attribute(FIELD, "aria-describedby") == err.get_attribute("id") == "s-push-err-alerts"
    assert page.evaluate(SAME) == [True] * 5
    box, row = err.bounding_box(), page.locator(ROW).bounding_box()
    assert 0 <= box["y"] - (row["y"] + row["height"]) < 24 and box["y"] < 800  # right under its row, on screen
    assert page.inner_text("#s-msg") == ""                                    # (not also at the top of the page)
    at = page.evaluate(where)
    caught_up(page, site.a_call_runs())                                       # it stays through the refreshes ...
    assert err.inner_text().startswith("Not saved: that is a protected topic") and page.input_value(FIELD) == READ_RING
    assert page.evaluate(where) == at
    page.click(FIELD)
    page.keyboard.press("End")
    page.keyboard.type("-x")                                                  # ... until the field is edited
    assert err.inner_text() == "" and not err.is_visible() and page.get_attribute(FIELD, "aria-invalid") is None
    assert site.cad.pushes.effective(ALERTS) == {"topic": OUR_ALERTS, "source": "ssm"}


def test_a_save_that_takes_updates_the_card_in_place(site, page):
    top = scroll_to_pushes(page)
    page.fill(STATUS_FIELD, "half-typed-elsewhere")
    page.fill(FIELD, f"  {TYPED} ")
    page.click(f"{ROW} button:text-is('Save')")
    page.wait_for_function("(t) => document.querySelector('#s-pushes').innerText.includes('In effect: ' + t)", arg=TYPED)
    caught_up(page, site.cad.store.rev)
    assert site.cad.pushes.effective(ALERTS) == {"topic": TYPED, "source": "settings"}
    assert page.input_value(FIELD) == TYPED                                    # as saved
    assert page.input_value(STATUS_FIELD) == "half-typed-elsewhere"
    assert buttons(page, ROW) == ["Save", "Use default", "Turn off", "Send test push"]
    assert page.evaluate(SCROLL) == top


def test_each_button_does_what_it_says_after_the_row_changes_under_it(site, page):
    """Set, off, default, off again: the buttons a row has change with its state, and one that takes another's place
    never keeps the other's click (Turn off relabelled "Use default" turned it off again; "Turn off" drawn over a
    Use default reset it with no question). The push rows and the feeds sizeup reads."""
    scroll_to_pushes(page)
    page.fill(FIELD, TYPED)
    assert press(page, f"{ROW} button:text-is('Save')") == ("notify", {"action": "set", "key": "alerts", "topic": TYPED, "operator": "JD"})
    assert buttons(page, ROW) == ["Save", "Use default", "Turn off", "Send test push"]
    assert press(page, f"{ROW} button:text-is('Turn off')", confirm="Turn off")[1] == {"action": "off", "key": "alerts", "operator": "JD"}
    assert buttons(page, ROW) == ["Save", "Use default", "Send test push"]
    assert site.cad.pushes.effective(ALERTS)["source"] == "off"
    assert press(page, f"{ROW} button:text-is('Use default')")[1] == {"action": "reset", "key": "alerts", "operator": "JD"}
    assert site.cad.pushes.effective(ALERTS) == {"topic": OUR_ALERTS, "source": "ssm"}
    assert buttons(page, ROW) == ["Save", "Turn off", "Send test push"]
    assert press(page, f"{ROW} button:text-is('Turn off')", confirm="Turn off")[1] == {"action": "off", "key": "alerts", "operator": "JD"}
    # the ring feed: Turn off (asks nothing), Clear, Turn off
    assert buttons(page, RING_ROW) == ["Save", "Turn off"]
    assert press(page, f"{RING_ROW} button:text-is('Turn off')")[1] == {"action": "off", "key": "prealert_ring", "operator": "JD"}
    assert buttons(page, RING_ROW) == ["Save", "Clear"]
    assert press(page, f"{RING_ROW} button:text-is('Clear')")[1] == {"action": "reset", "key": "prealert_ring", "operator": "JD"}
    assert buttons(page, RING_ROW) == ["Save", "Turn off"]
    assert press(page, f"{RING_ROW} button:text-is('Turn off')")[1] == {"action": "off", "key": "prealert_ring", "operator": "JD"}
    page.fill(RING_FIELD, "example-ring-topic-ef01")
    assert press(page, f"{RING_ROW} button:text-is('Save')")[1] == {"action": "set", "key": "prealert_ring", "topic": "example-ring-topic-ef01", "operator": "JD"}
    assert buttons(page, RING_ROW) == ["Save", "Clear", "Turn off"]


def test_each_button_does_what_it_says_after_another_admin_changes_the_row(site, page):
    """A row drawn while off (Save, Use default), then set back to its default from another console: its "Turn off" is
    a new button that asks and turns it off, not the old Use default under a new label."""
    for key in ("alerts", "prealert_ring"):
        assert site.j("POST", "/api/cad/settings/notify", {"action": "off", "key": key, "operator": "KM"})[0] == 200
    page.reload()
    page.fill("#operator", "JD")
    page.wait_for_selector(FIELD)
    scroll_to_pushes(page)
    assert buttons(page, ROW) == ["Save", "Use default", "Send test push"] and buttons(page, RING_ROW) == ["Save", "Clear"]
    for key in ("alerts", "prealert_ring"):
        assert site.j("POST", "/api/cad/settings/notify", {"action": "reset", "key": key, "operator": "KM"})[0] == 200
    caught_up(page, site.cad.store.rev)
    assert buttons(page, ROW) == ["Save", "Turn off", "Send test push"] and buttons(page, RING_ROW) == ["Save", "Turn off"]
    # "Turn off" where "Use default" / "Clear" was: it asks first, and turns it off
    assert press(page, f"{ROW} button:text-is('Turn off')", confirm="Turn off")[1] == {"action": "off", "key": "alerts", "operator": "JD"}
    assert press(page, f"{RING_ROW} button:text-is('Turn off')")[1] == {"action": "off", "key": "prealert_ring", "operator": "JD"}
    assert site.cad.pushes.effective(ALERTS)["source"] == "off"
    # ... and the other way: off here, set there
    assert site.j("POST", "/api/cad/settings/notify", {"action": "set", "key": "alerts", "topic": TYPED, "operator": "KM"})[0] == 200
    caught_up(page, site.cad.store.rev)
    assert buttons(page, ROW) == ["Save", "Use default", "Turn off", "Send test push"]
    assert press(page, f"{ROW} button:text-is('Use default')")[1] == {"action": "reset", "key": "alerts", "operator": "JD"}
    assert site.cad.pushes.effective(ALERTS) == {"topic": OUR_ALERTS, "source": "ssm"}


def test_a_test_push_that_fails_and_a_server_that_does_not_answer_say_so_under_the_field(site, page):
    scroll_to_pushes(page)
    site.push_error = "ntfy answered 507"
    page.click(f"{ROW} button:text-is('Send test push')")
    err = page.locator("#s-pushes [data-err=alerts]")
    err.wait_for(state="visible")
    assert err.inner_text() == "Test push not sent: ntfy answered 507"
    site.push_error = ""
    page.click(f"{ROW} button:text-is('Send test push')")
    page.wait_for_function("() => document.querySelector('#s-pushes [data-err=alerts]').textContent === ''")
    # the hub gone (a 502 that isn't JSON), then the network
    page.fill(FIELD, TYPED)
    page.route("**/api/cad/settings/notify", lambda route: route.fulfill(status=502, body="Bad Gateway"))
    page.click(f"{ROW} button:text-is('Save')")
    err.wait_for(state="visible")
    assert err.inner_text().startswith("Not saved: 502") and page.input_value(FIELD) == TYPED
    page.unroute("**/api/cad/settings/notify")
    page.route("**/api/cad/settings/notify", lambda route: route.abort())
    page.click(f"{ROW} button:text-is('Save')")
    page.wait_for_function("() => document.querySelector('#s-pushes [data-err=alerts]').textContent === 'Not saved: no answer from the server'")
    assert page.input_value(FIELD) == TYPED
    page.click(f"{ROW} button:text-is('Turn off')")
    page.click("[role=alertdialog]:visible button:text-is('Turn off')")
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
    page.click(f"{ROW} button:text-is('Save')")
    page.wait_for_function("(t) => document.querySelector('#s-pushes').innerText.includes('In effect: ' + t)", arg=TYPED)
    assert site.cad.pushes.effective(ALERTS) == {"topic": TYPED, "source": "settings"}


def test_a_press_that_is_never_released_holds_a_redraw_only_so_long(site, page):
    """The pointer goes down in the section and its release never arrives (it left the window): what waited for the
    release is drawn once the press's time is up."""
    page.evaluate("() => document.querySelector('#s-system').dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))")
    caught_up(page, site.another_admin_mutes_an_alert())
    assert page.evaluate("() => S.pressDeferred") is True
    page.wait_for_function("() => !S.pressDeferred && /Muted until/.test(document.querySelector('#s-pushes').innerText)", timeout=8000)


def test_other_sections_keep_typed_text_and_scroll_through_a_refused_save(site, page):
    """The same rule outside Push notifications (sPost): a refusal puts the switches back, not the text."""
    page.fill(STATUS_FIELD, "half-typed-elsewhere")
    scroll_to_pushes(page)
    where = "() => Math.round(document.querySelector(\"#s-pushes input[aria-label^='Status & health topic']\").getBoundingClientRect().top)"
    top = page.evaluate(where)               # (on screen: the line at the top of the page takes room of its own)
    page.route("**/api/cad/settings/alert_mutes", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": "example refusal"})))
    box = page.locator("#s-pushes label.check input[type=checkbox]").first
    box.click()
    page.wait_for_function("() => document.querySelector('#s-msg').textContent === 'example refusal'")
    page.wait_for_timeout(300)
    assert box.is_checked() is False                                           # the switch is back to what is saved
    assert page.input_value(STATUS_FIELD) == "half-typed-elsewhere"            # what was typed is still there
    assert page.evaluate(READABLE, "example refusal") is True                  # the reason isn't behind the section's head
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


def test_text_typed_in_a_units_row_stays_that_units_when_the_rows_move(site, page):
    """A refused edit leaves its text in the row; another admin moves the unit above it down: the text is still in the
    same unit's row, not in the one that now sits where it was."""
    page.goto(site.base + "/cad#settings/units")
    page.wait_for_selector("#s-units li.unit input.s-title")
    codes = "() => [...document.querySelectorAll('#s-units li.unit')].slice(1, 3).map((li) => li.querySelector('.s-code').textContent)"
    above, mine = page.evaluate(codes)
    title = lambda code: f"#s-units li.unit:has(.s-code:text-is('{code}')) input.s-title"   # noqa: E731
    page.route("**/api/cad/settings/units", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": "example refusal"})))
    was = page.input_value(title(above))
    page.fill(title(mine), "Example title")
    page.evaluate("() => document.activeElement.blur()")
    page.wait_for_function("() => document.querySelector('#s-msg').textContent === 'example refusal'")
    page.wait_for_timeout(200)
    assert page.input_value(title(mine)) == "Example title"
    page.unroute("**/api/cad/settings/units")
    code, out = site.j("POST", "/api/cad/settings/units", {"action": "move", "code": above, "dir": 1, "operator": "KM"})
    assert code == 200, out
    caught_up(page, site.cad.store.rev)
    assert page.evaluate(codes) == [mine, above]                               # the rows moved on the page
    assert page.input_value(title(mine)) == "Example title" and page.input_value(title(above)) == was


def test_every_section_opens_and_sits_through_a_call_without_an_error(site, page):
    for sec in ("lists", "runcards", "units", "dispatch", "users", "radios", "data", "system"):
        page.goto(site.base + f"/cad#settings/{sec}")
        page.wait_for_function("(sec) => S.section === sec && S.data && !S.loading", arg=sec)
        caught_up(page, site.a_call_runs())
        assert page.evaluate(f"() => !document.querySelector('#s-{sec}').hidden"), sec
