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
GET  /api/cad/stream                      SSE: the state, then each second only what changed (server.py Broadcaster)
GET  /api/cad/geocode?q=209 westchester   location suggestions, Port Chester / Rye Brook first, each with its town
                                          (with the place search's mile markers, exits and named places, #67)
                                          each with place, street_address, cross (the streets at each end of its block)
                                          and block (for the map), #204; two streets typed ("N Main St / Westchester
                                          Ave", "n main & west", #213): where they meet in our towns, at most 25 (total,
                                          more), cross = both streets; a street under two letters: error
GET  /api/cad/geocode/here?lat=&lon=      the new call's map (#204): the address point, intersection and named place
                                          nearest a click (each a suggestion as above), and the street under it; read only
GET  /api/cad/places?q=287 eb mm 10&near=lat,lon&iid=&limit=
                                          place search (#67: a note's "@"): {places: [{kind (address | intersection |
                                          street | place | milemarker | exit), label, sub (town), lat, lon, source,
                                          category?, detail?, route?, dir?, value?, score}]}, our towns first, then
                                          nearest to near (else the event iid's point); read only: viewers too
GET  /api/cad/runcards                    run cards
GET  /api/cad/airport                     the Airport map layer (#87): data/khpn_airport.geojson, with "airport" (where the
                                          field is) and "sources"; {} when the file isn't there
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
                                          places {action: set, name, aliases?, place: {label, lat, lon, sub?}, note?} |
                                          {action: delete, key}: the department's own place names (#67) |
                                          dispatch | users | runcards | import (dry_run / apply) |
                                          dispatch {action: "voice_preview"} -> {text, audio_url, audio_note}:
                                          a sample said by the voice set now (the same render as a dispatch)
                                          channels {action: set | set_many | reset, key ("FG1", "tg:307") | keys,
                                          record?, transcribe?, engine?: local | aws | both} (Settings > Radios) |
                                          stt {action: set | reset, key: vocabulary | aws_monthly_limit_usd, value}
                                          | {action: push}: the vocabulary to Amazon Transcribe (sizeup/channels.py)
                                          tones (the tone book, #80/#115/#122): {action: set, a_hz, b_hz, places, service,
                                          role?, confirmed?, ours?, page?, list?} a pair named or edited (page: read again) |
                                          {action: move, key, places, service} a pair under another entity (or a new one) |
                                          {action: entity, key, ours?, notes?, aliases?} | {action: merge, key, into:
                                          {places, service}} | {action: delete | reset, key} | {action: reread, page}: the
                                          page's recording through the tone decoder again
                                          camera_wall {action: set, key: margin_m, value} | {action: camera, id, mode: auto |
                                          include | exclude} | {action: reset}: the camera wall's area (#125, Settings > System)
GET  /api/cad/tones                       {named: [pairs named here], choices: [{key, name, places, service, aliases,
                                          ours}] (the tone book's entities, the county's agencies), places (one canonical
                                          list), services, names, ours: the ours entities' names}
GET  /api/cad/stt                         Settings > Radios > Vocabulary: {vocabulary: {phrases, edited, suggestions,
                                          aws (as sent), skipped, sig, state}, limit, aws (where it runs, this month's cost)}
GET  /api/cad/export/<name>.csv           incidents | calls | unit-history | event-log, the sheet's columns
GET  /api/cad/dispatch/<id>.wav           a logged dispatch's audio
GET  /api/cad/audio/<key>.wav             a preview's audio
GET  /api/cad/tts/voices                  {engine, voice, speed, fallback_voice, voices: [{id, name, accent, gender,
                                          engines? (Polly's)}], polly_engine? (the one in use)}
POST /api/cad/incident                    Accept event: {type, subtype?, emergency?, received_via?, until_at?,
                                          address?, cross_street?, caller_name?, caller_address?, phone?,
                                          remarks?, lat?, lon?, geo_label?, operator?; an Airport event (#105):
                                          acft_type?, acft_nature?, acft_eta? (minutes), acft_runway? (16|34|11|29),
                                          acft_souls?, acft_level? (1-5, from the souls unless given), acft_fuel?, acft_cargo?}
POST /api/cad/incident/<id>               edit: any of the fields above, and map_layers?: {layer: true|false} | null, the
                                          map's layer set for this event (#111; "cameras", "gis:hydrants" ...; null: the default)
POST /api/cad/incident/<id>/accept        {type?, subtype?, operator?}  a 60 Control page -> pending (numbered)
POST /api/cad/incident/<id>/dismiss       {operator?, reason?}   a 60 Control page nobody takes
POST /api/cad/incident/<id>/close         {disposition, operator?}  units cleared
POST /api/cad/incident/<id>/reopen        {operator?}  a closed or dismissed event back on the boards, its number kept
                                          (#89): closed -> active if a unit was ever on it, else pending (a
                                          non-emergency one: open again), the disposition cleared into the chronology;
                                          dismissed -> queued (60 Control, to accept or dismiss); no unit comes back
                                          by itself. 400 for an open event; dispatchers and admins
POST /api/cad/incident/<id>/note          {text, units?: ["E63"], places?: [{label, lat, lon, kind?, sub?, source?}],
                                          operator?}   units: the unit tags (who said it); places: its "@" places, each
                                          kept while "@label" is in the text (never moves the event)
GET  /api/cad/incident/<id>/units?q=e6    unit autocomplete: on the event first, then fire units, then the rest
POST /api/cad/incident/<id>/recommend     {type?, subtype?}      -> {units, unfilled, already, card, note}
POST /api/cad/incident/<id>/feed-use      {field: type|location} -> the event: the CAD feed's value over a person's (#297)
POST /api/cad/incident/<id>/auto-undo     {step, operator?}  an automatic step (60 Control dispatching, #311; the event's
                                          auto.steps) taken back: the unit (or the close) is then a person's
POST /api/cad/incident/<id>/keep          {traffic_id, undo?, operator?}  an automatic radio-log link (mention / fireground,
                                          #311) confirmed: its audio and row are kept like any on the event
POST /api/cad/dispatch-mode               {mode: 60control | pcfd, operator?}  the header's switch (#311,
                                          sizeup/cad/automode.py); dispatchers and admins; in the Settings audit log
POST /api/cad/incident/<id>/preview       {unit_ids, full?, remarks?} -> {text, kind, audio_url, audio_note, remarks,
                                                                    tones, tone_warnings}
POST /api/cad/incident/<id>/dispatch      {unit_ids, operator, channel?, full?, remarks?} -> {dispatch_id, text, ...}
                                          remarks: said as "Reported: ..." (at most 500 characters)
POST /api/cad/incident/<id>/upgrade       {type?, subtype?, info?, remarks?, unit_ids?, full?, operator, channel?, preview?}
POST /api/cad/incident/<id>/attach        {unit_ids, operator?}  units on a non-emergency event, no dispatch;
                                          another agency's unit (any event): on it Dispatched (its set's first
                                          status: a keyholder Notified), then its statuses (#219)
                                          {unit_ids, status: RS | OL | ET | IC, operator} -> {ok, moved, status}: on
                                          any open event in that status, not dispatched (no Disp time, no dispatch
                                          logged, nothing said); 400 for a unit already on it, out of service, or
                                          another agency's in a status not in its set; one on another call is moved
POST /api/cad/unit/<id>/status            {status, operator?, note?}  IC (incident command): one unit per event,
                                          whoever held it there goes back to OL (command_from: [names])
POST /api/cad/unit/<id>/station           {station, operator?}  home firehouse (data/firehouses.json name; '' unsets)
POST /api/cad/runcards                    {type (TYPE or TYPE/SUBTYPE), slots, spoken?, remarks?, rename_from?}   add / edit one
POST /api/cad/runcards/delete             {type}
GET  /api/cad/radios                      the radio registry (radio ID -> rig), conflicts, units not fitting
                                          the ID pattern, the edit log
POST /api/cad/radios                      {system: mdc|p25, radio_id, unit?, label?, source?, notes?, operator?}  add / edit
POST /api/cad/radios/delete               {system, radio_id, operator?}
POST /api/cad/radio-id                    {system: mdc | fleetsync, radio_id, unit? | label? | ignore?, operator?}  who a
                                          new FG1 radio is (#163, the state's radio_ids; sizeup/cad/newids.py): unit
                                          names it (its transmissions go on that unit's events), label just says who,
                                          ignore stops asking; dispatchers and admins, only for an ID nobody has set
GET  /api/cad/comms?unit=&channel=&event=&pcfd=1&emergency=1&since=&until=&main_limit=&limit=&rev=
                                          every transmission (newest first) with its rig, radio ID, events, the
                                          emergency button and its alarm (emergency=1: only those, #128); Main
                                          and Other each their own newest main_limit (200) / limit (300), #241
GET  /api/cad/feed?limit=&tg=307,333      the pinned 60 Control feed: newest transmissions on the incident
                                          talkgroup(s), pc = why it is talking to us (sizeup/cad/feed.py)
POST /api/cad/incident/<id>/link          {traffic_id, operator}  put a transmission on the event (why manual)
POST /api/cad/incident/<id>/unlink        {traffic_id, operator}  take it off: an auto link (unit, channel,
                                          dispatcher) stays off (sizeup/cad/comms.py radio_link_exclusions)
GET  /api/cad/rigs                        board units (PCFD's own flagged), for the talk alerts
POST /api/cad/prealert/<id>/dismiss       {operator?, reason?}  a phone pre-alert from Tapout that is nothing
                                          (sizeup/prealert.py; the state's "prealert"); dispatchers and admins
GET  /api/cad/export/prealerts.csv        every ring kept (90 days) with its outcome: matched / dismissed / expired
POST /api/cad/airport-alert/<id>/accept   {operator?}  an airport pre-alert (HPN ATC, #233; the state's airport_alerts;
                                          sizeup/cad/airport_alerts.py) made into a PENDING event: never a dispatch
POST /api/cad/airport-alert/<id>/dismiss  {operator?, reason?}  ... that is nothing; dispatchers and admins
GET  /api/cad/export/airport-alerts.csv   every airport pre-alert kept (90 days), ATC and ADS-B, with its outcome
POST /api/cad/hint/<id>/accept            {operator?}  a unit status suggested from the radio (#37, the state's
                                          status_hints; sizeup/cad/statushints.py): set, with "Status from radio
                                          (heard 14:32:10 “...”)" in the chronology; dispatchers and admins
POST /api/cad/hint/<id>/dismiss           {operator?}  not so: that transmission never suggests it again
GET  /api/cad/export/status-hints.csv     every suggestion and how it ended (accepted / dismissed / expired ...)
GET  /api/cad/alarms                      the emergency alarms (sizeup/cad/alarms.py, #128): {open, notices, recent, drill,
                                          settings: {keywords, channels, other, drill, defaults, edited, push}, history}
POST /api/cad/alarm/<id>/ack              {operator?}  an emergency alarm (or notice) seen: who and when, in the linked
                                          event's chronology too; dispatchers and admins (viewers see it, can't)
POST /api/cad/alarm/<id>/clear            {operator?}  all clear (resolved, not just seen): its emergency window ends
                                          (the talkgroup and radio no longer kept with Record off); acknowledges it
                                          too; in the chronology; dispatchers and admins
POST /api/cad/settings/alarms             {action: set | reset, key: keywords | channels | other | drill, value}: admins
GET  /api/cad/datasources[?rev=]          Settings > Data sources (#186, sizeup/datasources.py): every data pull with
                                          {key, name, group, source, last_fetched, next_due, ok, error, items, bytes,
                                          schedule (managed | builtin | none), every, running, ...}, rev; ?rev= the same:
                                          {rev, same: true}. Admins and dispatchers (a viewer: 403). Polled by the panel
                                          while it is open: never in the state push
POST /api/cad/cameras/<id>/dvr/pin        {on: bool}  keep recording a camera (#277, sizeup/dvr.py; dispatchers, admins)
POST /api/cad/cameras/<id>/dvr/clip       {from, to (unix s), incident_id, note?}  save a recorded range (at most 60 min,
                                          within what the DVR has) as a camera clip attached to the event (#323,
                                          sizeup/attachments.py): {ok, attachment} at once ("saving"), uploaded in the
                                          background; dispatchers and admins; audited. No bucket: 400 "not configured"
GET  /api/cad/incident/<id>/attachments   {configured, attachments: [{id, kind (camera | radio), source, source_name,
                                          start, end, bytes, note, status, created_by, created_at, objects: [{name,
                                          bytes, sha256}], play?, download?}]} in time order (also in GET
                                          /api/cad/incident/<id> as "attachments"); ?deleted=1: the deleted ones too
GET  /api/cad/incident/<id>/attachments/audit   every attach / view / download / export / delete of its attachments
GET  /api/cad/incident/<id>/export.zip    every attachment's objects, with manifest.json (sizes, sha256, verified); audited
GET  /api/cad/attachments/<id>            one attachment; .../index.m3u8 (a camera clip as a VOD playlist, a view: audited),
                                          .../<n>.ts (its segments, from S3), .../audio.wav (a radio one; audited),
                                          .../download (one .ts or .wav; audited). Anyone signed in; never public
POST /api/cad/attachments/<id>/delete     {}  admins only: the objects deleted, the row and its audit kept
POST /api/cad/datasources/<key>/refresh   {operator?}  Refresh now: runs in the background, the answer at once
                                          ({ok, source: the row, running}); 429 {error} when refused (refreshing already,
                                          too soon after the last, or the source is resting); admins; in the person's
                                          activity and the Settings audit log
GET  /api/cad/notify                      Settings > System > Push notifications (sizeup/notify.py Pushes): each kind's
                                          topic in effect and where it came from (Settings / SSM / config / off), the
                                          last push and test; admins only (the names are secret; ADMIN_READS)
GET  /api/cad/health                      the hub only, served by server.py: Settings > Health (sizeup/cloud/health.py);
                                          admins only (ADMIN_READS, #203: nobody else sees station health)
POST /api/cad/settings/notify             {action: set {key: alerts | status, topic ("" = the default)} | off {key} |
                                          reset {key} | server {value} | test {key}}: admins; audited without the names
POST /api/cad/settings/datasources        {action: set, key, value: off | hourly | daily | weekly | monthly} | {action:
                                          reset, key?}: how often a data source is refreshed; admins
POST /api/cad/settings/atc                {action: set {key: identify | map | vocab, value: true | false} | reset {key}}: who is
                                          talking on HPN Tower (#252, sizeup/atc_talkers.py) on the rows / the map; admins
POST /api/cad/settings/hub_errors         {action: set {key: on | per_signature | total, value} | reset {key}}: the hub's own
                                          error spikes to the status topic (#285, sizeup/errwatch.py); admins
POST /api/cad/settings/department_units  {action: set {key: a feed's department entry code, value: the department,
                                          accepts?: true} | reset {key}}: a whole department paged, never a unit
                                          (#370; accepts: it accepts a call alone in 60 Control mode); admins
POST /api/cad/settings/map_display        {action: set {key: detail_zoom | active_zoom | board_detail_zoom |
                                          board_active_zoom, value: a zoom} | reset {key}}: below it a rig is drawn small on
                                          the maps (#376, sizeup/cad/mapview.py); admins
POST /api/cad/settings/alert_mutes        {action: mute {key: a health alert kind, hours?} | unmute {key}}: shown on
                                          Settings > Health, not pushed (#340, sizeup/cloud/health.py); admins
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import parse_qs, unquote

from sizeup.cad import airport
from sizeup.cad.activity import CSV_MAX_ROWS, KINDS, MAX_ROWS
from sizeup.cad.service import Cad
from sizeup.cad.settings import LastAdmin
from sizeup.cad.store import TZ, CadError
from sizeup.datasources import TooSoon

JSON = "application/json"
MAX_BODY = 65536
INCIDENT = re.compile(r"/api/cad/incident/(\d{1,9})(?:/(accept|dismiss|close|reopen|note|recommend|preview|dispatch|upgrade|attach|link|unlink|keep|return|feed-use|auto-undo))?")
SUGGEST = re.compile(r"/api/cad/incident/(\d{1,9})/units")
EXPORT = re.compile(r"/api/cad/export/([a-z-]{1,20}\.csv)")
UNIT = re.compile(r"/api/cad/unit/(\d{1,9})/status")
UNIT_STATION = re.compile(r"/api/cad/unit/(\d{1,9})/station")
DISPATCH_WAV = re.compile(r"/api/cad/dispatch/(\d{1,9})\.wav")
PREVIEW_WAV = re.compile(r"/api/cad/audio/([0-9a-f]{20})\.wav")
SETTINGS = re.compile(r"/api/cad/settings/([a-z_]{1,30})")
PREALERT = re.compile(r"/api/cad/prealert/(\d{1,9})/dismiss")
AIRPORT_ALERT = re.compile(r"/api/cad/airport-alert/(\d{1,9})/(accept|dismiss)")
HINT = re.compile(r"/api/cad/hint/(\d{1,9})/(accept|dismiss)")
ALARM_ACK = re.compile(r"/api/cad/alarm/(\d{1,9})/ack")
ALARM_CLEAR = re.compile(r"/api/cad/alarm/(\d{1,9})/clear")
DISPATCH_MODE = re.compile(r"/api/cad/dispatch-mode")
DATASOURCE = re.compile(r"/api/cad/datasources/([A-Za-z0-9_.:%-]{1,200})/refresh")
DVR_PIN = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/dvr/pin")      # keep recording a camera (#277)
DVR_CLIP = re.compile(r"/api/cad/cameras/([A-Za-z0-9_.:-]{1,64})/dvr/clip")    # a camera clip on an event (#323)
ATTACHMENT = re.compile(r"/api/cad/attachments/([0-9a-f]{16})(?:/(index\.m3u8|(\d{1,6})\.ts|audio\.wav|download|delete))?")
INCIDENT_ATTACHMENTS = re.compile(r"/api/cad/incident/(\d{1,9})/(attachments|attachments/audit|export\.zip)")
DS_READERS = ("admin", "dispatcher")      # who may see Settings > Data sources (#186)
# The GETs only an admin may read (with a verified sign-in, like WRITES): path -> why, in the 403. The rest of the
# GETs are anyone signed in's (Data sources: DS_READERS). server.py asks the same for the ones it serves itself.
ADMIN_READS = {
    "/api/cad/notify": "the push topics' names are secret",                  # Settings > System > Push notifications
    "/api/cad/health": "station health is for admins (Settings > Health)",   # #203: off the header, on an admin tab
    "/api/reception/sample": "a reception test's audio is for the admin running it",   # #410 (server.py serves it)
}

# Who may POST what (enforced only with a verified sign-in, see above): "dispatch" = dispatchers and
# admins (running calls), "admin" = admins only (Settings in every form, and the radios), "read" =
# anyone signed in (a POST that changes nothing). First match wins; a path not here is "admin".
WRITES = (
    (re.compile(r"/api/cad/incident(?:/\d{1,9}(?:/(?:accept|dismiss|close|reopen|note|recommend|preview|dispatch|upgrade"
                r"|attach|link|unlink|keep|return|feed-use|auto-undo))?)?"), "dispatch"),      # events, notes, dispatches, radio links
    (DISPATCH_MODE, "dispatch"),                                            # 60 Control / PCFD dispatching (#311)
    (UNIT, "dispatch"),                                                     # unit status on the board
    (PREALERT, "dispatch"),                                                 # a phone pre-alert that is nothing
    (AIRPORT_ALERT, "dispatch"),                                            # an airport pre-alert: an event, or nothing
    (HINT, "dispatch"),                                                     # a status suggested from the radio (#37)
    (ALARM_ACK, "dispatch"),                                                # an emergency alarm seen (#128)
    (ALARM_CLEAR, "dispatch"),                                              # ... and resolved: its window closed
    (re.compile(r"/api/cad/radio-id"), "dispatch"),                          # who a new FG1 radio is (#163)
    (re.compile(r"/api/incident/[^/]{1,80}/close"), "dispatch"),            # the monitor's incident list (server.py)
    (UNIT_STATION, "admin"),                                                # a unit's home station: Settings > Units
    (re.compile(r"/api/cad/settings/(?:channels|stt)"), "admin"),           # Settings > Radios: record / transcribe /
                                                                            # engine per channel, the AWS vocabulary + limit
    (SETTINGS, "admin"),                                                    # lists, units, dispatch, users, import ...
    (re.compile(r"/api/cad/runcards(?:/delete)?"), "admin"),
    (re.compile(r"/api/cad/radios(?:/delete)?"), "admin"),                 # the radio-ID registry
    (DATASOURCE, "admin"),                                                  # a data source's Refresh now (#186)
    (DVR_PIN, "dispatch"),                                                  # keep recording a camera (#277)
    (DVR_CLIP, "dispatch"),                                                 # save a camera clip on an event (#323)
    (ATTACHMENT, "admin"),                                                  # delete an event's attachment (#323)
    (re.compile(r"/api/(?:talkgroup|talkgroup-group|mode|retranscribe|adsb-receiver)"), "admin"),   # the station's radios (server.py)
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
    ev = n or ("a 60 Control page" if act in ("dismiss", "accept", "reopen") else f"event {iid}")
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
    if act == "feed-use":
        return f"Used the CAD feed's {_clip(b.get('field'), 20)} on {ev}", n
    if act == "reopen":
        return f"Reopened {ev}" + (f": {o['status']}" if o.get("status") else ""), n
    if act == "note":
        tags = (o.get("units") or []) + [f"@{p['label']}" for p in o.get("places") or []]
        return f"Note on {ev}" + (f" ({', '.join(tags)})" if tags else "") + f": {_clip(b.get('text'))}", n
    if act == "dispatch":
        return f"Dispatched {ev}: {_units(c, b.get('unit_ids'))}" + (" (full response)" if b.get("full") else ""), n
    if act == "upgrade":
        what = [x for x in (_type(b.get("type", ""), b.get("subtype", "")) if b.get("type") else "",
                            _units(c, b.get("unit_ids")) if b.get("unit_ids") else "", "full response" if b.get("full") else "") if x]
        return f"Upgraded {ev}" + (f": {', '.join(what)}" if what else ""), n
    if act == "attach" and b.get("status"):
        return (f"Added {_units(c, b.get('unit_ids'))} to {ev} {c.lists.status_phrase(str(b['status']))} (not dispatched)"
                + (f"; moved {', '.join(o['moved'])}" if o.get("moved") else "")), n
    if act == "attach":
        return f"Attached {_units(c, b.get('unit_ids'))} to {ev}", n
    if act == "link":
        return f"Linked a transmission to {ev}", n
    if act == "unlink":
        return f"Removed a transmission from {ev}", n
    if act == "keep":
        return f"{'Stopped keeping' if b.get('undo') else 'Kept'} a transmission on {ev}", n
    if act == "auto-undo":
        return f"Undid an automatic step on {ev}", n
    if act == "return":
        return f"Returned {_units(c, [b.get('unit_id')])} to {ev} ({b.get('status', '')})", n
    return f"{act.capitalize()} {ev}", n


def _unit_status(m, b, o, c):
    n = o.get("incident_number") or (_number(c, b["incident_id"]) if b.get("incident_id") else "")
    return f"Set {o.get('name', '')} to {b.get('status', '')}" + (f" ({_clip(b['note'], 40)})" if b.get("note") else "") \
        + (f" on {n}" if n else "") + (f"; command from {', '.join(o['command_from'])}" if o.get("command_from") else ""), n


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
    (DISPATCH_MODE, lambda m, b, o, c: (f"Dispatch mode: {o.get('label') or _clip(b.get('mode'), 20)}", "")),
    (PREALERT, lambda m, b, o, c: (f"Dismissed the phone pre-alert of {_prealert_at(o)}"
                                   + (f": {_clip(b.get('reason'))}" if b.get("reason") else ""), "")),
    (AIRPORT_ALERT, lambda m, b, o, c: (
        f"{'Accepted' if m.group(2) == 'accept' else 'Dismissed'} the airport pre-alert: {_clip(o.get('title'), 80)}"
        + (f" (event {(o.get('incident') or {}).get('number')})" if m.group(2) == "accept" and o.get("incident") else "")
        + (f": {_clip(b.get('reason'))}" if b.get("reason") else ""), (o.get("incident") or {}).get("number") or "")),
    (HINT, lambda m, b, o, c: (f"{'Accepted' if m.group(2) == 'accept' else 'Dismissed'} the radio's "
                               f"{o.get('name') or o.get('unit') or ''} {o.get('status', '')}".replace("  ", " ")
                               + (f" on {o['incident_number']}" if o.get("incident_number") else ""), o.get("incident_number") or "")),
    (ALARM_ACK, lambda m, b, o, c: (f"Acknowledged the emergency alarm: {_clip(o.get('who'), 30)} {o.get('what', '')} "
                                    f"on {o.get('channel', '')}".rstrip(), o.get("incident_number") or "")),
    (ALARM_CLEAR, lambda m, b, o, c: (f"All clear on the emergency alarm: {_clip(o.get('who'), 30)} {o.get('what', '')} "
                                      f"on {o.get('channel', '')}".rstrip(), o.get("incident_number") or "")),
    (re.compile(r"/api/cad/radio-id"), lambda m, b, o, c: (
        f"Radio {b.get('system', '')} {_clip(b.get('radio_id'), 12)} "
        + ("ignored" if b.get("ignore") is True else f"named {_clip(b.get('unit') or b.get('label'), 40)}")
        + (f" (its transmissions on {', '.join(o.get('linked') or [])})" if o.get("linked") else ""),
        (o.get("linked") or [""])[0])),
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
    (DVR_PIN, lambda m, b, o, c: (f"{'Kept' if b.get('on') else 'Stopped keeping'} camera {m.group(1)} recording", "")),
    (DVR_CLIP, lambda m, b, o, c: (f"Saved a clip of camera {m.group(1)} to {_number(c, b.get('incident_id')) or 'an event'} "
                                   f"({_span(b)})", _number(c, b.get("incident_id")))),
    (ATTACHMENT, lambda m, b, o, c: (f"Deleted the {(o.get('attachment') or {}).get('kind', '')} attachment {m.group(1)} from "
                                     f"{_number(c, (o.get('attachment') or {}).get('incident_id')) or 'an event'}",
                                     _number(c, (o.get("attachment") or {}).get("incident_id")))),
    (DATASOURCE, lambda m, b, o, c: (f"Refreshed data source: {_clip((o.get('source') or {}).get('name') or unquote(m.group(1)), 60)}", "")),
    (re.compile(r"/api/talkgroup"), lambda m, b, o, c: (f"Talkgroup {b.get('tg')}: {_toggles(b) or 'no change'}", "")),
    (re.compile(r"/api/talkgroup-group"), lambda m, b, o, c: (f"Talkgroups {_clip(b.get('group'), 40)}: {_toggles(b) or 'no change'}", "")),
    (re.compile(r"/api/mode"), lambda m, b, o, c: (f"Radio mode: {_clip(b.get('mode'), 30)}" + (f" ({_clip(b['sdr'], 30)})" if b.get("sdr") else ""), "")),
    (re.compile(r"/api/reception"), lambda m, b, o, c: (
        (f"Reception test started: {_clip(', '.join(map(str, b.get('jobs') or [b.get('job')])), 40)} on SDR {_clip(b.get('sdr'), 30)}"
         + (f", pausing {_clip(b.get('pause'), 30)}" if b.get("pause") else "")) if b.get("action") == "start"
        else "Reception test stopped" if b.get("action") == "stop" else "Reception test: antenna note", "")),
    (re.compile(r"/api/adsb-receiver"), lambda m, b, o, c: ((f"Named SDR {_clip(b.get('sdr'), 30)} the ADS-B receiver"
                                                            if b.get("sdr") else "Unnamed the ADS-B receiver's SDR"), "")),
    (re.compile(r"/api/retranscribe"), lambda m, b, o, c: ("Retranscribe: every recording" if b.get("all")
                                                           else f"Retranscribe: {len(b.get('ids') or [])} recordings", "")),
)


def _span(b) -> str:
    try:
        s = float(b["to"]) - float(b["from"])
        return f"{int(s // 60)}:{int(s % 60):02d}"
    except (KeyError, TypeError, ValueError):
        return "?"


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

    def _datasources(self, q: dict, who) -> tuple[int, bytes, str]:
        """GET /api/cad/datasources: admins and dispatchers (Settings > Data sources, #186)."""
        if who is not None and who.email and self.cad.settings.standing(who.email)["may"] not in DS_READERS:
            return _json(403, {"error": "Admins and dispatchers only"})
        ds = self.cad.datasources
        if ds is None:
            return _json(200, {"configured": False, "rev": "", "sources": [], "groups": [], "choices": []})
        out = ds.public() | {"configured": True}
        if (q.get("rev") or [""])[0] == out["rev"]:
            return _json(200, {"rev": out["rev"], "same": True})
        return _json(200, out)

    def read_refused(self, path: str, who) -> tuple[int, bytes, str] | None:
        """A GET in ADMIN_READS from someone signed in who isn't an admin: the 403; else None (nothing is checked
        without a verified person, as with the POSTs)."""
        why = ADMIN_READS.get(path)
        if why is None or who is None or not who.email:
            return None
        try:
            if self.cad.settings.standing(who.email)["may"] == "admin":
                return None
        except CadError:
            pass
        return _json(403, {"error": f"Admins only: {why}"})

    def _notify(self, who) -> tuple[int, bytes, str]:
        """GET /api/cad/notify: Settings > System > Push notifications, admins only (ADMIN_READS: a topic's name is its
        only secret)."""
        p = getattr(self.cad, "pushes", None)
        return _json(200, {"configured": False} if p is None else {"configured": True, **p.page()})

    def _dvr_pin(self, cid: str, body: dict) -> dict:
        """POST /api/cad/cameras/<id>/dvr/pin {on}: keep recording this camera until unpinned, or stop (#277)."""
        fn = getattr(self.cad, "dvr_pin", None)
        if fn is None:
            raise CadError("no camera recording here")
        if not isinstance(body.get("on"), bool):
            raise CadError("on: true or false")
        try:
            return {"ok": True, "dvr": fn(cid, body["on"])}
        except KeyError:
            raise CadError(f"no camera {cid}") from None
        except ValueError as e:
            raise CadError(str(e)) from None

    def _ds_refresh(self, key: str, body: dict) -> dict:
        """POST /api/cad/datasources/<key>/refresh: started (the row), in the Settings audit log too."""
        ds = self.cad.datasources
        if ds is None:
            raise CadError("no data sources here (the CAD of record has them)")
        src = ds.get(key)
        if src is None:
            raise CadError(f"no data source {key!r}")
        before = ds.row(key)["last_fetched"]
        row = ds.refresh(key, by=str(body.get("operator") or ""))
        was = datetime.fromtimestamp(before, TZ).isoformat(timespec="seconds") if before else "never"
        self.cad.settings.note(str(body.get("operator") or ""), "system", f"datasources/{key}", f"last fetched {was}", src.action)
        return {"ok": True, "running": row["running"], "source": row}

    def get(self, path: str, query: str, who=None) -> tuple[int, bytes, str]:
        if (no := self.read_refused(path, who)) is not None:
            return no
        try:
            if path == "/api/cad/datasources":
                return self._datasources(parse_qs(query), who)
            if path == "/api/cad/notify":
                return self._notify(who)
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
            if path == "/api/cad/tones":
                return _json(200, self.cad.tone_names())
            if path == "/api/cad/state":
                return _json(200, self.cad.state())
            if path == "/api/cad/runcards":
                return _json(200, {"run_cards": self.cad.run_cards()})
            if path == "/api/cad/airport":
                return _json(200, airport.layer())
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
            if path == "/api/cad/alarms":
                return _json(200, self.cad.alarms_page())
            if m := SUGGEST.fullmatch(path):
                return _json(200, {"units": self.cad.suggest_units(int(m.group(1)), (parse_qs(query).get("q") or [""])[0][:20])})
            if m := INCIDENT.fullmatch(path):
                if m.group(2) is None:
                    out = self.cad.full(int(m.group(1)))
                    att = getattr(self.cad, "attachments", None)
                    if att is not None and isinstance(out, dict):
                        out["attachments"] = att.list(int(m.group(1)))
                    return _json(200, out)
            if (m := INCIDENT_ATTACHMENTS.fullmatch(path)) or (m := ATTACHMENT.fullmatch(path)):
                return self._attachments_get(path, m, parse_qs(query), who)
            if m := EXPORT.fullmatch(path):
                data = self.cad.export(m.group(1))
                return (200, data, "text/csv; charset=utf-8") if data is not None else (404, b"no such export\n", "text/plain")
            if path == "/api/cad/tts/voices":
                return _json(200, self.cad.tts_voices())
            if path == "/api/cad/places":
                return _json(200, self.cad.search_places(parse_qs(query)))
            if path == "/api/cad/geocode":
                return _json(200, self.cad.geocode((parse_qs(query).get("q") or [""])[0]))
            if path == "/api/cad/geocode/here":
                return _json(200, self.cad.here(parse_qs(query)))
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
        except TooSoon as e:                   # a data source's Refresh now, refused (#186): rate limit, running, resting
            if signed:
                self.refused(path, who, str(e), client)
            return _json(429, {"error": str(e)})
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
            fn = {None: c.update, "accept": c.accept, "dismiss": c.dismiss, "close": c.close, "reopen": c.reopen, "note": c.note,
                  "recommend": c.recommend, "preview": c.preview, "dispatch": c.dispatch, "upgrade": c.upgrade,
                  "attach": c.attach, "link": c.link_traffic, "unlink": c.unlink_traffic, "keep": c.keep_traffic,
                  "return": c.return_unit, "feed-use": c.feed_use, "auto-undo": c.auto_undo}[action]
            return fn(iid, body)
        if m := UNIT.fullmatch(path):
            return c.unit_status(int(m.group(1)), body)
        if path == "/api/cad/dispatch-mode":
            return c.dispatch_mode(body)
        if m := PREALERT.fullmatch(path):
            return c.prealert_dismiss(int(m.group(1)), body)
        if m := AIRPORT_ALERT.fullmatch(path):
            return c.airport_alert(int(m.group(1)), m.group(2), body)
        if m := HINT.fullmatch(path):
            return c.hint(int(m.group(1)), m.group(2), body)
        if m := ALARM_ACK.fullmatch(path):
            return c.alarm_ack(int(m.group(1)), body)
        if m := ALARM_CLEAR.fullmatch(path):
            return c.alarm_clear(int(m.group(1)), body)
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
        if path == "/api/cad/radio-id":
            return c.name_radio(body)
        if m := DATASOURCE.fullmatch(path):
            return self._ds_refresh(unquote(m.group(1)), body)
        if m := DVR_PIN.fullmatch(path):
            return self._dvr_pin(m.group(1), body)
        if m := DVR_CLIP.fullmatch(path):
            return {"ok": True, "attachment": self._attach_call(lambda a: a.save_camera(m.group(1), body,
                                                                                         str(body.get("operator") or "")))}
        if (m := ATTACHMENT.fullmatch(path)) and m.group(2) == "delete":
            return {"ok": True, "attachment": self._attach_call(lambda a: a.delete(m.group(1), str(body.get("operator") or "")))}
        return None

    # -- incident attachments (#323, sizeup/attachments.py) -----------------------------------------------------------

    def _attach_call(self, fn):
        from sizeup.attachments import NOT_CONFIGURED, AttachError
        from sizeup.aws import AwsError
        att = getattr(self.cad, "attachments", None)
        if att is None:
            raise CadError(NOT_CONFIGURED)
        try:
            return fn(att)
        except AttachError as e:
            raise CadError(str(e)) from None
        except AwsError as e:
            raise CadError(f"S3: {e}") from None

    def _attachments_get(self, path: str, m, q: dict, who):
        """The attachments' GETs: anyone signed in; a view, a download and an export are audited (and in the person's
        activity)."""
        from sizeup.attachments import NOT_CONFIGURED, AttachError
        from sizeup.aws import AwsError
        att = getattr(self.cad, "attachments", None)
        email = who.email if who is not None and who.email else ""
        name = email
        if email:
            try:
                name = self.cad.settings.standing(email)["operator"] or email
            except CadError:
                pass
        hdr = {"Cache-Control": "no-store"}

        def note(summary: str, iid) -> None:
            if email:
                self.cad.activity.record(email, "action", f"GET {path}", summary, _number(self.cad, iid))
        try:
            if m.re is INCIDENT_ATTACHMENTS:
                iid, what = int(m.group(1)), m.group(2)
                if att is None:
                    if what == "attachments":
                        return _json(200, {"configured": False, "attachments": []})
                    return _json(400, {"error": NOT_CONFIGURED})
                if what == "attachments":
                    return _json(200, {"configured": att.configured,
                                       "attachments": att.list(iid, deleted=(q.get("deleted") or [""])[0] == "1")})
                if what == "attachments/audit":
                    return _json(200, {"audit": att.audit_rows(iid=iid)})
                body, fname = att.export(iid, name)
                note(f"Exported the attachments of {_number(self.cad, iid) or f'event {iid}'}", iid)
                return 200, body, "application/zip", {"Content-Disposition": f'attachment; filename="{fname}"', **hdr}
            if att is None:
                return _json(400, {"error": NOT_CONFIGURED})
            aid, what = m.group(1), m.group(2)
            row = att.row(aid)
            if row is None:
                return _json(404, {"error": "no such attachment"})
            if what is None:
                return _json(200, {"attachment": att.public(row), "audit": att.audit_rows(aid=aid, limit=100)})
            if what == "delete":
                return 405, b'{"error":"POST"}', JSON
            if what == "index.m3u8":
                body = att.playlist(aid)
                att.viewed(row, name)
                return 200, body, "application/vnd.apple.mpegurl", hdr
            if what == "audio.wav":
                body = att.audio_wav(aid)
                att.viewed(row, name)
                return 200, body, "audio/wav", {"Cache-Control": "private, max-age=3600"}
            if what == "download":
                body, fname, ctype = att.download(aid)
                att.viewed(row, name, "download")
                note(f"Downloaded the {row['kind']} attachment {aid}", row["incident_id"])
                return 200, body, ctype, {"Content-Disposition": f'attachment; filename="{fname}"', **hdr}
            return 200, att.segment(aid, int(m.group(3))), "video/MP2T", {"Cache-Control": "private, max-age=3600"}
        except KeyError:
            return _json(404, {"error": "not stored (saving, failed or deleted)"})
        except AttachError as e:
            return _json(400, {"error": str(e)})
        except AwsError as e:
            return _json(502, {"error": f"S3: {e}"})
