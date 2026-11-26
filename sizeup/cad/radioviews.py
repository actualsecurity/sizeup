"""The Radio page's views (the rail's VIEWS: Our calls, Police, ...): set in the app, not in code.

Department views: settings_overrides section "radio_views", key "views" -> a list, in the rail's order:

    [{"id": "ours", "name": "Our calls", "mark": "F", "color": "", "filter": {"ours": true}},
     {"id": "u:k3x9q2", "name": "Mutual aid", "mark": "M", "color": "#c2410c",
      "filter": {"services": ["fire", "ems"], "systems": ["westchester"], "sources": ["fm:FG2"], "units": ["E60"],
                 "linked": true}}, ...]

    id       ours | police | ems | transit | air | all (the views that were coded, SEED) or "u:<random>" (made here)
    name     1 to 24 characters
    mark     the badge's letter: one capital letter or digit, or "∗"
    color    the badge's colour "#rrggbb", "" the service's own
    filter   what the view shows. Within a dimension any (OR), across dimensions all (AND):
               ours      our fire channels (Main: dispatch, paging, firegrounds), with the unit notes, as Our calls
               services  sources of these services (sourcekeys.SERVICES)
               systems   sources of these systems (analog, airband, westchester, another P25 system's name)
               sources   these channels / talkgroups (source keys), added to the above
               units     only transmissions by these units (the rig)
               linked    only transmissions on an incident
             No channel dimension (ours, services, systems, sources): every source (All, less the viewer's mutes).

Not set (the default): SEED, today's coded views, so nothing disappears. Admins (POST /api/cad/settings/radio_views
{action: set {views, base} | reset {base}}), audited as Radios, exported with the rest. base: the rev the edit started
from (someone else saved meanwhile: refused).

Each person's own (sizeup/cad/prefs.py namespace "radio_views", static/radio-views.js): their own views ("p:<random>",
same shape), and which views they hide and in what order. Every view hidden: the rail has no VIEWS.

A reader who may not see another P25 system's traffic (#419) is sent no key or system of it; a view that named only
those is not sent at all (it would read as All).
"""

from __future__ import annotations

import hashlib
import json
import re

from sizeup import sourcekeys
from sizeup.cad.radiofolders import context_key, is_key
from sizeup.cad.store import CadError

SECTION, KEY = "radio_views", "views"
MAX_VIEWS, MAX_MINE, NAME_MAX = 30, 20, 24
MAX_SOURCES, MAX_UNITS, MAX_SYSTEMS, UNIT_MAX = 100, 20, 12, 20
SEED_IDS = ("ours", "police", "ems", "transit", "air", "all")
VIEW_ID = re.compile(r"ours|police|ems|transit|air|all|u:[a-z0-9]{4,24}")
MINE_ID = re.compile(r"p:[a-z0-9]{4,24}")
MARK = re.compile(r"[A-Z0-9]|∗")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./_-]{0,19}")
FILTER_KEYS = ("ours", "services", "systems", "sources", "units", "linked")
SEED = [
    {"id": "ours", "name": "Our calls", "mark": "F", "color": "", "filter": {"ours": True}},
    {"id": "police", "name": "Police", "mark": "P", "color": "", "filter": {"services": ["police"]}},
    {"id": "ems", "name": "EMS", "mark": "E", "color": "", "filter": {"services": ["ems"]}},
    {"id": "transit", "name": "Transit", "mark": "T", "color": "", "filter": {"services": ["transit"]}},
    {"id": "air", "name": "Air", "mark": "A", "color": "", "filter": {"services": ["air"]}},
    {"id": "all", "name": "All", "mark": "∗", "color": "", "filter": {}},
]


def _text(v, what: str) -> str:
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = " ".join(v.split())
    if not v:
        raise CadError(f"{what}: required")
    if len(v) > NAME_MAX:
        raise CadError(f"{what}: at most {NAME_MAX} characters")
    if not v.isprintable():
        raise CadError(f"{what}: letters, digits and punctuation")
    return v


