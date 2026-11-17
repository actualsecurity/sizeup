"""The Radio page's folders (#607): how the rail's channel tree is grouped, set by an admin for the department.

Not set (the default): the automatic grouping (static/radio-nav.js): a heading per system (County, another P25
system, Analog, Airband), a folder per group under it (a talkgroup's Tag in the trunk-recorder talkgroups file;
"Analog", "Airband" for the FM and airband channels), the channels in the hub's order (GET /api/cad/comms/sources).

Set: settings_overrides section "radio_folders", key "layout" ->

    {"folders": [{"id": "d:westchester:Fire", "name": "County fire", "section": "County",
                  "items": ["p25:westchester:1234", ...]},
                 {"id": "u:k3x9q2", "name": "Fireground", "section": "", "items": ["fm:FG1", ...]}, ...]}

    id       "d:<system>:<group>" a default folder (renamed, moved: a new talkgroup of that group still lands in it), or
             "u:<random>" one an admin made
    name     the folder's name (1 to 40 characters)
    section  the heading over it (folders in a row with the same heading share one; "" none)
    items    source keys (sizeup/sourcekeys.py: fm:, air:, p25:), in order; each in one folder only (the first wins)

The folders and their order as listed. A source in no folder (a talkgroup heard for the first time, one never placed)
goes to its default folder: there if that folder is listed, else that folder made again next to its heading's last
folder. Nothing ever disappears from the rail. A key no source has now is kept and simply not shown (a talkgroup
not heard lately comes back where it was). Folded / unfolded is each viewer's own (the browser's), never here.

Admins (POST /api/cad/settings/radio_folders), audited as Radios, exported with the rest. Everyone signed in sees it;
a viewer who may not see another system's traffic gets none of its keys, and no folder holding only those.
"""

from __future__ import annotations

import hashlib
import json
import re

from sizeup import sourcekeys
from sizeup.cad.store import CadError

SECTION, KEY = "radio_folders", "layout"
MAX_FOLDERS, MAX_ITEMS, NAME_MAX, KEY_MAX = 200, 2000, 40, 120
FOLDER_ID = re.compile(r"d:[^\x00-\x1f\x7f]{1,160}|u:[a-z0-9]{4,24}")
ITEM = re.compile(r"(?:fm|air):[^\x00-\x1f\x7f]{1,100}")


def _name(v, what: str, required: bool) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = " ".join(v.split())
    if required and not v:
        raise CadError(f"{what}: required")
    if len(v) > NAME_MAX:
        raise CadError(f"{what}: at most {NAME_MAX} characters")
    return v


def is_key(k) -> bool:
    """A source key's shape (fm:, air:, p25:, the county's older tg:)."""
    return isinstance(k, str) and len(k) <= KEY_MAX and (bool(ITEM.fullmatch(k)) or sourcekeys.parse(k) is not None)


def context_key(k: str) -> bool:
    """Another P25 system's talkgroup (context only: #419)."""
    got = sourcekeys.parse(k)
    return got is not None and got[0] not in sourcekeys.OURS_SYSTEMS


def clean(v) -> dict:
    """The layout as stored, or CadError for a shape that isn't one. An item that isn't a source key, or one already
    in an earlier folder, is dropped (not an error): the rail shows each source once."""
    if not isinstance(v, dict) or set(v) - {"folders"} or not isinstance(v.get("folders"), list):
        raise CadError("layout: {folders: [{id, name, section, items}]}")
    fs = v["folders"]
    if len(fs) > MAX_FOLDERS:
        raise CadError(f"folders: at most {MAX_FOLDERS}")
    out, ids, seen = [], set(), set()
    for i, f in enumerate(fs):
        if not isinstance(f, dict) or set(f) - {"id", "name", "section", "items"}:
            raise CadError(f"folders[{i}]: {{id, name, section, items}}")
        fid = f.get("id")
        if not isinstance(fid, str) or not FOLDER_ID.fullmatch(fid):
            raise CadError(f"folders[{i}].id: d:<system>:<group> or u:<letters and digits>")
        if fid in ids:
            raise CadError(f"folders: {fid} twice")
        ids.add(fid)
        items = f.get("items", [])
        if not isinstance(items, list):
            raise CadError(f"folders[{i}].items: a list")
        keep = []
        for k in items:
            if is_key(k) and k not in seen:
                seen.add(k)
                keep.append(k)
        out.append({"id": fid, "name": _name(f.get("name"), f"folders[{i}].name", True),
                    "section": _name(f.get("section"), f"folders[{i}].section", False), "items": keep})
    if len(seen) > MAX_ITEMS:
        raise CadError(f"folders: at most {MAX_ITEMS} channels in all")
    return {"folders": out}


def rev(layout: dict | None) -> str:
    """A short fingerprint of the stored layout ("" the default): the page's edit is based on one, and the sources'
    rev moves with it."""
    if not layout:
        return ""
    return hashlib.sha256(json.dumps(layout, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def stored(ov: dict | None) -> dict | None:
    """The section's layout, None when unset or unreadable (the default grouping)."""
    try:
        return clean(((ov or {}).get(SECTION) or {})[KEY])
    except (KeyError, TypeError, CadError):
        return None


def context_folder(fid: str) -> bool:
    """A default folder of another P25 system ("d:metro:Police"): its id names that system."""
    return fid.startswith("d:") and fid[2:].split(":", 1)[0] not in sourcekeys.OURS_SYSTEMS


def for_viewer(layout: dict | None, sees_context: bool = True, hidden_labels=()) -> dict | None:
    """What one reader is sent: {rev, folders} or None (the default). Without context: none of another system's keys,
    no folder that held only those, no default folder of another system (its visible channels go to their own default
    folder on the page), and no heading that is another system's label (hidden_labels)."""
    if not layout:
        return None
    hide = {str(x).strip().lower() for x in hidden_labels}
    folders = []
    for f in layout["folders"]:
        if sees_context:
            folders.append(dict(f))
            continue
        items = [k for k in f["items"] if not context_key(k)]
        if (f["items"] and not items) or context_folder(f["id"]):
            continue
        folders.append({**f, "items": items, "section": "" if f["section"].lower() in hide else f["section"]})
    return {"rev": rev(layout), "folders": folders}


def merge_hidden(new: dict, old: dict | None) -> dict:
    """An admin who may not see another system's traffic (no sign-in: all-in-one) saved what they saw: what they were
    never shown stays where it was. Their keys stay in the same folder if it is still there; a folder they were never
    shown (one that held only such keys, another system's default folder) is put back after the folder it followed."""
    if not old:
        return new
    out = new["folders"]
    by = {f["id"]: f for f in out}
    placed = {k for f in out for k in f["items"]}
    prev = None
    for f in old["folders"]:
        hidden = [k for k in f["items"] if context_key(k) and k not in placed]
        if f["id"] in by:
            by[f["id"]]["items"] += hidden
        elif context_folder(f["id"]) or (f["items"] and len(hidden) == len(f["items"])):
            g = {**f, "items": hidden}
            at = out.index(by[prev]) + 1 if prev in by else 0
            out.insert(at, g)
            by[g["id"]] = g
        placed.update(hidden)
        if f["id"] in by:
            prev = f["id"]
    return clean({"folders": out})


def summary(layout: dict | None) -> str:
    """The audit's old / new: each folder's name and how many channels it holds, in order."""
    if not layout:
        return "default"
    s = ", ".join(f"{f['name']} ({len(f['items'])})" for f in layout["folders"]) or "no folders"
    return s if len(s) <= 400 else s[:399] + "…"
