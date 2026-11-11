"""#425: where the hub's CPU goes (sizeup/perf.py): CPU by thread, always on; a sampling profiler on demand; a trend
line an hour. Real threads and real HTTP in this process; the kernel's files as Linux writes them, in a temp dir."""

import gc
import json
import re
import threading
import time
from pathlib import Path

import pytest

from sizeup import perf as perf_mod
from sizeup.perf import (FIRST_LINE_TICKS, HOUR_TICKS, HTTP, KEEP_TICKS, Busy, Perf, ThreadClock, TooSoon, group,
                         route_family, task_vcpu)
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401  (fixtures)
from tests.test_app import wait
from tests.test_cloud import HubRun, hub_cfg
from tests.test_roles import COREY, DEE, JULIAN, VAL, people

ROOT = Path(__file__).resolve().parent.parent
# Never in anything the profiler says: a topic in a thread's name, a query string, who asked, an address typed in.
TOPIC, QUERY, STREET = "zq-ring-topic-7f3a", "q=12+Elm+Pl&token=s3cr3t-value", "12 Elm Pl"
needs_thread_cpu = pytest.mark.skipif(ThreadClock().kind == "none", reason="no per-thread CPU clock on this machine")


def spin(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(range(2000))


def burner(stop: threading.Event) -> None:
    spin(stop)


@pytest.fixture
def busy():
    """A thread that computes ("burner") and one that sleeps ("sleeper"), until the test ends."""
    stop = threading.Event()
    ts = [threading.Thread(target=burner, args=(stop,), name="burner", daemon=True),
          threading.Thread(target=stop.wait, name="sleeper", daemon=True)]
    for t in ts:
        t.start()
    yield stop
    stop.set()
    for t in ts:
        t.join(5)


@pytest.fixture
def quiet():
    made = []

    def make(**kw) -> Perf:
        p = Perf(log=kw.pop("log", lambda m: None), tick_s=kw.pop("tick_s", 3600), vcpu=kw.pop("vcpu", 0.5), **kw)
        made.append(p)
        return p
    yield make
    for p in made:
        p.stop()


# -- accounting ------------------------------------------------------------------------------------------------------------

@needs_thread_cpu
def test_a_busy_thread_is_charged_its_cpu_and_a_sleeping_one_next_to_nothing(busy, quiet):
    p = quiet().start()
    t0 = time.perf_counter()
    time.sleep(0.6)
    rec = p.tick()
    wall = time.perf_counter() - t0
    assert 0.05 < rec["cpu"]["burner"] <= wall * 1.1, rec["cpu"]
    assert rec["cpu"]["burner"] > 0.5 * rec["proc"]                           # most of what the process used (never of the
                                                                              # wall clock: the machine may be busy)
    assert rec["cpu"].get("sleeper", 0.0) < 0.02
    assert rec["cpu"]["burner"] > 20 * rec["cpu"].get("sleeper", 0.001)
    assert rec["proc"] >= rec["cpu"]["burner"] and rec["threads"] >= 3
    s = p.snapshot()
    rows = {r["name"]: r for r in s["threads"]}
    assert s["threads"][0]["name"] == "burner" and rows["burner"]["live"] == 1
    assert rows["burner"]["now"] > 5 and rows["sleeper"]["now"] < 3           # % of one core
    assert s["vcpu"] == 0.5 and s["windows"]["now"]["task"] == pytest.approx(2 * s["windows"]["now"]["core"], abs=0.2)
    assert s["process"]["threads"] >= 3 and s["process"]["fds"] and s["process"]["blocks"] > 0
    assert s["process"]["rss"] or s["process"]["rss_peak"]


@needs_thread_cpu
def test_what_was_used_before_the_start_is_not_counted(busy, quiet):
    time.sleep(0.3)
    p = quiet().start()
    rec = p.tick()
    assert rec["cpu"].get("burner", 0.0) < 0.15, (rec["cpu"], rec["dt"], rec["threads"], rec["os_threads"])


def test_a_thread_gone_between_two_readings_reports_itself(quiet):
    """A request thread lives for milliseconds: no reading ever sees it, so it reports its own CPU as it ends."""
    p = quiet().start()

    def work() -> None:
        t = time.thread_time()
        while time.thread_time() - t < 0.05:
            sum(range(500))
        p.thread_exit()
    t = threading.Thread(target=work, name="Thread-41 (process_request_thread)")
    t.start()
    t.join()
    rec = p.tick()
    assert 0.045 < rec["cpu"][HTTP] < 0.5
    assert rec["cpu"][HTTP] <= rec["proc"] + 1e-6
    assert p.tick()["cpu"].get(HTTP, 0.0) < 0.005         # once (but for the last of its exit, if the kernel still had it)


def test_windows_roll_and_since_the_start_keeps_everything(quiet, monkeypatch):
    now = [1000.0]
    p = quiet(mono=lambda: now[0], clock=lambda: 1_790_000_000.0 + now[0])
    monkeypatch.setattr(p.threads, "read", lambda tids=None: {})
    cpu = [0.0]
    monkeypatch.setattr(perf_mod.time, "process_time", lambda: cpu[0])
    p.start()
    n = KEEP_TICKS + 30
    for i in range(n):
        now[0] += 60.0
        cpu[0] += 6.0 if i < 30 else 30.0            # the first half hour 10% of a core, then 50%
        p.tick()
    assert len(p.ticks) == KEEP_TICKS and p.n_ticks == n
    s = p.snapshot()
    w = s["windows"]
    assert (w["now"]["s"], w["m5"]["s"], w["h1"]["s"], w["boot"]["s"]) == (60, 300, 3600, n * 60)
    assert w["h1"]["core"] == 50.0 and w["now"]["core"] == 50.0             # the first half hour has rolled out of the hour
    assert w["boot"]["core"] == pytest.approx(100 * (30 * 6 + 60 * 30) / (n * 60), abs=0.1)
    assert w["h1"]["task"] == 100.0                                            # of the task's 0.5 vCPU
    assert s["uptime_s"] == n * 60 and s["started"] == 1_790_001_000.0


def test_requests_are_counted_by_path_family_never_by_path_or_query(quiet):
    p = quiet().start()
    p.request("GET", f"/api/cad/incident/2291/note?{QUERY}", 200, 0.004, 0.002)
    p.request("GET", "/api/cad/incident/2292/note", 200, 0.006, 0.004)
    p.request("POST", "/api/cad/cameras/tw-0412/dvr/pin", 200, 0.002, 0.001)
    p.request("GET", f"/{STREET}", 404, 0.001, 0.001)
    p.request("GET", "/static/cad.js?v=9f2c", 200, 0.001, 0.0005)
    p.tick()
    routes = {r["route"]: r for r in p.snapshot()["routes"]}
    assert set(routes) == {"GET /api/cad/incident/:id/note", "POST /api/cad/cameras/:id/dvr/pin", "GET (not found)", "GET /static/*"}
    note = routes["GET /api/cad/incident/:id/note"]
    assert (note["n"], note["cpu_s"], note["cpu_ms"], note["ms"], note["h1_n"]) == (2, 0.006, 3.0, 5.0, 2)
    for i in range(perf_mod.MAX_ROUTES + 50):                 # bounded: more families than are kept go to "(other)"
        p.request("GET", f"/api/{'x' * (i % 20 + 1)}/{chr(97 + i % 26)}{'y' * (i // 26)}", 200, 0.001, 0.001)
    p.tick()
    assert len(p.route_total) <= perf_mod.MAX_ROUTES + 1 and perf_mod.OTHER in p.route_total


@pytest.mark.parametrize("path, family", [
    ("/api/cad/state", "/api/cad/state"),
    ("/api/cad/incident/2291/close", "/api/cad/incident/:id/close"),
    ("/api/cad/attachments/0123456789abcdef/17.ts", "/api/cad/attachments/:id/:id"),
    ("/api/cad/attachments/0123456789abcdef/index.m3u8", "/api/cad/attachments/:id/index.m3u8"),
    ("/api/audio/307-1790000000-12.wav", "/api/audio/:id"),
    ("/api/cad/geocode?q=12+Elm+Pl", "/api/cad/geocode"),
    ("/api/cad/unit/E58", "/api/cad/unit/:id"),
    ("/static/cad.js", "/static/*"), ("/cad", "/cad"), ("/healthz", "/healthz"),
    ("/someone@example.org", "(other)"), ("/wp-login.php", "(other)")])
def test_path_families(path, family):
    assert route_family(path) == family


@pytest.mark.parametrize("name, shown", [
    ("broadcast", "broadcast"), ("MainThread", "MainThread"), ("station-health", "station-health"),
    ("Thread-12 (process_request_thread)", "http"), ("Thread-7 (_drain)", "_drain"), ("Thread-3", "thread"),
    ("ThreadPoolExecutor-0_3", "ThreadPoolExecutor"), ("dvr-4", "dvr"),
    (f"ntfy-{TOPIC}", "ntfy"), ("aws-stt-South 2", "aws-stt"), ("stream-tw-0412", "stream"), ("source-county-a", "source"),
    ("nfm-FG1", "nfm"), ("http-8091", "http"), ("http-stream", "http-stream"), ("odd name; here", "odd_name__here")])
def test_thread_groups_show_only_the_fixed_part_of_a_name(name, shown):
    assert group(name) == shown


def test_every_thread_name_made_at_run_time_is_cut_to_its_fixed_part():
    """A thread named with an f-string carries data (a topic, a source, a camera): each such prefix must be in
    perf.DYNAMIC, so the made part never reaches the table, the profile or the log."""
    found = set()
    for py in (ROOT / "sizeup").rglob("*.py"):
        for m in re.finditer(r"""name=f(["'])([^"'{]*)\{""", py.read_text()):
            found.add((m.group(2), py.name))
    assert found, "no thread named at run time: has the source moved?"
    missing = [(p, f) for p, f in found if not any(p.startswith(d) for d in perf_mod.DYNAMIC)]
    assert not missing, f"add to sizeup/perf.py DYNAMIC: {missing}"
    for p, _ in found:
        assert group(p + "secret-Value 9") == next(d for d in perf_mod.DYNAMIC if p.startswith(d))[:-1]


# -- the kernel's own files (Linux, as the hub runs) ---------------------------------------------------------------------------

# /proc/<pid>/task/<tid>/stat as Linux 6.x writes it; Python names the kernel thread for the Thread, cut to 15 bytes:
# brackets and spaces in the middle of the line. Fields 14 and 15 (utime, stime) in clock ticks.
STAT = ("{tid} ({comm}) S 1 7 7 0 -1 4194368 2311 0 0 0 {ut} {st} 0 0 20 0 53 0 8012 2318901248 61034 18446744073709551615 "
        "187650212200448 187650214936988 281474163448144 0 0 0 0 16781312 17642 0 0 0 -1 1 0 0 0 0 0 187650215003480 "
        "187650215582704 187651024777216 281474163451493 281474163451546 281474163451546 281474163453925 0\n")


def proc_tree(tmp_path, threads: dict, schedstat: bool = True) -> str:
    for tid, (comm, ut, st, ns) in threads.items():
        d = tmp_path / "self" / "task" / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(STAT.format(tid=tid, comm=comm, ut=ut, st=st))
        if schedstat:
            (d / "schedstat").write_text(f"{ns} 48211973 9120\n")
    (tmp_path / "self" / "statm").write_text("566138 61034 3310 713 0 96212 0\n")
    (tmp_path / "self" / "fd").mkdir(exist_ok=True)
    for i in range(7):
        (tmp_path / "self" / "fd" / str(i)).write_text("")
    return str(tmp_path)


def test_thread_cpu_from_proc_schedstat_and_stat(tmp_path):
    import os
    threads = {7: ("python", 912, 233, 11_462_118_337), 31: ("broadcast", 4410, 301, 47_120_554_901),
               58: ("Thread-9 (proces", 3, 1, 41_870_112)}
    proc = proc_tree(tmp_path, threads)
    c = ThreadClock(proc)
    assert c.kind == "schedstat"
    assert c.read() == {7: 11.462118337, 31: 47.120554901, 58: 0.041870112}
    assert c.read([31, 999]) == {31: 47.120554901}                 # a thread that ended: just not there
    for d in (tmp_path / "self" / "task").iterdir():               # a kernel without schedstats: utime + stime
        (d / "schedstat").unlink()
    c = ThreadClock(proc)
    tick = os.sysconf("SC_CLK_TCK")
    assert c.kind == "stat" and c.read() == {7: (912 + 233) / tick, 31: (4410 + 301) / tick, 58: 4 / tick}
    assert perf_mod.rss_bytes(proc) == 61034 * os.sysconf("SC_PAGE_SIZE") and perf_mod.open_fds(proc) == 7
    assert ThreadClock(str(tmp_path / "nowhere")).kind in ("mach", "none")


def test_the_profilers_clock_keeps_files_open_and_lets_go_of_ended_threads(tmp_path):
    proc = proc_tree(tmp_path, {7: ("python", 1, 1, 1_000_000_000), 31: ("broadcast", 1, 1, 2_000_000_000)})
    c = ThreadClock(proc, hold=True)
    assert c.read([7, 31]) == {7: 1.0, 31: 2.0} and len(c._fds) == 2 and (c._pread is not None)
    (tmp_path / "self" / "task" / "31" / "schedstat").write_text("2500000000 1 1\n")
    assert c.read([7, 31])[31] == 2.5                               # the same open file, read again from its start
    c.read([7, 31, 404])
    assert 404 not in c._fds
    c.close()
    assert c._fds == {}


def test_the_tasks_cpu_allowance(tmp_path):
    assert task_vcpu({"SIZEUP_PERF_VCPU": "0.5"}, str(tmp_path)) == 0.5
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    assert task_vcpu({}, str(tmp_path)) == 0.5
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert task_vcpu({}, str(tmp_path)) is None
    asked = []
    meta = json.dumps({"Cluster": "c", "Limits": {"CPU": 0.5, "Memory": 2048}, "Containers": []}).encode()   # ECS's shape
    env = {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/abc"}
    assert task_vcpu(env, str(tmp_path), fetch=lambda u: asked.append(u) or meta) == 0.5
    assert asked == ["http://169.254.170.2/v4/abc/task"]
    assert task_vcpu(env, str(tmp_path), fetch=lambda u: 1 / 0) is None


def test_the_collectors_count_and_time(quiet):
    p = quiet().start()
    junk = [[i] for i in range(20000)]
    for x in junk:
        x.append(x)
    del junk, x
    gc.collect()
    assert p.gc_n[2] >= 1 and p.gc_s > 0 and p.gc_max > 0
    s = p.snapshot()["process"]["gc"]
    assert s["collections"][2] >= 1 and s["s"] >= 0
    p.stop()
    assert p._gc not in gc.callbacks


def test_registered_sizes_are_read_at_each_tick_and_a_broken_one_is_left_out(quiet):
    p = quiet().start()
    things = [1, 2, 3]
    p.gauge("things", lambda: len(things))
    p.gauge("gone", lambda: 1 / 0)
    p.gauge("unset", lambda: None)
    assert p.tick()["gauges"] == {"things": 3}
    things.append(4)
    p.tick()
    assert p.snapshot()["gauges"] == {"things": 4}


# -- the trend line ------------------------------------------------------------------------------------------------------

LINE = re.compile(r"perf (5m|1h): cpu=\d+\.\d% of 0\.5 vCPU top\[((?:[A-Za-z0-9_.()<>-]+=\d+\.\d ?){1,8})\] gc=\d+\.\d% "
                  r"rss_mb=(\d+|\?) threads=\d+ fds=(\d+|\?) up_h=\d+\.\d")


def test_one_line_after_five_minutes_then_one_an_hour(quiet, monkeypatch):
    lines = []
    now = [0.0]
    p = quiet(log=lines.append, mono=lambda: now[0])
    names = {100 + i: f"worker{chr(97 + i)}" for i in range(10)}
    names[200] = f"ntfy-{TOPIC}"
    cpu = dict.fromkeys(names, 0.0)
    monkeypatch.setattr(p.threads, "read", lambda tids=None: dict(cpu))
    spent = [0.0]
    monkeypatch.setattr(perf_mod.time, "process_time", lambda: spent[0])
    p.start()
    p._names = {tid: group(n) for tid, n in names.items()}
    for n in range(1, 2 * HOUR_TICKS + 1):
        now[0] += 60.0
        for i, tid in enumerate(sorted(cpu)):
            cpu[tid] += 0.6 * (i + 1) * (2 if n > HOUR_TICKS else 1)      # the second hour: twice the first
        spent[0] = sum(cpu.values())
        p.tick()
        assert len(lines) == (0 if n < FIRST_LINE_TICKS else 1 if n < HOUR_TICKS else 2 if n < 2 * HOUR_TICKS else 3), n
    for line in lines:
        assert LINE.fullmatch(line), line
        assert "\n" not in line and TOPIC not in line and line.count("=") <= 8 + 6
    assert lines[0].startswith("perf 5m:") and lines[1].startswith("perf 1h:") and "up_h=1.0" in lines[1] and "up_h=2.0" in lines[2]
    top = lambda line: dict(kv.split("=") for kv in LINE.fullmatch(line).group(2).split())   # noqa: E731
    assert list(top(lines[1]))[:2] == ["ntfy", "workerj"] and len(top(lines[1])) == 8                # the top 8, heaviest first
    assert float(top(lines[1])["ntfy"]) == 22.0                     # 6.6 s a minute: 11% of a core, 22% of 0.5 vCPU
    assert float(top(lines[2])["ntfy"]) == 44.0                     # the growth, hour on hour, in the log
    hours = p.snapshot()["hours"]
    assert [h["up_s"] for h in hours] == [3600, 7200] and hours[1]["top"]["ntfy"] == 2 * hours[0]["top"]["ntfy"]
    assert hours[1]["core_pct"] == pytest.approx(2 * hours[0]["core_pct"], abs=0.2)


def test_the_line_says_one_core_when_the_allowance_is_unknown(quiet):
    p = quiet(vcpu=None).start()
    p.tick()
    assert re.match(r"perf \d+m: cpu=\d+\.\d% of 1 core top\[", p.hour_line(p._hour(1)))


# -- the profiler --------------------------------------------------------------------------------------------------------

@needs_thread_cpu
def test_a_profile_charges_the_function_that_computes_not_the_threads_that_wait(busy, quiet):
    p = quiet().start()
    sleepers = [threading.Thread(target=busy.wait, name=f"waiter-{i}", daemon=True) for i in range(30)]
    for t in sleepers:
        t.start()
    out = p.profile(1.0, hz=100)
    threads = {t["thread"]: t for t in out["threads"]}
    assert out["threads"][0]["thread"] == "burner" and threads["burner"]["pct"] > 90
    assert threads.get("waiter", {"pct": 0})["pct"] < 2 and threads.get("sleeper", {"pct": 0})["pct"] < 2    # 31 threads asleep
    assert out["self"][0]["fn"].startswith("spin (tests/test_profiler.py:") and out["self"][0]["pct"] > 85
    cum = {x["fn"].split(" ")[0]: x["pct"] for x in out["cumulative"]}
    assert cum["burner"] > 90 and cum["spin"] > 90 and cum["Thread.run"] > 90
    assert out["lines"][0]["at"].startswith("tests/test_profiler.py:") and out["lines"][0]["at"].endswith(" spin")
    heavy = out["folded"].splitlines()[0]
    assert re.fullmatch(r"burner;Thread\._bootstrap \(python3\.\d+/threading\.py:\d+\);.*;burner \(tests/test_profiler\.py:\d+\);"
                        r"spin \(tests/test_profiler\.py:\d+\) \d+", heavy), heavy
    assert 50_000 < int(heavy.rsplit(" ", 1)[1]) < 2_500_000           # microseconds of CPU
    assert 0 < out["cpu_ms"] <= out["process_cpu_ms"] * 1.05
    assert "perf-sampler" not in threads                              # itself: measured apart
    o = out["overhead"]
    assert o["cpu_ms"] > 0 and o["us_per_sample"] > 0 and out["samples"] >= 1    # its own cost, measured and said


@needs_thread_cpu
def test_a_profile_stops_on_time_and_is_bounded(busy, quiet, monkeypatch):
    monkeypatch.setattr(perf_mod, "MAX_STACKS", 3)
    monkeypatch.setattr(perf_mod, "MAX_DEPTH", 4)
    monkeypatch.setattr(perf_mod, "FOLDED_MAX", 2)
    stop = threading.Event()

    def deep(n: int) -> None:
        if n:
            return deep(n - 1)
        spin(stop)
    many = [threading.Thread(target=deep, args=(5 + i,), name=f"deep{chr(97 + i)}", daemon=True) for i in range(8)]
    for t in many:
        t.start()
    p = quiet().start()
    t0 = time.perf_counter()
    try:
        out = p.profile(1.0, hz=100)
    finally:
        stop.set()
    took = time.perf_counter() - t0
    assert 0.9 < took < 20 and 0.9 < out["seconds"] < 4 and out["asked_s"] == 1.0     # (loose: a loaded machine, and the
                                                                                       # whole suite's threads in this process)
    assert out["stacks"] <= 3 + 9 and out["stacks_dropped"] > 0 and out["stacks_truncated"] > 0      # 3, and one "(other stacks)" a thread
    assert len(out["folded"].splitlines()) == 2 and out["folded_dropped"] == out["stacks"] - 2
    assert any("(other stacks)" in x["fn"] for x in out["self"])
    assert all(line.count(";") <= 4 + 1 for line in out["folded"].splitlines())                        # 4 frames and "(deeper)"
    assert not [t for t in threading.enumerate() if t.name == "perf-sampler"]
    # asked for more than is allowed: cut to the limits
    assert perf_mod.PROFILE_MAX_S == 120 and perf_mod.PROFILE_HZ_MAX == 100
    p._next_ok = 0.0
    assert p.profile_start(9999, hz=9999) == {"seconds": 120.0, "hz": 100.0}
    p.profile_cancel()
    assert p.profile_wait(5) is not None and p.last_profile["seconds"] < 3


def test_one_profile_at_a_time_and_a_rest_after_each(quiet):
    now = [50.0]
    p = quiet(mono=lambda: now[0]).start()
    assert p.profile_start(5)["seconds"] == 5
    with pytest.raises(Busy):
        p.profile_start(1)
    assert p.snapshot()["profile"]["running"] is True
    p.profile_cancel()
    assert p.profile_wait(5) is not None
    with pytest.raises(TooSoon) as e:
        p.profile_start(1)
    assert 0 < e.value.retry_s <= perf_mod.PROFILE_REST_S and "profile again in 30 s" in str(e.value)
    assert p.snapshot()["profile"] == {"running": False, "seconds": None, "retry_s": 30, "last": p.last_profile["started"]}
    now[0] += perf_mod.PROFILE_REST_S + 1
    assert p.profile_start(1)["seconds"] == 1
    p.profile_cancel()
    p.profile_wait(5)


# -- the hub: admins only, and nothing of anyone in what it says -----------------------------------------------------------------

@pytest.fixture
def access_hub(tmp_path, jwks):  # noqa: F811
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD))
    h.server.access.certs_url = jwks.url
    h.server.access.log = lambda m: None
    yield h
    h.close()


