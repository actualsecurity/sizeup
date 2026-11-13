"""What may leave the station in a log line (station logs to CloudWatch): the scrub, the level and the repeat limiter.

Stdlib only. Used by the station's shipper (sizeup/cloud/logship.py) on every line before it is spooled, and again by
the hub (sizeup/cloud/cwlogs.py) before anything is written to CloudWatch: two passes, either one enough.

    scrub(line)          the line with credentials redacted, or None: DROP it (fail closed). Dropped:
                           - any value registered with secret() (the station token, the Access service token, every
                             adapter credential read through sizeup/procenv.py or fetched from SSM by
                             sizeup/cloud/adapterssm.py), wherever it appears in the line
                           - a private key, or a password-like word left without a redacted value after the patterns
                         Redacted (the line kept): key=value / key: value pairs whose key names a credential, Bearer
                         and Basic auth, a URL's user:password@, credential query parameters, AWS access key ids and
                         secret-shaped strings, JWTs, long mixed-case tokens, and every registered credential NAME
                         (an adapter's credential names say which county system it logs in to)
    level(priority, msg) "debug" | "info" | "warning" | "error": journald's PRIORITY, raised by the words in the
                         line (sizeup prints everything at info: "Traceback", "failed" make it an error)
    Repeats              the same line (digits and hex ignored) at most BURST times per WINDOW_S per unit; then one
                         "[repeated N× in 10 min]" line for the rest

Never logs a value, a name or a pattern match itself.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict

LEVELS = ("debug", "info", "warning", "error")
REDACTED = "[redacted]"
MIN_SECRET = 6                 # a registered value shorter than this is too common to match on ("admin", "1234")
MAX_LINE = 2000                # a longer line is cut (a traceback's frame lines come one per journal entry)

_lock = threading.Lock()
_values: set[str] = set()
_names: set[str] = set()

# key = value / key: value / "key": "value", the key naming a credential
_KEY = (r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key"
        r"|client[_-]?secret|client[_-]?id|session(?:[_-]?id)?|cookie|set-cookie|auth(?:orization)?|credentials?"
        r"|x-amz-security-token|x-amz-signature|cf-access-client-(?:id|secret)|cf_authorization|sig(?:nature)?)")
_KV = re.compile(r"(?i)(\b[\w.-]*" + _KEY + r"[\w.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&\"'}]+)")
_AUTH = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)[^/\s:@]+:[^/\s@]+@")
_QUERY = re.compile(r"(?i)([?&](?:" + _KEY + r"|code|key|x-amz-[a-z-]+)=)[^&\s#\"']+")
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# 32+ characters of a token alphabet with upper case, lower case and a digit: a secret's shape (a git sha, a uuid or a
# hex digest is lower case only and stays)
_LONG = re.compile(r"(?<![A-Za-z0-9/+_=-])[A-Za-z0-9/+_=-]{32,}(?![A-Za-z0-9/+_=-])")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|PuTTY-User-Key-File|\bssh-(?:rsa|ed25519) AAAA")
# after the patterns: a password-like word still in the line, not right before a redaction: dropped
_LEFT = re.compile(r"(?i)\b(?:pass(?:word|wd|phrase)|pwd|secret[_ -]?key|client[_ -]?secret|private[_ -]?key)\b"
                   r"(?![\"']?\s*[:=]\s*[\"']?\[redacted\])")


def secret(*values: str) -> None:
    """Values that must never leave the station in a log line (a line with one is dropped). Kept in memory only."""
    with _lock:
        for v in values:
            v = (v or "").strip()
            if len(v) >= MIN_SECRET:
                _values.add(v)


def secret_name(*names: str) -> None:
    """Credential NAMES to redact wherever they appear (an adapter's names say what it logs in to)."""
    with _lock:
        for n in names:
            n = (n or "").strip()
            if len(n) >= 3:
                _names.add(n)


def forget() -> None:
    """(tests) Nothing registered."""
    with _lock:
        _values.clear()
        _names.clear()


def registered() -> tuple[int, int]:
    """How many values and names are registered (counts only: the station's status)."""
    with _lock:
        return len(_values), len(_names)


def _long(m: re.Match) -> str:
    s = m.group(0)
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        return REDACTED
    return s


def _aws_secret(s: str) -> str:
    """A 40-character AWS secret access key shape (mixed case, or / +), never a 40-hex git sha."""
    def one(m: re.Match) -> str:
        t = m.group(0)
        return REDACTED if (re.search(r"[A-Z/+]", t) and re.search(r"[a-z]", t)) else t
    return re.sub(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])", one, s)


