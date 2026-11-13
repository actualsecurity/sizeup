"""What may leave the station in a log line (station logs to CloudWatch): the scrub, the level and the repeat limiter.

Stdlib only. The station's shipper (sizeup/cloud/logship.py) scrubs every line before it is spooled, knowing the
station's credential VALUES (registered here at runtime). The hub (sizeup/cloud/cwlogs.py) runs the same patterns again
before anything is written to CloudWatch and before a line is shown, but knows only its own values (its tokens and
secrets): the station's pass is the one that drops a line with a known value; the hub's is a second pattern pass.

    scrub(line)          the line with credentials redacted, or None: DROP it (fail closed). In order:
                           1. normalised: NFKC (fullwidth letters become ASCII), zero-width and format characters gone
                           2. DROPPED when it holds a registered value in any of its forms (as is, URL-encoded, JSON-,
                              HTML- or repr-escaped, base64, base64 of user:password, with or without "Basic "):
                              checked on the WHOLE line, before any cut. Short values (from the credstore) match as a
                              whole word only
                           3. dropped: a private key
                           4. redacted, the WHOLE value (to the end of the quoted string, escapes included, or of the
                              token): key=value / key: value / "key": "value" whose key names a credential (password,
                              pw, p, pin, pass, token, secret, key, cookie, session, auth, Value (an SSM echo) ...), a
                              registered credential name as a key, -p / --password / -u / --user on a command line,
                              Bearer / Basic / Digest auth, user:password@host (with or without a scheme), user:password
                              after login / auth / credential words, ('user', 'password') tuples after them, credential
                              query parameters, AWS key ids and secrets, JWTs, long mixed-case tokens
                           5. registered credential NAMES replaced by [credential] (an adapter's names say which county
                              system it logs in to), after the patterns so NAME=value is redacted first
                           6. dropped: a password-like word (password, passwd, passphrase, pwd, pw, pin, pass, secret)
                              still followed by anything but a redaction, punctuation or the end of the line
    level(priority, msg) "debug" | "info" | "warning" | "error": journald's PRIORITY, raised by the words in the
                         line (sizeup prints everything at info: "Traceback", "failed" make it an error)
    Repeats              the same line (digits and hex ignored) at most BURST times per WINDOW_S per unit; then one
                         "[repeated N× in 10 min]" line for the rest

Never logs a value, a name or a pattern match itself.
"""

from __future__ import annotations

import base64
import html
import json
import re
import threading
import unicodedata
import urllib.parse
from collections import OrderedDict

LEVELS = ("debug", "info", "warning", "error")
REDACTED = "[redacted]"
MIN_SECRET = 6                 # a registered value shorter than this is too common to match on ("admin", "1234") ...
MIN_SHORT = 2                  # ... unless it is a credential from the credstore: then as a whole word, down to this
MAX_LINE = 2000                # a longer line is cut, after the values were looked for in all of it

_lock = threading.Lock()
_values: set[str] = set()      # every form of every registered value
_short: set[str] = set()       # short credstore values: whole words only
_names: set[str] = set()

_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u180e]")

# a value: a quoted string to its closing quote (escapes inside it), else the whole token
_VAL = r"(?!\[(?:redacted|credential)\])(\"(?:\\.|[^\"\\])*\"?|'(?:\\.|[^'\\])*'?|\S+)"
_KEY = (r"(?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key"
        r"|client[_-]?secret|client[_-]?id|session(?:[_-]?id)?|cookie|set-cookie|auth(?:orization)?|credentials?"
        r"|x-amz-security-token|x-amz-signature|cf-access-client-(?:id|secret)|cf_authorization|sig(?:nature)?)")
_KV = re.compile(r"(?i)(\b[\w.-]*" + _KEY + r"[\w.-]*[\"']?\s*[:=]\s*)" + _VAL)
_SHORT_KV = re.compile(r"(?i)((?<![\w.-])[\"']?(?:p|pw|pin|pwd|pass|passwd)[\"']?\s*[:=]\s*)" + _VAL)
_SSM = re.compile(r"((?<![\w.-])[\"']?Value[\"']?\s*[:=]\s*)" + _VAL)            # an SSM parameter echoed
_CLI = re.compile(r"(?i)((?<!\S)(?:-p|-u|--user|--pass(?:word)?|--passwd|--token|--secret|--auth|--credentials?)"
                  r"(?:\s+|=))" + _VAL)
