"""The console's live player in a browser (sizeup/web/static/live-player.js, cad.html's #lp): one click turns it on and
allows sound, what plays goes on while the dispatcher moves between Overview, Incident, Radio, Search and Settings (the
same clip, never restarted), a Priority transmission interrupts a Normal one (which resumes after) or waits for it, as
chosen in Listening, a person's choices go to the hub (/api/me/prefs/listen) and come back from it, a channel's level is
set from the Radio rail, and M / ] mute and skip.

Headless Chromium against a real WebServer and CAD (skipped where Playwright isn't installed, as on CI). The radio log's
answers (/api/cad/comms) and the clips are this test's own: invented rows and tones, released one by one as if heard.
SIZEUP_SHOTS=<dir> saves screenshots."""
import io
import json
import math
import os
import struct
import time
import wave
from urllib.parse import parse_qs, urlparse

import pytest

from tests.test_reminders_ui import WHO, browser  # noqa: F401  (fixture)
from tests.test_settings_no_redraw import Site

SHOTS = os.environ.get("SIZEUP_SHOTS", "")


def tone(seconds: float, hz: int = 440) -> bytes:
    """A quiet sine tone as a WAV (8 kHz, 16 bit, mono)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * hz * i / 8000)))
                               for i in range(int(8000 * seconds))))
    return buf.getvalue()


class Air:
    """The radio log as this test hears it: rows released by heard(), each with its clip's length."""

    def __init__(self):
        self.rows, self.pending, self.secs, self.cursor, self.puts = [], [], {}, 1, []
        self.prefs = {"signed_in": False, "id": None, "prefs": {}}

    def heard(self, rid, source, service, ours, seconds, channel, talker="", **kw):
        t = {"id": rid, "kind": "radio", "source": source, "service": service, "ours": int(ours), "main": bool(ours),
             "system": source.split(":")[1] if source.startswith("p25:") else "analog", "channel": channel,
             "talker": talker, "start": time.time(), "has_audio": True, "status": "", "text": "", **kw}
        self.secs[rid] = seconds
        self.pending.append(t)
        return t

    def comms(self, route):
        q = parse_qs(urlparse(route.request.url).query)
        if "changed" in q:
            rows, self.pending = self.pending, []
            self.rows += rows
            self.cursor += len(rows)
            body = {"rows": rows, "cursor": self.cursor, "reload": False, "gone": []}
        else:
            body = {"rows": self.rows[-20:], "cursor": self.cursor, "older": False, "next": "", "reload": False, "gone": []}
        route.fulfill(status=200, body=json.dumps(body), content_type="application/json")

    def audio(self, route):
        rid = urlparse(route.request.url).path.rsplit("/", 1)[-1].removesuffix(".wav")
        route.fulfill(status=200, body=tone(self.secs.get(rid, 1.0)), content_type="audio/wav")

    def me(self, route):
        if route.request.method == "PUT":
            ns, body = route.request.url.rsplit("/", 1)[-1], json.loads(route.request.post_data)
            self.puts.append((ns, body))
            self.prefs["prefs"][ns] = body                       # as the hub keeps it
            route.fulfill(status=200, body=json.dumps({"ok": True}), content_type="application/json")
        else:
            route.fulfill(status=200, body=json.dumps(self.prefs), content_type="application/json")


# the department's views as the tree brings them (sizeup/cad/radioviews.py SEED: none set), Our calls renamed
SEED = [{"id": "ours", "name": "PCFD Primary", "mark": "F", "color": "", "filter": {"ours": True}},
        {"id": "police", "name": "Police", "mark": "P", "color": "", "filter": {"services": ["police"]}},
        {"id": "transit", "name": "Transit", "mark": "T", "color": "", "filter": {"services": ["transit"]}},
        {"id": "all", "name": "All", "mark": "∗", "color": "", "filter": {}}]
SOURCES = {"rev": "s1", "now": 0, "live": [], "folders": None, "services": {},
           "views": {"rev": "v1", "custom": True, "views": SEED}, "sources": [
    {"key": "fm:FG1", "system": "analog", "system_label": "Analog", "group": "Analog", "name": "FG1", "service": "fire",
     "tier": "", "ours": True, "mutable": False, "last": 0, "counts": [], "mhz": 154.1},
    {"key": "p25:westchester:1111", "system": "westchester", "system_label": "County", "group": "Fire", "name": "Fire Dispatch",
     "service": "fire", "tier": "text", "ours": True, "mutable": False, "talkgroup": 1111, "last": 0, "counts": []},
    {"key": "p25:westchester:2222", "system": "westchester", "system_label": "County", "group": "Law", "name": "PD Main",
     "service": "police", "tier": "text", "ours": False, "mutable": True, "talkgroup": 2222, "last": 0, "counts": []},
]}


