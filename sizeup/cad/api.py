"""CAD JSON API under /api/cad/ (served by sizeup/web/server.py). Every POST takes a JSON body;
a bad one is a 400 {"error": reason}.

Who: on the hub behind Cloudflare Access the server hands each request the verified identity
(sizeup/cloud/access.py), and every POST's `operator` is that person ("Name <email>", Settings >
Users), whatever the page sent: post() is the one place every write goes through. Without Access,
`operator` is what the page sent (the Operator field), as before.

Roles (user, 2026-10-08: "now that i added julian and myself, myself as an admin i should only be
the one to make changes?"): with that verified person, every POST is checked against WRITES first
(permit(); server.py asks the same for its own POSTs): admins change anything, dispatchers run
calls, viewers (and deactivated users) only look; a 403 {"error"} otherwise, logged. Until there is
an active admin with a sign-in email anyone signed in may do anything (Settings.standing()). Without
Access nothing is checked: a typed or picked name is no proof of who is at the console.

Activity (user, 2026-10-08: "lets add some per user logging too"): with that verified person, each
write that goes through is a sentence in their activity (DOES, next to WRITES), each refusal too,
and their first request of each Access login a "signed in" (visit(); sizeup/cad/activity.py).
Without Access nothing is recorded there.

GET  /api/cad/state                       lists, units, events (open, recently closed), run cards
GET  /api/cad/stream                      SSE: the state, pushed every second (server.py)
GET  /api/cad/geocode?q=209 westchester   location suggestions, Port Chester / Rye Brook first, each with its town
GET  /api/cad/runcards                    run cards
GET  /api/cad/incident/<id>               one event with its units, chronology and dispatches
GET  /api/cad/history                     the Incident database: every numbered event, newest first
GET  /api/cad/admin                       record counts and the next event number
GET  /api/cad/whoami                      {email, name, callsign, source: "access" | "none", role, enforced}: who
                                          signed in; with Access also {active, may, bootstrap, admins: [names]}
GET  /api/cad/activity?email=&kind=&since=&limit=
                                          per-person activity on the hub (sizeup/cad/activity.py), newest first:
                                          {rows, seen: [{email, name, last_seen, last_ip, last_ua}], all, email};
                                          an admin sees anyone's (email: one person, none: everyone), anyone
                                          else only their own (403 for someone else's)
GET  /api/cad/export/activity.csv?email=&kind=&since=   the same as a spreadsheet: admins only
GET  /api/cad/settings                    the Settings page: lists, units, dispatch, operators, system (sizeup/cad/settings.py)
GET  /api/cad/settings/export             every setting as one JSON file (edits, run cards, operators)
GET  /api/cad/settings/audit?section=     every Settings change (who, section, key, old -> new), newest first
POST /api/cad/settings/<section>          {action, ..., operator?}: event_types | event_subtypes | dispositions |
                                          received_via | unit_statuses | event_class | type_subtypes | geo_town_order | units |
                                          dispatch | users | runcards | import (dry_run / apply) |
                                          dispatch {action: "voice_preview"} -> {text, audio_url, audio_note}:
                                          a sample said by the voice set now (the same render as a dispatch)
                                          channels {action: set | set_many | reset, key ("FG1", "tg:307") | keys,
                                          record?, transcribe?, engine?: local | aws | both} (Settings > Radios) |
                                          stt {action: set | reset, key: vocabulary | aws_monthly_limit_usd, value}
                                          | {action: push}: the vocabulary to Amazon Transcribe (sizeup/channels.py)
GET  /api/cad/stt                         Settings > Radios > Vocabulary: {vocabulary: {phrases, edited, suggestions,
                                          aws (as sent), skipped, sig, state}, limit, aws (where it runs, this month's cost)}
GET  /api/cad/export/<name>.csv           incidents | calls | unit-history | event-log, the sheet's columns
GET  /api/cad/dispatch/<id>.wav           a logged dispatch's audio
GET  /api/cad/audio/<key>.wav             a preview's audio
GET  /api/cad/tts/voices                  {engine, voice, speed, fallback_voice, voices: [{id, name, accent, gender,
                                          engines? (Polly's)}], polly_engine? (the one in use)}
POST /api/cad/incident                    Accept event: {type, subtype?, emergency?, received_via?, until_at?,
                                          address?, cross_street?, caller_name?, caller_address?, phone?,
                                          remarks?, lat?, lon?, geo_label?, operator?}
POST /api/cad/incident/<id>               edit: any of the fields above
POST /api/cad/incident/<id>/accept        {type?, subtype?, operator?}  a 60 Control page -> pending (numbered)
POST /api/cad/incident/<id>/dismiss       {operator?, reason?}   a 60 Control page nobody takes
POST /api/cad/incident/<id>/close         {disposition, operator?}  units cleared
POST /api/cad/incident/<id>/note          {text, units?: ["E63"], operator?}   units: the unit tags (who said it)
GET  /api/cad/incident/<id>/units?q=e6    unit autocomplete: on the event first, then fire units, then the rest
POST /api/cad/incident/<id>/recommend     {type?, subtype?}      -> {units, unfilled, already, card, note}
POST /api/cad/incident/<id>/preview       {unit_ids, full?, remarks?} -> {text, kind, audio_url, audio_note, remarks,
                                                                    tones, tone_warnings}
POST /api/cad/incident/<id>/dispatch      {unit_ids, operator, channel?, full?, remarks?} -> {dispatch_id, text, ...}
                                          remarks: said as "Reported: ..." (at most 500 characters)
POST /api/cad/incident/<id>/upgrade       {type?, subtype?, info?, remarks?, unit_ids?, full?, operator, channel?, preview?}
POST /api/cad/incident/<id>/attach        {unit_ids, operator?}  units on a non-emergency event, no dispatch
POST /api/cad/unit/<id>/status            {status, operator?, note?}
POST /api/cad/unit/<id>/station           {station, operator?}  home firehouse (data/firehouses.json name; '' unsets)
POST /api/cad/runcards                    {type (TYPE or TYPE/SUBTYPE), slots, spoken?, rename_from?}   add / edit one
POST /api/cad/runcards/delete             {type}
GET  /api/cad/radios                      the radio registry (radio ID -> rig), conflicts, units not fitting
                                          the ID pattern, the edit log
POST /api/cad/radios                      {system: mdc|p25, radio_id, unit?, label?, source?, notes?, operator?}  add / edit
POST /api/cad/radios/delete               {system, radio_id, operator?}
GET  /api/cad/comms?unit=&channel=&event=&pcfd=1&since=&until=&limit=&rev=
                                          every transmission (newest first) with its rig, radio ID, events
GET  /api/cad/feed?limit=&tg=307,333      the pinned 60 Control feed: newest transmissions on the incident
                                          talkgroup(s), pc = why it is talking to us (sizeup/cad/feed.py)
POST /api/cad/incident/<id>/link          {traffic_id, operator}  put a transmission on the event (why manual)
GET  /api/cad/rigs                        board units (PCFD's own flagged), for the talk alerts
POST /api/cad/prealert/<id>/dismiss       {operator?, reason?}  a phone pre-alert from Tapout that is nothing
                                          (sizeup/prealert.py; the state's "prealert"); dispatchers and admins
GET  /api/cad/export/prealerts.csv        every ring kept (90 days) with its outcome: matched / dismissed / expired
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from sizeup.cad.activity import CSV_MAX_ROWS, KINDS, MAX_ROWS
from sizeup.cad.service import Cad
from sizeup.cad.settings import LastAdmin
from sizeup.cad.store import CadError

JSON = "application/json"
MAX_BODY = 65536
INCIDENT = re.compile(r"/api/cad/incident/(\d{1,9})(?:/(accept|dismiss|close|note|recommend|preview|dispatch|upgrade|attach|link|return))?")
SUGGEST = re.compile(r"/api/cad/incident/(\d{1,9})/units")
EXPORT = re.compile(r"/api/cad/export/([a-z-]{1,20}\.csv)")
UNIT = re.compile(r"/api/cad/unit/(\d{1,9})/status")
UNIT_STATION = re.compile(r"/api/cad/unit/(\d{1,9})/station")
DISPATCH_WAV = re.compile(r"/api/cad/dispatch/(\d{1,9})\.wav")
PREVIEW_WAV = re.compile(r"/api/cad/audio/([0-9a-f]{20})\.wav")
SETTINGS = re.compile(r"/api/cad/settings/([a-z_]{1,30})")
PREALERT = re.compile(r"/api/cad/prealert/(\d{1,9})/dismiss")

# Who may POST what (enforced only with a verified sign-in, see above): "dispatch" = dispatchers and
# admins (running calls), "admin" = admins only (Settings in every form, and the radios), "read" =
# anyone signed in (a POST that changes nothing). First match wins; a path not here is "admin".
WRITES = (
    (re.compile(r"/api/cad/incident(?:/\d{1,9}(?:/(?:accept|dismiss|close|note|recommend|preview|dispatch|upgrade|attach"
                r"|link|return))?)?"), "dispatch"),                       # events, notes, dispatches, radio links
    (UNIT, "dispatch"),                                                     # unit status on the board
    (PREALERT, "dispatch"),                                                 # a phone pre-alert that is nothing
    (re.compile(r"/api/incident/[^/]{1,80}/close"), "dispatch"),            # the monitor's incident list (server.py)
    (UNIT_STATION, "admin"),                                                # a unit's home station: Settings > Units
    (re.compile(r"/api/cad/settings/(?:channels|stt)"), "admin"),           # Settings > Radios: record / transcribe /
                                                                            # engine per channel, the AWS vocabulary + limit
    (SETTINGS, "admin"),                                                    # lists, units, dispatch, users, import ...
    (re.compile(r"/api/cad/runcards(?:/delete)?"), "admin"),
    (re.compile(r"/api/cad/radios(?:/delete)?"), "admin"),                 # the radio-ID registry
    (re.compile(r"/api/(?:talkgroup|talkgroup-group|mode|retranscribe)"), "admin"),   # the station's radios (server.py)
)
READ_ACTIONS = {("/api/cad/settings/dispatch", "preview")}                  # the wording on a sample event
MAY = {"admin": ("read", "dispatch", "admin"), "dispatcher": ("read", "dispatch"), "viewer": ("read",)}
NO_CLIENT = ("", "")                     # (ip, browser) of a request: sizeup/web/server.py passes Cf-Connecting-Ip, User-Agent


# What a write that went through says in the person's activity (sizeup/cad/activity.py): route ->
# fn(match, body, result, cad) -> (summary, incident number) or None (it changed nothing: a preview, a
# recommendation). First match wins; a route not here is "Changed <route>". Never whole bodies: the
# names of what changed, not their values (a setting's value stays in the settings audit).

def _tail(path: str) -> str:
    return path.removeprefix("/api/cad/").removeprefix("/api/")


def _clip(v, n: int = 60) -> str:
    t = " ".join(str(v or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _number(c, iid) -> str:
    try:
        return c.store.incident(int(iid))["number"] or ""
    except (CadError, TypeError, ValueError):
        return ""


def _units(c, ids) -> str:
    by = {u["id"]: u["name"] for u in c.store.units()}
    return ", ".join(by.get(i, f"#{i}") for i in ids) if isinstance(ids, list) else ""


def _type(t: str, sub: str = "") -> str:
    return f"{t}/{sub}" if sub else t


def _created(m, b, o, c):
    n = o.get("number") or ""
    return f"Created {n} {_type(o.get('type', ''), o.get('subtype', ''))}".rstrip() + (f" at {o['address']}" if o.get("address") else ""), n


def _on_incident(m, b, o, c):
    iid, act = int(m.group(1)), m.group(2)
    n = _number(c, iid)
    ev = n or ("a 60 Control page" if act in ("dismiss", "accept") else f"event {iid}")
    if act is None:
        return f"Edited {ev}: {', '.join(k for k in b if k != 'operator') or 'nothing'}", n
    if act in ("recommend", "preview") or (act == "upgrade" and b.get("preview")):
        return None
    if act == "accept":
        return f"Accepted {ev}" + (f" as {_type(b.get('type', ''), b.get('subtype', ''))}" if b.get("type") else ""), n
    if act == "dismiss":
        return f"Dismissed {ev}" + (f": {_clip(b.get('reason'))}" if b.get("reason") else ""), n
    if act == "close":
        return f"Closed {ev}: {b.get('disposition', '')}", n
    if act == "note":
        tags = o.get("units") or []
        return f"Note on {ev}" + (f" ({', '.join(tags)})" if tags else "") + f": {_clip(b.get('text'))}", n
    if act == "dispatch":
        return f"Dispatched {ev}: {_units(c, b.get('unit_ids'))}" + (" (full response)" if b.get("full") else ""), n
    if act == "upgrade":
        what = [x for x in (_type(b.get("type", ""), b.get("subtype", "")) if b.get("type") else "",
                            _units(c, b.get("unit_ids")) if b.get("unit_ids") else "", "full response" if b.get("full") else "") if x]
        return f"Upgraded {ev}" + (f": {', '.join(what)}" if what else ""), n
    if act == "attach":
        return f"Attached {_units(c, b.get('unit_ids'))} to {ev}", n
    if act == "link":
        return f"Linked a transmission to {ev}", n
    if act == "return":
        return f"Returned {_units(c, [b.get('unit_id')])} to {ev} ({b.get('status', '')})", n
    return f"{act.capitalize()} {ev}", n


def _unit_status(m, b, o, c):
    n = o.get("incident_number") or (_number(c, b["incident_id"]) if b.get("incident_id") else "")
    return f"Set {o.get('name', '')} to {b.get('status', '')}" + (f" ({_clip(b['note'], 40)})" if b.get("note") else "") \
        + (f" on {n}" if n else ""), n


def _settings(m, b, o, c):
    sec, act = m.group(1), str(b.get("action") or "")
    if (sec == "import" and act != "apply") or (sec == "dispatch" and act in ("preview", "voice_preview")):
        return None
    if sec == "import":
        return "Changed settings: imported a settings file", ""
    if sec == "users":
        who = o.get("name") or b.get("name") or ""
        if act == "add":
            return f"Changed settings: users: added {who} ({b.get('role') or 'dispatcher'})", ""
        if act == "edit":
            return f"Changed settings: users/{who} {', '.join(k for k in ('name', 'role', 'active', 'email', 'callsign') if k in b)}", ""
        return f"Changed settings: users/{who} {act}d", ""
    key = next((str(b[k]) for k in ("code", "key", "type", "name") if isinstance(b.get(k), (str, int)) and b.get(k) != ""), "")
    return f"Changed settings: {sec}{f'/{_clip(key, 40)}' if key else ''} {act}".rstrip(), ""


def _channels(m, b, o, c):
    act = str(b.get("action") or "")
    what = ", ".join(f"{k} {'on' if b[k] else 'off'}" if isinstance(b[k], bool) else f"{k} {b[k]}"
                     for k in ("record", "transcribe", "engine") if b.get(k) is not None)
    if act == "reset":
        return f"Channel {_clip(b.get('key'), 30)}: back to the default", ""
    if act == "set_many":
        return f"Channels ({len(b.get('keys') or [])}): {what or 'no change'}", ""
    return f"Channel {_clip(b.get('key'), 30)}: {what or 'no change'}", ""


def _stt(m, b, o, c):
    act, key = str(b.get("action") or ""), str(b.get("key") or "")
    if act == "push":
        return "Pushed the speech-to-text vocabulary to Amazon Transcribe", ""
    if key == "vocabulary":
        return ("Changed the speech-to-text vocabulary" + (f" ({len(b['value'])} phrases)" if isinstance(b.get("value"), list)
                                                           else " (back to the default)")), ""
    return f"Changed the AWS monthly limit: {b.get('value') if act == 'set' else 'default'}", ""


def _prealert_at(o) -> str:
    from sizeup.prealert import _hms
    return _hms(o.get("at")) or "?"


def _toggles(b) -> str:
    return ", ".join(f"{k} {'on' if b[k] else 'off'}" for k in ("record", "main") if b.get(k) is not None)


DOES = (
    (re.compile(r"/api/cad/incident"), _created),
    (INCIDENT, _on_incident),
    (UNIT, _unit_status),
    (PREALERT, lambda m, b, o, c: (f"Dismissed the phone pre-alert of {_prealert_at(o)}"
                                   + (f": {_clip(b.get('reason'))}" if b.get("reason") else ""), "")),
    (UNIT_STATION, lambda m, b, o, c: (f"Changed settings: {o.get('name', '')} station {_clip(b.get('station'), 40) or 'unset'}", "")),
    (re.compile(r"/api/cad/settings/channels"), _channels),
    (re.compile(r"/api/cad/settings/stt"), _stt),
    (SETTINGS, _settings),
    (re.compile(r"/api/cad/runcards"), lambda m, b, o, c: (f"Changed settings: run card {_clip(b.get('type'), 50)} saved", "")),
    (re.compile(r"/api/cad/runcards/delete"), lambda m, b, o, c: (f"Changed settings: run card {_clip(b.get('type'), 50)} deleted", "")),
    (re.compile(r"/api/cad/radios"), lambda m, b, o, c: (f"Changed settings: radio {b.get('system', '')} {_clip(b.get('radio_id'), 12)}"
                                                         + (f" → {_clip(b.get('unit'), 12)}" if b.get("unit") else ""), "")),
    (re.compile(r"/api/cad/radios/delete"), lambda m, b, o, c: (f"Changed settings: radio {b.get('system', '')} "
                                                                f"{_clip(b.get('radio_id'), 12)} deleted", "")),
    (re.compile(r"/api/incident/([^/]{1,80})/close"), lambda m, b, o, c: (f"Closed monitor incident {m.group(1)}", "")),
    (re.compile(r"/api/talkgroup"), lambda m, b, o, c: (f"Talkgroup {b.get('tg')}: {_toggles(b) or 'no change'}", "")),
    (re.compile(r"/api/talkgroup-group"), lambda m, b, o, c: (f"Talkgroups {_clip(b.get('group'), 40)}: {_toggles(b) or 'no change'}", "")),
    (re.compile(r"/api/mode"), lambda m, b, o, c: (f"Radio mode: {_clip(b.get('mode'), 30)}" + (f" ({_clip(b['sdr'], 30)})" if b.get("sdr") else ""), "")),
    (re.compile(r"/api/retranscribe"), lambda m, b, o, c: ("Retranscribe: every recording" if b.get("all")
                                                           else f"Retranscribe: {len(b.get('ids') or [])} recordings", "")),
)


def summarize(path: str, body: dict, out, c) -> tuple[str, str] | None:
    """(summary, incident number) for a write that went through, None: nothing to record."""
    o = out if isinstance(out, dict) else {}
    try:
        for rx, fn in DOES:
            if m := rx.fullmatch(path):
                return fn(m, body, o, c)
    except Exception as e:      # a summary never breaks the write it describes
        c.log(f"CAD activity: no summary for {path} ({type(e).__name__}: {e})")
    return f"Changed {_tail(path)}", ""


def needs(path: str, body: dict | None = None) -> str:
    """"read" | "dispatch" | "admin": what a POST to path needs."""
    if body is not None and (path, body.get("action")) in READ_ACTIONS:
        return "read"
    return next((need for rx, need in WRITES if rx.fullmatch(path)), "admin")


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON


class CadApi:
    def __init__(self, cad: Cad):
        self.cad = cad

    def permit(self, path: str, who, body: dict | None = None, st: dict | None = None,
               client: tuple[str, str] = NO_CLIENT) -> tuple[int, bytes, str] | None:
        """None when this POST may go ahead; else the 403 (in who's activity, so logged: who, what).
        Nothing is checked without a verified person (who: sizeup/cloud/access.py Identity)."""
        if who is None or not who.email:
            return None
        try:
            st = st or self.cad.settings.standing(who.email)
        except CadError as e:
            self.refused(path, who, f"signed in as {who.email}: {e}", client)
            return _json(403, {"error": f"signed in as {who.email}: {e}"})
        need = needs(path, body)
        if need in MAY.get(st["may"], ("read",)):
            return None
        ask = f" (ask {', '.join(st['admins'])})" if st["admins"] else ""
        if not st["active"]:
            why = f"Account deactivated{ask}"
        elif need == "admin":
            why = f"Admins only (you: {st['role']})"
        else:
            why = f"Dispatchers and admins only (you: {st['role']})"
        self.refused(path, who, f"needs {need}; {st['role']}{'' if st['active'] else ', deactivated'}", client)
        return _json(403, {"error": why})

    # -- activity (sizeup/cad/activity.py): only for a verified person -----------------------------------

    def visit(self, who, client: tuple[str, str] = NO_CLIENT, route: str = "") -> None:
        """Every request from someone signed in (server.py): their sign-in, when they were last seen."""
        if who is None or not who.email:
            return
        try:
            self.cad.activity.visit(who.email, who.session, *client, route)
        except Exception as e:           # never in the way of the request
            self.cad.log(f"CAD activity: {who.email}: {type(e).__name__}: {e}")

    def did(self, path: str, who, body: dict | None = None, out=None, client: tuple[str, str] = NO_CLIENT) -> None:
        """A write that went through (here or server.py's own POSTs), in who's activity."""
        if who is None or not who.email:
            return
        said = summarize(path, body or {}, out, self.cad)
        if said is not None:
            self.cad.activity.record(who.email, "action", f"POST {path}", said[0], said[1], True, *client)

    def refused(self, path: str, who, why: str, client: tuple[str, str] = NO_CLIENT) -> None:
        m = INCIDENT.fullmatch(path)
        self.cad.activity.record(who.email, "refused", f"POST {path}", f"Refused: {_tail(path)} ({why})",
                                 _number(self.cad, m.group(1)) if m else "", False, *client)

    def _activity(self, q: dict, who, csv: bool = False) -> tuple[int, bytes, str]:
        """GET /api/cad/activity, /api/cad/export/activity.csv: an admin anyone's, anyone else their own."""
        def one(k: str) -> str:
            return (q.get(k) or [""])[0].strip()
        email, kind = one("email").lower()[:254], one("kind")
        if kind and kind not in KINDS:
            return _json(400, {"error": f"kind: one of {', '.join(KINDS)}"})
        try:
            since = float(one("since")) if one("since") else None
            limit = CSV_MAX_ROWS if csv else min(int(one("limit")) if one("limit") else 200, MAX_ROWS)
        except ValueError:
            return _json(400, {"error": "since: unix seconds, limit: a number"})
        everyone = who is None or not who.email or self.cad.settings.standing(who.email)["may"] == "admin"
        if not everyone:
            if csv:
                return _json(403, {"error": "Admins only"})
            if email and email != who.email:
                return _json(403, {"error": "Admins only"})
            email = who.email
        act = self.cad.activity
        rows = act.rows(email, kind, since, limit)
        if csv:
            return 200, act.csv(rows), "text/csv; charset=utf-8"
        return _json(200, {"rows": rows, "seen": act.seen("" if everyone else who.email), "all": everyone, "email": email})

    def get(self, path: str, query: str, who=None) -> tuple[int, bytes, str]:
        try:
            if path == "/api/cad/whoami":
                return _json(200, self.cad.settings.whoami(who.email if who is not None else ""))
            if path in ("/api/cad/activity", "/api/cad/export/activity.csv"):
                return self._activity(parse_qs(query), who, csv=path.endswith(".csv"))
            if path == "/api/cad/settings":
                return _json(200, self.cad.settings_get())
            if path == "/api/cad/settings/export":
                return _json(200, self.cad.settings_export())
            if path == "/api/cad/settings/audit":
                return _json(200, self.cad.settings_audit_log(parse_qs(query)))
            if path == "/api/cad/stt":
                return _json(200, self.cad.settings.stt_page())
            if path == "/api/cad/state":
                return _json(200, self.cad.state())
            if path == "/api/cad/runcards":
                return _json(200, {"run_cards": self.cad.run_cards()})
            if path == "/api/cad/history":
                return _json(200, self.cad.history())
            if path == "/api/cad/admin":
                return _json(200, self.cad.admin())
            if path == "/api/cad/radios":
                return _json(200, self.cad.radio_registry())
            if path == "/api/cad/comms":
                return _json(200, self.cad.comms_query(parse_qs(query)))
            if path == "/api/cad/feed":
                return _json(200, self.cad.feed_query(parse_qs(query)))
            if path == "/api/cad/rigs":
                return _json(200, self.cad.rigs())
            if m := SUGGEST.fullmatch(path):
                return _json(200, {"units": self.cad.suggest_units(int(m.group(1)), (parse_qs(query).get("q") or [""])[0][:20])})
            if m := INCIDENT.fullmatch(path):
                if m.group(2) is None:
                    return _json(200, self.cad.full(int(m.group(1))))
            if m := EXPORT.fullmatch(path):
                data = self.cad.export(m.group(1))
                return (200, data, "text/csv; charset=utf-8") if data is not None else (404, b"no such export\n", "text/plain")
            if path == "/api/cad/tts/voices":
                return _json(200, self.cad.tts_voices())
            if path == "/api/cad/geocode":
                return _json(200, self.cad.geocode((parse_qs(query).get("q") or [""])[0]))
            if m := DISPATCH_WAV.fullmatch(path):
                wav = self.cad.dispatch_audio(int(m.group(1)))
                return (200, wav, "audio/wav") if wav else (404, b"no audio for that dispatch\n", "text/plain")
            if m := PREVIEW_WAV.fullmatch(path):
                wav = self.cad.preview_audio(m.group(1))
                return (200, wav, "audio/wav") if wav else (404, b"no such preview\n", "text/plain")
        except CadError as e:
            return _json(400, {"error": str(e)})
        return 404, b"not found\n", "text/plain"

    def post(self, path: str, raw: bytes, who=None, client: tuple[str, str] = NO_CLIENT) -> tuple[int, bytes, str]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError as e:
            return _json(400, {"error": f"bad JSON: {e}"})
        if not isinstance(body, dict):
            return _json(400, {"error": "the body must be a JSON object"})
        c = self.cad
        signed = who is not None and bool(who.email)
        try:
            if signed:                             # signed in: that's the operator, not what the page typed
                st = c.settings.standing(who.email)
                if refused := self.permit(path, who, body, st, client):
                    return refused
                body["operator"] = st["operator"]
            out = self._write(path, body, signed)
        except LastAdmin as e:
            if signed:
                self.refused(path, who, "the last admin", client)
            return _json(409, {"error": str(e)})
        except CadError as e:
            return _json(400, {"error": str(e)})
        if out is None:
            return 404, b"not found\n", "text/plain"
        self.did(path, who, body, out, client)
        return _json(200, out)

    def _write(self, path: str, body: dict, guard: bool):
        """The write itself: its answer, None for no such route. guard: roles enforced: keep an admin."""
        c = self.cad
        if m := SETTINGS.fullmatch(path):
            return c.settings_post(m.group(1), body, guard=guard)
        if path == "/api/cad/incident":
            return c.create(body)
        if m := INCIDENT.fullmatch(path):
            iid, action = int(m.group(1)), m.group(2)
            fn = {None: c.update, "accept": c.accept, "dismiss": c.dismiss, "close": c.close, "note": c.note,
                  "recommend": c.recommend, "preview": c.preview, "dispatch": c.dispatch, "upgrade": c.upgrade,
                  "attach": c.attach, "link": c.link_traffic, "return": c.return_unit}[action]
            return fn(iid, body)
        if m := UNIT.fullmatch(path):
            return c.unit_status(int(m.group(1)), body)
        if m := PREALERT.fullmatch(path):
            return c.prealert_dismiss(int(m.group(1)), body)
        if m := UNIT_STATION.fullmatch(path):
            return c.unit_station(int(m.group(1)), body)
        if path == "/api/cad/runcards":
            return c.save_run_card(body)
        if path == "/api/cad/runcards/delete":
            return c.delete_run_card(body)
        if path == "/api/cad/radios":
            return c.save_radio(body)
        if path == "/api/cad/radios/delete":
            return c.delete_radio(body)
        return None