_CLI_P = re.compile(r"(?<!\S)(-p)(?=\S)(\S+)")                                  # mysql-style -pPASSWORD
_AUTH = re.compile(r"(?i)\b(bearer|basic|digest)\s+[^\s,;]+")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)?[^/\s:@'\"]+:[^/\s@'\"]+@(?=[\w.\[-])")
_AFTER_AUTH = re.compile(r"(?i)\b(?:credentials?|creds|login|log-?on|logged in|sign(?:ed)?[ -]?in|auth\w*|user(?:name)?)\b")
_PAIR = re.compile(r"(?<![\w/])([\w.+@-]+):(?!\d{2}\b)([^\s,;)\]}\"']+)")         # user:password (not 12:30)
_TUPLE = re.compile(r"\(\s*(['\"])(?:\\.|(?!\1).)*\1\s*,\s*(['\"])(?:\\.|(?!\2).)*\2\s*\)")
_QUERY = re.compile(r"(?i)([?&](?:" + _KEY + r"|p|pw|pin|pass|code|key|x-amz-[a-z-]+)=)[^&\s#\"']+")
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA|AGPA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# 32+ characters of a token alphabet with upper case, lower case and a digit: a secret's shape (a git sha, a uuid or a
# hex digest is lower case only and stays)
_LONG = re.compile(r"(?<![A-Za-z0-9/+_=-])[A-Za-z0-9/+_=-]{32,}(?![A-Za-z0-9/+_=-])")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|PuTTY-User-Key-File|\bssh-(?:rsa|ed25519) AAAA")
# after the patterns: a password-like word followed by anything but a redaction, punctuation or the end: dropped
_LEFT = re.compile(r"(?i)(?<![\w-])(?:pass(?:word|wd|phrase)?|pwd|pw|pin|secret)s?(?![\w-])"
                   r"(?![\"']?\s*[:=]?\s*[\"']?\[(?:redacted|credential)\])(?=\s*[:=]?\s*[\"']?[\w\[{(<$%@#!*~^&+-])")


def _forms(v: str) -> set[str]:
    """v as it may appear in a log line: as is, URL-encoded, JSON-, HTML- and repr-escaped, and base64."""
    out = {v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), json.dumps(v)[1:-1],
           json.dumps(v, ensure_ascii=False)[1:-1], html.escape(v), html.escape(v, quote=False), repr(v)[1:-1],
           base64.b64encode(v.encode()).decode().rstrip("=")}
    return {f for f in out if f}


def secret(*values: str, short: bool = False) -> None:
    """Values that must never leave the station in a log line (a line with one, in any of its forms, is dropped). Kept
    in memory only. short: a credstore value, matched down to MIN_SHORT characters as a whole word."""
    with _lock:
        for v in values:
            v = (v or "").strip()
            if len(v) >= MIN_SECRET:
                _values.update(_forms(v))
            elif short and len(v) >= MIN_SHORT:
                _short.add(v)


def secret_pair(user: str, password: str) -> None:
    """A login: base64 of "user:password" (HTTP Basic auth), with the password itself."""
    user, password = (user or "").strip(), (password or "").strip()
    if user and password:
        with _lock:
            _values.add(base64.b64encode(f"{user}:{password}".encode()).decode().rstrip("="))
        secret(password, short=True)


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
        _short.clear()
        _names.clear()


def registered() -> tuple[int, int]:
    """How many values (all forms) and names are registered (counts only: the station's status)."""
    with _lock:
        return len(_values) + len(_short), len(_names)


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


def normalise(line: str) -> str:
    s = unicodedata.normalize("NFKC", str(line))
    s = _ZW.sub("", s)
    return s.replace("\r", " ").replace("\0", " ").replace("\t", " ")


def _has_value(s: str, values, short) -> bool:
    if any(v in s for v in values):
        return True
    return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", s) for v in short)


def scrub(line: str) -> str | None:
    """The line safe to send, or None (drop it). See the module doc."""
    if line is None:
        return None
    raw = str(line)
    s = normalise(raw)
    with _lock:
        values, short, names = tuple(_values), tuple(_short), tuple(_names)
    if _has_value(s, values, short) or _has_value(raw, values, short):     # the whole line, before any cut
        return None
    if _PRIVATE_KEY.search(s):
        return None
    if len(s) > MAX_LINE:
        s = s[:MAX_LINE] + " …"
    names_rx = sorted(names, key=len, reverse=True)
    for n in names_rx:                                   # a credential's name as a key: its value goes
        s = re.sub(r"(" + re.escape(n) + r"[\"']?\s*[:=]\s*)" + _VAL, lambda m: m.group(1) + REDACTED, s)
    s = _USERINFO.sub(lambda m: (m.group(1) or "") + REDACTED + "@", s)
    s = _AUTH.sub(lambda m: m.group(1) + " " + REDACTED, s)
    s = _QUERY.sub(lambda m: m.group(1) + REDACTED, s)
    s = _CLI.sub(lambda m: m.group(1) + REDACTED, s)
    s = _CLI_P.sub(lambda m: m.group(1) + " " + REDACTED, s)
    s = _KV.sub(lambda m: m.group(1) + REDACTED, s)
    s = _SHORT_KV.sub(lambda m: m.group(1) + REDACTED, s)
    s = _SSM.sub(lambda m: m.group(1) + REDACTED, s)
    if _AFTER_AUTH.search(s):
        s = _TUPLE.sub("(" + REDACTED + ")", s)
        s = _PAIR.sub(lambda m: m.group(1) + ":" + REDACTED, s)
    s = _AWS_KEY_ID.sub("[aws-key]", s)
    s = _JWT.sub(REDACTED, s)
    s = _aws_secret(s)
    s = _LONG.sub(_long, s)
    for n in names_rx:
        if n in s:
            s = s.replace(n, "[credential]")
    if _LEFT.search(s):
        return None
    return s


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
