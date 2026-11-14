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
    stop.burner = ts[0]
    yield stop
    stop.set()
    for t in ts:
        t.join(5)


def share(out: dict, name: str = "burner") -> float:
    """What thread `name` used of the CPU this test's own threads used in the profile (the burner, the ones asleep,
    the test's thread), 0..1. Never of the whole process's: threads earlier tests left behind in it compute too (an
    xdist worker runs many files; a whole run in one process ends with about 1000 threads), and they took the burner's
    "over 90% of the process" down to 88%."""
    ms = {t["thread"]: t["ms"] for t in out["threads"]}
    mine = sum(ms.get(n, 0.0) for n in ("burner", "sleeper", "waiter", "MainThread"))
    return ms.get(name, 0.0) / mine if mine else 0.0


KERNEL = ThreadClock()             # (one for the tests' own readings: it holds no file open between reads)


def cpu_of(t: threading.Thread) -> float:
    """The CPU seconds thread t has used so far, by the kernel's own count (the clock the accounting reads)."""
    return KERNEL.read().get(t.native_id, 0.0)


def burn(t: threading.Thread, cpu_s: float, timeout: float = 120.0) -> float:
    """Wait until thread t has used cpu_s more CPU; what it had used by then. Never a sleep of so many seconds: on a
    loaded machine the burner once got 0.035 s of CPU in a 0.6 s sleep, and anything measured from it was noise."""
    c0, end = cpu_of(t), time.monotonic() + timeout
    while (c := cpu_of(t)) - c0 < cpu_s:
        assert time.monotonic() < end, f"the burner got {c - c0:.3f} s of CPU in {timeout:.0f} s"
        time.sleep(0.01)
    return c


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
    t0 = time.perf_counter()
    p = quiet().start()
    burn(busy.burner, 0.3)                                                    # (not a sleep: see burn)
    rec = p.tick()
    wall = time.perf_counter() - t0
    assert 0.25 < rec["cpu"]["burner"] <= wall * 1.1, rec["cpu"]              # the 0.3 s it used since the start, counted
    assert rec["cpu"]["burner"] > 0.5 * rec["proc"]                           # most of what the process used (never of the
                                                                              # wall clock: the machine may be busy)
    assert rec["cpu"].get("sleeper", 0.0) < 0.02
    assert rec["cpu"]["burner"] > 20 * rec["cpu"].get("sleeper", 0.001)
    assert rec["proc"] >= rec["cpu"]["burner"] and rec["threads"] >= 3
    s = p.snapshot()
    rows = {r["name"]: r for r in s["threads"]}
    assert s["threads"][0]["name"] == "burner" and rows["burner"]["live"] == 1
    # % of one core, over the tick: what it used of the time that passed (not "over 5%": a machine busy enough gives
    # the burner less than that of the wall clock, and the 0.3 s above is what says it was counted)
    assert rows["burner"]["now"] == pytest.approx(100 * rec["cpu"]["burner"] / rec["dt"], abs=0.11)
    assert rows["burner"]["now"] > 10 * rows["sleeper"]["now"] and rows["sleeper"]["now"] < 3
    assert s["vcpu"] == 0.5 and s["windows"]["now"]["task"] == pytest.approx(2 * s["windows"]["now"]["core"], abs=0.2)
    assert s["process"]["threads"] >= 3 and s["process"]["fds"] and s["process"]["blocks"] > 0
    assert s["process"]["rss"] or s["process"]["rss_peak"]


@needs_thread_cpu
def test_what_was_used_before_the_start_is_not_counted(busy, quiet):
    before = burn(busy.burner, 0.3)                    # 0.3 s of CPU used before the start: never in the first tick
    p = quiet().start()
    rec = p.tick()
    after = cpu_of(busy.burner)
    # The burner goes on computing between the start and the tick. That was "under 0.15 s" once; a loaded runner took
    # longer than that between the two lines (0.178 s, test_profiler.py:92 on main). The bound that holds on any
    # machine: no more than the thread used between a reading taken before the start and one taken after the tick,
    # by the same clock (10 ms for a clock in ticks). Counting what came before the start would add 0.3 s to that.
    got = rec["cpu"].get("burner", 0.0)
    assert got <= after - before + 0.011, (got, before, after, rec["cpu"], rec["dt"], rec["threads"], rec["os_threads"])


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


