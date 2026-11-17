"""Private term lists that bias speech-to-text, per kind of radio source (the STT plan, step 3a).

The lists are kept out of this public repo. The hub reads them from its own backups bucket, where only its task
role (and the admin) can read them:

    s3://<LITESTREAM_BUCKET>/<LITESTREAM_PATH>/vocab/<name>.txt        (LITESTREAM_PATH: sizeup-hub)

All-in-one (dev) reads them from a folder instead ([stt] terms_dir: <dir>/<name>.txt or <dir>/terms/<name>.txt).
A station never reads them: the hub sends it the top of each list for whisper's prompt (set_stt_terms, below).
docs/stt-vocabulary.md says how to upload them.

    name          used on
    county_fire   the county's Fire/EMS dispatch and paging (South 2, Fire/EMS 15 = tg:307, FG1, FG2), our talkgroups
    pd_dispatch   police dispatch (MRRS 9211 / 9212, the county's police talkgroups)
    ems           EMS (MRRS 9570, the county's EMS talkgroups)
    airband       HPN Tower / Approach / Ground
    places        every one of them: loaded with each

Format: UTF-8, one term per line, "#" lines are comments (section headers), blank lines skipped; priority order
(the top is what a smaller budget takes); numbers already spelled as said (transcribe.spoken()). Terms are deduped
ignoring case, hyphens and punctuation ("I-95" = "I 95").

A term set is a source's own list with places woven in (PLACES_SHARE of every stretch of the top is places, so a
budget that takes the top takes both), deduped. Its version is the sha256 of its terms as parsed (12 hex): a comment
or a blank line changes nothing, any term does. A transcript records which set biased it (radio_traffic.vocab_v,
"<own>@<version>", e.g. "county_fire@0123456789ab", whisper; "<own>+<own vocabulary>@<version>" an AWS term
vocabulary; an AWS vocabulary without terms: "<its name>@<its phrases' sig>").

    whisper   with_terms(): the set's top terms without a number (written() digits; MAX_NUMERIC_PROMPT_TERMS) that fit what
              is left of whisper's prompt budget (PROMPT_TOKENS, estimated conservatively: est_tokens) after the prompt
              the source already had, which goes last (whisper.cpp keeps the end of a prompt that is too long). Its
              guard against a prompt read back on dead air is sizeup/stt.py prompt_noise (only with a list in the prompt).
    AWS       TermVocabularies: per (list, the channel's own vocabulary) one custom vocabulary: the own phrases first
              (Settings' sizeup-pcfd, sizeup-pd, sizeup-atc with its operators), then the list's terms up to AWS's
              limits, named sizeup-terms-<list>-<own>-<version>, made by the hub's leader the first time a channel of the
              pair asks and again when what it holds changes, used once READY. Until then (or when AWS refused it) the
              channel keeps its own. The last KEEP names are kept (a stream open with one keeps it), older ones deleted.

Only recognition is biased; AWS's spelled-out numbers of the list come back as digits like its own phrases' (respell).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sizeup import aws

NAMES = ("county_fire", "pd_dispatch", "ems", "airband", "places")
SHARED = "places"
OWN = tuple(n for n in NAMES if n != SHARED)
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
S3_SUB = "vocab"                  # s3://<bucket>/<LITESTREAM_PATH>/vocab/<name>.txt
MAX_FILE_BYTES = 512 * 1024       # a list larger than this is refused (the largest is ~70 KB)
MAX_TERMS = 10000                 # terms kept of one file at most
MAX_TERM_CHARS = 100              # a longer line isn't a term said on the air: skipped
PLACES_SHARE = 0.35               # of every stretch of a set's top, this much is places
REFRESH_S = 300.0                 # the lists are looked at this often (a conditional GET: 304 when unchanged)
SETTLE_S = 60.0                   # a change is used once a read this much later finds the same (an upload in progress)
TIMEOUT_S = 15.0
# whisper's prompt: whisper keeps at most n_text_ctx / 2 = 224 tokens of it (the end). est_tokens() overcounts (below),
# so a prompt within PROMPT_TOKENS by its estimate is within whisper's own limit, with room to spare.
PROMPT_TOKENS = 210
CHARS_PER_TOKEN = 3.5
MIN_PROMPT_TOKENS = 12            # less room than this left: no terms (a prompt of two words biases nothing)
PUSH_TERMS = 300                  # terms of each set the hub sends the station (more than a prompt can hold)
# Numbers in whisper's prompt are written as digits (written()): spelled out, whisper writes every number it hears as
# words ("sixty control", "one twenty three King Street") and the dispatch parser finds no unit and no house number. A
# prompt full of callsigns also pulls a heard number toward one of them ("Engine 14" -> a listed "Engine 49"), so no
# term with a number goes in (MAX_NUMERIC_PROMPT_TERMS; the talkers on the call still do). Measured on 146 real county
# clips (docs/stt-vocabulary.md): spelled, 56 of 81 transcripts that should have digits had them (large-v3-turbo; 71
# with no list); in digits, 73; numbers taken from the prompt that were never said, 8 with every numeric term, 3 with
# none (2 with no list).
MAX_NUMERIC_PROMPT_TERMS = 0
PROFILE_STREETS = 12              # a police profile's busiest streets kept in its prompt next to its term list
KEEP = 3                          # AWS vocabularies kept per set: the one in use and the two before it

# Which list biases which source: by its key first, then airband / paging, then its service (sizeup/sourcekeys.py).
BY_KEY = {"p25:mrrs:9211": "pd_dispatch", "p25:mrrs:9212": "pd_dispatch", "p25:mrrs:9570": "ems"}
BY_SERVICE = {"fire": "county_fire", "police": "pd_dispatch", "ems": "ems", "air": "airband"}


def own_for(key: str = "", service: str = "", kind: str = "") -> str | None:
    """The list a source takes ("county_fire" ...; places goes with it), None for none (transit, other). key: its source
    key (p25:mrrs:9211, fm:South 2, air:HPN Tower) or the older form (tg:307, South 2); kind: the channel's kind."""
    key, service, kind = str(key or ""), str(service or "").lower(), str(kind or "")
    if kind == "airband" or key.startswith("air:"):
        return "airband"
    if key in BY_KEY:
        return BY_KEY[key]
    if kind == "paging":
        return "county_fire"
    return BY_SERVICE.get(service)


