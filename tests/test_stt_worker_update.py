"""The GPU STT worker's self-update (tools/stt_worker/worker.py Updater) and the hub's side of it
(sizeup/cloud/sttworkers.py worker_build, WorkerApi.wanted, the per-worker setting). GitHub is a fake that answers
with the shapes of its REST API's compare endpoint (docs.github.com, "Compare two commits": status ahead / behind /
identical / diverged, merge_base_commit, ahead_by, behind_by, commits, files) and of raw.githubusercontent.com (the
file's bytes; 404 "404: Not Found"). No network: every fetch goes to the fake, and https_get itself is checked against
a fake connection. Commits and hashes are made up; the worker.py bytes are this repo's."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import tools.stt_worker.worker as W
from tests.test_stt_worker import FakeSmi

REPO_WORKER = Path(W.__file__)
NEW = REPO_WORKER.read_bytes()                        # the version the hub names: this repo's worker.py
NEW_SHA = hashlib.sha256(NEW).hexdigest()
OLD = NEW.replace(b'VERSION = "', b'VERSION = "old-', 1)   # the one installed (another hash)
OLD_SHA = hashlib.sha256(OLD).hexdigest()
COMMIT = "3b7e1f0c9a2d4e6b8c0a1f3e5d7b9c2a4e6f8a0b"
OTHER = "9d4c2b1a0f8e7d6c5b4a39281706f5e4d3c2b1a0"
TOKEN = "sw1.gpu-1." + "Q" * 43


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def compare_answer(status: str, base: str, head: str) -> bytes:
    """GitHub's compare main...<head>, trimmed to the keys it always has (per_page=1: at most one commit listed)."""
    ahead = 0 if status in ("behind", "identical") else 2
    behind = 3 if status in ("behind", "diverged") else 0
    commit = lambda sha: {"sha": sha, "node_id": "C_kwDOLmadeup", "commit": {"message": "made up", "tree": {"sha": "0" * 40}},
                          "url": f"https://api.github.com/repos/actualsecurity/sizeup/commits/{sha}", "parents": []}
    return json.dumps({
        "url": f"https://api.github.com/repos/actualsecurity/sizeup/compare/main...{head}",
        "html_url": f"https://github.com/actualsecurity/sizeup/compare/main...{head}",
        "base_commit": commit("f" * 40), "merge_base_commit": commit(base), "status": status, "ahead_by": ahead,
        "behind_by": behind, "total_commits": ahead, "commits": [commit(head)] if ahead else [], "files": []}).encode()


class FakeGitHub:
    """api.github.com's compare and raw.githubusercontent.com's files, for actualsecurity/sizeup only. compare:
    {commit: (status, merge base)}; files: {commit: bytes}. api_status / raw_status with headers: an error answer."""

    def __init__(self):
        self.compare = {COMMIT: ("behind", COMMIT)}
        self.files = {COMMIT: NEW}
        self.calls: list[tuple[str, str]] = []
        self.api_status, self.raw_status, self.headers = 200, 200, {}

    def __call__(self, host: str, path: str, max_bytes: int):
        self.calls.append((host, path))
        if host == W.GITHUB_API:
            m = re.fullmatch(r"/repos/actualsecurity/sizeup/compare/main\.\.\.([0-9a-f]{40})\?per_page=1", path)
            assert m, path
            if self.api_status != 200:
                return self.api_status, self.headers, b'{"message":"API rate limit exceeded"}'
            if m.group(1) not in self.compare:
                return 404, {}, b'{"message":"Not Found","documentation_url":"https://docs.github.com/rest"}'
            st, base = self.compare[m.group(1)]
            return 200, {"content-type": "application/json; charset=utf-8"}, compare_answer(st, base, m.group(1))
        assert host == W.GITHUB_RAW, host
        m = re.fullmatch(r"/actualsecurity/sizeup/([0-9a-f]{40})/tools/stt_worker/worker\.py", path)
        assert m, path
        if self.raw_status != 200:
            return self.raw_status, self.headers, b"503 Service Unavailable"
        data = self.files.get(m.group(1))
        if data is None:
            return 404, {}, b"404: Not Found"
        return 200, {"content-type": "text/plain; charset=utf-8"}, data[:max_bytes + 1]


