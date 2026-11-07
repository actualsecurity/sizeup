"""The hub's own error spikes, pushed to the status topic (#285).

Today about 1,500 "aircraft: OverflowError" lines in 25 min went unnoticed in the hub's log. ErrorWatch reads every
line the hub writes (stdout and stderr, through LineTee: the log() lines and Python's tracebacks alike), counts the
error lines, and says so on the status topic (sizeup/notify.py STATUS, through Pushes: the topic in effect, never a
name written here) when they spike.

LineTee.write() only writes through and puts whole lines on a queue (SimpleQueue: safe from a finalizer or the
unraisable hook writing to stderr in the middle of a write); the lines are read on the hub-errors thread (drain()).

An error line (classify()):

    a traceback        "Traceback (most recent call last):" ... its last line "OverflowError: ..." (once, a chained
                       one too; the frames name the module: the last sizeup/<module>.py in them)
    an exception name  "aircraft: OverflowError: cannot convert ..." · "aws transcribe FG1: Connection: BlockingIOError"
    ERROR / error:     "transcribe: ERROR ..." · "hub: error: ..."

Not counted: the access log ("http GET /x 500 ...": requests, not the hub's faults), our own lines ("errors:",
"notify:": no feedback loop), warnings without an exception.

A signature is (source, type). Both come from fixed lists, never from the text of a line, so a request path, a
browser's user agent or a name in a message can't become part of a notice (the status topic is public ntfy):

    source   the line's prefix when it is one of SOURCES (sizeup's module names and its known log prefixes:
             "aircraft", "aws transcribe"), or the traceback's sizeup module; anything else is "hub"
    type     a known exception name (Python's built-in ones, the classes sizeup defines, a few common library ones;
             from a traceback: the class Python printed); anything else is "error"

The last message seen (digits stripped, at most MSG_MAX characters) is kept as a sample for Settings > Health (admins)
only: a notice names the source, the type and the count, never a message.

When (tick(), every TICK_S):

    a signature        more than `per_signature` (K, default 10) of it in WINDOW_S (5 min): one notice
                       "Hub errors: aircraft OverflowError ×1,480 in 25 min · since 16:13"
    the total          more than `total` (default 100) error lines of any kind in WINDOW_S, the signatures pushed this
                       hour left out (one fault, one notice): "Hub errors: 240 in 5 min · top hub KeyError ×9"
    at most            one notice per signature (and for the total) per REPEAT_S (1 h)
    back to normal     once every pushed signature has been at or under K for QUIET_S (15 min), and the total under
                       its threshold as long: one "Hub errors back to normal" notice (a slow trickle is normal)

What was pushed when (and what is still alerting) is kept in hub.sqlite (meta "errwatch"): a redeploy doesn't push a
fault that is still going again before its hour is up.

A one-off error (or a few) is never a notice: it shows on Settings > Health with the rest.

Settings > System > Hub error alerts (the CAD settings' SECTION, admins): on / off, K and the total. Read at every
tick: no restart. Off: still counted for Settings > Health, nothing pushed.
"""

from __future__ import annotations

import builtins
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from sizeup import tz

SECTION = "hub_errors"
WINDOW_S = 300.0            # the rate's window: "more than K in 5 min"
REPEAT_S = 3600.0           # one notice per signature (and for the total) per hour
QUIET_S = 900.0             # "back to normal" after this long at or under the thresholds
TICK_S = 30.0
DRAIN_S = 1.0               # the hub-errors thread reads the queued lines this often
DEFAULTS = {"on": True, "per_signature": 10, "total": 100}
LIMITS = {"per_signature": (2, 10000), "total": (5, 50000)}
LABELS = {"on": "Push hub error spikes to the status topic", "per_signature": "Same error, per 5 min",
          "total": "All errors, per 5 min"}
MSG_MAX = 80
MAX_SIGS = 400              # signatures kept; past this a new one is counted as ("hub", "error")
MAX_TIMES = 2 * LIMITS["per_signature"][1]     # timestamps kept per signature: more than the largest K
MAX_ALL = 2 * LIMITS["total"][1]               # ... and of every error line: more than the largest total
MAX_QUEUE = 200000          # lines waiting to be read; past this they are dropped (counted)
PENDING_S = 1.0             # a traceback's last line waits this long for "During handling ..." (chained: once)
TOP = 8                     # Settings > Health: the top signatures shown
META = "errwatch"           # hub.sqlite meta key

