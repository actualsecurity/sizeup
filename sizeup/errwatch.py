"""The hub's own error spikes, pushed to the status topic (#285).

Today about 1,500 "aircraft: OverflowError" lines in 25 min went unnoticed in the hub's log. ErrorWatch reads every
line the hub writes (stdout and stderr, through LineTee: the log() lines and Python's tracebacks alike), counts the
error lines, and says so on the status topic (sizeup/notify.py STATUS, through Pushes: the topic in effect, never a
name written here) when they spike.

An error line (classify()):

    a traceback        "Traceback (most recent call last):" ... its last line "OverflowError: ..." (once; the frames
                       name the module: the last sizeup/<module>.py in them)
    an exception name  "aircraft: OverflowError: cannot convert ..." · "aws transcribe FG1: Connection: BlockingIOError"
                       (any word ending in Error or Exception)
    ERROR / error:     "transcribe: ERROR ..." · "hub: error: ..."

Not counted: the access log ("http GET /x 500 ...": requests, not the hub's faults), our own lines ("errors:",
"notify:": no feedback loop), warnings without an exception.

A signature is (source, exception type, message): the source is the log line's prefix ("aircraft", "aws
transcribe": its lowercase words, at most two) or the traceback's module; the message is normalised (digits stripped,
hex and spaces collapsed, at most MSG_MAX characters). The message is for Settings > Health only (admins): a
notice names the source and the type, never the message, so nothing of an incident, a caller, the county's CAD or a
credential that an error message may carry reaches the status topic (public ntfy).

When (tick(), every TICK_S):

    a signature        more than `per_signature` (K, default 10) of it in WINDOW_S (5 min): one notice
                       "Hub errors: aircraft OverflowError ×1,480 in 25 min · since 16:13"
    the total          more than `total` (default 100) error lines of any kind in WINDOW_S, the signatures already
                       pushed left out (one fault, one notice): one notice "Hub errors: 240 in 5 min · top hub KeyError ×9"
    at most            one notice per signature (and for the total) per REPEAT_S (1 h)
    back to normal     once every signature that was pushed has been quiet QUIET_S (15 min) and the total is under
                       its threshold as long: one "Hub errors back to normal" notice for them

A one-off error (or a few) is never a notice: it shows on Settings > Health with the rest.

Settings > System > Hub error alerts (the CAD settings' SECTION, admins): on / off, K and the total. Read at every
tick: no restart. Off: still counted for Settings > Health, nothing pushed.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from collections import deque
from typing import Callable

from sizeup import tz

SECTION = "hub_errors"
WINDOW_S = 300.0            # the rate's window: "more than K in 5 min"
REPEAT_S = 3600.0           # one notice per signature (and for the total) per hour
QUIET_S = 900.0             # "back to normal" after this long without one
TICK_S = 30.0
DEFAULTS = {"on": True, "per_signature": 10, "total": 100}
LIMITS = {"per_signature": (2, 10000), "total": (5, 100000)}
LABELS = {"on": "Push hub error spikes to the status topic", "per_signature": "One error, more than (in 5 min)",
          "total": "All errors, more than (in 5 min)"}
MSG_MAX = 80
MAX_SIGS = 500              # signatures kept (the least recent dropped past this)
MAX_TIMES = 20000           # timestamps kept per window deque
TOP = 8                     # Settings > Health: the top signatures shown

TS = re.compile(r"^\s*\d{1,2}:\d\d:\d\d(?:[.,]\d+)?\s+")
EXC = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b")
GENERIC = re.compile(r"\bERROR\b|\b[Ee]rror:")
PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9 _./-]{0,40}?):\s")
FRAME = re.compile(r'^\s+File "[^"]*?sizeup/([A-Za-z0-9_/]+)\.py"')
IGNORE = ("http", "errors", "notify")      # the access log; our own lines (sizeup/notify.py logs its failures)
PREAMBLE = ("Exception in thread", "Exception occurred during processing", "Exception ignored in")
CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


def _norm(msg: str) -> str:
    m = re.sub(r"0x[0-9A-Fa-f]+", "0x", msg)
    m = re.sub(r"\d+", "", m)
    return re.sub(r"\s+", " ", m).strip()[:MSG_MAX]


def _source(prefix: str) -> str:
    """"aws transcribe FG1" -> "aws transcribe"; "aircraft" -> "aircraft"; "CAD" -> "cad"."""
    words = []
    for w in prefix.split():
        if not re.fullmatch(r"[a-z][a-z_-]*", w):
            break
        words.append(w)
    return " ".join(words[:2]) or prefix.split()[0].lower()[:24]


def classify(line: str) -> tuple[str, str, str] | None:
    """One log line (not part of a traceback) -> (source, type, message), or None when it isn't an error line."""
    s = TS.sub("", line.rstrip("\r\n"))
    if not s.strip():
        return None
    m = PREFIX.match(s)
    prefix = m.group(1).strip() if m and not EXC.fullmatch(m.group(1).strip()) else ""
    if prefix.split(" ")[0].lower() in IGNORE:
        return None
    rest = s[m.end():] if prefix else s
    e = EXC.search(rest)
    if e:
        typ = e.group(1)
        after = rest[e.end():]
        msg = after[1:] if after.startswith(":") and after[1:].strip() else rest
    elif GENERIC.search(rest):
        typ, msg = "error", rest
    else:
        return None
    return (_source(prefix) if prefix else "hub", typ, _norm(msg))