@pytest.fixture
def site(tmp_path, monkeypatch):
    from sizeup.cad.automode import AutoDispatch
    monkeypatch.setattr(AutoDispatch, "hand_dispatch", lambda self: None)
    s = Site(tmp_path, monkeypatch)
    yield s
    s.ws.shutdown()


def open_console(browser, site, air, path="/cad"):  # noqa: F811
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.route(lambda url: not url.startswith(site.base), lambda route: route.abort())
    pg.route(site.base + "/api/cad/whoami", lambda route: route.fulfill(status=200, body=json.dumps(WHO), content_type="application/json"))
    pg.route(lambda url: "/api/cad/comms?" in url, air.comms)
    pg.route(lambda url: "/api/cad/comms/sources" in url, lambda route: route.fulfill(
        status=200, body=json.dumps({**SOURCES, "now": time.time()}), content_type="application/json"))
    pg.route(lambda url: "/api/audio/" in url, air.audio)
    pg.route(lambda url: "/api/me/prefs" in url, air.me)
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.add_init_script("try { localStorage.setItem('sizeup.cad.operator', 'JD'); } catch (e) {}")
    pg.goto(site.base + path)
    pg.wait_for_function("typeof cad !== 'undefined' && cad.state && window.LivePlayer")
    return pg


def push(pg):
    """The state push says the radio log moved (as the hub's comms_rev does when a transmission is logged)."""
    pg.evaluate("() => { cad.state = Object.assign({}, cad.state, { comms_rev: 'r' + Math.random() }); for (const f of cad.hooks) f(); }")


def debug(pg):
    return pg.evaluate("() => LivePlayer.debug")


def wait(pg, js, timeout=10000):
    pg.wait_for_function(js, timeout=timeout)


def listen_on(pg):
    pg.click("#lp-on")
    wait(pg, "() => LivePlayer.armed && LivePlayer.debug.cursor !== null")
    assert pg.is_visible("#lp")


def test_live_audio_follows_the_dispatcher_across_views(browser, site):  # noqa: F811
    air = Air()
    air.heard("old-1", "fm:FG1", "fire", True, 1, "FG1")          # heard before Listen: never played
    pg = open_console(browser, site, air)
    assert not pg.is_visible("#lp")
    listen_on(pg)
    air.heard("tx-1", "fm:FG1", "fire", True, 6, "FG1", talker="E60")
    push(pg)
    wait(pg, "() => LivePlayer.debug.playing === 'tx-1' && LivePlayer.debug.t > 0.3")
    assert "FG1" in pg.inner_text("#lp-now") and "E60" in pg.inner_text("#lp-now")
    last = debug(pg)["t"]
    for go in ("#overview-open", "#dispatch-open", "#radio-open", "#search-open", "#settings-open", "#overview-open"):
        pg.click(go)
        pg.wait_for_timeout(250)
        d = debug(pg)
        assert d["playing"] == "tx-1", go
        assert d["t"] >= last, f"restarted on {go}"       # the same clip, on from where it was
        last = d["t"]
        assert pg.is_visible("#lp"), go
    if SHOTS:
        pg.click("#overview-open")
        pg.screenshot(path=f"{SHOTS}/live-player-overview.png")
    assert pg.evaluate("() => document.querySelectorAll('#lp').length") == 1
    assert not pg.errors, pg.errors


def test_priority_interrupts_a_normal_one_then_it_resumes(browser, site):  # noqa: F811
    air = Air()
    air.prefs = {"signed_in": True, "id": 7, "prefs": {"listen": {"rules": {"ours": "priority", "svc:police": "normal"}}}}
    pg = open_console(browser, site, air)
    listen_on(pg)
    air.heard("pd-1", "p25:westchester:2222", "police", False, 6, "PD Main", talkgroup=2222)
    push(pg)
    wait(pg, "() => LivePlayer.debug.playing === 'pd-1' && LivePlayer.debug.t > 0.5")
    air.heard("fg-1", "fm:FG1", "fire", True, 1, "FG1")
    push(pg)
    wait(pg, "() => LivePlayer.debug.playing === 'fg-1'")
    d = debug(pg)
    assert d["paused"] == "pd-1" and d["under"] == "pd-1"          # paused where it was, on its own element
    assert pg.inner_text("#lp-queue") == "+1"
    assert pg.is_visible("#lp-now .lp-p")                         # the P badge on what plays
    wait(pg, "() => LivePlayer.debug.playing === 'pd-1'")          # FG1 done: the police one resumes ...
    assert debug(pg)["t"] > 0.4                                    # ... from where it stopped
    assert not pg.errors, pg.errors


