"""Where the hub's CPU goes (#425): per-thread CPU accounting that is always on, and a sampling profiler on demand.

The hub is one process with ~50 named background threads on half a vCPU; requests are fast, so its CPU is the threads',
and nothing said which. Three parts, all standard library:

- **Accounting** (Perf.tick, every TICK_S): each thread's CPU time, read from the kernel (Linux:
  /proc/self/task/<tid>/schedstat, ns on the CPU, else .../stat's utime+stime; macOS, for development and the tests:
  the Mach thread list), by thread *group* (group(): the thread's name with whatever is made at run time cut off, so a
  topic or a source in a name never shows), kept for an hour and summed since the start. Request threads are born and
  gone between two reads: each reports its own CPU as it ends (thread_exit, time.thread_time()), as "http"; the state
  streams, which live for hours, as "http-stream". Each request also adds its CPU and time to its path family
  (route_family(): the path with every id cut out; never a query string). With it: memory, threads, open files, the
  garbage collector's count and time (gc.callbacks), and whatever sizes were registered (gauge()).
- **Profiler** (Perf.profile_start, off until asked): a thread reads every thread's stack (sys._current_frames()) HZ
  times a second for a bounded time. A stack says where a thread *is*, not what it costs: the ~50 threads asleep in
  a wait would fill a plain sample count. So each sample is a 1 ms window: the CPU clocks before, the clocks and the
  stacks after; only a thread whose clock moved in the window counts, for the CPU it used in it, and each thread
  group's total is then set to the CPU it really used over the whole profile (Perf._sample). Out: CPU by thread, by
  function (self and cumulative), by line, and the stacks in the folded format flame graph tools read
  ("thread;a;b;c <microseconds>"). One at a time, a rest after each, bounded memory (MAX_STACKS, MAX_DEPTH), its own
  CPU measured and reported. What it cannot see: time inside one C call is its Python caller's (json, sqlite, zlib).
- **Trend** (Perf.hour_line): one log line an hour with the top threads' shares, memory and thread count, so what grows
  with uptime shows in the log without anyone asking.

Nothing here reads a request, a body, a query string or a local variable: thread groups, function names, file:line and
path families only. GET /api/cad/perf and POST /api/cad/perf/profile are admins' (sizeup/cad/api.py ADMIN_READS, WRITES).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
import re
import sys
import threading
import time
from collections import deque
from typing import Callable, Iterable

TICK_S = 60.0                # the accounting's period
KEEP_TICKS = 60              # ... and how many are kept: an hour
HOUR_TICKS = 60              # a trend line every this many ticks
FIRST_LINE_TICKS = 5         # ... and one after the first 5 min: what a fresh start looks like, to compare with
KEEP_HOURS = 72
TOP_LOG = 8                  # threads in the hourly line
MAX_ROUTES = 150             # path families kept (more: "(other)")
PROFILE_S = 30.0
PROFILE_MAX_S = 120.0
PROFILE_MIN_S = 1.0
PROFILE_HZ = 50.0
PROFILE_HZ_MAX = 100.0
PROFILE_HZ_MIN = 5.0
PROFILE_REST_S = 30.0        # after one ends: none for this long, or for as long as it ran (so never over half the time)
WINDOW_S = 0.001             # the profiler's window: a thread whose CPU clock moved in this long was computing
MORE, UNCAUGHT = "(other stacks)", "(short bursts, not caught)"
MAX_STACKS = 4000            # distinct stacks kept in one profile (more: "(other stacks)")
MAX_DEPTH = 48               # frames kept of one stack, from where it is
FOLDED_MAX = 2000            # folded stacks returned (the heaviest)
TOP_N = 40

# Thread names made at run time (name=f"..."): only what is before the made part is shown. A thread named for a push
# topic, an incident source or a channel says none of them here. tests/test_profiler.py reads the source for every such
# name and fails on one that is not listed.
DYNAMIC = ("ntfy-", "stream-", "aws-stt-", "source-", "nfm-", "http-", "updating-")
_UNNAMED = re.compile(r"Thread-\d+(?: \((.+)\))?")
_COUNTER = re.compile(r"(?:[-_ ]\d+)+$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.()<>-]")
HTTP, STREAM, OTHER, NATIVE = "http", "http-stream", "(other)", "(native)"


def group(name: str) -> str:
    """A thread's name as it is shown: its group. Request threads are "http", an unnamed thread is its target function,
    a pool's threads are the pool, a name made at run time is its fixed part (DYNAMIC)."""
    name = str(name or "")
    if m := _UNNAMED.fullmatch(name):
        fn = m.group(1) or "thread"
        return HTTP if fn == "process_request_thread" else _UNSAFE.sub("_", fn)[:32]
    if name == STREAM:
        return STREAM
    for p in DYNAMIC:
        if name.startswith(p):
            return p[:-1]
    return _UNSAFE.sub("_", _COUNTER.sub("", name))[:32] or "thread"


