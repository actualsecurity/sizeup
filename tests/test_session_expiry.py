"""The console's sign-in ending (#818). Admin, 2026-11: "can we raise a 'reauth' button or warning when we are coming up
on the end of an auth session?"

    the hub     GET /api/session {expires_at, now}: the verified Access token's exp, nothing else from it (null without
                Access); GET /reauth: a page that tells the console and closes, no parameters, no redirect. The app's
                /api/v1 and the station's /api/station are untouched.
    the page    static/session.js: the warning WARN_S before exp; a failed same-origin fetch, an audio error or a dropped
                stream checks /api/session with redirect: "manual" (an opaque redirect = the edge sending it to the
                Access login = signed out); signed out, no requests go out and live.js holds its stream; "Re-sign in"
                opens /reauth in a pop-up whose message (this origin only) makes the page check again, then the
                stream resumes and the failed audio plays again. profile.js draws it in the header: the avatar's ring,
                the chip beside it, its menu (in a browser below).

The page's half runs session.js / live.js / reauth.js under node with a small fake DOM: the shapes are the browser's
(the HAR of a real expiry: every request a 302 at Cloudflare's edge to the Access login, fetches a TypeError,
/api/audio an ORB-blocked media error). The values (hosts, clips, emails) are made up.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sizeup.cloud.access import Identity
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req, verifier  # noqa: F401 - fixtures
from tests.test_cloud import TOKEN, HubRun, hub_cfg

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")


# -- the hub ----------------------------------------------------------------------------------------------------------

def test_the_identity_carries_exp_but_is_still_who_it_is(keys, jwks):
    v = verifier(jwks)
    exp = int(time.time()) + 1234
    who = v.identity(jwt(keys[0], exp=exp))
    assert who.email == "casey@example.org" and who.exp == exp
    assert who == Identity(email="casey@example.org", sub="u-1", session=who.session)      # exp is not part of who
    assert v.identity(jwt(keys[0], exp=exp)).exp == exp                                     # the kept token's too


@pytest.fixture
def hub(tmp_path, jwks):
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    h.server.access.certs_url = jwks.url
    h.server.access.log = lambda m: None
    yield h
    h.close()


def test_session_says_when_the_sign_in_ends_and_nothing_else(keys, hub):
    exp = int(time.time()) + 900
    code, body = req(hub.url + "/api/session", headers={"Cf-Access-Jwt-Assertion": jwt(keys[0], exp=exp)})
    assert code == 200 and set(body) == {"expires_at", "now"}               # no email, sub, jti, aud ...
    assert body["expires_at"] == exp and abs(body["now"] - time.time()) < 5
    # the browser's cookie (CF_Authorization) the same
    code, body = req(hub.url + "/api/session", headers={"Cookie": f"CF_Authorization={jwt(keys[0], exp=exp + 60)}"})
    assert code == 200 and body["expires_at"] == exp + 60
    # never cached, never in the access log's routine lines
    with __import__("urllib.request").request.urlopen(__import__("urllib.request").request.Request(
            hub.url + "/api/session", headers={"Cf-Access-Jwt-Assertion": jwt(keys[0])})) as r:
        assert r.headers["Cache-Control"] == "no-store"


def test_session_and_reauth_need_a_signed_in_person(keys, hub):
    for path in ("/api/session", "/reauth"):
        assert req(hub.url + path)[0] == 401
        assert req(hub.url + path, headers={"Cf-Access-Jwt-Assertion": jwt(keys[0], email=None, common_name="x.access")})[0] == 401
        assert req(hub.url + path, headers={"Cf-Access-Jwt-Assertion": jwt(keys[0], exp=int(time.time()) - 600)})[0] == 401
    # the station's endpoints: its bearer token, as before
    assert req(hub.url + "/api/station/jobs?wait=0", headers={"Authorization": f"Bearer {TOKEN}"})[0] == 200


def test_reauth_is_a_page_that_never_redirects(keys, hub):
    h = {"Cf-Access-Jwt-Assertion": jwt(keys[0])}
    for q in ("", "?next=https://elsewhere.example/", "?redirect_url=//elsewhere.example", "?to=javascript:alert(1)"):
        code, page = req(hub.url + "/reauth" + q, headers=h)
        assert code == 200 and isinstance(page, bytes), q
        assert b"elsewhere" not in page and b"javascript:" not in page
        assert b'<script src="/static/reauth.js?v=' in page                 # versioned, as every page's scripts
    js = (STATIC / "reauth.js").read_text()
    assert "location.search" not in js and "location.hash" not in js and "location.href =" not in js
    assert "postMessage(MSG, location.origin)" in js                         # to this origin only, never "*"
    assert '"*"' not in js


def test_reauth_start_drops_the_cookie_and_goes_to_reauth_only(keys, hub):
    """Before the end the cookie is still good: the edge would serve /reauth without a new sign-in (the token's exp is
    fixed). /reauth/start drops it, so /reauth goes through the Access login."""
    import urllib.request

    class NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = urllib.request.build_opener(NoFollow)

    def start(q="", **h):
        r = urllib.request.Request(hub.url + "/reauth/start" + q, headers={"Cf-Access-Jwt-Assertion": jwt(keys[0]), **h})
        try:
            resp = opener.open(r, timeout=10)
        except urllib.error.HTTPError as e:
            resp = e
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers
    import urllib.error
    for q in ("", "?next=https://elsewhere.example/", "?redirect_url=//elsewhere.example"):
        code, h = start(q, **{"Sec-Fetch-Site": "same-origin"})
        assert code == 302 and h["Location"] == "/reauth", q                     # always there: no open redirect
        assert h["Set-Cookie"].startswith("CF_Authorization=; Path=/; Max-Age=0;") and "elsewhere" not in str(h)
        assert h["Cache-Control"] == "no-store"
    code, h = start(**{"Sec-Fetch-Site": "cross-site"})                       # another site sending someone here
    assert code == 302 and h["Location"] == "/reauth" and h["Set-Cookie"] is None
    assert req(hub.url + "/reauth/start")[0] == 401                             # behind Access like the rest


def test_the_hub_sends_no_opener_policy_that_would_cut_the_popup_off(keys, hub):
    """#826: the console page and /reauth* come with no Cross-Origin-Opener-Policy (the pop-up's way back doesn't need
    the opener anyway: BroadcastChannel, storage)."""
    import urllib.request
    h = {"Cf-Access-Jwt-Assertion": jwt(keys[0])}
    for path in ("/cad", "/reauth"):
        with urllib.request.urlopen(urllib.request.Request(hub.url + path, headers=h)) as r:
            assert r.headers.get("Cross-Origin-Opener-Policy") is None, path


def test_without_access_there_is_no_end(tmp_path):
    h = HubRun(hub_cfg(tmp_path))                                           # a hub not set up with Access (local dev)
    try:
        assert h.server.access is None
        code, body = req(h.url + "/api/session")
        assert code == 200 and body["expires_at"] is None
        assert req(h.url + "/reauth")[0] == 200
    finally:
        h.close()


def test_the_console_loads_session_js_first_and_the_station_page_not_at_all():
    cad = (STATIC / "cad.html").read_text()
    block = cad[cad.index("<!-- bundle js cad -->"):]
    assert block.split("\n")[1] == '<script src="/static/session.js"></script>'    # before anything asks the hub
    assert "session.js" not in (STATIC / "station.html").read_text()            # the station: no Access


# -- the page (node) --------------------------------------------------------------------------------------------------

HARNESS = r"""
"use strict";
const fs = require("fs"), vm = require("vm");
const [SESSION, LIVE, REAUTH] = process.argv.slice(2).map((f) => fs.readFileSync(f, "utf8"));
const ORIGIN = "https://hub.example.test";
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };

