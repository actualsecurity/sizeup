"""Settings > Station > Health's LLM rows (health.js, sizeup/cloud/infer.py): each GPU worker's two models, the lane's
jobs an hour, p50 / p95 and failures by task. Playwright + Chromium; skipped without them. Every id, model and number is
made up. SIZEUP_SHOTS=<dir> also saves a screenshot there (the PR's)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_roles import CASEY, people
from tests.test_stations_card import Access, browser_page
from tests.test_sttworkers_card import SHOTS, FakeHealth


class InferHealth(FakeHealth):
    def panel(self):
        out = super().panel()
        out["health"]["stt_pool"]["infer"] = {
            "available": True, "ready": 1, "queued": 0, "leased": 1, "jobs_h": 9, "ok_h": 7, "p50_s": 1.4, "p95_s": 2.9,
            "why_h": {"ok": 7, "deadline": 1, "gaming": 1},
            "workers": [{"id": "gpu-1", "state": "online", "stt": "ggml-large-v3-turbo-q5_0.bin",
                         "llm": "qwen2.5-7b-instruct-q4_k_m", "llm_version": "a1b2c3d4e5f6", "busy": True,
                         "jobs_h": 9, "fails_h": 2}],
            "tasks": {"pd_classify": {"n": 9, "ok": 7, "p50_s": 1.4, "p95_s": 2.9, "tokens_in": 640, "tokens_out": 74,
                                      "why": {"deadline": 1, "gaming": 1}}}}
        return out


@pytest.fixture
def site(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad), access=Access(), role="hub", station_health=InferHealth())
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    yield type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "cad": cad})()
    ws.shutdown()


def test_health_shows_each_workers_stt_and_llm_and_the_lanes_numbers_by_task(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/health")
        pg.get_by_text("GPU workers: LLM").wait_for()
        panel = pg.locator("#s-station-health")
        assert panel.get_by_text("7/h · p50 1.4 s · p95 2.9 s · 0 waiting · 1 out").is_visible()
        assert panel.get_by_text("1 deadline · 1 gaming").first.is_visible()
        row = panel.locator("tr", has_text="qwen2.5-7b-instruct-q4_k_m")
        assert row.locator("td").nth(2).inner_text() == "ggml-large-v3-turbo-q5_0.bin"
        assert row.locator("td").nth(3).get_attribute("title") == "Version a1b2c3d4e5f6"
        task = panel.locator("tr", has_text="pd_classify")
        assert [task.locator("td").nth(i).inner_text() for i in range(5)] == ["pd_classify", "9", "1.4 s", "2.9 s", "640/74"]
        if SHOTS:
            head = panel.get_by_text("GPU workers: LLM")
            pg.evaluate("el => el.scrollIntoView({block: 'center'})", head.element_handle())
            time.sleep(0.2)
            box = head.bounding_box()
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(Path(SHOTS) / "health-gpu-llm.png"),
                          clip={"x": box["x"] - 8, "y": box["y"] - 8, "width": 980, "height": 330})
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


@pytest.fixture
def workers_site(tmp_path):
    from sizeup.cloud.hubstore import MonitorDb
    from sizeup.cloud.sttworkers import WorkerRegistry
    cad = make_cad(tmp_path)
    people(cad)
    reg = WorkerRegistry(MonitorDb(tmp_path / "hub.sqlite"), log=lambda m: None)
    reg.add("gpu-1", "Gaming PC")
    reg.add("gpu-2", "Whisper only")
    cad.stt_workers = reg
    cad.stt_workers_live = lambda wid: {"state": "online", "model": "large-v3-turbo-q5_0", "seen": time.time(),
                                        "llm": "qwen2.5-7b-instruct-q4_k_m" if wid == "gpu-1" else ""}
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: {"time": 0}, lambda cid: None,
                   lambda iid: False, cad=CadApi(cad), access=Access(), role="hub", station_health=InferHealth())
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    yield type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "cad": cad, "reg": reg})()
    ws.shutdown()


def test_an_admin_turns_a_workers_llm_jobs_off_and_it_is_audited(workers_site):
    site = workers_site
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/workers")
        card = pg.locator("#s-station-workers .card")
        row = card.locator("tr", has_text="Gaming PC")
        box = row.locator("label", has_text="qwen2.5-7b-instruct-q4_k_m").locator("input")
        box.wait_for()
        assert box.is_checked()                                  # on by default
        assert card.locator("tr", has_text="Whisper only").locator("input[type=checkbox]").count() == 0
        box.uncheck()
        deadline = time.time() + 5
        while site.reg.llm_on("gpu-1") and time.time() < deadline:
            time.sleep(0.1)
        assert site.reg.llm_on("gpu-1") is False and site.reg.llm_on("gpu-2") is True
        import json as _json
        assert "stt workers/gpu-1/llm jobs" in _json.dumps(site.cad.settings.audit_log("system"))
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            card.screenshot(path=str(Path(SHOTS) / "sttworkers-llm-jobs.png"))
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()