def as_(key, email):
    return {"Cf-Access-Jwt-Assertion": jwt(key, email=email)}


def test_only_an_admin_reads_the_cpu_table_or_runs_a_profile(keys, access_hub):  # noqa: F811
    h = access_hub
    people(h.app.cad)
    assert req(h.url + "/api/cad/perf")[0] == 401
    for who in (JULIAN, VAL, DEE):                                     # a dispatcher, a viewer, a deactivated dispatcher
        for path in ("/api/cad/perf", "/api/cad/perf/profile", "/api/cad/perf/profile?format=folded"):
            code, out = req(h.url + path, headers=as_(keys[0], who))
            assert code == 403 and out["error"].startswith("Admins only") and "threads" not in out, (who, path)
        code, out = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1}, as_(keys[0], who))
        assert code == 403 and "error" in out and "folded" not in out, who
    assert h.app.perf.last_profile is None and h.app.perf.snapshot()["profile"]["running"] is False     # none ran
    code, out = req(h.url + "/api/cad/perf", headers=as_(keys[0], COREY))
    assert code == 200 and {"threads", "windows", "process", "routes", "hours", "gauges", "uptime_s"} <= set(out)
    code, out = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1}, as_(keys[0], COREY))
    assert code == 200 and out["samples"] > 0 and "folded" in out
    rows = h.app.cad.activity.rows()                                   # who profiled, and who was refused, in the activity
    said = {(r["email"], r["summary"]) for r in rows}
    assert (COREY, "Profiled the hub's CPU (1.0 s)") in said
    assert (JULIAN, "Refused: perf/profile (needs admin; dispatcher)") in said