@pytest.fixture(scope="module")
def all_routes():
    """Every route the hub has: the map layers' modules are imported when an app is built, as on the hub."""
    import sizeup.app  # noqa: F401
    import sizeup.cloud.hub  # noqa: F401
    import sizeup.dvr  # noqa: F401
    import sizeup.gis  # noqa: F401
    import sizeup.roads  # noqa: F401
    import sizeup.weather  # noqa: F401
    return perf_mod.routes()


def test_requests_are_counted_by_the_route_that_answered_never_by_path_or_query(quiet, all_routes):
    p = quiet().start()
    p.request("GET", f"/api/cad/incident/2291?{QUERY}", 200, 0.004, 0.002)
    p.request("GET", "/api/cad/incident/2292", 200, 0.006, 0.004)
    p.request("POST", "/api/cad/cameras/tw-0412/dvr/pin", 200, 0.002, 0.001)
    p.request("GET", f"/{STREET}", 404, 0.001, 0.001)
    p.request("GET", "/api/cad/health", 403, 0.001, 0.001)                 # refused: not even which route
    p.request("POST", "/api/cad/settings/users", 403, 0.001, 0.001)
    p.request("GET", "/api/cad/elm/place", 401, 0.001, 0.001)
    p.request("GET", "/api/cad/state", 500, 0.001, 0.001)
    p.request("GET", "/static/cad.js?v=9f2c", 200, 0.001, 0.0005)
    p.request("GET", "/api/cad/state", 304, 0.001, 0.0005)
    p.tick()
    routes = {r["route"]: r for r in p.snapshot()["routes"]}
    assert set(routes) == {"GET /api/cad/incident/:id", "POST /api/cad/cameras/:id/dvr/pin", "GET (not found)", "GET (refused)",
                           "POST (refused)", "GET (error)", "GET /static/*", "GET /api/cad/state"}
    one = routes["GET /api/cad/incident/:id"]
    assert (one["n"], one["cpu_s"], one["cpu_ms"], one["ms"], one["h1_n"]) == (2, 0.006, 3.0, 5.0, 2)
    assert routes["GET (refused)"]["n"] == 2


def test_a_flood_of_junk_paths_never_fills_the_table_or_pushes_a_real_route_out(quiet, all_routes):
    """A signed-in viewer posting to paths that don't exist (403, 404), or asking for a thousand ids: nothing of what
    was typed is kept, and the real routes stay their own rows."""
    p = quiet().start()
    p.request("GET", "/api/cad/state", 200, 0.001, 0.001)
    for i in range(400):
        junk = f"/api/cad/{'x' * (i % 20 + 1)}/{chr(97 + i % 26)}{'y' * (i // 26)}"
        p.request("POST", junk, 403, 0.001, 0.001)
        p.request("GET", junk, 404, 0.001, 0.001)
        p.request("GET", junk, 200, 0.001, 0.001)                         # (even answered: no route of ours)
        p.request("GET", f"/api/cad/incident/{i}", 200, 0.001, 0.001)
    p.request("GET", "/api/cad/comms", 200, 0.001, 0.001)
    p.tick()
    assert set(p.route_total) == {"GET /api/cad/state", "POST (refused)", "GET (not found)", "GET /api/(unlisted)",
                                  "GET /api/cad/incident/:id", "GET /api/cad/comms"}
    assert "xx" not in json.dumps(p.snapshot())


def test_the_table_is_bounded_and_the_family_unused_the_longest_makes_room(quiet, monkeypatch):
    monkeypatch.setattr(perf_mod, "MAX_ROUTES", 4)
    monkeypatch.setattr(perf_mod, "route_key", lambda m, path, code: path)     # (as many families as the test names)
    p = quiet().start()
    for name in ("a", "b", "c", "d"):
        p.request("GET", name, 200, 0.001, 0.001)
    p.tick()
    p.request("GET", "a", 200, 0.001, 0.001)                               # a: used again; b is now the oldest
    p.request("GET", "c", 200, 0.001, 0.001)
    p.request("GET", "d", 200, 0.001, 0.001)
    p.tick()
    p.request("GET", "e", 200, 0.001, 0.001)
    p.tick()
    assert set(p.route_total) == {"a", "c", "d", "e", perf_mod.OTHER} and p.route_total[perf_mod.OTHER][0] == 1
    assert sum(r[0] for r in p.route_total.values()) == 8                   # nothing lost
    for i in range(50):
        p.request("GET", f"n{i}", 200, 0.001, 0.001)
    assert len(p._route_seen) <= 4 and len(p.route_total) <= 4 + 1 + 4


