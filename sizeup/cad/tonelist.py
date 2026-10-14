"""Settings > Radios > Tones (#115): every two-tone pair the decoder knows, edited without touching the tone list.

The tone plan (sizeup/radio/tones.py TonePlan) is the county tone list (data/westchester_tones.csv; [[tones]] in the
config) plus what an admin changed here, kept as overrides in the Settings section "tones" (sizeup/cad/settings.py):

    key "832.5/1201.0"               a pair named here ("Name these tones" on a page row, #80, or Add pair):
                                     {name, a_hz, b_hz, ours, page?, type?, confirmed?}  (#84's entries: the first five)
    key "435.1/435.1@Larchmont FD"   an edit to that row of the tone list (A/B Hz @ its name there), never a rewrite of
                                     the file: {list, a_hz, b_hz, name, ours, type?, confirmed?}, or {list, a_hz, b_hz,
                                     deleted: true} (taken out of the plan). "Reset to list" drops the override.

type: fire | ems | police | other (missing: guessed from the name); confirmed: false = an estimate (the plan tags the
name " (est.)", which a heard or listed name outranks); missing: a pair named here is confirmed, a list row is what
the list says (estimated=1: not confirmed). ours goes by the name (IncidentStore, LivePaging): a name marked ours here
makes every pair with that name ours; PCFD's and Rye Brook's names always are.

Heard: the pages in the CAD's communications log (radio_traffic.tones, 90 days unlinked) and unknown_tones.jsonl
(every page with a pair nobody's code matched, #72), one count per page. A pair heard that the plan doesn't match is
"heard but unnamed": decodes of one pair (within the plan's tolerance) are one row, with its latest page to play.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sizeup.cad.store import CadError
from sizeup.radio.tones import ToneCode, TonePlan, base_name, pair_key

TYPES = ("fire", "ems", "police", "other")
TONE_HZ = (250.0, 3500.0)          # the decoder's band (radio/tones.py)
TAGS = (" (heard)", " (est.)")
MAX_UNNAMED = 100
DATA = Path(__file__).resolve().parent.parent.parent / "data"
HOME = {"Port Chester": "Port Chester FD", "Rye Brook": "Rye Brook FD"}    # config.HOME_DEPARTMENTS


def guess_type(name: str) -> str:
    n = name.lower()
    if re.search(r"\b(ems|vac|ambulance|medics?|als|bls|rescue squad)\b", n):
        return "ems"
    if re.search(r"\b(pd|police|sheriff|nysp)\b", n):
        return "police"
    if re.search(r"\b(des|oem|dpw|correctional)\b", n):
        return "other"
    return "fire"


def tag_of(name: str) -> str:
    return next((t for t in TAGS if name.endswith(t)), "")


def list_key(c: ToneCode) -> str:
    """A tone-list row as the override that edits it names it: 'A/B@its name there'."""
    return f"{pair_key(c.a_hz, c.b_hz)}@{base_name(c.name)}"


def is_list_key(key: str) -> bool:
    return "@" in key


# -- an override, checked ---------------------------------------------------------------------------------------

def _text(v, what: str, n: int, required: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = " ".join(v.split())
    if required and not v:
        raise CadError(f"{what}: required")
    if len(v) > n:
        raise CadError(f"{what}: at most {n} characters")
    return v


def clean(v, where: str = "") -> tuple[str, dict]:
    """An override value checked -> (its key, the value stored). Only the fields given are kept (a #84 entry stays
    as it was, so its signature and audit don't change). CadError."""
    what = f"{where}: " if where else ""
    if not isinstance(v, dict):
        raise CadError(f"{what}an object {{a_hz, b_hz, name, ours}}")
    hz = {}
    for k in ("a_hz", "b_hz"):
        x = v.get(k)
        if k == "b_hz" and x in (None, ""):
            hz[k] = None
            continue
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not TONE_HZ[0] <= x <= TONE_HZ[1]:
            raise CadError(f"{what}{k}: {TONE_HZ[0]:.0f} to {TONE_HZ[1]:.0f} Hz")
        hz[k] = round(float(x), 1)
    key = pair_key(hz["a_hz"], hz["b_hz"])
    listed = _text(v.get("list"), f"{what}list", 60, required=False)
    if listed:
        key += f"@{listed}"
    deleted = v.get("deleted", False)
    if not isinstance(deleted, bool):
        raise CadError(f"{what}deleted: true or false")
    if deleted:
        if not listed:
            raise CadError(f"{what}deleted: only a row of the tone list (list: its name there)")
        return key, {"list": listed, "a_hz": hz["a_hz"], "b_hz": hz["b_hz"], "deleted": True}
    name = _text(v.get("name"), f"{what}name", 60)
    if tag_of(name):
        raise CadError(f"{what}name: the department's name alone")
    ours = v.get("ours", False)
    if not isinstance(ours, bool):
        raise CadError(f"{what}ours: true or false")
    out: dict = {"list": listed} if listed else {}
    out.update({"name": name, "a_hz": hz["a_hz"], "b_hz": hz["b_hz"], "ours": ours})
    if v.get("type") not in (None, ""):
        if v["type"] not in TYPES:
            raise CadError(f"{what}type: {', '.join(TYPES)}")
        out["type"] = v["type"]
    if v.get("confirmed") is not None:
        if not isinstance(v["confirmed"], bool):
            raise CadError(f"{what}confirmed: true or false")
        out["confirmed"] = v["confirmed"]
    page = _text(v.get("page"), f"{what}page", 80, required=False)
    if page and not listed:
        out["page"] = page
    return key, out


# -- the plan ---------------------------------------------------------------------------------------------------

def _confirmed_default(c: ToneCode) -> bool:
    return tag_of(c.name) != " (est.)"


def effective(base: Iterable[ToneCode], ov: dict) -> list[ToneCode]:
    """The tone list with the overrides on it: edited rows renamed (tagged by confirmed), removed rows out, the pairs
    named here added."""
    out: list[ToneCode] = []
    keys = set()
    for c in base:
        k = list_key(c)
        keys.add(k)
        o = ov.get(k)
        if o is None:
            out.append(c)
            continue
        if o.get("deleted"):
            continue
        tag = tag_of(c.name)
        if "confirmed" in o:
            tag = (tag if tag == " (heard)" else "") if o["confirmed"] else " (est.)"
        out.append(ToneCode(o["name"] + tag, c.a_hz, c.b_hz, c.agency, source=c.source, fetched=c.fetched))
    for k, o in ov.items():
        if k in keys or o.get("deleted") or not o.get("name"):
            continue                    # a row of a list that has changed under it: as a pair named here
        tag = "" if o.get("confirmed", True) else " (est.)"
        out.append(ToneCode(str(o["name"]) + tag, float(o["a_hz"]), float(o["b_hz"]) if o.get("b_hz") else None,
                            source="named"))
    return out


def ours_names(base_ours: Iterable[str], ov: dict) -> set[str]:
    return set(base_ours) | {str(o["name"]) for o in ov.values() if not o.get("deleted") and o.get("ours") and o.get("name")}


# -- what was heard ---------------------------------------------------------------------------------------------

@dataclass
class Heard:
    """One decoded pair on one page."""
    page: str
    at: float
    channel: str
    a_hz: float
    b_hz: float | None
    voice_at: float | None = None


def heard(comms_rows: Iterable[dict], jsonl: Path | None) -> list[Heard]:
    """Every pair on every page: the communications log's page rows, and unknown_tones.jsonl for pages it no longer
    has (pruned, or from before #64). Newest first."""
    out: list[Heard] = []
    seen: set[str] = set()
    for r in comms_rows:
        try:
            tones = json.loads(r["tones"]) if r.get("tones") else []
        except (TypeError, ValueError):
            continue
        seen.add(str(r["id"]))
        for t in tones:
            try:
                out.append(Heard(str(r["id"]), float(r["start"]), r.get("channel") or "", float(t["a_hz"]),
                                 float(t["b_hz"]) if t.get("b_hz") else None, r.get("voice_at")))
            except (KeyError, TypeError, ValueError):
                continue
    if jsonl is not None:
        try:
            lines = jsonl.read_text().splitlines()
        except OSError:
            lines = []
        for ln in lines:
            try:
                d = json.loads(ln)
                pid = str(d.get("page") or "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                for a, b in d.get("pairs") or []:
                    out.append(Heard(pid, float(d["at"]), str(d.get("channel") or ""), float(a), float(b) if b else None))
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
    out.sort(key=lambda h: -h.at)
    return out


def _near(tol: float, hz: float | None, want: float | None) -> bool:
    if want is None or hz is None:
        return want is None and hz is None
    return abs(hz - want) <= tol * want


def _same(tol: float, a: float, b: float | None, ca: float, cb: float | None) -> bool:
    return _near(tol, a, ca) and _near(tol, b, cb)


def unnamed(events: list[Heard], plan: TonePlan, has_clip=None) -> list[dict]:
    """The pairs heard that the plan doesn't match, one row per pair (its decodes within the tolerance), newest first:
    {a_hz, b_hz, first, last, count, page (the latest), channel, has_audio}."""
    rows: list[dict] = []
    for h in events:                     # newest first: the first decode of a pair is its latest
        if plan.match_pair(h.a_hz, h.b_hz):
            continue
        row = next((r for r in rows if _same(plan.tolerance, h.a_hz, h.b_hz, r["a_hz"], r["b_hz"])), None)
        if row is None:
            if len(rows) >= MAX_UNNAMED:
                continue
            rows.append({"a_hz": round(h.a_hz, 1), "b_hz": round(h.b_hz, 1) if h.b_hz else None, "first": h.at,
                         "last": h.at, "pages": {h.page}, "page": h.page, "channel": h.channel})
        else:
            row["first"] = min(row["first"], h.at)
            row["pages"].add(h.page)
    for r in rows:
        r["count"] = len(r.pop("pages"))
        r["has_audio"] = bool(has_clip(r["page"])) if has_clip else True
    return rows


# -- the table --------------------------------------------------------------------------------------------------

def table(base: list[ToneCode], ov: dict, meta: dict, events: list[Heard], ours: set[str], locked: set[str],
          tolerance: float = 0.015) -> list[dict]:
    """Every pair, the list's and the ones named here: what Settings > Radios > Tones shows. meta: override key ->
    {operator, at}. ours: the names that are ours now; locked: the names that are ours whatever is set here."""
    rows: list[dict] = []
    keys = set()
    for c in base:
        k = list_key(c)
        if k in keys:
            continue                    # the same code twice (the list's and the config's [[tones]]): one row, one edit
        keys.add(k)
        name = base_name(c.name)
        orig = {"name": name, "type": guess_type(name), "ours": name in ours and name in locked, "confirmed": _confirmed_default(c)}
        o = ov.get(k)
        r = {"id": k, "a_hz": c.a_hz, "b_hz": c.b_hz, "origin": "config" if c.source == "config" else
             "heard" if tag_of(c.name) == " (heard)" else "list", "source": c.source, "fetched": c.fetched, "list": orig}
        if o is None:
            r.update(orig, state="list")
        elif o.get("deleted"):
            r.update(orig, state="deleted")
        else:
            r.update(name=o["name"], type=o.get("type") or guess_type(o["name"]),
                     confirmed=o.get("confirmed", orig["confirmed"]), state="edited")
        rows.append(r)
    for k, o in ov.items():
        if k in keys or o.get("deleted") or not o.get("name"):
            continue
        rows.append({"id": k, "a_hz": o["a_hz"], "b_hz": o.get("b_hz"), "origin": "named", "source": "", "fetched": "",
                     "list": None, "name": o["name"], "type": o.get("type") or guess_type(o["name"]),
                     "confirmed": o.get("confirmed", True), "state": "named", "page": o.get("page", "")})
    for r in rows:
        r["ours"] = r["name"] in ours and r["state"] != "deleted"
        r["ours_locked"] = r["name"] in locked
        m = meta.get(r["id"]) or {}
        r["operator"], r["at"] = m.get("operator", ""), m.get("at")
        pages, last = set(), None
        for h in events:
            if _same(tolerance, h.a_hz, h.b_hz, r["a_hz"], r["b_hz"]):
                pages.add(h.page)
                last = h.at if last is None else max(last, h.at)
        r["heard"], r["last"] = len(pages), last
    rows.sort(key=lambda r: (r["name"].lower(), r["a_hz"], r["b_hz"] or 0))
    return rows


# -- names to pick from -----------------------------------------------------------------------------------------

def _rows(path: Path) -> list[dict]:
    try:
        text = path.read_text()
    except OSError:
        return []
    return list(csv.DictReader(ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")))


def county_agencies(data: Path = DATA) -> list[tuple[str, str]]:
    """(name, type) for every agency the county files name: the fire departments (westchester_departments.csv, the
    apparatus roster's departments), the EMS agencies (westchester_ems.csv)."""
    out: list[tuple[str, str]] = []
    for r in _rows(data / "westchester_departments.csv"):
        n = HOME.get(r.get("department", ""), r.get("department", "")).strip()
        if n:
            out.append((n, guess_type(n)))
    for r in _rows(data / "westchester_apparatus.csv"):
        n = HOME.get(r.get("department", ""), r.get("department", "")).strip()
        if n and ("/" not in n or guess_type(n) != "fire"):     # joint units ("Fairview/Greenville/Hartsdale"): no
            out.append((n, guess_type(n)))
    for r in _rows(data / "westchester_ems.csv"):
        n = (r.get("name") or "").strip()
        if n:
            out.append((n, "ems"))
    return out


def suggestions(plan_codes: Iterable[ToneCode], ov: dict, more: Iterable[str], agencies: Iterable[tuple[str, str]]) -> list[dict]:
    """[{name, type}] once each (by name, any case), sorted: every name in the plan (with the type set here), the
    console's departments, the county's agencies, EMS included."""
    typed = {str(o["name"]): o["type"] for o in ov.values() if o.get("name") and o.get("type")}
    seen: dict[str, dict] = {}
    for n, t in ([(base_name(c.name), "") for c in plan_codes] + [(m, "") for m in more] + list(agencies)):
        if n and n.lower() not in seen:
            seen[n.lower()] = {"name": n, "type": typed.get(n) or t or guess_type(n)}
    return sorted(seen.values(), key=lambda s: s["name"].lower())
