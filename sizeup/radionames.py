"""One name per radio source (admin, 2026-11-25: a talkgroup showed one name on the Radio rail and another, ambiguous one
on every chip): what a person reads for a channel, talkgroup or frequency, everywhere, comes from here (the hub) and
static/radio-names.js (the page), both from the same admin store in Settings > Radios > Channels.

    name     the admins' display name (radio_folders' `names`, sizeup/cad/radiofolders.py: the rail's Edit and Settings >
             Radios > Channels write the same store), else the source's own label (its talkgroups file's Alpha Tag, the
             configured FM / airband channel's label)
    short    the admins' short label for chips (the settings section radio_shorts: a second field in the same Settings
             row), else the name
    title    "<system> · <name> (TG <n>)" / "<system> · <name> (<MHz> MHz)": the hover, so a short or a generic name is
             never ambiguous

Display only, as radiofolders.py says of names: keys, the radio log's `channel` column, recordings, transcripts,
matchers, intel rules, exports and every comparison keep the real label and number.

The CAD of record sets this from its settings (Settings._apply: use()) and its systems (system labels: County, the
other P25 systems' labels, Analog, Airband); a station never does (it shows no names). Module state, as
sizeup/sourcekeys.py keeps the admins' services: one process, one CAD.
"""

from __future__ import annotations

from sizeup import sourcekeys

SECTION = "radio_shorts"       # settings_overrides section: {qualified source key: short}
SHORT_MAX = 24
_names: dict[str, str] = {}
_shorts: dict[str, str] = {}
_systems: dict[str, str] = {}
_freqs: dict[str, str] = {}


def qualified(key) -> str:
    """A source key as names are kept: "tg:307" is the county's p25:westchester:307; fm: / air: as they are."""
    got = sourcekeys.parse(str(key or ""))
    return sourcekeys.p25(*got) if got else str(key or "")


def key_of(row) -> str:
    """A row's source key: its own `source`, else from its talkgroup (and system) or its channel label (a talking-now
    entry: p25_system; an FM / airband channel: fm:<label>, air:<label> when it is airband)."""
    if not isinstance(row, dict):
        return ""
    if row.get("source"):
        return qualified(row["source"])
    tg = row.get("talkgroup", row.get("tg"))
    if tg is not None and str(tg).strip().isdigit():
        system = str(row.get("p25_system") or row.get("system") or "")
        return sourcekeys.p25(sourcekeys.COUNTY if system in ("", "analog", "airband", "fm") else system, int(tg))
    ch = str(row.get("channel") or "")
    if not ch:
        return ""
    air = row.get("system") == "airband" or row.get("service") == "air" or f"air:{ch}" in _names or f"air:{ch}" in _shorts
    return f"{'air' if air else 'fm'}:{ch}"


def clean_short(v) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise ValueError("short: text")
    v = " ".join(v.split())
    if len(v) > SHORT_MAX:
        raise ValueError(f"short: at most {SHORT_MAX} characters")
    if v and not v.isprintable():
        raise ValueError("short: printable text")
    return v


def clean_shorts(m) -> dict[str, str]:
    """{key: short} as kept: qualified keys of a source's shape, non-blank shorts (anything else dropped)."""
    from sizeup.cad import radiofolders
    out: dict[str, str] = {}
    for k, v in (m.items() if isinstance(m, dict) else ()):
        try:
            s = clean_short(v)
        except ValueError:
            continue
        if radiofolders.is_key(k) and s:
            out[qualified(k)] = s
    return dict(sorted(out.items()))


def use(names=None, shorts=None, systems=None, freqs=None) -> None:
    """The CAD's choices (each None: kept as it is): names {key: name} (radio_folders' names), shorts, system labels
    {system: label}, FM / airband frequencies {label: MHz}."""
    global _names, _shorts, _systems, _freqs
    if names is not None:
        _names = {qualified(k): str(v) for k, v in dict(names).items() if v}
    if shorts is not None:
        _shorts = clean_shorts(shorts)
    if systems is not None:
        _systems = {str(k): str(v) for k, v in dict(systems).items()}
    if freqs is not None:
        _freqs = {str(k): str(v) for k, v in dict(freqs).items()}


def real(row) -> str:
    """The source's own label (its Alpha Tag, its channel's label): what the radio log keeps."""
    if not isinstance(row, dict):
        return str(row or "")
    ch = str(row.get("channel") or "")
    if ch:
        return ch
    k = key_of(row)
    got = sourcekeys.parse(k)
    return f"TG {got[1]}" if got else k.split(":", 1)[-1]


def name(row) -> str:
    """The full display name (a row, or a bare key)."""
    row = row if isinstance(row, dict) else {"source": row}
    return _names.get(key_of(row)) or real(row)


def short(row) -> str:
    """The chip's label."""
    row = row if isinstance(row, dict) else {"source": row}
    return _shorts.get(key_of(row)) or name(row)


SYS_DEFAULT = {"analog": "Analog", "airband": "Airband", sourcekeys.COUNTY: "County"}


def system_label(row) -> str:
    k = key_of(row if isinstance(row, dict) else {"source": row})
    got = sourcekeys.parse(k)
    sysname = got[0] if got else "airband" if k.startswith("air:") else "analog"
    return _systems.get(sysname) or SYS_DEFAULT.get(sysname) or sysname


def title(row) -> str:
    """The hover: "<system> · <name> (TG n | MHz)"."""
    row = row if isinstance(row, dict) else {"source": row}
    k = key_of(row)
    got = sourcekeys.parse(k)
    mhz = row.get("mhz") if isinstance(row.get("mhz"), (int, float)) and row.get("mhz") > 0 else _freqs.get(k.split(":", 1)[-1])
    num = f"TG {got[1]}" if got else f"{float(mhz):g} MHz" if mhz else ""
    full = name(row)
    if full != real(row):
        num = ", ".join(x for x in (real(row), num) if x)
    return f"{system_label(row)} · {full}" + (f" ({num})" if num else "")


def for_viewer(sees_context: bool, hidden_labels=()) -> dict:
    """GET /api/cad/radio-names for one reader: {rev, names, shorts, systems, freqs}; a reader who may not see another
    system's traffic gets none of its keys or its label."""
    from sizeup.cad import radiofolders
    ok = (lambda k: True) if sees_context else (lambda k: not radiofolders.context_key(k))
    hide = {str(x).strip().lower() for x in hidden_labels}
    systems = {k: v for k, v in _systems.items()
               if sees_context or (k in sourcekeys.OURS_SYSTEMS and v.strip().lower() not in hide)}
    return {"rev": rev(), "names": {k: v for k, v in _names.items() if ok(k)},
            "shorts": {k: v for k, v in _shorts.items() if ok(k)}, "systems": systems, "freqs": dict(_freqs)}


def rev() -> str:
    """The names' and shorts' fingerprint ("" none): the state push carries it; a page asks again when it moves."""
    import hashlib
    import json
    if not _names and not _shorts:
        return ""
    raw = json.dumps([_names, _shorts], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]