@pytest.mark.parametrize("path, family", [
    ("/api/cad/state", "/api/cad/state"),
    ("/api/cad/incident/2291/close", "/api/cad/incident/:id/close"),
    ("/api/cad/attachments/0123456789abcdef/17.ts", "/api/cad/attachments/:id/:id"),
    ("/api/cad/attachments/0123456789abcdef/index.m3u8", "/api/cad/attachments/:id/index.m3u8"),
    ("/api/audio/307-1790000000-12.wav", "/api/audio/:id"),
    ("/api/audio/afternoon.wav", "/api/audio/:id"),                        # an id that is a plain word
    ("/api/cad/geocode?q=12+Elm+Pl", "/api/cad/geocode"),
    ("/api/cad/unit/58/status", "/api/cad/unit/:id/status"),
    ("/api/cad/cameras/elmplace/image", "/api/cad/cameras/:id/image"),       # a camera's slug
    ("/api/cad/cameras/image/image", "/api/cad/cameras/:id/image"),          # ... or one that spells a route word
    ("/api/cad/cameras/elmplace/stream/status", "/api/cad/cameras/:id/stream/status"),
    ("/api/cad/cameras/elmplace/stream/chunk_0412.ts", "/api/cad/cameras/:id/stream/:id"),
    ("/api/cad/cameras/elmplace/dvr/index.m3u8", "/api/cad/cameras/:id/dvr/index.m3u8"),
    ("/api/cad/gis/hydrants", "/api/cad/gis/:id"),                           # a layer's key
    ("/api/cad/datasources/elmfeed/refresh", "/api/cad/datasources/:id/refresh"),
    ("/api/incident/elmplace/close", "/api/incident/:id/close"),
    ("/api/station/jobs/abcdefabcdefabcd", "/api/station/jobs/:id"),       # a job id of 16 letters a-f
    ("/api/cad/weather/radar/now/7/37/48.png", "/api/cad/weather/radar/now/:id/:id/:id"),
    ("/api/cad/export/incidents.csv", "/api/cad/export/:id"),
    ("/api/cad/vessels", "/api/cad/vessels"), ("/api/cad/vessels/near", "/api/cad/vessels/near"),      # #402
    ("/api/station/vessels", "/api/station/vessels"),
    ("/api/cad/dispatch/8812.wav", "/api/cad/dispatch/:id"),                # a dispatch's voice (#426)
    ("/api/cad/incident/2291/dispatch", "/api/cad/incident/:id/dispatch"),
    ("/api/cad/incident/2291/preview", "/api/cad/incident/:id/preview"),
    ("/api/cad/elm/place", "/api/(unlisted)"), ("/api/elmplace", "/api/(unlisted)"),
    ("/static/cad.js", "/static/*"), ("/cad", "/cad"), ("/healthz", "/healthz"),
    ("/someone@example.org", "(other)"), ("/wp-login.php", "(other)")])
def test_path_families(path, family, all_routes):
    assert route_family(path) == family
    assert "elm" not in route_family(path)


@pytest.mark.parametrize("name, shown", [
    ("broadcast", "broadcast"), ("MainThread", "MainThread"), ("station-health", "station-health"),
    ("Thread-12 (process_request_thread)", "http"), ("Thread-7 (_drain)", "_drain"), ("Thread-3", "thread"),
    ("ThreadPoolExecutor-0_3", "ThreadPoolExecutor"), ("dvr-4", "dvr"),
    (f"ntfy-{TOPIC}", "ntfy"), ("aws-stt-South 2", "aws-stt"), ("stream-tw-0412", "stream"), ("source-county-a", "source"),
    ("nfm-FG1", "nfm"), ("http-8091", "http"), ("datasource-elmfeed", "datasource"), ("http-stream", "http-stream"), ("odd name; here", "odd_name__here")])
def test_thread_groups_show_only_the_fixed_part_of_a_name(name, shown):
    assert group(name) == shown


