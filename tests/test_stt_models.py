"""Per-channel speech models on the GPU workers (#639, sizeup/sttmodels.py): each channel's model by service and by
Settings, the order the workers load them in, the pool sending a clip to a worker that has its model loaded (never
waiting long for one, never starving fire/EMS), the context every clip carries (names only: the radio-ID hints, the
channel's call signs, the aircraft near the field at the clip's time), repetition loops discarded, and what each run
records about its context (a hash and a length, never the content). Invented radio IDs, names, callsigns and texts;
no GPU, no network beyond 127.0.0.1."""

from __future__ import annotations

import json
import time

import pytest

from sizeup import atc, sttmodels, sttpool, sttruns
from sizeup.cloud.sttworkers import WorkerApi, clean_result
from sizeup.stt import LIVE, URGENT
from sizeup.talkers import hint_names
from tests.test_channels import fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_radios import cad
from tests.test_stt_pool import Clock, Jobs, online, pool

QWEN, ATC, Q5 = "qwen3-asr-1.7b-q8_0", "large-v3-turbo-us-atc-v3-f16", "large-v3-turbo-q5_0"


class Meta:
    """hub.sqlite's meta, in memory."""

    def __init__(self):
        self.d = {}

    def get(self, key, default=""):
        return self.d.get(key, default)

    def put(self, key, value):
        self.d[key] = value


def choices():
    return sttmodels.Choices(Meta(), clock=Clock())


# -- the catalog and the choices ---------------------------------------------------------------------------------

def test_the_hub_reads_the_repo_catalog_and_its_defaults_are_in_it():
    cat, digest = sttmodels.load()
    assert len(digest) == 64 and {QWEN, ATC, Q5} <= set(cat)
    assert cat[QWEN]["context"] == "list" and cat[ATC]["context"] == "prompt" and not cat[ATC]["context_default"]
    assert set(sttmodels.RECOMMENDED.values()) <= set(cat) and sttmodels.FALLBACK in cat


def test_todays_model_until_an_admin_chooses_and_a_channels_choice_over_it():
    c = choices()
    # nothing changes the live model by itself (no slowdown, no untested model on fire traffic): today's whisper
    for key, svc in (("tg:307", "fire"), ("p25:mrrs:9211", "police"), ("HPN Tower", "air"), ("tg:999", "transit")):
        assert c.model_for(key, svc) == Q5
    assert c.panel()["recommended"] == {"fire": QWEN, "ems": QWEN, "police": QWEN, "air": ATC}
    assert c.want() == []                                   # (the primary is every worker's own: nothing to load)
    c.set("tg:307", {"model": QWEN})
    assert c.model_for("tg:307", "fire") == QWEN
    with pytest.raises(ValueError, match="model"):
        c.set("tg:307", {"model": "not-in-the-catalog"})
    with pytest.raises(ValueError, match="key"):
        c.set("tg:307; drop", {"model": Q5})
    c.set("tg:307", {"model": None})
    assert c.model_for("tg:307", "fire") == Q5 and c.settings() == {}


def test_context_is_per_model_by_default_and_per_channel_when_set():
    c = choices()
    assert c.context_on("HPN Tower", ATC) is False and c.context_on("HPN Tower", Q5) is True      # (prompt loops)
    assert c.plain("HPN Tower") == [ATC]
    c.set("HPN Tower", {"context": "on"})
    assert c.context_on("HPN Tower", ATC) and c.plain("HPN Tower") == []
    # ... but on an airband channel a whisper model never gets a prompt (#900), its switch on or not: a list model does
    c.is_airband = lambda key: key == "HPN Tower"
    assert not c.context_on("HPN Tower", ATC) and not c.context_on("HPN Tower", Q5) and c.context_on("HPN Tower", QWEN)
    assert set(c.plain("HPN Tower")) == {ATC, Q5, "large-v3-turbo-q8_0"}
    c.set("tg:307", {"context": "off"})
    assert set(c.plain("tg:307")) == set(c.catalog)                   # an A/B: none of the models gets context
    with pytest.raises(ValueError):
        c.set("tg:307", {"context": "sometimes"})


def test_call_signs_are_checked():
    c = choices()
    c.set("p25:mrrs:9211", {"callsigns": "908, Headquarters, 908"})
    assert c.callsigns("p25:mrrs:9211") == ["908", "Headquarters"]
    with pytest.raises(ValueError):
        c.set("p25:mrrs:9211", {"callsigns": ["ok", "<script>"]})
    with pytest.raises(ValueError):
        c.set("p25:mrrs:9211", {"callsigns": [f"C{i}" for i in range(sttmodels.CALLSIGNS_MAX + 1)]})