class _Sig:
    __slots__ = ("key", "times", "first", "last", "count", "total", "pushed_at", "alerting")

    def __init__(self, key: tuple[str, str, str]):
        self.key = key
        self.times: deque = deque(maxlen=MAX_TIMES)   # in the last WINDOW_S
        self.first = self.last = 0.0                    # this episode (since it was last quiet QUIET_S)
        self.count = 0                                  # ... how many in it
        self.total = 0                                  # since the hub started
        self.pushed_at = 0.0                            # its last notice
        self.alerting = False                           # pushed, not yet back to normal


def _fmt(n: int) -> str:
    return f"{n:,}"


def _dur(s: float) -> str:
    m = max(1, round(s / 60))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


class ErrorWatch:
    """feed() each line; tick() decides the notices; panel() for Settings > Health. notifier: what Pushes'
    notifier(STATUS) gives (send, enabled); settings(): the CAD settings' SECTION ({} without)."""

    def __init__(self, notifier=None, settings: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, click: str = ""):
        self.notifier, self.settings, self.clock, self.log, self.click = notifier, settings or (lambda: {}), clock, log, click
        self.sigs: dict[tuple, _Sig] = {}
        self.times: deque = deque(maxlen=MAX_TIMES * 5)  # every error line in the last WINDOW_S
        self.total = 0
        self.started = clock()
        self.total_pushed_at = 0.0
        self.total_alerting = False
        self.total_high = 0.0                            # the last tick the total was over its threshold
        self.pushes: deque = deque(maxlen=20)            # the notices sent: {at, title} (Settings > Health)
        self._tb: dict[str, dict] = {}                   # stream -> the traceback being read {module}
        self._lock = threading.Lock()

    # -- the settings -----------------------------------------------------------------------------------------

    def config(self) -> dict:
        try:
            ov = self.settings() or {}
        except Exception:  # noqa: BLE001 - the CAD's settings unreadable: the defaults
            ov = {}
        out = dict(DEFAULTS)
        if isinstance(ov.get("on"), bool):
            out["on"] = ov["on"]
        for k, (lo, hi) in LIMITS.items():
            v = ov.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
        return out

    # -- reading the log --------------------------------------------------------------------------------------

    def feed(self, line: str, stream: str = "out") -> None:
        """One line the hub wrote (stream: which of its outputs, so a traceback on stderr is read whole while
        stdout goes on). Never raises."""
        try:
            self._feed(line, stream)
        except Exception:  # noqa: BLE001 - the watcher must never break the log
            pass

    def _feed(self, line: str, stream: str) -> None:
        s = TS.sub("", line.rstrip("\r\n"))
        tb = self._tb.get(stream)
        if s.startswith("Traceback (most recent call last)"):
            self._tb[stream] = {"module": ""}
            return
        if tb is not None:
            f = FRAME.match(s)
            if f:
                tb["module"] = f.group(1).replace("/", ".")
                return
            if not s.strip() or s[:1].isspace():
                return                                      # a frame's code line, or a caret line
            if s.startswith(CHAINED):
                return
            del self._tb[stream]                            # its last line: the exception
            e = EXC.match(s) or re.match(r"([A-Za-z_][A-Za-z0-9_.]*)(?::|$)", s)
            typ = (e.group(1).rsplit(".", 1)[-1] if e else "Traceback")
            msg = s[e.end():].lstrip(": ") if e else s
            self.record((tb["module"] or "traceback", typ, _norm(msg)))
            return
        if s.startswith(PREAMBLE) or s.startswith(CHAINED):
            return
        sig = classify(s)
        if sig is not None:
            self.record(sig)

    def record(self, key: tuple[str, str, str], now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            sig = self.sigs.get(key)
            if sig is None:
                if len(self.sigs) >= MAX_SIGS:
                    old = min(self.sigs.values(), key=lambda x: (x.alerting, x.last))
                    del self.sigs[old.key]
                sig = self.sigs[key] = _Sig(key)
            if not sig.count or now - sig.last >= QUIET_S:
                sig.first, sig.count = now, 0
            sig.count += 1
            sig.total += 1
            sig.last = now
            sig.times.append(now)
            self.times.append(now)
            self.total += 1

    # -- deciding ---------------------------------------------------------------------------------------------

    @staticmethod
    def _trim(d: deque, since: float) -> int:
        while d and d[0] < since:
            d.popleft()
        return len(d)

    def tick(self, now: float | None = None) -> list[tuple[str, str]]:
        """The notices due now, sent (when on and a status topic is in effect): [(title, body)]."""
        now = self.clock() if now is None else now
        cfg = self.config()
        k, limit = cfg["per_signature"], cfg["total"]
        out: list[tuple[str, str]] = []
        with self._lock:
            since = now - WINDOW_S
            n5 = self._trim(self.times, since)
            for sig in self.sigs.values():
                self._trim(sig.times, since)
            # a signature over K
            for sig in sorted(self.sigs.values(), key=lambda x: -len(x.times)):
                if len(sig.times) > k and now - sig.pushed_at >= REPEAT_S:
                    sig.pushed_at, sig.alerting = now, True
                    out.append(self._spike(sig, now, k))
            # the total over its threshold, not counting the signatures pushed this hour (one fault, one notice)
            rest = n5 - sum(len(s.times) for s in self.sigs.values() if s.pushed_at and now - s.pushed_at < REPEAT_S)
            if rest > limit:
                self.total_high = now
            if rest > limit and now - self.total_pushed_at >= REPEAT_S:
                self.total_pushed_at, self.total_alerting = now, True
                top = max(self.sigs.values(), key=lambda x: len(x.times), default=None)
                title = f"Hub errors: {_fmt(n5)} in 5 min" + (
                    f" · top {self._name(top)} ×{_fmt(len(top.times))}" if top is not None and top.times else "")
                out.append((title, f"More than {_fmt(limit)} error lines in 5 min. Details: Settings > Health."))
            # back to normal
            calm = [s for s in self.sigs.values() if s.alerting and now - s.last >= QUIET_S]
            loud = [s for s in self.sigs.values() if s.alerting and now - s.last < QUIET_S]
            total_calm = self.total_alerting and now - self.total_high >= QUIET_S
            if (calm or total_calm) and not loud and (not self.total_alerting or total_calm):
                parts = [f"{self._name(s)} ×{_fmt(s.count)} in {_dur(s.last - s.first)}, last {tz.hhmm(s.last)}"
                         for s in sorted(calm, key=lambda x: -x.count)[:3]]
                for s in calm:
                    s.alerting = False
                self.total_alerting = False
                out.append(("Hub errors back to normal" + (f": {self._name(calm[0])}" if len(calm) == 1 else ""),
                            ("; ".join(parts) + ". " if parts else "") + f"Quiet {_dur(QUIET_S)}."))
            for title, body in out:
                self.pushes.append({"at": now, "title": title})
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
        while not stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the watcher must never die
                self.log(f"errors: {type(e).__name__}")

    # -- Settings > Health (admins) -----------------------------------------------------------------------------

    def panel(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        cfg = self.config()
        with self._lock:
            since = now - WINDOW_S
            n5 = self._trim(self.times, since)
            rows = []
            for s in self.sigs.values():
                self._trim(s.times, since)
            for s in sorted(self.sigs.values(), key=lambda x: (-len(x.times), -x.last))[:TOP]:
                rows.append({"source": s.key[0], "type": s.key[1], "message": s.key[2], "last5": len(s.times),
                             "count": s.count, "first": s.first, "last": s.last, "total": s.total,
                             "alerting": s.alerting, "pushed_at": s.pushed_at or None})
            return {"on": cfg["on"], "per_signature": cfg["per_signature"], "total_limit": cfg["total"],
                    "window_min": WINDOW_S / 60, "repeat_min": REPEAT_S / 60, "quiet_min": QUIET_S / 60,
                    "last5": n5, "per_min": round(n5 / (WINDOW_S / 60), 1), "total": self.total, "since": self.started,
                    "signatures": len(self.sigs), "top": rows, "pushes": list(self.pushes)[::-1],
                    "alerting": self.total_alerting or any(s.alerting for s in self.sigs.values())}


class LineTee:
    """A text stream that writes through to the real one and hands every whole line to watch.feed()."""

    def __init__(self, stream, watch: ErrorWatch, name: str):
        self._stream, self._watch, self._name = stream, watch, name
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s) -> int:
        n = self._stream.write(s)
        try:
            with self._lock:
                self._buf += s if isinstance(s, str) else ""
                if "\n" not in self._buf:
                    if len(self._buf) > 65536:
                        self._buf = ""
                    return n
                *lines, self._buf = self._buf.split("\n")
            for line in lines:
                self._watch.feed(line, self._name)
        except Exception:  # noqa: BLE001 - the log comes first
            pass
        return n

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install(watch: ErrorWatch) -> None:
    """Every line the process writes to stdout and stderr, through watch (the hub: sizeup/app.py main)."""
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name)
        if isinstance(cur, LineTee):
            cur._watch = watch
        else:
            setattr(sys, name, LineTee(cur, watch, name))
