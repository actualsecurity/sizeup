"""A pre-alert's context bundle: everything known around one pre-alert, as plain data. The CAD's More info page
(/cad#prealert/<kind>/<id>, static/prealert-info.js) renders it, and the inference layer will read the very same
bundle, so what a person sees and what a model is given never drift apart. Read only: nothing here writes, captures
or fetches anything new; every list is capped.

    build(cad, kind, pid, admin=False, may="viewer")   the bundle (NotFound: no such pre-alert, or not this reader's)
    page_path(kind, pid) / page_url(public_url, kind, pid)   the page's address (the push's click link, a later PR)

Kinds (the event's stable identity is (kind, id); the id is the table's own):

    airport   airport_alerts (sizeup/cad/airport_alerts.py): HPN ATC, ADS-B emergencies, 60 Control's airport alert
              levels. Evidence kept: the transmissions as snippets ({id, at, channel, text, hits, context}: the text
              itself, so it outlives the radio log), the aircraft named (a snapshot each, with its fix time), the
              location words and an approximate point, the ADS-B details (lines), the outcome (who, when, the note)
    intel     intel_events (sizeup/intel/events.py): radio pre-alerts. Evidence kept: the transmissions by id only
              (traffic_ids; their words are the radio log's, kept as long as it keeps them), the rules with their
              weights, the scores, the gazetteer's place, the hits per transmission (intel_hits, 30 days) and the
              verdicts (intel_feedback)

The bundle (SCHEMA_VERSION; every section says why it is empty in "empty", "" when it isn't):

    event         {kind, id}: stable; a later PR keeps an append-only log of updates per event against it
    schema, generated_at, cursor   the cursor changes whenever anything shown changes (a poll compares it)
    summary       title, source, category, when, the scores
    why           the rules / categories that fired, their weights, the words matched. Rule phrases (an intel pack's
                  patterns) only for an admin, as Settings > System > Intel (api.py ADMIN_READS); the words heard are
                  the transcript's own and shown to every reader
    trigger       the evidence transmissions: time, channel, talker / unit, the full transcript, has_audio (the clip's
                  audio is played from /api/audio/<id>.wav while the hub keeps it), the words each matched
    before_after  the same and related channels (WINDOW_S each side, per source) around the trigger, oldest first: the
                  nearest CAP["before"] before the first evidence and CAP["after"] from it on
    place         the point (approximate when it is), the words or the gazetteer's label, other readings, and what is
                  near: pre-plan points (hydrants, Knox boxes ... as entered), cameras (when the app wired a lookup).
                  Only for a site (SITE_PRECISIONS): an intel place confirmed, or unconfirmed but sure with no other
                  reading ("unconfirmed": true); never ATC's approximate point
    site_history  strictly before the pre-alert: the calls at that address or site (the address key, as History here;
                  the airport: its AIRPORT events), newest first, with their comments, disposition, units and times;
                  the earlier pre-alerts there; the address notes (premise notes the reader may see). Rule hits a
                  non-admin sees: the rules the event fired on, or ones that map or push now
    now           open calls near it and the units on them; for the airport, the aircraft as recorded with the alert
                  (only those it names: the traffic around it is not kept)
    outcome       accepted / dismissed / expired / ended, by whom and when; the event it made or is near; the verdicts

Another P25 system's transmissions (an intel pre-alert's) are in it only for a reader who may see them: the caller
sets sizeup.sourcekeys.request_context (the API does), else comms leaves them out, as everywhere."""

from __future__ import annotations

import hashlib
import json

from sizeup import sourcekeys
from sizeup.cad import addrkey
from sizeup.cad.store import CadError
from sizeup.intel.model import lowest

SCHEMA_VERSION = 1
KINDS = ("airport", "intel")
OPEN = ("queued", "pending", "active")
# before / after the trigger, each side, per source (airport_alerts' source, or "intel"); a caller may pass its own
WINDOW_S = {"atc": 300.0, "radio": 300.0, "adsb": 600.0, "intel": 600.0}
CAP = {"trigger": 12, "before": 30, "after": 30, "site_calls": 10, "comments": 5, "site_prealerts": 10, "notes": 10,
       "preplan": 12, "cameras": 3, "now_calls": 10, "feedback": 20, "related_channels": 6}