def test_the_hub_counts_its_threads_and_requests_and_says_nothing_of_anyone(keys, access_hub):  # noqa: F811
    """End to end on a hub: requests with a query string and a signed-in person, a thread named for a topic, then
    the table, a profile and its folded stacks: thread groups, functions, file:line and path families only."""
    h = access_hub
    people(h.app.cad)
    admin = as_(keys[0], COREY)
    stop = threading.Event()
    t = threading.Thread(target=burner, args=(stop,), name=f"ntfy-{TOPIC}", daemon=True)
    t.start()
    try:
        for i in range(5):
            assert req(h.url + f"/api/cad/state?{QUERY}", headers=admin)[0] == 200
            req(h.url + f"/api/cad/incident/{9000 + i}?{QUERY}", headers=admin)
        req(h.url + f"/{STREET.replace(' ', '%20')}", headers=admin)
        code, prof = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1, "hz": 100}, admin)
        assert code == 200
        code, again = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1}, admin)
        assert code == 429 and again["retry_s"] > 0 and "profile again in" in again["error"]           # the rest after one
        wait(lambda: sum(r[0] for k, r in list(h.app.perf._routes.items()) if "perf/profile" in k) == 2)   # (a request is
        h.app.perf.tick()                                                # counted just after its answer is sent)
        code, table = req(h.url + "/api/cad/perf", headers=admin)
        code2, last = req(h.url + "/api/cad/perf/profile", headers=admin)
        code3, folded = req(h.url + "/api/cad/perf/profile?format=folded", headers=admin)
    finally:
        stop.set()
        t.join(5)
    assert (code, code2, code3) == (200, 200, 200) and last["folded"] == prof["folded"]
    assert folded.decode().strip() == prof["folded"].strip()
    routes = {r["route"]: r for r in table["routes"]}
    assert routes["GET /api/cad/state"]["n"] == 5 and routes["GET /api/cad/incident/:id"]["n"] == 5
    assert "GET (not found)" in routes and routes["POST /api/cad/perf/profile"]["n"] == 2
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(h.url + "/api/cad/stream", headers=admin), timeout=10) as r:
        r.read1(100)                                                     # a state stream open: its thread counts apart
        h.app.perf.tick()
        live = {x["name"]: x["live"] for x in h.app.perf.snapshot()["threads"]}
    assert live.get("http-stream") == 1
    names = {r["name"] for r in table["threads"]}
    assert {"http", "broadcast", "ntfy"} <= names or ThreadClock().kind == "none"
    if ThreadClock().kind != "none":
        assert prof["threads"][0]["thread"] == "ntfy" and prof["self"][0]["fn"].startswith("spin (tests/test_profiler.py:")
    said = json.dumps([prof, table, last]) + folded.decode()
    for secret in (TOPIC, "s3cr3t", "token=", "q=12", STREET, "Elm", COREY, COREY.lower(), JULIAN, "9001", "?", "Cf-Access",
                   str(Path.home())):
        assert secret not in said, secret
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", said)                 # nobody's address
    assert len(set(re.findall(r'"(?:fn|at|thread|route|name)": "([^"]+)"', said))) > 20      # (and it did say something)


