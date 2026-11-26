"""Settings > Station > STT workers in a browser (sizeup/web/static/sttworkers.js, #639): an admin adds a GPU worker and
sees its token once, turns a class of the pool off, revokes the worker; a dispatcher never sees the tab. And the
Health tab's GPU worker and latency rows (health.js). Playwright + Chromium; skipped without them. Every id, name and
number is made up. SIZEUP_SHOTS=<dir> also saves screenshots there (the PR's)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud.hubstore import MonitorDb
from sizeup.cloud.sttworkers import WorkerRegistry
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_roles import CASEY, JORDAN, people
from tests.test_stations_card import Access, browser_page

SHOTS = os.environ.get("SIZEUP_SHOTS", "")
RUNS = "4f1c9e2a7b3d8c6e0a5f2b9d1c7e3a8f6b0d4c2e9a1f7b3c5d8e0a2f4b6c9d1e"     # made-up worker.py hashes
WANTED = "c4d1e8a07b2f9e3d6a5c1b0f8e7d2a9c3b6f5e4d1a0c9b8e7f6d5c4b3a2e1f0d"
UPDATE = {"update": {"sha256": RUNS, "commit": "", "auto": True, "state": f"staged {WANTED[:12]}, waiting: gaming",
                     "bad": []}, "update_wanted": WANTED, "update_current": False}


class FakeHealth:
    """Settings > Station > Health's data, the GPU pool's part filled in (invented numbers)."""

    def panel(self):
        now = time.time()
        pool = {"active": True, "settings": {"on": True, "classes": {}}, "ready": 1, "queued": 2, "leased": 1,
                "queued_by_class": {"low": 2}, "done_h": 412, "p50_s": 0.7, "p90_s": 1.9,
                "fallbacks": {"unclaimed": 3, "lease": 1, "released": 0, "no_worker": 0, "error": 0}, "refused_late": 1,
                "workers": [{"id": "gpu-1", "state": "online", "model": "ggml-large-v3-turbo-q5_0.bin",
                             "gpu": {"util_pct": 31, "mem_used_mb": 1650, "mem_total_mb": 12288}, "jobs_h": 412,
                             "lost": 1, "seen": now - 2, "seen_s": 2, "keep_awake": {"on": True, "held": True},
                             **UPDATE},
                            {"id": "gpu-2", "state": "offline", "model": "ggml-large-v3-turbo-q5_0.bin", "gpu": {},
                             "jobs_h": 0, "lost": 0, "seen": now - 600, "seen_s": 600, "asleep": True,
                             "keep_awake": {"on": True, "held": True}},
                            {"id": "gpu-3", "state": "offline", "model": "ggml-large-v3-turbo-q5_0.bin", "gpu": {},
                             "jobs_h": 0, "lost": 0, "seen": now - 900, "seen_s": 900, "asleep": False,
                             "keep_awake": {"on": False, "held": False}},
                            {"id": "station:st-north", "kind": "station", "state": "paused", "model": "small.en-q5_1",
                             "load": {"own": 6, "cpu_pct": 91.0, "service_s": 14.2, "why": "its own clips first"},
                             "gpu": {}, "jobs_h": 38, "lost": 0, "seen": now - 1, "seen_s": 1},
                            {"id": "station:st-south", "kind": "station", "state": "online", "model": "small.en-q5_1",
                             "load": {"own": 0, "cpu_pct": 22.0, "service_s": 12.8}, "gpu": {}, "jobs_h": 141, "lost": 0,
                             "seen": now - 1, "seen_s": 1}],
                "latency": [{"channel": "tg:307", "n": 61, "p50_s": 2.1, "p95_s": 6.0, "target": True, "over": False,
                             "engines": {"gpu-worker": 58, "aws": 3}},
                            {"channel": "p25:mrrs:9211", "n": 40, "p50_s": 6.2, "p95_s": 17.5, "target": True,
                             "over": True, "engines": {"gpu-worker": 30, "aws": 10}},
                            {"channel": "tg:383", "n": 300, "p50_s": 1.4, "p95_s": 3.9, "target": False, "over": False,
                             "engines": {"gpu-worker": 300}}],
                "target": {"p50_s": 5.0, "p95_s": 15.0}}
        return {"health": {"stt_pool": pool, "hints": [], "dongles": [], "channels": []}, "alerts": [], "history": [],
                "notify": {"on": False, "muted": []}, "thresholds": {}}


@pytest.fixture
def site(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda m: None)
    cad.stt_workers = reg
    cad.stt_workers_live = lambda wid: {"state": "online", "model": "large-v3-turbo-q5_0", "seen": time.time(), **UPDATE}
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad), access=Access(), role="hub", station_health=FakeHealth())
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    yield type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "reg": reg, "cad": cad})()
    ws.shutdown()


def shot(pg, sel, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.locator(sel).screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def test_an_admin_adds_a_worker_sees_the_token_once_switches_a_class_and_revokes(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/workers")
        card = pg.locator("#s-station-workers .card")
        card.wait_for()
        assert card.locator("td.empty").inner_text() == "None"
        card.locator("label", has_text="Id").locator("input").fill("gpu-1")
        card.locator("label", has_text="Name").locator("input").fill("Gaming PC")
        card.get_by_role("button", name="Add").click()
        tok = card.locator(".notice.ok p.mono").first
        tok.wait_for()
        token = tok.inner_text()
        assert token.startswith("sw1.gpu-1.") and site.reg.resolve(token) == "gpu-1"
        shot(pg, "#s-station-workers", "sttworkers-new-token")
        card.get_by_role("button", name="Done").click()
        pg.wait_for_function("!document.querySelector('#s-station-workers .notice.ok')")
        assert token not in pg.content()
        card.locator("td", has_text="online").wait_for()
        card.locator("label", has_text="Shed clips (catch-up)").locator("input").uncheck()
        pg.wait_for_function("() => true")
        deadline = time.time() + 5
        while site.reg.pool_settings()["classes"]["catchup"] and time.time() < deadline:
            time.sleep(0.1)
        assert site.reg.pool_settings()["classes"]["catchup"] is False
        shot(pg, "#s-station-workers", "sttworkers-list")
        card.locator("tr", has_text="Gaming PC").get_by_role("button", name="Revoke").click()
        pg.get_by_role("alertdialog").get_by_role("button", name="Revoke").click()
        card.locator("td", has_text="revoked").wait_for()
        assert pg.evaluate("() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage)").count("sw1.") == 0
        audit = json.dumps(site.cad.settings.audit_log("system"))
        assert "stt workers/gpu-1" in audit and token not in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_health_shows_the_gpu_workers_and_the_latency_by_channel(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/health")
        pg.get_by_text("GPU workers (hub)").wait_for()
        panel = pg.locator("#s-station-health")
        assert panel.get_by_text("412/h · p50 0.7 s · p90 1.9 s").is_visible()
        assert panel.get_by_text("Speech-to-text latency").is_visible()
        assert panel.locator("td.hl-bad", has_text="17.5 s").is_visible()          # a pre-alert channel over target
        assert panel.get_by_text("st-north (station)").is_visible()                # a station's whisper (#639 phase 2)
        assert panel.get_by_text("6 own · CPU 91% · 14.2 s/clip").is_visible()
        ver = panel.locator("td.hl-warn", has_text=RUNS[:12])                       # a GPU worker behind the hub's version
        assert ver.is_visible() and f"Hub: {WANTED[:12]}" in ver.get_attribute("title")
        if SHOTS:                                  # (the panel scrolls: from the GPU workers' heading down)
            head = panel.get_by_text("GPU workers (hub)")
            pg.evaluate("el => el.scrollIntoView({block: 'start'})", head.element_handle())
            box = head.bounding_box()
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(Path(SHOTS) / "health-gpu-workers.png"),
                          clip={"x": box["x"] - 8, "y": box["y"] - 8, "width": 980, "height": 470})
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_health_says_keeps_pc_awake_and_offline_since_asleep(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/health")
        pg.get_by_text("GPU workers (hub)").wait_for()
        panel = pg.locator("#s-station-health")
        rows = {r: panel.locator("tr", has=pg.locator("td", has_text=r)).first for r in ("gpu-1", "gpu-2", "gpu-3")}
        on = rows["gpu-1"].locator("td.hl-ok", has_text="online · keeps PC awake")
        assert on.is_visible() and "idle-sleep" in on.get_attribute("title")
        asleep = rows["gpu-2"].locator("td.hl-bad", has_text="offline since")
        assert asleep.inner_text().endswith(" (asleep?)") and "stopped while online" in asleep.get_attribute("title")
        quiet = rows["gpu-3"].locator("td.hl-bad", has_text="offline since")
        assert "asleep" not in quiet.inner_text() and "keeps PC awake" not in rows["gpu-3"].inner_text()
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_an_admin_holds_pins_and_frees_a_workers_self_update(site):
    site.reg.add("gpu-1", "Gaming PC")
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/workers")
        card = pg.locator("#s-station-workers .card")
        row = card.locator("tr", has_text="Gaming PC")
        row.locator("span.mono.hl-warn", has_text=RUNS[:12]).wait_for()          # behind the hub's version
        sel = row.locator("select")
        assert sel.input_value() == "auto"

        def mode(want):                    # (the page's requests go through Playwright's route: wait in Playwright)
            deadline = time.time() + 5
            while site.reg.update_of("gpu-1") != want and time.time() < deadline:
                pg.wait_for_timeout(100)
            assert site.reg.update_of("gpu-1") == want
        sel.select_option("hold")
        mode({"mode": "hold"})
        sel.select_option("pin")
        card.locator("label", has_text="Commit").locator("input").fill("3b7e1f0c9a2d4e6b8c0a1f3e5d7b9c2a4e6f8a0b")
        card.locator("label", has_text="SHA-256").locator("input").fill(WANTED)
        shot(pg, "#s-station-workers", "sttworkers-pin")
        card.get_by_role("button", name="Pin", exact=True).click()
        mode({"mode": "pin", "commit": "3b7e1f0c9a2d4e6b8c0a1f3e5d7b9c2a4e6f8a0b", "sha256": WANTED})
        pg.wait_for_function("[...document.querySelectorAll('#s-station-workers select option')].some(o => o.textContent === 'Pin 3b7e1f0')")
        sel.select_option("auto")
        mode({"mode": "auto"})
        audit = json.dumps(site.cad.settings.audit_log("system"))
        assert "stt workers/gpu-1/update" in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_a_dispatcher_never_sees_the_tab(site):
    pw, browser, pg = browser_page(site, JORDAN)
    try:
        pg.goto(site.base + "/cad#settings/station/workers")
        pg.wait_for_function("typeof cad !== 'undefined' && !!cad.state && !!cad.who")
        pg.wait_for_timeout(1000)
        assert not pg.locator("#s-station-workers .card").is_visible()
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()