def install(tmp_path, data: bytes = OLD) -> Path:
    d = tmp_path / "sizeup-stt"
    d.mkdir(parents=True, exist_ok=True)
    cur = d / "worker.py"
    cur.write_bytes(data)
    if os.name != "nt":
        os.chmod(cur, 0o750)
    return cur


def cfg_for(tmp_path, **kw) -> W.Config:
    return W.Config(hub_url="https://hub.example.org", token=TOKEN, state_dir=str(tmp_path / "state"), log_file="-", **kw)


def offer(mode="auto", commit=COMMIT, sha=NEW_SHA, url=None) -> dict:
    wu = {"mode": mode}
    if commit:
        wu.update(commit=commit, sha256=sha, url=url or f"https://raw.githubusercontent.com/actualsecurity/sizeup/{commit}"
                                                         "/tools/stt_worker/worker.py")
    return {"ok": True, "pool_on": True, "worker_update": wu}


def updater(tmp_path, gh=None, clock=None, selftest=None, **kw):
    cur = install(tmp_path)
    up = W.Updater(cfg_for(tmp_path, **kw), path=cur, fetch=gh or FakeGitHub(), clock=clock or Clock(),
                   selftest=selftest or (lambda new, sha: ""))
    return up, cur


IDLE = lambda: ""                                       # noqa: E731


# --- the hub: the version it names ----------------------------------------------------------------------------------