TS = re.compile(r"^\s*\d{1,2}:\d\d:\d\d(?:[.,]\d+)?\s+")
EXC = re.compile(r"\b([A-Z][A-Za-z0-9_]{0,38}(?:Error|Exception))\b")
GENERIC = re.compile(r"\bERROR\b|\b[Ee]rror:")
PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9 _./-]{0,40}?):\s")
FRAME = re.compile(r'^\s+File "[^"]*?sizeup/([A-Za-z0-9_/]+)\.py"')
TB_LAST = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)(?::|$)")
IGNORE = ("http", "errors", "notify")      # the access log; our own lines (sizeup/notify.py logs its failures)
PREAMBLE = ("Exception in thread", "Exception occurred during processing", "Exception ignored in")
CHAINED = ("During handling of the above exception", "The above exception was the direct cause")

_PKG = Path(__file__).resolve().parent
MODULES = frozenset(p.stem for p in _PKG.rglob("*.py") if not p.stem.startswith("_"))
# The log prefixes sizeup writes that aren't a module's name ("aws transcribe FG1: ..." -> "aws transcribe").
SOURCES = MODULES | frozenset({
    "aws transcribe", "hub link", "cad", "cad activity", "cloudflare access", "sdr", "adsb", "roadwork", "leader",
    "usage", "layers", "atc vocabulary", "atc talkers", "airport pre-alert", "tts-server", "whisper-server",
    "speech-to-text", "auto-update", "audio", "storage", "page hold", "emergency window", "polly", "readsb", "sizeup",
    "features", "index", "version", "trunk-recorder", "litestream", "access", "health", "hub"})
# Python's own exceptions, the ones sizeup defines, and a few common library ones: the only types a line can name.
KNOWN = frozenset(n for n in dir(builtins) if isinstance(getattr(builtins, n), type)
                  and issubclass(getattr(builtins, n), BaseException)) | frozenset(
    m.group(1) for p in _PKG.rglob("*.py") for m in re.finditer(r"^class (\w+(?:Error|Exception))\b",
                                                               p.read_text(errors="replace"), re.M)) | frozenset({
    "URLError", "HTTPError", "JSONDecodeError", "SSLError", "SSLEOFError", "SSLZeroReturnError", "CalledProcessError",
    "TOMLDecodeError", "ClientError", "BotoCoreError", "NoCredentialsError", "EndpointConnectionError",
    "ReadTimeoutError", "ConnectTimeoutError", "OperationalError", "DatabaseError", "IntegrityError",
    "ProgrammingError", "ZoneInfoNotFoundError", "LZMAError", "ParseError", "ExpatError", "IncompleteReadError"})


def _norm(msg: str) -> str:
    m = re.sub(r"0x[0-9A-Fa-f]+", "0x", msg)
    m = re.sub(r"\d+", "", m)
    return re.sub(r"\s+", " ", m).strip()[:MSG_MAX]


def _source(prefix: str) -> str:
    """"aws transcribe FG1" -> "aws transcribe"; "aircraft" -> "aircraft"; "CAD" -> "cad"; anything not in
    SOURCES -> "hub" (a name in a message never becomes a source)."""
    words = prefix.lower().split()
    if len(words) >= 2 and " ".join(words[:2]) in SOURCES:
        return " ".join(words[:2])
    return words[0] if words and words[0] in SOURCES else "hub"


def _known(name: str) -> str:
    return name if name in KNOWN else "error"


def _first_word(s: str) -> str:
    w = s.split(None, 1)
    return w[0].rstrip(":").lower() if w else ""