def test_one_profile_at_a_time_over_http(access_hub, keys):  # noqa: F811
    h = access_hub
    people(h.app.cad)
    admin = as_(keys[0], COREY)
    code, out = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 20, "wait": False}, admin)
    assert code == 202 and out == {"running": True, "seconds": 20.0, "hz": 50.0}
    code, out = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1}, admin)
    assert code == 409 and out["running"] is True
    h.app.perf.profile_cancel()
    assert h.app.perf.profile_wait(5) is not None
    for bad in ({"seconds": "long"}, {"seconds": [1]}, {"hz": None}):
        code, out = req(h.url + "/api/cad/perf/profile", "POST", bad, admin)
        assert code == 400 and "error" in out, bad
    code, out = req(h.url + "/api/cad/perf/profile", "POST", {"seconds": 1}, admin)
    assert code == 429


def test_no_perf_off_the_hub(tmp_path):
    """Only the hub has it: the all-in-one app (and so the station) answers as for any path it doesn't know."""
    from sizeup.web.server import WebServer
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda i: None, lambda i: False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.httpd.server_address[1]}"
        assert req(url + "/api/cad/perf")[0] == 404
        assert req(url + "/api/cad/perf/profile", "POST", {"seconds": 1})[0] == 404
    finally:
        srv.shutdown()
