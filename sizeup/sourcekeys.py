"""One key for every radio source (GitHub #409; the Radio page's source registry, #415): talkgroup and radio IDs are
only unique inside one P25 system, so a talkgroup is never named by its number alone.

    fm:<label>              an analog channel               fm:FG1, fm:South 2
    air:<label>             an airband channel              air:HPN Tower
    p25:<system>:<tg>       a talkgroup of a P25 system     p25:westchester:307, p25:mrrs:9211

The keys from before this, still read everywhere and still what the county's settings are saved under (a release from
before this reads them back: switching back is safe): "tg:307" is the county system's talkgroup 307 (COUNTY: the first
system), a bare label ("FG1") is that channel. legacy() gives that older form where one exists; another system's
talkgroup has none, and is only ever written qualified.

Each row of the radio log carries its source (sizeup/cad/comms.py: `source`, `system`, `group`, `service`, `ours`,
`dept`, `tier`, `safety`), so nothing downstream has to read a talkgroup number alone.

    service    fire | police | ems | transit | air | other: the talkgroups file's "Service" column, else from its
               group ("Tag") by the words below
    ours       in "Our calls": our fire channels (the analog ones, and the talkgroups marked Main). Never another
               system's talkgroup
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar

COUNTY = "westchester"       # the first P25 system (trunk-recorder's shortName): what "tg:<n>" means
SERVICES = ("fire", "police", "ems", "transit", "air", "other")
SYSTEM = re.compile(r"[a-z][a-z0-9_-]{0,23}")
P25_KEY = re.compile(r"p25:(?P<system>[a-z][a-z0-9_-]{0,23}):(?P<tg>\d{1,9})")
TG_KEY = re.compile(r"tg:(?P<tg>\d{1,9})")
# service by the group's words, whole words only ("Law Dispatch" is police: no "atc" inside "dispatch"); the first
# rule that matches wins (police before fire: "Fire Police"; fire before EMS: "Fire/EMS"). A talkgroups file's own
# "Service" column always wins over this (the other systems' files have one).
SERVICE_WORDS = (("police", {"police", "pd", "law", "sheriff", "wcpd", "mtapd", "nysp", "le"}),
                 ("fire", {"fire", "hazmat"}),
                 ("ems", {"ems", "hospital", "medic", "medics", "ambulance"}),
                 ("transit", {"transit", "railroad", "rail", "mta", "dot", "bus", "metro-north", "mnr"}),
                 ("air", {"airband", "atc", "aviation"}))
OURS_SYSTEMS = ("", COUNTY, "analog", "airband")      # the systems whose rows are not "context"
RESERVED_SYSTEMS = tuple(x for x in OURS_SYSTEMS if x)  # never another P25 system's name (systems.discover refuses it)
OURS_SQL = "(" + ", ".join(f"'{x}'" for x in OURS_SYSTEMS) + ")"

# -- who may be shown another system's traffic (#419: police / context transcripts and audio only behind a sign-in) --
#
# One predicate, may_see_context(). A request sets it for its own thread (request_context(who): a verified,
# signed-in person; `who` is only ever set by the server when Cloudflare Access verified them). Outside a request
# (a state built for the stream every client shares) it is what the builder passes: True only when every listener
# of that stream was verified, i.e. the server enforces Access. Everywhere else (the station's own page, all-in-one,
# a hub with no Access) another system's traffic is not served at all: no rows, no text, no ids, no audio.
_sees: ContextVar = ContextVar("sizeup_sees_context", default=None)


def may_see_context(broadcast_ok: bool = False) -> bool:
    v = _sees.get()
    return bool(broadcast_ok) if v is None else bool(v)


@contextmanager
def request_context(who):
    token = _sees.set(who is not None)
    try:
        yield
    finally:
        _sees.reset(token)


_context_tgs: frozenset[int] = frozenset()


def set_context_talkgroups(tgs) -> None:
    """The talkgroup numbers of the other P25 systems here (the app, from its config): the lists a person edits
    (the alarms' channels, the 60 Control feed, the status hints) refuse them, see context_talkgroups()."""
    global _context_tgs
    _context_tgs = frozenset(int(t) for t in tgs)


def context_talkgroups() -> frozenset[int]:
    return _context_tgs


def is_context(row) -> bool:
    """A row of the radio log (or a transmission as a dict) from another P25 system: context only. Every reader
    that acts on what was said (alarms, the feed, status hints, the pre-alerts) leaves it alone."""
    try:
        s = row.get("system") if isinstance(row, dict) else row["system"]
    except (KeyError, IndexError, TypeError):
        s = ""
    return bool(s) and s not in OURS_SYSTEMS


def other_p25(row) -> bool:
    """A transmission (a row or a dict) on another P25 system's talkgroup: a talkgroup, and a system that is neither
    unset nor the county's. What Comms.record() and Comms.retext() route to on_context_text, never to on_text. With
    RESERVED_SYSTEMS refused as a system's name it agrees with is_context() on every P25 row."""
    try:
        tg = row.get("talkgroup") if isinstance(row, dict) else row["talkgroup"]
        s = row.get("system") if isinstance(row, dict) else row["system"]
    except (KeyError, IndexError, TypeError):
        return False
    return tg is not None and str(s or "") not in ("", COUNTY)


def refuse_context(tgs, what: str) -> None:
    """ValueError when a list a person configures names a talkgroup of another system."""
    bad = sorted(int(t) for t in tgs if int(t) in _context_tgs)
    if bad:
        raise ValueError(f"{what}: talkgroup {bad[0]} is another P25 system's (context only): it can't be listed here")


def p25(system: str, tg: int) -> str:
    return f"p25:{system or COUNTY}:{int(tg)}"


def fm(label: str, kind: str = "") -> str:
    return f"{'air' if kind == 'airband' else 'fm'}:{label}"


def parse(key: str, county: str = COUNTY) -> tuple[str, int] | None:
    """(system, talkgroup) for a talkgroup's key, qualified or the older "tg:<n>" (the county's); None for a channel."""
    m = P25_KEY.fullmatch(str(key))
    if m:
        return m["system"], int(m["tg"])
    m = TG_KEY.fullmatch(str(key))
    return (county, int(m["tg"])) if m else None


def is_talkgroup(key: str) -> bool:
    return parse(key) is not None


def legacy(key: str, county: str = COUNTY) -> str:
    """The key as it was before sources were qualified ("tg:307", "FG1"), for what is saved and sent under the older
    names; a talkgroup of another system stays as it is (it never had one)."""
    key = str(key)
    got = parse(key, county)
    if got is not None:
        return f"tg:{got[1]}" if got[0] == county else p25(*got)
    return key.split(":", 1)[1] if key.startswith(("fm:", "air:")) else key


def channel_key(system: str, tg: int, county: str = COUNTY) -> str:
    """The key a talkgroup's speech-to-text choices are under (sizeup/channels.py): "tg:<n>" for the county's (as
    saved before), the qualified one for any other system's."""
    return f"tg:{int(tg)}" if (system or county) == county else p25(system, tg)


def of_row(channel: str, talkgroup, system: str = "", air: bool = False, county: str = COUNTY) -> str:
    """The source of a row of the radio log."""
    if talkgroup is None:
        return fm(channel, "airband" if air else "")
    return p25(system or county, int(talkgroup))


def service(group: str = "", listed: str = "", kind: str = "") -> str:
    """fire | police | ems | transit | air | other for a source: as listed, else by its kind and its group's words."""
    v = str(listed or "").strip().lower()
    if v in SERVICES:
        return v
    if kind == "airband":
        return "air"
    if kind in ("paging", "traffic", "fireground"):
        return "fire"
    low = str(group or "").lower()
    words = set(re.findall(r"[a-z0-9]+", low)) | set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", low))
    for name, any_of in SERVICE_WORDS:
        if words & any_of:
            return name
    return "other"