def thread_names(tree) -> tuple[list, list]:
    """(made, unknown) for one module's syntax tree. made: the fixed start of every thread name that is put together at
    run time (an f-string), wherever it is given: Thread(name=...), a pool's thread_name_prefix, a Thread subclass's
    super().__init__(name=...), or an argument to a helper that passes its parameter on as the name (spawn(fn, name)).
    unknown: (line, source) of a name that is neither a plain string nor one of those: nobody can say what it holds."""
    import ast
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def owner(node):                     # the function or lambda a node is in
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return None

    def called(call) -> str:
        f = call.func
        return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""

    def constant_loop(name: str, node) -> bool:      # for name, fn in (("hub-forward", ...), ...): every name a plain string
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.For) and name in {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}:
                return isinstance(node.iter, (ast.Tuple, ast.List)) and all(
                    isinstance(e, (ast.Tuple, ast.List)) and all(not isinstance(x, (ast.JoinedStr, ast.BinOp, ast.Call))
                                                                  for x in e.elts) for e in node.iter.elts)
        return False

    made, unknown, helpers = [], [], {}

    def judge(value, call) -> None:
        if isinstance(value, ast.Constant):
            return
        if isinstance(value, ast.JoinedStr):
            first = value.values[0] if value.values else None
            made.append((first.value if isinstance(first, ast.Constant) else "", value.lineno))
            return
        fn = owner(call)
        if isinstance(value, ast.Name) and fn is not None and value.id in [a.arg for a in fn.args.args + fn.args.kwonlyargs]:
            where = parents.get(fn)                      # a helper: def spawn(fn, name) / self.spawn = lambda fn, name: ...
            while isinstance(where, (ast.BoolOp, ast.IfExp)):
                where = parents.get(where)
            names = [fn.name] if not isinstance(fn, ast.Lambda) else [
                t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "")
                for t in (where.targets if isinstance(where, ast.Assign) else [])]
            for n in names:
                helpers[n] = (value.id, [a.arg for a in fn.args.args if a.arg != "self"].index(value.id)
                              if value.id in [a.arg for a in fn.args.args] else None)
            if names:
                return
        if isinstance(value, ast.Name) and constant_loop(value.id, call):
            return
        unknown.append((value.lineno, ast.unparse(value)))

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords if k.arg}
        if called(c) == "Thread" and "name" in kw:
            judge(kw["name"], c)
        elif called(c) == "ThreadPoolExecutor" and "thread_name_prefix" in kw:
            judge(kw["thread_name_prefix"], c)
        elif called(c) == "__init__" and "name" in kw and any(
                isinstance(b, (ast.Name, ast.Attribute)) and ast.unparse(b).endswith("Thread")
                for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) and c in ast.walk(cls) for b in cls.bases):
            judge(kw["name"], c)
    for c in calls:                                      # what the helpers are handed
        if called(c) in helpers:
            param, at = helpers[called(c)]
            kw = {k.arg: k.value for k in c.keywords if k.arg}
            value = kw.get(param) or (c.args[at] if at is not None and at < len(c.args) else None)
            if value is not None and not (isinstance(value, ast.Name) and owner(c) is not None
                                          and value.id in [a.arg for a in owner(c).args.args]):
                judge(value, c)
    return made, unknown


def test_the_guard_reads_every_way_a_thread_is_named():
    import ast
    made, unknown = thread_names(ast.parse("""
import threading
from concurrent.futures import ThreadPoolExecutor
class Runner:
    def __init__(self, spawn=None):
        self.spawn = spawn or (lambda fn, name: threading.Thread(target=fn, name=name, daemon=True).start())
    def go(self, key, topic):
        self.spawn(lambda: None, f"feed-{key}")
        self.spawn(lambda: None, "plain")
        threading.Thread(target=print, name=f"ring-{topic}").start()
        threading.Thread(target=print, name="fixed").start()
        threading.Thread(target=print, name=topic.upper()).start()
        ThreadPoolExecutor(2, thread_name_prefix=f"pool-{key}")
        for name, fn in (("one", print), ("two", print)):
            threading.Thread(target=fn, name=name).start()
def launch(fn, label):
    threading.Thread(target=fn, name=label).start()
launch(print, f"job-{1 + 1}")
launch(print, label="x" + str(2))
class Worker(threading.Thread):
    def __init__(self, channel):
        super().__init__(name=f"stt-{channel}", daemon=True)
"""))
    assert sorted(p for p, _ in made) == ["feed-", "job-", "pool-", "ring-", "stt-"]
    assert [src for _, src in unknown] == ["topic.upper()", "'x' + str(2)"]