function makeEnv() {
  const env = { clock: 1_763_000_000_000, timers: [], calls: [], respond: null, opened: [], openReturns: {}, es: [],
                events: [], liveUps: 0 };
  class FakeDate extends Date {
    constructor(...a) { super(...(a.length ? a : [env.clock])); }
    static now() { return env.clock; }
  }
  const timer = (fn, ms, every) => { const t = { fn, at: env.clock + ms, every: every ? ms : 0, on: true }; env.timers.push(t); return t; };
  env.advance = async (ms) => {
    const end = env.clock + ms;
    for (;;) {
      const due = env.timers.filter((t) => t.on && t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      env.clock = Math.max(env.clock, due.at);
      if (due.every) due.at += due.every; else due.on = false;
      due.fn();
      await flush();
    }
    env.clock = end;
    await flush();
  };
  let body;
  class El {
    constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {};
                       this.className = ""; this.textContent = ""; this.title = ""; this.l = new EventTarget(); }
    get classList() {
      const self = this, set = () => new Set(self.className.split(" ").filter(Boolean));
      return { toggle(c, on) { const s = set(); if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c); self.className = [...s].join(" "); },
               contains(c) { return set().has(c); } };
    }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    append(...xs) { for (const x of xs) { if (typeof x === "object") { if (x.parent) x.remove(); x.parent = this; } this.children.push(x); } }
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; } }
    get isConnected() { let n = this; while (n.parent) n = n.parent; return n === body; }
    addEventListener(t, f) { this.l.addEventListener(t, f); }
    click() { const e = new Event("click", { cancelable: true }); this.l.dispatchEvent(e); return e.defaultPrevented; }
    text() { return this.children.length ? this.children.map((c) => typeof c === "string" ? c : c.text()).join("") : this.textContent; }
  }
  body = new El("body");
  const docT = new EventTarget();
  const document = { body, visibilityState: "visible", createElement: (t) => new El(t),
                     addEventListener: (t, f, c) => docT.addEventListener(t, f, c), dispatchEvent: (e) => docT.dispatchEvent(e) };
  class Media extends EventTarget {
    constructor() { super(); this.src = ""; this.currentSrc = ""; this.loads = 0; this.plays = 0; }
    load() { this.loads++; }
    play() { this.plays++; return Promise.resolve(); }
  }
  const channels = env.channels = [];
  class BC {
    constructor(name) { this.name = name; channels.push(this); }
    postMessage(data) { for (const c of channels) if (c !== this && c.name === this.name && c.onmessage) c.onmessage({ data }); }
    close() {}
  }
  class ES { constructor(url) { this.url = url; this.readyState = 0; this.closed = false; env.es.push(this); }
             close() { this.readyState = 2; this.closed = true; } }
  class CE extends Event { constructor(t, o = {}) { super(t, o); this.detail = o.detail; } }
  const winT = new EventTarget();
  const ctx = vm.createContext({
    console, URL, Event, EventTarget, CustomEvent: CE, TypeError, setImmediate, document, BroadcastChannel: BC,
    HTMLMediaElement: Media, EventSource: ES, __Date: FakeDate,
    location: { href: ORIGIN + "/cad", origin: ORIGIN },
    setInterval: (f, ms) => timer(f, ms, true), setTimeout: (f, ms) => timer(f, ms || 0, false),
    clearInterval: (t) => { if (t) t.on = false; }, clearTimeout: (t) => { if (t && typeof t === "object") t.on = false; },
    addEventListener: (t, f) => winT.addEventListener(t, f),
    dispatchEvent: (e) => { env.events.push(e.type); return winT.dispatchEvent(e); },
    open: (...a) => { env.opened.push(a); return env.openReturns; },
    fetch: (input, init) => { env.calls.push({ url: String(input), init: init || {} }); return env.respond(String(input), init || {}); },
  });
  vm.runInContext("Date = __Date; var window = this;", ctx);
  env.ctx = ctx;
  env.body = body;
  env.Media = Media;
  env.run = (src) => vm.runInContext(src, ctx);
  env.message = (origin, data) => { const e = new Event("message"); e.origin = origin; e.data = data; winT.dispatchEvent(e); };
  env.sessionCalls = () => env.calls.filter((c) => c.url.startsWith("/api/session"));
  // Re-sign in clicked (the header's link, profile.js): prevented = the pop-up took it over
  env.click = () => { const e = { prevented: false, preventDefault() { this.prevented = true; } };
                      const opened = env.ctx.SessionWatch.reauth(e); return { opened, prevented: e.prevented }; };
  env.changes = () => env.events.filter((t) => t === "sizeup:session").length;
  return env;
}
const res = (status, data, type = "basic") => Promise.resolve({ status, ok: status >= 200 && status < 300, type,
                                                                 json: async () => data });