def test_the_workers_load_fire_first_then_police_then_air():
    c = choices()
    c.set("HPN Tower", {"model": ATC})
    c.set("p25:mrrs:9211", {"model": "large-v3-turbo-q8_0"})
    c.set("tg:307", {"model": QWEN})
    assert c.want({"HPN Tower": "air", "p25:mrrs:9211": "police", "tg:307": "fire"}) == [QWEN, "large-v3-turbo-q8_0",
                                                                                        ATC]


# -- the pool: a worker that has the model first, never a long wait ---------------------------------------------

def test_a_clip_goes_to_the_worker_with_its_model_loaded():
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    a, b = online(p, "gpu-a"), online(p, "gpu-b")
    a.models, b.models = [Q5], [Q5, QWEN]
    assert p.submit(jobs.job("c1", LIVE, model=QWEN))
    assert p.claim("gpu-a", 0) is None                        # b has it and is idle: a leaves it for a moment
    j = p.claim("gpu-b", 0)
    assert j.id == "c1" and j.public()["model"] == QWEN


def test_with_no_worker_free_with_the_model_any_worker_takes_it_at_once_or_after_the_short_wait():
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    a = online(p, "gpu-a")
    a.models = [Q5]
    assert p.submit(jobs.job("c1", LIVE, model=QWEN))
    assert p.claim("gpu-a", 0).id == "c1"                     # nobody has it: no wait at all
    b = online(p, "gpu-b")
    b.models = [QWEN]
    b.leases = {"x", "y"}                                     # b is full
    assert p.submit(jobs.job("c2", LIVE, model=QWEN))
    a.leases.clear()
    assert p.claim("gpu-a", 0).id == "c2"                     # b can't take it now: a does
    b.leases.clear()
    assert p.submit(jobs.job("c3", LIVE, model=QWEN))
    a.leases.clear()
    assert p.claim("gpu-a", 0) is None
    clk.t += sttpool.MODEL_DEFER_S
    assert p.claim("gpu-a", 0).id == "c3"                     # b never claimed it: a takes it after the short wait


def test_fire_traffic_is_never_starved_by_a_model_preference():
    """The classes stay strict: a worker without the fire clip's model takes the next class only while one that has it
    is idle in a claim, and that one takes the fire clip at once."""
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    a, b = online(p, "gpu-a"), online(p, "gpu-b")
    a.models, b.models = [Q5, ATC], [Q5, QWEN]
    assert p.submit(jobs.job("fire", URGENT, model=QWEN))
    assert p.submit(jobs.job("air", LIVE, model=ATC))
    assert p.claim("gpu-b", 0).id == "fire"
    assert p.claim("gpu-a", 0).id == "air"
    assert not jobs.fell


# -- context: names only, the hints, call signs, aircraft at the clip's time ---------------------------------------