def test_the_hub_names_its_own_build_and_the_hash_of_the_worker_py_in_its_image(tmp_path):
    from sizeup.cloud import sttworkers as S
    assert S.WORKER_FILE == REPO_WORKER.resolve()                     # (/app/tools/stt_worker/worker.py in the image)
    f = tmp_path / "worker.py"
    f.write_bytes(b"print('made up')\n")
    v = S.worker_build(COMMIT.upper() + "\n", f)
    assert v == {"commit": COMMIT, "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                 "url": f"https://raw.githubusercontent.com/actualsecurity/sizeup/{COMMIT}/tools/stt_worker/worker.py"}
    assert S.worker_build("", f) == {} and S.worker_build("main", f) == {} and S.worker_build(COMMIT[:12], f) == {}
    assert S.worker_build(COMMIT, tmp_path / "missing.py") == {}       # no worker.py in the image: nothing named
    docker = (REPO_WORKER.parents[2] / "Dockerfile").read_text()
    assert "COPY tools/stt_worker/worker.py ./tools/stt_worker/worker.py" in docker
    assert "!tools/stt_worker/worker.py" in (REPO_WORKER.parents[2] / ".dockerignore").read_text()


def test_the_hub_tells_a_worker_that_says_its_version_and_an_admin_holds_or_pins_it(tmp_path, monkeypatch):
    from sizeup.cloud.sttworkers import WorkerError
    from tests import contracts as c
    monkeypatch.setenv("SIZEUP_BUILD", COMMIT)
    lv = c.Live(tmp_path)
    try:
        api, reg = lv.hub.app.stt_api, lv.hub.app.hub.workers
        mine = {"sha256": OLD_SHA, "commit": "", "auto": True, "state": "current", "bad": []}
        code, ans = lv.call("POST", "/api/station/stt/hello", {"state": "paused", "model": "m", "version": "2",
                                                              "self_update": mine}, token="sw1")
        assert code == 200 and ans["worker_update"] == {
            "mode": "auto", "commit": COMMIT, "sha256": NEW_SHA,
            "url": f"https://raw.githubusercontent.com/actualsecurity/sizeup/{COMMIT}/tools/stt_worker/worker.py"}
        # a worker from before the self-update (no self_update): never told
        code, ans = lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m"}, token="sw1")
        assert code == 200 and "worker_update" not in ans
        # Health and Settings: what it runs, against what the hub wants
        lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m", "self_update": mine}, token="sw1")
        (w,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert w["update"]["sha256"] == OLD_SHA and w["update_wanted"] == NEW_SHA and w["update_current"] is False
        assert lv.hub.app._worker_live(c.WORKER)["update_current"] is False
        # hold, pin, auto (audited); a pin must be a full commit and a SHA-256
        notes = []
        note = lambda k, o, n: notes.append((k, o, n))                  # noqa: E731
        reg.act({"action": "update", "id": c.WORKER, "mode": "hold"}, "casey", note)
        code, ans = lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m", "self_update": mine},
                            token="sw1")
        assert ans["worker_update"] == {"mode": "hold"}
        reg.act({"action": "update", "id": c.WORKER, "mode": "pin", "commit": OTHER, "sha256": OLD_SHA.upper()}, "casey",
                note)
        assert api.wanted(c.WORKER) == {"mode": "pin", "commit": OTHER, "sha256": OLD_SHA, "url":
                                        f"https://raw.githubusercontent.com/actualsecurity/sizeup/{OTHER}/tools/stt_worker/worker.py"}
        (w,) = [x for x in lv.hub.app.pool_health()["workers"] if x["id"] == c.WORKER]
        assert w["update_current"] is True
        assert reg.panel()["workers"][0]["update_mode"] == {"mode": "pin", "commit": OTHER, "sha256": OLD_SHA}
        for bad in ({"mode": "pin", "commit": OTHER[:12], "sha256": OLD_SHA}, {"mode": "pin", "commit": OTHER},
                    {"mode": "latest"}):
            with pytest.raises(WorkerError):
                reg.act({"action": "update", "id": c.WORKER, **bad}, "casey", note)
        reg.act({"action": "update", "id": c.WORKER, "mode": "auto"}, "casey", note)
        assert api.wanted(c.WORKER)["mode"] == "auto" and reg.update_settings() == {}
        assert [n[2] for n in notes] == ["hold", f"pin {OTHER[:12]} ({OLD_SHA[:12]})", "auto"]
        # a station's whisper is never told, whatever it sends
        lv.hub.app.hub.workers.set_pool(stations={c.STATION: True})
        code, ans = lv.call("POST", "/api/station/stt/state", {"state": "paused", "model": "m", "self_update": mine},
                            token="sz1")
        assert code == 200 and "worker_update" not in ans
    finally:
        lv.close()


def test_the_worker_hears_the_hubs_version_from_the_real_hub(tmp_path, monkeypatch):
    """The worker client against a real hub: it says its version (self_update) and takes the hub's word."""
    from tests import contracts as c
    monkeypatch.setenv("SIZEUP_BUILD", COMMIT)
    lv = c.Live(tmp_path)
    try:
        cur = install(tmp_path)
        cfg = W.Config(hub_url=lv.url, token=lv.worker, whisper_url="http://127.0.0.1:9", state_dir=str(tmp_path / "w"),
                       log_file="-")
        up = W.Updater(cfg, path=cur, fetch=FakeGitHub(), selftest=lambda n, s: "")
        w = W.Worker(cfg, run=FakeSmi(), updater=up)
        w.hello()
        assert up.offered == ("auto", COMMIT, NEW_SHA)
        w.heartbeat()
        assert lv.hub.app.sttpool.workers[c.WORKER].update["sha256"] == OLD_SHA
    finally:
        lv.close()


# --- the worker: when it updates ------------------------------------------------------------------------------------

def test_only_a_different_hash_with_auto_update_on_starts_an_update(tmp_path):
    gh = FakeGitHub()
    up, cur = updater(tmp_path, gh)
    assert up.step(IDLE) is False and up.status == "waiting for the hub" and not gh.calls
    up.offer({"ok": True})                                           # an older hub: no word
    assert up.offered is None
    up.offer(offer(sha=OLD_SHA))                                     # the hash it runs: nothing to do
    assert up.step(IDLE) is False and up.status == "current" and not gh.calls
    up.offer(offer(mode="hold", commit=""))
    assert up.step(IDLE) is False and up.status == "held by the hub" and not gh.calls
    up.offer(offer(mode="auto", commit=""))                          # a hub that can't tell its build
    assert up.step(IDLE) is False and up.status == "the hub names no version" and not gh.calls
    up.offer(offer(mode="something-new"))                            # a word it doesn't know: hold
    assert up.offered == ("hold", "", "")
    up.offer(offer(sha=NEW_SHA.upper()))                             # not a hash it takes
    assert up.offered == ("none", "", "") and up.step(IDLE) is False and not gh.calls

    off, _ = updater(tmp_path / "off", gh, auto_update=False)
    off.offer(offer())
    assert off.step(IDLE) is False and off.status == "off (auto_update)" and not gh.calls
    assert off.report() == {"sha256": OLD_SHA, "commit": "", "auto": False, "state": "off (auto_update)", "bad": []}

    up.offer(offer())
    assert up.step(IDLE) is True and up.status == f"updating to {NEW_SHA[:12]}"
    assert gh.calls == [(W.GITHUB_API, f"/repos/actualsecurity/sizeup/compare/main...{COMMIT}?per_page=1"),
                        (W.GITHUB_RAW, f"/actualsecurity/sizeup/{COMMIT}/tools/stt_worker/worker.py")]
    assert up.new_path.read_bytes() == NEW and cur.read_bytes() == OLD
    if os.name != "nt":
        assert up.new_path.stat().st_mode & 0o777 == 0o750


def test_auto_update_is_a_config_key_on_by_default(tmp_path):
    p = tmp_path / "worker.json"
    p.write_text(json.dumps({"auto_update": False}))
    assert W.load_config(p, env={}).auto_update is False
    assert W.Config().auto_update is True


@pytest.mark.parametrize("compare, why", [
    (("diverged", "e" * 40), "diverged"),         # a fork's commit (GitHub serves it under the parent's name too)
    (("ahead", "f" * 40), "ahead"),               # a branch not merged into main
    (("behind", "e" * 40), "behind"),             # behind, but not itself the merge base: not taken
    (None, "not in actualsecurity/sizeup (404)"),  # not in the repo at all
])
def test_a_commit_not_on_main_is_refused_and_never_downloaded(tmp_path, compare, why, caplog):
    gh = FakeGitHub()
    if compare is None:
        gh.compare.clear()
    else:
        gh.compare[COMMIT] = compare
    clock = Clock()
    up, cur = updater(tmp_path, gh, clock)
    up.offer(offer())
    with caplog.at_level(logging.ERROR, logger="sizeup.stt_worker"):
        assert up.step(IDLE) is False
    assert why in up.status and up.status.startswith(f"refused {NEW_SHA[:12]}")
    assert [h for h, _ in gh.calls] == [W.GITHUB_API] and not up.new_path.exists() and cur.read_bytes() == OLD
    clock.t += 10 * W.UPDATE_CHECK_S                                 # refused: not asked again by this process
    assert up.step(IDLE) is False and len(gh.calls) == 1
    assert len([r for r in caplog.records if "refused" in r.getMessage()]) == 1     # said once


def test_the_hubs_url_and_hosts_are_never_used(tmp_path):
    """The hub names a commit and a hash; where to get it is the worker's own: GitHub, main of actualsecurity/sizeup."""
    gh = FakeGitHub()
    up, _ = updater(tmp_path, gh)
    up.offer(offer(url="https://evil.example.net/actualsecurity/sizeup/worker.py"))
    assert up.step(IDLE) is True
    assert {h for h, _ in gh.calls} == {W.GITHUB_API, W.GITHUB_RAW} and all("evil" not in p for _, p in gh.calls)
    assert (W.UPDATE_REPO, W.UPDATE_BRANCH, W.UPDATE_FILE) == ("actualsecurity/sizeup", "main",
                                                              "tools/stt_worker/worker.py")
    src = REPO_WORKER.read_text()
    assert 'wu.get("url")' not in src and "wu['url']" not in src


def test_https_get_talks_only_to_github_and_sends_no_token(monkeypatch):
    sent = []

    class Conn:
        def __init__(self, host, port, timeout, context):
            sent.append((host, port, context.verify_mode))

        def request(self, method, path, headers):
            sent.append((method, path, headers))

        def getresponse(self):
            class R:
                status = 200

                def getheaders(self):
                    return [("X-RateLimit-Remaining", "59")]

                def read(self, n):
                    return b"x" * min(n, 10)
            return R()

        def close(self):
            pass
    monkeypatch.setattr(W.http.client, "HTTPSConnection", Conn)
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)
    assert W.https_get(W.GITHUB_RAW, "/actualsecurity/sizeup/x", 5) == (200, {"x-ratelimit-remaining": "59"}, b"xxxxxx")
    import ssl
    assert sent[0] == (W.GITHUB_RAW, 443, ssl.CERT_REQUIRED)
    headers = sent[1][2]
    assert set(headers) == {"User-Agent", "Accept"} and TOKEN not in json.dumps(sent)
    with pytest.raises(W.UpdateError) as e:
        W.https_get("hub.example.org", "/x", 5)
    assert e.value.final and len(sent) == 2