def _list(v, what: str, most: int, ok) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise CadError(f"{what}: a list")
    if len(v) > most:
        raise CadError(f"{what}: at most {most}")
    out = []
    for x in v:
        if not ok(x):
            raise CadError(f"{what}: {x!r} is not one")
        if x not in out:
            out.append(x)
    return out


def clean_filter(f, what: str = "filter") -> dict:
    """A view's filter as stored: only the dimensions that say something (no empty list, no false)."""
    if f is None:
        f = {}
    if not isinstance(f, dict) or set(f) - set(FILTER_KEYS):
        raise CadError(f"{what}: {{{', '.join(FILTER_KEYS)}}}")
    out: dict = {}
    for b in ("ours", "linked"):
        if b in f and not isinstance(f[b], bool):
            raise CadError(f"{what}.{b}: true or false")
        if f.get(b):
            out[b] = True
    lists = {
        "services": _list(f.get("services"), f"{what}.services", len(sourcekeys.SERVICES), lambda x: x in sourcekeys.SERVICES),
        "systems": _list(f.get("systems"), f"{what}.systems", MAX_SYSTEMS,
                         lambda x: isinstance(x, str) and bool(sourcekeys.SYSTEM.fullmatch(x))),
        "sources": [sourcekeys.service_key(k) for k in _list(f.get("sources"), f"{what}.sources", MAX_SOURCES, is_key)],
        "units": _list(f.get("units"), f"{what}.units", MAX_UNITS, lambda x: isinstance(x, str) and bool(UNIT.fullmatch(x))),
    }
    for k in ("services", "systems", "sources", "units"):
        if lists[k]:
            out[k] = list(dict.fromkeys(lists[k]))
    return {k: out[k] for k in FILTER_KEYS if k in out}


def clean_view(v, what: str, ids=VIEW_ID) -> dict:
    if not isinstance(v, dict) or set(v) - {"id", "name", "mark", "color", "filter"}:
        raise CadError(f"{what}: {{id, name, mark, color, filter}}")
    vid = v.get("id")
    if not isinstance(vid, str) or not ids.fullmatch(vid):
        raise CadError(f"{what}.id: {'p:' if ids is MINE_ID else 'u:'}<letters and digits>")
    mark = v.get("mark")
    if not isinstance(mark, str) or not MARK.fullmatch(mark):
        raise CadError(f"{what}.mark: one capital letter or digit, or ∗")
    color = v.get("color") or ""
    if not isinstance(color, str) or (color and not COLOR.fullmatch(color)):
        raise CadError(f"{what}.color: #rrggbb or none")
    return {"id": vid, "name": _text(v.get("name"), f"{what}.name"), "mark": mark, "color": color.lower(),
            "filter": clean_filter(v.get("filter"), f"{what}.filter")}


def clean(views, ids=VIEW_ID, most: int = MAX_VIEWS) -> list[dict]:
    """A list of views as stored, or CadError."""
    if not isinstance(views, list):
        raise CadError("views: [{id, name, mark, color, filter}]")
    if len(views) > most:
        raise CadError(f"views: at most {most}")
    out, seen = [], set()
    for i, v in enumerate(views):
        c = clean_view(v, f"views[{i}]", ids)
        if c["id"] in seen:
            raise CadError(f"views: {c['id']} twice")
        seen.add(c["id"])
        out.append(c)
    return out


def stored(ov: dict | None) -> list[dict] | None:
    """The department's views, None when unset or unreadable (SEED)."""
    try:
        return clean(((ov or {}).get(SECTION) or {})[KEY])
    except (KeyError, TypeError, CadError):
        return None