def classify(line: str) -> tuple[str, str, str] | None:
    """One log line (not part of a traceback) -> (source, type, message sample), or None when it isn't an error
    line."""
    s = TS.sub("", line.rstrip("\r\n"))
    if not s.strip() or _first_word(s) in IGNORE:
        return None
    m = PREFIX.match(s)
    prefix = m.group(1).strip() if m and not EXC.fullmatch(m.group(1).strip()) else ""
    if prefix and _first_word(prefix) in IGNORE:
        return None
    rest = s[m.end():] if prefix else s
    e = EXC.search(rest)
    if e:
        typ = _known(e.group(1))
        after = rest[e.end():]
        msg = after[1:] if after.startswith(":") and after[1:].strip() else rest
    elif GENERIC.search(rest):
        typ, msg = "error", rest
    else:
        return None
    return (_source(prefix) if prefix else "hub", typ, _norm(msg))


class _Sig:
    __slots__ = ("key", "times", "first", "last", "count", "total", "pushed_at", "alerting", "over_at", "sample")

    def __init__(self, key: tuple[str, str]):
        self.key = key
        self.times: deque = deque(maxlen=MAX_TIMES)    # in the last WINDOW_S
        self.first = self.last = 0.0                    # this episode
        self.count = 0                                  # ... how many in it
        self.total = 0                                  # since the hub started
        self.pushed_at = 0.0                            # its last notice
        self.alerting = False                           # pushed, not yet back to normal
        self.over_at = 0.0                              # its last line while it was over K
        self.sample = ""                                # the last message (Settings > Health only)


def _fmt(n: int) -> str:
    return f"{n:,}"


def _dur(s: float) -> str:
    m = max(1, round(s / 60))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