# -- the lists ---------------------------------------------------------------------------------------------------------

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_ONES.update(oh=0, niner=9)
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i >= 2}
_NUM = re.compile(r"\b(?:(?:%s|%s|hundred)\b[ -]?)+" % ("|".join(_ONES), "|".join(_TENS)), re.I)


def _digits(words: list[str]) -> str:
    """A run of number words as radio says them -> its digits, chunk by chunk ("twenty seven sixty one" 2761, "one
    forty two" 142, "four oh one" 401, "one two zero zero" 1200, "fifteen hundred" 1500)."""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w in _TENS:
            v = _TENS[w]
            if i + 1 < len(words) and words[i + 1] in _ONES and 0 < _ONES[words[i + 1]] < 10:
                v, i = v + _ONES[words[i + 1]], i + 1
            out.append(str(v))
        elif w == "hundred":
            out.append("00" if out else "100")
        else:
            out.append(str(_ONES[w]))
        i += 1
    return "".join(out)


def written(term: str) -> str:
    """A term as whisper should write it: its numbers in digits ("Engine one forty two" -> "Engine 142", "eighty eight
    Alpha three" -> "88 Alpha 3", "ten thirty two" -> "10-32"); the rest as it is. "oh" alone stays a word."""
    def one(m: re.Match) -> str:
        words = m.group(0).lower().replace("-", " ").split()
        if words == ["oh"]:
            return m.group(0)
        tail = " " if m.group(0)[-1:] in " -" else ""
        if len(words) >= 2 and words[0] == "ten" and words[1] != "hundred":
            return f"10-{_digits(words[1:])}{tail}"
        return _digits(words) + tail
    return " ".join(_NUM.sub(one, str(term)).split())


def has_number(term: str) -> bool:
    return any(c.isdigit() for c in term)

def term_key(term: str) -> str:
    """What two terms are compared by: case, hyphens and punctuation ignored."""
    return " ".join(re.sub(r"[^\w\s]|_", " ", term.casefold()).split())