const failed = () => Promise.reject(new TypeError("Failed to fetch"));
// the hub: signed in until exp (unix s), on the server's clock skew s behind this machine's
const signedIn = (env, exp, skew = 0) => (url) => url.startsWith("/api/session") ? res(200, { expires_at: exp, now: env.clock / 1000 - skew }) : res(200, {});
// the Access edge once the sign-in ended: a 302 to the login, so manual = opaqueredirect, else a CORS TypeError
const edge = () => (url, init) => init.redirect === "manual" ? res(0, null, "opaqueredirect") : failed();

(async () => {
  const out = {};

  // 1. the warning: 10 min before exp, the minutes as they go; a skewed clock doesn't matter
  for (const skew of [0, 3600]) {
    const env = makeEnv();
    const exp = env.clock / 1000 - skew + 25 * 60;
    env.respond = signedIn(env, exp, skew);
    env.run(SESSION);
    await flush();
    const o = out[`warn_${skew}`] = { at0: env.ctx.SessionWatch.label(), state0: env.ctx.SessionWatch.state() };
    await env.advance(14 * 60 * 1000);
    o.at14 = env.ctx.SessionWatch.label();
    await env.advance(2 * 60 * 1000);
    o.at16 = env.ctx.SessionWatch.label();
    o.state16 = env.ctx.SessionWatch.state();
    o.tip16 = env.ctx.SessionWatch.tip();
    o.href16 = env.ctx.SessionWatch.href();
    o.changes16 = env.changes();
    o.renew = env.click();
    o.renewOpened = env.opened.slice();
    await env.advance(60 * 1000);
    o.state17 = env.ctx.SessionWatch.state();
    o.changes17 = env.changes();
    await env.advance(8 * 60 * 1000 + 30 * 1000);
    o.at24_5 = env.ctx.SessionWatch.label();
    o.polls = env.sessionCalls().length;
    // exp passes: the edge redirects; the page finds out without anything else failing
    env.respond = edge();
    await env.advance(60 * 1000);
    o.after = env.ctx.SessionWatch.label();
    o.afterState = env.ctx.SessionWatch.state();
    o.afterHref = env.ctx.SessionWatch.href();
    o.check = env.sessionCalls().at(-1).init;
    o.checkUrl = env.sessionCalls().at(-1).url;
  }

  // 2. already ended: a redirected fetch (a TypeError) -> one check -> signed out; then nothing goes out
  {
    const env = makeEnv();
    env.ctx.LiveConn = { up: () => env.liveUps++ };
    env.respond = signedIn(env, env.clock / 1000 + 3 * 3600);
    env.run(SESSION);
    await flush();
    const o = out.fetch = { label0: env.ctx.SessionWatch.label() };
    env.respond = edge();
    o.first = await env.run('fetch("/api/cad/comms?rev=7")').then(() => "ok", (e) => e.name);
    await flush();
    o.label = env.ctx.SessionWatch.label();
    o.out = env.ctx.SessionWatch.out;
    o.checks = env.sessionCalls().length;
    o.liveUps = env.liveUps;
    o.events = env.events.slice();
    const before = env.calls.length;
    o.next = await env.run('fetch("/api/cad/state?since=abc")').then(() => "ok", (e) => `${e.name}: ${e.message}`);
    o.request = await env.run('fetch(new URL("/api/cad/whoami", location.href))').then(() => "ok", (e) => e.name);
    o.wentOut = env.calls.length - before;
    // others' hosts and the app's own API are not the console's sign-in
    env.respond = () => res(200, {});
    o.tiles = await env.run('fetch("https://tiles.example.test/1/2/3.pbf")').then(() => "ok", (e) => e.name);
    o.v1 = await env.run('fetch("/api/v1/me")').then(() => "ok", (e) => e.name);
    o.wentOut2 = env.calls.length - before;
    // more failures meanwhile: no more checks than the minute's
    for (let i = 0; i < 5; i++) await env.run('fetch("/api/cad/state")').catch(() => {});
    o.checks2 = env.sessionCalls().length;
  }

  // 3. a 401 from the hub itself (the origin reached without a token) checks too; a failure soon after doesn't again
  {
    const env = makeEnv();
    env.respond = signedIn(env, env.clock / 1000 + 3 * 3600);
    env.run(SESSION);
    await flush();
    const n0 = env.sessionCalls().length;
    await env.advance(30 * 1000);
    env.respond = (url) => url.startsWith("/api/session") ? res(200, { expires_at: env.clock / 1000 + 3 * 3600, now: env.clock / 1000 }) : res(401, {});
    await env.run('fetch("/api/cad/state")');
    await flush();
    const n1 = env.sessionCalls().length;
    await env.run('fetch("/api/cad/state")');
    await flush();
    out.unauthorized = { first: n1 - n0, again: env.sessionCalls().length - n1, out: env.ctx.SessionWatch.out };
    // a role refusal (403) is not a sign-in matter
    await env.advance(30 * 1000);
    env.respond = (url) => url.startsWith("/api/session") ? res(200, { expires_at: null, now: 0 }) : res(403, {});
    const n2 = env.sessionCalls().length;
    await env.run('fetch("/api/cad/settings/users", { method: "POST" })');
    await flush();
    out.unauthorized.forbidden = env.sessionCalls().length - n2;
    // the hub itself says 401 to /api/session (the edge let it through: a hub without the team's keys yet): not out
    await env.advance(30 * 1000);
    env.respond = () => res(401, { error: "sign in through Cloudflare Access" });
    out.unauthorized.hub401 = await env.ctx.SessionWatch.check();
    out.unauthorized.hub401out = env.ctx.SessionWatch.out;
  }

  // 4. an audio error (ORB-blocked): signed out; the pop-up; messages; signed in again: the audio plays again
  {
    const env = makeEnv();
    env.respond = signedIn(env, env.clock / 1000 + 3 * 3600);
    env.run(SESSION);
    await flush();
    const o = out.audio = {};
    const m = new env.Media();                 // a player with its own retry (cad.js's)
    m.retries = 0;
    m.sessionRetry = () => { m.retries++; };
    m.src = m.currentSrc = ORIGIN + "/api/audio/tx-0001.wav";
    await env.ctx.HTMLMediaElement.prototype.play.call(m);
    const other = new env.Media();             // the same, but something else plays there since: not again
    other.retries = 0;
    other.sessionRetry = () => { other.retries++; };
    other.src = ORIGIN + "/api/audio/tx-0002.wav";
    await env.ctx.HTMLMediaElement.prototype.play.call(other);
    const plain = new env.Media();             // no retry of its own (an alert sound): never played late
    plain.src = plain.currentSrc = ORIGIN + "/api/audio/tx-0004.wav";
    await env.ctx.HTMLMediaElement.prototype.play.call(plain);
    env.respond = edge();
    m.dispatchEvent(new Event("error"));
    other.dispatchEvent(new Event("error"));
    plain.dispatchEvent(new Event("error"));
    await flush();
    other.src = ORIGIN + "/api/audio/tx-0003.wav";
    o.label = env.ctx.SessionWatch.label();
    o.checks = env.sessionCalls().length;
    let resumed = false;
    env.ctx.SessionWatch.whenIn().then(() => { resumed = true; });
    // Re-sign in: a pop-up (the link's own new tab not followed)
    env.openReturns = { closed: false };
    o.click = env.click();
    o.opened = env.opened.slice();
    // a pop-up blocker: the link opens it in a new tab
    env.openReturns = null;
    o.blocked = env.click();
    // the pop-up's message: only this origin and only its shape
    env.respond = signedIn(env, env.clock / 1000 + 24 * 3600);
    const c0 = env.sessionCalls().length;
    env.message("https://elsewhere.example", { type: "sizeup:signed-in" });
    env.message(ORIGIN, { type: "something-else" });
    env.message(ORIGIN, "sizeup:signed-in");
    await flush();
    o.ignored = env.sessionCalls().length - c0;
    o.stillOut = env.ctx.SessionWatch.out;
    env.message(ORIGIN, { type: "sizeup:signed-in" });
    await flush();
    o.checked = env.sessionCalls().length - c0;
    o.out = env.ctx.SessionWatch.out;
    o.after = env.ctx.SessionWatch.label();
    o.level = env.ctx.SessionWatch.state().level;
    o.resumed = resumed;
    o.events = env.events.slice();
    o.m = { retries: m.retries, loads: m.loads, plays: m.plays };
    o.other = { retries: other.retries, loads: other.loads, plays: other.plays };
    o.plain = { loads: plain.loads, plays: plain.plays };
  }

  // 4b. the pop-up's message while a check sent before it is still on its way: checked again after it
  {
    const env = makeEnv();
    env.respond = edge();
    env.run(SESSION);
    await flush();
    let release;
    env.respond = () => new Promise((ok) => { release = () => ok({ status: 0, ok: false, type: "opaqueredirect", json: async () => null }); });
    await env.advance(61 * 1000);              // the minute's check while signed out: on its way
    env.respond = signedIn(env, env.clock / 1000 + 24 * 3600);
    env.message(ORIGIN, { type: "sizeup:signed-in" });
    release();                                 // the old one answers "out"
    await flush();
    out.stale = { out: env.ctx.SessionWatch.out, label: env.ctx.SessionWatch.label() };
  }

  // 4c. the pop-up: closed by this page on the signal (its own close() may be refused); the storage event signals too;
  //     a pop-up stuck on its blank first page: sent its address once more, then closed, and the next one is a tab
  {
    const env = makeEnv();
    env.respond = signedIn(env, env.clock / 1000 + 300);
    env.run(SESSION);
    await flush();
    const o = out.popup = {};
    const pop = (href) => ({ closed: false, closes: 0, replaced: [], close() { this.closed = true; this.closes++; },
                             location: { href, replace(u) { pop.last.replaced.push(u); } } });
    // a pop-up that went on to the Access login (cross-origin: its location can't be read)
    const away = { closed: false, closes: 0, close() { this.closed = true; this.closes++; },
                   get location() { throw new Error("SecurityError"); } };
    env.openReturns = away;
    o.opened = env.click().opened;
    await env.advance(15000);                       // the watch: it moved on, so it is left alone
    o.awayLeft = { closed: away.closed };
    env.respond = signedIn(env, env.clock / 1000 + 24 * 3600);
    // another page's localStorage write (reauth.js): the storage event
    const se = new Event("storage"); se.key = "sizeup:signed-in"; se.newValue = "1";
    env.ctx.dispatchEvent(se);
    await flush();
    o.afterStorage = { closed: away.closed, closes: away.closes, level: env.ctx.SessionWatch.state().level };
    const other = new Event("storage"); other.key = "something"; other.newValue = "1";
    const n0 = env.sessionCalls().length;
    env.ctx.dispatchEvent(other);
    await flush();
    o.otherKey = env.sessionCalls().length - n0;
    // a pop-up that never leaves about:blank
    env.respond = signedIn(env, env.clock / 1000 + 300);
    await env.ctx.SessionWatch.check();
    const stuck = pop("about:blank");
    pop.last = stuck;
    env.openReturns = stuck;
    env.opened.length = 0;
    o.stuckOpened = env.click().opened;
    await env.advance(5000);
    o.replaced = stuck.replaced.slice();
    await env.advance(7000);
    o.stuckClosed = stuck.closed;
    env.opened.length = 0;
    o.next = env.click();
    o.nextOpened = env.opened.length;
  }

  // 5. a clip that is just missing (signed in): one check, nothing else
  {
    const env = makeEnv();
    env.respond = signedIn(env, env.clock / 1000 + 3 * 3600);
    env.run(SESSION);
    await flush();
    const m = new env.Media();
    m.src = m.currentSrc = ORIGIN + "/api/audio/gone.wav";
    await env.ctx.HTMLMediaElement.prototype.play.call(m);
    m.dispatchEvent(new Event("error"));
    await flush();
    out.missing = { out: env.ctx.SessionWatch.out, label: env.ctx.SessionWatch.label(), failed: env.ctx.SessionWatch._st.failed.size,
                    checks: env.sessionCalls().length };
  }

  // 6. /reauth (another window): tells the opener (this origin only) and every tab (BroadcastChannel), closes
  {
    const env = makeEnv();
    env.respond = edge();
    env.run(SESSION);
    await flush();
    const o = out.reauth = { outBefore: env.ctx.SessionWatch.out };
    const pop = makeEnv();
    pop.channels.push(...env.channels);        // one browser: the same channels
    env.channels.push(...pop.channels.filter((c) => !env.channels.includes(c)));
    const posted = [];
    let closed = 0;
    pop.ctx.opener = { closed: false, postMessage: (d, origin) => posted.push([d, origin]) };
    pop.ctx.close = () => { closed++; };
    env.respond = signedIn(env, env.clock / 1000 + 24 * 3600);
    pop.run(REAUTH);
    await flush();
    await pop.advance(1000);
    o.posted = posted;
    o.closed = closed;
    o.outAfter = env.ctx.SessionWatch.out;
    o.label = env.ctx.SessionWatch.label();
    // opened as a plain tab (no opener): the channel alone
    const pop2 = makeEnv();
    pop2.ctx.opener = null;
    pop2.ctx.close = () => {};
    pop2.run(REAUTH);
    o.noOpener = "ok";
  }

  // 7. live.js: signed out, the stream is closed and nothing polls; signed in again, a poll and the stream again
  {
    const env = makeEnv();
    env.respond = signedIn(env, env.clock / 1000 + 3 * 3600);
    env.run(SESSION);
    env.run(LIVE);
    await flush();
    const states = [];
    env.ctx.__on = (s) => states.push(s);
    env.run('liveState("/api/cad/stream", "/api/cad/state", __on)');
    await flush();
    await env.advance(4000);
    const o = out.live = { streams: env.es.length };
    const es0 = env.es[0];
    // the sign-in ends: the stream errors (the browser gave up on it: a redirect to another origin)
    env.respond = edge();
    es0.readyState = 2;
    es0.onerror();
    await flush();
    o.out = env.ctx.SessionWatch.out;
    o.closed = es0.closed;
    const n0 = env.calls.filter((c) => c.url.startsWith("/api/cad/state")).length;
    await env.advance(60 * 1000);
    o.polls = env.calls.filter((c) => c.url.startsWith("/api/cad/state")).length - n0;
    o.streamsWhileOut = env.es.length;
    o.pill = env.body.children.filter((c) => typeof c === "object").map((c) => c.className);
    o.level = env.ctx.SessionWatch.state().level;
    // signed in again (another tab's /reauth)
    env.respond = (url) => url.startsWith("/api/session") ? res(200, { expires_at: env.clock / 1000 + 3 * 3600, now: env.clock / 1000 })
                                                         : res(200, { time: 1 });
    env.message(ORIGIN, { type: "sizeup:signed-in" });
    await flush();
    o.pollsAfter = env.calls.filter((c) => c.url.startsWith("/api/cad/state")).length - n0;
    o.streamsAfter = env.es.length;
    o.outAfter = env.ctx.SessionWatch.out;
  }

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    h = tmp_path_factory.mktemp("session") / "harness.js"
    h.write_text(HARNESS)
    r = subprocess.run([NODE, str(h), str(STATIC / "session.js"), str(STATIC / "live.js"), str(STATIC / "reauth.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("skew", [0, 3600])
def test_the_warning_shows_ten_minutes_before_and_counts_down(page, skew):
    o = page[f"warn_{skew}"]
    assert o["at0"] == "" and o["state0"]["level"] == "" and o["state0"]["access"] is True   # 25 min left: nothing
    assert o["at14"] == ""                                         # 11 min left: still nothing
    assert o["at16"] == "Sign-in ends in 9 min"
    assert {k: o["state16"][k] for k in ("level", "left", "access")} == {"level": "warn", "left": 9, "access": True}
    assert o["state16"]["until"] > 0
    assert o["tip16"].startswith("Cloudflare Access sign-in ends at ")
    assert o["href16"] == "/reauth/start"                          # still signed in: renew
    assert o["renew"] == {"opened": True, "prevented": True}
    # opened in the click, straight at its address, a new window each time (#826: never a blank one navigated later)
    assert o["renewOpened"] == [["https://hub.example.test/reauth/start", "_blank", "width=520,height=640"]]
    # the header is told on a change (the minutes), not on every tick
    assert o["state17"]["left"] == 8 and o["changes17"] == o["changes16"] + 1
    assert o["at24_5"] == "Sign-in ends in 1 min"
    assert o["polls"] >= 5                                         # load, every 5 min
    assert o["after"] == "Signed out" and o["afterState"]["level"] == "out" and o["afterState"]["until"] == 0
    assert o["afterHref"] == "/reauth"
    assert o["check"] == {"redirect": "manual", "cache": "no-store", "credentials": "same-origin"}
    assert o["checkUrl"].startswith("/api/session?t=")             # cache-busting


def test_a_redirected_fetch_is_signed_out_and_then_nothing_goes_out(page):
    o = page["fetch"]
    assert o["label0"] == ""
    assert o["first"] == "TypeError"                               # the caller sees what it saw before
    assert o["out"] is True and o["label"] == "Signed out" and o["checks"] == 2      # load + this one
    assert o["liveUps"] == 1 and "sizeup:signed-out" in o["events"]  # no "Reconnecting…": it says what it is
    assert o["next"] == "TypeError: signed out (Cloudflare Access)" and o["request"] == "TypeError"
    assert o["wentOut"] == 0                                       # no storm of redirects
    assert o["tiles"] == "ok" and o["v1"] == "ok" and o["wentOut2"] == 2
    assert o["checks2"] == o["checks"]


def test_a_401_checks_once_and_a_403_not_at_all(page):
    assert page["unauthorized"] == {"first": 1, "again": 0, "out": False, "forbidden": 0, "hub401": "unknown", "hub401out": False}


def test_an_audio_error_signs_out_and_re_sign_in_plays_it_again(page):
    o = page["audio"]
    assert o["label"] == "Signed out" and o["checks"] == 2
    assert o["click"] == {"opened": True, "prevented": True}       # the pop-up opened: the link not followed
    assert o["opened"] == [["https://hub.example.test/reauth", "_blank", "width=520,height=640"]]
    assert o["blocked"] == {"opened": False, "prevented": False}   # blocked: the link opens /reauth in a new tab
    assert o["ignored"] == 0 and o["stillOut"] is True              # another origin, another shape: nothing
    assert o["checked"] == 1 and o["out"] is False and o["after"] == "" and o["level"] == ""
    assert o["resumed"] is True and o["events"][-1] == "sizeup:signed-in"
    assert o["m"] == {"retries": 1, "loads": 0, "plays": 1}         # its own retry: the failed clip, again
    assert o["other"] == {"retries": 0, "loads": 0, "plays": 1}     # something else plays there now: left alone
    assert o["plain"] == {"loads": 0, "plays": 1}                   # no retry of its own: not played late


def test_a_message_during_an_older_check_checks_again(page):
    assert page["stale"] == {"out": False, "label": ""}


def test_the_popup_is_closed_from_here_and_a_stuck_one_gives_way_to_a_tab(page):
    o = page["popup"]
    assert o["opened"] is True and o["awayLeft"] == {"closed": False}       # on the Access login: left alone
    # signed in elsewhere (reauth.js's localStorage write): checked, and the pop-up closed from this page
    assert o["afterStorage"] == {"closed": True, "closes": 1, "level": ""}
    assert o["otherKey"] == 0                                               # another key: nothing
    # stuck on its blank page: its address once more at 5 s, closed at 12 s, and the next Re-sign in is the link's tab
    assert o["stuckOpened"] is True and o["replaced"] == ["https://hub.example.test/reauth/start"]
    assert o["stuckClosed"] is True and o["next"] == {"opened": False, "prevented": False} and o["nextOpened"] == 0


def test_a_missing_clip_is_not_a_sign_in_matter(page):
    assert page["missing"] == {"out": False, "label": "", "failed": 0, "checks": 2}


def test_reauth_tells_this_origin_only_and_closes(page):
    o = page["reauth"]
    assert o["outBefore"] is True
    assert o["posted"] == [[{"type": "sizeup:signed-in"}, "https://hub.example.test"]]   # targetOrigin: this origin
    assert o["closed"] == 1
    assert o["outAfter"] is False and o["label"] == ""               # the console checked (the channel) and is in
    assert o["noOpener"] == "ok"


def test_live_holds_the_stream_while_signed_out_and_resumes(page):
    o = page["live"]
    assert o["streams"] == 1 and o["out"] is True and o["closed"] is True
    assert o["polls"] == 0 and o["streamsWhileOut"] == 1           # 60 s signed out: nothing asked of the hub
    assert o["pill"] == [] and o["level"] == "out"                # "Signed out" (the header), not "Reconnecting…"
    assert o["outAfter"] is False and o["pollsAfter"] == 1 and o["streamsAfter"] == 2


# -- in a browser (Playwright, when installed: not on CI) ----------------------------------------------------------------

LOGIN = "https://login.example.invalid/cdn-cgi/access/login/hub.example.invalid"


@pytest.fixture
def console(tmp_path, keys, jwks):
    pw = pytest.importorskip("playwright.sync_api")
    from sizeup.cad.api import CadApi
    from sizeup.web.server import WebServer
    from tests.test_cad import make_cad
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(make_cad(tmp_path)), access=verifier(jwks))
    import threading
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{ws.httpd.server_address[1]}"
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no Chromium: {e}")
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        ctx.add_cookies([{"name": "CF_Authorization", "value": jwt(keys[0], exp=int(time.time()) + 300), "url": base}])
        ctx.route(lambda url: not url.startswith(base), lambda route: route.abort())
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        yield ctx, pg, base
        browser.close()
    ws.shutdown()


def test_in_a_browser_warned_signed_out_and_back_without_a_reload(console, keys):
    import os
    ctx, pg, base = console
    shots = os.environ.get("SIZEUP_SHOTS")                    # a directory: the PR's screenshots
    reqs = []
    pg.on("request", lambda r: reqs.append(r.url))
    pg.set_viewport_size({"width": 1920, "height": 900})
    pg.goto(base + "/cad")
    # the profile button, top right: the person's initials; 5 min left: an amber ring and the chip beside it
    me, chip = pg.locator("#me .me-btn"), pg.locator("#me .me-chip")
    chip.wait_for()
    text = lambda loc: " ".join(loc.inner_text().split())
    assert text(chip) in ("5 min \u00b7 Re-sign in", "4 min \u00b7 Re-sign in")
    assert pg.locator("label.operator").is_hidden()                       # who it is: the profile's now
    assert chip.get_attribute("href") == "/reauth/start"
    assert "warn" in me.get_attribute("class").split() and text(me.locator(".me-av")) == "C"
    assert "Cloudflare Access sign-in ends at" in me.get_attribute("title")
    box, hdr = me.bounding_box(), pg.locator("body > header").bounding_box()
    assert box["x"] + box["width"] > hdr["width"] - 60                   # at the right end of the header
    assert pg.locator(".session-pill").count() == 0                       # no bottom-centre pill any more
    if shots:
        pg.screenshot(path=f"{shots}/profile-ending.png", clip={"x": 1220, "y": 0, "width": 700, "height": 160})
    # its menu: who, until when, Re-sign in; Esc closes it and gives the focus back
    me.click()
    menu = pg.locator("#ui-menu")
    menu.wait_for()
    lines = [" ".join(t.split()) for t in menu.locator(".menu-info, .menu-item").all_inner_texts()]
    assert lines[0].startswith("casey@example.org") and lines[1].startswith("Sign-in until ") and lines[2] == "Re-sign in"
    assert "Preferences" not in " ".join(lines)                          # not built yet: hidden
    if shots:
        pg.screenshot(path=f"{shots}/profile-menu.png", clip={"x": 1420, "y": 0, "width": 500, "height": 200})
    pg.keyboard.press("Escape")
    menu.wait_for(state="detached")
    assert pg.evaluate("document.activeElement.classList.contains('me-btn')")
    me.click()
    menu.wait_for()
    pg.mouse.click(300, 400)                                              # a click outside closes it
    menu.wait_for(state="detached")
    pg.evaluate("window.__loaded = 1")                                    # gone if the page reloads

    # the sign-in ends: Cloudflare's edge sends every request to the Access login
    seen = []

    def edge(route):
        seen.append(route.request.url)
        route.fulfill(status=302, headers={"Location": LOGIN, "Cache-Control": "no-store"})
    pg.route(lambda url: url.startswith(base), edge)
    # the open stream goes on (it was let in before); the next clip played fails (the edge's redirect)
    pg.evaluate("new Audio('/api/audio/tx-0001.wav').play().catch(() => {})")
    pg.locator("#me .me-btn.err").wait_for(timeout=10000)                 # one check: an opaque redirect
    assert text(chip) == "Signed out \u00b7 Re-sign in" and "err" in chip.get_attribute("class").split()
    assert chip.get_attribute("href") == "/reauth"
    assert pg.locator(".reconnect-pill").count() == 0                     # not "Reconnecting…"
    n0 = len(seen)
    pg.wait_for_timeout(8000)
    assert len(seen) - n0 <= 1                                            # signed out: nothing more goes out
    if shots:
        pg.screenshot(path=f"{shots}/profile-signed-out.png", clip={"x": 1220, "y": 0, "width": 700, "height": 160})

    # Re-sign in from the menu: /reauth in a pop-up, as after Access signed in again (a new cookie); it tells the
    # page and closes
    pg.unroute_all()
    ctx.add_cookies([{"name": "CF_Authorization", "value": jwt(keys[0], exp=int(time.time()) + 24 * 3600), "url": base}])
    at_click = len(reqs)
    # by keys this time: Enter on the button opens the menu on its first choice (Re-sign in), Enter follows it
    me.focus()
    pg.keyboard.press("Enter")
    item = pg.locator("#ui-menu a.menu-item", has_text="Re-sign in")
    item.wait_for()
    assert item.get_attribute("href") == "/reauth"
    assert pg.evaluate("document.activeElement.textContent.trim()") == "Re-sign in"
    with ctx.expect_page() as popup_info:
        pg.keyboard.press("Enter")
    popup = popup_info.value
    popup.wait_for_event("close", timeout=10000)
    chip.wait_for(state="hidden")
    assert "err" not in me.get_attribute("class").split() and "warn" not in me.get_attribute("class").split()
    assert pg.evaluate("window.__loaded") == 1                            # the same page: nothing reloaded
    assert pg.evaluate("SessionWatch.out") is False
    after = reqs[at_click:]
    assert any("/api/cad/state" in u for u in after) and any("/api/cad/stream" in u for u in after)   # resumed
    assert pg.errors == []


def test_in_a_browser_the_profile_fits_a_phone_and_the_wall_board(console):
    import os
    ctx, pg, base = console
    shots = os.environ.get("SIZEUP_SHOTS")
    # a laptop: the chip folds to the minutes (Re-sign in: the chip itself, its hover, the menu); still on screen
    pg.goto(base + "/cad")
    chip, me = pg.locator("#me .me-chip"), pg.locator("#me .me-btn")
    chip.wait_for()
    pg.locator("label.operator").wait_for(state="hidden")                 # whoami in, the stream on: the header as
    pg.wait_for_function("document.querySelector('#conn').textContent === 'Live'")   # it stays
    pg.wait_for_timeout(300)
    assert " ".join(chip.inner_text().split()) in ("5 min", "4 min") and chip.get_attribute("aria-label").endswith(": Re-sign in")
    for out in (False, True):
        if out:      # (the state as signed out, drawn: what fits is the question here)
            pg.evaluate("SessionWatch._st.out = true; window.dispatchEvent(new CustomEvent('sizeup:session'))")
            pg.locator("#me .me-btn.err").wait_for()
        b = me.bounding_box()
        assert b["x"] + b["width"] <= 1280 and pg.evaluate("document.documentElement.scrollWidth") <= 1280, out
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.goto(base + "/cad")
    chip.wait_for()
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390     # no sideways scroll
    assert " ".join(chip.inner_text().split()) in ("5 min", "4 min")          # folded: Re-sign in in its hover / menu
    for loc in (chip, pg.locator("#me .me-btn")):
        b = loc.bounding_box()
        assert b["x"] >= 0 and b["x"] + b["width"] <= 390
    if shots:
        pg.screenshot(path=f"{shots}/profile-phone.png", clip={"x": 0, "y": 0, "width": 390, "height": 140})
    pg.locator("#me .me-btn").click()
    m = pg.locator("#ui-menu")
    m.wait_for()
    b = m.bounding_box()
    assert b["x"] >= 0 and b["x"] + b["width"] <= 390
    if shots:
        pg.screenshot(path=f"{shots}/profile-phone-menu.png", clip={"x": 0, "y": 0, "width": 390, "height": 320})
    # the wall board has the same header: the button there too
    pg.set_viewport_size({"width": 1280, "height": 800})
    pg.goto(base + "/monitor")
    pg.locator("#me .me-btn").wait_for()
    pg.locator("#me .me-chip").wait_for()
    # a pop-up blocker, from the menu: the link itself opens it in a new tab (Space too)
    for key in ("click", " "):
        pg.evaluate("window.open = () => null")
        pg.locator("#me .me-btn").click() if key == "click" else (pg.locator("#me .me-btn").focus(), pg.keyboard.press("Enter"))
        item = pg.locator("#ui-menu a.menu-item", has_text="Re-sign in")
        item.wait_for()
        with ctx.expect_page() as tab:
            item.click() if key == "click" else pg.keyboard.press(" ")
        assert tab.value.url.split("?")[0].endswith(("/reauth/start", "/reauth")), key
        tab.value.close()
        pg.locator("#ui-menu").wait_for(state="detached")
    assert pg.errors == []


# -- the pop-up's round trip through a stand-in for Cloudflare's edge and the Access team domain (#826) ----------------

class FakeEdge:
    """Cloudflare's edge and the Access team domain, on this machine. The edge (127.0.0.1) passes a request with the
    CF_Authorization cookie on to the hub, sends one without it to the team's login (another origin: localhost),
    and at /cdn-cgi/access/authorized sets a fresh cookie and goes back. The team's login, its session still good,
    answers a page that goes on by script (optionally with Cross-Origin-Opener-Policy), as Access's does. The edge
    takes off the hub's cookie attributes a browser refuses over plain http (Secure, SameSite=None): it stands where
    Cloudflare ends TLS."""

    def __init__(self, hub_port, token, coop=""):
        import http.client
        import threading
        from http.cookies import SimpleCookie
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, quote, urlsplit
        me = self
        self.log = []

        class Edge(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _redir(self, where, extra=None):
                self.send_response(302)
                self.send_header("Location", where)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_GET(self):
                u = urlsplit(self.path)
                jar = SimpleCookie(self.headers.get("Cookie") or "")
                if u.path == "/cdn-cgi/access/authorized":
                    me.log.append("authorized")
                    back = parse_qs(u.query).get("redirect_url", ["/"])[0]
                    return self._redir(back, {"Set-Cookie": f"CF_Authorization={token()}; Path=/; HttpOnly"})
                if "CF_Authorization" not in jar:
                    me.log.append(f"login {u.path}")
                    return self._redir(f"http://localhost:{me.team_port}/cdn-cgi/access/login/hub?redirect_url={quote(self.path)}")
                if u.path.startswith("/reauth"):
                    me.log.append(u.path)
                c = http.client.HTTPConnection("127.0.0.1", hub_port, timeout=60)
                c.request("GET", self.path, headers={k: v for k, v in self.headers.items() if k.lower() not in ("connection",)})
                r = c.getresponse()
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() in ("connection", "transfer-encoding", "keep-alive"):
                        continue
                    if k.lower() == "set-cookie":
                        v = v.replace("; Secure", "").replace("; SameSite=None", "; SameSite=Lax")
                    self.send_header(k, v)
                if r.getheader("Content-Length") is None:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                try:
                    while b := r.read1(65536):
                        self.wfile.write(b)
                        self.wfile.flush()
                except OSError:
                    pass
                c.close()

        class Team(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                back = parse_qs(urlsplit(self.path).query).get("redirect_url", ["/"])[0]
                to = f"http://127.0.0.1:{me.edge_port}/cdn-cgi/access/authorized?redirect_url={quote(back)}"
                body = (f"<!doctype html><title>Sign in</title><p>Signing in"
                        f"<script>setTimeout(() => location.replace({json.dumps(to)}), 200)</script>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if coop:
                    self.send_header("Cross-Origin-Opener-Policy", coop)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), Edge), ThreadingHTTPServer(("127.0.0.1", 0), Team)]
        self.edge_port, self.team_port = (s.server_address[1] for s in self.servers)
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.edge_port}"

    def close(self):
        for s in self.servers:
            s.shutdown()


@pytest.fixture(params=["chromium", "chromium+coop", "webkit"])
def edged(request, tmp_path, keys, jwks):
    """(context, console page, edge): the console 5 min before its sign-in ends, behind the stand-in edge."""
    pw = pytest.importorskip("playwright.sync_api")
    import threading
    from sizeup.cad.api import CadApi
    from sizeup.web.server import WebServer
    from tests.test_cad import make_cad
    engine, _, coop = request.param.partition("+")
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(make_cad(tmp_path)), access=verifier(jwks))
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    edge = FakeEdge(ws.httpd.server_address[1], lambda: jwt(keys[0], exp=int(time.time()) + 24 * 3600),
                    "same-origin" if coop else "")
    with pw.sync_playwright() as p:
        try:
            browser = getattr(p, engine).launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no {engine}: {e}")
        ctx = browser.new_context(viewport={"width": 1920, "height": 900})
        ctx.add_cookies([{"name": "CF_Authorization", "value": jwt(keys[0], exp=int(time.time()) + 300), "url": edge.url}])
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(edge.url + "/cad")
        pg.locator("#me .me-chip").wait_for()
        yield ctx, pg, edge
        browser.close()
    edge.close()
    ws.shutdown()


def _navigations(ctx):
    """[(page | "new", url)] of every navigation request from now on, in order ("new": a window's very first, made
    before its page exists)."""
    out = []

    def seen(r):
        if not r.is_navigation_request():
            return
        try:
            out.append((r.frame.page, r.url))
        except Exception:  # noqa: BLE001 - Playwright: no frame yet, the request opened the window
            out.append(("new", r.url))
    ctx.on("request", seen)
    return out


def _renewed(pg):
    pg.wait_for_function("SessionWatch.state().level === '' && SessionWatch.state().until > Date.now() + 23 * 3600e3")


def test_in_a_browser_re_sign_in_goes_straight_to_reauth_start_and_closes(edged):
    """#826: the pop-up's first navigation is /reauth/start (never a blank window navigated later); through the
    edge's round trip it renews the sign-in, tells the console and is closed; nothing is left open."""
    ctx, pg, edge = edged
    navs = _navigations(ctx)
    with ctx.expect_page() as info:
        pg.locator("#me .me-chip").click()
    popup = info.value
    popup.wait_for_event("close", timeout=15000)
    first = [u for p, u in navs if p in (popup, "new")]
    assert first and first[0].endswith("/reauth/start"), first          # the window's very first request          # straight there: not about:blank first
    assert edge.log[:4] == ["/reauth/start", "login /reauth", "authorized", "/reauth"]
    _renewed(pg)
    assert ctx.pages == [pg] and pg.errors == []                          # no window left behind


def test_in_a_browser_a_blocked_popup_opens_a_tab_that_still_renews(edged):
    """window.open refused (a blocker, or a browser that opens it noopener): the link's own new tab (rel=noopener, no
    opener at all) goes the same way, and the console hears it without the opener (BroadcastChannel / storage)."""
    ctx, pg, edge = edged
    pg.evaluate("window.open = () => null")
    navs = _navigations(ctx)
    with ctx.expect_page() as info:
        pg.locator("#me .me-chip").click()
    tab = info.value
    _renewed(pg)
    first = [u for p, u in navs if p in (tab, "new")]
    assert first and first[0].endswith("/reauth/start"), first          # the window's very first request
    if not tab.is_closed():                                               # a tab script may not close: it says so
        assert "Signed in" in tab.locator("main").inner_text()
    assert pg.errors == []