def test_hint_names_fire_units_and_dispatch_consoles_in_officers_and_raw_ids_out(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    # an MRRS range of officers' radios (they swap radios: the label says nobody), and a dispatch console's exact ID
    r.save_range({"system": "mrrs", "lo": "9900100", "hi": "9900199", "label": "Rye PD officer"}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900500", "label": "908", "hint": True}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900501", "label": "Headquarters", "hint": True}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900502", "label": "Car 12 officer", "hint": False}, "admin")
    # an MRRS radio's own exact label with no switch set (a role: "officers swap radios"): never, by default
    r.save({"system": "mrrs", "radio_id": "9900503", "label": "Made-up PD officer"}, "admin")
    named, blocked = r.hints("mrrs", "9900503")
    assert named == [] and hint_names("Made-up PD officer", "", "9900503", named, blocked) == []
    assert not r.hint_on(r.get("mrrs", "9900503")) and r.hint_on(r.get("mrrs", "9900500"))
    named, blocked = r.hints("mrrs", "9900150")
    assert named == [] and hint_names("Rye PD officer", "", "9900150", named, blocked) == []      # a range: never
    named, blocked = r.hints("mrrs", "9900500, 9900150")
    assert named == ["908"]
    assert hint_names("908, Rye PD officer", "", "9900500, 9900150", named, blocked) == ["908"]   # a console: in
    assert r.hints("mrrs", "9900501")[0] == ["Headquarters"]
    assert r.hints("mrrs", "9900502")[0] == []                                                     # its switch off
    # a fire unit's radio: in, by its unit (E63) and its alias; the same radio with its switch off: out
    assert hint_names("E63", "E63", "41228") == ["Engine 63"]
    assert hint_names("77A4, 60 Ctrl OP 3", "", "41229,41230") == ["77 Alpha 4", "60 Control"]
    r.save({"system": "p25", "radio_id": "41228", "unit": "E63"}, "admin")
    assert r.hints("p25", "41228") == ([], [])
    r.save({"system": "p25", "radio_id": "41228", "hint": False}, "admin")
    named, blocked = r.hints("p25", "41228")
    assert hint_names("E63", "E63", "41228", named, blocked) == []
    # an FG1 radio with no alias shows its MDC ID: never in a prompt
    assert hint_names("6309", "", "6309") == []
    log = r.log()
    assert any(x["key"] == "p25:41228/hint" and x["new"] == "off" for x in log)


def test_the_context_list_is_ranked_capped_and_once_each():
    got = sttmodels.context_list(["Engine 60", "60 Control"], ["908", "engine 60"], ["Delta 766"],
                                 [f"Term {i}" for i in range(20)])
    assert got[:4] == ["Engine 60", "60 Control", "908", "Delta 766"] and len(got) == sttmodels.CONTEXT_MAX
    assert sttmodels.context_list() == []


def test_aircraft_callsigns_nearest_first_capped():
    tel = {"DAL": "Delta", "N": "november"}
    planes = [{"flight": "DAL766", "phase": "near", "dist": 6.0}, {"flight": "DAL12", "phase": "arriving", "dist": 9.0},
              {"flight": "N123AB", "phase": "low", "dist": 1.0}, {"flight": "DAL766", "phase": "near", "dist": 6.0}]
    got = atc.spoken_callsigns(planes, tel, cap=2)
    assert len(got) == 2 and got[0] == atc.spoken_callsign("DAL12", tel)       # the arrival first, each once


def test_an_old_airband_clip_gets_no_aircraft():
    from sizeup.app import ATC_CONTEXT_AGE_S, App

    class A:
        _atc_planes = App._atc_planes

        def _atc_aircraft(self):
            return [{"flight": "DAL766", "lat": 41.06, "lon": -73.70, "alt_baro": 2000, "dist": 3.0, "phase": "near"}]
    a = A()
    assert a._atc_planes("HPN Tower", time.time() - ATC_CONTEXT_AGE_S - 5) == []


# -- loops, and what a run keeps about its context ----------------------------------------------------------------

@pytest.mark.parametrize("text, seconds, loop", [
    ("Engine 60 Engine 60 Engine 60 Engine 60", 1.0, True),          # (faster than anyone repeats a call)
    ("copy " * 12, 3.0, True),
    ("Thank you. " * 8, 10.0, True),
    ("mayday " * 6, 1.0, False),                                     # a safety word said again and again is real
    ("units responding, units responding, Engine 60 on scene at Main Street", 4.0, False),
    ("Engine 60 to 60 Control, Engine 60 on location, nothing showing", 4.0, False),
    ("ok", 1.0, False),
])
def test_repetition_loops(text, seconds, loop):
    """The hub discards a GPU worker's loop by the one shared rule (sizeup/stt_quality.py), the clip's length known."""
    from sizeup import stt_quality
    assert (stt_quality.assess(text, seconds) == "repeated") is loop
    assert not hasattr(sttmodels, "is_loop")


def test_a_run_keeps_the_contexts_hash_and_length_never_its_content():
    runs = sttruns.Runs()
    # (a context item no timestamp, id or hex hash can hold by chance, unlike "908")
    v, n = sttmodels.context_version("list", "a prompt", ["Engine 60", "Quebec Zulu"])
    assert n == len("Engine 60, Quebec Zulu") and len(v) == 10
    assert sttmodels.context_version("none", "a prompt", ["x"]) == ("", 0)
    runs.add("c1", "gpu-worker", text="Engine 60 responding", status="done", model=QWEN, prompt_v=v, prompt_n=n,
             ctx="list", want=QWEN)
    runs.add("c2", "gpu-worker", text="", status="no_speech", model=Q5, ctx="none", gate="quiet", want=QWEN)
    (r1,) = runs.of("c1")
    (r2,) = runs.of("c2")
    assert (r1["prompt_n"], r1["ctx"], r1["want"], r1["model"]) == (n, "list", QWEN, QWEN)
    assert (r2["gate"], r2["want"], r2["model"]) == ("quiet", QWEN, Q5)
    assert "Quebec Zulu" not in json.dumps([r1, r2])


# -- the worker API ---------------------------------------------------------------------------------------------

class Reg:
    def resolve(self, bearer):
        return "gpu-1"

    def touch(self, wid):
        pass

    def pool_settings(self):
        return {"on": True, "classes": {}, "stations": {}}

    def any(self):
        return True

    def stations_on(self, sid=None):
        return False

    def update_of(self, wid):
        return {"mode": "auto"}


def test_a_worker_hears_the_catalog_and_its_models_are_kept():
    p = pool(Clock())
    api = WorkerApi(Reg(), p, build={"commit": "c" * 40, "sha256": "d" * 64})
    api.stt_models = lambda: {"catalog": {"commit": "c" * 40, "sha256": "e" * 64}, "want": [QWEN, ATC]}
    hdr = {"Authorization": "Bearer sw1.gpu-1.x"}
    body = json.dumps({"state": "online", "model": Q5, "models": {"stt": Q5, "stt_loaded": [QWEN],
                                                                    "stt_state": {ATC: "downloading model"}}}).encode()
    code, out, _ = api.handle("POST", "/api/station/stt/hello", hdr, lambda n: body)
    out = json.loads(out)
    assert code == 200 and out["stt_models"]["want"] == [QWEN, ATC]
    w = p.workers["gpu-1"]
    assert w.models == [Q5, QWEN] and w.stt_state == {ATC: "downloading model"}
    assert p.stats()["workers"][0]["models"] == [Q5, QWEN]


def test_a_result_says_its_gate_and_context_form_and_nothing_else_gets_in():
    r = clean_result({"text": "", "model": QWEN, "gate": "quiet", "context": "list"})
    assert r["gate"] == "quiet" and r["context"] == "list"
    r = clean_result({"text": "x", "gate": "loud<script>", "context": "everything"})
    assert r["gate"] == "" and r["context"] == ""


# -- end to end on a hub ------------------------------------------------------------------------------------------

def test_a_hub_names_the_model_and_context_and_a_loop_goes_to_aws(tmp_path, fake_aws):
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_app import wait
    from tests.test_channels import AWS_TEXT, aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_stt_pool import FakeWorker, pending_event, row, send

    class ModelWorker(FakeWorker):
        def post(self, path, body):
            if path.endswith("/claim"):
                body = dict(body, models=[Q5, QWEN])
            return call(self.url + path, "POST", body, token=self.token)

    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    from tests.test_stt_engine_select import _set
    _set(hub, "FG1", "auto")                              # (Auto · priority: a GPU worker first, #896)
    hub.app.stt_choices.set("FG1", {"callsigns": ["Command"], "model": QWEN})
    hub.app._pool_class = lambda item, key: LIVE          # (an urgent clip's words are never discarded: a live one's)
    token = hub.app.hub.workers.add("gpu-1", "Test box")
    w = ModelWorker(hub.url, token, text=" ".join(["Engine 60"] * 8)).start()      # (a loop: sizeup/stt_quality.py)
    try:
        assert wait(lambda: hub.app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-7"))
        assert wait(lambda: w.jobs, 20)
        job = w.jobs[0]
        assert job["model"] == QWEN and "Command" in job["context"] and job["prompt"].endswith("Command.")
        assert ATC in job["plain"]
        live = hub.app._worker_live("gpu-1")              # Settings > Station > STT workers: what it has loaded
        assert live["models"] == [Q5, QWEN] and live["stt_state"] == {}
        # its text was a loop: discarded, AWS gave the text of record
        assert wait(lambda: row(hub, "FG1-7").get("text") == AWS_TEXT + ".", 30), row(hub, "FG1-7")
        assert wait(lambda: len(hub.app.stt_runs.of("FG1-7")) >= 2, 5)
        runs = {r["role"]: r for r in hub.app.stt_runs.of("FG1-7")}
        assert runs["discarded"]["status"] == "loop" and runs["discarded"]["want"] == QWEN
        assert runs["text"]["engine"] == "aws"
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_context_list_read_back_is_caught():
    ctx = ["Engine 60", "Ladder 31", "60 Control", "Main Street"]
    assert sttmodels.context_echo("Engine 60, Ladder 31, 60 Control, Main Street.", ctx)
    assert not sttmodels.context_echo("Engine 60 responding to Main Street.", ctx)
    assert not sttmodels.context_echo("60 Control, Ladder 31 on scene, nothing showing, investigating.", ctx)
    # a real call-up of two names (the review's: discarded before, AWS's cost or a catch-up try burnt): kept ...
    assert not sttmodels.context_echo("60 Control, Engine 60.", ctx, 2.0)
    assert not sttmodels.context_echo("60 Control, Engine 60.", ctx)
    # ... but the same two names for a 12 s clip are a read-back, and three are whatever the length
    assert sttmodels.context_echo("60 Control, Engine 60.", ctx, 12.0)
    assert sttmodels.context_echo("Engine 60, Ladder 31, 60 Control.", ctx, 3.0)
    assert not sttmodels.context_echo("Engine 60, Ladder 31.", [])


def test_another_systems_talker_text_never_gets_in_only_its_exact_names(tmp_path):
    from types import SimpleNamespace

    from sizeup.app import App
    c = cad(tmp_path)
    r = c.radios
    r.save_range({"system": "mrrs", "lo": "9904000", "hi": "9904999", "label": "4000 series patrol"}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900600", "label": "2398 Sgt", "hint": False}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900601", "label": "908", "hint": True}, "admin")
    r.save({"system": "mrrs", "radio_id": "9900602", "label": "Made-up PD officer"}, "admin")     # (no switch: off)

    class A:
        _talker_hints = App._talker_hints
        _talker_names = App._talker_names
        radios = r
    a = A()
    for unit, rid, want in (("4000 series patrol", "9904123", []), ("2398 Sgt", "9900600", []),
                            ("908", "9900601", ["908"]), ("Medic 5 officer", "9904124", []),
                            ("Made-up PD officer", "9900602", [])):
        item = SimpleNamespace(system="mrrs", unit=unit, radio_id=rid, rig="", talkgroup=9211)
        assert a._talker_names(item) == want, unit


def test_the_hint_switch_alone_is_no_edit_and_blocks_alias_names(tmp_path):
    c = cad(tmp_path)
    r = c.radios
    r.heard("p25", "41290", "E63")
    assert r.resolve("p25", "41290")["unit"] == "E63"
    r.save({"system": "p25", "radio_id": "41290", "hint": False}, "admin")
    assert r.resolve("p25", "41290")["unit"] == "E63" and not r.get("p25", "41290")["edited"]
    r.save({"system": "mdc", "radio_id": "6105", "hint": False}, "admin")
    named, blocked = r.hints("mdc", "6105")
    # an FG1 radio named in the config's MDC aliases ("Engine 61 Portable 1"): its spoken unit blocked too
    assert hint_names("Engine 61 Portable 1", "E61", "6105", named, blocked + ["E61"]) == []


def test_county_talkgroup_keys_are_kept_as_routed():
    c = choices()
    c.set("p25:westchester:307", {"model": Q5})
    assert c.model_for("tg:307", "fire") == Q5 and "p25:westchester:307" not in c.settings()


def test_a_discard_goes_to_the_next_engine_once_on_the_fallback_thread_and_urgent_is_kept():
    p, jobs = pool(Clock()), Jobs()
    j = jobs.job("c1", LIVE)
    p.discard(j, "a repetition loop (discarded)")
    assert p.drain() == 1 and jobs.fell == {"c1": "a repetition loop (discarded)"}
    assert p.fallbacks["discarded"] == 1 and j.extra["fell"] == "discarded"


def test_the_catalog_is_in_the_hub_image():
    from pathlib import Path
    root = Path(sttmodels.__file__).resolve().parent.parent
    assert "COPY deploy/stt-worker/models.json ./deploy/stt-worker/models.json" in (root / "Dockerfile").read_text()
    assert "!deploy/stt-worker/models.json" in (root / ".dockerignore").read_text()
    from tests.stt_worker_module import W
    assert sttmodels.PINS == root / W.CATALOG_PATH      # the file the hub hashes is the one the workers fetch


# -- the workers' local LLM (the hub names it) and champion changes (intel told first) -----------------------------

LLMS = {"qwen3-8b-q4_k_m": {"id": "qwen3-8b-q4_k_m", "what": "8B", "vram_mb": 6200},
        "qwen3-4b-instruct-2507-q4_k_m": {"id": "qwen3-4b-instruct-2507-q4_k_m", "what": "4B", "vram_mb": 3500}}


def test_the_llm_is_todays_8b_until_an_admin_picks_another_and_speech_comes_first():
    c = sttmodels.Choices(Meta(), clock=Clock(), llms=dict(LLMS))
    assert c.llm() == sttmodels.DEFAULT_LLM == "qwen3-8b-q4_k_m" and c.llm_priority() == "yield"
    old, new = c.set_llm("qwen3-4b-instruct-2507-q4_k_m")
    assert old == {"model": "qwen3-8b-q4_k_m", "priority": "yield"} and new["model"] == "qwen3-4b-instruct-2507-q4_k_m"
    assert c.set_llm(priority="keep")[1] == {"model": "qwen3-4b-instruct-2507-q4_k_m", "priority": "keep"}
    assert c.set_llm("")[1]["model"] == "qwen3-8b-q4_k_m"                       # back to the default
    with pytest.raises(ValueError):
        c.set_llm("gpt-something")
    with pytest.raises(ValueError):
        c.set_llm(priority="first")
    # a catalog without llm_models (one from before them): nothing named, the workers keep worker.toml's
    assert sttmodels.Choices(Meta(), clock=Clock(), llms={}).llm() == ""
    assert c.llm_panel()["default"] == "qwen3-8b-q4_k_m" and len(c.llm_panel()["catalog"]) == 2


def test_the_repo_catalogs_llms_are_read_when_there_are_some():
    llms = sttmodels.load_llms()
    assert all(sttmodels.ID.fullmatch(k) and v["id"] == k for k, v in llms.items())


def test_the_hub_tells_the_workers_the_llm_and_its_priority_and_health_shows_its_state(tmp_path):
    from tests.test_cloud import HubRun, call, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        hub.app.stt_choices.llms = dict(LLMS)
        hub.app.stt_api.build = {"commit": "c" * 40}
        word = hub.app._stt_models_word()
        assert word["llm"] == "qwen3-8b-q4_k_m" and word["llm_priority"] == "yield" and "want" in word
        hub.app.stt_api.build = {}
        assert hub.app._stt_models_word() == {"llm_priority": "yield"}     # (a local run: the priority only)
        token = hub.app.hub.workers.add("gpu-1")
        code, out = call(hub.url + "/api/station/stt/hello", "POST", {
            "version": "t", "model": Q5, "state": "online",
            "models": {"stt": Q5, "llm": "", "llm_state": "unavailable: gave its VRAM to the speech models"}},
            token=token)
        assert code == 200 and out["stt_models"]["llm_priority"] == "yield"
        inf = hub.app.infer_lane.stats()
        [w] = inf["workers"]
        assert w["llm_state"] == "unavailable: gave its VRAM to the speech models"
        assert hub.app.pool_health()["llm_setting"]["model"] == "qwen3-8b-q4_k_m"
    finally:
        hub.close()


def test_a_champion_change_tells_intel_first_and_is_kept(tmp_path):
    from tests.test_cloud import HubRun, hub_cfg
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        app.is_push_source = lambda key, kind="": key == "tg:307"            # (a pre-alert channel)
        seen = []
        app.stt_champion_hooks.append(lambda row: seen.append((row, app.stt_choices.model_for(row["key"], "fire"))))
        before = time.time() - 1
        def api_set(key, body):                 # (as the API's channel_model: the hook on a model change only)
            def before(k, o, n):
                if o.get("model") != n.get("model"):
                    app.stt_champion_change(k, o.get("model") or "", n.get("model") or "", "Admin A", "settings")
            return app.stt_choices.set(key, body, before=before)
        api_set("tg:307", {"model": QWEN})
        # the listener ran BEFORE the change applied (the model then still today's), and it is kept for intel
        [(row, at_the_time)] = seen
        assert row["prealert"] and row["new"] == QWEN and at_the_time == Q5
        assert app.stt_choices.model_for("tg:307", "fire") == QWEN
        [kept] = app.stt_champion_log(before)
        assert kept["key"] == "tg:307" and kept["old"] == "" and kept["by"] == "Admin A"
        api_set("tg:999", {"callsigns": ["Base"]})                           # (no model change: nobody told)
        assert len(seen) == 1
    finally:
        hub.close()