class ErrorWatch:
    """feed() each line (queued); drain() reads them (the hub-errors thread); tick() decides the notices; panel() for
    Settings > Health. notifier: what Pushes' notifier(STATUS) gives (send, enabled); settings(): the CAD settings'
    SECTION ({} without); db: hub.sqlite's MonitorDb (get / put), where the notices' state is kept."""

    def __init__(self, notifier=None, settings: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, click: str = "", db=None):
        self.notifier, self.settings, self.clock, self.log, self.click = notifier, settings or (lambda: {}), clock, log, click
        self.db = db
        self.sigs: dict[tuple, _Sig] = {}
        self.times: deque = deque(maxlen=MAX_ALL)       # every error line in the last WINDOW_S: (at, signature)
        self.total = 0
        self.dropped = 0                                 # lines not read: the queue was full
        self.started = clock()
        self.total_pushed_at = 0.0
        self.total_alerting = False
        self.total_high = 0.0                            # the last tick the total was over its threshold
        self.pushes: deque = deque(maxlen=20)            # the notices sent: {at, title} (Settings > Health)
        self._cfg = dict(DEFAULTS)                       # the settings last read (kept when they can't be)
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._tb: dict[str, dict] = {}                   # stream -> the traceback being read {module}
        self._pending: dict[str, tuple] = {}             # stream -> (at, key, sample): a traceback's last line
        self._lock = threading.Lock()                    # the counts (drain / tick / panel)
        self._drain_lock = threading.Lock()              # one reader of the queue (_tb, _pending)
        self._load()

    # -- the settings -----------------------------------------------------------------------------------------

    def config(self) -> dict:
        try:
            ov = self.settings() or {}
        except Exception:  # noqa: BLE001 - the CAD's settings unreadable: what they last were
            return dict(self._cfg)
        out = dict(DEFAULTS)
        if isinstance(ov.get("on"), bool):
            out["on"] = ov["on"]
        for k, (lo, hi) in LIMITS.items():
            v = ov.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
        self._cfg = out
        return dict(out)

    # -- kept across a redeploy (hub.sqlite) ------------------------------------------------------------------

    def _load(self) -> None:
        if self.db is None:
            return
        try:
            d = json.loads(self.db.get(META, "") or "{}")
            for row in d.get("sigs") or []:
                src, typ = str(row["source"]), str(row["type"])
                if src not in SOURCES and src != "hub" or (typ not in KNOWN and typ != "error"):
                    continue
                s = self.sigs[(src, typ)] = _Sig((src, typ))
                s.pushed_at, s.alerting = float(row.get("pushed_at") or 0), bool(row.get("alerting"))
                # still alerting: QUIET_S from now to be back to normal (the window is empty after a restart)
                s.over_at = max(float(row.get("over_at") or 0), self.started if s.alerting else 0.0)
            self.total_pushed_at = float(d.get("total_pushed_at") or 0)
            self.total_alerting = bool(d.get("total_alerting"))
            self.total_high = max(float(d.get("total_high") or 0), self.started if self.total_alerting else 0.0)
        except Exception as e:  # noqa: BLE001 - a bad row: start afresh
            self.log(f"errors: saved state not read ({type(e).__name__}): starting afresh")

    def _save(self) -> None:
        if self.db is None:
            return
        now = self.clock()
        with self._lock:
            rows = [{"source": s.key[0], "type": s.key[1], "pushed_at": s.pushed_at, "alerting": s.alerting,
                     "over_at": s.over_at}
                    for s in self.sigs.values() if s.alerting or (s.pushed_at and now - s.pushed_at < REPEAT_S)]
            d = {"sigs": rows, "total_pushed_at": self.total_pushed_at, "total_alerting": self.total_alerting,
                 "total_high": self.total_high}
        try:
            self.db.put(META, json.dumps(d))
        except Exception as e:  # noqa: BLE001 - kept in memory anyway
            self.log(f"errors: state not saved ({type(e).__name__})")

    # -- reading the log --------------------------------------------------------------------------------------

    def feed(self, line: str, stream: str = "out") -> None:
        """One line the hub wrote, queued for drain() (stream: which of its outputs, so a traceback on stderr is
        read whole while stdout goes on). Takes no lock: safe from anywhere, a finalizer included. Never raises."""
        try:
            if self._q.qsize() >= MAX_QUEUE:
                self.dropped += 1
                return
            self._q.put((self.clock(), line, stream))
        except Exception:  # noqa: BLE001 - the watcher must never break the log
            pass

    def drain(self, limit: int = MAX_QUEUE) -> int:
        """Read the queued lines (the hub-errors thread; tick() and panel() too). How many were read."""
        n = 0
        with self._drain_lock:
            while n < limit:
                try:
                    at, line, stream = self._q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                try:
                    self._read(line, stream, at)
                except Exception:  # noqa: BLE001 - one odd line
                    pass
            now = self.clock()
            for stream, (at, key, sample) in list(self._pending.items()):
                if now - at >= PENDING_S:
                    del self._pending[stream]
                    self.record(key, at, sample=sample)
        return n

    def _flush(self, stream: str) -> None:
        p = self._pending.pop(stream, None)
        if p is not None:
            self.record(p[1], p[0], sample=p[2])

    def _read(self, line: str, stream: str, at: float) -> None:
        s = TS.sub("", line.rstrip("\r\n"))
        if stream in self._pending:
            if not s.strip():
                return
            if s.startswith(CHAINED):
                self._pending.pop(stream, None)             # chained: the last exception is the one counted
                return
            self._flush(stream)
        tb = self._tb.get(stream)
        if s.startswith("Traceback (most recent call last)"):
            if len(self._tb) > 1000:                        # threads that died mid-traceback
                self._tb.clear()
            self._tb[stream] = {"module": ""}
            return
        if tb is not None:
            f = FRAME.match(s)
            if f:
                mod = f.group(1).replace("/", ".")
                if mod.rsplit(".", 1)[-1] in MODULES:
                    tb["module"] = mod.rsplit(".", 1)[-1]
                return
            if not s.strip() or s[:1].isspace():
                return                                      # a frame's code line, or a caret line
            del self._tb[stream]                            # its last line: the exception
            e = TB_LAST.match(s)
            name = e.group(1) if e else ""
            last = name.rsplit(".", 1)[-1]
            root = name.split(".", 1)[0]
            typ = last if (last in KNOWN or (root == "sizeup" and "." in name and len(last) <= 40)) else "error"
            msg = s[e.end():].lstrip(": ") if e else s
            self._pending[stream] = (at, (tb["module"] or "hub", typ), _norm(msg))
            return
        if s.startswith(PREAMBLE) or s.startswith(CHAINED):
            return
        sig = classify(s)
        if sig is not None:
            self.record(sig[:2], at, sample=sig[2])

    def record(self, key: tuple, now: float | None = None, sample: str = "") -> None:
        key = tuple(key[:2])
        now = self.clock() if now is None else now
        with self._lock:
            sig = self.sigs.get(key)
            if sig is None:
                if len(self.sigs) >= MAX_SIGS:
                    key = ("hub", "error")
                    sig = self.sigs.get(key)
                if sig is None:
                    sig = self.sigs[key] = _Sig(key)
            if not sig.count or now - sig.last >= QUIET_S:
                sig.first, sig.count = now, 0
            sig.count += 1
            sig.total += 1
            sig.last = now
            if sample:
                sig.sample = sample
            sig.times.append(now)
            self.times.append((now, sig.key))
            self.total += 1

    # -- deciding ---------------------------------------------------------------------------------------------

    @staticmethod
    def _trim(d: deque, since: float) -> int:
        while d and d[0] < since:
            d.popleft()
        return len(d)

    def _trim_all(self, since: float) -> int:
        d = self.times
        while d and d[0][0] < since:
            d.popleft()
        return len(d)

    def tick(self, now: float | None = None) -> list[tuple[str, str]]:
        """The notices due now, sent (when on and a status topic is in effect): [(title, body)]."""
        self.drain()
        now = self.clock() if now is None else now
        cfg = self.config()
        k, limit = cfg["per_signature"], cfg["total"]
        out: list[tuple[str, str]] = []
        changed = False
        with self._lock:
            since = now - WINDOW_S
            n5 = self._trim_all(since)
            for sig in self.sigs.values():
                if self._trim(sig.times, since) > k:
                    sig.over_at = sig.times[-1]             # its last line while over K: quiet from there
            # a signature over K
            for sig in sorted(self.sigs.values(), key=lambda x: -len(x.times)):
                if len(sig.times) > k and now - sig.pushed_at >= REPEAT_S:
                    sig.pushed_at, sig.alerting = now, True
                    out.append(self._spike(sig, now, k))
            # the total over its threshold, not counting the signatures pushed this hour (one fault, one notice)
            pushed = {s.key for s in self.sigs.values() if s.pushed_at and now - s.pushed_at < REPEAT_S}
            rest = sum(1 for _, key in self.times if key not in pushed) if pushed else n5
            if rest > limit:
                self.total_high = now
            if rest > limit and now - self.total_pushed_at >= REPEAT_S:
                self.total_pushed_at, self.total_alerting = now, True
                top = max(self.sigs.values(), key=lambda x: len(x.times), default=None)
                title = f"Hub errors: {_fmt(n5)} in 5 min" + (
                    f" · top {self._name(top)} ×{_fmt(len(top.times))}" if top is not None and top.times else "")
                out.append((title, f"More than {_fmt(limit)} error lines in 5 min. Details: Settings > Health."))
            # back to normal: every pushed signature at or under K (a trickle is normal), the total under its own
            calm = [s for s in self.sigs.values() if s.alerting and now - s.over_at >= QUIET_S]
            loud = [s for s in self.sigs.values() if s.alerting and now - s.over_at < QUIET_S]
            total_calm = self.total_alerting and now - self.total_high >= QUIET_S
            if (calm or total_calm) and not loud and (not self.total_alerting or total_calm):
                parts = [f"{self._name(s)} ×{_fmt(s.count)} in {_dur(s.last - s.first)}, last {tz.hhmm(s.last)}"
                         for s in sorted(calm, key=lambda x: -x.count)[:3]]
                title = "Hub errors back to normal" + (f": {self._name(calm[0])}" if len(calm) == 1 else "")
                for s in calm:
                    s.alerting, s.count = False, 0          # the next one starts a new episode
                self.total_alerting = False
                out.append((title, ("; ".join(parts) + ". " if parts else "") + f"Quiet {_dur(QUIET_S)}."))
            for title, body in out:
                self.pushes.append({"at": now, "title": title})
                changed = True
        if changed:
            self._save()
        if out:
            n = self.notifier
            on = cfg["on"] and n is not None and getattr(n, "enabled", False)
            for title, body in out:
                self.log(f"errors: {title}" + ("" if on else " (not pushed: " + ("off in Settings" if not cfg["on"]
                                                                                   else "no status topic") + ")"))
                if on:
                    back = title.startswith("Hub errors back")
                    n.send(title, body, priority=3 if back else 4,
                           tags=("white_check_mark" if back else "warning", "sizeup"), click=self.click)
        return out

    @staticmethod
    def _name(sig: _Sig) -> str:
        return f"{sig.key[0]} {sig.key[1]}" if sig.key[1] != "error" else f"{sig.key[0]} error"

    def _spike(self, sig: _Sig, now: float, k: int) -> tuple[str, str]:
        title = f"Hub errors: {self._name(sig)} ×{_fmt(sig.count)} in {_dur(now - sig.first)} · since {tz.hhmm(sig.first)}"
        return title, f"{_fmt(len(sig.times))} in the last 5 min (more than {k}). Details: Settings > Health."

    def run(self, stop: threading.Event) -> None:
        """The hub-errors thread: reads the queued lines every DRAIN_S, decides every TICK_S."""
        last = time.monotonic()
        while not stop.wait(DRAIN_S):
            try:
                self.drain()
                if time.monotonic() - last >= TICK_S:
                    last = time.monotonic()
                    self.tick()
            except Exception as e:  # noqa: BLE001 - the watcher must never die
                self.log(f"errors: {type(e).__name__}")

    # -- Settings > Health (admins) -----------------------------------------------------------------------------

    def panel(self, now: float | None = None) -> dict:
        self.drain()
        now = self.clock() if now is None else now
        cfg = self.config()
        with self._lock:
            since = now - WINDOW_S
            n5 = self._trim_all(since)
            rows = []
            for s in self.sigs.values():
                self._trim(s.times, since)
            for s in sorted((s for s in self.sigs.values() if s.total),
                            key=lambda x: (-len(x.times), -x.last))[:TOP]:
                rows.append({"source": s.key[0], "type": s.key[1], "message": s.sample, "last5": len(s.times),
                             "count": s.count, "first": s.first, "last": s.last, "total": s.total,
                             "alerting": s.alerting, "pushed_at": s.pushed_at or None})
            return {"on": cfg["on"], "per_signature": cfg["per_signature"], "total_limit": cfg["total"],
                    "window_min": WINDOW_S / 60, "repeat_min": REPEAT_S / 60, "quiet_min": QUIET_S / 60,
                    "last5": n5, "per_min": round(n5 / (WINDOW_S / 60), 1), "total": self.total, "since": self.started,
                    "signatures": sum(1 for s in self.sigs.values() if s.total), "top": rows,
                    "pushes": list(self.pushes)[::-1], "dropped": self.dropped,
                    "alerting": self.total_alerting or any(s.alerting for s in self.sigs.values())}