SITE_YEARS = 5                  # site history goes back this far at most
NOW_NEAR_M = 1500.0             # "now": open calls this close to the point
# an intel place good enough to be a site (address key, notes, pre-plan points, earlier calls): these precisions (a
# block or a street is a stretch, never a building), confirmed by the words; or unconfirmed but the gazetteer sure
# (where at least UNCONFIRMED_WHERE) with no other reading: police rarely say the town ("respond to 120 Example Ave"),
# and those are the addresses notes matter at. Then the bundle says "unconfirmed place". ATC's point: never a site
SITE_PRECISIONS = ("address", "intersection", "landmark")
UNCONFIRMED_WHERE = 0.75
TEXT_MAX = 600                  # a remark or a comment, clipped


class NotFound(Exception):
    """No such pre-alert (or one this reader may not see): the API's 404, never a hint that it exists."""


def page_path(kind: str, pid: int) -> str:
    """'/cad#prealert/intel/12': only the kind and the id (the page asks for the rest, signed in)."""
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    return f"/cad#prealert/{kind}/{int(pid)}"


def page_url(public_url: str, kind: str, pid: int) -> str:
    """The page's full address for a push's click ('' without a public address: no link)."""
    base = (public_url or "").rstrip("/")
    return base + page_path(kind, pid) if base else ""


def _empty(items: list, why: str) -> dict:
    return {"items": items, "empty": "" if items else why}


def _clip(v, n: int = TEXT_MAX) -> str:
    t = str(v or "")
    return t if len(t) <= n else t[:n - 1] + "…"


def _incident(cad, iid) -> dict | None:
    if not iid:
        return None
    r = cad.store.one("SELECT id, number, type, subtype, status, address, lat, lon, created_at, closed_at, disposition "
                      "FROM incidents WHERE id = ?", (int(iid),))
    return dict(r) if r else None


# -- transmissions ----------------------------------------------------------------------------------------------------

TX_KEYS = ("id", "start", "end", "channel", "source", "system", "service", "talkgroup", "talker", "radio_id", "rig",
           "dispatcher", "model", "status", "emergency", "atc_text", "fixed_text", "fixes", "atc_snaps", "atc_weak",
           "carrier_db", "mhz", "voice_at")


def _tx(cad, r: dict) -> dict:
    """A transmission as the bundle carries it: the transcript in full ("text": as transcribed, so the row's "As
    transcribed" hover has it; "shown": the written form when that differs: ATC's, the status words put back), whether
    its audio is still kept."""
    out = {k: r.get(k) for k in TX_KEYS if r.get(k) not in (None, "", [])}
    out["text"] = r.get("text") or ""
    shown = r.get("atc_text") or r.get("fixed_text") or out["text"]
    if shown != out["text"]:
        out["shown"] = shown
    out["transcript_kept"] = True
    out["has_audio"] = bool(cad.has_clip(r["id"]))
    return out


def _rows(cad, where: list, args: list, limit: int) -> list[dict]:
    """Radio log rows, newest first, decorated as the Radio view shows them; another system's only for a reader who
    may see them (comms._fetch)."""
    rows = cad.comms._fetch(where, args, limit)
    cad.comms._decorate(rows)
    return rows


