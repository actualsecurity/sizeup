"""Feed types: the department's own mapping of a feed's type codes to ours (Settings > Lists > Feed types).

A county feed sends its own code beside ours ("MAIN,SUB": the adapter's extra "county_type"; a feed without one, its
type as it is). Cad.map_type reads it in this order:

    1. a row here       {feed, county code} -> {type, subtype}: the exact "MAIN,SUB" first, else "MAIN" alone (whose
                        subtype, when the row has none, is still matched as in 3). Wins over everything else
    2. automatic        our code as it is, a type moved under another, a page's words (Cad.map_type)
    3. the subtype      our TYPE with no subtype and the feed's SUB: the one subtype of that TYPE it names (code or
                        name, case and punctuation aside, a plural or a short form: Lists.match_subtype). None or two:
                        no subtype and a timeline line ("... subtype X not in Service's subtypes"), never another
                        type's

Rows are the hub's settings data (settings_overrides, section SECTION, key "<feed>|<code>"), set by admins and audited
as Lists; never in the repo. The codes each feed sent are counted here (feed_types_seen, the CAD file: the department's
data) so Settings can list the ones nothing maps fully, with a one-click Map. Logs never carry a code.
"""

from __future__ import annotations

import re

SECTION = "feed_types"
SEEN_ROWS = 300                       # the seen codes kept (the latest)
SEEN_DAYS = 60                        # ... and shown when seen this recently
SHOWN = 40
SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")      # sizeup/sources SOURCE_ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_types_seen (
  source TEXT NOT NULL,                 -- the feed (source_reports.source)
  code TEXT NOT NULL,                   -- the feed's own code as sent ("MAIN,SUB"), spaces folded
  label TEXT NOT NULL DEFAULT '',
  sent TEXT NOT NULL DEFAULT '',        -- the type the adapter sent with it (ours when it mapped it)
  n INTEGER NOT NULL DEFAULT 0,         -- events seen with it
  first_at REAL NOT NULL,
  last_at REAL NOT NULL,
  PRIMARY KEY (source, code)
);
"""


def split(raw) -> tuple[str, str]:
    """'SERVICE, SAMPLE' / 'SERVICE/SAMPLE' -> ('SERVICE', 'SAMPLE'); 'SERVICE' -> ('SERVICE', '')."""
    main, _, sub = re.sub(r"\s*[,/]\s*", ",", " ".join(str(raw or "").split()).upper(), count=1).partition(",")
    return main.strip(), sub.strip()


def norm(raw) -> str:
    """A code as rows are keyed: each part's letters and digits only ('Service , Sample-1' -> 'SERVICE,SAMPLE1')."""
    main, sub = (re.sub(r"[^A-Z0-9]", "", p) for p in split(raw))
    return f"{main},{sub}" if main and sub else main


def row_key(source: str, code: str) -> str:
    return f"{source}|{norm(code)}"


def parse_key(k: str) -> tuple[str, str]:
    source, _, code = str(k).partition("|")
    return source, code


def clean(source, code, typ, sub, lists) -> tuple[str, dict]:
    """(the row's key, its value) checked against the lists: ValueError with the reason."""
    source = str(source or "").strip()
    if not SOURCE.fullmatch(source):
        raise ValueError("feed: a source id")
    if not isinstance(code, str) or not norm(code) or len(code) > 80:
        raise ValueError("county code: MAIN or MAIN,SUB")
    typ, sub = str(typ or "").strip().upper(), str(sub or "").strip().upper()
    lists.check_type(typ, sub, required=True)
    return row_key(source, code), {"type": typ, "subtype": sub}


def lookup(rows: dict, source: str, code: str, lists) -> tuple[str, str, bool] | None:
    """The row for this feed's code: (TYPE, SUBTYPE, exact: the "MAIN,SUB" row), else None. A row the lists no longer
    allow (its type retired, its subtype moved) is passed over: the automatic mapping goes on."""
    if not rows or not source or not norm(code):
        return None
    k = norm(code)
    for c, exact in ((k, True), (k.partition(",")[0], "," not in k)):
        v = rows.get(f"{source}|{c}")
        if not isinstance(v, dict):
            continue
        try:
            lists.check_type(v.get("type") or "", v.get("subtype") or "", required=True)
        except ValueError:
            continue
        return v["type"], v.get("subtype") or "", exact
    return None


MISS = re.compile(r"^.{1,60} subtype .{1,40} (?:not in|matches several of) .{1,60}'s subtypes")


def miss_note(label: str, said: str, type_name: str, hits=()) -> str:
    """The line for a feed's subtype word that names none (or several) of our type's subtypes."""
    head = f"{label or 'Feed'} subtype {' '.join(str(said).split())[:40]}"
    return (f"{head} matches several of {type_name}'s subtypes: {', '.join(hits)}" if hits
            else f"{head} not in {type_name}'s subtypes")


def is_miss(note: str) -> bool:
    return bool(MISS.match(note or ""))


def note_seen(db, source: str, code: str, label: str, sent: str, at: float) -> None:
    """One more event seen with this code (the caller's transaction); the oldest dropped past SEEN_ROWS."""
    code = " ".join(str(code or "").split())[:80]
    if not code or not source:
        return
    db.execute("INSERT INTO feed_types_seen (source, code, label, sent, n, first_at, last_at) VALUES (?, ?, ?, ?, 1, ?, ?) "
               "ON CONFLICT(source, code) DO UPDATE SET n = n + 1, last_at = excluded.last_at, label = excluded.label, "
               "sent = excluded.sent", (source, code, label[:60], str(sent or "")[:80], at, at))
    db.execute("DELETE FROM feed_types_seen WHERE rowid NOT IN (SELECT rowid FROM feed_types_seen ORDER BY last_at DESC "
               "LIMIT ?)", (SEEN_ROWS,))