def scrub(line: str) -> str | None:
    """The line safe to send, or None (drop it). See the module doc."""
    if line is None:
        return None
    s = str(line).replace("\r", " ").replace("\0", " ")
    if len(s) > MAX_LINE:
        s = s[:MAX_LINE] + " …"
    with _lock:
        values, names = tuple(_values), tuple(_names)
    for v in values:
        if v in s:
            return None
    if _PRIVATE_KEY.search(s):
        return None
    for n in sorted(names, key=len, reverse=True):
        if n in s:
            s = re.sub(re.escape(n), "[credential]", s)
    s = _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", s)
    s = _AUTH.sub(lambda m: m.group(1) + " " + REDACTED, s)
    s = _QUERY.sub(lambda m: m.group(1) + REDACTED, s)
    s = _KV.sub(lambda m: m.group(1) + REDACTED, s)
    s = _AWS_KEY_ID.sub("[aws-key]", s)
    s = _JWT.sub(REDACTED, s)
    s = _aws_secret(s)
    s = _LONG.sub(_long, s)
    if _LEFT.search(s) and not _benign_password(s):
        return None
    return s


# Lines that name a password without carrying one: kept (sizeup's own wording; anything else naming one is dropped).
_BENIGN = re.compile(r"(?i)\bpassword (?:is )?(?:set|missing|not set|saved|changed|refused|accepted|version|versions"
                     r"|v\d+|from ssm|unchanged|rotated|required|expired|ok)\b|\bpassword\(s\)|\bno password\b"
                     r"|\[redacted\]")


def _benign_password(s: str) -> bool:
    """Every password-like word in the line is part of a phrase that carries no value."""
    rest = _BENIGN.sub("", s)
    return not _LEFT.search(rest)


# -- level -------------------------------------------------------------------------------------------------------

_ERR = re.compile(r"(?i)\b(?:traceback|exception|error|errors|failed|failure|fatal|critical|crash(?:ed)?|panic"
                  r"|segmentation fault|core dumped|killed)\b")
_WARN = re.compile(r"(?i)\b(?:warn(?:ing)?|refused|timed? ?out|timeout|retry(?:ing)?|offline|backing off|"
                   r"giving up|stalled|stuck|dropped|degraded|restart(?:ing|ed)?|not reachable|unreachable)\b")
_DEBUG = re.compile(r"(?i)^\s*(?:\d\d:\d\d:\d\d\s+)?(?:debug\b|\[debug\]|dbg:)")


def level(priority, msg: str) -> str:
    """journald PRIORITY (0-7) and the line's words -> debug | info | warning | error."""
    try:
        p = int(priority)
    except (TypeError, ValueError):
        p = 6
    lv = "error" if p <= 3 else "warning" if p == 4 else "debug" if p >= 7 else "info"
    if lv in ("info", "debug") and msg:
        if _DEBUG.search(msg):
            return "debug"
        if _ERR.search(msg):
            return "error"
        if _WARN.search(msg):
            return "warning"
    return lv


def at_least(lv: str, threshold: str) -> bool:
    """lv is at or above threshold (an unknown threshold: info)."""
    t = threshold if threshold in LEVELS else "info"
    return LEVELS.index(lv if lv in LEVELS else "info") >= LEVELS.index(t)


# -- repeats -----------------------------------------------------------------------------------------------------

_NORM = re.compile(r"0x[0-9a-fA-F]+|[0-9a-f]{7,}|\d+(?:\.\d+)?")


def shape(msg: str) -> str:
    """The line with its numbers and hex ids folded: "#"."""
    return _NORM.sub("#", msg)[:300]


class Repeats:
    """At most burst lines of one shape per unit per window_s; the rest counted, then one summary line."""

    def __init__(self, burst: int = 5, window_s: float = 600.0, max_keys: int = 4000):
        self.burst, self.window_s, self.max_keys = burst, window_s, max_keys
        self._seen: OrderedDict[tuple[str, str], list] = OrderedDict()   # key -> [window start, n, last line, level]
        self.suppressed = 0

    def allow(self, unit: str, msg: str, lv: str, at: float) -> list[tuple[float, str, str, str]]:
        """What to send for this line: [(at, unit, level, msg)] (itself, a summary of an earlier window first, or
        nothing when it is a repeat past the burst)."""
        key = (unit, shape(msg))
        out: list[tuple[float, str, str, str]] = []
        e = self._seen.get(key)
        if e is not None and at - e[0] >= self.window_s:
            if e[1] > self.burst:
                out.append((at, unit, e[3], f"[repeated {e[1] - self.burst}× in {self.window_s / 60:g} min] {e[2]}"))
            e = None
        if e is None:
            e = [at, 0, msg, lv]
            self._seen[key] = e
        self._seen.move_to_end(key)
        e[1] += 1
        e[2], e[3] = msg, lv
        if e[1] <= self.burst:
            out.append((at, unit, lv, msg))
        else:
            self.suppressed += 1
        while len(self._seen) > self.max_keys:
            self._seen.popitem(last=False)
        return out

    def flush(self, now: float) -> list[tuple[float, str, str, str]]:
        """The summaries of windows that have ended (each once)."""
        out = []
        for key, e in list(self._seen.items()):
            if now - e[0] >= self.window_s:
                if e[1] > self.burst:
                    out.append((now, key[0], e[3], f"[repeated {e[1] - self.burst}× in {self.window_s / 60:g} min] {e[2]}"))
                del self._seen[key]
        return out