def _by_ids(cad, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = _rows(cad, [f"t.id IN ({','.join('?' * len(ids))})"], list(ids), len(ids))
    return {r["id"]: r for r in rows}


def _fetch_asc(cad, where: list, args: list, limit: int) -> list[dict]:
    """_rows, oldest first (comms._fetch's own gate)."""
    rows = cad.comms._fetch(where, args, limit, order="ASC")
    cad.comms._decorate(rows)
    return rows


def _window(cad, found: list[dict], window_s: float, evidence: set[str]) -> dict:
    """The same and related channels (the same system and talkgroup group) around the trigger, oldest first: up to
    CAP["before"] before the first evidence (the nearest ones) and CAP["after"] from it on (the evidence among them)."""
    if not found:
        return {"window_s": window_s, "channels": [], "items": [], "empty": "no transmission to look around"}
    lo = min(r["start"] for r in found) - window_s
    hi = max(r.get("end") or r["start"] for r in found) + window_s
    chans = list(dict.fromkeys(r["channel"] for r in found if r.get("channel")))
    groups = list(dict.fromkeys((r.get("system") or "", r.get("group") or "") for r in found if r.get("group")))
    ors, args = [], []
    if chans:
        ors.append(f"t.channel IN ({','.join('?' * len(chans))})")
        args += chans
    for system, group in groups[:CAP["related_channels"]]:
        ors.append('(t.system = ? AND t."group" = ?)')
        args += [system, group]
    if not ors:
        return {"window_s": window_s, "channels": [], "items": [], "empty": "no channel to look on"}
    first = min(r["start"] for r in found)
    before = _rows(cad, ["t.start >= ? AND t.start < ?", " OR ".join(ors)], [lo, first, *args], CAP["before"])
    after = _fetch_asc(cad, ["t.start >= ? AND t.start <= ?", " OR ".join(ors)], [first, hi, *args],
                       CAP["after"] + len(evidence))
    rows = list(reversed(before)) + after
    items = []
    for r in rows:
        t = _tx(cad, r)
        t["evidence"] = r["id"] in evidence
        t["related"] = r.get("channel") not in chans
        items.append(t)
    related = list(dict.fromkeys(r["channel"] for r in rows if r.get("channel") not in chans))
    return {"window_s": window_s, "from": lo, "to": hi, "channels": chans, "related_channels": related,
            "capped": {"before": len(before) >= CAP["before"], "after": len(after) >= CAP["after"] + len(evidence)},
            "items": items,
            "empty": "" if items else f"nothing else heard within {window_s / 60:g} min"}


# -- the place and what is there ------------------------------------------------------------------------------------

def _site(cad, label: str, lat, lon) -> dict:
    """The address keys of a place (as an incident at it would have), for History here's lookups."""
    try:
        k = cad.addrs.key_of({"id": None, "address": label or "", "lat": lat, "lon": lon})
        return k.as_dict()
    except Exception:  # noqa: BLE001 - no key: the point alone
        return {"addr_key": "", "bldg_key": "", "addr_street": "", "addr_how": "", "addr_label": ""}


def _nearby(cad, key: dict, lat, lon, may: str) -> dict:
    out = {}
    pseudo = {"id": 0, "type": "", "bldg_key": key.get("bldg_key") or "", "lat": lat, "lon": lon}
    pp = getattr(cad, "preplan", None)
    if pp is None:
        out["preplan"] = _empty([], "no pre-plan points here")
    else:
        pts = pp.for_incident(pseudo, may)[:CAP["preplan"]]
        out["preplan"] = _empty([{k: p.get(k) for k in ("id", "kind", "label", "note", "lat", "lon", "addr_label", "nearby")}
                                 for p in pts], "no pre-plan points at this address")
    cams = getattr(cad, "cameras_near", None)
    if cams is None or lat is None:
        out["cameras"] = _empty([], "no camera lookup here" if cams is None else "no point")
    else:
        try:
            cs = cams(float(lat), float(lon))[:CAP["cameras"]]
        except Exception:  # noqa: BLE001 - the bundle never fails on a camera
            cs = []
        out["cameras"] = _empty([{k: c.get(k) for k in ("id", "name", "road", "dir", "lat", "lon", "km")} for c in cs],
                                "no camera nearby")
    out["hydrants"] = _empty([], "hydrants: on the map's GIS layer (no lookup by point)")
    return out


def _site_calls(cad, key: dict, lat, lon, airport: bool, place_name: str, at: float) -> dict:
    """The calls at the site strictly before the pre-alert (at), at most SITE_YEARS back, newest first."""
    since = at - SITE_YEARS * 365 * 86400
    rows: list[dict] = []
    basis = []
    if key.get("bldg_key") or lat is not None:
        try:
            rows = list(cad.addrs.building(key.get("bldg_key") or "", lat, lon, key.get("addr_street") or ""))
            basis.append("address" if key.get("bldg_key") else "point")
        except Exception:  # noqa: BLE001 - History here's own errors never stop the bundle
            rows = []
    if airport:
        seen = {r["id"] for r in rows}
        rows += [r for r in cad.store.q("SELECT id, number, created_at, closed_at, type, subtype, disposition, status, address, "
                                        "addr_label, lat, lon FROM incidents WHERE created_at >= ? AND created_at < ? AND type = "
                                        "'AIRPORT' AND number IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                                        (since, at, CAP["site_calls"]))
                 if r["id"] not in seen]
        basis.append("airport events")
    if place_name:
        seen = {r["id"] for r in rows}
        rows += [r for r in cad.store.q("SELECT id, number, created_at, closed_at, type, subtype, disposition, status, address, "
                                        "addr_label, lat, lon FROM incidents WHERE created_at >= ? AND created_at < ? AND place = ? "
                                        "COLLATE NOCASE AND number IS NOT NULL ORDER BY created_at DESC LIMIT ?",
                                        (since, at, place_name, CAP["site_calls"])) if r["id"] not in seen]
        basis.append("common place")
    rows = sorted((r for r in rows if since <= r["created_at"] < at), key=lambda r: (-r["created_at"], -r["id"]))
    total = len(rows)
    rows = rows[:CAP["site_calls"]]
    ids = [r["id"] for r in rows]
    extra: dict[int, dict] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in cad.store.q(f"SELECT id, remarks FROM incidents WHERE id IN ({marks})", ids):
            extra.setdefault(r["id"], {})["remarks"] = _clip(r["remarks"])
        units: dict[int, list[str]] = {}
        for a in cad.store.q(f"SELECT a.incident_id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                             f"WHERE a.incident_id IN ({marks}) ORDER BY a.id", ids):
            lst = units.setdefault(a["incident_id"], [])
            if a["name"] not in lst:
                lst.append(a["name"])
        comments: dict[int, list[dict]] = {}
        for e in cad.store.q(f"SELECT incident_id, at, kind, text, operator FROM events WHERE incident_id IN ({marks}) "
                             f"AND kind IN ('note', 'remarks', 'upgrade') ORDER BY at DESC", ids):
            lst = comments.setdefault(e["incident_id"], [])
            if len(lst) < CAP["comments"]:
                lst.append({"at": e["at"], "kind": e["kind"], "text": _clip(e["text"]), "by": e["operator"]})
    items = []
    for r in rows:
        items.append({**{k: r.get(k) for k in ("id", "number", "created_at", "closed_at", "type", "subtype", "disposition",
                                               "status", "address", "lat", "lon")},
                      "units": units.get(r["id"], []), "remarks": extra.get(r["id"], {}).get("remarks", ""),
                      "comments": comments.get(r["id"], [])})
    why = "no address or point to match on" if not basis else f"no calls here in {SITE_YEARS} years"
    return {"basis": basis, "total": total, "capped": total > len(items), **_empty(items, why)}


def _notes(cad, key: dict, lat, lon, may: str) -> dict:
    pm = getattr(cad, "premise", None)
    if pm is None:
        return _empty([], "no address notes here")
    if not key.get("bldg_key") and lat is None:
        return _empty([], "no address or point to match on")
    notes = pm.for_incident({"id": 0, "type": "", "bldg_key": key.get("bldg_key") or "", "lat": lat, "lon": lon}, may)
    return _empty([{k: n.get(k) for k in ("id", "label", "category", "priority", "text", "author_name", "updated_at",
                                          "review_at", "expires_at", "nearby")} for n in notes[:CAP["notes"]]],
                  "no address notes here")


def _now(cad, lat, lon, airport: bool, now: float) -> dict:
    incs = cad.store.q(f"SELECT id, number, type, subtype, status, address, lat, lon, created_at FROM incidents "
                       f"WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY created_at DESC LIMIT 200", OPEN)
    near = []
    for i in incs:
        d = None
        if lat is not None and i["lat"] is not None:
            d = addrkey.meters(float(lat), float(lon), i["lat"], i["lon"])
        if (d is not None and d <= NOW_NEAR_M) or (airport and i["type"] == "AIRPORT"):
            near.append({**i, "m": None if d is None else round(d)})
    near = sorted(near, key=lambda i: (i["m"] is None, i["m"] or 0, -i["created_at"]))[:CAP["now_calls"]]
    if near:
        ids = [i["id"] for i in near]
        units: dict[int, list[dict]] = {}
        for a in cad.store.q(f"SELECT a.incident_id, u.name, u.status, a.dispatched_at, a.enroute_at, a.onscene_at FROM "
                             f"assignments a JOIN units u ON u.id = a.unit_id WHERE a.cleared_at IS NULL AND a.incident_id "
                             f"IN ({','.join('?' * len(ids))}) ORDER BY a.id", ids):
            units.setdefault(a["incident_id"], []).append({k: a[k] for k in ("name", "status", "dispatched_at", "enroute_at",
                                                                             "onscene_at")})
        for i in near:
            i["units"] = units.get(i["id"], [])
    why = "no point to measure from" if lat is None and not airport else f"no open call within {NOW_NEAR_M / 1000:g} km"
    return {"as_of": now, "near_m": NOW_NEAR_M, "calls": _empty(near, why)}


# -- the two kinds ------------------------------------------------------------------------------------------------------

def _airport(cad, pid: int, admin: bool, may: str, now: float, windows: dict) -> tuple[dict, list]:
    try:
        a = cad.airport_alerts.get(int(pid))
    except CadError:
        raise NotFound(f"no airport pre-alert {pid}") from None
    d = a.get("data") or {}
    snips = (d.get("snippets") or [])[-CAP["trigger"]:]
    found = _by_ids(cad, [s["id"] for s in snips if s.get("id")])
    trigger = []
    for s in snips:
        r = found.get(s.get("id"))
        t = _tx(cad, r) if r else {"id": s.get("id"), "start": s.get("at"), "channel": s.get("channel") or "",
                                   "text": s.get("text") or "", "transcript_kept": False,
                                   "has_audio": bool(s.get("id")) and bool(cad.has_clip(s["id"]))}
        if r is None:
            t["kept"] = "the pre-alert's own copy (the radio log no longer has it)"
        t["context"] = bool(s.get("context"))
        t["matched"] = list(s.get("hits") or [])
        trigger.append(t)
    window_s = float(windows.get(a["source"], windows.get("atc", 300.0)))
    evidence = {s["id"] for s in snips if s.get("id")}
    loc = d.get("location") or {}
    placed = loc.get("lat") is not None and loc.get("lon") is not None
    lat, lon = (float(loc["lat"]), float(loc["lon"])) if placed else (None, None)
    # an ATC point is a guess from the words (hundreds of metres to a mile or more): never a building. Site history is
    # the airport's own events; no address key, notes or pre-plan points from the guess (intel's exact-place gate)
    key: dict = {}
    site_lat, site_lon = lat, lon
    pts = [x for x in d.get("aircraft") or [] if x.get("lat") is not None]
    if not placed and pts:                       # no point from the words: the named aircraft's fix, for "now" only
        site_lat, site_lon = pts[0]["lat"], pts[0]["lon"]
    matched = list(dict.fromkeys(h for s in snips for h in (s.get("hits") or [])))
    out = {
        "summary": {"title": a["title"], "detail": a["detail"], "source": a["source"], "source_name": a["source_name"],
                    "category": a["kind"], "categories": d.get("kinds") or [a["kind"]], "channel": a["channel"],
                    "at": a["at"], "updated_at": a["updated_at"], "level": a["level"], "confidence": a["confidence"],
                    "count": a["count"], "drill": bool(d.get("drill")), "alert_level": d.get("alert_level"),
                    "lines": d.get("lines") or []},
        "why": {"rules": [{"id": k} for k in d.get("kinds") or [a["kind"]]], "matched": matched,
                "score": {"confidence": a["confidence"], "level": a["level"], "pushed": d.get("pushed")},
                "phrases_hidden": False,
                "empty": "" if matched or d.get("lines") else "no words matched (an aircraft's own data)"},
        "trigger": _empty(trigger, "no transmissions: from the aircraft's own data"),
        "before_after": _window(cad, list(found.values()), window_s, evidence),
        "place": {"point": ({"lat": lat, "lon": lon, "radius_m": loc.get("radius_m"), "approx": True} if placed else None),
                  "label": loc.get("basis") or "", "basis": loc.get("basis") or "", "phrases": d.get("phrases") or [],
                  "alts": [], "key": key, "nearby": {},
                  "nearby_empty": "an approximate point: nothing looked up near it",
                  "empty": "" if placed else ("not placed: " + (loc.get("basis") or "no location words"))},
        "site_history": {
            "site": "HPN airport", "key": key,
            "calls": _site_calls(cad, key, None, None, True, "", a["at"]),
            "prealerts": _empty([{k: r.get(k) for k in ("id", "at", "title", "status", "source_name", "level")}
                                 for r in cad.airport_alerts._rows("a.at < ? AND a.at >= ? AND a.id != ?",
                                                                   (a["at"], a["at"] - SITE_YEARS * 365 * 86400, a["id"]),
                                                                   CAP["site_prealerts"])],
                                "no earlier airport pre-alerts kept (90 days)"),
            "notes": _empty([], "an approximate point: no address to match notes on"),
        },
        "now": {**_now(cad, site_lat, site_lon, True, now),
                "aircraft": {"items": d.get("aircraft") or [], "as_recorded": True,
                             "empty": "" if d.get("aircraft") else "no aircraft named",
                             "note": "As recorded with the pre-alert: only the aircraft it names, each at its fix time "
                                     "(pos_at); the traffic around it is not kept"}},
        "outcome": {"status": a["status"], "at": a["outcome_at"], "by": a["operator"], "note": a["note"],
                    "acknowledged": d.get("acknowledged"), "incident": _incident(cad, a["incident_id"]),
                    "near_incident": None, "feedback": [], "pushed": d.get("pushed") is not None},
    }
    rev = [a["updated_at"], a["status"], a["outcome_at"], a["incident_id"]]
    return out, rev


def _matched_phrases(rule, text: str) -> list[str]:
    from sizeup.intel import rules as rules_mod
    ws = rules_mod.words(text)
    out = []
    for t, w in rules_mod.readings(text, ws, rule.norm):
        for p in rule.any:
            if p.src not in out and p.found(t, w):
                out.append(p.src)
    return out


def _intel(cad, pid: int, admin: bool, may: str, now: float, windows: dict) -> tuple[dict, list]:
    rows = cad.intel.events.rows("id = ?", (int(pid),), 1) if hasattr(cad.intel.events, "rows") else []
    if not rows:
        raise NotFound(f"no radio pre-alert {pid}")
    e = cad.intel_admin.shown(rows[0])
    if not admin and e["mode"] not in ("map", "push"):          # shadow and log: rules being tuned, the admins' only
        raise NotFound(f"no radio pre-alert {pid}")
    raw = rows[0]
    fired = {str(r[0]) for r in e["rules"] or []}
    ids = list(raw["traffic_ids"])[-CAP["trigger"]:]
    found = _by_ids(cad, ids)
    hits: dict[str, list[dict]] = {}
    for h in (cad.store.q(f"SELECT traffic_id, rule_id, weight FROM intel_hits WHERE traffic_id IN "
                          f"({','.join('?' * len(ids))})", ids) if ids else []):
        hits.setdefault(h["traffic_id"], []).append({"id": h["rule_id"], "weight": h["weight"]})
    pack = cad.intel.pack(e["pack"])
    by_rule = {r.id: r for r in pack.rules} if pack else {}

    def shown_rule(rid: str) -> bool:
        """A hit a non-admin may see: a rule the event fired on, or one that maps or pushes now (never a shadow,
        log or below-threshold rule being tuned, nor an admin's own local.* one)."""
        if admin or rid in fired:
            return True
        r = by_rule.get(rid)
        if r is None or rid.startswith("local.") or not r.enabled:
            return False
        # the rung the engine gives it: the lowest of the pack's, its category's and its own mode
        return lowest(pack.mode, pack.category(r.category).get("mode", "push"), r.mode) in ("map", "push")
    trigger = []
    for tid in ids:
        r = found.get(tid)
        t = _tx(cad, r) if r else {"id": tid, "transcript_kept": False, "has_audio": bool(cad.has_clip(tid)), "text": "",
                                   "kept": "gone from the radio log (or another system's, not this reader's)"}
        t["rules"] = sorted((h for h in hits.get(tid, []) if shown_rule(h["id"])), key=lambda h: -h["weight"])
        if admin and r is not None:
            t["matched"] = list(dict.fromkeys(p for h in t["rules"] if h["id"] in by_rule
                                              for p in _matched_phrases(by_rule[h["id"]], r.get("text") or "")))
        trigger.append(t)
    if not found and ids:
        why = "its transmissions are gone from the radio log"
    else:
        why = "no transmissions"
    lat, lon = e["lat"], e["lon"]
    placed = lat is not None and lon is not None and e["precision"] != "none"
    # a site (SITE_PRECISIONS): else no address key, notes, pre-plan points or earlier calls
    confirmed = bool(e["confirmed"])
    exact = placed and e["precision"] in SITE_PRECISIONS and (
        confirmed or (float(e["where"] or 0) >= UNCONFIRMED_WHERE and not (e["alts"] or [])))
    unsure = exact and not confirmed
    key = _site(cad, e["place"], lat, lon) if exact else {}
    # the earlier ones at the same place, strictly before this one (intel_events has no index on first_at or the place:
    # a scan of its 90 days, bounded by the LIMIT after the filter)
    modes = "" if admin else " AND mode IN ('map', 'push')"
    prior = cad.store.q("SELECT id, first_at, category, label_full, status, level, lat, lon, mode FROM intel_events WHERE "
                        "first_at < ? AND first_at >= ? AND id != ?" + modes + " AND (label_full = ? COLLATE NOCASE OR "
                        "(lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?)) ORDER BY first_at DESC LIMIT ?",
                        (e["first_at"], e["first_at"] - SITE_YEARS * 365 * 86400, e["id"], e["place"] or "\x00",
                         *addrkey.bbox(lat, lon), CAP["site_prealerts"] * 3)) if exact else []
    prior = [p for p in prior if (e["place"] and (p["label_full"] or "").lower() == e["place"].lower())
             or (p["lat"] is not None and addrkey.meters(lat, lon, p["lat"], p["lon"]) <= addrkey.GROUP_M)]
    feedback = cad.store.q("SELECT verdict, operator, at, corrected_category FROM intel_feedback WHERE event_id = ? "
                           "ORDER BY at DESC, id DESC LIMIT ?", (e["id"], CAP["feedback"]))
    votes = [dict(f) if admin else {k: f[k] for k in ("verdict", "at", "corrected_category")} for f in feedback]   # who: admins
    now_hash = cad.intel.ruleset_hash() if hasattr(cad.intel, "ruleset_hash") else ""
    status_by = e.get("dismissed_by") or ""
    out = {
        "summary": {"title": " · ".join(x for x in (e["pack_label"], e["label"], e["town"]) if x), "detail": e["note"],
                    "source": "intel", "source_name": e["pack_label"], "category": e["category"], "categories": [e["category"]],
                    "category_label": e["label"], "pack": e["pack"], "at": e["first_at"], "updated_at": raw["updated_at"],
                    "last_at": e["last_at"], "until": e["until"], "level": e["level"], "what": e["what"],
                    "where": e["where"], "mode": e["mode"], "safety": e["safety"], "count": len(raw["traffic_ids"]),
                    "suggested": e["suggested"], "lines": []},
        "why": {"rules": [{"id": r[0], "rev": r[1] if len(r) > 1 else None, "weight": r[2] if len(r) > 2 else None}
                          for r in e["rules"] or []],
                "matched": list(dict.fromkeys(p for t in trigger for p in t.get("matched") or [])),
                "score": {"what": e["what"], "where": e["where"], "level": e["level"], "precision": e["precision"],
                          "confirmed": e["confirmed"]},
                "ruleset_hash": raw.get("ruleset_hash") or "", "phrases_hidden": not admin,
                # the phrases are found again with the rules as they are now: the event may have fired under others
                "matched_from": "current rules",
                "rules_changed": bool(raw.get("ruleset_hash")) and raw.get("ruleset_hash") != now_hash,
                "empty": "" if e["rules"] else "no rules recorded"},
        "trigger": _empty(trigger, why),
        "before_after": _window(cad, list(found.values()), float(windows.get("intel", 600.0)), set(ids)),
        "place": {"point": ({"lat": lat, "lon": lon, "radius_m": e["radius_m"], "approx": not e["confirmed"],
                             "precision": e["precision"]} if placed else None),
                  "label": e["place"], "town": e["town"], "basis": e["basis"], "phrases": [], "alts": e["alts"] or [],
                  "key": key, "nearby": _nearby(cad, key, lat, lon, may) if exact else {}, "site_unconfirmed": unsure,
                  "empty": "" if placed else "location not heard"},
        "site_history": {
            "site": e["place"] if exact else "", "key": key, "unconfirmed": unsure,
            "calls": (_site_calls(cad, key, lat, lon, False, e["place"] if e["precision"] == "landmark" else "", e["first_at"])
                      if exact else _empty([], f"place too coarse or unsure ({e['precision']}) for a site")),
            "prealerts": _empty([dict(p) for p in prior[:CAP["site_prealerts"]]],
                                "no earlier radio pre-alerts there (kept 90 days)" if exact else "no site"),
            "notes": _notes(cad, key, lat, lon, may) if exact else _empty([], "no site"),
        },
        "now": {**_now(cad, lat if placed else None, lon if placed else None, False, now),
                "aircraft": {"items": [], "empty": "not an airport pre-alert"}},
        "outcome": {"status": e["status"], "at": raw["updated_at"] if e["status"] != "open" else None, "by": status_by,
                    "note": e["note"], "acknowledged": None, "incident": _incident(cad, raw.get("incident_id")),
                    "near_incident": _incident(cad, e.get("near_incident")), "fd": e.get("fd_shown") or "",
                    "fd_state": e.get("fd") or "",
                    "feedback": votes, "pushed": e["pushed_at"] is not None,
                    "pushed_at": e["pushed_at"], "suppressed": e["suppressed"]},
    }
    rev = [raw["updated_at"], e["status"], len(feedback), feedback[0]["at"] if feedback else 0]
    return out, rev


def build(cad, kind: str, pid: int, *, admin: bool = False, may: str = "viewer", now: float | None = None,
          windows: dict | None = None) -> dict:
    """The bundle for one pre-alert (the module doc). admin: rule phrases and shadow / log events; may: the reader's
    standing for address notes and pre-plan points (premise.py / preplan.py visibility)."""
    if kind not in KINDS:
        raise NotFound(f"kind: one of {', '.join(KINDS)}")
    if kind == "intel" and not sourcekeys.may_see_context(cad.comms.context_broadcast):
        # fails closed for every caller (the push, inference): another system's traffic only for a reader who may see it
        raise NotFound(f"no radio pre-alert {pid}")
    now = cad.clock() if now is None else float(now)
    w = {**WINDOW_S, **(windows or {})}
    body, rev = (_airport if kind == "airport" else _intel)(cad, int(pid), admin, may, now, w)
    # the cursor: what is shown, hashed (the time-free parts: the "now" section's as_of is left out), so a poll can
    # tell a change without the whole bundle; a later PR's updates log appends against (kind, id)
    stable = {k: v for k, v in body.items() if k != "now"}
    stable["now"] = {k: v for k, v in body["now"].items() if k != "as_of"}
    digest = hashlib.sha256(json.dumps([rev, stable], sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"schema": SCHEMA_VERSION, "event": {"kind": kind, "id": int(pid)}, "url": page_path(kind, pid),
            "generated_at": now, "cursor": digest, **body}
