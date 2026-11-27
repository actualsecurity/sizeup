"""The per-channel speech models in Settings (#639 models, sizeup/sttmodels.py), in a browser: Settings > Radios >
Channels' Speech model select (radio-controls.js), Settings > Station > STT workers' loaded models and its Speech
models card (sttworkers.js), and Settings > Radios > Radio IDs' Hint switch (radios-panel.js). Playwright + Chromium;
skipped without them. Every id, name, key and number is made up. SIZEUP_SHOTS=<dir> also saves screenshots there."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from sizeup import sttmodels
from sizeup.cad.api import CadApi
from sizeup.cloud.hubstore import MonitorDb
from sizeup.cloud.sttworkers import WorkerRegistry
from sizeup.web.server import WebServer
from tests.test_cad import make_cad
from tests.test_channels_by_system import STATE
from tests.test_roles import CASEY, JORDAN, people
from tests.test_stations_card import Access, browser_page

SHOTS = os.environ.get("SIZEUP_SHOTS", "")
QWEN, ATC, Q5, Q8 = "qwen3-asr-1.7b-q8_0", "large-v3-turbo-us-atc-v3-f16", "large-v3-turbo-q5_0", "large-v3-turbo-q8_0"


@pytest.fixture
def site(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    mdb = MonitorDb(tmp_path / "hub.sqlite")
    reg = WorkerRegistry(mdb, log=lambda m: None)
    reg.add("gpu-1", "Gaming PC")
    cad.stt_workers = reg
    cad.stt_workers_live = lambda wid: {"state": "online", "model": Q5, "seen": time.time(), "models": [Q5, QWEN],
                                        "stt_state": {Q5: "ready", QWEN: "ready", ATC: "downloading model",
                                                      Q8: "no room (VRAM)"}}
    cad.stt_models = sttmodels.Choices(mdb)
    cad.stt_models.set("FG1", {"callsigns": ["Command"]})
    cad.stt_models.set("tg:307", {"model": Q8, "context": "off"})
    cad.cloud = lambda: {"role": "hub", "state": "ok", "word": "", "detail": "connected"}
    state = json.loads(json.dumps(STATE))
    state["talkgroups"][0]["service_auto"] = "fire"
    state["talkgroups"][2]["service_auto"] = "police"
    ws = WebServer("127.0.0.1", 0, {"timezone": "America/New_York"}, lambda: dict(state, time=time.time()),
                   lambda cid: None, lambda iid: False, cad=CadApi(cad), access=Access(), role="hub")
    threading.Thread(target=ws.serve_forever, daemon=True).start()
    yield type("Site", (), {"base": f"http://127.0.0.1:{ws.httpd.server_address[1]}", "cad": cad})()
    ws.shutdown()


def shot(pg, sel, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        pg.locator(sel).first.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def until(pg, fn, what):
    deadline = time.time() + 5
    while not fn() and time.time() < deadline:
        pg.wait_for_timeout(100)
    assert fn(), what


def test_a_channels_speech_model_auto_by_its_service_and_chosen(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/radios/channels")
        row = lambda key: pg.locator(f"li.tg-row[data-ckey='{key}']")        # noqa: E731
        sel = lambda key: row(key).locator("select.tg-mod")                 # noqa: E731
        sel("FG1").wait_for()
        # Auto: today's whisper on every channel until an admin switches it (the recommendation in the hover); a choice
        assert sel("FG1").locator("option").first.inner_text() == f"Auto ({Q5})" and sel("FG1").input_value() == ""
        assert sel("Tower").locator("option").first.inner_text() == f"Auto ({Q5})"
        assert sel("p25:mrrs:9211").locator("option").first.inner_text() == f"Auto ({Q5})"
        assert sel("tg:307").input_value() == Q8
        assert [o.get_attribute("value") for o in sel("FG1").locator("option").all()] == ["", Q5, Q8, QWEN, ATC]
        t = sel("tg:307").get_attribute("title")
        assert Q8 in t and "Context: off" in t and "(auto)" not in t
        assert "Call signs: Command" in sel("FG1").get_attribute("title")
        shot(pg, "#settings-radio-controls .tg-group", "channels-speech-model")
        sel("FG1").select_option(QWEN)
        until(pg, lambda: site.cad.stt_models.model_for("FG1", "fire") == QWEN, "FG1: Qwen3-ASR")
        sel("tg:307").select_option("")
        until(pg, lambda: "model" not in site.cad.stt_models.settings().get("tg:307", {}), "tg:307 back to Auto")
        assert site.cad.stt_models.settings()["tg:307"] == {"context": "off"}
        audit = json.dumps(site.cad.settings.audit_log("radios"))
        assert "stt_models/FG1/model" in audit and "stt_models/tg:307/model" in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_no_speech_model_select_for_a_dispatcher(site):
    pw, browser, pg = browser_page(site, JORDAN)
    try:
        pg.goto(site.base + "/cad#settings/radios/channels")
        pg.locator("li.tg-row[data-ckey='FG1']").wait_for()
        pg.wait_for_timeout(500)
        assert not pg.locator("select.tg-mod").first.is_visible()
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_the_workers_models_and_each_channels_context_and_call_signs(site):
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/station/workers")
        row = pg.locator("#s-station-workers tr", has_text="Gaming PC")
        cell = row.locator("td", has_text=QWEN)
        cell.wait_for()
        assert cell.inner_text() == f"{Q5} · {QWEN} · {ATC}: downloading · {Q8}: no room (VRAM)"
        assert cell.locator("span.hl-warn").inner_text() == f"{Q8}: no room (VRAM)"
        assert f"{ATC}: downloading model" in cell.get_attribute("title")
        m = pg.locator("#s-station-workers .stt-models")
        m.wait_for()
        cat = m.locator("table").first
        assert cat.locator("tr", has_text=QWEN).locator("td").all_inner_texts() == [QWEN, "llama-asr", "3.7 GB",
                                                                                   "fire · ems · police", "list · on"]
        assert cat.locator("tr", has_text=ATC).locator("td").nth(3).inner_text() == "air"
        assert cat.locator("tr", has_text=Q5).first.locator("td").nth(3).inner_text() == "default"
        chans = m.locator("table").nth(1)
        fg1 = chans.locator("tr", has_text="FG1")
        assert fg1.locator("input").input_value() == "Command" and fg1.locator("select").input_value() == "default"
        fg1.locator("select").select_option("off")
        until(pg, lambda: site.cad.stt_models.settings()["FG1"].get("context") == "off", "FG1 context off")
        fg1 = chans.locator("tr", has_text="FG1")
        fg1.locator("input").fill("Command, Made-up Base")
        fg1.locator("input").press("Tab")
        until(pg, lambda: site.cad.stt_models.callsigns("FG1") == ["Command", "Made-up Base"], "FG1 call signs")
        # a channel not listed yet
        add = chans.locator("tr").last
        add.locator("input").first.fill("South 2")
        add.locator("input").nth(1).fill("Made-up Desk")
        add.get_by_role("button", name="Add").click()
        until(pg, lambda: site.cad.stt_models.callsigns("South 2") == ["Made-up Desk"], "South 2 added")
        chans.locator("td", has_text="South 2").wait_for()
        # a call sign that isn't one: said, nothing saved
        fg1 = chans.locator("tr", has_text="FG1")
        fg1.locator("input").fill("Command, <b>")
        fg1.locator("input").press("Tab")
        m.get_by_text("isn't a call sign").wait_for()
        assert site.cad.stt_models.callsigns("FG1") == ["Command", "Made-up Base"]
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            pg.locator("#s-station-workers tr", has_text="Gaming PC").screenshot(path=str(Path(SHOTS) / "sttworkers-models-row.png"))
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        shot(pg, "#s-station-workers .card", "sttworkers-speech-models")
        audit = json.dumps(site.cad.settings.audit_log("radios"))
        assert "stt_models/FG1/context" in audit and "stt_models/South 2/callsigns" in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_a_radios_hint_switch_shows_the_effective_state_and_sets_it(site):
    r = site.cad.radios
    r.save_range({"system": "mrrs", "lo": "9900100", "hi": "9900199", "label": "Made-up PD"}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900501", "label": "Headquarters"}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900150"}, "admin")
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/radios/ids")
        sys = pg.locator("#settings-radios .rp-tools select").nth(1)
        sys.wait_for()
        sys.select_option("mrrs")
        hq = pg.locator("#settings-radios tr", has_text="9900501").locator("button.sw")
        hq.wait_for()
        # another P25 system's radio: off by default, even with its own exact label (MRRS: roles, agencies)
        assert hq.get_attribute("aria-checked") == "false" and "(default)" in hq.get_attribute("title")
        assert "A range's label is never a hint" in hq.get_attribute("title")
        ranged = pg.locator("#settings-radios tr", has_text="9900150").locator("button.sw")
        assert ranged.get_attribute("aria-checked") == "false"                     # a range's label: off by default
        shot(pg, "#settings-radios .rp-scroll", "radio-ids-hint")
        hq.click()                                                                  # an admin's on: a console's name
        until(pg, lambda: r.get("mrrs", "9900501")["hint"] == 1, "its hint on")
        hq = pg.locator("#settings-radios tr", has_text="9900501").locator("button.sw")
        pg.wait_for_function("() => document.querySelector('#settings-radios tr button.sw[title^=\"Hint on\"]')")
        assert "(default)" not in hq.get_attribute("title")
        assert r.hint_on(r.get("mrrs", "9900501"))
        hq.click()                                                                  # off again
        until(pg, lambda: r.get("mrrs", "9900501")["hint"] == 0, "its hint off")
        assert not r.hint_on(r.get("mrrs", "9900501"))
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_a_channels_challenger_its_evidence_and_promote_on_its_radios_row(site):
    """#869: the challenger is on the channel's own row (never the STT workers pane), its evidence terse there (n of the
    samples needed, the difference; the rest in the hover), and Promote only once the hub says the evidence is there,
    shown again before the admin confirms."""
    from sizeup import sttscore
    boards = []
    site.cad.stt_challenges = lambda: boards

    def promote(key, op, confirm=False):
        ok, said = sttscore.promote_ok(boards[0])
        if not ok:
            raise ValueError(f"not enough evidence to promote: {said}")
        if not confirm:
            return {"confirm": said}
        old, new = site.cad.stt_models.promote(key)
        return {"old": old, "new": new, "evidence": said}
    site.cad.stt_promote = promote
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/radios/channels")
        row = lambda key: pg.locator(f"li.tg-row[data-ckey='{key}']")        # noqa: E731
        ch = row("tg:307").locator("select.tg-chall")
        ch.wait_for()
        assert Q8 not in [o.get_attribute("value") for o in ch.locator("option").all()]   # (never its own champion)
        ch.select_option(QWEN)
        until(pg, lambda: (site.cad.stt_models.challenger_for("tg:307") or ("",))[0] == QWEN, "a challenger set")
        assert not row("tg:307").locator("button.tg-promote").is_visible()       # (no evidence yet: no Promote)
        boards.append({"key": "tg:307", "champion": Q8, "challenger": QWEN, "pct": 10.0, "n": 42, "samples": 44,
                       "champ": 0.612, "chall": 0.705, "diff": 0.093, "ci": 0.031, "verdict": "challenger better",
                       "domain": "fire", "min_n": 40, "promoted": {}, "rolled_back": {}, "edits": 3,
                       "norm": sttscore.NORM, "paused": False})
        boards[0].update(dict(zip(("promote_ok", "evidence"), sttscore.promote_ok(boards[0]))))
        pg.reload()
        ev = row("tg:307").locator(".tg-ev")
        ev.wait_for()
        assert ev.inner_text() == "42/40 · +0.09" and "0.612" in ev.get_attribute("title")
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        shot(pg, "#settings-radio-controls .tg-group", "channels-challenger")
        row("tg:307").locator("button.tg-promote").click()
        dialog = pg.get_by_role("alertdialog")
        dialog.wait_for()
        assert "over 42 clips" in dialog.inner_text()                             # (the evidence, before it applies)
        pg.get_by_role("button", name="Promote").last.click()
        until(pg, lambda: site.cad.stt_models.settings()["tg:307"].get("model") == QWEN, "promoted")
        assert site.cad.stt_models.challenger_for("tg:307")[0] == Q8
        audit = json.dumps(site.cad.settings.audit_log("radios"))
        assert "stt_models/tg:307/challenger" in audit and "stt_models/tg:307/model" in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()


def test_an_aws_channels_shadow_evidence_on_its_radios_row(site):
    """#878: an AWS channel's GPU candidates against AWS: terse on its row ("vs AWS n/needed", a check when one matches
    or beats AWS), the rest in the hover; a click turns its shadow runs off (an admin's, audited). Never the STT workers
    pane, never a switch of the channel."""
    shadows = [{"key": "FG1", "on": True, "pct": 10.0, "min_n": 40, "models": [
        {"model": QWEN, "n": 41, "agree": 0.91, "agree_n": 41, "ids": 0.97, "ids_n": 38,
         "time": {"aws": 0.8, "model": 0.82, "n": 12}, "edit": {"aws": None, "model": None, "n": 0},
         "verdict": "matches or beats AWS on 41 clips", "ok": True}]}]
    site.cad.stt_shadows = lambda: [dict(s, on=site.cad.stt_models.shadow_on(s["key"])) for s in shadows]
    pw, browser, pg = browser_page(site, CASEY)
    try:
        pg.goto(site.base + "/cad#settings/radios/channels")
        sh = pg.locator("li.tg-row[data-ckey='FG1'] button.tg-shadow")
        sh.wait_for()
        assert sh.inner_text() == "vs AWS 41/40 ✓"
        t = sh.get_attribute("title")
        assert "matches or beats AWS on 41 clips" in t and "ids 97% (38)" in t and "Only an admin" in t
        assert not pg.locator("li.tg-row[data-ckey='tg:307'] button.tg-shadow").is_visible()   # (no evidence there)
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        shot(pg, "#settings-radio-controls .tg-group", "channels-shadow-vs-aws")
        sh.click()
        until(pg, lambda: not site.cad.stt_models.shadow_on("FG1"), "shadow off")
        audit = json.dumps(site.cad.settings.audit_log("radios"))
        assert "stt_models/FG1/shadow" in audit
        assert pg.errors == []
    finally:
        browser.close()
        pw.stop()