_WORD = re.compile(r"[a-z][a-z_-]{0,23}(?:\.(?:js|css|html|svg|png|ico|json|wav|ts|m3u8|zip|woff2?))?")
_PAGES = ("/", "/cad", "/cad.html", "/monitor", "/comms", "/comms.html", "/station", "/index.html", "/healthz")


def route_family(path: str) -> str:
    """A request's path family: the path without its query, every part that is not a plain lower-case word (an id, a
    number, a name) as ":id", static files as one. A path that is none of ours is "(other)": what someone typed into
    the address bar is never kept."""
    path = str(path or "").split("?", 1)[0].split("#", 1)[0]
    if path in _PAGES:
        return path
    if path.startswith("/static/"):
        return "/static/*"
    if not path.startswith("/api/"):
        return OTHER
    parts = [p if _WORD.fullmatch(p) else ":id" for p in path.split("/")[1:7] if p]
    return "/" + "/".join(parts)


# -- each thread's CPU time, from the kernel -----------------------------------------------------------------------------

class _MachBasic(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in ("user_s", "user_us", "sys_s", "sys_us", "cpu_usage", "policy", "run_state",
                                           "flags", "suspend_count", "sleep_time")]


class _MachIdent(ctypes.Structure):
    _fields_ = [("thread_id", ctypes.c_uint64), ("thread_handle", ctypes.c_uint64), ("dispatch_qaddr", ctypes.c_uint64)]


class ThreadClock:
    """{native thread id: CPU seconds} for this process's threads. kind: where it reads ("schedstat": ns on the CPU;
    "stat": utime+stime in clock ticks, 10 ms; "mach": macOS; "none": nowhere, and read() is {}). Only ever a file of
    our own process or our own task's thread list: a thread that ended meanwhile is just not in the answer (never a
    call on a thread id that may be gone). hold: keep each thread's file open between reads (the profiler reads them
    HZ times a second) and read them without letting go of the GIL (libc's pread through ctypes.PyDLL: os.pread gives
    the GIL up at every call, and while another thread computes each of the ~50 would wait a switch interval to get it
    back, so the busier the hub the fewer the samples); close() when done."""

    def __init__(self, proc: str = "/proc", hold: bool = False):
        self.dir = os.path.join(proc, "self", "task")
        self.hold = hold
        self._fds: dict[int, int] = {}
        self._tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._libc = None
        self._pread = None
        self._buf = ctypes.create_string_buffer(512)
        self.kind = self._detect()
        if hold and self.kind in ("schedstat", "stat"):
            try:
                fn = ctypes.PyDLL(None, use_errno=True).pread
                fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_longlong]
                fn.restype = ctypes.c_ssize_t
                self._pread = fn
            except (OSError, AttributeError):      # no libc to ask: os.pread (correct, slower under load)
                pass

    def _detect(self) -> str:
        try:
            tid = next(iter(os.listdir(self.dir)))
        except (OSError, StopIteration):
            return "mach" if sys.platform == "darwin" and self._mach_init() else "none"
        try:
            with open(os.path.join(self.dir, tid, "schedstat"), "rb") as f:
                int(f.read().split()[0])
            return "schedstat"
        except (OSError, ValueError, IndexError):
            return "stat"

    def _parse(self, raw: bytes) -> float:
        if self.kind == "schedstat":             # "<ns on the CPU> <ns waiting to run> <timeslices>"
            return int(raw.split(None, 1)[0]) / 1e9
        f = raw.rpartition(b")")[2].split()      # after "pid (comm)": the name may itself hold spaces and brackets
        return (int(f[11]) + int(f[12])) / self._tick      # utime, stime: fields 14 and 15

    def read(self, tids: Iterable[int] | None = None) -> dict[int, float]:
        """tids: only these (the profiler: the Python threads); None: every thread of the process."""
        if self.kind == "mach":
            return self._mach_read()
        if self.kind == "none":
            return {}
        if tids is None:
            try:
                tids = [int(n) for n in os.listdir(self.dir) if n.isdigit()]
            except OSError:
                return {}
        out: dict[int, float] = {}
        for tid in tids:
            try:
                if self.hold:
                    fd = self._fds.get(tid)
                    if fd is None:
                        fd = self._fds[tid] = os.open(os.path.join(self.dir, str(tid), self.kind), os.O_RDONLY)
                    if self._pread is not None:
                        n = self._pread(fd, self._buf, 512, 0)
                        if n <= 0:
                            raise OSError("gone")
                        raw = self._buf.raw[:n]
                    else:
                        raw = os.pread(fd, 512, 0)
                else:
                    with open(os.path.join(self.dir, str(tid), self.kind), "rb") as f:
                        raw = f.read(512)
                out[tid] = self._parse(raw)
            except (OSError, ValueError, IndexError):     # the thread ended: no such file, or ESRCH on an open one
                fd = self._fds.pop(tid, None)
                if fd is not None:
                    self._close(fd)
        if self.hold and len(self._fds) > len(out) + 64:    # threads that ended and were never asked for again
            for tid in [t for t in self._fds if t not in out]:
                self._close(self._fds.pop(tid))
        return out

    @staticmethod
    def _close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def close(self) -> None:
        for fd in self._fds.values():
            self._close(fd)
        self._fds.clear()

    # macOS (development, the tests): the task's own thread list, each with its id (= threading's native_id) and times

    def _mach_init(self) -> bool:
        try:
            c = ctypes.PyDLL(ctypes.util.find_library("c"), use_errno=True)       # (PyDLL: the GIL is kept, as above)
            self._task = ctypes.c_uint.in_dll(c, "mach_task_self_").value
            c.task_threads.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint)]
            c.thread_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            c.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
            c.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
            self._libc = c
            return bool(self._mach_read())
        except Exception:  # noqa: BLE001 - no per-thread CPU on this machine: the process's own is still counted
            self._libc = None
            return False

    def _mach_read(self) -> dict[int, float]:
        c = self._libc
        if c is None:
            return {}
        ports, n = ctypes.POINTER(ctypes.c_uint)(), ctypes.c_uint()
        if c.task_threads(self._task, ctypes.byref(ports), ctypes.byref(n)):
            return {}
        out: dict[int, float] = {}
        basic, ident = _MachBasic(), _MachIdent()
        for i in range(n.value):
            port = ports[i]
            nb, ni = ctypes.c_uint(10), ctypes.c_uint(6)      # THREAD_BASIC_INFO = 3, THREAD_IDENTIFIER_INFO = 4
            if (c.thread_info(port, 3, ctypes.byref(basic), ctypes.byref(nb)) == 0
                    and c.thread_info(port, 4, ctypes.byref(ident), ctypes.byref(ni)) == 0):
                out[ident.thread_id] = basic.user_s + basic.sys_s + (basic.user_us + basic.sys_us) / 1e6
            c.mach_port_deallocate(self._task, port)
        c.vm_deallocate(self._task, ports, n.value * ctypes.sizeof(ctypes.c_uint))
        return out


