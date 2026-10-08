"""CAD JSON API under /api/cad/ (served by sizeup/web/server.py). Every POST takes a JSON body;
a bad one is a 400 {"error": reason}.

Who: on the hub behind Cloudflare Access the server hands each request the verified identity
(sizeup/cloud/access.py), and every POST's `operator` is that person ("Name <email>", Settings >
Users), whatever the page sent: post() is the one place every write goes through. Without Access,
`operator` is what the page sent (the Operator field), as before.

GET  /api/cad/state                       lists, units, events (open, recently closed), run cards
GET  /api/cad/stream                      SSE: the state, pushed every second (server.py)
GET  /api/cad/geocode?q=209 westchester   location suggestions, Port Chester / Rye Brook first, each with its town
GET  /api/cad/runcards                    run cards
GET  /api/cad/incident/<id>               one event with its units, chronology and dispatches
GET  /api/cad/history                     the Incident database: every numbered event, newest first
GET  /api/cad/admin                       record counts and the next event number
GET  /api/cad/whoami                      {email, name, callsign, source: "access" | "none"}: who signed in
GET  /api/cad/settings                    the Settings page: lists, units, dispatch, operators, system (sizeup/cad/settings.py)
GET  /api/cad/settings/export             every setting as one JSON file (edits, run cards, operators)
GET  /api/cad/settings/audit?section=     every Settings change (who, section, key, old -> new), newest first
POST /api/cad/settings/<section>          {action, ..., operator?}: event_types | event_subtypes | dispositions |
                                          received_via | unit_statuses | event_class | type_subtypes | geo_town_order | units |
                                          dispatch | users | runcards | import (dry_run / apply)
GET  /api/cad/export/<name>.csv           incidents | calls | unit-history | event-log, the sheet's columns
GET  /api/cad/dispatch/<id>.wav           a logged dispatch's audio
GET  /api/cad/audio/<key>.wav             a preview's audio
GET  /api/cad/tts/voices                  {engine, voice, speed, fallback_voice, voices: [{id, name, accent, gender}]}
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
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from sizeup.cad.service import Cad
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


def _json(code: int, obj) -> tuple[int, bytes, str]:
    return code, json.dumps(obj, separators=(",", ":")).encode(), JSON


class CadApi:
    def __init__(self, cad: Cad):
        self.cad = cad

    def get(self, path: str, query: str, who=None) -> tuple[int, bytes, str]:
        try:
            if path == "/api/cad/whoami":
                return _json(200, self.cad.settings.whoami(who.email if who is not None else ""))
            if path == "/api/cad/settings":
                return _json(200, self.cad.settings_get())
            if path == "/api/cad/settings/export":
                return _json(200, self.cad.settings_export())
            if path == "/api/cad/settings/audit":
                return _json(200, self.cad.settings_audit_log(parse_qs(query)))
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

    def post(self, path: str, raw: bytes, who=None) -> tuple[int, bytes, str]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError as e:
            return _json(400, {"error": f"bad JSON: {e}"})
        if not isinstance(body, dict):
            return _json(400, {"error": "the body must be a JSON object"})
        c = self.cad
        try:
            if who is not None and who.email:      # signed in: that's the operator, not what the page typed
                body["operator"] = c.settings.person(who.email)["operator"]
            if m := SETTINGS.fullmatch(path):
                return _json(200, c.settings_post(m.group(1), body))
            if path == "/api/cad/incident":
                return _json(200, c.create(body))
            if m := INCIDENT.fullmatch(path):
                iid, action = int(m.group(1)), m.group(2)
                fn = {None: c.update, "accept": c.accept, "dismiss": c.dismiss, "close": c.close, "note": c.note,
                      "recommend": c.recommend, "preview": c.preview, "dispatch": c.dispatch, "upgrade": c.upgrade,
                      "attach": c.attach, "link": c.link_traffic, "return": c.return_unit}[action]
                return _json(200, fn(iid, body))
            if m := UNIT.fullmatch(path):
                return _json(200, c.unit_status(int(m.group(1)), body))
            if m := UNIT_STATION.fullmatch(path):
                return _json(200, c.unit_station(int(m.group(1)), body))
            if path == "/api/cad/runcards":
                return _json(200, c.save_run_card(body))
            if path == "/api/cad/runcards/delete":
                return _json(200, c.delete_run_card(body))
            if path == "/api/cad/radios":
                return _json(200, c.save_radio(body))
            if path == "/api/cad/radios/delete":
                return _json(200, c.delete_radio(body))
        except CadError as e:
            return _json(400, {"error": str(e)})
        return 404, b"not found\n", "text/plain"