def rev(views: list[dict] | None) -> str:
    """A short fingerprint of the stored views ("" the default)."""
    if views is None:
        return ""
    return hashlib.sha256(json.dumps(views, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()[:12]


def channels(f: dict) -> bool:
    """The filter names channels (else: every source)."""
    return bool(f.get("ours") or f.get("services") or f.get("systems") or f.get("sources"))


def context_system(s: str) -> bool:
    return s not in sourcekeys.OURS_SYSTEMS


def for_viewer_list(views: list[dict], sees_context: bool) -> list[dict]:
    """The views as this reader may know them: without another system's keys and systems; a view that named only those
    is left out (stripped, it would read as All)."""
    if sees_context:
        return [dict(v) for v in views]
    out = []
    for v in views:
        f = dict(v["filter"])
        had = channels(f)
        if "sources" in f:
            f["sources"] = [k for k in f["sources"] if not context_key(k)]
        if "systems" in f:
            f["systems"] = [s for s in f["systems"] if not context_system(s)]
        f = {k: x for k, x in f.items() if x}
        if had and not channels(f):
            continue
        out.append({**v, "filter": f})
    return out


def for_viewer(views: list[dict] | None, sees_context: bool = True) -> dict:
    """What one reader is sent: {rev, custom, views}."""
    return {"rev": rev(views), "custom": views is not None,
            "views": for_viewer_list(SEED if views is None else views, sees_context)}


def merge_hidden(new: list[dict], old: list[dict]) -> list[dict]:
    """An admin who may not see another system's traffic (no sign-in: all-in-one) saved what they saw: what they were
    never shown stays (a view's other-system keys and systems; a view they were never shown, after the one it followed)."""
    out = [dict(v, filter=dict(v["filter"])) for v in new]
    by = {v["id"]: v for v in out}
    prev = None
    for v in old:
        f = v["filter"]
        keys = [k for k in f.get("sources", []) if context_key(k)]
        systems = [s for s in f.get("systems", []) if context_system(s)]
        if v["id"] in by:
            g = by[v["id"]]["filter"]
            if keys:
                g["sources"] = list(dict.fromkeys(g.get("sources", []) + keys))
            if systems:
                g["systems"] = list(dict.fromkeys(g.get("systems", []) + systems))
        elif channels(f) and not channels({**f, "sources": [k for k in f.get("sources", []) if not context_key(k)],
                                           "systems": [s for s in f.get("systems", []) if not context_system(s)]}):
            at = out.index(by[prev]) + 1 if prev in by else 0
            out.insert(at, dict(v))
            by[v["id"]] = out[at]
        if v["id"] in by:
            prev = v["id"]
    return clean(out)


def summary(views: list[dict] | None) -> str:
    """The audit's old / new: each view's name and badge, in order."""
    if views is None:
        return "default"
    s = ", ".join(f"{v['name']} ({v['mark']}{'' if v in SEED else ', ' + _fsum(v['filter'])})" for v in views) or "no views"
    return s if len(s) <= 400 else s[:399] + "…"


def _fsum(f: dict) -> str:
    parts = []
    if f.get("ours"):
        parts.append("ours")
    for k in ("services", "systems", "units"):
        if f.get(k):
            parts.append("/".join(f[k]))
    if f.get("sources"):
        parts.append(f"{len(f['sources'])} ch")
    if f.get("linked"):
        parts.append("linked")
    return " ".join(parts) or "all"


# ---- each person's own (prefs namespace radio_views) ---------------------------------------------------------------

def clean_prefs(v) -> dict:
    """{hidden: [ids], order: [ids], mine: [views "p:..."]}: CadError for any other shape."""
    if not isinstance(v, dict) or set(v) - {"hidden", "order", "mine"}:
        raise CadError("radio_views: {hidden, order, mine}")
    out: dict = {}
    any_id = lambda x: isinstance(x, str) and bool(VIEW_ID.fullmatch(x) or MINE_ID.fullmatch(x))  # noqa: E731
    for k in ("hidden", "order"):
        if k in v:
            out[k] = _list(v[k], f"radio_views.{k}", MAX_VIEWS + MAX_MINE, any_id)
    if "mine" in v:
        out["mine"] = clean(v["mine"], MINE_ID, MAX_MINE)
    return out