# -- the process: memory, threads, open files, its CPU allowance ---------------------------------------------------------

def rss_bytes(proc: str = "/proc") -> int | None:
    """Resident memory now (Linux); None elsewhere (rss_peak() still answers)."""
    try:
        with open(os.path.join(proc, "self", "statm"), "rb") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def rss_peak() -> int | None:
    try:
        import resource
        n = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return n if sys.platform == "darwin" else n * 1024      # bytes on macOS, KB on Linux
    except Exception:  # noqa: BLE001
        return None


def open_fds(proc: str = "/proc") -> int | None:
    for d in (os.path.join(proc, "self", "fd"), "/dev/fd"):
        try:
            return len(os.listdir(d))
        except OSError:
            continue
    return None


def task_vcpu(env=os.environ, cgroup: str = "/sys/fs/cgroup", fetch: Callable[[str], bytes] | None = None) -> float | None:
    """The CPU this process may use, in vCPUs (the Fargate task's 0.5): SIZEUP_PERF_VCPU, else the cgroup's limit, else
    what ECS says of the task (its metadata endpoint, in the task itself; asked once). None: not limited, or unknown."""
    try:
        if env.get("SIZEUP_PERF_VCPU"):
            return float(env["SIZEUP_PERF_VCPU"]) or None
    except ValueError:
        pass
    try:
        with open(os.path.join(cgroup, "cpu.max")) as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            return round(int(quota) / int(period), 3)
    except (OSError, ValueError):
        pass
    url = env.get("ECS_CONTAINER_METADATA_URI_V4")
    if url:
        try:
            import json
            if fetch is None:
                import urllib.request

                def fetch(u: str) -> bytes:
                    with urllib.request.urlopen(u, timeout=2) as r:     # link-local, inside the task
                        return r.read(65536)
            cpu = (json.loads(fetch(url.rstrip("/") + "/task")).get("Limits") or {}).get("CPU")
            return float(cpu) if cpu else None
        except Exception:  # noqa: BLE001 - shares of one core are still shown
            pass
    return None


# -- the profiler's output ---------------------------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).replace("\\", "/") + "/"


def _file(path: str) -> str:
    """A source file as it is shown: from our package (or the installed one) down, else its folder and name; never the
    machine's own directories."""
    path = path.replace("\\", "/")
    if path.startswith(_ROOT):
        return path[len(_ROOT):]
    i = path.rfind("/site-packages/")
    return path[i + len("/site-packages/"):] if i >= 0 else "/".join(path.split("/")[-2:])


def _label(code) -> str:
    name = getattr(code, "co_qualname", None) or code.co_name
    return f"{name} ({_file(code.co_filename)}:{code.co_firstlineno})".replace(";", ",")