def test_every_thread_name_made_at_run_time_is_cut_to_its_fixed_part():
    """A thread named at run time carries data (a topic, a source, a camera, a data source's key): the fixed start of
    each must be in perf.DYNAMIC, so the made part never reaches the table, the profile or the log. Read from the
    syntax tree: Thread(name=...), pools, Thread subclasses, and names handed to spawn helpers by position."""
    import ast
    made, unknown = [], []
    for py in sorted((ROOT / "sizeup").rglob("*.py")):
        m, u = thread_names(ast.parse(py.read_text()))
        made += [(p, f"{py.name}:{line}") for p, line in m]
        unknown += [(f"{py.name}:{line}", src) for line, src in u]
    assert not unknown, f"a thread name that is not a plain string or an f-string: {unknown}"
    assert {p for p, _ in made} >= {"ntfy-", "stream-", "aws-stt-", "source-", "datasource-", "nfm-"}, made
    missing = [(p, at) for p, at in made if not any(p.startswith(d) for d in perf_mod.DYNAMIC)]
    assert not missing, f"add to sizeup/perf.py DYNAMIC: {missing}"
    for p, _ in made:
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


def test_a_read_of_a_few_threads_never_closes_the_others_files(tmp_path, monkeypatch):
    """The profiler reads every thread, then only the few that computed. With more than ~65 threads that second read
    used to close every other thread's file, and the next sample opened them all again."""
    import os
    n = 96
    proc = proc_tree(tmp_path, {1000 + i: ("worker", 1, 1, 1_000_000_000 + i) for i in range(n)})
    tids = [1000 + i for i in range(n)]
    c = ThreadClock(proc, hold=True)
    opened = []
    real_open, c_open = os.open, c._open
    monkeypatch.setattr(os, "open", lambda *a, **kw: opened.append(a[0]) or real_open(*a, **kw))
    if c_open is not None:
        c._open = lambda path, flags: opened.append(path) or c_open(path, flags)
    for _ in range(10):                                  # ten samples: all, then two of them
        assert len(c.read(tids)) == n
        assert len(c.read(tids[:2])) == 2
    assert len(opened) == n and len(c._fds) == n         # each file opened once
    for tid in tids[:6]:                                 # six threads end
        for f in (tmp_path / "self" / "task" / str(tid)).iterdir():
            f.unlink()
    c.forget(tids[6:])
    assert len(c._fds) == n - 6 and len(c.read(tids)) == n - 6
    c.close()


def test_the_tasks_cpu_allowance(tmp_path):
    assert task_vcpu({"SIZEUP_PERF_VCPU": "0.5"}, str(tmp_path)) == 0.5
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    assert task_vcpu({}, str(tmp_path)) == 0.5
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert task_vcpu({}, str(tmp_path)) is None
    v1 = tmp_path / "v1" / "cpu"                                    # cgroup v1: quota and period, each its own file
    v1.mkdir(parents=True)
    (v1 / "cpu.cfs_quota_us").write_text("50000\n")
    (v1 / "cpu.cfs_period_us").write_text("100000\n")
    assert task_vcpu({}, str(tmp_path / "v1")) == 0.5
    (v1 / "cpu.cfs_quota_us").write_text("-1\n")
    assert task_vcpu({}, str(tmp_path / "v1")) is None
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
    before = cpu_of(busy.burner)
    out = p.profile(1.0, hz=100)
    used = cpu_of(busy.burner) - before                    # (from before the profile to after it: at least its share)
    threads = {t["thread"]: t for t in out["threads"]}
    mine = threads["burner"]["ms"]
    assert out["threads"][0]["thread"] == "burner" and share(out) > 0.90
    assert threads.get("waiter", {"pct": 0})["pct"] < 2 and threads.get("sleeper", {"pct": 0})["pct"] < 2    # 31 threads asleep
    # of the burner's own CPU (was: 85% and 90% of the process's, with the burner over 90% of it)
    assert out["self"][0]["fn"].startswith("spin (tests/test_profiler.py:") and out["self"][0]["ms"] > 0.94 * mine
    cum = {x["fn"].split(" ")[0]: x["ms"] for x in out["cumulative"]}
    assert cum["burner"] > 0.99 * mine and cum["spin"] > 0.99 * mine and cum["Thread.run"] > 0.99 * mine
    assert out["lines"][0]["at"].startswith("tests/test_profiler.py:") and out["lines"][0]["at"].endswith(" spin")
    heavy = out["folded"].splitlines()[0]
    assert re.fullmatch(r"burner;Thread\._bootstrap \(python3\.\d+/threading\.py:\d+\);.*;burner \(tests/test_profiler\.py:\d+\);"
                        r"spin \(tests/test_profiler\.py:\d+\) \d+", heavy), heavy
    # microseconds of CPU: the burner's own over the profile, nearly all of it on this one stack. Against what the
    # thread really used, and what the profile says it used: never against the wall clock ("over 50 ms of a 1 s
    # profile" failed with 42 ms on a machine that gave the burner no more).
    us = int(heavy.rsplit(" ", 1)[1])
    assert 0 < us <= used * 1e6 * 1.02 + 11_000, (us, used)
    assert us == pytest.approx(threads["burner"]["ms"] * 1000, rel=0.15), (us, threads["burner"])
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
    # 3 stacks, then one "(other stacks)" a thread group at most (and one for a group never caught): whatever else runs
    assert out["stacks"] <= 3 + 2 * len(out["threads"]) and out["stacks_dropped"] > 0 and out["stacks_truncated"] > 0
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