def parse(text: str) -> list[str]:
    """A term file's terms, in order: comments and blank lines out, spaces collapsed, duplicates (term_key) dropped."""
    out, seen = [], set()
    for line in text.splitlines():
        t = " ".join(unicodedata.normalize("NFC", line).split())
        if not t or t.startswith("#") or len(t) > MAX_TERM_CHARS:
            continue
        k = term_key(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
            if len(out) >= MAX_TERMS:
                break
    return out


def merge(own: list[str], shared: list[str], share: float = PLACES_SHARE) -> list[str]:
    """own and shared woven together in priority order, own first: at every point of the result about `share` of it is
    shared (while each lasts), duplicates dropped."""
    out, seen = [], set()
    i = j = n_shared = 0
    while i < len(own) or j < len(shared):
        take_shared = j < len(shared) and (i >= len(own) or n_shared < share * len(out))
        if take_shared:
            t, j = shared[j], j + 1
        else:
            t, i = own[i], i + 1
        k = term_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        n_shared += take_shared
    return out


def version(terms) -> str:
    return hashlib.sha256("\n".join(terms).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class TermSet:
    own: str                  # county_fire, pd_dispatch, ems, airband
    terms: tuple[str, ...]    # in priority order, places woven in
    v: str                    # version(terms)
    places: bool = True       # places.txt was there to go with it

    @property
    def vocab_v(self) -> str:
        """What a transcript biased by it records (radio_traffic.vocab_v): the list and the set's version."""
        return f"{self.own}@{self.v}"


def build_sets(lists: dict[str, list[str]], share: float = PLACES_SHARE) -> dict[str, TermSet]:
    """Every source list there is, each with places woven in."""
    shared = lists.get(SHARED) or []
    out = {}
    for own in OWN:
        if lists.get(own):
            terms = tuple(merge(lists[own], shared, share))
            out[own] = TermSet(own, terms, version(terms), bool(shared))
    return out


# -- whisper's prompt ------------------------------------------------------------------------------------------------

TOKEN = re.compile(r"[A-Za-z]+|\d|[^\sA-Za-z\d]")


def est_tokens(text: str) -> int:
    """whisper's tokens in a text, overcounted: a word CHARS_PER_TOKEN letters a token, rounded up, a digit, a sign or
    anything else (an accent, another script) one each. Measured (2026-11) against whisper's own BPE (the vocabulary in
    the ggml models, large-v3-turbo's and the .en models'), on prompts that fill PROMPT_TOKENS from every list: the real
    count was at most 0.97 of this estimate (0.86 for the .en models), and about 0.6 to 0.75 of it for most prompts."""
    return sum(math.ceil(len(t) / CHARS_PER_TOKEN) if t.isalpha() and t.isascii() else 1
               for t in TOKEN.findall(str(text or "")))


def prompt(terms, budget: int = PROMPT_TOKENS) -> tuple[str, int]:
    """The top terms that fit `budget` (estimated tokens), the most important LAST ("c, b, a."): nearest the audio, and
    what whisper.cpp keeps if a prompt is ever cut (it keeps the end). (the text, how many terms); ("", 0) when even the
    first won't fit or the room is under MIN_PROMPT_TOKENS."""
    if budget < MIN_PROMPT_TOKENS:
        return "", 0
    took, used = [], 1                                   # the closing "."
    for t in terms:
        cost = est_tokens(t) + (1 if took else 0)        # ", " before it
        if used + cost > budget:
            break
        took.append(t)
        used += cost
    return (", ".join(reversed(took)) + "." if took else ""), len(took)


def prompt_terms(terms, max_numeric: int = MAX_NUMERIC_PROMPT_TERMS) -> list[str]:
    """The terms as whisper's prompt takes them, in order: written() (digits), deduped, at most max_numeric with a
    number."""
    out, seen, n = [], set(), 0
    for t in terms:
        w = written(t)
        k = term_key(w)
        if not k or k in seen:
            continue
        if has_number(w):
            if n >= max_numeric:
                continue
            n += 1
        seen.add(k)
        out.append(w)
    return out


def with_terms(base: str, ts: TermSet | None, budget: int = PROMPT_TOKENS,
               max_numeric: int = MAX_NUMERIC_PROMPT_TERMS) -> tuple[str, str]:
    """whisper's prompt: the set's top terms (prompt_terms) in what `base` (the prompt the source already has) leaves of
    the budget, then base (the talkers on the call, the aircraft near HPN: what is most about this clip, last).
    (prompt, vocab_v): vocab_v "" when no term went in."""
    base = str(base or "").strip()
    if ts is None or not ts.terms:
        return base, ""
    text, n = prompt(prompt_terms(ts.terms, max_numeric), budget - est_tokens(base))
    if not n:
        return base, ""
    return (f"{text} {base}" if base else text), ts.vocab_v


# -- where the lists are read from ----------------------------------------------------------------------------------

class DirFiles:
    """<dir>/<name>.txt (or <dir>/terms/<name>.txt: a checkout of the private repo's vocab folder). Dev / all-in-one."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()

    def where(self) -> str:
        return str(self.root)

    def _path(self, name: str) -> Path | None:
        for p in (self.root / f"{name}.txt", self.root / "terms" / f"{name}.txt"):
            if p.is_file():
                return p
        return None

    def fetch(self, name: str, etag: str = "") -> tuple[int, bytes, str]:
        """(200, its bytes, a tag) | (304, b"", tag) unchanged since `etag` | (404, b"", "")."""
        p = self._path(name)
        if p is None:
            return 404, b"", ""
        st = p.stat()
        tag = f"{st.st_mtime_ns}-{st.st_size}"
        if etag and tag == etag:
            return 304, b"", tag
        if st.st_size > MAX_FILE_BYTES:
            raise ValueError(f"{name}.txt: over {MAX_FILE_BYTES // 1024} KB")
        return 200, p.read_bytes(), tag


def _send(method: str, url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers.items()), r.read(MAX_FILE_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read(1 << 14)


class S3Files:
    """s3://<bucket>/<prefix>/<name>.txt, by a conditional GET (If-None-Match: 304 when unchanged). The hub's task role
    may already read every key of its backups bucket (sizeup-aws/2-iam.sh, policy "backups"): no IAM change."""

    def __init__(self, bucket: str, prefix: str, region: str = "", endpoint: str = "",
                 send: Callable[[str, str, dict, float], tuple[int, dict, bytes]] | None = None,
                 creds: Callable[[], aws.Credentials] = aws.credentials):
        self.bucket, self.prefix, self.region = bucket, prefix.strip("/"), aws.region(region)
        self.endpoint = (endpoint or f"https://{bucket}.s3.{self.region}.amazonaws.com").rstrip("/")
        self.send, self.creds = send or _send, creds

    def where(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}/"

    def fetch(self, name: str, etag: str = "") -> tuple[int, bytes, str]:
        url = f"{self.endpoint}/{urllib.parse.quote(f'{self.prefix}/{name}.txt')}"
        hdrs = {"x-amz-content-sha256": aws.EMPTY_SHA256, **({"If-None-Match": etag} if etag else {})}
        signed = aws.sign("GET", url, hdrs, b"", self.creds(), self.region, "s3", payload_sha=aws.EMPTY_SHA256)
        status, rh, data = self.send("GET", url, {k: v for k, v in signed.items() if k != "Host"}, TIMEOUT_S)
        rh = {k.lower(): v for k, v in (rh or {}).items()}
        if status == 304:
            return 304, b"", etag
        if status == 404:
            return 404, b"", ""
        if status == 403:
            aws.forget_credentials()
        if status != 200:
            code = re.search(rb"<Code>([^<]{1,64})</Code>", data or b"")
            raise aws.AwsError(f"S3 GET {name}.txt: {status}" + (f" {code.group(1).decode()}" if code else ""),
                               code.group(1).decode() if code else f"HTTP {status}", status)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name}.txt: over {MAX_FILE_BYTES // 1024} KB")
        return 200, data, rh.get("etag", "")


def files_from(env: dict, terms_dir: str = "") -> DirFiles | S3Files | None:
    """The hub's (LITESTREAM_BUCKET / LITESTREAM_PATH / LITESTREAM_REGION: its backups bucket), else [stt] terms_dir,
    else None (no lists: nothing changes)."""
    bucket = str(env.get("LITESTREAM_BUCKET", "")).strip()
    if bucket:
        prefix = (str(env.get("LITESTREAM_PATH", "")).strip() or "sizeup-hub").strip("/")
        return S3Files(bucket, f"{prefix}/{S3_SUB}", str(env.get("LITESTREAM_REGION", "")).strip())
    if terms_dir:
        return DirFiles(terms_dir)
    return None


class TermStore:
    """The lists as last read, their sets, and a thread that looks again every REFRESH_S. on_read(store, changed) after
    every read (never under the lock): changed when a set did. A list that can't be read keeps what was read before.
    The first read is used at once; a later change only once a read SETTLE_S later finds the same lists (five files
    uploaded one after the other are used together, never half of them). loaded: the first read is over."""

    def __init__(self, files, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 on_read: Callable[[TermStore, bool], None] = lambda store, changed: None):
        self.files, self.log, self.clock, self.on_read = files, log, clock, on_read
        self._lock = threading.Lock()
        self._lists: dict[str, tuple[str, list[str]]] = {}      # name -> (its tag, its terms)
        self._sets: dict[str, TermSet] = {}
        self.v = ""                                             # every set's version together: "" for none
        self._pending = ""                                      # a change read once, not used yet
        self.read_at: float | None = None
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def get(self, own: str | None) -> TermSet | None:
        return self._sets.get(own) if own else None

    def sets(self) -> dict[str, TermSet]:
        return dict(self._sets)

    def refresh(self) -> bool:
        """Read every list again (unchanged ones answer 304). True: a set changed."""
        errors = []
        with self._lock:
            lists = dict(self._lists)
        for name in NAMES:
            tag = lists.get(name, ("", []))[0]
            try:
                status, data, new_tag = self.files.fetch(name, tag)
            except (OSError, ValueError, aws.AwsError) as e:
                errors.append(f"{name}: {e}"[:200])
                continue
            if status == 404:
                lists.pop(name, None)
            elif status == 200:
                try:
                    lists[name] = (new_tag, parse(data.decode("utf-8")))
                except UnicodeDecodeError:
                    errors.append(f"{name}: not UTF-8")
        sets = build_sets({n: t for n, (_tag, t) in lists.items()})
        v = version(f"{s.own}@{s.v}" for s in sets.values()) if sets else "-"
        with self._lock:
            first = self.read_at is None
            changed = v != (self.v or "-") and (first or v == self._pending)
            settling = v != (self.v or "-") and not changed
            self._lists, self._pending = lists, (v if settling else "")
            if changed:
                self._sets, self.v = sets, ("" if v == "-" else v)
            self.read_at, self.error = self.clock(), "; ".join(errors)
        if settling:
            self.log(f"speech-to-text terms: a change at {self.files.where()}: used once it is the same "
                     f"{SETTLE_S:g} s from now")
        if errors:
            self.log(f"speech-to-text terms: not read: {self.error}")
        if changed:
            self.log("speech-to-text terms: " + (", ".join(f"{s.own} {len(s.terms)} ({s.v})" for s in sets.values())
                                                 or "none") + f" from {self.files.where()}")
        try:
            self.on_read(self, changed)
        except Exception as e:  # noqa: BLE001 - a reader's trouble never stops the lists
            self.log(f"speech-to-text terms: {type(e).__name__}: {e}")
        return changed

    def state(self) -> dict:
        """No terms, only counts and versions (Settings, health)."""
        return {"v": self.v, "where": self.files.where(), "read_at": self.read_at, "error": self.error,
                "sets": {s.own: {"terms": len(s.terms), "v": s.v, "places": s.places} for s in self._sets.values()}}

    @property
    def loaded(self) -> bool:
        return self.read_at is not None

    @property
    def settling(self) -> bool:
        return bool(self._pending)

    def payload(self, n: int = PUSH_TERMS) -> dict:
        """What the hub sends the station (set_stt_terms): each set's top n terms and its version."""
        return {"v": self.v, "sets": {s.own: {"terms": list(s.terms[:n]), "v": s.v, "places": s.places}
                                      for s in self._sets.values()}}

    def start(self, every: float = REFRESH_S) -> TermStore:
        def run():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception as e:  # noqa: BLE001 - looked at again next time
                    self.log(f"speech-to-text terms: {type(e).__name__}: {e}")
                self._stop.wait(min(every, SETTLE_S) if self.settling else every)
        self._thread = threading.Thread(target=run, name="stt-terms", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


class Pushed:
    """The station's copy: what the hub last sent (set_stt_terms), in memory only (never written to the station's
    disk; the hub sends it again when the station reconnects with none)."""

    MAX_SETS = 8
    MAX_PUSHED = 400

    def __init__(self):
        self._sets: dict[str, TermSet] = {}
        self.v = ""

    def get(self, own: str | None) -> TermSet | None:
        return self._sets.get(own) if own else None

    def apply(self, payload) -> dict:
        """Check and keep it (ValueError: refused, the copy before kept). {v, sets}: what the station reports."""
        if not isinstance(payload, dict) or not isinstance(payload.get("sets", {}), dict):
            raise ValueError("stt terms: an object with sets")
        raw = payload.get("sets") or {}
        if len(raw) > self.MAX_SETS:
            raise ValueError("stt terms: too many sets")
        sets = {}
        for own, d in raw.items():
            if not isinstance(own, str) or not NAME.fullmatch(own) or not isinstance(d, dict):
                raise ValueError("stt terms: a set's name")
            terms = d.get("terms")
            if not isinstance(terms, list) or len(terms) > self.MAX_PUSHED or not all(
                    isinstance(t, str) and 0 < len(t) <= MAX_TERM_CHARS for t in terms):
                raise ValueError(f"stt terms: {own}: its terms")
            v = str(d.get("v") or "")
            if not re.fullmatch(r"[0-9a-f]{12}", v):
                raise ValueError(f"stt terms: {own}: its version")
            sets[own] = TermSet(own, tuple(" ".join(t.split()) for t in terms), v, bool(d.get("places", True)))
        v = str(payload.get("v") or "")
        if not re.fullmatch(r"(?:[0-9a-f]{12})?", v):
            raise ValueError("stt terms: the version")
        self._sets, self.v = sets, v
        return self.state()

    def state(self) -> dict:
        return {"v": self.v, "sets": {s.own: s.v for s in self._sets.values()}}


class StationPush:
    """The hub's side: the station is sent the store's payload whenever the version it reports differs (one push at a
    time, again at most every RETRY_S after a failure: a station from before this answers "not allowed")."""

    RETRY_S = 60.0

    def __init__(self, payload: Callable[[], dict], send: Callable[[dict], object], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic):
        self.payload, self.send, self.log, self.clock = payload, send, log, clock
        self._lock = threading.Lock()
        self._busy = False
        self._failed_at: float | None = None
        self.error = ""

    def check(self, reported, connected: bool = True) -> bool:
        """reported: the station's status "stt_terms" ({v}, or None from a station before this). True: a push started.
        payload() None: nothing to send yet (the lists' first read isn't over: never an empty set before it)."""
        want = self.payload()
        got = reported.get("v") if isinstance(reported, dict) else None
        if want is None or not connected or (got or "") == (want.get("v") or ""):
            return False
        with self._lock:
            if self._busy or (self._failed_at is not None and self.clock() - self._failed_at < self.RETRY_S):
                return False
            self._busy = True

        def push():
            try:
                self.send(want)
                self._failed_at, self.error = None, ""
            except Exception as e:  # noqa: BLE001 - StationOffline, RuntimeError (an older station): again later
                if str(e)[:200] != self.error:
                    self.log(f"hub: speech-to-text terms not sent to the station: {e}"[:300])
                self._failed_at, self.error = self.clock(), str(e)[:200]
            finally:
                with self._lock:
                    self._busy = False
        threading.Thread(target=push, name="hub-stt-terms", daemon=True).start()
        return True


# -- Amazon Transcribe ----------------------------------------------------------------------------------------------
#
# A stream takes ONE custom vocabulary. So a channel's term vocabulary is its own vocabulary's phrases first (sizeup-pcfd
# as Settings > Radios > Vocabulary has it, edits included; sizeup-pd; sizeup-atc with the operators #257 adds), then
# the list's terms, up to AWS's limits: one vocabulary per (list, own vocabulary) pair, made the first time a channel of
# that pair asks for it. Its terms go in written() (digits), so build_vocabulary spells them for AWS and keeps the
# respelling back to digits, as it does for the own phrases.

VOCAB_PREFIX = "sizeup-terms-"     # inside the task role's transcribe grant (vocabulary/sizeup-*: sizeup-aws/12)
VOCAB_NAME = re.compile(VOCAB_PREFIX + r"(?P<own>[a-z][a-z0-9_]{0,31})-(?P<base>[a-z0-9]{1,24})-(?P<v>[0-9a-f]{12})")
RECHECK_S = 3600.0                 # a READY vocabulary is looked at this often (deleted out of band: made again)
FAILED_BACKOFF_S = (3600.0, 6 * 3600.0, 24 * 3600.0)   # a FAILED one tried again after these (then every 24 h)
ERROR_BACKOFF_S = REFRESH_S        # an API error: tried again at the next read


def base_short(base: str) -> str:
    """sizeup-pcfd -> pcfd; "" (no own vocabulary) -> none."""
    b = re.sub(r"[^a-z0-9]", "", (base or "").removeprefix("sizeup-").lower())[:24]
    return b or "none"


def aws_phrases(terms) -> list[str]:
    """The terms as transcribe.build_vocabulary takes them: written() (digits: it spells them and keeps the respelling),
    accents dropped (AWS's en-US list takes ASCII letters, apostrophes, periods and hyphens: to_aws() does the rest)."""
    out = []
    for t in terms:
        a = unicodedata.normalize("NFKD", written(t)).encode("ascii", "ignore").decode()
        if a.strip():
            out.append(a)
    return out


def combined(base_phrases, terms) -> dict:
    """build_vocabulary over the own phrases, then the terms: {source (what Vocabulary.push takes), phrases, respell,
    v} (v: the phrases as sent and their respelling)."""
    from sizeup.transcribe import build_vocabulary
    source = [*[str(p) for p in base_phrases or ()], *aws_phrases(terms)]
    built = build_vocabulary(source)
    return {**built, "source": source,
            "v": version([*built["phrases"], *(f"{k}={v}" for k, v in sorted(built["respell"].items()))])}


def vocab_v_of(name: str) -> str:
    """The vocab_v of a term vocabulary's name: "<list>+<own>@<version>" ("" for a name that isn't one)."""
    m = VOCAB_NAME.fullmatch(name or "")
    return f"{m['own']}+{m['base']}@{m['v']}" if m else ""


class _Scrubbed:
    """AWS's calls with its error text left out: an AwsError keeps its code and status, a FAILED vocabulary a fixed
    reason, never AWS's message (which can quote a phrase of the list). For the log, the state and Settings."""

    def __init__(self, call):
        self.call = call

    def __call__(self, *a, **kw):
        try:
            r = self.call(*a, **kw)
            if isinstance(r, dict) and r.get("FailureReason"):     # (FAILED: AWS's reason can quote the phrase)
                r = {**r, "FailureReason": "AWS refused the list (its reason not kept: it can quote a term)"}
            return r
        except aws.AwsError as e:
            code = e.code or f"HTTP {e.status}"
            raise aws.AwsError(code, e.code, e.status) from None


class TermVocabularies:
    """The term vocabularies on AWS, one per (list, own vocabulary) pair. want(own, base) registers a pair (a channel of
    it streamed); sync(sets) has the leader make each registered pair's vocabulary for what it holds now (a new name:
    CreateVocabulary, GetVocabulary until READY, transcribe.Vocabulary). ready(own, base) is the newest READY one (the
    one before stays in use while the next is PENDING; none READY: the channel keeps its own vocabulary). Kept in the
    CAD's meta (get/put): the pairs, and per pair which name is wanted, which READY (and its respelling), the names made
    (the last KEEP kept, older deleted), when a FAILED one may be tried again and when a READY one was last looked at."""

    PAIRS = "stt_terms_aws_pairs"

    def __init__(self, region: str, get: Callable[[str], str | None], put: Callable[[str, str], None],
                 base_phrases: Callable[[str], list[str]] = lambda base: [], endpoint: str = "",
                 call: Callable[..., dict] = aws.json_call, log: Callable[[str], None] = print,
                 leader: Callable[[], bool] = lambda: True, poll_s: float = 10.0, clock: Callable[[], float] = time.time):
        self.region, self.endpoint, self.log, self.leader, self.poll_s = region, endpoint, log, leader, poll_s
        self.call, self.base_phrases, self.clock = _Scrubbed(call), base_phrases, clock
        self._get, self._put = get, put
        self._lock = threading.RLock()
        self._vocabs: dict[tuple[str, str], object] = {}
        self._sets: dict[str, TermSet] = {}
        self._syncing = False

    # -- what is kept ------------------------------------------------------------------------------------------------

    def _json(self, key: str) -> dict:
        try:
            d = json.loads(self._get(key) or "{}")
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    def pairs(self) -> list[tuple[str, str]]:
        return [(str(o), str(b)) for o, b in self._json(self.PAIRS).get("pairs") or () if NAME.fullmatch(str(o))]

    @staticmethod
    def _key(own: str, base: str) -> str:
        return f"stt_terms_aws_{own}_{base_short(base)}"

    def meta(self, own: str, base: str) -> dict:
        d = self._json(self._key(own, base))
        return {"want": str(d.get("want") or ""), "ready": str(d.get("ready") or ""),
                "names": [str(n) for n in d.get("names") or () if VOCAB_NAME.fullmatch(str(n))],
                "respell": d.get("respell") if isinstance(d.get("respell"), dict) else {},
                "fails": int(d.get("fails") or 0), "retry_at": float(d.get("retry_at") or 0),
                "checked": float(d.get("checked") or 0)}

    def _save(self, own: str, base: str, **kw) -> dict:
        with self._lock:
            d = self.meta(own, base)
            d.update(kw)
            self._put(self._key(own, base), json.dumps(d))
        return d

    def _vocab(self, own: str, base: str, name: str):
        from sizeup.transcribe import Vocabulary
        v = self._vocabs.get((own, base))
        if v is None or v.name != name:
            v = Vocabulary(self.region, self._get, self._put, name, self.endpoint, call=self.call, log=self.log,
                           poll_s=self.poll_s, on_ready=lambda n, o=own, b=base: self._ready(o, b, n),
                           key=f"aws_vocabulary_terms_{own}_{base_short(base)}")
            self._vocabs[(own, base)] = v
        return v

    # -- what channels ask -------------------------------------------------------------------------------------------

    def ready(self, own: str | None, base: str = "") -> str:
        """The vocabulary a channel of that pair streams with now: "" for none (it keeps its own)."""
        return self.meta(own, base)["ready"] if own else ""

    def want(self, own: str, base: str = "") -> None:
        """A channel of this pair streamed: its vocabulary made (in the background, the leader only) if it isn't."""
        if not own or (own, base) in self.pairs() or not self.leader():
            return
        with self._lock:
            self._put(self.PAIRS, json.dumps({"pairs": [*self.pairs(), [own, base]]}))
        threading.Thread(target=self.sync, name="stt-terms-aws", daemon=True).start()

    def respell(self, name: str) -> dict:
        """The respelling of a term vocabulary while it is the READY one ({} otherwise)."""
        m = VOCAB_NAME.fullmatch(name or "")
        if not m:
            return {}
        for own, base in self.pairs():
            if own == m["own"] and base_short(base) == m["base"]:
                mt = self.meta(own, base)
                return mt["respell"] if mt["ready"] == name else {}
        return {}

    # -- the leader's work -------------------------------------------------------------------------------------------

    def sync(self, sets: dict[str, TermSet] | None = None) -> None:
        """Each registered pair's vocabulary made for what it holds now; a list no longer there: its vocabularies
        deleted. The leader only (another task reads what the leader made)."""
        if sets is not None:
            self._sets = dict(sets)
        if not self.leader():
            return
        with self._lock:
            if self._syncing:
                return
            self._syncing = True
        try:
            for own, base in self.pairs():
                try:
                    self._sync_one(own, base)
                except Exception as e:  # noqa: BLE001 - the next pair, and this one at the next read
                    self.log(f"aws transcribe: term vocabulary {own}/{base_short(base)}: {type(e).__name__}")
        finally:
            with self._lock:
                self._syncing = False

    def _sync_one(self, own: str, base: str) -> None:
        ts = self._sets.get(own)
        m = self.meta(own, base)
        if ts is None:                                   # the list was taken away: so are its vocabularies
            if m["names"] or m["ready"]:
                self._delete(m["names"])
                self._save(own, base, want="", ready="", names=[], respell={})
                self.log(f"aws transcribe: term list {own} gone: its vocabularies deleted")
            return
        built = combined(self.base_phrases(base), ts.terms)
        want = f"{VOCAB_PREFIX}{own}-{base_short(base)}-{built['v']}"
        vocab = self._vocab(own, base, want)
        now = self.clock()
        if m["want"] == want:
            state = vocab.state()["state"]
            if state == "PENDING":
                vocab.poll()                             # a restart while it was being made: look until it is READY
                return
            if state == "READY":
                if now - m["checked"] >= RECHECK_S:     # deleted out of band? made again
                    self._save(own, base, checked=now)
                    if vocab.check()["state"] == "none":
                        self.log(f"aws transcribe: term vocabulary {want} is gone from AWS: made again")
                        self._push(own, base, vocab, built, m)
                return
            if state == "FAILED":
                if not m["retry_at"]:                    # just found FAILED: tried again only after a while
                    self._save(own, base, fails=1, retry_at=now + FAILED_BACKOFF_S[0])
                    self.log(f"aws transcribe: term vocabulary {want} FAILED: tried again in "
                             f"{FAILED_BACKOFF_S[0] / 3600:g} h; its channels keep their own")
                    return
                if now < m["retry_at"]:
                    return
                fails = m["fails"] + 1
                self._save(own, base, fails=fails,         # (the same name again: UpdateVocabulary)
                           retry_at=now + FAILED_BACKOFF_S[min(fails, len(FAILED_BACKOFF_S)) - 1])
            elif state == "error" and now < m["retry_at"]:
                return
        else:
            self._save(own, base, want=want, names=[*[n for n in m["names"] if n != want], want], fails=0, retry_at=0)
        self._push(own, base, vocab, built, self.meta(own, base))

    def _push(self, own: str, base: str, vocab, built: dict, m: dict) -> None:
        st = vocab.push(built["source"])
        if st["state"] == "error":
            self._save(own, base, retry_at=self.clock() + ERROR_BACKOFF_S)
        self.log(f"aws transcribe: term vocabulary {vocab.name}: {st.get('phrases', 0)} phrases ({st['state']}"
                 + (f": {st['reason']}" if st.get("reason") else "") + ")")

    def _ready(self, own: str, base: str, name: str) -> None:
        m = self.meta(own, base)
        if name == m["ready"]:
            if m["fails"]:
                self._save(own, base, fails=0, retry_at=0)
            return
        if name != m["want"]:
            return
        keep = m["names"][-KEEP:]                        # (name is the newest: it is the one wanted)
        respell = self._vocab(own, base, name).state().get("respell") or {}
        self._save(own, base, ready=name, names=keep, respell=respell, fails=0, retry_at=0, checked=self.clock())
        self.log(f"aws transcribe: term vocabulary {name} READY: in use on its channels from their next stream")
        if self.leader():
            self._delete([n for n in m["names"] if n not in keep])

    def _delete(self, names) -> None:
        for old in names:
            try:
                self.call("transcribe", "Transcribe.DeleteVocabulary", {"VocabularyName": old}, self.region,
                          self.endpoint)
            except aws.AwsError as e:
                if e.code != "NotFoundException":
                    self.log(f"aws transcribe: term vocabulary {old} not deleted: {e.code or e.status}")

    def cleanup(self) -> int:
        """Every term vocabulary made here deleted ([stt] terms_aws turned off): the leader only. How many."""
        if not self.leader():
            return 0
        n = 0
        for own, base in self.pairs():
            m = self.meta(own, base)
            if m["names"]:
                self._delete(m["names"])
                n += len(m["names"])
            self._save(own, base, want="", ready="", names=[], respell={})
        with self._lock:
            self._put(self.PAIRS, json.dumps({"pairs": []}))
        if n:
            self.log(f"aws transcribe: terms_aws off: {n} term vocabularies deleted")
        return n

    def state(self) -> dict:
        """No phrases: names, states, counts (Settings > Radios > Vocabulary)."""
        out = {}
        for own, base in self.pairs():
            m = self.meta(own, base)
            if m["want"]:
                st = self._vocab(own, base, m["want"]).state()
                out[f"{own}+{base_short(base)}"] = {"want": m["want"], "ready": m["ready"], "state": st["state"],
                                                    "reason": st["reason"], "phrases": st["phrases"]}
        return out