def test_queue_mode_lets_the_normal_one_finish_and_off_is_never_played(browser, site):  # noqa: F811
    air = Air()
    air.prefs = {"signed_in": True, "id": 7, "prefs": {"listen": {"rules": {"ours": "priority", "svc:police": "normal"},
                                                                  "mode": "queue"}}}
    pg = open_console(browser, site, air)
    listen_on(pg)
    air.heard("pd-1", "p25:westchester:2222", "police", False, 2, "PD Main")
    push(pg)
    wait(pg, "() => LivePlayer.debug.playing === 'pd-1'")
    air.heard("tr-1", "p25:metro:3333", "transit", False, 1, "Bus Ops")     # Off: never queued
    air.heard("fg-1", "fm:FG1", "fire", True, 1, "FG1")
    push(pg)
    wait(pg, "() => LivePlayer.debug.priority === 1")
    assert debug(pg)["playing"] == "pd-1"                          # it waits for the Normal one
    wait(pg, "() => LivePlayer.debug.playing === 'fg-1'")
    wait(pg, "() => !LivePlayer.debug.playing")
    assert debug(pg)["normal"] == 0 and debug(pg)["priority"] == 0
    assert not pg.errors, pg.errors


def test_listening_choices_go_to_the_hub_and_come_back(browser, site):  # noqa: F811
    air = Air()
    air.prefs = {"signed_in": True, "id": 7, "prefs": {"listen": {"rules": {"ours": "priority", "sys:metro": "normal"},
                                                                  "max_age": 90}}}
    pg = open_console(browser, site, air)
    listen_on(pg)
    pg.click("#lp-set")
    wait(pg, "() => document.querySelector('#lp-panel [data-target=\"view:police\"]')")
    assert pg.input_value("#lp-maxage") == "90"                    # the hub's copy, not the defaults
    pg.select_option('#lp-panel [data-target="view:police"]', "normal")
    pg.click('#lp-panel [data-mode="duck"]')
    pg.click("#lp-ours")                                           # a quick toggle
    deadline = time.time() + 6
    while time.time() < deadline and not (air.puts and air.puts[-1][1].get("ours_only")):
        pg.wait_for_timeout(200)
    ns, body = air.puts[-1]
    assert ns == "listen"
    # (the rule from before the views, "ours", moved to the Our calls view's)
    assert body == {"rules": {"view:ours": "priority", "sys:metro": "normal", "view:police": "normal"}, "mode": "duck",
                    "max_age": 90, "ours_only": True}
    assert len(air.puts) <= 2                                      # a burst of clicks: one write (two at most)
    if SHOTS:
        pg.screenshot(path=f"{SHOTS}/live-player-listening.png")
    pg.keyboard.press("Escape")
    assert pg.is_hidden("#lp-panel")
    # another browser (its own storage): the hub's copy
    pg2 = open_console(browser, site, air)
    pg2.wait_for_function("() => LivePlayer.prefs.mode === 'duck' && LivePlayer.prefs.ours_only === true")
    assert not pg.errors and not pg2.errors


def test_a_channels_level_from_the_radio_rail(browser, site):  # noqa: F811
    air = Air()
    air.prefs = {"signed_in": True, "id": 7, "prefs": {}}
    pg = open_console(browser, site, air, "/cad#radio?ui=2")
    wait(pg, "() => document.querySelector('#rn-tree [data-listen=\"p25:westchester:2222\"]')")
    b = '#rn-tree [data-listen="p25:westchester:2222"]'
    assert pg.inner_text(b).strip() == "–"                         # police: Off for a new person
    assert pg.inner_text('#rn-tree [data-listen="fm:FG1"]').strip() == "P"
    pg.click(b)
    pg.click('#ui-menu .menu-item:has-text("Priority")')
    wait(pg, f"() => document.querySelector('{b}').textContent.trim() === 'P'")
    assert pg.evaluate("() => LivePlayer.prefs.rules['src:p25:westchester:2222']") == "priority"
    if SHOTS:
        pg.screenshot(path=f"{SHOTS}/live-player-rail.png")
    deadline = time.time() + 6
    while time.time() < deadline and not air.puts:
        pg.wait_for_timeout(200)
    assert air.puts and air.puts[-1][1]["rules"]["src:p25:westchester:2222"] == "priority"
    assert not pg.errors, pg.errors