class Busy(Exception):
    """A profile is running: one at a time."""


class TooSoon(Exception):
    """One just ran: retry_s to wait."""

    def __init__(self, retry_s: float):
        super().__init__(f"profile again in {max(1, round(retry_s))} s")
        self.retry_s = retry_s


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole > 0 else 0.0


def summarize(agg: dict, lines: dict, meta: dict) -> dict:
    """What a profile says. agg: {(thread group, stack: code objects, leaf first): CPU s}; lines: {(leaf code, line): s}."""
    total = sum(agg.values())
    threads: dict[str, float] = {}
    self_s: dict[str, float] = {}
    cum_s: dict[str, float] = {}
    labels: dict = {}

    def lab(c) -> str:
        if isinstance(c, str):
            return c
        s = labels.get(c)
        if s is None:
            s = labels[c] = _label(c)
        return s

    folded = []
    for (name, stack), w in agg.items():
        threads[name] = threads.get(name, 0.0) + w
        names = [lab(c) for c in stack]
        self_s[names[0]] = self_s.get(names[0], 0.0) + w
        for n in set(names):
            cum_s[n] = cum_s.get(n, 0.0) + w
        folded.append((w, ";".join([name, *reversed(names)])))
    folded.sort(reverse=True)

    def top(d: dict, key: str, n: int = TOP_N) -> list[dict]:
        return [{key: k, "ms": round(v * 1000, 1), "pct": _pct(v, total)}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

    caught = meta.pop("caught", {})

    by_line: dict[str, float] = {}
    for (c, ln), w in lines.items():           # (two code objects may be one line's: a comprehension's and its function's)
        at = c if isinstance(c, str) else f"{_file(c.co_filename)}:{ln} {getattr(c, 'co_qualname', None) or c.co_name}"
        by_line[at] = by_line.get(at, 0.0) + w
    return {**meta, "cpu_ms": round(total * 1000, 1),
            "threads": [dict(t, caught=caught.get(t["thread"], 0)) for t in top(threads, "thread")],
            "self": top(self_s, "fn"), "cumulative": top(cum_s, "fn"), "lines": top(by_line, "at", 25),
            "stacks": len(agg), "folded_dropped": max(0, len(folded) - FOLDED_MAX),
            "folded": "\n".join(f"{s} {max(1, round(w * 1e6))}" for w, s in folded[:FOLDED_MAX])}


# -- the whole of it ---------------------------------------------------------------------------------------------------

class Perf:
    def __init__(self, log: Callable[[str], None] = print, tick_s: float = TICK_S, vcpu: float | None | str = "auto",
                 proc: str = "/proc", clock: Callable[[], float] = time.time, mono: Callable[[], float] = time.monotonic):
        self.log, self.tick_s, self.proc, self.clock, self.mono = log, tick_s, proc, clock, mono
        self.vcpu = vcpu
        self.threads = ThreadClock(proc)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last: dict[int, float] = {}          # tid -> the CPU s counted of it so far
        self._names: dict[int, str] = {}           # tid -> its group, as last seen (a thread ending: still its group)
        self._ended: dict[str, float] = {}         # group -> CPU s of threads that ended since the last tick
        self._routes: dict[str, list[float]] = {}  # "GET /family" -> [requests, CPU s, wall s] since the last tick
        self._route_keys: set[str] = set()
        self.ticks: deque[dict] = deque(maxlen=KEEP_TICKS)
        self.hours: deque[dict] = deque(maxlen=KEEP_HOURS)
        self.total: dict[str, float] = {}          # group -> CPU s since start()
        self.route_total: dict[str, list[float]] = {}
        self.proc_total = 0.0
        self.n_ticks = 0
        self.started = self.t0 = self._t_last = self._proc_last = None
        self.gauges: dict[str, Callable[[], int | None]] = {}
        self.gc_n = [0, 0, 0]
        self.gc_s = 0.0
        self.gc_max = 0.0
        self._gc_t0 = None
        self._gc_ticked = 0.0
        self._plock = threading.Lock()             # the profiler's own state
        self._profiling: dict | None = None
        self._pstop = threading.Event()
        self._pdone = threading.Event()
        self._next_ok = 0.0
        self.last_profile: dict | None = None

    # -- registered sizes: how many things a cache holds (cheap, a len()) ---------------------------------------------

    def gauge(self, name: str, fn: Callable[[], int | None]) -> None:
        self.gauges[_UNSAFE.sub("_", name)[:32]] = fn

    def _gauges(self) -> dict[str, int]:
        out = {}
        for name, fn in list(self.gauges.items()):
            try:
                v = fn()
                if v is not None:
                    out[name] = int(v)
            except Exception:  # noqa: BLE001 - a size is never in the way
                pass
        return out

    # -- the garbage collector ---------------------------------------------------------------------------------------

    def _gc(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._gc_t0 = time.perf_counter()
        elif self._gc_t0 is not None:
            dt, self._gc_t0 = time.perf_counter() - self._gc_t0, None
            self.gc_n[min(2, int(info.get("generation", 0)))] += 1
            self.gc_s += dt
            self.gc_max = max(self.gc_max, dt)

    # -- accounting --------------------------------------------------------------------------------------------------

    def start(self) -> Perf:
        """The baseline (what was used before now is not counted), the collector's hook, the accounting thread."""
        if self.started is not None:
            return self
        with self._lock:
            self.started, self.t0 = self.clock(), self.mono()
            self._t_last, self._proc_last = self.t0, time.process_time()
            self._names = {t.native_id: group(t.name) for t in threading.enumerate() if t.native_id}
            self._last = self.threads.read()
        if self._gc not in gc.callbacks:
            gc.callbacks.append(self._gc)
        threading.Thread(target=self._run, name="perf", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._pstop.set()
        try:
            gc.callbacks.remove(self._gc)
        except ValueError:
            pass

    def _run(self) -> None:
        if self.vcpu == "auto":
            self.vcpu = task_vcpu()
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never dies, never in the way
                self.log(f"perf: {type(e).__name__}: {e}")

    def thread_exit(self) -> None:
        """Called by a thread as it ends (the web server's request threads): its CPU not yet counted, to its group.
        A request thread lives for milliseconds, so no tick ever sees it."""
        if self.started is None:
            return
        cpu, tid = time.thread_time(), threading.get_native_id()
        name = group(threading.current_thread().name)
        with self._lock:
            d = cpu - self._last.get(tid, 0.0)
            if d > 0:
                self._ended[name] = self._ended.get(name, 0.0) + d
            self._last[tid] = cpu          # (still listed until it is really gone: the next tick counts only what is past this)

    def request(self, method: str, path: str, code: int, wall_s: float, cpu_s: float) -> None:
        """One request answered: its CPU and time, to its path family (never the path itself, never its query)."""
        if self.started is None:
            return
        key = f"{method if method in ('GET', 'POST', 'HEAD') else 'OTHER'} {'(not found)' if code == 404 else route_family(path)}"
        with self._lock:
            if key not in self._route_keys:
                if len(self._route_keys) >= MAX_ROUTES:
                    key = OTHER
                self._route_keys.add(key)
            r = self._routes.get(key)
            if r is None:
                r = self._routes[key] = [0, 0.0, 0.0]
            r[0] += 1
            r[1] += max(0.0, cpu_s)
            r[2] += max(0.0, wall_s)

    def tick(self) -> dict:
        """One accounting pass: what each thread group used since the last one."""
        if self.started is None:
            raise RuntimeError("perf: not started")
        live = {t.native_id: group(t.name) for t in threading.enumerate() if t.native_id}
        with self._lock:
            now = self.mono()
            cpu = self.threads.read()
            proc = time.process_time()
            self._names.update(live)
            used: dict[str, float] = dict(self._ended)
            for tid, c in cpu.items():
                d = c - self._last.get(tid, 0.0)
                if d > 0:
                    name = self._names.get(tid, NATIVE)
                    used[name] = used.get(name, 0.0) + d
                else:
                    c = self._last.get(tid, c)     # (a coarser clock than the thread's own: never back)
                cpu[tid] = c
            self._last = cpu
            self._names = {tid: n for tid, n in self._names.items() if tid in cpu or tid in live}
            self._ended = {}
            routes, self._routes = self._routes, {}
            dt, self._t_last = max(1e-6, now - self._t_last), now
            dproc, self._proc_last = max(0.0, proc - self._proc_last), proc
            rest = dproc - sum(used.values())      # threads that came and went unseen, and what no clock here reads
            if rest > 0.0005:
                used[OTHER] = used.get(OTHER, 0.0) + rest
            counts: dict[str, int] = {}
            for n in live.values():
                counts[n] = counts.get(n, 0) + 1
            rec = {"t": self.clock(), "dt": dt, "cpu": used, "proc": max(dproc, sum(used.values())), "routes": routes,
                   "live": counts, "rss": rss_bytes(self.proc), "threads": len(live), "os_threads": len(cpu) or None,
                   "fds": open_fds(self.proc), "gc_s": self.gc_s - self._gc_ticked, "blocks": sys.getallocatedblocks()}
            self._gc_ticked = self.gc_s
            self.ticks.append(rec)
            for name, s in used.items():
                self.total[name] = self.total.get(name, 0.0) + s
            for key, r in routes.items():
                t = self.route_total.setdefault(key, [0, 0.0, 0.0])
                for i in range(3):
                    t[i] += r[i]
            self.proc_total += rec["proc"]
            self.n_ticks += 1
            n = self.n_ticks
        rec["gauges"] = self._gauges()             # (outside the lock: someone else's len())
        if n == FIRST_LINE_TICKS or n % HOUR_TICKS == 0:
            hour = self._hour(HOUR_TICKS if n % HOUR_TICKS == 0 else FIRST_LINE_TICKS)
            if n % HOUR_TICKS == 0:
                self.hours.append(hour)
            self.log(self.hour_line(hour))
        return rec

    def _window(self, n: int | None) -> tuple[float, dict[str, float], float, float]:
        """(seconds, {group: CPU s}, process CPU s, collector s) over the last n ticks (None: since the start)."""
        with self._lock:
            if n is None:
                return (self._t_last - self.t0, dict(self.total), self.proc_total, self._gc_ticked)
            recs = list(self.ticks)[-n:]
        cpu: dict[str, float] = {}
        for r in recs:
            for name, s in r["cpu"].items():
                cpu[name] = cpu.get(name, 0.0) + s
        return sum(r["dt"] for r in recs), cpu, sum(r["proc"] for r in recs), sum(r["gc_s"] for r in recs)

    def _vcpu(self) -> float | None:
        return self.vcpu if isinstance(self.vcpu, (int, float)) and self.vcpu else None

    def _hour(self, n: int) -> dict:
        """The last n ticks as one record: the trend line's, and GET /api/cad/perf's "hours"."""
        dt, cpu, proc, gc_s = self._window(n)
        with self._lock:
            last = self.ticks[-1]
        return {"t": last["t"], "s": round(dt), "up_s": round(self.mono() - self.t0), "core_pct": _pct(proc, dt),
                "gc_pct": _pct(gc_s, dt),
                "top": {k: _pct(v, dt) for k, v in sorted(cpu.items(), key=lambda kv: -kv[1])[:TOP_LOG]},
                "rss": last["rss"] if last["rss"] is not None else rss_peak(), "threads": last["threads"],
                "fds": last["fds"], "blocks": last["blocks"], "gauges": last.get("gauges") or {}}

    def hour_line(self, h: dict) -> str:
        """The trend line: one line, thread groups and numbers only. Shares are of the task's CPU when that is known
        (the Fargate task's 0.5 vCPU), else of one core."""
        v = self._vcpu()
        k = 1.0 / v if v else 1.0
        span = f"{h['s'] / 3600:.0f}h" if h["s"] >= 3000 else f"{h['s'] / 60:.0f}m"
        of = f"{v:g} vCPU" if v else "1 core"
        top = " ".join(f"{name}={pct * k:.1f}" for name, pct in h["top"].items())
        rss = f"{h['rss'] / 1048576:.0f}" if h.get("rss") else "?"
        return (f"perf {span}: cpu={h['core_pct'] * k:.1f}% of {of} top[{top}] gc={h['gc_pct'] * k:.1f}% rss_mb={rss} "
                f"threads={h['threads']} fds={h['fds'] if h['fds'] is not None else '?'} up_h={h['up_s'] / 3600:.1f}")

    def snapshot(self) -> dict:
        """GET /api/cad/perf: the table by thread group over the last tick, 5 min, 1 h and since the start (% of one
        core; "task": of the task's vCPUs), the process, the path families, the hourly trend."""
        if self.started is None:
            return {"started": None}
        v = self._vcpu()
        wins = {"now": self._window(1), "m5": self._window(5), "h1": self._window(KEEP_TICKS), "boot": self._window(None)}
        with self._lock:
            last = self.ticks[-1] if self.ticks else None
            recs = list(self.ticks)
            route_total = {k: list(r) for k, r in self.route_total.items()}
        names = sorted({n for w in wins.values() for n in w[1]} | set((last or {}).get("live", ())),
                       key=lambda n: (-wins["h1"][1].get(n, 0.0), -wins["boot"][1].get(n, 0.0), n))
        rows = [{"name": n, "live": (last or {}).get("live", {}).get(n, 0),
                 **{k: _pct(w[1].get(n, 0.0), w[0]) for k, w in wins.items()}} for n in names]
        hour: dict[str, list[float]] = {}
        for r in recs:
            for key, x in r["routes"].items():
                t = hour.setdefault(key, [0, 0.0, 0.0])
                for i in range(3):
                    t[i] += x[i]
        span = wins["h1"][0]
        routes = [{"route": key, "n": int(t[0]), "cpu_s": round(t[1], 3), "cpu_ms": round(1000 * t[1] / t[0], 2) if t[0] else 0,
                   "ms": round(1000 * t[2] / t[0], 1) if t[0] else 0, "h1": _pct(hour.get(key, [0, 0.0])[1], span),
                   "h1_n": int(hour.get(key, [0])[0])}
                  for key, t in sorted(route_total.items(), key=lambda kv: -kv[1][1])[:TOP_N]]
        with self._plock:
            p = self._profiling
            prof = {"running": p is not None, "seconds": p["seconds"] if p else None,
                    "retry_s": max(0, round(self._next_ok - self.mono())) if p is None else None,
                    "last": self.last_profile["started"] if self.last_profile else None}
        return {"started": self.started, "uptime_s": round(self.mono() - self.t0), "tick_s": self.tick_s, "ticks": self.n_ticks,
                "vcpu": v, "clock": self.threads.kind,
                "windows": {k: {"s": round(w[0]), "core": _pct(w[2], w[0]), "task": _pct(w[2], w[0] * v) if v else None,
                                "gc": _pct(w[3], w[0])} for k, w in wins.items()},
                "threads": rows,
                "process": {"rss": last["rss"] if last else rss_bytes(self.proc), "rss_peak": rss_peak(),
                            "threads": threading.active_count(), "os_threads": last["os_threads"] if last else None,
                            "fds": open_fds(self.proc), "blocks": sys.getallocatedblocks(),
                            "gc": {"collections": list(self.gc_n), "s": round(self.gc_s, 3), "max_ms": round(self.gc_max * 1000, 1)}},
                "gauges": (last or {}).get("gauges") or self._gauges(), "routes": routes, "hours": list(self.hours),
                "profile": prof}

    # -- the profiler --------------------------------------------------------------------------------------------------

    def profile_start(self, seconds: float = PROFILE_S, hz: float = PROFILE_HZ) -> dict:
        """Start one (Busy: one is running; TooSoon: one just ended). Returns {seconds, hz}; wait with profile_wait()."""
        seconds = min(PROFILE_MAX_S, max(PROFILE_MIN_S, float(seconds)))
        hz = min(PROFILE_HZ_MAX, max(PROFILE_HZ_MIN, float(hz)))
        if seconds != seconds or hz != hz:
            raise ValueError("seconds and hz: numbers")
        with self._plock:
            if self._profiling is not None:
                raise Busy("a profile is running")
            if (wait := self._next_ok - self.mono()) > 0:
                raise TooSoon(wait)
            self._profiling = {"seconds": seconds, "hz": hz, "started": self.clock()}
            self._pstop.clear()
            self._pdone.clear()
        threading.Thread(target=self._profile, args=(seconds, hz), name="perf-sampler", daemon=True).start()
        return {"seconds": seconds, "hz": hz}

    def profile_wait(self, timeout: float) -> dict | None:
        """The profile just started, once it is done (None: still running after timeout)."""
        return self.last_profile if self._pdone.wait(timeout) else None

    def profile_cancel(self) -> None:
        self._pstop.set()

    def profile(self, seconds: float = PROFILE_S, hz: float = PROFILE_HZ) -> dict:
        """Start one and wait for it."""
        p = self.profile_start(seconds, hz)
        out = self.profile_wait(p["seconds"] + 30)
        if out is None:
            raise RuntimeError("perf: the profile did not end")
        return out

    def _profile(self, seconds: float, hz: float) -> None:
        started, t0 = self.clock(), time.perf_counter()
        out: dict = {"error": "the profile failed"}
        try:
            out = self._sample(seconds, hz)
        except Exception as e:  # noqa: BLE001 - never in the way
            out = {"error": f"{type(e).__name__}"}
            self.log(f"perf: profile: {type(e).__name__}: {e}")
        ran = time.perf_counter() - t0
        out.update(started=started, asked_s=seconds)
        with self._plock:
            self.last_profile = out
            self._profiling = None
            self._next_ok = self.mono() + max(PROFILE_REST_S, ran)
        self._pdone.set()
        if "error" not in out:
            self.log(f"perf profile: {out['seconds']:.0f}s {out['hz']:g}Hz samples={out['samples']} cpu_ms={out['cpu_ms']:.0f} "
                     f"overhead={out['overhead']['core_pct']}% of 1 core")

    def _sample(self, seconds: float, hz: float) -> dict:
        """hz times a second: read every thread's CPU clock, let WINDOW_S pass, read them again and take every
        thread's stack in the same breath, let WINDOW_S pass again and read the clocks of those that had moved. A
        thread whose clock moved both before and after its stack was taken was computing when it was taken, so the
        stack is where its CPU goes, and it counts for the CPU it used in the two windows; one that is waiting counts
        nothing, wherever it waits. (The CPU since the last sample would not do: a thread that works 5 ms and sleeps is
        asleep at most samples, and its work would be charged to the line it sleeps on. Nor one window: this thread
        gets the GIL back just as the working thread lets go of it, which is when it goes to sleep.) Each thread
        group's stacks are then scaled to the CPU the group really used over the whole profile, from the same clocks:
        the totals are exact, their split by function is the sample's. A coarse clock (kind "stat", 10 ms) cannot see
        a 1 ms window: then the CPU since the last sample is used."""
        clock = ThreadClock(self.proc, hold=True)
        my_ident = threading.get_ident()
        windowed = clock.kind != "stat"
        agg: dict[tuple, float] = {}               # (group, stack: code objects, leaf first) -> CPU s seen in the windows
        lines: dict[tuple, float] = {}             # (group, leaf code, line) -> the same
        seen: dict[str, float] = {}                # group -> CPU s seen in the windows
        caught: dict[str, int] = {}                # group -> times a thread of it was caught computing
        groups: dict[int, str] = {}                # tid -> group
        period = 1.0 / hz
        window = min(WINDOW_S, period / 4) if windowed else 0.0
        samples = dropped = truncated = 0
        try:
            first = clock.read()
            final = dict(first)
            cpu0, proc0, t0 = time.thread_time(), time.process_time(), time.perf_counter()
            nxt, end = t0 + period, t0 + seconds
            while not self._pstop.wait(max(0.0, nxt - window - time.perf_counter())):
                now = time.perf_counter()
                if now >= end:
                    break
                nxt = max(nxt + period, now + period / 2)        # a late read never brings a burst after it
                threads = [(t.ident, t.native_id, t.name) for t in threading.enumerate() if t.native_id and t.ident != my_ident]
                tids = None if clock.kind == "mach" else [tid for _, tid, _ in threads]
                if windowed:
                    before = clock.read(tids)
                    if self._pstop.wait(window):
                        break
                else:
                    before = final
                cpu = clock.read(tids)
                frames = sys._current_frames()
                samples += 1
                found = []                          # (tid, group, stack, leaf): the threads that computed in the window
                for ident, tid, name in threads:
                    c = cpu.get(tid)
                    if c is None:
                        continue
                    g = groups.get(tid)
                    if g is None:
                        g = groups[tid] = group(name)
                    if c - before.get(tid, c if windowed else 0.0) <= 0:
                        continue                    # asleep or waiting: where it is costs nothing
                    f = frames.get(ident)
                    if f is None:
                        continue
                    leaf, stack, depth = (g, f.f_code, f.f_lineno), [], 0
                    while f is not None and depth < MAX_DEPTH:
                        stack.append(f.f_code)
                        f, depth = f.f_back, depth + 1
                    if f is not None:
                        stack.append("(deeper)")
                        truncated += 1
                    found.append((tid, g, tuple(stack), leaf))
                frames = f = None                   # (no frame kept alive between reads)
                final.update(cpu)
                if not found:
                    continue
                if windowed:                        # still computing after its stack was taken? else that was its wait
                    if self._pstop.wait(window):
                        break
                    after = clock.read([tid for tid, *_ in found] if clock.kind != "mach" else None)
                    final.update(after)
                else:
                    after = cpu
                for tid, g, stack, leaf in found:
                    c = after.get(tid)
                    if c is None or (windowed and c <= cpu[tid]):
                        continue
                    d = c - before.get(tid, 0.0)
                    key = (g, stack)
                    if key not in agg and len(agg) >= MAX_STACKS:
                        key, leaf = (g, (MORE,)), (g, MORE, 0)
                        dropped += 1
                    agg[key] = agg.get(key, 0.0) + d
                    if leaf in lines or len(lines) < MAX_STACKS + 64:
                        lines[leaf] = lines.get(leaf, 0.0) + d
                    seen[g] = seen.get(g, 0.0) + d
                    caught[g] = caught.get(g, 0) + 1
            ran = time.perf_counter() - t0
            own, proc = time.thread_time() - cpu0, time.process_time() - proc0
        finally:
            clock.close()
        used: dict[str, float] = {}                 # group -> the CPU it really used meanwhile
        for tid, c in final.items():
            d = c - first.get(tid, 0.0)
            if d > 0 and tid in groups:
                used[groups[tid]] = used.get(groups[tid], 0.0) + d
        scale = {g: used.get(g, s) / s for g, s in seen.items() if s > 0}
        agg = {k: w * scale[k[0]] for k, w in agg.items()}
        by_line: dict[tuple, float] = {}
        for (g, code, ln), w in lines.items():
            by_line[(code, ln)] = by_line.get((code, ln), 0.0) + w * scale[g]
        for g, s in used.items():                   # never caught computing: its CPU is real, where it went is not known
            if g not in seen and s >= 0.0005:
                agg[(g, (UNCAUGHT,))] = s
        return summarize(agg, by_line, {
            "seconds": round(ran, 2), "hz": hz, "samples": samples, "clock": clock.kind,
            "method": "window" if windowed else "interval", "window_ms": round(window * 1000, 2),
            "seen_ms": round(sum(seen.values()) * 1000, 1),     # CPU seen in the windows: what the split by function rests on
            "caught": caught,
            "process_cpu_ms": round(proc * 1000, 1),          # everything the process used meanwhile, the profiler included
            "overhead": {"cpu_ms": round(own * 1000, 1), "core_pct": _pct(own, ran),
                         "us_per_sample": round(own * 1e6 / samples) if samples else 0},
            "stacks_dropped": dropped, "stacks_truncated": truncated})
