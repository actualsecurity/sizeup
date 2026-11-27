"""The GPU worker's runtimes (deploy/stt-worker/worker.py Runtimes): llama.cpp's llama-server and whisper.cpp's
whisper-server kept at worker.py's pinned builds with no step on the PC. The pins mirror pins.json; downloads are
resumed, checked (size, SHA-256) and unpacked safely beside the running build; the switch happens only while idle,
is tested, and a failure rolls back with a backoff; the hub hears each runtime's build and state. Fakes only: no
llama-server or whisper-server is started, no network."""

from __future__ import annotations

import collections
import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from tests.stt_worker_module import W
from tests.test_stt_worker import FakeHub, FakeWhisper, hub, make_cfg, no_secrets_in_logs, no_smi, whisper  # noqa: F401
from tests.test_stt_worker_models import FakeProc, opener

PINS = json.loads((Path(W.__file__).with_name("pins.json")).read_text())["artifacts"]
Disk = collections.namedtuple("Disk", "total used free")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_zip(files: dict, links: tuple = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


WHISPER_ZIP = make_zip({"Release/whisper-server.exe": b"whisper b6000", "Release/ggml.dll": b"ggml"})
LLAMA_ZIP = make_zip({"llama-server.exe": b"llama b13000", "ggml-cuda.dll": b"cuda kernels"})
CUDART_ZIP = make_zip({"cudart64_12.dll": b"cuda runtime"})
TEST_GGUF = b"GGUF a made-up tiny test model"
TEST_URL = "https://huggingface.co/ggml-org/models-moved/resolve/" + "c" * 40 + "/tinyllamas/test.gguf"


def pin(repo: str, build: str, name: str, data: bytes) -> dict:
    return {"url": f"https://github.com/ggml-org/{repo}/releases/download/{build}/{name}", "bytes": len(data),
            "sha256": sha(data)}


@pytest.fixture
def pins(monkeypatch):
    """Test pins (made-up zips) in place of the real RUNTIMES; {url: the zip's bytes}."""
    rts = {
        "whisper": dict(W.RUNTIMES["whisper"], build="b6000",
                        zips=(pin("whisper.cpp", "b6000", "whisper-test.zip", WHISPER_ZIP),)),
        "llama": dict(W.RUNTIMES["llama"], build="b13000",
                      zips=(pin("llama.cpp", "b13000", "llama-test.zip", LLAMA_ZIP),
                            pin("llama.cpp", "b13000", "cudart-test.zip", CUDART_ZIP))),
    }
    monkeypatch.setattr(W, "RUNTIMES", rts)
    monkeypatch.setattr(W, "RUNTIME_TEST_MODEL", {"url": TEST_URL, "bytes": len(TEST_GGUF), "sha256": sha(TEST_GGUF)})
    monkeypatch.setattr(W, "UPDATE_QUIET_S", 0.0)
    monkeypatch.setattr(W, "RUNTIME_TEST_S", 3.0)
    blobs = {z["url"]: data for rt in rts.values() for z, data in zip(
        rt["zips"], (WHISPER_ZIP,) if rt["exe"].endswith("whisper-server.exe") else (LLAMA_ZIP, CUDART_ZIP))}
    return {**blobs, TEST_URL: TEST_GGUF}


def fake_download(blobs: dict, offsets: list | None = None, calls: list | None = None):
    def download(url, dest, size, sha256, stop):
        if calls is not None:
            calls.append(url)
        W.download_file(url, dest, size, sha256, stop, opener=opener(blobs[url], log=offsets))
    return download


def version_of(argv: list) -> str:
    """llama-server --version: the build of the folder it is in (the installer's: b12359)."""
    p = str(argv[0])
    return "version: 13000 (ea722948)\n" if "b13000" in p else "version: 12359 (b1b770d9)\n"


def install(tmp_path) -> Path:
    """The installer's layout: whisper\\Release\\whisper-server.exe and llama\\llama-server.exe (a hand install: no
    .sizeup-pins)."""
    base = tmp_path / "inst"
    (base / "whisper" / "Release").mkdir(parents=True)
    (base / "whisper" / "Release" / "whisper-server.exe").write_bytes(b"whisper by hand")
    (base / "llama").mkdir()
    (base / "llama" / "llama-server.exe").write_bytes(b"llama b12359")
    return base


def rt_cfg(tmp_path, **kw) -> W.Config:
    base = install(tmp_path)
    cfg = W.Config(base_dir=str(base), state_dir=str(tmp_path / "state"),
                   whisper_server=["whisper/Release/whisper-server.exe", "-m", "m.bin", "--port", "8179"],
                   llm_server=["llama/llama-server.exe", "-m", "q.gguf", "--port", "8180"], llm_url="http://127.0.0.1:8180")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def runtimes(cfg, blobs, **kw) -> W.Runtimes:
    r = W.Runtimes(cfg, run=version_of, download=fake_download(blobs, **kw), windows=True)
    r.disk = lambda p: Disk(10 ** 13, 0, 10 ** 13)
    return r


# --- the pins ------------------------------------------------------------------------------------------------------

def test_runtimes_mirror_pins_json():
    """worker.py's RUNTIMES are pins.json's whisper_zip, llama_zip and llama_cudart_zip (the self-update's hash covers
    them: the builds come with worker.py), each an allowed download, its build the release tag."""
    for rt, p in W.RUNTIMES.items():
        assert len(p["pins"]) == len(p["zips"])
        for key, z in zip(p["pins"], p["zips"]):
            art = PINS[key]
            assert z == {"url": art["url"], "bytes": art["bytes"], "sha256": art["sha256"].lower()}, key
            assert W.model_url_ok(z["url"]), key
            assert f"/releases/download/{p['build']}/" in z["url"], key
            assert W.SHA256.fullmatch(z["sha256"])
    assert W.RUNTIMES["llama"]["build"] == "b12728" and W.RUNTIMES["whisper"]["build"] == "b5454"
    assert set(PINS) >= {k for p in W.RUNTIMES.values() for k in p["pins"]}


# --- download, check, unpack -----------------------------------------------------------------------------------------

def test_download_verify_unpack_beside_the_running_build(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    assert r.build("llama") == "b12359" and r.build("whisper") == ""       # (--version; whisper by hand: unknown)
    assert r.step(stop=lambda: False) == "whisper"                         # both unpacked; whisper first to switch
    base = Path(cfg.base_dir)
    for rt, build, exe in (("whisper", "b6000", "Release/whisper-server.exe"), ("llama", "b13000", "llama-server.exe")):
        folder = base / "runtimes" / rt / build
        assert (folder / exe).is_file()
        mark = json.loads((folder / W.RUNTIME_MARK).read_text())
        assert mark["build"] == build and mark["zips"] == [z["sha256"] for z in W.RUNTIMES[rt]["zips"]]
        assert mark["exe_sha256"] == sha((folder / exe).read_bytes())
        assert not list((base / "runtimes" / rt).glob(".download/*.zip"))   # the zips deleted once unpacked
        assert r.ready(rt, build)
    assert (base / "runtimes" / "llama" / "b13000" / "cudart64_12.dll").is_file()   # both zips in one folder
    # the running builds untouched, and still what runs until the switch
    assert (base / "llama" / "llama-server.exe").read_bytes() == b"llama b12359"
    assert r.program("llama/llama-server.exe") == "llama/llama-server.exe"
    assert r.active("llama") == "" and r.build("llama") == "b12359"


def test_a_partial_download_is_resumed_with_a_range(tmp_path, pins, monkeypatch):
    monkeypatch.setattr(W, "DOWNLOAD_CHUNK", 64)
    cfg = rt_cfg(tmp_path, llm_server=[], llm_url="")                      # whisper only
    z = W.RUNTIMES["whisper"]["zips"][0]
    part = Path(cfg.base_dir) / "runtimes" / "whisper" / ".download" / f"{z['sha256'][:16]}-whisper-test.zip.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(WHISPER_ZIP[:100])                                    # a download a stop left
    offsets: list = []
    r = runtimes(cfg, pins, offsets=offsets)
    assert r.step(stop=lambda: False) == "whisper"
    assert offsets == [100] and r.ready("whisper", "b6000")


def test_no_download_without_the_disk_space(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    calls: list = []
    r = runtimes(cfg, pins, calls=calls)
    r.disk = lambda p: Disk(10 ** 12, 10 ** 12, 10 ** 6)                   # 1 MB free
    assert r.step(stop=lambda: False) == ""
    assert calls == [] and r.status["whisper"].startswith("b6000: no disk space")
    assert "again in 30 min" in r.status["whisper"]
    assert r.report()["llama"]["state"].startswith("b13000: no disk space")
    assert not (Path(cfg.base_dir) / "runtimes" / "llama" / "b13000").exists()


def test_nothing_downloaded_while_gaming_and_a_stopped_download_resumes(tmp_path, pins, monkeypatch):
    monkeypatch.setattr(W, "DOWNLOAD_CHUNK", 64)
    cfg = rt_cfg(tmp_path, llm_server=[], llm_url="")
    calls: list = []
    r = runtimes(cfg, pins, calls=calls)
    assert r.step(stop=lambda: True, held="gaming") == "" and calls == []
    assert r.status["whisper"] == "b6000: waiting (gaming)"
    n = {"chunks": 0}

    def stop():                                                            # gaming begins mid-download
        n["chunks"] += 1
        return n["chunks"] > 2
    assert r.step(stop=stop) == "" and r.status["whisper"] == "b6000: waiting (gaming)"
    part = next((Path(cfg.base_dir) / "runtimes" / "whisper" / ".download").glob("*.part"))
    assert part.stat().st_size == 128                                      # kept for later
    offsets: list = []
    r.download = fake_download(pins, offsets=offsets)
    assert r.step(stop=lambda: False) == "whisper" and offsets == [128]


@pytest.mark.parametrize("files,links,why", [
    ({"../evil.dll": b"x", "llama-server.exe": b"l"}, (), "outside its folder"),
    ({"/abs/evil.dll": b"x", "llama-server.exe": b"l"}, (), "outside its folder"),
    ({"C:/Windows/evil.dll": b"x", "llama-server.exe": b"l"}, (), "outside its folder"),
    ({"a\\..\\..\\evil.dll": b"x", "llama-server.exe": b"l"}, (), "outside its folder"),
    ({"llama-server.exe": b"l"}, ("ggml.dll",), "a link"),
    ({"other.exe": b"l"}, (), "no llama-server.exe"),
])
def test_an_unsafe_zip_is_refused_whole_and_marked_bad(tmp_path, pins, monkeypatch, files, links, why):
    bad = make_zip(files, links)
    rts = dict(W.RUNTIMES)
    rts["llama"] = dict(rts["llama"], zips=(pin("llama.cpp", "b13000", "llama-test.zip", bad),))
    monkeypatch.setattr(W, "RUNTIMES", rts)
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, {**pins, rts["llama"]["zips"][0]["url"]: bad})
    r.step(stop=lambda: False)
    rtdir = Path(cfg.base_dir) / "runtimes" / "llama"
    assert not (rtdir / "b13000").exists() and not list(rtdir.glob(".*.unpacking"))
    assert not (tmp_path / "evil.dll").exists() and not (Path(cfg.base_dir) / "runtimes" / "evil.dll").exists()
    assert r.status["llama"].startswith("bad: ") and why in r.status["llama"]
    assert r.bad("llama", "b13000")


def test_a_wrong_download_is_tried_once_more_then_marked_bad(tmp_path, pins):
    cfg = rt_cfg(tmp_path, llm_server=[], llm_url="")
    url = W.RUNTIMES["whisper"]["zips"][0]["url"]
    r = runtimes(cfg, {url: bytes(len(WHISPER_ZIP))})                       # its size, not its hash
    r.step(stop=lambda: False)
    assert r.status["whisper"] == "b6000: download failed (again in 1 min)" and not r.bad("whisper", "b6000")
    r.wall = lambda: 10 ** 10                                              # (its minute passed)
    r.step(stop=lambda: False)
    assert r.bad("whisper", "b6000").startswith("download: hash mismatch")


# --- which program runs ----------------------------------------------------------------------------------------------

def test_the_installers_paths_run_the_active_build(tmp_path, pins):
    cfg = rt_cfg(tmp_path, whisper_bin="C:/own/whisper-server.exe")
    r = runtimes(cfg, pins)
    assert r.program("llama/llama-server.exe") == "llama/llama-server.exe"  # never updated: as it is
    r.step(stop=lambda: False)
    r.begin("llama")
    base = Path(cfg.base_dir)
    new = str(base / "runtimes" / "llama" / "b13000" / "llama-server.exe")
    assert r.program("llama/llama-server.exe") == new
    assert r.program(str(base / "llama" / "llama-server.exe")) == new      # absolute: the same file
    assert r.program("/usr/local/bin/llama-server") == "/usr/local/bin/llama-server"   # a program of its own
    assert r.program("C:/own/whisper-server.exe") == "C:/own/whisper-server.exe"
    assert r.unmanaged("llama") == "" and r.unmanaged("whisper") == ""
    # a worker.toml with programs elsewhere only: not managed (nothing downloaded)
    own = rt_cfg(tmp_path / "own", whisper_server=["/opt/whisper-server"], llm_server=[], llm_url="")
    assert W.Runtimes(own, run=version_of, windows=True).unmanaged("whisper") == "not managed (own path)"
    assert W.Runtimes(own, run=version_of, windows=True).unmanaged("llama") == "unused"
    assert W.Runtimes(cfg, run=version_of, windows=False).unmanaged("llama") == "not managed (not Windows)"
    cfg.auto_update = False
    assert W.Runtimes(cfg, run=version_of, windows=True).unmanaged("llama") == "off (auto_update)"


def test_the_installers_own_folder_is_known_by_its_pins(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    exe = Path(cfg.base_dir) / "whisper" / "Release" / "whisper-server.exe"
    mark = f"{W.RUNTIMES['whisper']['zips'][0]['sha256'].upper()}|{sha(exe.read_bytes()).upper()}"
    (Path(cfg.base_dir) / "whisper" / W.INSTALLER_MARK).write_text(mark)
    calls: list = []
    r = runtimes(cfg, pins, calls=calls)
    assert r.build("whisper") == "b6000"                                   # installed from these pins: nothing to do
    r.step(stop=lambda: False)
    assert r.status["whisper"] == "ok" and not any("whisper" in c for c in calls)
    exe.write_bytes(b"changed by hand")                                    # the program isn't what was installed
    assert r.build("whisper") == ""


def test_llama_build_is_asked_again_after_a_switch(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    m = W.Models(cfg, run=version_of)
    m.program = r.program
    assert m.llama_build() == 12359
    r.step(stop=lambda: False)
    r.begin("llama")
    assert m.llama_build() == 13000                                        # another file: --version asked again
    r.finish("llama", "a failed test")
    assert m.llama_build() == 12359                                        # rolled back: the old one's again


# --- the switch (the Worker) -----------------------------------------------------------------------------------------

class Procs(list):
    def __call__(self, argv, **kw):
        p = FakeProc(argv)
        self.append(p)
        return p


def worker(tmp_path, hub, whisper, pins, **kw):
    base = install(tmp_path)
    port = whisper.url.split(":")[2].split("/")[0]
    kw = {"whisper_server": ["whisper/Release/whisper-server.exe", "-m", "m.bin", "--port", port],
          "llm_url": "http://127.0.0.1:18180", "llm_model": "test-llm", "llm_model_sha256": "0" * 64,
          "llm_server": ["llama/llama-server.exe", "-m", "q.gguf", "--port", "18180"], **kw}
    cfg = make_cfg(tmp_path, hub, whisper, base_dir=str(base), **kw)
    procs = Procs()
    w = W.Worker(cfg, run=no_smi, popen=procs)
    w.runtimes = W.Runtimes(cfg, run=version_of, download=fake_download(pins), windows=True)
    w.runtimes.disk = lambda p: Disk(10 ** 13, 0, 10 ** 13)
    for proc in (w.proc, w.llm_proc):
        if proc is not None:
            proc.program = w.runtimes.program
    w.stt_models.program = w.runtimes.program
    w.llm_verified = True
    if w.llm is not None:
        w.llm.status = lambda: 200
        w.llm.check = lambda: ({"ok": True}, 5, 1)
    w.probes = []                                                          # (_llama_probe: its own test, below)
    w._llama_probe = lambda exe, end: (w.probes.append(exe), ("", False))[1]
    return w, procs


def test_switch_only_while_idle(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins)
    w.job = {"lease": "x"}
    assert not w.runtimes_step() and w.runtimes.status["whisper"] == "b6000 ready, waiting: a clip"
    w.job, w.llm_job = None, {"lease": "y"}
    assert not w.runtimes_step() and w.runtimes.status["whisper"].endswith("waiting: an LLM job")
    w.llm_job, w._llm_call = None, object()
    assert not w.runtimes_step() and w.runtimes.status["whisper"].endswith("waiting: an LLM job")
    w._llm_call = None
    w.control.set(gaming=True)
    assert not w.runtimes_step() and w.runtimes.status["whisper"] == "b6000 ready, waiting: gaming"
    w.control.set(gaming=False, paused=True)
    assert not w.runtimes_step() and w.runtimes.status["whisper"] == "b6000 ready, waiting: paused"
    assert procs == [] and w.runtimes.active("whisper") == "" and w.runtimes.active("llama") == ""
    w.control.set(paused=False)
    w.draining = True
    assert not w.runtimes_step() and w.runtimes.status["whisper"].endswith("waiting: a self-update")
    w.draining = False
    # an LLM claim in flight when the switch begins: waited for (the drain), never cut
    w._llm_claiming = True
    import threading
    threading.Timer(0.3, lambda: setattr(w, "_llm_claiming", False)).start()
    states = []
    w.whisper.alive = (lambda f: lambda: (states.append(w.state()), f())[1])(w.whisper.alive)
    assert w.runtimes_step()                                               # whisper first
    assert states and states[0] == ("paused", "updating")                  # no jobs meanwhile
    assert w.switching == "" and w.state() == ("online", "")


def test_a_good_switch_restarts_from_the_new_folder_and_prunes(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins)
    base = Path(w.cfg.base_dir)
    assert w.runtimes_step()                                               # whisper
    assert procs[-1].argv[0] == str(base / "runtimes" / "whisper" / "b6000" / "Release" / "whisper-server.exe")
    assert w.whisper_ok and w.runtimes.active("whisper") == "b6000" and w.runtimes.status["whisper"] == "ok"
    assert w.runtimes_step()                                               # then llama
    assert procs[-1].argv[0] == str(base / "runtimes" / "llama" / "b13000" / "llama-server.exe")
    assert w.runtimes.report() == {"whisper": {"build": "b6000", "state": "ok"},
                                   "llama": {"build": "b13000", "state": "ok"}}
    body = w._state_body()
    assert body["runtimes"] == w.runtimes.report() and str(base) not in json.dumps(body)
    assert not w.runtimes_step()                                           # nothing left to do
    doc = json.loads((Path(w.cfg.state_dir) / "runtimes.json").read_text())
    assert doc["llama"]["active"] == "b13000" and doc["llama"]["prev"] == "" and "pending" not in doc["llama"]
    # an older build left in runtimes/ and the one before: pruned to the last two
    for b in ("b12000", "b12500"):
        (base / "runtimes" / "llama" / b).mkdir()
        (base / "runtimes" / "llama" / b / "llama-server.exe").write_bytes(b"x")
    w.runtimes._change("llama", lambda r: r.update(prev="b12500"))
    w.runtimes.prune("llama")
    assert sorted(p.name for p in (base / "runtimes" / "llama").iterdir() if not p.name.startswith(".")) == \
        ["b12500", "b13000"]
    assert (base / "llama" / "llama-server.exe").is_file()                 # the installer's folder: never deleted


def test_a_failed_test_rolls_back_with_a_backoff_then_marks_the_build_bad(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None
    w.runtimes.run = lambda argv: "version: 12359 (b1b770d9)\n"           # the new folder's program: not b13000
    now = [1_000_000.0]
    w.runtimes.wall = lambda: now[0]
    base = Path(w.cfg.base_dir)
    assert not w.runtimes_step()
    assert w.runtimes.active("llama") == ""                                # rolled back to the installer's
    assert procs[-1].argv[0] == str(base / "llama" / "llama-server.exe")  # restarted from it
    assert w.runtimes.status["llama"] == "rolled back: --version says b12359 (again in 30 min)"
    assert (base / "runtimes" / "llama" / "b13000").is_dir()               # kept: tried again later, no download
    n = len(procs)
    assert not w.runtimes_step() and len(procs) == n                       # not before its backoff
    now[0] += 1801
    assert not w.runtimes_step()
    assert w.runtimes.status["llama"].endswith("(again in 60 min)")        # doubled
    now[0] += 3601
    assert not w.runtimes_step()
    assert w.runtimes.status["llama"].startswith("bad: --version says b12359 (3 times)")
    now[0] += 10 ** 4
    n = len(procs)
    assert not w.runtimes_step() and len(procs) == n                       # bad: never flapping
    assert w.runtimes.report()["llama"] == {"build": "b12359", "state": "bad: --version says b12359 (3 times)"}
    now[0] += W.RUNTIME_BAD_S
    w.runtimes.run = version_of
    assert w.runtimes_step() and w.runtimes.active("llama") == "b13000"    # a day later: tried again, and kept


def test_a_failed_llm_answer_rolls_back(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None

    def check():
        if "b13000" in procs[-1].argv[0]:
            raise W.LlmError("llama-server said 500")
        return {"ok": True}, 5, 1
    w.llm.check = check
    assert not w.runtimes_step()
    assert w.runtimes.status["llama"].startswith("rolled back: llama-server: llama-server said 500")
    assert procs[-1].argv[0] == str(Path(w.cfg.base_dir) / "llama" / "llama-server.exe")


def test_gaming_during_the_test_rolls_back_without_counting(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None
    def status():
        w.control.set(gaming=True)                                         # the game starts mid-test
        return 200
    w.llm.status = status
    w.llm.check = lambda: (_ for _ in ()).throw(W.LlmError("out of VRAM"))
    assert not w.runtimes_step()
    doc = json.loads((Path(w.cfg.state_dir) / "runtimes.json").read_text())
    assert doc["llama"]["active"] == "" and doc["llama"]["fails"].get("b13000", 0) == 0
    assert w.runtimes.status["llama"].startswith("rolled back: llama-server: out of VRAM")


def test_a_switch_cut_short_is_rolled_back_at_the_next_start(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    r.step(stop=lambda: False)
    r.begin("llama")                                                       # the worker stopped mid-test
    r2 = runtimes(cfg, pins)
    assert r2.active("llama") == "b13000"
    assert r2.boot() == ["llama"]
    assert r2.active("llama") == "" and r2.status["llama"].startswith("rolled back: interrupted")
    assert json.loads((Path(cfg.state_dir) / "runtimes.json").read_text())["llama"]["fails"] == {"b13000": 0}


def test_the_self_update_waits_for_a_runtime_switch(tmp_path, hub, whisper, pins):
    w, _ = worker(tmp_path, hub, whisper, pins)
    w.connected, w.switching = True, "llama"
    assert w.update_busy() == "a runtime update" and w.state() == ("paused", "updating")


def test_retry_clears_the_builds_marked_bad(tmp_path, pins, capsys):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    r.mark_bad("llama", "b13000", "a test")
    assert r.bad("llama", "b13000")
    assert W._update_cli(cfg, "retry", None) == 0
    assert "build(s) marked bad cleared" in capsys.readouterr().out
    assert not runtimes(cfg, pins).bad("llama", "b13000")


# --- the hub -------------------------------------------------------------------------------------------------------

SAID = {"llama": {"build": "b12359", "state": "downloading b12728"}, "whisper": {"build": "b5454", "state": "ok"}}


def test_the_hub_keeps_the_runtimes_a_worker_says_and_health_has_them(tmp_path):
    from tests import contracts as c
    from tests.test_contracts_workers import gpu_worker, seen
    lv = c.Live(tmp_path)
    try:
        w = gpu_worker(lv, tmp_path)
        w.runtimes.report = lambda: SAID
        w.hello()
        w.heartbeat()
        hello, state = seen(lv, "hello"), seen(lv, "state")     # (each conforming to its schema)
        assert hello[-1][0]["runtimes"] == state[-1][0]["runtimes"] == SAID
        (row,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert row["runtimes"] == SAID
    finally:
        lv.close()


def test_a_worker_from_before_says_none_and_bad_values_are_dropped(tmp_path):
    from sizeup.cloud import sttworkers
    from tests import contracts as c
    lv = c.Live(tmp_path)
    api = "/api/station/stt/"
    try:
        pool = lv.hub.app.sttpool
        code, _ = lv.call("POST", api + "state", {"state": "online", "model": "m", "runtimes": SAID}, token="sw1")
        assert code == 200 and pool.workers[c.WORKER].runtimes == SAID
        code, _ = lv.call("POST", api + "state", {"state": "online", "model": "m"}, token="sw1")   # a rollback
        assert code == 200 and pool.workers[c.WORKER].runtimes == {}
        for bad in ([SAID], "b12728", None, {"llama": "b12728"}):
            code, ans = lv.call("POST", api + "state", {"state": "online", "model": "m", "runtimes": bad}, token="sw1")
            assert code == 200 and ans["ok"] is True and pool.workers[c.WORKER].runtimes == {}
    finally:
        lv.close()
    assert sttworkers.clean_runtimes({"llama": {"build": "12728", "state": " rolled \n back " + "x" * 80},
                                      "other": {"build": "b1", "state": "ok"}}) == \
        {"llama": {"build": "", "state": ("rolled back " + "x" * 80)[:60]}}
    assert sttworkers.clean_runtimes({"whisper": {"build": "b5454"}}) == {"whisper": {"build": "b5454", "state": ""}}
    assert c.errors("stt-worker-state", {"state": "online", "model": "m", "runtimes": {"llama": {"build": "x"}}}) != []


def test_health_shows_the_builds_with_the_state_in_the_hover():
    js = (Path(W.__file__).parents[2] / "sizeup" / "web" / "static" / "health.js").read_text()
    assert '"Version", "Runtimes", "Load"' in js
    assert "${k} ${r[k].build" in js and "${k}: ${st(k)" in js


# --- review fixes: gaming after the drain, errors, untestable switches, the running folder, interruptions ------------

def test_gaming_that_begins_during_the_drain_stops_the_switch(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins)
    w._claiming = True                                                     # a claim in flight: drained first ...
    import threading

    def game():
        w.control.set(gaming=True)                                         # ... and a game starts meanwhile
        w._claiming = False
    threading.Timer(0.3, game).start()
    assert not w.runtimes_step()
    assert procs == [] and w.runtimes.active("whisper") == "" and w.switching == ""
    assert w.runtimes.status["whisper"] == "b6000 ready, waiting: gaming"


def test_an_error_mid_switch_rolls_back_and_counts(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None

    def boom():
        raise RuntimeError("a bug")
    w.llm.check = lambda: boom() if "b13000" in procs[-1].argv[0] else ({"ok": True}, 1, 1)
    assert not w.runtimes_step()
    assert w.runtimes.active("llama") == "" and w.runtimes.status["llama"].startswith("rolled back: error: Runtime")
    doc = json.loads((Path(w.cfg.state_dir) / "runtimes.json").read_text())
    assert doc["llama"]["fails"] == {"b13000": 1} and "pending" not in doc["llama"]


def test_never_switched_untested(tmp_path, hub, whisper, pins):
    """llama.cpp has its own test model: switched and tested whatever the LLM does (the README's llama_bin-only
    config, a 12 GB card whose LLM gave its room, a GGUF whose hash failed). whisper.cpp needs worker.toml's own
    whisper_server: without it, nothing is downloaded."""
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[], llm_server=[], llm_url="",
                      llama_bin="llama/llama-server.exe", whisper_bin="whisper/Release/whisper-server.exe")
    w.proc = None
    assert w.llm_proc is None
    assert w.runtimes_step()                                               # llama: its probe, kept
    assert w.probes == [str(Path(w.cfg.base_dir) / "runtimes" / "llama" / "b13000" / "llama-server.exe")]
    assert w.runtimes.status["whisper"] == "b6000: not downloaded: no whisper_server to test it with"
    assert not (Path(w.cfg.base_dir) / "runtimes" / "whisper").exists()
    w2, _ = worker(tmp_path / "b", hub, whisper, pins, whisper_server=[])
    w2.proc = None
    w2.llm_verified = False                                                # (its GGUF's hash failed)
    assert w2.runtimes_step() and w2.runtimes.active("llama") == "b13000"
    w3, _ = worker(tmp_path / "c", hub, whisper, pins, whisper_server=[])
    w3.proc = None
    w3.stt_models.llm_room_ok = False                                      # (the LLM gave its room)
    assert w3.runtimes_step() and w3.runtimes.active("llama") == "b13000"


def test_the_running_builds_folder_is_never_unpacked_over(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    folder = Path(cfg.base_dir) / "runtimes" / "llama" / "b13000"
    folder.mkdir(parents=True)
    (folder / "llama-server.exe").write_bytes(b"running")                 # its marker unreadable for a moment
    r._change("llama", lambda x: x.update(active="b13000"))
    r.step(stop=lambda: False)
    assert (folder / "llama-server.exe").read_bytes() == b"running"
    assert r.status["llama"].startswith("b13000: unpack failed")


def test_a_build_interrupted_twice_in_a_row_counts(tmp_path, pins):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    r.step(stop=lambda: False)
    r.begin("llama")
    assert runtimes(cfg, pins).boot() == ["llama"]                        # once: the worker stopped, not counted
    r.begin("llama")
    assert runtimes(cfg, pins).boot() == ["llama"]                        # twice in a row: it takes the PC down
    doc = json.loads((Path(cfg.state_dir) / "runtimes.json").read_text())
    assert doc["llama"]["fails"] == {"b13000": 1}


def test_an_llm_job_taken_as_a_switch_begins_is_released(tmp_path, hub, whisper, pins):
    w, _ = worker(tmp_path, hub, whisper, pins)
    released = []
    w._llm_release = lambda lease, why: released.append(why)
    w.switching = "llama"
    w.do_infer({"lease": "a" * 16, "id": "j", "task": "t", "messages": [], "schema": {}})
    assert released == ["paused"] and w.llm_job is None


# --- second review (#871): gaming rollbacks, its own llama test, catalog models, rename, whisper's own process,
# --- the one idle-switch lock ---------------------------------------------------------------------------------------

def test_a_rollback_while_gaming_only_stops_the_servers(tmp_path, hub, whisper, pins):
    """A game that starts mid-test: the build before made active again, its servers only stopped (no ~6 GB loaded
    into the game's VRAM); the monitors start them once the PC is free."""
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None

    def status():
        w.control.set(gaming=True)
        return 503                                                         # (still loading when the game starts)
    w.llm.status = status
    assert not w.runtimes_step()
    assert w.runtimes.active("llama") == "" and w.runtimes.status["llama"].startswith("rolled back: gaming")
    assert len(procs) == 1 and "b13000" in procs[0].argv[0] and procs[0].returncode is not None   # stopped, none new
    assert not w.llm_proc.running()
    doc = json.loads((Path(w.cfg.state_dir) / "runtimes.json").read_text())
    assert doc["llama"]["fails"]["b13000"] == 0                            # not counted
    wv, wprocs = worker(tmp_path / "w", hub, whisper, pins, llm_server=[], llm_url="")
    wv.whisper.alive = lambda: (wv.control.set(gaming=True), False)[1]
    assert not wv.runtimes_step()
    # whisper-server, as always, keeps running through gaming (until gaming_stop_whisper_min): the build before
    assert len(wprocs) == 2 and "b6000" in wprocs[0].argv[0] and wprocs[0].returncode is not None
    assert wprocs[1].argv[0].endswith("whisper/Release/whisper-server.exe") and wv.proc.running()
    wl, lprocs = worker(tmp_path / "wl", hub, whisper, pins, llm_server=[], llm_url="")
    wl._gaming_since = wl.clock() - wl.cfg.gaming_stop_whisper_min * 60 - 1   # gaming long already: it stays off
    wl.whisper.alive = lambda: (wl.control.set(gaming=True), False)[1]
    assert not wl.runtimes_step()
    assert len(lprocs) == 1 and not wl.proc.running()


def test_the_llama_probe_runs_its_test_model_on_its_own_port_and_stops(tmp_path, hub, whisper, pins, monkeypatch):
    cfg = rt_cfg(tmp_path)
    r = runtimes(cfg, pins)
    r.step(stop=lambda: False)
    assert r.test_model_ready()
    procs = Procs()
    w = W.Worker(make_cfg(tmp_path, hub, whisper, base_dir=cfg.base_dir), run=no_smi, popen=procs)
    w.runtimes = r
    monkeypatch.setattr(W, "MODEL_PORT_FIRST", 18291)
    asked = []
    monkeypatch.setattr(W.Llm, "status", lambda self: (asked.append((self.port, bool(self.key))), 200)[1])
    monkeypatch.setattr(W.Llm, "tiny", lambda self: None)
    exe = str(r.exe("llama", "b13000"))
    assert w._llama_probe(exe, w.clock() + 3) == ("", False)
    (p,) = procs
    assert p.argv[:3] == [exe, "-m", str(r.test_model())] and p.argv[p.argv.index("--port") + 1] == "18291"
    assert p.argv[p.argv.index("--host") + 1] == "127.0.0.1" and "--api-key" not in p.argv
    assert asked[0] == (18291, True) and p.returncode is not None         # its own key; stopped after
    monkeypatch.setattr(W.Llm, "tiny", lambda self: (_ for _ in ()).throw(W.LlmError("llama-server said 500")))
    assert w._llama_probe(exe, w.clock() + 3) == ("its test model: llama-server said 500", False)
    r.test_model().unlink()
    assert w._llama_probe(exe, w.clock() + 3) == ("its test model isn't there", False)


def catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, **kw):
    from tests.test_stt_worker_models import two_models
    cat = tmp_path / "models.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    cat.write_text(json.dumps(two_models()))
    kw = {"vram_budget_mb": 20000, **kw}
    w, procs = worker(tmp_path, hub, whisper, pins, catalog_file=str(cat), models_dir=str(tmp_path / "models"), **kw)
    m = w.stt_models
    for e in m.catalog.values():
        for f in e["files"].values():
            m.path_of(f).parent.mkdir(parents=True, exist_ok=True)
            m.path_of(f).write_bytes({sha(b): b for b in (b"asr", b"mm", b"atc")}[f["sha256"]])
    monkeypatch.setattr(W, "MODEL_PORT_FIRST", 18391)
    m.want = ["atc-test"]
    return w, procs


def test_the_runtime_test_decodes_on_one_catalog_model_of_its_kind(tmp_path, hub, whisper, pins, monkeypatch):
    w, procs = catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, llm_server=[], llm_url="")
    tests = []
    monkeypatch.setattr(W.ModelServer, "alive", lambda self: self.proc is not None and self.proc.running())
    monkeypatch.setattr(W.ModelServer, "test", lambda self: tests.append(self.id))
    assert w.runtimes_step()
    probe = [p for p in procs if "-m" in p.argv and "ggml-atc.bin" in p.argv[p.argv.index("-m") + 1]]
    assert tests == ["atc-test"] and len(probe) == 1 and "b6000" in probe[0].argv[0]
    assert probe[0].returncode is not None and "atc-test" not in w.stt_models.servers   # stopped: the plan starts it


def test_a_catalog_model_failing_on_the_new_build_rolls_the_build_back_never_its_hash(tmp_path, hub, whisper, pins,
                                                                                      monkeypatch):
    w, procs = catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, llm_server=[], llm_url="")

    def fail(self):
        raise W.WhisperError("whisper-server said 500")
    monkeypatch.setattr(W.ModelServer, "alive", lambda self: self.proc is not None and self.proc.running())
    monkeypatch.setattr(W.ModelServer, "test", fail)
    assert not w.runtimes_step()                                           # its test: the catalog model fails
    m = w.stt_models
    e = m.catalog["atc-test"]
    assert w.runtimes.active("whisper") == "" and w.runtimes.status["whisper"].startswith("rolled back: atc-test")
    assert not m.bad(e["sha256"]) and not m._fails
    # probation: a build kept, then a model of its kind fails to load on it: the build goes back, not the model
    monkeypatch.setattr(W.ModelServer, "test", lambda self: None)
    w.runtimes._change("whisper", lambda r: r.pop("next", None))
    assert w.runtimes_step() and w.runtimes.on_probation("whisper")
    s = W.ModelServer(e, m.paths(e), 18399, None, 5)
    s.kind = "whisper-ggml"
    m.failed_load(s, exited=False)
    assert not m._fails and m.status["atc-test"] == "failed: its runtime (rolled back)"
    w.runtimes_step()
    assert w.runtimes.active("whisper") == "" and not w.runtimes.on_probation("whisper")
    doc = json.loads((Path(w.cfg.state_dir) / "runtimes.json").read_text())
    assert doc["whisper"]["fails"]["b6000"] == 1 and not m.bad(e["sha256"])   # (a test failure, then a pass: 0;
                                                                           # then its probation failure: 1)
    m.failed_load(s, exited=False)                                         # off probation: the model's own failure
    assert m._fails == {e["sha256"]: 1}


def test_a_refused_rename_is_tried_again_without_unpacking_again(tmp_path, pins, monkeypatch):
    cfg = rt_cfg(tmp_path, llm_server=[], llm_url="")
    r = runtimes(cfg, pins)
    unzips = []
    real = W.unzip_checked
    monkeypatch.setattr(W, "unzip_checked", lambda z, d, *a: (unzips.append(z), real(z, d, *a))[1])
    tries = {"n": 0}

    def rename(src, dst):
        tries["n"] += 1
        if tries["n"] <= 3:
            raise PermissionError("a virus scan holds it")
        W.os.replace(src, dst)
    r.rename, r.sleep = rename, lambda s: None
    assert r.step(stop=lambda: False) == "whisper"                         # three refusals, then the rename
    assert tries["n"] == 4 and len(unzips) == 1
    shutil_dir = Path(cfg.base_dir) / "runtimes" / "whisper" / "b6000"
    import shutil
    shutil.rmtree(shutil_dir)
    monkeypatch.setattr(W, "RUNTIME_RENAME_S", 0.0)
    tries["n"], unzips[:] = -100, []                                       # refused past its time: kept for later
    r.download = fake_download(pins)
    r.step(stop=lambda: False)
    assert r.status["whisper"].startswith("b6000: unpack failed") and len(unzips) == 1
    assert (Path(cfg.base_dir) / "runtimes" / "whisper" / ".b6000.unpacking" / W.RUNTIME_MARK).is_file()
    tries["n"] = 10
    r._change("whisper", lambda x: x.pop("next", None))
    assert r.step(stop=lambda: False) == "whisper" and len(unzips) == 1   # renamed now, never unpacked again


def test_the_whisper_test_needs_its_own_process_on_its_port(tmp_path, hub, whisper, pins, monkeypatch):
    w, procs = worker(tmp_path, hub, whisper, pins, llm_server=[], llm_url="")

    def dead(argv, **kw):
        p = FakeProc(argv)
        p.returncode = 1                                                   # exited at once: another program answers
        procs.append(p)
        return p
    w.proc.popen = dead
    assert not w.runtimes_step()
    assert w.runtimes.status["whisper"].startswith("rolled back: whisper-server exited")
    w.proc.popen = procs
    monkeypatch.setattr(W, "port_owner", lambda port, run=None: 31337)   # another program's pid on its port
    w.runtimes._change("whisper", lambda r: r.pop("next", None))
    assert not w.runtimes_step()
    assert w.runtimes.status["whisper"].startswith("rolled back: another program answers on whisper")


def test_one_idle_switch_lock_for_every_switch(tmp_path, hub, whisper, pins):
    import threading
    w, procs = worker(tmp_path, hub, whisper, pins)
    inside, leave = threading.Event(), threading.Event()

    def hold():
        with w.idle_switch("hub config"):
            inside.set()
            leave.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    assert inside.wait(5) and w.state() == ("paused", "updating")
    assert not w.runtimes_step() and w.runtimes.status["whisper"] == "b6000 ready, waiting: hub config under way"
    swapped = []
    w.updater.swap = lambda: swapped.append(1) or True
    assert not w.drain_and_swap() and swapped == [] and not w.draining   # the self-update too: not now
    with pytest.raises(W.NotIdle, match="hub config under way"):
        with w.idle_switch("the LLM's model", scope="llm"):
            pass
    leave.set()
    t.join(5)
    assert w.state()[1] != "updating" and w.switching == "" and procs == []
    w.job = {"lease": "x"}                                                 # a clip in hand: the LLM's switch goes on
    with w.idle_switch("the LLM's model", scope="llm", drain_s=0.2):
        assert w.llm_switching and not w.switching and w.state()[1] != "updating" and not w.llm_ok
    with pytest.raises(W.NotIdle, match="busy"):
        with w.idle_switch("x", drain_s=0.2):
            pass
    w.job = None
    with pytest.raises(W.NotIdle, match="gaming"):
        w.control.set(gaming=True)
        with w.idle_switch("x", drain_s=0.2):
            pass


def test_a_failing_rollback_restart_releases_the_lock(tmp_path, hub, whisper, pins):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None
    w.runtimes.run = lambda argv: "version: 12359 (b1b770d9)\n"           # its test fails ...
    calls = {"n": 0}
    real = w._restart_runtime

    def restart(rt, start=True):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("the build before won't start")                  # ... and so does the rollback's restart
        return real(rt, start)
    w._restart_runtime = restart
    assert not w.runtimes_step()
    assert w.runtimes.active("llama") == "" and w.switching == "" and w._switch_mx.acquire(blocking=False)
    w._switch_mx.release()


def three_whisper_models():
    from tests.test_stt_worker_models import entry, two_models, HF
    cat = two_models()
    cat["stt_models"]["atc-two"] = entry(files={"model": {"file": "ggml-atc2.bin", "url": f"{HF}/ggml-atc2.bin",
                                                          "bytes": 4, "sha256": sha(b"atc2")}}, vram_mb=2300)
    return cat


def wide_worker(tmp_path, hub, whisper, pins, monkeypatch, **kw):
    w, procs = catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, **kw)
    m = w.stt_models
    cat = tmp_path / "models.json"
    cat.write_text(json.dumps(three_whisper_models()))
    m._load_local(str(cat))
    f = m.catalog["atc-two"]["files"]["model"]
    m.path_of(f).write_bytes(b"atc2")
    m.want = ["atc-test", "atc-two"]
    return w, procs


def test_every_wanted_model_of_its_kind_is_probed(tmp_path, hub, whisper, pins, monkeypatch):
    w, procs = wide_worker(tmp_path, hub, whisper, pins, monkeypatch, llm_server=[], llm_url="")
    tested = []

    def test(self):
        tested.append(self.id)
        if self.id == "atc-two":
            raise W.WhisperError("whisper-server said 500")
    monkeypatch.setattr(W.ModelServer, "alive", lambda self: self.proc is not None and self.proc.running())
    monkeypatch.setattr(W.ModelServer, "test", test)
    assert not w.runtimes_step()
    assert "atc-test" in tested and "atc-two" in tested                    # not the first one only
    assert w.runtimes.status["whisper"].startswith("rolled back: atc-two")


def test_a_probe_out_of_vram_or_without_room_leaves_the_build_untested_never_a_loop(tmp_path, hub, whisper, pins,
                                                                                    monkeypatch):
    """Out of VRAM isn't the build's fault: never a rollback (and so never a switch every 30 min). A model that
    doesn't fit beside what is loaded isn't probed at all (the LLM is never stopped for it): its probation covers it."""
    w, procs = catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, llm_server=[], llm_url="")

    def oom(argv, **kw):
        p = FakeProc(argv)
        if "-m" in argv and "ggml-atc.bin" in argv[argv.index("-m") + 1]:
            p.returncode = 1                                               # the probe's server: out of VRAM
            Path(w.cfg.state_dir, "model-atc-test.log").write_text("cudaMalloc failed: out of memory\n")
        procs.append(p)
        return p
    w.stt_models.popen = oom
    assert w.runtimes_step()                                               # kept: untested, on probation
    assert w.runtimes.active("whisper") == "b6000" and w.runtimes.on_probation("whisper")
    w2, procs2 = catalog_worker(tmp_path / "small", hub, whisper, pins, monkeypatch, llm_server=[], llm_url="",
                                vram_budget_mb=2000)                       # 1000 (primary) + 2300 > 2000
    assert w2.runtimes_step() and w2.runtimes.active("whisper") == "b6000"
    assert not [p for p in procs2 if "ggml-atc.bin" in " ".join(p.argv)]   # never started


def test_probation_failures_add_up_across_passing_tests(tmp_path, hub, whisper, pins, monkeypatch):
    """A model that fails only after the switch (probation): each revert counts, a passing test between them doesn't
    reset it: 30, then 60 min, then bad. No endless switch, revert, switch."""
    w, procs = catalog_worker(tmp_path, hub, whisper, pins, monkeypatch, llm_server=[], llm_url="")
    monkeypatch.setattr(W.ModelServer, "alive", lambda self: self.proc is not None and self.proc.running())
    monkeypatch.setattr(W.ModelServer, "test", lambda self: None)
    m = w.stt_models
    e = m.catalog["atc-test"]
    s = W.ModelServer(e, m.paths(e), 18399, None, 5)
    now = [2_000_000.0]
    w.runtimes.wall = lambda: now[0]
    notes = []
    for n in range(1, W.RUNTIME_BAD_TRIES + 1):
        assert w.runtimes_step(), n                                        # its test passes ...
        m.failed_load(s, exited=False)                                     # ... then a model of its kind fails
        w.runtimes_step()
        notes.append(w.runtimes.status["whisper"])
        now[0] += 7 * 3600
    assert notes[0].endswith("(again in 30 min)") and notes[1].endswith("(again in 60 min)")
    assert notes[2].startswith("bad: ") and w.runtimes.bad("whisper", "b6000")
    assert not m.bad(e["sha256"]) and not m._fails


def test_a_probation_passed_whole_forgets_its_failures(tmp_path, pins):
    cfg = rt_cfg(tmp_path, llm_server=[], llm_url="")
    clock = [100.0]
    r = W.Runtimes(cfg, run=version_of, download=fake_download(pins), windows=True, clock=lambda: clock[0])
    r.disk = lambda p: Disk(10 ** 13, 0, 10 ** 13)
    r.step(stop=lambda: False)
    r.begin("whisper")
    r.finish("whisper", "")
    r.revert("whisper", "a model failed")
    r._change("whisper", lambda x: x.pop("next", None))
    r.begin("whisper")
    r.finish("whisper", "")
    assert r._rec("whisper")["fails"] == {"b6000": 1}                     # kept through the passing test
    clock[0] += W.RUNTIME_PROBATION_S + 1
    r.step(stop=lambda: False)
    rec = r._rec("whisper")
    assert rec["fails"] == {} and rec["probation_failed"] == []


def test_the_llm_test_checks_its_own_process_on_its_port(tmp_path, hub, whisper, pins, monkeypatch):
    w, procs = worker(tmp_path, hub, whisper, pins, whisper_server=[])
    w.proc = None
    monkeypatch.setattr(W, "port_owner", lambda port, run=None: 31337 if port == w.llm.port else 0)
    assert not w.runtimes_step()
    assert w.runtimes.status["llama"].startswith("rolled back: another program answers on llama")