def test_keys_mute_and_skip(browser, site):  # noqa: F811
    air = Air()
    pg = open_console(browser, site, air)
    listen_on(pg)
    air.heard("fg-1", "fm:FG1", "fire", True, 5, "FG1")
    air.heard("fg-2", "fm:FG1", "fire", True, 5, "FG1")
    push(pg)
    wait(pg, "() => LivePlayer.debug.playing === 'fg-1'")
    pg.keyboard.press("m")
    assert pg.get_attribute("#lp-mute", "aria-pressed") == "true"
    pg.keyboard.press("]")
    wait(pg, "() => LivePlayer.debug.playing === 'fg-2'")
    pg.keyboard.press("m")
    assert pg.get_attribute("#lp-mute", "aria-pressed") == "false"
    pg.click("#lp-off")
    assert not pg.is_visible("#lp") and pg.get_attribute("#lp-on", "aria-pressed") == "false"
    assert not pg.errors, pg.errors


def test_listening_is_the_rails_views_and_a_rename_follows_at_once(browser, site):  # noqa: F811
    """Listening's groups are the person's Radio views (names, badges, order); the quick toggle is the Our calls view's
    (by its id, named as it is now); its hover lists the Main channels with where to change them. No Mute police, and
    no Listen on the Radio view (the header's is the one)."""
    air = Air()
    air.prefs = {"signed_in": True, "id": 7, "prefs": {}}
    pg = open_console(browser, site, air)
    listen_on(pg)
    wait(pg, "() => document.querySelector('#lp-ours').textContent === 'PCFD Primary only'")
    assert not pg.query_selector("#lp-police") and not pg.query_selector("#rv-live")
    tip = pg.get_attribute("#lp-ours", "title")
    assert "Main: FG1, Fire Dispatch" in tip and "Settings › Radios › Views" in tip and "Settings › Radios › Channels" in tip
    pg.click("#lp-set")
    wait(pg, "() => document.querySelector('#lp-panel [data-target=\"view:ours\"]')")
    names = pg.eval_on_selector_all("#lp-panel .lp-rule .trunc", "ns => ns.map((n) => n.textContent)")
    # the rail's views: Transit has no channel in this tree, so neither the rail nor Listening shows it
    assert names[:3] == ["PCFD Primary", "Police", "All"] and "Transit" not in names
    assert not pg.query_selector('#lp-panel [data-target^="svc:"]')                # no hard-coded service groups
    assert pg.get_attribute('#lp-panel a[href="#settings/radios/channels"]', "title").startswith("Which channels are Main")
    if SHOTS:
        pg.screenshot(path=f"{SHOTS}/our-calls-listening.png")
    # renamed in the rail's editor (or Settings > Radios > Views): Listening and the toggle follow, its level kept
    pg.evaluate("""() => RadioViews.setShared({ rev: "v2", custom: true, views: RadioViews.every().map((v) =>
                     v.id === "ours" ? { ...v, name: "Engine Co" } : v) })""")
    wait(pg, "() => document.querySelector('#lp-ours').textContent === 'Engine Co only'")
    wait(pg, "() => [...document.querySelectorAll('#lp-panel .lp-rule .trunc')].some((n) => n.textContent === 'Engine Co')")
    assert pg.input_value('#lp-panel [data-target="view:ours"]') == "priority"
    # a view hidden by this person: off the list; one with a level still decides, so it stays, dimmed, to change
    pg.evaluate("() => RadioViews.setMine({ ...RadioViews.mine(), hidden: ['all'] })")
    wait(pg, "() => !document.querySelector('#lp-panel [data-target=\"view:all\"]')")
    pg.select_option('#lp-panel [data-target="view:police"]', "normal")
    pg.evaluate("() => RadioViews.setMine({ ...RadioViews.mine(), hidden: ['all', 'police'] })")
    wait(pg, "() => [...document.querySelectorAll('#lp-panel .lp-hid .trunc')].some((n) => n.textContent === 'Police (hidden)')")
    assert not pg.errors, pg.errors
