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

COUNTY = "westchester"       # the first P25 system (trunk-recorder's shortName): what "tg:<n>" means
SERVICES = ("fire", "police", "ems", "transit", "air", "other")
SYSTEM = re.compile(r"[a-z][a-z0-9_-]{0,23}")
P25_KEY = re.compile(r"p25:(?P<system>[a-z][a-z0-9_-]{0,23}):(?P<tg>\d{1,9})")
TG_KEY = re.compile(r"tg:(?P<tg>\d{1,9})")
SERVICE_WORDS = (("fire", ("fire", "hazmat", "airport incident")), ("police", ("police", " pd ", "sheriff", "wcpd")),
                 ("ems", ("ems", "hospital", "medic", "ambulance")),
                 ("transit", ("transit", "metro-north", "railroad", "mta", " dot ", " bus")), ("air", ("airband", "atc")))


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
    g = f" {str(group or '').lower()} "
    for name, words in SERVICE_WORDS:
        if any(w in g for w in words):
            return name
    return "other"