class CoarseClock(ThreadClock):
    """The real per-thread clock, as a kernel without schedstats gives it: in whole 10 ms ticks (kind "stat")."""

    def __init__(self, proc="/proc", hold=False):
        super().__init__(proc, hold)
        self.real, self.kind = self.kind, "stat"

    def read(self, tids=None):
        self.kind = self.real
        try:
            return {tid: int(c * 100) / 100 for tid, c in super().read(tids).items()}
        finally:
            self.kind = "stat"


@needs_thread_cpu
def test_with_a_coarse_clock_the_profile_goes_by_the_cpu_since_the_last_sample(busy, quiet, monkeypatch):
    """No 1 ms window can be seen in 10 ms ticks (/proc/<tid>/stat only): the profile still answers (it raised KeyError)."""
    monkeypatch.setattr(perf_mod, "ThreadClock", CoarseClock)
    logged = []
    p = quiet(log=logged.append).start()
    out = p.profile(1.5, hz=50)
    assert "error" not in out, (out, logged)
    assert out["method"] == "interval" and out["clock"] == "stat" and out["window_ms"] == 0
    threads = {t["thread"]: t for t in out["threads"]}
    assert out["threads"][0]["thread"] == "burner" and share(out) > 0.80 and threads["burner"]["caught"] >= 1
    assert out["self"][0]["fn"].startswith("spin (tests/test_profiler.py:") and out["cpu_ms"] > 50
    assert threads.get("sleeper", {"pct": 0})["pct"] < 5


def test_a_profile_that_fails_is_never_left_running(quiet, monkeypatch):
    """Whatever goes wrong, the sampling or the log line about it: not "running" for ever, the waiter let go."""
    def boom(*a):
        raise RuntimeError("the log is gone")
    p = quiet(log=boom).start()
    monkeypatch.setattr(p, "_sample", lambda seconds, hz: 1 / 0)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)        # (the thread ends with the log's error)
    p.profile_start(1)
    assert p._pdone.wait(5)
    assert p.last_profile["error"] == "ZeroDivisionError" and p.snapshot()["profile"]["running"] is False
    with pytest.raises(TooSoon):
        p.profile_start(1)
    p._next_ok = 0.0
    monkeypatch.setattr(p, "_sample", lambda seconds, hz: {"seconds": 1.0})   # sampled, then the log line fails
    p.profile_start(1)
    assert p._pdone.wait(5) and p.snapshot()["profile"]["running"] is False and "error" not in p.last_profile


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
        wait(lambda: "POST (refused)" in h.app.perf._routes)   # (a request is
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
    assert routes["GET /api/cad/state"]["n"] == 5 and routes["GET (refused)"]["n"] == 5        # (no such incidents: 400)
    assert "GET (not found)" in routes and routes["POST /api/cad/perf/profile"]["n"] == 1 and routes["POST (refused)"]["n"] == 1
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
    assert code == 202 and out == {"running": True, "seconds": 20.0, "hz": 25.0}        # 25 Hz unless asked
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