def test_a_file_whose_hash_isnt_the_hubs_is_refused(tmp_path):
    gh = FakeGitHub()
    gh.files[COMMIT] = NEW + b"\n# something else\n"
    up, cur = updater(tmp_path, gh)
    up.offer(offer())
    assert up.step(IDLE) is False and "hash mismatch" in up.status
    assert not up.new_path.exists() and cur.read_bytes() == OLD


def test_a_file_over_the_size_cap_is_refused(tmp_path):
    gh = FakeGitHub()
    big = NEW + b"#" * W.UPDATE_MAX_BYTES
    gh.files[COMMIT] = big
    up, cur = updater(tmp_path, gh)
    up.offer(offer(sha=hashlib.sha256(big).hexdigest()))
    assert up.step(IDLE) is False and "larger than 2 MB" in up.status
    assert not up.new_path.exists() and cur.read_bytes() == OLD


def test_a_failed_self_check_means_no_swap(tmp_path):
    up, cur = updater(tmp_path, selftest=lambda new, sha: "exit 2")
    up.offer(offer())
    assert up.step(IDLE) is False and "self-check failed: exit 2" in up.status
    assert not up.new_path.exists() and cur.read_bytes() == OLD and not up.prev_path.exists()


def test_the_real_self_check_of_a_new_file(tmp_path, monkeypatch):
    """`python worker.py.new selftest`: this repo's worker.py passes with a config it takes; a file that doesn't import,
    a config it refuses, or a hash that isn't the file's do not."""
    d = tmp_path / "sizeup-stt"
    d.mkdir()
    conf = d / "worker.json"
    conf.write_text(json.dumps({"hub_url": "https://hub.example.org", "state_dir": str(tmp_path / "state"),
                                "log_file": "-"}))
    new = d / "worker.py.new"
    new.write_bytes(NEW)
    for k in [k for k in os.environ if k.startswith("SIZEUP_STT_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SIZEUP_STT_WORKER_TOKEN", TOKEN)         # (the selftest gets the worker's own environment)
    up = W.Updater(W.load_config(conf), path=d / "worker.py", config_path=conf)
    assert up.run_selftest(new, NEW_SHA) == ""
    assert up.run_selftest(new, OLD_SHA) == "it didn't report ok for this file"
    conf.write_text(json.dumps({"hub_url": "http://hub.example.org"}))             # a config it refuses: exit 2
    assert up.run_selftest(new, NEW_SHA).startswith("exit 2: config: hub_url: https only")
    new.write_bytes(b"def broken(:\n")
    assert up.run_selftest(new, hashlib.sha256(b"def broken(:\n").hexdigest()).startswith("exit 1")
    assert not list(d.glob(".selftest*")) and not (tmp_path / "state").exists()   # (it writes nothing of the worker's)


# --- the worker: when it swaps, and back ----------------------------------------------------------------------------

def make_worker(tmp_path, up: W.Updater) -> W.Worker:
    w = W.Worker(up.cfg, run=FakeSmi(), updater=up)
    w.connected = True
    w._gaming_seen = -1e9
    up.started = -1e9
    return w


def test_the_swap_waits_for_idle_then_renames_and_exits_75(tmp_path):
    up, cur = updater(tmp_path, clock=None)
    up.clock = __import__("time").monotonic
    w = make_worker(tmp_path, up)
    up.offer(offer())
    w.job = {"lease": "a" * 16, "fast": True}
    assert up.step(w.update_busy) is False and up.status == f"staged {NEW_SHA[:12]}, waiting: a clip"
    w.job = None
    w.llm_job = {"lease": "b" * 16}
    assert up.step(w.update_busy) is False and up.status.endswith("waiting: an LLM job")
    w.llm_job = None
    w.control.set(gaming=True)
    assert up.step(w.update_busy) is False and up.status.endswith("waiting: gaming")
    w.control.set(gaming=False)
    w._gaming_seen = w.clock() - 30                                   # gaming ended half a minute ago
    assert up.step(w.update_busy) is False and up.status.endswith("waiting: gaming just ended")
    w._gaming_seen = -1e9
    w.connected = False
    assert up.step(w.update_busy) is False and up.status.endswith("waiting: the hub")
    w.connected = True
    assert cur.read_bytes() == OLD and up.new_path.exists()
    assert up.step(w.update_busy) is True
    assert w.drain_and_swap() is True
    assert w.draining and w.state() == ("paused", "updating") and w.exit_code == W.UPDATE_EXIT and w.stopped
    assert cur.read_bytes() == NEW and up.prev_path.read_bytes() == OLD and not up.new_path.exists()
    if os.name != "nt":
        assert cur.stat().st_mode & 0o777 == 0o750 and up.prev_path.stat().st_mode & 0o777 == 0o750
    pending = json.loads(up.state_path.read_text())["pending"]
    assert {k: pending[k] for k in ("from", "to", "commit", "starts")} == {"from": OLD_SHA, "to": NEW_SHA, "commit": COMMIT,
                                                                         "starts": 0}


def test_a_swap_that_cant_write_its_record_doesnt_swap(tmp_path, monkeypatch):
    up, cur = updater(tmp_path)
    up.offer(offer())
    assert up.step(IDLE) is True

    def no(*a, **k):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(W, "write_json", no)
    assert up.swap() is False and cur.read_bytes() == OLD and "can't swap" in up.status


def swapped(tmp_path, clock=None) -> tuple[W.Updater, Path]:
    """worker.py swapped to NEW by the old updater; the new file's updater (what its next start runs)."""
    up, cur = updater(tmp_path)
    up.offer(offer())
    assert up.step(IDLE) is True and up.swap() is True
    return W.Updater(up.cfg, path=cur, fetch=FakeGitHub(), clock=clock or Clock(), selftest=lambda n, s: ""), cur


def test_the_new_file_kept_once_it_reaches_the_hub(tmp_path):
    new, cur = swapped(tmp_path)
    assert new.own == NEW_SHA and new.boot() is False and new.status == "new: not confirmed yet"
    new.offer(offer())
    assert new.step(IDLE) is False and new.status == "current"
    new.confirm()
    doc = json.loads(new.state_path.read_text())
    assert "pending" not in doc and doc["last"]["event"] == "updated" and doc["last"]["from"] == OLD_SHA
    assert new.report()["commit"] == COMMIT and new.status == f"updated from {OLD_SHA[:12]}"
    assert cur.read_bytes() == NEW and new.prev_path.read_bytes() == OLD


def test_a_new_file_that_keeps_failing_to_start_is_rolled_back_and_never_tried_again(tmp_path):
    new, cur = swapped(tmp_path)
    for start in range(W.UPDATE_MAX_STARTS):                         # it crashes before it reaches the hub ...
        again = W.Updater(new.cfg, path=cur)
        assert again.boot() is False, start
    last = W.Updater(new.cfg, path=cur)
    assert last.boot() is True                                       # ... one start too many: the old one is back
    assert cur.read_bytes() == OLD
    if os.name != "nt":
        assert cur.stat().st_mode & 0o777 == 0o750
    doc = json.loads(new.state_path.read_text())
    assert doc["last"]["event"] == "rolled back" and doc["last"]["from"] == NEW_SHA and doc["last"]["to"] == OLD_SHA
    assert NEW_SHA in doc["bad"] and "3 starts without reaching the hub" in doc["bad"][NEW_SHA]["why"]
    # the old file starts again: the hub still wants NEW_SHA; it is never fetched again, and the hub hears why
    gh = FakeGitHub()
    old = W.Updater(new.cfg, path=cur, fetch=gh, clock=Clock(), selftest=lambda n, s: "")
    assert old.boot() is False and old.status == f"rolled back from {NEW_SHA[:12]}"
    old.offer(offer())
    assert old.step(IDLE) is False and old.status == f"refused {NEW_SHA[:12]}: rolled back before" and not gh.calls
    assert old.report()["bad"] == [NEW_SHA]


def test_a_new_file_that_runs_but_never_reaches_the_hub_is_rolled_back(tmp_path):
    clock = Clock()
    new, cur = swapped(tmp_path, clock)
    assert new.boot() is False and not new.overdue()
    clock.t += W.UPDATE_CONFIRM_S - 1
    assert not new.overdue()
    clock.t += 1
    assert new.overdue()
    w = make_worker(tmp_path, new)
    w.connected = False
    w._update_loop()                                                 # (returns once it has rolled back)
    assert w.exit_code == W.UPDATE_EXIT and w.stopped and cur.read_bytes() == OLD
    assert NEW_SHA in json.loads(new.state_path.read_text())["bad"]


def test_a_rollback_without_a_previous_file_carries_on(tmp_path):
    new, cur = swapped(tmp_path)
    new.prev_path.unlink()
    assert new.rollback("made up") is False and cur.read_bytes() == NEW
    assert json.loads(new.state_path.read_text())["last"]["event"] == "rollback failed"


def test_a_file_replaced_by_hand_forgets_the_pending_swap(tmp_path):
    new, cur = swapped(tmp_path)
    cur.write_bytes(OLD + b"\n# by hand\n")
    again = W.Updater(new.cfg, path=cur)
    assert again.boot() is False and "pending" not in json.loads(new.state_path.read_text())


# --- throttling -----------------------------------------------------------------------------------------------------

def test_github_at_most_every_10_min_and_longer_after_errors(tmp_path):
    gh = FakeGitHub()
    gh.api_status = 502
    clock = Clock()
    up, _ = updater(tmp_path, gh, clock)
    up.offer(offer())
    assert up.step(IDLE) is False and len(gh.calls) == 1 and up.status.startswith("GitHub: GitHub's API said 502")
    clock.t += W.UPDATE_CHECK_S - 1
    assert up.step(IDLE) is False and len(gh.calls) == 1
    clock.t += 1
    assert up.step(IDLE) is False and len(gh.calls) == 2              # the second failure: 20 min
    clock.t += 2 * W.UPDATE_CHECK_S - 1
    assert up.step(IDLE) is False and len(gh.calls) == 2
    clock.t += 1
    gh.api_status = 200
    assert up.step(IDLE) is True and len(gh.calls) == 4


def test_githubs_rate_limit_is_respected(tmp_path):
    gh = FakeGitHub()
    gh.api_status = 403
    clock = Clock()
    up, _ = updater(tmp_path, gh, clock)
    now = up.wall()
    gh.headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now + 3000))}
    up.offer(offer())
    assert up.step(IDLE) is False and "again in" in up.status
    clock.t += 2900
    assert up.step(IDLE) is False and len(gh.calls) == 1
    clock.t += 200
    assert up.step(IDLE) is False and len(gh.calls) == 2
    assert W.github_wait(429, {"retry-after": "120"}, now) == 120.0
    assert W.github_wait(403, {"x-ratelimit-remaining": "12"}, now) is None


# --- the CLI ----------------------------------------------------------------------------------------------------------

def test_update_cli_retry_and_rollback_by_hand(tmp_path):
    new, cur = swapped(tmp_path)
    new.confirm()
    conf = cur.parent / "worker.json"
    conf.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "log_file": "-"}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIZEUP_STT_")}
    run = lambda *a: subprocess.run([sys.executable, str(cur), *a, "-c", str(conf)], capture_output=True, text=True,
                                    env=env, timeout=60)                # noqa: E731
    out = run("update")
    assert out.returncode == 0 and f"running: {NEW_SHA[:12]} (commit {COMMIT[:12]})" in out.stdout
    out = run("update", "rollback")
    assert out.returncode == 0 and "Restart the worker" in out.stdout and cur.read_bytes() == OLD
    doc = json.loads(new.state_path.read_text())
    assert doc["bad"][NEW_SHA]["why"] == "rolled back by hand"
    out = subprocess.run([sys.executable, str(cur.parent / "worker.py.prev"), "update", "retry", "-c", str(conf)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0 and out.stdout.startswith("1 hash(es) marked bad cleared")
    assert "bad" not in json.loads(new.state_path.read_text())