class LineTee:
    """A text stream that writes through to the real one and queues every whole line for watch (feed()). Each thread
    has its own unfinished line (print() writes the text and the newline separately), so nothing here takes a lock:
    a write from a finalizer or the unraisable hook in the middle of one can't deadlock."""

    def __init__(self, stream, watch: ErrorWatch, name: str):
        self._stream, self._watch, self._name = stream, watch, name
        self._local = threading.local()
        self._lock = threading.RLock()       # install(): swapping the watch

    def write(self, s) -> int:
        n = self._stream.write(s)
        loc = self._local
        if getattr(loc, "busy", False) or not isinstance(s, str):
            return n                          # re-entered (a finalizer writing mid-write): through only
        loc.busy = True
        try:
            buf = getattr(loc, "buf", "") + s
            if "\n" in buf:
                *lines, buf = buf.split("\n")
                w, key = self._watch, f"{self._name}:{threading.get_ident()}"     # a traceback per thread
                for line in lines:
                    w.feed(line, key)
            loc.buf = buf if len(buf) <= 65536 else ""
        except Exception:  # noqa: BLE001 - the log comes first
            pass
        finally:
            loc.busy = False
        return n

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install(watch: ErrorWatch) -> None:
    """Every line the process writes to stdout and stderr, through watch (the hub: sizeup/app.py main). A stream that
    is None (no console) is left alone."""
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name)
        if cur is None:
            continue
        if isinstance(cur, LineTee):
            with cur._lock:
                cur._watch = watch
        else:
            setattr(sys, name, LineTee(cur, watch, name))
