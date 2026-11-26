"""Settings: what the console's Settings page edits, kept in the CAD database over the data file.

data/cad_lists.json is the defaults. An edit is stored as an override (settings_overrides: section,
key -> JSON) and laid over the defaults (overlay()); the result is checked exactly like the file
(lists.build()) before anything is written, then used at once: the lists, the boards, the dispatch
wording, the tones and channels change for every open console on the next state push (no restart).
Reset to default = drop the override (one item, or a whole section).

    event_types, event_subtypes,   key CODE -> the item as edited ({"name", "color", "retired": true...});
    dispositions, received_via,              {"deleted": true}: a default item removed (only if never used)
    unit_statuses,                 key "_order" -> [codes]: the list's order
    location_qualifiers            (a qualifier: {"name", "short", "spoken"}: "In front of", "IFO", "in front of")
    event_class                    key TYPE | TYPE/SUBTYPE | */SUBTYPE -> emergency | non-emergency | null (a default rule removed)
    type_subtypes                  key TYPE -> [the subtypes it allows] ([]: no subtype)
    status_flow                    key STATUS | "*" -> [next statuses] (the incident's unit buttons) | null (removed)
    geo_town_order                 key "_list" -> [towns]
    units                          key CODE -> the unit as edited ({kind, spoken, title, department, spare,
                                   response, station, status_set (another agency's: its statuses, third party
                                   or not), retired}); key "_boards" -> the boards' layout
    dispatch_wording               key template name -> text; "lead_in_joint_only" -> {town: text}
    dispatch_tones                 key joint town -> [[Hz, s], ...] | null (not set: PCFD's are played)
    dispatch                       key "tones" -> PCFD's tone groups, "channels" -> [labels], "voice", "speed";
                                   the ETA estimates (sizeup/cad/eta.py): "road_factor", "speed_mph", "turnout_min",
                                   "return_margin_min", "order" (nearest | card), "quarters_status"; "weather_speak"
                                   (true: an NWS warning is said once by the dispatch voice where a console plays
                                   it, never keyed; sizeup/weather.py, #132)
    channels                       key "FG1" | "South 2" | "tg:307" -> {record?, transcribe?, engine?}: which channel is
                                   kept, transcribed, and by what (sizeup/channels.py; Settings > Radios)
    stt                            key "vocabulary" -> [phrases] (Amazon Transcribe's custom vocabulary),
                                   "aws_monthly_limit_usd" -> the monthly AWS limit (sizeup/transcribe.py),
                                   "aws_context_budget_usd" -> another P25 system's (police) own monthly AWS limit
    tones                          the tone book (#80, #115, #122; sizeup/cad/tonelist.py): key "A/B" -> a pair named
                                   here {a_hz, b_hz, places, service, role?, confirmed?, page?} (#84/#115: {name, ...});
                                   "A/B@<name in the list>" -> an edit to a row of data/westchester_tones.csv {list,
                                   places, service, role?, confirmed?} or {list, deleted: true}; "entity:<places>|<service>"
                                   -> an entity {places, service, ours?, notes?, aliases?, merged_into?}. Read over the
                                   list, never written into it (sizeup/app.py _apply_named_tones)
    camera_wall                    key "margin_m" -> metres around our fire districts, "include" / "exclude" -> [camera
                                   ids]: the camera wall's area (#125, Settings > System; sizeup/roads.py Roads.area)
    notify                         key "alerts" | "status" -> {"topic": name} | {"off": true}, "server" -> the ntfy URL;
                                   "prealert_ring" | "prealert_status" -> {"topic": name} | {"off": true}: the Tapout feed
                                   sizeup SUBSCRIBES to (env SIZEUP_PREALERT_*_TOPIC wins; none set: that feed is off),
                                   "protected" -> {"topics": [names]}: more topics never published to (the subscribed
                                   ones always are). None of these names is in the repo.
                                   the push topics by kind over env / SSM / the config (sizeup/notify.py Pushes;
                                   Settings > System > Push notifications, admins). Secret by name: never exported,
                                   kept through an import, a fingerprint in the audit log
    atc                            key "identify" -> true | false (default true): ATC talkers on the airband rows, "map" ->
                                   true | false (default true): the talker on the Aircraft map (#252, sizeup/atc_talkers.py;
                                   Settings > System > ATC talkers, admins)
    hub_errors                     key "on" -> true | false (default true), "per_signature" / "total" -> a count: the hub's
                                   own error spikes pushed to the status topic (#285, sizeup/errwatch.py; Settings >
                                   System > Hub error alerts, admins)
    map_display                    key "detail_zoom" | "active_zoom" | "board_detail_zoom" | "board_active_zoom" -> a zoom:
                                   below it an idle / active rig is drawn small on the console's maps / the wall board's
                                   (#376, sizeup/cad/mapview.py DISPLAY_DEFAULTS; Settings > System > Map display, admins)
    premise_visibility             key a premise-note category ("medical") -> "dispatcher" | "admin": who may see its notes
                                   (not set: everyone signed in; #403, sizeup/cad/premise.py; Settings > System > Address
                                   notes, admins); also a pre-plan point kind ("knox_box", "gate_access": #456,
                                   sizeup/cad/preplan.py RESTRICTABLE)
    audio_retention                key "_default" | a channel's label ("FG1", "HPN Tower") | "tg:<n>" -> days (0.5 to 365): how
                                   long the hub keeps that channel's clips, the ones already kept too (#414,
                                   sizeup/audiostore.py keep_days; Settings > System > Audio retention, admins)
    alert_mutes                    key an alert kind (sizeup/cloud/health.py MUTE_KINDS: adsb, whisper, job, trunk,
                                   silent, hub_errors ...) -> {"until": epoch | null (until unmuted)}: shown on Settings >
                                   Health, not pushed (#340; Settings > System > Push notifications, admins)
    signal_metrics                 key "margin_db" | "miss_pct" | "shift_db" | "opens_x" | "rate_x" | "cc_drop_pct" -> a
                                   number: the suggested squelch's margin and the shift warnings' thresholds (#606,
                                   sizeup/cloud/metricsinsight.py; Settings > Radios > Signal, admins)
    radio_folders                  key "layout" -> {folders: [{id, name, section, items: [source keys]}], names?: {source
                                   key: name}}: the Radio page rail's folders, set for the department, and the channels'
                                   display names (#607, sizeup/cad/radiofolders.py; admins, from the rail's Edit or a
                                   name from Settings > Radios > Channels); not set: the automatic grouping
    radio_views                    key "views" -> [{id, name, mark, color, filter: {ours, services, systems, sources, units,
                                   linked}}]: the Radio page rail's views, set for the department (sizeup/cad/radioviews.py;
                                   admins, from the rail or Settings > Radios > Views); not set: the views that were coded
    radio_main                     key an FM / airband channel's label -> true | false: its Main switch (Settings > Radios >
                                   Channels, admins): Main = Our calls; not set: on, but an airband channel's off (a
                                   talkgroup's Main is in the talkgroups' own choices, sizeup/talkgroups.py)
    radio_shorts                   key a qualified source key -> a short label (1 to 24 characters) for the chips: the second
                                   field beside a channel's name in Settings > Radios > Channels (sizeup/radionames.py:
                                   every label on every page; admins); not set: its name
    radio_services                 key a source key (qualified: p25:<system>:<tg>, fm:<label>, air:<label>) -> fire | police
                                   | ems | transit | air | other: that source's service whatever its talkgroups file or
                                   its group's words say (sizeup/sourcekeys.py; admins, from Settings > Radios > Channels
                                   or the rail's Edit); not set: automatic. A change re-tags its rows of the last
                                   RETAG_DAYS (Cad.retag_services, audited with the count)
    datasources                    key a data source ("gis:hydrants", "aircraft:registry") -> off | hourly | daily | weekly |
                                   monthly: how often the registry refreshes it (#186, Settings > Data sources;
                                   sizeup/datasources.py); not set: the source's default
    station_logs                   key "on" | "level" | "include" | "exclude" | "retention_days": the station's logs into
                                   CloudWatch (sizeup/cloud/cwlogs.py; Settings > Station > Logs, admins)
    dongle_swap                    key "auto_critical" -> true | false (default false): a gone dongle's Fire/EMS 15 or
                                   paging job moved by itself to a new dongle only when the station marks the swap "auto"
                                   (App._auto_ok; sizeup/cloud/dongleswap.py; Settings > Radios > Receivers, admins)
    hydrant_display                key "assume_c" -> true | false (default true): an untested hydrant drawn as Class C,
                                   always marked assumed ("C*"; #840, sizeup/cad/hydrants.py; Settings > Hydrants, admins)
    reminders                      key "<rule>_on" -> true | false, "<rule>_min" -> minutes (rules: pending, dispatched,
                                   queued, onscene, until), "units" -> fire_ems | agencies | all, "snooze_min" -> minutes:
                                   what flashes when left too long (sizeup/cad/reminders.py; Settings > Dispatch >
                                   Reminders, admins); not set: the defaults (pending and dispatched on, 5 min)
    feed_types                     key "<feed>|<code>" (a feed's source id, its own code "MAIN" or "MAIN,SUB") -> {"type",
                                   "subtype"}: the department's mapping, over the automatic one (sizeup/cad/feedtypes.py;
                                   Settings > Lists > Feed types, admins, audited as Lists)
    department_units               key a feed's department entry code -> the department it pages ("Port Chester FD"), or
                                   {"name", "accepts": true}: a whole department, never a unit (#370,
                                   sizeup/cad/sources.py; admins, no panel); never a board unit's code

Codes never change (they are stored on events and exported): a rename changes the name only. An
item in use can't be deleted, only retired (named on old events, off the pick lists). Every change
writes settings_audit (who, page section, key, old -> new). operators: the people at the console
(a picker for the operator field), each optionally keyed by the email they sign in with through
Cloudflare Access on the hub (person(): the display name and callsign for a verified email; an email
not listed yet is added unnamed for an admin to name; standing(): what that person may do, enforced
by sizeup/cad/api.py). export() / import: every setting as one JSON file, checked and shown as a diff
(dry run) before it replaces anything.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

from sizeup import channels as channels_mod
from sizeup import radionames
from sizeup.cad import alarms as alarms_mod
from sizeup.cad import commonplaces, dispatcher, eta, feedtypes, mapview, radiofolders, radioviews, reminders, runcards, tonelist
from sizeup.cad import lists as lists_mod
from sizeup.cad.lists import CLASSES, CODE, COLOR, LIST_SECTIONS, LONG_CODE, RESPONSES, ROLES, ListsError
from sizeup.cad.store import OPERATOR_MAX, SCHEMA_VERSION, TZ, CadError
from sizeup import audiostore, errwatch, polly, sourcekeys
from sizeup.cloud import cwlogs, metricsinsight
from sizeup.cloud import health as health_mod
from sizeup.geo.localaddr import parse_number, split_address
from sizeup.geo.places import norm_tokens
from sizeup.radio.tones import pair_key
from sizeup.tts import KOKORO_VOICES

FORMAT, FORMAT_VERSION = "sizeup-cad-settings", 1
OPERATOR_ROLES = ("admin", "dispatcher", "viewer")
REMOVE = object()
EMAIL = re.compile(r"[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[^@\s<>]{1,63}")

# Where each stored section shows in Settings (the audit log's section).
PAGE_SECTION = {**{s: "lists" for s in LIST_SECTIONS}, "event_class": "lists", "geo_town_order": "lists", "status_flow": "lists",
                "type_subtypes": "lists",
                "units": "units", "dispatch_wording": "dispatch", "dispatch_tones": "dispatch", "dispatch": "dispatch",
                "channels": "radios", "stt": "radios", "places": "lists", "tones": "radios", "alarms": "system",
                channels_mod.SQUELCH_SECTION: "radios"}
# The camera wall's area (#125, sizeup/roads.py Roads.area): Settings > System.
WALL_SECTION, WALL_KEYS, WALL_MARGIN = "camera_wall", ("margin_m", "include", "exclude"), (0, 5000, 1000)
PAGE_SECTION[WALL_SECTION] = "system"
# How often each data source is refreshed (#186, sizeup/datasources.py): Settings > Data sources, audited as System.
DS_SECTION = "datasources"
PAGE_SECTION[DS_SECTION] = "system"
# Who is talking on HPN Tower (#252, sizeup/atc_talkers.py): Settings > System > ATC talkers, audited as System.
ATC_SECTION, ATC_KEYS = "atc", {"identify": "Identify ATC talkers", "map": "Show ATC talker on the map",
                                 # #257 (sizeup/atc_vocab.py): the sizeup-atc vocabulary refreshed from the operators seen
                                 "vocab": "Refresh the ATC vocabulary from the traffic"}
PAGE_SECTION[ATC_SECTION] = "system"
# How the hydrants are drawn (#840, sizeup/cad/hydrants.py): Settings > Hydrants, audited as System.
HYD_SECTION, HYD_KEYS = "hydrant_display", {"assume_c": "Untested hydrants: assume Class C"}
PAGE_SECTION[HYD_SECTION] = "system"
# A dongle swapped for a new one (sizeup/radio/replace.py, sizeup/cloud/dongleswap.py): Settings > Radios > Receivers,
# audited as Radios. key "auto_critical" -> true: the county's and paging's jobs move by themselves to a new dongle,
# 60 s after their dongle went, only when the station marks the swap "auto" (App._auto_ok: one new dongle fits, in the
# old one's USB port, first seen after it went, the job running nowhere, the same tuner; a push says so). Off by
# default: a person's "Use it" only.
# A paging channel's squelch (Settings > Radios > Squelch, admins, audited as Radios; Settings._paging_guard): its Open
# at least PAGING_IDLE_GAP_DB over the recent idle max, at most metricsinsight.PAGE_GAP_DB under the recent pages; with no
# pages to check against, at most PAGING_BLIND_RAISE_DB over the receiver's default.
PAGING_IDLE_GAP_DB, PAGING_BLIND_RAISE_DB = 2.0, 6.0
SWAP_SECTION, SWAP_KEYS = "dongle_swap", {"auto_critical": "Auto-replace a gone dongle for Fire/EMS 15 and paging"}
PAGE_SECTION[SWAP_SECTION] = "radios"
# What flashes when a call or a unit is left too long (sizeup/cad/reminders.py): Settings > Dispatch, audited as Dispatch.
REM_SECTION = reminders.SECTION
PAGE_SECTION[REM_SECTION] = "dispatch"
# A feed's department entries (#370, sizeup/cad/sources.py department_units): key the code as the feed writes it ->
# the department ("Port Chester FD"), or {"name", "accepts": true} (it accepts a call alone in 60 Control mode). The
# hub's own (never in the repo): set by admins (POST, no panel), audited as Dispatch. Never a board unit's code.
DEPT_SECTION = "department_units"
PAGE_SECTION[DEPT_SECTION] = "dispatch"
# A feed's own type codes mapped to ours by the department (sizeup/cad/feedtypes.py): Settings > Lists > Feed types,
# admins, audited as Lists. key "<feed>|<code>" -> {"type", "subtype"}.
FT_SECTION = feedtypes.SECTION
PAGE_SECTION[FT_SECTION] = "lists"
# The push topics by kind (sizeup/notify.py Pushes): Settings > System > Push notifications, audited as System.
NOTIFY_SECTION = "notify"
PAGE_SECTION[NOTIFY_SECTION] = "system"
# The dispatch mode (#311, sizeup/cad/automode.py): switched from the header, not a setting: never exported, kept
# through an import, like the push topics.
MODE_SECTION = "dispatch_mode"
PAGE_SECTION[MODE_SECTION] = "dispatch"
KEPT_SECTIONS = (NOTIFY_SECTION, MODE_SECTION)
# Radio pre-alerts (sizeup/intel, #411): Settings > System > Intel, audited as System (sizeup/intel/admin.py).
INTEL_SECTIONS = ("intel", "intel_packs", "intel_rules")
PAGE_SECTION.update({k: "system" for k in INTEL_SECTIONS})
# The hub's own error spikes (#285, sizeup/errwatch.py): Settings > System > Hub error alerts, audited as System.
ERR_SECTION = errwatch.SECTION
PAGE_SECTION[ERR_SECTION] = "system"
# Health alerts muted (#340, sizeup/cloud/health.py): Settings > System > Push notifications, audited as System.
MUTE_SECTION = health_mod.MUTE_SECTION
PAGE_SECTION[MUTE_SECTION] = "system"
MUTE_HOURS_MAX = 24 * 30
# The rigs' zooms on the maps (#376, sizeup/cad/mapview.py): Settings > System > Map display, audited as System.
MAPD_SECTION = mapview.DISPLAY_SECTION
PAGE_SECTION[MAPD_SECTION] = "system"
# Who may see each category of premise notes (#403, sizeup/cad/premise.py): Settings > System > Address notes, admins,
# audited as System. key a category -> "dispatcher" | "admin" (the least standing); not set: everyone signed in.
PREMISE_SECTION = "premise_visibility"
PAGE_SECTION[PREMISE_SECTION] = "system"
# Which services' emergencies the Radio page shows prominently when they aren't ours (Settings > Radios > Emergencies):
# a banner, a highlighted row, the full "Emergency (not ours)" chip. Any other service's: a muted "EM" chip only.
# Ours (our fire channels, our rigs, scene safety) always stand out, and so does a source whose service isn't known
# (blank): quiet is only for a service that is known and not listed. "other" (Trunk Ops, Interop, Special Events, OEM:
# where a mutual-aid firefighter or an unmapped portable of ours could be) is prominent by default.
# Presentation only: what is recorded, kept, alarmed and pushed doesn't change. Admins, audited as Radios.
# key "prominent" -> [services] (sourcekeys.SERVICES).
EM_SECTION, EM_DEFAULT = "radio_emergencies", ("fire", "ems", "other")
PAGE_SECTION[EM_SECTION] = "radios"
# The Radio page's folders (#607, sizeup/cad/radiofolders.py): how the rail's channel tree is grouped, admins, audited
# as Radios. key "layout" -> {folders: [{id, name, section, items}]}; not set: the automatic grouping.
FOLDERS_SECTION = radiofolders.SECTION
PAGE_SECTION[FOLDERS_SECTION] = "radios"
# The Radio page's views (sizeup/cad/radioviews.py): the rail's VIEWS for the department, admins, audited as Radios.
# key "views" -> [{id, name, mark, color, filter}]; not set: radioviews.SEED (the views that were coded).
VIEWS_SECTION = radioviews.SECTION
PAGE_SECTION[VIEWS_SECTION] = "radios"
# A source's service chosen by the admins (fire, police, ...: sizeup/sourcekeys.py), over its talkgroups file's column
# and its group's words: Settings > Radios > Channels and the rail's Edit, audited as Radios. key a qualified source key
# -> a service; not set: automatic.
SVC_SECTION = "radio_services"
PAGE_SECTION[SVC_SECTION] = "radios"
# An FM / airband channel's Main switch (a talkgroup's is in sizeup/talkgroups.py): Main is "Our calls" everywhere (the
# Radio page's Main and Our calls view, the live player), Settings > Radios > Channels, admins, audited as Radios.
# key a channel's label -> true | false; not set: on, but an airband channel's off (Cad.fm_main).
FM_MAIN_SECTION = "radio_main"
PAGE_SECTION[FM_MAIN_SECTION] = "radios"
# A channel's or talkgroup's short label for chips (sizeup/radionames.py): a second field beside its name in Settings >
# Radios > Channels, admins, audited as Radios. key a qualified source key -> the short label; not set: its name.
SHORT_SECTION = radionames.SECTION
PAGE_SECTION[SHORT_SECTION] = "radios"
# The station metrics read back (#606, sizeup/cloud/metricsinsight.py): the suggested squelch's margin and the shift
# warnings' thresholds, Settings > Radios > Signal, admins, audited as Radios. key -> a number (LIMITS); not set: DEFAULTS.
SIGNAL_SECTION = metricsinsight.SECTION
PAGE_SECTION[SIGNAL_SECTION] = "radios"
PREMISE_LEVELS = ("everyone", "dispatcher", "admin")
# The station's logs into CloudWatch (sizeup/cloud/cwlogs.py): Settings > Station > Logs, admins, audited as System.
LOGS_SECTION = cwlogs.SECTION
PAGE_SECTION[LOGS_SECTION] = "system"


def services_moved(before: dict, after: dict) -> list[str]:
    """The source keys whose service choice differs between two sets of overrides (settings radio_services)."""
    a, b = sourcekeys.clean_services(before.get(SVC_SECTION)), sourcekeys.clean_services(after.get(SVC_SECTION))
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def mark_retag(db, keys, at: float, operator: str) -> None:
    """In the setting's own transaction: these sources' recent rows are still to re-tag (Cad.retag_services clears it)."""
    for k in keys:
        db.execute("INSERT INTO radio_retag_pending (key, at, operator) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                   "at = excluded.at, operator = excluded.operator", (k, at, operator or ""))


def em_value(v) -> list[str]:
    """A list of services (sourcekeys.SERVICES), in their order, each once; CadError otherwise. Empty: none stand out."""
    if not isinstance(v, list) or any(x not in sourcekeys.SERVICES for x in v):
        raise CadError(f"prominent: a list of {', '.join(sourcekeys.SERVICES)}")
    return [x for x in sourcekeys.SERVICES if x in v]


def em_prominent(e) -> list[str]:
    """The stored section's services, or the default (fire, EMS, other) when unset or unreadable."""
    try:
        return em_value((e or {})["prominent"])
    except (KeyError, TypeError, CadError):
        return list(EM_DEFAULT)


def _visibility_keys() -> dict[str, str]:
    """premise_visibility's keys: the premise-note categories, and the pre-plan point kinds an admin may restrict (#456,
    sizeup/cad/preplan.py RESTRICTABLE: the Knox boxes, the gate codes)."""
    from sizeup.cad.preplan import KINDS, RESTRICTABLE
    from sizeup.cad.premise import CATEGORIES
    return {**CATEGORIES, **{k: f"Pre-plan: {KINDS[k]}" for k in RESTRICTABLE}}
# How long each channel's / talkgroup's clips are kept on the hub (#414, sizeup/audiostore.py keep_days): Settings >
# System > Audio retention, admins, audited as System; applies to the clips already kept at the next prune.
RET_SECTION = audiostore.RETENTION_SECTION
PAGE_SECTION[RET_SECTION] = "system"
# ... and its transcripts and metadata (the CAD's radio log, sizeup/cad/comms.py prune): the same keys, longer by default
TRS_SECTION = audiostore.TRANSCRIPT_SECTION
PAGE_SECTION[TRS_SECTION] = "system"
# Hand-corrected transcripts (sizeup/cad/sttfix.py): "keep" (default on): a corrected clip and its audio are keepers.
FIX_SECTION = "stt_corrections"
PAGE_SECTION[FIX_SECTION] = "system"
WALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")      # sizeup/roads.py CAMERA_ID
MAX_WALL_IDS = 200
AUDIT_SECTIONS = ("lists", "run cards", "units", "dispatch", "users", "system", "radios")
LIST_FIELDS = {"event_types": ("color",), "event_subtypes": (), "dispositions": (), "received_via": (),
               "unit_statuses": ("role", "bg", "fg"), "location_qualifiers": ("short", "spoken")}
LIST_CODE = {"event_types": CODE, "event_subtypes": CODE, "dispositions": LONG_CODE, "received_via": LONG_CODE,
             "unit_statuses": LONG_CODE, "location_qualifiers": CODE}
LIST_TITLES = {"event_types": "Event types", "event_subtypes": "Event subtypes", "dispositions": "Dispositions",
               "received_via": "Received via", "unit_statuses": "Unit statuses", "location_qualifiers": "Location qualifiers"}
WORDING = (("new", "First dispatch"), ("add_on", "Added to the assignment"), ("upgrade", "Upgrade"),
           ("full_response", "Full department response"), ("remarks", "Dispatcher remarks"), ("lead_in", "Lead-in"),
           ("lead_in_joint", "Lead-in with a joint department"), ("new_no_location", "First dispatch, no location"),
           ("already_group", "Add-on: units already there"), ("change_type", "Upgrade: new type"),
           ("change_type_full", "Upgrade: new type, full response"), ("change_full", "Upgrade: full response"),
           ("respond", "Upgrade: more units"))
MAX_TONES, MAX_GROUPS, MAX_CHANNELS = 8, 4, 8
MAX_VOCAB, MAX_PHRASE_CHARS = 1500, 100
STT_KEYS = ("vocabulary", "aws_monthly_limit_usd", "aws_context_budget_usd",   # the "stt" section's keys
            "catchup", "catchup_max_age_h")    # shed clips caught up (sizeup/sttcatchup.py): on/off, how far back
MAX_VOCAB_BYTES = 50_000          # AWS's limit; also keeps the Settings request under its 64 KB
MAX_PLACE_NAMES = 300
# The county's "common place names" list (sizeup/cad/commonplaces.py): imported into section "places" under keys
# "county/...", replaced as a whole on each import; Settings > Lists > Place names (admins), audited as Lists, counts only.
COMMON_SECTION = "common_places"
MAX_COMMON_NAMES = commonplaces.MAX_ROWS
# The department's own address points (sizeup/geo/localaddr.py): an address the map data lacks ("189-1/2 Example St"),
# pinned by hand. Key: "<number> <street>, <town>" in lower case; value {number (as written), street (the gazetteer's
# name), muni, lat, lon, note}. Settings > Lists > Address points (admins), audited as Lists, exported with the rest.
ADDR_SECTION, MAX_LOCAL_ADDRESSES, ADDR_PIN_M = "local_addresses", 500, 400
PAGE_SECTION[ADDR_SECTION] = "lists"
TONES_SECTION, MAX_NAMED_TONES, TONE_HZ = "tones", 400, tonelist.TONE_HZ     # the decoder's band (radio/tones.py)


def tone_groups(tones) -> list[list[list[float]]]:
    """[[hz, s], ...] (one group) or [[[hz, s], ...], ...] (one per department paged) -> groups."""
    return [[list(t) for t in g] for g in dispatcher.tone_groups(tones)]


def tones_value(groups: list) -> tuple:
    """What the dispatcher takes: one group flat (as the config writes it), several as groups."""
    return tuple(tuple(t) for t in groups[0]) if len(groups) == 1 else tuple(tuple(tuple(t) for t in g) for g in groups)


def _j(v) -> str:
    return "" if v is None else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _norm(item: dict | None) -> dict:
    """An item without its code and empty parts, for 'is this the default?'."""
    return {k: v for k, v in (item or {}).items() if k != "code" and v not in ("", None, False)}


def _text(v, what: str, n: int, required: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = v.strip()
    if required and not v:
        raise CadError(f"{what}: required")
    if len(v) > n:
        raise CadError(f"{what}: at most {n} characters")
    return v


def _dir(body: dict) -> int:
    d = body.get("dir")
    if d not in (-1, 1) or isinstance(d, bool):
        raise CadError("dir: -1 (up) or 1 (down)")
    return d


def overlay(d: dict, ov: dict) -> dict:
    """The data file's contents with the overrides laid over (unchecked: lists.build() checks it)."""
    d = copy.deepcopy(d)
    gone: dict[str, set] = {}
    for sec in LIST_SECTIONS:
        o = ov.get(sec) or {}
        if not o:
            continue
        have = d.get(sec)
        if have is None and sec == "location_qualifiers":   # a data file without its own: the defaults are its items
            have = [dict(q) for q in lists_mod.DEFAULT_QUALIFIERS]
        items = [x if isinstance(x, dict) else {"code": x} for x in have or []]
        by = {str(x.get("code")): x for x in items}
        order = list(by)
        for code, v in o.items():
            if code.startswith("_") or not isinstance(v, dict):
                continue
            if v.get("deleted"):
                if code in by:
                    order.remove(code)
                    by.pop(code)
                    gone.setdefault(sec, set()).add(code)
                continue
            if code not in by:
                order.append(code)
            keep = {"help": by[code]["help"]} if code in by and by[code].get("help") and "help" not in v else {}   # the definition stays (#105)
            by[code] = {**keep, **v, "code": code}
        if isinstance(o.get("_order"), list):
            want = [c for c in o["_order"] if c in by]
            order = want + [c for c in order if c not in want]
        d[sec] = [by[c] for c in order]
    o = ov.get("type_subtypes") or {}
    if o:                                      # a type or subtype deleted since: its leftovers drop out
        def codes(sec):
            return {str(x.get("code") if isinstance(x, dict) else x) for x in d.get(sec) or []}
        types, subs = codes("event_types"), codes("event_subtypes")
        d["type_subtypes"] = dict(d.get("type_subtypes") or {}, **{t: [x for x in v if x in subs] for t, v in o.items()
                                                                     if t in types and isinstance(v, list)})
    # A deleted type or subtype takes its rules, phrases and default cards with it.
    gt, gs = gone.get("event_types", set()), gone.get("event_subtypes", set())
    if gt or gs:
        def dead(k: str) -> bool:
            t, _, s = k.partition("/")
            return (t in gt) or (s in gs) or (t == "*" and s in gs)
        for m in ("event_class", "spoken"):
            d[m] = {k: v for k, v in (d.get(m) or {}).items() if not dead(k)}
        d["type_subtypes"] = {t: [s for s in v if s not in gs] for t, v in (d.get("type_subtypes") or {}).items() if t not in gt}
        d["run_cards"] = [c for c in d.get("run_cards") or [] if not dead(str(c.get("type", "")))]
    o = ov.get("event_class") or {}
    if o:
        ec = dict(d.get("event_class") or {})
        for k, v in o.items():
            if v is None:
                ec.pop(k, None)
            else:
                ec[k] = v
        d["event_class"] = ec
    o = ov.get("status_flow") or {}
    gone_st = gone.get("unit_statuses", set())
    if o or gone_st:
        fl = dict(d.get("status_flow") or {})
        for k, v in o.items():
            if v is None:
                fl.pop(k, None)
            else:
                fl[k] = v
        d["status_flow"] = {k: [c for c in v if c not in gone_st] for k, v in fl.items() if k not in gone_st}
    o = ov.get("geo_town_order") or {}
    if isinstance(o.get("_list"), list):
        d["geo_town_order"] = list(o["_list"])
    o = ov.get("units") or {}
    if o:
        units = dict(d.get("units") or {})
        boards = copy.deepcopy(o["_boards"]) if isinstance(o.get("_boards"), list) else d.get("unit_boards") or []
        meta = {b.get("id"): lists_mod.board_meta(b) for b in d.get("unit_boards") or [] if isinstance(b, dict)}
        for b in boards:                       # how a board shows follows the data file (a saved layout is its units)
            if isinstance(b, dict) and b.get("id") in meta:
                for k in lists_mod.BOARD_META:
                    b.pop(k, None)
                b.update(meta[b["id"]])
        off = set()
        for code, v in o.items():
            if code.startswith("_") or not isinstance(v, dict):
                continue
            if v.get("deleted"):
                units.pop(code, None)
                off.add(code)
                continue
            was = units.get(code)
            units[code] = {k: x for k, x in v.items() if k != "board"}
            # A unit's statuses from the data file (status_set: the Other board's fire and EMS codes keep the agency set)
            # stay when a saved edit doesn't say: one saved before the key existed, or an older export imported.
            if "status_set" not in v and isinstance(was, dict) and was.get("status_set"):
                units[code]["status_set"] = was["status_set"]
            if v.get("retired"):
                off.add(code)
        for b in boards:
            b["units"] = [c for c in b.get("units") or [] if c not in off]
        d["units"], d["unit_boards"] = units, boards
        # The default cards (seed only) lose a slot naming a unit that is off the boards.
        on = {c for b in boards for c in b["units"] if c}
        cards = []
        for c in d.get("run_cards") or []:
            slots = [s for s in c.get("slots") or [] if not s.get("unit") or s["unit"] in on]
            if slots:
                cards.append(dict(c, slots=slots))
        d["run_cards"] = cards
    o = ov.get("dispatch_wording") or {}
    if o:
        d["dispatch_wording"] = dict(d.get("dispatch_wording") or {}, **o)
    o = ov.get("dispatch_tones") or {}
    if o:
        d["dispatch_tones"] = dict(d.get("dispatch_tones") or {}, **o)
    ag = d.get("agency_status_sets")
    if isinstance(ag, dict) and isinstance(ag.get("sets"), dict):   # a status deleted, a board gone: out of the sets (#219)
        ids = {b.get("id") for b in d.get("unit_boards") or [] if isinstance(b, dict)}
        sets = {k: dict(v, statuses=[c for c in v.get("statuses") or [] if c not in gone_st],
                        labels={c: t for c, t in (v.get("labels") or {}).items() if c not in gone_st},
                        **({"flow": {c: [x for x in n if x not in gone_st] for c, n in v["flow"].items() if c not in gone_st}}
                           if isinstance(v.get("flow"), dict) else {}))
                for k, v in ag["sets"].items() if isinstance(v, dict)}
        d["agency_status_sets"] = dict(ag, sets=sets, boards={b: x for b, x in (ag.get("boards") or {}).items() if b in ids},
                                       **({"third_party_only": [c for c in ag["third_party_only"] if c not in gone_st]}
                                          if isinstance(ag.get("third_party_only"), list) else {}))
    return d


class LastAdmin(CadError):
    """A change that would leave no active admin with a sign-in email (409: nobody could change
    Settings again). Only checked where roles are enforced (the hub with Cloudflare Access)."""


def body_places(body: dict) -> list[str]:
    """A request's places: places [names] or place (one)."""
    raw = body.get("places")
    if raw is None and body.get("place") not in (None, ""):
        raw = [body["place"]]
    if not isinstance(raw, list) or not raw:
        raise CadError("places: one or more place names")
    return tonelist._places({"places": raw}, "")


def _without_imported(ov: dict) -> dict:
    """The overrides without the county's imported place names (section "places", keys "county/..."): the settings
    file never carries them, and importing one never touches them (Common places has its own import)."""
    if "places" not in ov:
        return ov
    places = {k: v for k, v in ov["places"].items() if not commonplaces.is_imported(k, v if isinstance(v, dict) else None)}
    return {**{k: v for k, v in ov.items() if k != "places"}, **({"places": places} if places else {})}


def _is_admin(o: dict) -> bool:
    return o.get("role") == "admin" and bool(o.get("active")) and bool(o.get("email"))


class Editor:
    """The Settings page's backend, for one Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.path = Path(cad.s.lists) if cad.s.lists else lists_mod.DEFAULT_PATH
        self.defaults = lists_mod.read(self.path)
        self.base = cad.lists                                   # the data file alone
        self.config_tones = tone_groups(cad.s.tones)
        self.config_channels = tuple(cad.s.channels)
        self.config_voice = (getattr(cad.s, "voice", ""), getattr(cad.s, "speed", 1.0))
        # The tone list under the "tones" overrides (Settings > Radios > Tones, #115): the app sets it (use_tone_list):
        # the config's codes (data/westchester_tones.csv + [[tones]]), the names ours by config, unknown_tones.jsonl.
        self.base_tones: list = []
        self.base_ours: set[str] = set()
        self.unknown_log: Path | None = None
        self._heard_cache: tuple = ((), [])
        self._sections: dict[str, tuple] = {}        # section() -> (_written() then, its overrides)
        self._usage_cache: dict[str, tuple] = {}     # _page_usage(): section -> (what it counts' marks, when, the counts)

    # -- overrides ------------------------------------------------------------------------------------

    def overrides(self, skip: tuple[str, ...] = ()) -> dict[str, dict]:
        """Every section's overrides; skip: sections left out (the page: "places", thousands of imported names it reads
        through local_places, never here)."""
        out: dict[str, dict] = {}
        where = f" WHERE section NOT IN ({','.join('?' * len(skip))})" if skip else ""
        for r in self.store.q(f"SELECT section, key, value FROM settings_overrides{where} ORDER BY section, key", skip):
            out.setdefault(r["section"], {})[r["key"]] = json.loads(r["value"])
        return out

    def settings_rev(self) -> str:
        """What moves with every change the Settings page shows as a setting: any override written or removed, any
        audited change (every Settings write is audited), a user added or edited. The page reloads at once when it
        moves (settings.js sHook); what moves with the calls and the radio ("used 3×", a unit's status, the audio kept)
        it reads again on its own, slower. The state carries it (Cad.state, built only when the rev moves)."""
        a = self.store.one("SELECT MAX(id) AS id FROM settings_audit")
        o = self.store.one("SELECT COUNT(*) AS n, MAX(updated_at) AS at, TOTAL(LENGTH(value)) AS size FROM settings_overrides")
        u = self.store.one("SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM operators")
        return json.dumps([a["id"] if a else None, *(o.values() if o else ()), *(u.values() if u else ())], separators=(",", ":"))

    def section(self, name: str) -> dict:
        """One section's overrides only (the push topics, read at every push: sizeup/notify.py Pushes; the alarms'
        settings, read at every state push). Read again only when the file may have changed (_written); a copy each
        time, so a caller may change what it gets."""
        ver = self._written()
        got = self._sections.get(name) if ver is not None else None
        if got is None or got[0] != ver:
            got = (ver, {r["key"]: json.loads(r["value"])
                         for r in self.store.q("SELECT key, value FROM settings_overrides WHERE section = ?", (name,))})
            if ver is not None:
                self._sections[name] = got
        return copy.deepcopy(got[1])

    def _written(self) -> tuple | None:
        """What moves with any write to the CAD file: this connection's own (total_changes), another's (PRAGMA
        data_version: the replica follower before a takeover). None inside an open transaction (what it reads may still
        be rolled back): never cached then. The imported place names are thousands of rows in the same table, so a
        section is never read through overrides() where it is asked every second (#446)."""
        db = self.store.db
        with self.store.lock:
            if db.in_transaction:
                return None
            return db.total_changes, db.execute("PRAGMA data_version").fetchone()[0]

    MADE_FIRE_KEYS = {"kind", "response", "title", "spoken", "department"}

    def made_fire(self, units_ov: dict | None = None) -> list[str]:
        """Codes an old edit turned into one of our engines: a saved units edit of only kind engine (response fire or
        none) and a name, title or department, for a code the data file makes another agency's (a board-only code like
        CONED-G, or an agency kind). Before the fix, editing one field of a board-only code saved it so (_units_op).
        Shown in Settings > Units to reset, logged once at start; never rewritten here."""
        o = self.overrides().get("units", {}) if units_ov is None else units_ov
        on = {c for c, _, _ in self.base.board_units()}
        return sorted(c for c, v in o.items() if isinstance(v, dict) and not c.startswith("_") and c in on
                      and v.get("kind") == "engine" and v.get("response", "fire") == "fire"
                      and set(v) <= self.MADE_FIRE_KEYS and self.base.response(c) == "agency")

    def build(self, ov: dict) -> lists_mod.Lists:
        try:
            return lists_mod.build(overlay(self.defaults, ov), "settings", self.path)
        except ListsError as e:
            raise CadError(str(e).removeprefix("settings: ")) from None

    def startup(self) -> None:
        """At start: the saved edits over the data file. If they no longer fit it (the file changed
        under them), the file alone, said in the log: nothing is lost, Settings shows the edits."""
        ov = self.overrides()
        self._use_services(ov)                  # (whatever the lists make of the rest: a source's tag never waits)
        if not ov:
            return
        bad = self.made_fire(ov.get("units") or {})
        if bad:                                 # said once, left as they are: Settings > Units offers Reset on each
            self.cad.log(f"CAD: settings: {len(bad)} other agencies' units saved as our engines by an old edit "
                         f"({', '.join(bad)}): Settings > Units, Reset")
        try:
            L = self.build(ov)
        except CadError as e:
            self.cad.log(f"CAD: settings edits not applied ({e}): using {self.path.name} as it is")
            return
        self._use(L, ov)

    def _use(self, L, ov: dict) -> None:
        self.cad.lists = self.store.lists = L
        d = ov.get("dispatch") or {}
        self.cad.s.tones = tones_value(d.get("tones", self.config_tones))
        self.cad.s.channels = tuple(d.get("channels", self.config_channels))
        if hasattr(self.cad.s, "speed"):
            self.cad.s.voice = d.get("voice", self.config_voice[0])
            self.cad.s.speed = float(d.get("speed", self.config_voice[1]))
        self.cad.eta = eta.Params().with_overrides(d)
        self.cad.weather_speak = bool(d.get("weather_speak", False))     # sizeup/weather.py (#132): off by default
        a = ov.get(ATC_SECTION) or {}
        self.cad.atc_identify = bool(a.get("identify", True))           # sizeup/atc_talkers.py (#252): on by default
        self.cad.atc_map = bool(a.get("map", True))
        self.cad.atc_vocab_refresh = bool(a.get("vocab", True))
        was = getattr(self.cad, "hydrants_assume_c", True)        # (not set yet: the default, sizeup/cad/hydrants.py)
        self.cad.hydrants_assume_c = bool((ov.get(HYD_SECTION) or {}).get("assume_c", True))   # #840: on by default
        if was != self.cad.hydrants_assume_c and getattr(self.cad, "hydrants", None) is not None:
            self.cad.hydrants.display_changed()          # the layer drawn again, every page
        self.cad.dongle_auto_replace = (ov.get(SWAP_SECTION) or {}).get("auto_critical") is True   # off by default
        self.cad.map_display = mapview.display_zooms(ov.get(MAPD_SECTION))     # the maps' rigs by zoom (#376)
        self.cad.premise_visible = dict(ov.get(PREMISE_SECTION) or {})     # premise notes by category (#403)
        self.cad.em_prominent = em_prominent(ov.get(EM_SECTION))          # the Radio page's loud emergencies
        # each FM / airband channel's Main switch (Our calls): only the choices that differ from its default
        self.cad.fm_main_over = {str(k): bool(v) for k, v in (ov.get(FM_MAIN_SECTION) or {}).items() if isinstance(v, bool)}
        self.cad.radio_folders = radiofolders.stored(ov)                  # the Radio page's folders (#607)
        self.cad.radio_views = radioviews.stored(ov)                      # ... and its views (None: the coded ones)
        # one name per source, every label (sizeup/radionames.py): the folders' names and the chips' shorts
        radionames.use(names=(self.cad.radio_folders or {}).get("names") or {}, shorts=ov.get(SHORT_SECTION) or {})
        self._use_services(ov)
        self.cad._state = None
        if getattr(self.cad, "intel", None) is not None:
            self.cad.intel.sources_changed()      # a source's service, the intel topic: intel's push sources again

    def _use_services(self, ov: dict) -> None:
        """The admins' service per source: the CAD's copy (the pages) and the process's (every row tagged from now)."""
        self.cad.radio_services = sourcekeys.clean_services(ov.get(SVC_SECTION))
        sourcekeys.set_service_overrides(self.cad.radio_services)

    def _commit(self, changes: list[tuple[str, str, object]], operator: str, audit: list[tuple[str, str, object, object]],
                check=None, extra_sql=None) -> dict:
        """Write overrides (value or REMOVE) after the whole result checks out; audit; apply now."""
        ov = self.overrides()
        trial = copy.deepcopy(ov)
        for sec, key, val in changes:
            if val is REMOVE:
                trial.get(sec, {}).pop(key, None)
            else:
                trial.setdefault(sec, {})[key] = val
        L = self.build(trial)
        if check:
            check(L)
        now = self.store.clock()
        # a source's service chosen or put back (here, or by an import): its recent rows to re-tag, marked with it
        moved = services_moved(ov, trial)
        with self.store.tx() as db:
            mark_retag(db, moved, now, operator)
            for sec, key, val in changes:
                if val is REMOVE:
                    db.execute("DELETE FROM settings_overrides WHERE section = ? AND key = ?", (sec, key))
                else:
                    db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT(section, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                               "operator = excluded.operator", (sec, key, json.dumps(val), now, operator))
            for section, key, old, new in audit:
                self._audit(db, now, operator, section, key, old, new)
            if extra_sql:
                extra_sql(db)
        self._use(L, trial)
        self.store.sync_units()          # new / retired / moved units, statuses: on the boards now
        self.store.rev += 1
        if moved:
            self.cad.retag_services()                   # (off this thread)
        return {"ok": True}

    @staticmethod
    def _audit(db, at: float, operator: str, section: str, key: str, old, new) -> None:
        db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                   (at, operator or "", section, key, _j(old), _j(new)))

    def audit_log(self, section: str = "", limit: int = 500) -> list[dict]:
        if section:
            return self.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY at DESC, id DESC LIMIT ?",
                                (section, limit))
        return self.store.q("SELECT * FROM settings_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,))

    def audit_card(self, operator: str, old: dict | None, new: dict | None) -> None:
        def show(c):
            return {"type": c["type"], "spoken": c["spoken"], "slots": c["slots"]} if c else None
        key = (new or old or {}).get("type", "")
        with self.store.tx() as db:
            if old and new and old["type"] != new["type"]:
                key = f"{old['type']} -> {new['type']}"
            self._audit(db, self.store.clock(), operator, "run cards", key, show(old), show(new))

    # -- what is used where ---------------------------------------------------------------------------

    def usage(self, sec: str) -> dict[str, int]:
        """code -> how many records use it (events, run cards, unit history): can't be deleted."""
        q, out = self.store.q, {}

        def add(rows):
            for r in rows:
                if r["k"]:
                    out[r["k"]] = out.get(r["k"], 0) + r["n"]
        if sec in ("event_types", "event_subtypes", "dispositions", "received_via"):
            col = {"event_types": "type", "event_subtypes": "subtype", "dispositions": "disposition",
                   "received_via": "received_via"}[sec]
            add(q(f"SELECT {col} AS k, COUNT(*) AS n FROM incidents GROUP BY {col}"))
            if sec in ("event_types", "event_subtypes"):
                for c in q("SELECT type FROM run_cards"):
                    t, _, s = c["type"].partition("/")
                    k = t if sec == "event_types" else s
                    if k:
                        out[k] = out.get(k, 0) + 1
        elif sec == "location_qualifiers":             # events and hydrants that have one
            add(q("SELECT location_qualifier AS k, COUNT(*) AS n FROM incidents GROUP BY location_qualifier"))
            if q("SELECT 1 FROM sqlite_master WHERE name = 'hydrants'"):
                add(q("SELECT location_qualifier AS k, COUNT(*) AS n FROM hydrants GROUP BY location_qualifier"))
        elif sec == "unit_statuses":
            add(q("SELECT status AS k, COUNT(*) AS n FROM units GROUP BY status"))
            add(q("SELECT old_status AS k, COUNT(*) AS n FROM unit_history GROUP BY old_status"))
            add(q("SELECT new_status AS k, COUNT(*) AS n FROM unit_history GROUP BY new_status"))
        elif sec == "units":
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM assignments a JOIN units u ON u.id = a.unit_id GROUP BY u.name"))
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM unit_history h JOIN units u ON u.id = h.unit_id GROUP BY u.name"))
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM events e JOIN units u ON u.id = e.unit_id GROUP BY u.name"))
        return out

    PAGE_USAGE_S = 30.0          # the page's "used N×" at most this old (a delete still asks usage() itself)

    def _page_usage(self, sec: str) -> dict[str, int]:
        """usage() for the page: kept until a record is added to what it counts, at most PAGE_USAGE_S (its GROUP BYs
        over every incident, event and unit history row were most of a page read; the counts only say "in use")."""
        mark = tuple(self.store.one("SELECT (SELECT MAX(id) FROM incidents) AS i, (SELECT MAX(id) FROM events) AS e, "
                                    "(SELECT MAX(id) FROM unit_history) AS h, (SELECT COUNT(*) FROM assignments) AS a, "
                                    "(SELECT COUNT(*) FROM run_cards) AS c").values())
        now = time.monotonic()
        got = self._usage_cache.get(sec)
        if got is None or got[0] != mark or now - got[1] > self.PAGE_USAGE_S:
            got = self._usage_cache[sec] = (mark, now, self.usage(sec))
        return dict(got[2])

    def _used_why(self, sec: str, code: str) -> str:
        q = self.store.q
        parts = []
        if sec in ("event_types", "event_subtypes", "dispositions", "received_via"):
            col = {"event_types": "type", "event_subtypes": "subtype", "dispositions": "disposition",
                   "received_via": "received_via"}[sec]
            n = q(f"SELECT COUNT(*) AS n FROM incidents WHERE {col} = ?", (code,))[0]["n"]
            if n:
                parts.append(f"{n} event{'s' if n != 1 else ''}")
            if sec in ("event_types", "event_subtypes"):
                k = [c["type"] for c in q("SELECT type FROM run_cards")
                     if (c["type"].partition("/")[0] if sec == "event_types" else c["type"].partition("/")[2]) == code]
                if k:
                    parts.append(f"run card{'s' if len(k) != 1 else ''} {', '.join(k)}")
        elif self.usage(sec).get(code):
            parts.append(f"{self.usage(sec)[code]} record{'s' if self.usage(sec)[code] != 1 else ''}")
        return " and ".join(parts)

    def _cards_naming(self, code: str) -> list[str]:
        return sorted({c["type"] for c in runcards.cards(self.store) if any(s.get("unit") == code for s in c["slots"])})

    # -- the page ---------------------------------------------------------------------------------------

    def page(self) -> dict:
        L, ov = self.cad.lists, self.overrides(skip=("places",))      # (the place names: _place_names_page)
        out: dict = {"rev": self.store.rev, "lists": {}, "titles": LIST_TITLES, "roles": list(ROLES),
                     "audit_sections": list(AUDIT_SECTIONS)}
        live = self.store.units()
        for sec in LIST_SECTIONS:
            base = {x["code"]: x for x in getattr(self.base, sec)}
            o, used = ov.get(sec, {}), self._page_usage(sec)
            rows = [dict(x, state="added" if x["code"] not in base else "edited" if x["code"] in o else "default",
                         used=used.get(x["code"], 0), default=base.get(x["code"])) for x in getattr(L, sec)]
            rows += [dict(base[c], state="deleted", used=0, default=base[c]) for c, v in o.items()
                     if isinstance(v, dict) and v.get("deleted") and c in base]
            out["lists"][sec] = {"items": rows, "ordered": "_order" in o, "edited": bool(o)}
        now_in = {}
        for u in live:
            now_in[u["status"]] = now_in.get(u["status"], 0) + 1
        for r in out["lists"]["unit_statuses"]["items"]:
            r["now"] = now_in.get(r["code"], 0)
        o = ov.get("event_class", {})
        out["event_class"] = {"rules": [{"key": k, "value": v, "state": "edited" if k in o else "default",
                                         "default": self.base.event_class.get(k)} for k, v in L.event_class.items()]
                              + [{"key": k, "value": None, "state": "deleted", "default": self.base.event_class.get(k)}
                                 for k, v in o.items() if v is None and k in self.base.event_class],
                              "edited": bool(o)}
        o = ov.get("status_flow", {})
        out["status_flow"] = {"rows": [{"key": k, "next": list(v), "state": "edited" if k in o else "default",
                                        "default": list(self.base.status_flow.get(k, ())) or None}
                                       for k, v in L.status_flow.items()]
                              + [{"key": k, "next": [], "state": "deleted", "default": list(self.base.status_flow[k])}
                                 for k, v in o.items() if v is None and k in self.base.status_flow],
                              "edited": bool(o)}
        out["geo_town_order"] = {"towns": list(L.geo_town_order), "default": list(self.base.geo_town_order),
                                 "edited": "_list" in ov.get("geo_town_order", {})}
        out["place_names"] = self._place_names_page()
        out["local_addresses"] = self._addresses_page()
        out["units"] = self._unit_rows(L, ov, live)
        out["unit_kinds"] = [dict(k) for k in L.unit_kinds]
        out["boards"] = [{"id": b["id"], "title": b["title"], **lists_mod.board_meta(b)} for b in L.unit_boards]
        out["type_subtypes"] = self._map_page(L, ov)
        out["feed_types"] = self.feed_types_page(ov)
        out["dispatch"] = self._dispatch_page(L, ov)
        out["reminders"] = reminders.page(ov.get(REM_SECTION))
        out["operators"] = self.operators()
        out["system"] = self._system(ov, places=len(self._places_cached()[2]))      # (ov is without them)
        out["tones"] = self.tones_page()
        out["camera_wall"] = self.wall_page()
        out["atc"] = self.atc_page(ov)
        out["hub_errors"] = self.errors_page(ov)
        out["dongle_swap"] = self.swap_page(ov)
        out["map_display"] = self.mapd_page(ov)
        out["premise_visibility"] = self.premise_page(ov)
        out["radio_emergencies"] = self.em_page(ov)
        out["signal_metrics"] = self.signal_page(ov)
        out["alert_mutes"] = self.mutes_page(ov)
        out["audio_retention"] = self.retention_page(ov)
        out["station_logs"] = self.logs_page(ov)
        return out

    def logs_page(self, ov: dict | None = None) -> dict:
        """Settings > Station > Logs: each setting, its default and whether it is edited."""
        e = (self.section(LOGS_SECTION) if ov is None else (ov.get(LOGS_SECTION) or {}))
        c = cwlogs.config(e)
        return {k: {"label": cwlogs.LABELS[k], "value": c[k], "default": d, "edited": k in e}
                for k, d in cwlogs.DEFAULTS.items()} | {"levels": list(cwlogs.logscrub.LEVELS),
                                                         "retention_choices": list(cwlogs.RETENTION_DAYS)}

    def _logs_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, value} | reset {key}: admins."""
        key = body.get("key")
        if key not in cwlogs.DEFAULTS:
            raise CadError(f"key: one of {', '.join(cwlogs.DEFAULTS)}")
        d = cwlogs.DEFAULTS[key]
        cur = cwlogs.config(self.section(LOGS_SECTION))[key]
        if action == "reset":
            v = d
        elif action == "set":
            try:
                v = cwlogs.check_value(key, body.get("value"))
            except ValueError as e:
                raise CadError(str(e)) from None
        else:
            raise CadError("action: set or reset")
        shown = (lambda x: ("on" if x else "off") if key in ("on", "adapter") else ", ".join(x) or "none" if isinstance(x, list) else str(x))
        out = self._commit([(LOGS_SECTION, key, REMOVE if v == d and type(v) is type(d) else v)], op,
                           [("system", f"{LOGS_SECTION}/{key}", shown(cur), shown(v))])
        sl = getattr(self.cad, "station_logs", None)
        if key == "retention_days" and sl is not None:
            sl.checked = 0.0                 # applied at the next look (the Logs view's status, or the next batch)
        return {**out, "station_logs": self.logs_page()}

    def _context_channel(self, channel: str) -> bool:
        """This channel's newest transmission is another P25 system's (#409; one indexed row: (channel, start))."""
        if not channel:
            return False
        try:
            r = self.store.q("SELECT system FROM radio_traffic WHERE channel = ? ORDER BY start DESC LIMIT 1", (channel,))
        except Exception:  # noqa: BLE001 (no radio log, or one without the system column)
            return False
        return bool(r) and sourcekeys.is_context(r[0])

    def retention_page(self, ov: dict | None = None) -> dict | None:
        """Settings > System > Audio retention (#414): the defaults and every channel / talkgroup with clips or days of
        its own: clips, bytes, the audio's days and the transcripts' days in effect (talkgroup > channel > default;
        a transcript never shorter than its audio), edited or not; what is kept and what it costs. None where there is
        no audio database (not the hub)."""
        blobs = getattr(self.cad, "audio_blobs", None)
        if blobs is None or not hasattr(blobs, "channels"):
            return None
        o = self.overrides() if ov is None else ov
        r = o.get(RET_SECTION) or {}
        tr = o.get(TRS_SECTION) or {}
        base = float(getattr(self.cad, "audio_keep_days", 0.5))
        pol = audiostore.Retention(r, base)
        tpol = audiostore.Retention(tr, audiostore.TRANSCRIPT_DEFAULT_DAYS)
        rows: dict[str, dict] = {}
        live_n = live_b = 0
        hide = None if sourcekeys.may_see_context() else self._context_channel
        for c in blobs.channels():
            live_n += c["clips"]
            live_b += c["bytes"]
            key = audiostore.retention_key(c["channel"], c["talkgroup"])
            if not key or (hide is not None and hide(c["channel"])):
                continue                    # another P25 system's channel: its clips only to a signed-in person (#419)
            x = rows.setdefault(key, {"key": key, "label": c["channel"] or key, "channel": c["channel"],
                                      "talkgroup": c["talkgroup"], "clips": 0, "bytes": 0})
            x["clips"] += c["clips"]
            x["bytes"] += c["bytes"]
        for key in list(r) + list(tr):
            if key not in (audiostore.DEFAULT_KEY, audiostore.FORMAT_KEY) and key not in rows:
                m = audiostore.TG_KEY.fullmatch(key)
                rows[key] = {"key": key, "label": key, "channel": "" if m else key,
                             "talkgroup": int(m.group(1)) if m else None, "clips": 0, "bytes": 0}
        for x in rows.values():
            # the days the prunes use for these clips and their transcripts, whether this row is edited or not
            x["days"] = pol.keep_days(x["channel"], x["talkgroup"])
            x["t_days"] = max(x["days"], tpol.keep_days(x["channel"], x["talkgroup"]))
            x["edited"], x["t_edited"] = x["key"] in r, x["key"] in tr
        lo, hi = audiostore.RETENTION_DAYS
        tlo, thi = audiostore.TRANSCRIPT_DAYS
        gb = 1e9
        try:
            tn = int(self.store.q("SELECT COUNT(*) AS n FROM radio_traffic")[0]["n"])
        except Exception:  # noqa: BLE001
            tn = 0
        try:
            inc = self.store.q("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM attachments WHERE kind = 'radio' "
                               "AND status = 'stored'")[0]
            inc_n, inc_b = int(inc["n"]), int(inc["b"])
        except Exception:  # noqa: BLE001 (no attachments table)
            inc_n = inc_b = 0
        cost = audiostore.COST_GB_MONTH
        return {"default": {"value": pol.default, "default": base, "edited": audiostore.DEFAULT_KEY in r,
                            "t_value": max(pol.default, tpol.default), "t_default": audiostore.TRANSCRIPT_DEFAULT_DAYS,
                            "t_edited": audiostore.DEFAULT_KEY in tr},
                "rows": sorted(rows.values(), key=lambda x: (-x["bytes"], x["key"])), "min": lo, "max": hi,
                "t_min": tlo, "t_max": thi,
                "event_days": float(getattr(self.cad, "audio_event_days", 0) or 0),
                "format": {"value": r.get(audiostore.FORMAT_KEY, audiostore.FORMATS[0]), "choices": list(audiostore.FORMATS),
                           "edited": audiostore.FORMAT_KEY in r},
                # what is kept now and its S3 Standard storage a month (us-east-1; PUTs not included: ~$1 a month)
                "totals": {"audio": {"clips": live_n, "gb": round(live_b / gb, 3), "usd_month": round(live_b / gb * cost, 2)},
                           "transcripts": {"rows": tn},
                           "incident": {"clips": inc_n, "gb": round(inc_b / gb, 3), "usd_month": round(inc_b / gb * cost, 2)},
                           "usd_gb_month": cost}}

    @staticmethod
    def _ret_key(key, section: str = RET_SECTION) -> str:
        """The key as stored: tg:0123 is tg:123 (one talkgroup, one key). _format: the audio's section only."""
        m = audiostore.TG_KEY.fullmatch(key) if isinstance(key, str) else None
        if m:
            return audiostore.tg_key(int(m.group(1)))
        specials = (audiostore.DEFAULT_KEY, audiostore.FORMAT_KEY) if section == RET_SECTION else (audiostore.DEFAULT_KEY,)
        if not isinstance(key, str) or not (key in specials
                                            or (audiostore.CHANNEL_KEY.fullmatch(key) and not key.startswith(("_", "tg:"))
                                                and key == key.strip())):
            raise CadError(f"key: {', '.join(specials)}, a channel's name or tg:<talkgroup>")
        return key

    @staticmethod
    def _ret_value(key: str, v, section: str = RET_SECTION) -> float | str:
        if key == audiostore.FORMAT_KEY:       # how new clips are stored: only "wav" for now (FLAC / Opus would go here)
            if v not in audiostore.FORMATS:
                raise CadError(f"{key}: {', '.join(audiostore.FORMATS)}")
            return v
        lo, hi = audiostore.RETENTION_DAYS if section == RET_SECTION else audiostore.TRANSCRIPT_DAYS
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise CadError(f"{key}: days from {lo:g} to {hi:g}")
        return round(float(v), 2)

    def _retention_op(self, action: str, body: dict, op: str, section: str = RET_SECTION) -> dict:
        """set {key: _default | a channel's label | tg:<n> (| _format: the audio's), value} | reset {key}: admins, for
        the audio (audio_retention) or the transcripts and metadata (transcript_retention; never shorter than the
        audio, whatever is set). Applies to what is already kept at the next prune."""
        key = self._ret_key(body.get("key"), section)
        r = self.overrides().get(section) or {}
        cur = r.get(key)
        if action == "reset":
            v = REMOVE
        elif action == "set":
            v = self._ret_value(key, body.get("value"), section)
        else:
            raise CadError("action: set or reset")
        if (v is REMOVE and cur is None) or v == cur:      # nothing changes: nothing written, nothing audited
            return {"ok": True, "audio_retention": self.retention_page()}
        shown = (lambda x: "default" if x is None or x is REMOVE else str(x) if isinstance(x, str) else f"{x:g} d")
        out = self._commit([(section, key, v)], op, [("system", f"{section}/{key}", shown(cur), shown(v))])
        return {**out, "audio_retention": self.retention_page()}

    def mutes_page(self, ov: dict | None = None) -> dict:
        """Settings > System > Push notifications > Mute (#340): each health alert kind, muted or not, until when."""
        now = self.store.clock()
        on = health_mod.active_mutes((self.section(MUTE_SECTION) if ov is None else (ov.get(MUTE_SECTION) or {})), now)
        return {"kinds": [{"key": k, "label": lab, "muted": k in on, "until": on.get(k),
                           "timed_only": k in health_mod.MUTE_TIMED_ONLY,
                           **({"hint": health_mod.MUTE_HINTS[k]} if k in health_mod.MUTE_HINTS else {})}
                          for k, lab in health_mod.MUTE_KINDS.items()], "hours_max": MUTE_HOURS_MAX}

    @staticmethod
    def _mute_value(key: str, v, now: float) -> dict | None:
        """A mute as stored ({"until": epoch | None}); None for one already over (dropped)."""
        if not isinstance(v, dict) or set(v) - {"until"}:
            raise CadError('{"until": a time or null}')
        u = v.get("until")
        if u is None:
            if key in health_mod.MUTE_TIMED_ONLY:
                raise CadError(f"until: a time ({health_mod.MUTE_KINDS[key]} is muted for a time only)")
            return {"until": None}
        if isinstance(u, bool) or not isinstance(u, (int, float)) or not math.isfinite(u) \
                or not 0 <= u <= now + MUTE_HOURS_MAX * 3600.0:
            raise CadError(f"until: a time (epoch seconds) within {MUTE_HOURS_MAX // 24} days, or null (until unmuted)")
        return {"until": float(u)} if u > now else None

    def _mutes_op(self, action: str, body: dict, op: str) -> dict:
        """mute {key, hours?: 1..MUTE_HOURS_MAX (none: until unmuted)} | unmute {key}: admins (sizeup/cad/api.py)."""
        key = body.get("key")
        if key not in health_mod.MUTE_KINDS:
            raise CadError(f"key: one of {', '.join(health_mod.MUTE_KINDS)}")
        now = self.store.clock()
        cur = (self.overrides().get(MUTE_SECTION) or {}).get(key)
        def shown(v) -> str:
            if not isinstance(v, dict) or key not in health_mod.active_mutes({key: v}, now):
                return "pushed"
            return "muted" + (f" until {datetime.fromtimestamp(v['until'], TZ).strftime('%Y-%m-%d %H:%M')}"
                              if v.get("until") is not None else "")
        if action == "mute":
            h = body.get("hours")
            if h is None and key in health_mod.MUTE_TIMED_ONLY:
                raise CadError(f"hours: 1 to {MUTE_HOURS_MAX} ({health_mod.MUTE_KINDS[key]} is muted for a time only)")
            if h is None:
                new = {"until": None}
            elif isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 < h <= MUTE_HOURS_MAX:
                raise CadError(f"hours: 1 to {MUTE_HOURS_MAX}, or none (until unmuted)")
            else:
                new = {"until": round(now + h * 3600.0)}
        elif action == "unmute":
            if cur is None:
                return {"ok": True, "alert_mutes": self.mutes_page()}
            if shown(cur) == "pushed":                   # over already: tidied away, nothing to audit
                out = self._commit([(MUTE_SECTION, key, REMOVE)], op, [])
                return {**out, "alert_mutes": self.mutes_page()}
            new = REMOVE
        else:
            raise CadError("action: mute or unmute")
        out = self._commit([(MUTE_SECTION, key, new)], op,
                           [("system", f"{MUTE_SECTION}/{key}", shown(cur), shown(None if new is REMOVE else new))])
        return {**out, "alert_mutes": self.mutes_page()}

    def errors_page(self, ov: dict | None = None) -> dict:
        """Settings > System > Hub error alerts (#285): the switch and the two thresholds, each with its default."""
        e = (self.section(ERR_SECTION) if ov is None else (ov.get(ERR_SECTION) or {}))
        return {k: {"label": errwatch.LABELS[k], "value": e.get(k, d), "default": d, "edited": k in e,
                    **({"min": errwatch.LIMITS[k][0], "max": errwatch.LIMITS[k][1]} if k in errwatch.LIMITS else {})}
                for k, d in errwatch.DEFAULTS.items()} | {"window_min": errwatch.WINDOW_S / 60,
                                                         "repeat_min": errwatch.REPEAT_S / 60, "quiet_min": errwatch.QUIET_S / 60}

    @staticmethod
    def _errors_value(key, v):
        if key == "on":
            if not isinstance(v, bool):
                raise CadError("on: true or false")
            return v
        lo, hi = errwatch.LIMITS[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not lo <= v <= hi:
            raise CadError(f"{key}: a whole number from {lo} to {hi}")
        return int(v)

    def _errors_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: on | per_signature | total, value} | reset {key}: admins."""
        key = body.get("key")
        if key not in errwatch.DEFAULTS:
            raise CadError("key: on, per_signature or total")
        d = errwatch.DEFAULTS[key]
        cur = (self.overrides().get(ERR_SECTION) or {}).get(key, d)
        if action == "reset":
            v = d
        elif action == "set":
            v = self._errors_value(key, body.get("value"))
        else:
            raise CadError("action: set or reset")
        shown = (lambda x: ("on" if x else "off") if key in ("on", "adapter") else str(x))
        out = self._commit([(ERR_SECTION, key, REMOVE if v == d and type(v) is type(d) else v)], op,
                           [("system", f"{ERR_SECTION}/{key}", shown(cur), shown(v))])
        return {**out, "hub_errors": self.errors_page()}

    def signal_page(self, ov: dict | None = None) -> dict:
        """Settings > Radios > Signal (#606): the suggestion's margin and the shift thresholds, each with its default."""
        e = (self.section(SIGNAL_SECTION) if ov is None else (ov.get(SIGNAL_SECTION) or {}))
        cur = metricsinsight.settings(e)
        return {k: {"label": metricsinsight.LABELS[k], "value": cur[k], "default": d, "edited": k in e,
                    "min": metricsinsight.LIMITS[k][0], "max": metricsinsight.LIMITS[k][1]}
                for k, d in metricsinsight.DEFAULTS.items()}

    @staticmethod
    def _signal_value(key, v) -> float:
        lo, hi = metricsinsight.LIMITS[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise CadError(f"{key}: {lo:g} to {hi:g}")
        return round(float(v), 2)

    def _signal_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, value} | reset {key}: admins (sizeup/cad/api.py), audited as Radios."""
        key = body.get("key")
        if key not in metricsinsight.DEFAULTS:
            raise CadError(f"key: one of {', '.join(metricsinsight.DEFAULTS)}")
        d = metricsinsight.DEFAULTS[key]
        cur = metricsinsight.settings(self.section(SIGNAL_SECTION))[key]
        if action == "reset":
            v = d
        elif action == "set":
            v = self._signal_value(key, body.get("value"))
        else:
            raise CadError("action: set or reset")
        stored = self.section(SIGNAL_SECTION)
        if (v == d and key not in stored) or (key in stored and stored[key] == v):
            return {"ok": True, "signal_metrics": self.signal_page()}     # nothing changes: nothing to audit
        out = self._commit([(SIGNAL_SECTION, key, REMOVE if v == d else v)], op,
                           [("radios", f"{SIGNAL_SECTION}/{key}", f"{cur:g}", f"{v:g}")])
        return {**out, "signal_metrics": self.signal_page()}

    def mapd_page(self, ov: dict | None = None) -> dict:
        """Settings > System > Map display (#376): each zoom, its default and whether it is edited."""
        e = (self.section(MAPD_SECTION) if ov is None else (ov.get(MAPD_SECTION) or {}))
        z = mapview.display_zooms(e)
        return {k: {"label": mapview.DISPLAY_LABELS[k], "value": z[k], "default": d, "edited": k in e,
                    "min": mapview.display_limits(k)[0], "max": mapview.display_limits(k)[1]}
                for k, d in mapview.DISPLAY_DEFAULTS.items()}

    def premise_page(self, ov: dict | None = None) -> dict:
        """Settings > System > Address notes (#403): each category, who may see it (everyone | dispatcher | admin)."""
        e = (self.section(PREMISE_SECTION) if ov is None else (ov.get(PREMISE_SECTION) or {}))
        return {k: {"label": lab, "value": e.get(k, "everyone"), "edited": k in e, "choices": list(PREMISE_LEVELS)}
                for k, lab in _visibility_keys().items()}

    def _premise_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: a category, value: everyone | dispatcher | admin} | reset {key}: admins (who may see that category's
        premise notes: the state push carries only what everyone may see)."""
        key = body.get("key")
        if key not in _visibility_keys():
            raise CadError(f"key: {', '.join(_visibility_keys())}")
        cur = (self.overrides().get(PREMISE_SECTION) or {}).get(key, "everyone")
        if action == "reset":
            v = "everyone"
        elif action == "set":
            v = body.get("value")
            if v not in PREMISE_LEVELS:
                raise CadError(f"value: {', '.join(PREMISE_LEVELS)}")
        else:
            raise CadError("action: set or reset")
        if v == cur:                                    # nothing changes: nothing written, nothing audited
            return {"ok": True, "premise_visibility": self.premise_page()}
        out = self._commit([(PREMISE_SECTION, key, REMOVE if v == "everyone" else v)], op,
                           [("system", f"{PREMISE_SECTION}/{key}", cur, v)])
        return {**out, "premise_visibility": self.premise_page()}

    def em_page(self, ov: dict | None = None) -> dict:
        """Settings > Radios > Emergencies: the services whose emergencies (not ours) stand out on the Radio page."""
        e = (self.section(EM_SECTION) if ov is None else (ov.get(EM_SECTION) or {}))
        return {"prominent": em_prominent(e), "default": list(EM_DEFAULT), "edited": "prominent" in e,
                "choices": list(sourcekeys.SERVICES)}

    def _em_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: prominent, value: [services]} | reset {key: prominent}: admins, audited as Radios."""
        if body.get("key") != "prominent":
            raise CadError("key: prominent")
        cur = em_prominent(self.overrides().get(EM_SECTION))
        if action == "reset":
            v = list(EM_DEFAULT)
        elif action == "set":
            v = em_value(body.get("value"))
        else:
            raise CadError("action: set or reset")
        if v == cur:                                    # nothing changes: nothing written, nothing audited
            return {"ok": True, "radio_emergencies": self.em_page()}
        out = self._commit([(EM_SECTION, "prominent", REMOVE if v == list(EM_DEFAULT) else v)], op,
                           [("radios", f"{EM_SECTION}/prominent", ", ".join(cur) or "none", ", ".join(v) or "none")])
        return {**out, "radio_emergencies": self.em_page()}

    def _fm_main_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: an FM / airband channel's label, main: true | false}: its Main switch (Our calls), admins, audited as
        Radios (radio_main/<label>, Main -> Other). Its default (on; an airband channel's off) is kept as no choice."""
        if action != "set":
            raise CadError("action: set")
        key, main = body.get("key"), body.get("main")
        kinds = self.cad.fm_kinds()
        if not isinstance(key, str) or key not in kinds:
            raise CadError("key: an FM or airband channel's label")
        if not isinstance(main, bool):
            raise CadError("main: true or false")
        cur = self.cad.fm_main(key)
        if main == cur:                                 # nothing changes: nothing written, nothing audited
            return {"ok": True, "main": cur}
        default = self.cad.fm_main_default(key)
        out = self._commit([(FM_MAIN_SECTION, key, REMOVE if main == default else main)], op,
                           [("radios", f"{FM_MAIN_SECTION}/{key}", "Main" if cur else "Other", "Main" if main else "Other")])
        return {**out, "main": main}

    def services_page(self, cur: dict | None = None) -> dict:
        """{source key: service} the admins chose, as this reader may know of them (another system's: signed in only)."""
        cur = self.cad.radio_services if cur is None else cur
        sees = self.cad.comms._sees_context()
        return {k: v for k, v in cur.items() if sees or not radiofolders.context_key(k)}

    def _services_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, service} | set_many {services: {key: service}}: a source's service (fire | police | ems | transit |
        air | other), over its talkgroups file's column and its group's words; "automatic" (or blank): none chosen.
        Admins, audited as Radios (radio_services/<key>, old -> new); the source's rows of the last RETAG_DAYS re-tagged
        off this thread, the count audited when done (Cad.retag_services)."""
        if action == "set":
            want = {body.get("key"): body.get("service")}
        elif action == "set_many":
            want = body.get("services")
            if not isinstance(want, dict) or not want:
                raise CadError("services: {source key: service}")
            if len(want) > radiofolders.MAX_ITEMS:
                raise CadError(f"services: at most {radiofolders.MAX_ITEMS}")
        else:
            raise CadError("action: set or set_many")
        cur = sourcekeys.clean_services(self.section(SVC_SECTION))
        new = dict(cur)
        sees = self.cad.comms._sees_context()
        for raw, svc in want.items():
            if not radiofolders.is_key(raw):
                raise CadError("key: a source key (fm:<label>, air:<label>, p25:<system>:<talkgroup>)")
            key = sourcekeys.service_key(raw)
            if radiofolders.context_key(key) and not sees:
                raise CadError("key: another system's talkgroup (sign in to change it)")
            svc = sourcekeys.AUTOMATIC if svc in (None, "") else svc
            if svc != sourcekeys.AUTOMATIC and svc not in sourcekeys.SERVICES:
                raise CadError(f"service: {sourcekeys.AUTOMATIC} or one of {', '.join(sourcekeys.SERVICES)}")
            if svc == sourcekeys.AUTOMATIC:
                new.pop(key, None)
            else:
                new[key] = svc
        moved = sorted(k for k in set(cur) | set(new) if cur.get(k) != new.get(k))
        if not moved:                                   # nothing changes: nothing written, nothing audited
            return {"ok": True, "radio_services": self.services_page()}
        out = self._commit([(SVC_SECTION, k, new[k] if k in new else REMOVE) for k in moved], op,
                           [("radios", f"{SVC_SECTION}/{k}", cur.get(k, sourcekeys.AUTOMATIC), new.get(k, sourcekeys.AUTOMATIC))
                            for k in moved])
        return {**out, "radio_services": self.services_page()}

    def _folders_op(self, action: str, body: dict, op: str) -> dict:
        """set {folders: [{id, name, section, items}], names?: {key: name}, base?} | name {key, name, base?} | reset
        {base?}: the Radio page's folders and channel names (#607; name: one channel's alone, Settings > Radios >
        Channels; blank: its own again), admins,
        audited as Radios. base: the rev the edit started from (radiofolders.rev; "" the default): someone else saved
        meanwhile -> refused, nothing written."""
        if action == "short":
            return self._short_op(body, op)
        cur = radiofolders.stored(self.overrides())
        base = body.get("base")
        if base is not None and base != radiofolders.rev(cur):
            raise CadError("the folders were changed meanwhile: reload them and edit again")
        audit_key, old_s = f"{FOLDERS_SECTION}/{radiofolders.KEY}", None
        if action == "reset":
            v = None
        elif action == "set":
            v = radiofolders.clean({"folders": body.get("folders"), "names": body.get("names")})
            if not self.cad.comms._sees_context():
                v = radiofolders.merge_hidden(v, cur)
        elif action == "name":
            # one channel's or talkgroup's name (Settings > Radios > Channels): the folders as they are; display only
            key = radiofolders.name_key(body.get("key"))
            if radiofolders.context_key(key) and not self.cad.comms._sees_context():
                raise CadError("key: another system's talkgroup (sign in to name it)")
            v = radiofolders.named(cur, key, body.get("name"))
            audit_key = f"{FOLDERS_SECTION}/names/{key}"
            old_s = ((cur or {}).get("names") or {}).get(key) or "default"
        else:
            raise CadError("action: set, name, short or reset")
        if v == cur:                                    # nothing changes: nothing written, nothing audited
            return {"ok": True, "radio_folders": radiofolders.for_viewer(cur, sees_context=self.cad.comms._sees_context(),
                                                       hidden_labels=self.cad.context_labels())}
        out = self._commit([(FOLDERS_SECTION, radiofolders.KEY, REMOVE if v is None else v)], op,
                           [("radios", audit_key, radiofolders.summary(cur), radiofolders.summary(v)) if old_s is None
                            else ("radios", audit_key, old_s, ((v or {}).get("names") or {}).get(key) or "default")])
        return {**out, "radio_folders": radiofolders.for_viewer(v, sees_context=self.cad.comms._sees_context(),
                                                       hidden_labels=self.cad.context_labels())}

    def _views_op(self, action: str, body: dict, op: str) -> dict:
        """set {views: [{id, name, mark, color, filter}], base?} | reset {base?}: the Radio rail's views for the
        department (sizeup/cad/radioviews.py), admins, audited as Radios. base: the rev the edit started from
        (radioviews.rev; "" the default): someone else saved meanwhile -> refused, nothing written. The coded views
        saved as they are: back to the default (nothing kept)."""
        cur = radioviews.stored(self.overrides())
        base = body.get("base")
        if base is not None and base != radioviews.rev(cur):
            raise CadError("the views were changed meanwhile: reload them and edit again")
        sees = self.cad.comms._sees_context()
        if action == "reset":
            v = None
        elif action == "set":
            v = radioviews.clean(body.get("views"))
            if not sees:
                v = radioviews.merge_hidden(v, radioviews.SEED if cur is None else cur)
            if v == radioviews.SEED:
                v = None
        else:
            raise CadError("action: set or reset")
        if v == cur:                                    # nothing changes: nothing written, nothing audited
            return {"ok": True, "radio_views": radioviews.for_viewer(cur, sees)}
        out = self._commit([(VIEWS_SECTION, radioviews.KEY, REMOVE if v is None else v)], op,
                           [("radios", f"{VIEWS_SECTION}/{radioviews.KEY}", radioviews.summary(cur), radioviews.summary(v))])
        return {**out, "radio_views": radioviews.for_viewer(v, sees)}
    def shorts_page(self) -> dict:
        """{source key: short} the admins chose, as this reader may know of them (another system's: signed in only)."""
        sees = self.cad.comms._sees_context()
        return {k: v for k, v in radionames.clean_shorts(self.section(SHORT_SECTION)).items()
                if sees or not radiofolders.context_key(k)}

    def _short_op(self, body: dict, op: str) -> dict:
        """short {key, short}: a channel's or talkgroup's short label for chips (Settings > Radios > Channels, beside its
        name; sizeup/radionames.py), blank: its name. Admins, audited as Radios (radio_shorts/<key>, old -> new)."""
        key = radiofolders.name_key(body.get("key"))
        if radiofolders.context_key(key) and not self.cad.comms._sees_context():
            raise CadError("key: another system's talkgroup (sign in to name it)")
        try:
            v = radionames.clean_short(body.get("short"))
        except ValueError as e:
            raise CadError(str(e)) from None
        cur = radionames.clean_shorts(self.section(SHORT_SECTION))
        if cur.get(key, "") == v:                       # nothing changes: nothing written, nothing audited
            return {"ok": True, "radio_shorts": self.shorts_page()}
        out = self._commit([(SHORT_SECTION, key, v if v else REMOVE)], op,
                           [("radios", f"{SHORT_SECTION}/{key}", cur.get(key) or "default", v or "default")])
        return {**out, "radio_shorts": self.shorts_page()}

    def _mapd_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: detail_zoom | active_zoom | board_detail_zoom | board_active_zoom, value} | reset {key}: admins."""
        key = body.get("key")
        if key not in mapview.DISPLAY_DEFAULTS:
            raise CadError(f"key: {', '.join(mapview.DISPLAY_DEFAULTS)}")
        d = mapview.DISPLAY_DEFAULTS[key]
        cur = mapview.display_zooms(self.overrides().get(MAPD_SECTION))[key]
        if action == "reset":
            v = d
        elif action == "set":
            try:
                v = mapview.display_value(key, body.get("value"))
            except ValueError as e:
                raise CadError(str(e)) from None
        else:
            raise CadError("action: set or reset")
        if v == cur:                                    # nothing changes: nothing written, nothing audited
            return {"ok": True, "map_display": self.mapd_page()}
        out = self._commit([(MAPD_SECTION, key, REMOVE if v == d else v)], op,
                           [("system", f"{MAPD_SECTION}/{key}", f"{cur:g}", f"{v:g}")])
        return {**out, "map_display": self.mapd_page()}

    def atc_page(self, ov: dict | None = None) -> dict:
        """Settings > System > ATC talkers (#252): each switch, its default (on) and whether it is edited."""
        a = (self.section(ATC_SECTION) if ov is None else (ov.get(ATC_SECTION) or {}))
        return {k: {"label": lab, "value": bool(a.get(k, True)), "default": True, "edited": k in a} for k, lab in ATC_KEYS.items()} \
            | {"channels": sorted(getattr(getattr(self.cad, "atc_talkers", None), "channels", ()) or ())}

    def _atc_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: identify | map | vocab, value: true | false} | reset {key}: admins. Off: the raw transmissions, as before
        (vocab off: the vocabulary is only what Settings > Radios > Vocabulary > Push sends)."""
        key = body.get("key")
        if key not in ATC_KEYS:
            raise CadError("key: identify, map or vocab")
        cur = (self.overrides().get(ATC_SECTION) or {}).get(key, True)
        if action == "reset":
            v = True
        elif action == "set":
            v = body.get("value")
            if not isinstance(v, bool):
                raise CadError(f"{key}: true or false")
        else:
            raise CadError("action: set or reset")
        out = self._commit([(ATC_SECTION, key, REMOVE if v is True else v)], op,
                           [("system", f"{ATC_SECTION}/{key}", "on" if cur else "off", "on" if v else "off")])
        return {**out, "atc": self.atc_page()}

    def _hyd_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: assume_c, value: true | false} | reset {key}: admins (Settings > Hydrants, #840)."""
        key = body.get("key")
        if key not in HYD_KEYS:
            raise CadError("key: assume_c")
        cur = (self.overrides().get(HYD_SECTION) or {}).get(key, True)
        if action == "reset":
            v = True
        elif action == "set":
            v = body.get("value")
            if not isinstance(v, bool):
                raise CadError(f"{key}: true or false")
        else:
            raise CadError("action: set or reset")
        if v == cur:                                     # as it is: nothing written, nothing audited
            return {"ok": True, HYD_SECTION: {key: v}}
        out = self._commit([(HYD_SECTION, key, REMOVE if v is True else v)], op,
                           [("system", f"{HYD_SECTION}/{key}", "on" if cur else "off", "on" if v else "off")])
        return {**out, HYD_SECTION: {key: v}}

    def swap_page(self, ov: dict | None = None) -> dict:
        """Settings > Radios > Receivers: auto-replace for critical channels (off by default)."""
        a = (self.section(SWAP_SECTION) if ov is None else (ov.get(SWAP_SECTION) or {}))
        return {k: {"label": lab, "value": a.get(k) is True, "default": False, "edited": k in a} for k, lab in SWAP_KEYS.items()}

    def _swap_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: auto_critical, value: true | false} | reset {key}: admins."""
        key = body.get("key")
        if key not in SWAP_KEYS:
            raise CadError("key: auto_critical")
        cur = (self.overrides().get(SWAP_SECTION) or {}).get(key) is True
        if action == "reset":
            v = False
        elif action == "set":
            v = body.get("value")
            if not isinstance(v, bool):
                raise CadError(f"{key}: true or false")
        else:
            raise CadError("action: set or reset")
        if v == cur:                                     # as it is: nothing written, nothing audited
            return {"ok": True, "dongle_swap": self.swap_page()}
        out = self._commit([(SWAP_SECTION, key, v if v else REMOVE)], op,
                           [("radios", f"{SWAP_SECTION}/{key}", "on" if cur else "off", "on" if v else "off")])
        return {**out, "dongle_swap": self.swap_page()}

    def _rem_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, value} | reset {key} | reset_section: admins (Settings > Dispatch > Reminders)."""
        cur = self.section(REM_SECTION)
        if action == "reset_section":
            if not cur:
                return {"ok": True, REM_SECTION: reminders.page(cur)}
            out = self._commit([(REM_SECTION, k, REMOVE) for k in cur], op,
                               [("dispatch", f"{REM_SECTION}/{k}", v, None) for k, v in cur.items()])
            return {**out, REM_SECTION: reminders.page({})}
        key = body.get("key")
        if key not in reminders.keys():
            raise CadError(f"key: one of {', '.join(reminders.keys())}")
        if action == "reset":
            v = reminders.default(key)
        elif action == "set":
            try:
                v = reminders.clean(key, body.get("value"))
            except ValueError as e:
                raise CadError(str(e)) from None
        else:
            raise CadError("action: set, reset or reset_section")
        was = cur.get(key, reminders.default(key))
        if v == was:                                     # as it is: nothing written, nothing audited
            return {"ok": True, REM_SECTION: reminders.page(cur)}
        out = self._commit([(REM_SECTION, key, REMOVE if v == reminders.default(key) else v)], op,
                           [("dispatch", f"{REM_SECTION}/{key}", was, v)])
        return {**out, REM_SECTION: reminders.page(self.section(REM_SECTION))}

    def _dept_value(self, key, value, accepts=None) -> tuple[str, object]:
        """(code, the stored value: the name, or {"name", "accepts": true}). A board unit's code is refused."""
        if isinstance(value, dict):
            value, accepts = value.get("name"), value.get("accepts")
        key, name = _text(key, "key", 24), _text(value, "value", 60)
        if accepts is not None and not isinstance(accepts, bool):
            raise CadError("accepts: true or false")
        k = re.sub(r"[\s-]+", "", key).upper()
        clash = next((u["name"] for u in self.store.units()
                      if k in {re.sub(r"[\s-]+", "", str(x or "")).upper() for x in (u["name"], u["spoken"])}), None)
        if clash:
            raise CadError(f"{key}: the code of unit {clash}, not a department entry")
        return key, ({"name": name, "accepts": True} if accepts else name)

    def _dept_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: a feed's department entry code, value: the department, accepts?: true} | reset {key}: admins (#370)."""
        cur = self.section(DEPT_SECTION)
        if action == "reset":
            key = _text(body.get("key"), "key", 24)
            if key not in cur:
                raise CadError(f"{key} is not a department entry")
            return self._commit([(DEPT_SECTION, key, REMOVE)], op, [("dispatch", f"{DEPT_SECTION}/{key}", cur[key], "unset")])
        if action != "set":
            raise CadError("action: set or reset")
        key, v = self._dept_value(body.get("key"), body.get("value"), body.get("accepts"))
        if cur.get(key) == v:
            return {"ok": True}
        return self._commit([(DEPT_SECTION, key, v)], op, [("dispatch", f"{DEPT_SECTION}/{key}", cur.get(key, "unset"), v)])

    def feed_types_page(self, ov: dict | None = None) -> dict:
        """Settings > Lists > Feed types: the department's rows (each with whether the lists still allow it), the codes
        seen lately that nothing maps fully (Sources.feed_types_seen), and the feeds to pick from."""
        L = self.cad.lists
        rows = []
        for k, v in sorted(((ov.get(FT_SECTION) if ov is not None else self.section(FT_SECTION)) or {}).items()):
            source, code = feedtypes.parse_key(k)
            v = v if isinstance(v, dict) else {}
            try:
                L.check_type(str(v.get("type") or ""), str(v.get("subtype") or ""), required=True)
                why = ""
            except ValueError as e:
                why = str(e)
            rows.append({"key": k, "source": source, "code": code, "type": v.get("type") or "",
                         "subtype": v.get("subtype") or "",
                         "label": L.type_label(str(v.get("type") or ""), str(v.get("subtype") or "")), "off": why})
        src = getattr(self.cad, "sources", None)
        seen = src.feed_types_seen() if src is not None else []
        for r in seen:
            r["shown"] = L.type_label(r["type"], r["subtype"])
        feeds = {r["source"]: r["label"] for r in self.store.q("SELECT source, label FROM source_health ORDER BY rank")}
        for r in seen:
            feeds.setdefault(r["source"], r["label"])
        return {"rows": rows, "seen": seen, "feeds": [{"source": k, "label": v or k} for k, v in feeds.items()]}

    def _feed_types_op(self, action: str, body: dict, op: str) -> dict:
        """set {feed, code: "MAIN" | "MAIN,SUB", type, subtype?} | reset {key: "<feed>|<code>"}: admins, audited as Lists."""
        cur = self.section(FT_SECTION)
        if action == "reset":
            key = str(body.get("key") or "")
            if key not in cur:
                raise CadError("no such feed type row")
            out = self._commit([(FT_SECTION, key, REMOVE)], op, [("lists", f"{FT_SECTION}/{key}", cur[key], "unset")])
            return {**out, FT_SECTION: self.feed_types_page()}
        if action != "set":
            raise CadError("action: set or reset")
        try:
            key, v = feedtypes.clean(body.get("feed"), body.get("code"), body.get("type"), body.get("subtype"),
                                     self.cad.lists)
        except ValueError as e:
            raise CadError(str(e)) from None
        if cur.get(key) == v:
            return {"ok": True, FT_SECTION: self.feed_types_page()}
        out = self._commit([(FT_SECTION, key, v)], op, [("lists", f"{FT_SECTION}/{key}", cur.get(key, "unset"), v)])
        return {**out, FT_SECTION: self.feed_types_page()}

    def _unit_rows(self, L, ov: dict, units: list[dict] | None = None) -> dict:
        o, used = ov.get("units", {}), self._page_usage("units")
        base_on = {c for c, _, _ in self.base.board_units()}
        live = {u["name"]: u for u in (self.store.units() if units is None else units)}
        rows, on = [], set()
        for b in L.unit_boards:
            pos = 0
            for code in b["units"]:
                if not code:
                    continue
                on.add(code)
                rows.append(self._unit_row(L, code, b["id"], pos, o, used, base_on, live))
                pos += 1
        for code, u in L.units.items():
            if code not in on and u.get("retired"):
                rows.append(self._unit_row(L, code, "", 0, o, used, base_on, live))
        for code, v in o.items():
            if isinstance(v, dict) and v.get("deleted"):
                rows.append(dict(self._unit_row(self.base, code, "", 0, {}, used, base_on, live), state="deleted"))
        bad = set(self.made_fire(o))            # another agency's unit an old edit made our engine: Reset offered
        for r in rows:
            r["made_fire"] = r["code"] in bad and r.get("state") != "deleted"
        return {"rows": rows, "edited": bool(o), "layout_edited": "_boards" in o,
                "stations": [f["name"] for f in getattr(self.cad, "firehouses", [])],
                "status_sets": [{"id": k, "title": v.get("title") or k, "third_party": bool(v.get("third_party")),
                                 "statuses": list(L.agency_codes(k))} for k, v in L.agency_status_sets["sets"].items()]}

    def _unit_row(self, L, code, board, pos, o, used, base_on, live) -> dict:
        u, info = L.units.get(code, {}), L.unit(code)
        lv = live.get(code) or {}
        return {"code": code, "board": board, "pos": pos, "spoken": u.get("spoken") or "", "title": info["title"],
                "kind": info["kind"], "department": info["department"], "spare": info["spare"], "response": info["response"],
                "station": L.station(code), "status_set": L.agency_set(code, board or None),
                "third_party": L.third_party(code, board or None),
                "retired": bool(u.get("retired")), "used": used.get(code, 0), "named": info["named"],
                "state": "added" if code not in base_on and code not in self.base.units else "edited" if code in o else "default",
                "status": lv.get("status", ""), "incident_number": lv.get("incident_number")}

    def _dispatch_page(self, L, ov: dict) -> dict:
        o, d = ov.get("dispatch_wording", {}), ov.get("dispatch", {})
        return {"wording": [{"key": k, "label": lab, "value": L.wording[k], "default": self.base.wording[k],
                             "state": "edited" if k in o else "default"} for k, lab in WORDING],
                "placeholders": ["department", "units", "location", "type", "type_bare", "already", "info", "change",
                                 "respond", "remarks", "joint", "time", "full", "Full", "status"],
                "lead_in_joint_only": [{"town": t, "value": L.lead_in_joint_only.get(t, ""),
                                        "default": self.base.lead_in_joint_only.get(t, ""),
                                        "state": "edited" if "lead_in_joint_only" in o and
                                        L.lead_in_joint_only.get(t) != self.base.lead_in_joint_only.get(t) else "default"}
                                       for t in L.joint_response],
                "dept_tones": [{"town": t, "value": [list(x) for x in L.dispatch_tones[t]] if L.dispatch_tones.get(t) else None,
                                "default": [list(x) for x in self.base.dispatch_tones[t]] if self.base.dispatch_tones.get(t) else None,
                                "edited": t in ov.get("dispatch_tones", {})} for t in L.joint_response],
                "tones": {"value": tone_groups(self.cad.s.tones), "default": self.config_tones,
                          "edited": "tones" in d},
                "channels": {"value": list(self.cad.s.channels), "default": list(self.config_channels), "edited": "channels" in d},
                "voice": {"engine": getattr(self.cad.s, "tts_engine", "say"), "name": getattr(self.cad.s, "voice", ""),
                          "voices": self._voice_ids(), "default": self.config_voice[0], "default_speed": self.config_voice[1],
                          "edited": "voice" in d or "speed" in d,
                          "speed": getattr(self.cad.s, "speed", 1.0), "rate_wpm": getattr(self.cad.s, "rate_wpm", 0),
                          "fallback": getattr(self.cad.s, "fallback_voice", ""), "repeat": getattr(self.cad.s, "repeat", 0)},
                "transmit": {"enabled": self.cad.transmitter.enabled, "note": "Transmit off: logged, played here only"},
                "weather_speak": {"value": bool(d.get("weather_speak", False)), "default": False, "edited": "weather_speak" in d},
                "department": self.cad.s.department,
                "eta": self._eta_page(L, d)}

    def _eta_page(self, L, d: dict) -> dict:
        """The recommendation's ETA estimates (sizeup/cad/eta.py): each value, its default, edited or not."""
        cur, dflt = self.cad.eta.as_dict(), eta.Params().as_dict()
        return {"numbers": [{"key": k, "label": lab, "unit": unit, "min": lo, "max": hi, "value": cur[k], "default": dflt[k],
                             "edited": k in d} for k, (_, lo, hi, lab, unit) in eta.NUMBERS.items()],
                "order": {"value": cur["order"], "default": dflt["order"], "edited": "order" in d, "choices": list(eta.ORDERS)},
                "quarters_status": {"value": cur["quarters_status"], "default": dflt["quarters_status"],
                                    "edited": "quarters_status" in d,
                                    "choices": [{"code": c, "name": L.name("unit_statuses", c)} for c in L.free]},
                "note": "Estimates only: by road where the road graph has a way (OSM roads, road speeds × emergency "
                        "factor, closures in effect avoided), else straight-line distance × road factor at an average "
                        "speed; plus turnout from quarters. No traffic. Shown as “~3 min est.”."}

    def _system(self, ov: dict, places: int | None = None) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            raw = b""
        return {"data_file": lists_mod._shown_path(str(self.path)), "defaults_version": str(self.defaults.get("version") or ""),
                "sha256": hashlib.sha256(raw).hexdigest()[:12], "schema": SCHEMA_VERSION,
                "overrides": self._counts(ov, places), "db": self.store.path}

    @staticmethod
    def _counts(ov: dict, places: int | None) -> dict[str, int]:
        """System > Edits: how many overrides each section holds; places: the place names' count when ov was read
        without them (page(): overrides(skip=("places",)))."""
        out = {k: len(v) for k, v in ov.items()}
        if places:
            out["places"] = places
        return dict(sorted(out.items()))

    # -- edits ------------------------------------------------------------------------------------------

    def post(self, section: str, body: dict, guard: bool = False) -> dict:
        """guard: roles are enforced (the hub with Cloudflare Access): users and import may not take
        away the last active admin with a sign-in email (LastAdmin)."""
        op = _text(body.get("operator"), "operator", OPERATOR_MAX, required=False)
        action = body.get("action")
        if not isinstance(action, str):
            raise CadError("action: required")
        if section in LIST_SECTIONS:
            return self._list_op(section, action, body, op)
        fn = {"event_class": self._class_op, "status_flow": self._flow_op, "type_subtypes": self._map_op, "geo_town_order": self._towns_op, "units": self._units_op,
              "dispatch": self._dispatch_op, "users": self._users_op, "runcards": self._cards_op,
              "import": self._import_op, "channels": self._channels_op, "stt": self._stt_op,
              channels_mod.SQUELCH_SECTION: self._squelch_op,
              "places": self._places_op, COMMON_SECTION: self._common_op, ADDR_SECTION: self._addresses_op, TONES_SECTION: self._tones_op, WALL_SECTION: self._wall_op, DS_SECTION: self._ds_op,
              NOTIFY_SECTION: self._notify_op, ATC_SECTION: self._atc_op, HYD_SECTION: self._hyd_op, SWAP_SECTION: self._swap_op, REM_SECTION: self._rem_op, ERR_SECTION: self._errors_op, MAPD_SECTION: self._mapd_op, PREMISE_SECTION: self._premise_op, EM_SECTION: self._em_op, FM_MAIN_SECTION: self._fm_main_op, SIGNAL_SECTION: self._signal_op, FOLDERS_SECTION: self._folders_op, VIEWS_SECTION: self._views_op, SVC_SECTION: self._services_op, RET_SECTION: self._retention_op,
              TRS_SECTION: lambda a, b, o: self._retention_op(a, b, o, TRS_SECTION), MUTE_SECTION: self._mutes_op, LOGS_SECTION: self._logs_op, DEPT_SECTION: self._dept_op, FT_SECTION: self._feed_types_op, "alarms": lambda a, b, o: self.cad.alarms.settings_op(a, b, o),
              "intel": lambda a, b, o: self.cad.intel_admin.settings_op(a, b, o),
              FIX_SECTION: lambda a, b, o: self.cad.stt_fix.settings_op(a, b, o)}.get(section)
        if fn is None:
            raise CadError(f"no settings section {section!r}")
        if section in ("users", "import"):
            return fn(action, body, op, guard)
        return fn(action, body, op)

    def _clean_item(self, sec: str, raw, code: str) -> dict:
        if not isinstance(raw, dict):
            raise CadError("item: an object")
        out = {"name": _text(raw.get("name"), "name", 60)}
        for f in LIST_FIELDS[sec]:
            v = raw.get(f)
            if f == "short":                      # a location qualifier's text ("IFO") and words ("in front of")
                out[f] = _text(v, "short", lists_mod.QUAL_SHORT_MAX)
                if not lists_mod.QUAL_SHORT.fullmatch(out[f]):
                    raise CadError("short: letters, digits, / & . - (IFO)")
            elif f == "spoken":
                out[f] = " ".join(_text(v, "spoken", lists_mod.QUAL_SPOKEN_MAX).split()).lower()
                if not lists_mod.QUAL_SPOKEN.fullmatch(out[f]):
                    raise CadError("spoken: words as said, letters only (in front of)")
            elif f == "role":
                if v not in (None, "") + ROLES:
                    raise CadError(f"role: one of {', '.join(ROLES)} (or none: committed)")
                if v:
                    out["role"] = v
            elif v not in (None, ""):
                if not isinstance(v, str) or not COLOR.fullmatch(v):
                    raise CadError(f"{f}: a color #rrggbb")
                out[f] = v.upper()
            elif f == "color" or (sec == "unit_statuses" and f == "bg"):
                raise CadError(f"{f}: required (#rrggbb)")
        return out

    def _list_op(self, sec: str, action: str, body: dict, op: str) -> dict:
        L, ov = self.cad.lists, self.overrides()
        o = ov.get(sec, {})
        items = {x["code"]: x for x in getattr(L, sec)}
        base = {x["code"]: x for x in getattr(self.base, sec)}
        title = LIST_TITLES[sec].lower()
        if action == "reset_section":
            gone = [c for c in o if not c.startswith("_") and c not in base and self.usage(sec).get(c)]
            if gone:
                raise CadError(f"can't reset {title}: {', '.join(gone)} (added here) are used; retire them instead")
            return self._commit([(sec, k, REMOVE) for k in o], op, [("lists", f"{sec}", f"{len(o)} edits", "reset to the defaults")])
        if action == "move":
            code = str(body.get("code") or "")
            order = list(items)
            if code not in order:
                raise CadError(f"no {code!r} in {title}")
            i = order.index(code)
            to = body.get("to")
            to = i + _dir(body) if to is None else to
            if not isinstance(to, int) or not 0 <= to < len(order):
                return {"ok": True}
            order.insert(to, order.pop(i))
            base_order = [c for c in base if c in items] + [c for c in order if c not in base]
            return self._commit([(sec, "_order", REMOVE if order == base_order else order)], op,
                                [("lists", f"{sec}/_order", f"{code} at {i + 1}", f"{code} at {to + 1}")])
        code = _text(body.get("code"), "code", 40).upper()
        if action == "add":
            if not LIST_CODE[sec].fullmatch(code):
                raise CadError(f"code: {code!r}: capital letters, digits and - _ (" + ("up to 24" if LIST_CODE[sec] is CODE else "up to 40") + ")")
            if code in items:
                raise CadError(f"{code} is already in {title}" + (" (retired: restore it)" if items[code].get("retired") else ""))
            item = self._clean_item(sec, body.get("item"), code)
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", None, item)])
        if code not in items and not (action == "reset" and code in o):
            raise CadError(f"no {code!r} in {title}")
        cur = items.get(code)
        if sec == "dispositions" and code in lists_mod.SYSTEM_DISPOSITIONS and action in ("edit", "retire", "restore", "delete"):
            raise CadError(f"{code} is the hub's own (a passed Until closes an event with it): "
                           "it can't be edited, retired or deleted")
        if action == "edit":
            raw = body.get("item")
            if isinstance(raw, dict) and raw.get("code") not in (None, code):
                raise CadError("codes don't change (they are on events and in the exports): retire it and add a new one")
            item = self._clean_item(sec, dict(_norm(cur), **(raw or {})), code)
            if cur.get("retired"):
                item["retired"] = True
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", _norm(cur), item)])
        if action in ("retire", "restore"):
            on = action == "retire"
            if on and sec == "unit_statuses":
                n = sum(1 for u in self.store.units() if u["status"] == code)
                if n:
                    raise CadError(f"{n} unit{'s are' if n != 1 else ' is'} {cur['name']} now: change {'them' if n != 1 else 'it'} first")
            item = dict(_norm(cur), retired=True) if on else {k: v for k, v in _norm(cur).items() if k != "retired"}
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", "active" if on else "retired",
                                                          "retired" if on else "active")])
        if action == "delete":
            why = self._used_why(sec, code)
            if why:
                raise CadError(f"can't delete {cur['name']} ({code}): used by {why}. Retire it instead: "
                               "it stays on old events, off the pick lists")
            changes = [(sec, code, {"deleted": True} if code in base else REMOVE)]
            if isinstance(o.get("_order"), list) and code in o["_order"]:
                changes.append((sec, "_order", [c for c in o["_order"] if c != code]))
            if sec == "unit_statuses":          # its next-step buttons go with it
                for k, v in ov.get("status_flow", {}).items():
                    if k == code:
                        changes.append(("status_flow", k, REMOVE))
                    elif isinstance(v, list) and code in v:
                        changes.append(("status_flow", k, [c for c in v if c != code] or REMOVE))
            return self._commit(changes, op, [("lists", f"{sec}/{code}", _norm(cur), "deleted")])
        if action == "reset":
            if code not in o:
                raise CadError(f"{code} is already the default")
            if code not in base:
                return self._list_op(sec, "delete", body, op)
            return self._commit([(sec, code, REMOVE)], op, [("lists", f"{sec}/{code}", o[code], "default")])
        raise CadError(f"action: add, edit, move, retire, restore, delete, reset or reset_section")

    def _class_op(self, action: str, body: dict, op: str) -> dict:
        o = self.overrides().get("event_class", {})
        cur, base = self.cad.lists.event_class, self.base.event_class
        if action == "reset_section":
            return self._commit([("event_class", k, REMOVE) for k in o], op, [("lists", "event_class", f"{len(o)} edits", "reset")])
        key = _text(body.get("key"), "key", 60).upper()
        if action == "set":
            v = body.get("value")
            if v not in CLASSES:
                raise CadError("value: emergency or non-emergency")
            return self._commit([("event_class", key, REMOVE if base.get(key) == v else v)], op,
                                [("lists", f"event_class/{key}", cur.get(key), v)])
        if action == "remove":
            if key not in cur:
                raise CadError(f"no rule for {key}")
            return self._commit([("event_class", key, None if key in base else REMOVE)], op,
                                [("lists", f"event_class/{key}", cur[key], None)])
        if action == "reset":
            if key not in o:
                raise CadError(f"{key} is already the default")
            return self._commit([("event_class", key, REMOVE)], op, [("lists", f"event_class/{key}", cur.get(key), base.get(key))])
        raise CadError("action: set, remove, reset or reset_section")

    def _flow_op(self, action: str, body: dict, op: str) -> dict:
        """The next-step buttons on a unit that is on an incident: status (or "*", any other) -> statuses."""
        o = self.overrides().get("status_flow", {})
        cur, base = self.cad.lists.status_flow, self.base.status_flow
        if action == "reset_section":
            return self._commit([("status_flow", k, REMOVE) for k in o], op, [("lists", "status_flow", f"{len(o)} edits", "reset")])
        key = _text(body.get("key"), "key", 40).upper()
        if action == "set":
            nxt = body.get("next")
            if not isinstance(nxt, list) or not all(isinstance(c, str) for c in nxt) or len(set(nxt)) != len(nxt):
                raise CadError("next: a list of unit status codes, each once")
            if not nxt:
                raise CadError("next: at least one status (remove the row to use \u201cany other\u201d)")
            same = key in base and list(base[key]) == nxt
            return self._commit([("status_flow", key, REMOVE if same else nxt)], op,
                                [("lists", f"status_flow/{key}", list(cur.get(key, ())) or None, nxt)])
        if action == "remove":
            if key not in cur:
                raise CadError(f"no next steps for {key}")
            if key == "*":
                raise CadError("\u201cany other status\u201d stays (edit its steps instead)")
            return self._commit([("status_flow", key, None if key in base else REMOVE)], op,
                                [("lists", f"status_flow/{key}", list(cur[key]), None)])
        if action == "reset":
            if key not in o:
                raise CadError(f"{key} is already the default")
            return self._commit([("status_flow", key, REMOVE)], op,
                                [("lists", f"status_flow/{key}", list(cur.get(key, ())) or None, list(base.get(key, ())) or None)])
        raise CadError("action: set, remove, reset or reset_section")

    def _map_page(self, L, ov: dict) -> dict:
        """The type -> subtype map, a row per type: what it allows, whether the department gave it or it
        is a proposal to confirm (until edited or confirmed here), the default."""
        o, tsp = ov.get("type_subtypes", {}), L.type_subtypes_partial
        rows = []
        for t in L.event_types:
            c = t["code"]
            row = {"type": c, "name": t["name"], "subtypes": list(L.type_subtypes.get(c, ())), "listed": c in L.type_subtypes,
                   "state": "edited" if c in o else "default", "default": list(self.base.type_subtypes.get(c, ())),
                   "source": "given" if c in tsp.get("given", ()) else "proposed"}
            if t.get("retired"):
                row["retired"] = True
            if c in tsp.get("confirm", {}) and c not in o:
                row["confirm"] = tsp["confirm"][c]
            rows.append(row)
        return {"rows": rows, "gaps": L.map_gaps(), "edited": bool(o)}

    def _map_op(self, action: str, body: dict, op: str) -> dict:
        """Which subtypes a type allows (a checkbox per subtype in Settings). set with the list as it is:
        confirmed (a proposal no longer marked "to confirm"). A subtype off the map stays on old events;
        a run card for that pair has to go first (it could never match an event again)."""
        L, o = self.cad.lists, self.overrides().get("type_subtypes", {})
        if action == "reset_section":
            return self._commit([("type_subtypes", k, REMOVE) for k in o], op, [("lists", "type_subtypes", f"{len(o)} edits", "reset")],
                                check=self._cards_fit)
        typ = _text(body.get("type"), "type", 24).upper()
        if typ not in {t["code"] for t in L.event_types}:
            raise CadError(f"no event type {typ!r}")
        cur = list(L.type_subtypes.get(typ, ()))
        if action == "set":
            subs = body.get("subtypes")
            if not isinstance(subs, list) or not all(isinstance(x, str) for x in subs) or len(set(subs)) != len(subs):
                raise CadError("subtypes: a list of subtype codes, each once")
            known = {x["code"] for x in L.event_subtypes}
            bad = [x for x in subs if x not in known]
            if bad:
                raise CadError(f"subtypes: {', '.join(bad)} not in the event subtypes")
            new_on = [x for x in subs if x not in cur and L.retired("event_subtypes", x)]
            if new_on:
                raise CadError(f"subtypes: {', '.join(new_on)} retired (restore it first)")
            order = [x["code"] for x in L.event_subtypes]
            subs = sorted(subs, key=lambda x: (x not in cur, cur.index(x) if x in cur else order.index(x)))
            same = typ in self.base.type_subtypes and set(self.base.type_subtypes[typ]) == set(subs) \
                and typ not in L.type_subtypes_partial.get("confirm", {})
            return self._commit([("type_subtypes", typ, REMOVE if same else subs)], op,
                                [("lists", f"type_subtypes/{typ}", cur if typ in L.type_subtypes else None, subs)],
                                check=self._cards_fit)
        if action == "reset":
            if typ not in o:
                raise CadError(f"{typ} is already the default")
            return self._commit([("type_subtypes", typ, REMOVE)], op,
                                [("lists", f"type_subtypes/{typ}", cur, list(self.base.type_subtypes.get(typ, ())))],
                                check=self._cards_fit)
        raise CadError("action: set, reset or reset_section")

    def _cards_fit(self, new) -> None:
        """A map edit can't leave a run card for a pair the map no longer allows (one already off it: kept)."""
        old = self.cad.lists
        lost = []
        for c in runcards.cards(self.store):
            t, _, sub = c["type"].partition("/")
            if sub and sub in old.subtypes_for(t) and sub not in new.subtypes_for(t):
                lost.append(c["type"])
        if lost:
            raise CadError(f"run card{'s' if len(lost) != 1 else ''} {', '.join(lost)} would no longer fit: "
                           "change or delete the card first (Run cards)")

    # -- the department's own names for places (#67): "the Waterfront", "Kohl's plaza" -------------------
    # Kept as overrides (section "places", key: the name in lower case) so they are audited, exported and
    # imported with the rest; the place search (Cad.search_places, the location box) offers them first.

    def _places_cached(self) -> tuple:
        """(store rev, the names' version, the names): read again only when they changed (the county's are ~1,000)."""
        rev = self.store.rev
        got = getattr(self, "_places_cache", (None, None, []))
        if got[0] != rev:        # any write: the version asked again, the names read again only when it moved
            r = self.store.one("SELECT COUNT(*) AS n, MAX(updated_at) AS at, TOTAL(LENGTH(value)) AS size "
                               "FROM settings_overrides WHERE section = 'places'")
            ver = (r["n"], r["at"], r["size"]) if r else None
            if ver != got[1] or got[1] is None:
                rows = self.store.q("SELECT key, value FROM settings_overrides WHERE section = 'places' ORDER BY key")
                got = (rev, ver, [dict(json.loads(x["value"]), key=x["key"]) for x in rows])
            else:
                got = (rev, ver, got[2])
            self._places_cache = got
        return got

    def local_places(self) -> list[dict]:
        return [dict(r) for r in self._places_cached()[2]]

    def places_rev(self) -> str:
        """The place names' version (how many, the last change, their size): moves with every change to them. Nothing
        copied (the state asks at every build)."""
        return json.dumps(self._places_cached()[1])

    def _places_op(self, action: str, body: dict, op: str) -> dict:
        cur = {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in self.local_places()}
        if action == "delete":
            key = _text(body.get("key"), "key", 80)
            if key not in cur:
                raise CadError(f"no place name {key!r}")
            return self._commit([("places", key, REMOVE)], op, [("lists", f"place/{cur[key]['name']}", cur[key], None)])
        if action != "set":
            raise CadError("action: set or delete")
        name = _text(body.get("name"), "name", 60)
        key = " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())
        if not key:
            raise CadError("name: letters or digits")
        raw = body.get("aliases") or []
        if isinstance(raw, str):
            raw = [a for a in raw.split(",")]
        if not isinstance(raw, list) or len(raw) > 10:
            raise CadError("aliases: up to 10 other names")
        aliases = [a for a in (_text(a, "alias", 60, required=False) for a in raw) if a]
        where = body.get("place")
        if not isinstance(where, dict):
            raise CadError("place: pick where it is from the suggestions")
        label = _text(where.get("label"), "place", 200)
        try:
            lat, lon = float(where.get("lat")), float(where.get("lon"))
        except (TypeError, ValueError):
            raise CadError("place: lat and lon") from None
        if not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
            raise CadError("place: lat and lon in the area")
        if key not in cur and sum(1 for k in cur if not commonplaces.is_imported(k)) >= MAX_PLACE_NAMES:
            raise CadError(f"at most {MAX_PLACE_NAMES} place names")
        val = {"name": name, "aliases": aliases, "label": label, "lat": round(lat, 6), "lon": round(lon, 6),
               "muni": _text(where.get("sub"), "town", 80, required=False),
               "note": _text(body.get("note"), "note", 120, required=False)}
        return self._commit([("places", key, val)], op, [("lists", f"place/{name}", cur.get(key), val)])

    # -- the department's own address points (sizeup/geo/localaddr.py) ----------------------------------

    def local_addresses(self) -> tuple:
        """The local address points ({key, number, street, muni, lat, lon, note}): the same tuple while nothing changed
        (the gazetteer re-indexes them only on a change). Read only: never change a row."""
        rev = self.store.rev
        got = getattr(self, "_addr_cache", (None, ()))
        if got[0] != rev:
            rows = self.store.q("SELECT key, value FROM settings_overrides WHERE section = ? ORDER BY key", (ADDR_SECTION,))
            new = tuple(dict(json.loads(x["value"]), key=x["key"]) for x in rows)
            got = (rev, got[1] if new == got[1] else new)
            self._addr_cache = got
        return got[1]

    def _addresses_op(self, action: str, body: dict, op: str) -> dict:
        """set {address ("189-1/2 Example St"), town?, lat, lon, note?, key? (the point edited)} | delete {key}."""
        cur = {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in self.local_addresses()}
        if action == "delete":
            key = _text(body.get("key"), "key", 160)
            if key not in cur:
                raise CadError(f"no address point {key!r}")
            return self._commit([(ADDR_SECTION, key, REMOVE)], op, [("lists", f"address/{key}", cur[key], None)])
        if action != "set":
            raise CadError("action: set or delete")
        text = _text(body.get("address"), "address", 120)
        sp = split_address(text)
        if sp is None:
            raise CadError("address: a house number, then the street (189-1/2 Example St)")
        number_text = sp[2]
        street_text, _c, town_text = sp[1].partition(",")
        town = _text(body.get("town"), "town", 60, required=False) or town_text.strip()
        try:
            lat, lon = float(body.get("lat")), float(body.get("lon"))
        except (TypeError, ValueError):
            raise CadError("pin: lat and lon (click the map)") from None
        if not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
            raise CadError("pin: lat and lon in the area")
        ix = self.cad.place_index()
        if getattr(ix, "gaz", None) is None:
            raise CadError("the gazetteer is not loaded: try again shortly")
        street, muni = self._addr_place(ix, street_text, town, lat, lon)
        hn = sp[0]
        key = f"{hn.label} {street}, {muni}".lower()
        old_key = _text(body.get("key"), "key", 160, required=False)
        if old_key and old_key not in cur:
            raise CadError(f"no address point {old_key!r}")
        if key in cur and key != old_key:
            raise CadError(f"{hn.label} {street} is already pinned")
        if key not in cur and not old_key and len(cur) >= MAX_LOCAL_ADDRESSES:
            raise CadError(f"at most {MAX_LOCAL_ADDRESSES} address points")
        val = {"number": number_text, "street": street, "muni": muni, "lat": round(lat, 6), "lon": round(lon, 6),
               "note": _text(body.get("note"), "note", 120, required=False)}
        changes = [(ADDR_SECTION, key, val)]
        audit = [("lists", f"address/{key}", cur.get(key), val)]
        if old_key and old_key != key:              # renumbered or another street: the old one goes
            changes.insert(0, (ADDR_SECTION, old_key, REMOVE))
            audit.insert(0, ("lists", f"address/{old_key}", cur[old_key], None))
        return dict(self._commit(changes, op, audit), key=key)

    def _addr_place(self, ix, street_text: str, town: str, lat: float, lon: float) -> tuple[str, str]:
        """The street (the gazetteer's name) and town (its muni) of a pin, checked: a street of the map data, a town it
        is in, the pin within ADDR_PIN_M of that town's stretch of it. Blank town: the street's town at the pin."""
        gaz = ix.gaz
        sid = gaz.street_id(street_text)
        if sid is None:                      # "Example Street": the street whose every word was typed
            hits = [k for k, q in ix.street_ix.match(norm_tokens(street_text), limit=6) if q >= 0.99]
            sids = [ix.street_ids[k] for k in hits]
            sid = min(sids, key=lambda i: gaz._dist_to_street(i, lat, lon)) if sids else None
        if sid is None or not norm_tokens(street_text):
            raise CadError(f"street: {street_text.strip()!r} not in the map data")
        street = gaz.streets[sid].name
        towns = gaz.street_towns(sid)
        town_of = self.cad.lists.town_of
        if town:
            muni = next((m for m in towns if town.lower() in (m.lower(), (town_of(m) or "").lower())), None)
            if muni is None:
                raise CadError(f"town: {street} is in {', '.join(sorted({town_of(m) or m for m in towns})) or 'no town'}")
        else:
            muni = gaz.town_near(sid, lat, lon) or (towns[0] if towns else "")
        if gaz.dist_in_town(sid, muni, lat, lon) > ADDR_PIN_M:
            raise CadError(f"pin: over {ADDR_PIN_M} m from {street}" + (f" in {town_of(muni) or muni}" if muni else ""))
        return street, muni

    def _import_addresses(self, sec: dict) -> tuple[dict, list[str]]:
        """An imported local_addresses section, each point checked as the form checks it (a known street, its town,
        the pin on that town's stretch, the key its value's, at most MAX_LOCAL_ADDRESSES): a point that fails is
        skipped, counted in the notes. Without the gazetteer the streets can't be checked: kept as they are."""
        ix = self.cad.place_index()
        kept, bad = {}, 0
        for k, v in sec.items():
            try:
                if not isinstance(v, dict) or set(v) - {"number", "street", "muni", "lat", "lon", "note"}:
                    raise CadError("fields")
                hn = parse_number(v.get("number")) if isinstance(v.get("number"), str) else None
                if hn is None or not isinstance(v.get("street"), str) or not isinstance(v.get("muni"), str) \
                        or not isinstance(v.get("note", ""), str) or len(v.get("note", "")) > 120:
                    raise CadError("fields")
                lat, lon = v.get("lat"), v.get("lon")
                if any(isinstance(c, bool) or not isinstance(c, (int, float)) for c in (lat, lon)) \
                        or not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
                    raise CadError("pin")
                if getattr(ix, "gaz", None) is not None:
                    street, muni = self._addr_place(ix, v["street"], v["muni"], float(lat), float(lon))
                    if (street, muni) != (v["street"], v["muni"]):
                        raise CadError("street")
                if k != f"{hn.label} {v['street']}, {v['muni']}".lower() or len(kept) >= MAX_LOCAL_ADDRESSES:
                    raise CadError("key")
            except CadError:
                bad += 1
                continue
            kept[k] = dict(v)
        return kept, ([f"address points: {bad} skipped (not placeable here)"] if bad else [])

    def _addresses_page(self) -> dict:
        """Settings > Lists > Address points."""
        town_of = self.cad.lists.town_of
        items = [{"key": r["key"], "number": r.get("number") or "", "street": r.get("street") or "",
                  "town": town_of(r.get("muni") or "") or r.get("muni") or "", "lat": r.get("lat"), "lon": r.get("lon"),
                  "note": r.get("note") or ""} for r in self.local_addresses()]
        return {"items": items, "max": MAX_LOCAL_ADDRESSES}

    def _place_names_page(self) -> dict:
        """Settings > Lists > Place names: the names made by hand (items) and the imported list's summary."""
        made, imported = [], []
        for r in self.local_places():
            (imported if commonplaces.is_imported(r["key"], r) else made).append(r)
        lost = [{"name": r["name"], "address": r.get("address") or "", "town": r.get("muni") or ""}
                for r in imported if r.get("lat") is None]
        return {"items": made, "max": MAX_PLACE_NAMES,
                "imported": {"source": commonplaces.SOURCE, "names": len(imported),
                             "addresses": len({commonplaces.name_key(r.get("address") or "") for r in imported}),
                             "located": len(imported) - len(lost), "unresolved": lost[:300],
                             "date": max((r.get("imported") or "" for r in imported), default="")}}

    def _common_op(self, action: str, body: dict, op: str) -> dict:
        """The county's common place names (sizeup/cad/commonplaces.py): preview {file (base64), filename, columns?}
        -> counts and the addresses not found; apply: the same, written (every entry of that source replaced, names
        made by hand untouched); remove: every imported entry. Audited as counts only (never a name or an address)."""
        cur = {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in self.local_places()
               if commonplaces.is_imported(r["key"], r)}
        if action == "remove":
            if not cur:
                raise CadError("no imported place names")
            return self._commit([("places", k, REMOVE) for k in cur], op,
                                [("lists", f"place names/{commonplaces.SOURCE}", {"names": len(cur)}, {"names": 0})])
        if action not in ("preview", "apply"):
            raise CadError("action: preview, apply or remove")
        raw = body.get("file")
        if not isinstance(raw, str) or not raw:
            raise CadError("file: the spreadsheet (.xlsx or .csv), base64")
        raw = raw.split("base64,", 1)[-1]
        if len(raw) > commonplaces.MAX_BYTES * 4 // 3 + 8:
            raise CadError(f"file: at most {commonplaces.MAX_BYTES // (1024 * 1024)} MB")
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, binascii.Error):
            raise CadError("file: base64") from None
        filename = _text(body.get("filename"), "filename", 200, required=False)
        given = body.get("columns")
        if given is not None and not isinstance(given, dict):
            raise CadError("columns: {address, name, town}: column numbers from 0")
        try:
            rows = commonplaces.read_table(data, filename)
            if len(rows) < 2:
                raise commonplaces.TableError("no rows under the header")
            cols = commonplaces.find_columns(rows[0], given)
        except commonplaces.TableError as e:
            raise CadError(f"{filename or 'file'}: {e}") from None
        ix = self.cad.place_index()
        if ix is None:
            raise CadError("the gazetteer is not loaded: addresses can't be looked up (try again shortly)")
        town_of = self.cad.lists.town_of
        today = datetime.fromtimestamp(self.store.clock(), TZ).strftime("%Y-%m-%d")
        L = self.cad.lists
        known = {x.lower() for x in (*L.geo_town_order, *L.geo_town_aliases.values())}
        p = commonplaces.plan(rows, cols, lambda a, t: commonplaces.locate_address(ix, a, t, town_of), town_of, today, cur,
                              known)
        if p["counts"]["names"] > MAX_COMMON_NAMES:
            raise CadError(f"at most {MAX_COMMON_NAMES} names")
        out = {"ok": True, "header": rows[0], "columns": cols, "counts": p["counts"], "source": commonplaces.SOURCE,
               "unresolved": p["unresolved"][:300], "unknown_towns": p["unknown_towns"][:50]}
        if action == "preview":
            return dict(out, dry_run=True)
        if not p["entries"]:
            raise CadError("no names in the file")
        changes = [("places", k, REMOVE) for k in cur if k not in p["entries"]]
        changes += [("places", k, v) for k, v in p["entries"].items()]
        c = p["counts"]
        self._commit(changes, op, [("lists", f"place names/{commonplaces.SOURCE}", {"names": len(cur)},
                                    {k: c[k] for k in ("rows", "names", "new", "present", "removed", "unresolved")})])
        return out

    def _towns_op(self, action: str, body: dict, op: str) -> dict:
        cur, base = list(self.cad.lists.geo_town_order), list(self.base.geo_town_order)
        if action == "reset_section":
            return self._commit([("geo_town_order", "_list", REMOVE)], op, [("lists", "geo_town_order", cur, base)])
        if action != "set":
            raise CadError("action: set or reset_section")
        towns = body.get("towns")
        if not isinstance(towns, list) or len(towns) > 40:
            raise CadError("towns: a list (up to 40)")
        towns = [_text(t, "town", 60) for t in towns]
        if len({t.lower() for t in towns}) != len(towns):
            raise CadError("towns: each town once")
        return self._commit([("geo_town_order", "_list", REMOVE if towns == base else towns)], op,
                            [("lists", "geo_town_order", cur, towns)])

    # -- units ------------------------------------------------------------------------------------------

    def _layout(self) -> list[dict]:
        return [{"id": b["id"], "title": b["title"], "units": list(b["units"]), **lists_mod.board_meta(b)}
                for b in self.cad.lists.unit_boards]

    def _board_of(self, layout: list[dict], code: str) -> dict | None:
        return next((b for b in layout if code in b["units"]), None)

    def _layout_change(self, layout: list[dict]):
        base = [{"id": b["id"], "title": b["title"], "units": list(b["units"]), **lists_mod.board_meta(b)}
                for b in self.base.unit_boards]
        return REMOVE if layout == base else layout

    def _default_board(self, kind: str, layout: list[dict]) -> str:
        """Where a unit goes with no board said: other agencies' kinds on "agencies", the rest on "main"."""
        want = "agencies" if kind in self.cad.lists.attach_kinds else "main"
        return want if any(b["id"] == want for b in layout) else layout[0]["id"]

    def _unit_code(self, raw: str, known) -> str:
        """The unit code as the department wrote it (242_eNG, 239_Cascd): exact, else the one that
        matches in any case; a code not known yet is upper case (a new unit)."""
        if raw in known:
            return raw
        return next((c for c in known if c and c.upper() == raw.upper()), raw.upper())

    def _clean_unit(self, raw, cur: dict | None = None) -> dict:
        """A unit as edited: {kind, spoken, title, department, spare, response} (missing: as it was)."""
        if not isinstance(raw, dict):
            raise CadError("unit: an object")
        cur, L = cur or {}, self.cad.lists
        g = lambda k: raw[k] if k in raw else cur.get(k)
        kind = g("kind") or "engine"
        if kind not in L.kind_ids:
            raise CadError(f"kind: one of {', '.join(L.kind_ids)}")
        out = {"kind": kind}
        for k, n in (("spoken", 40), ("title", 60), ("department", 40)):
            v = _text(g(k), k, n, required=False)
            if v:
                out[k] = v
        if g("spare"):
            out["spare"] = True
        st = _text(g("station"), "station", 80, required=False)      # home firehouse (the maps)
        if st:
            names = [f["name"] for f in getattr(self.cad, "firehouses", [])]
            if st not in names:
                raise CadError(f"station: one of {', '.join(names)} (or empty: not set)")
            out["station"] = st
        r = raw.get("response") or lists_mod.unit_response(cur if cur else {"kind": kind}, L.attach_kinds)
        if r not in RESPONSES:
            raise CadError(f"response: one of {', '.join(RESPONSES)}")
        out["response"] = r
        ss = _text(g("status_set"), "status_set", 40, required=False)   # another agency's: its statuses (third party ...)
        if ss and r == "agency":
            sets = L.agency_status_sets["sets"]
            if ss not in sets:
                raise CadError(f"status_set: one of {', '.join(sets)}")
            out["status_set"] = ss
        return out

    def _same_unit(self, a: dict | None, b: dict | None, code: str) -> bool:
        """The same unit, however it is written (response given or from the flags, spoken = code)."""
        def k(u):
            u = u or {}
            return (u.get("kind", "agency"), (u.get("spoken") or code) if u.get("spoken") != code else code,
                    u.get("title") or "", u.get("department") or "", bool(u.get("spare")), bool(u.get("retired")),
                    u.get("station") or "", u.get("status_set") or "",
                    lists_mod.unit_response(u, self.cad.lists.attach_kinds))
        return k(a) == k(b)

    def _units_op(self, action: str, body: dict, op: str) -> dict:
        L, ov = self.cad.lists, self.overrides()
        o = ov.get("units", {})
        layout = self._layout()
        live = {u["name"]: u for u in self.store.units()}
        if action == "reset_section":
            return self._commit([("units", k, REMOVE) for k in o], op, [("units", "units", f"{len(o)} edits", "reset to the defaults")])
        known = {c for b in layout for c in b["units"] if c} | set(L.units) | {k for k in o if not k.startswith("_")} | set(live)
        code = self._unit_code(_text(body.get("code"), "code", 24), known)
        on_board = self._board_of(layout, code)
        cur = L.units.get(code)
        base = self.base.units.get(code)
        base_known = base is not None or any(code in b["units"] for b in self.base.unit_boards)

        def def_change(new: dict):
            return REMOVE if base_known and self._same_unit(new, base, code) else new

        if action == "add":
            if on_board or cur is not None or code in live:
                raise CadError(f"{code} is already a unit" + (" (retired: restore it)" if cur and cur.get("retired") else ""))
            if not CODE.fullmatch(code):
                raise CadError(f"code: {code!r}: capital letters, digits and - _ (up to 24)")
            u = self._clean_unit(body.get("unit"))
            if not u.get("spoken"):
                u["spoken"] = code
            board = str((body.get("unit") or {}).get("board") or self._default_board(u["kind"], layout))
            b = next((x for x in layout if x["id"] == board), None)
            if b is None:
                raise CadError(f"board: one of {', '.join(x['id'] for x in layout)}")
            b["units"].append(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, None, dict(u, board=board))])
        if cur is None and not on_board and not (action == "reset" and code in o):
            raise CadError(f"no unit {code!r}")
        # A board-only code (CONED-G, KEYHOLDER1, BAT10 ...: no units entry) is what the lists make of it, another agency's:
        # an edit that sends one field never makes it an engine (_clean_unit's default for a new unit), so never ours.
        cur = dict(cur or ({"kind": L.unit(code)["kind"]} if on_board or code in live else {}))
        if action == "station":                         # home firehouse only (the maps' station picker)
            old = L.station(code)
            u = self._clean_unit({"station": body.get("station")}, cur)
            if u.get("station", "") == old:
                return {"ok": True}
            if cur.get("retired"):
                u["retired"] = True
            return self._commit([("units", code, def_change(u))], op, [("units", f"{code}/station", old, u.get("station", ""))])
        if action == "edit":
            raw = body.get("unit") or {}
            if isinstance(raw, dict) and raw.get("code") not in (None, code):
                raise CadError("unit codes don't change (they are on events and in the exports): retire it and add a new one")
            u = self._clean_unit(raw, cur)
            if cur.get("retired"):
                u["retired"] = True
            changes = [("units", code, def_change(u))]
            if u["response"] == "agency":
                cards = self._cards_naming(code)
                if cards:
                    raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: "
                                   "another agency's unit is never on a run card; take it off first")
            board = raw.get("board") if isinstance(raw, dict) else None
            if board and on_board and board != on_board["id"]:
                b = next((x for x in layout if x["id"] == board), None)
                if b is None:
                    raise CadError(f"board: one of {', '.join(x['id'] for x in layout)}")
                on_board["units"].remove(code)
                b["units"].append(code)
                changes.append(("units", "_boards", self._layout_change(layout)))
            return self._commit(changes, op, [("units", code, _norm(cur), dict(u, **({"board": board} if board else {})))])
        if action == "move":
            if not on_board:
                raise CadError(f"{code} is not on a board")
            us = on_board["units"]
            i = us.index(code)
            step = _dir(body)
            j = i + step
            while 0 <= j < len(us) and us[j] is None:
                j += step
            if not 0 <= j < len(us):
                return {"ok": True}
            us[i], us[j] = us[j], us[i]
            return self._commit([("units", "_boards", self._layout_change(layout))], op,
                                [("units", f"{code}/order", f"{i + 1}", f"{j + 1}")])
        if action == "retire":
            lv = live.get(code) or {}
            if lv.get("incident_id"):
                raise CadError(f"{code} is on {lv['incident_number']}: clear it first")
            cards = self._cards_naming(code)
            if cards:
                raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: take it off first")
            u = dict(_norm(cur), retired=True, board=on_board["id"] if on_board else self._default_board(cur.get("kind", "agency"), layout))
            if on_board:
                on_board["units"].remove(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, "on " + (on_board["id"] if on_board else "no board"), "retired")])
        if action == "restore":
            if not cur.get("retired"):
                raise CadError(f"{code} is not retired")
            raw_board = (o.get(code) or {}).get("board") or self._default_board(cur.get("kind", "agency"), layout)
            b = next((x for x in layout if x["id"] == raw_board), None) or next(
                x for x in layout if x["id"] == self._default_board(cur.get("kind", "agency"), layout))
            b["units"].append(code)
            u = {k: v for k, v in _norm(cur).items() if k not in ("retired", "board")}
            return self._commit([("units", code, def_change(u)), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, "retired", f"on {b['id']}")])
        if action == "delete":
            lv = live.get(code)
            n = self.usage("units").get(code, 0)
            if n:
                raise CadError(f"can't delete {code}: in {n} record{'s' if n != 1 else ''} (events, unit history). "
                               "Retire it instead: it stays on old events, off the boards")
            cards = self._cards_naming(code)
            if cards:
                raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: take it off first")
            if on_board:
                on_board["units"].remove(code)

            def drop_row(db):
                if lv:
                    db.execute("DELETE FROM units WHERE id = ?", (lv["id"],))
            return self._commit([("units", code, {"deleted": True} if base_known else REMOVE),
                                 ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, _norm(cur), "deleted")], extra_sql=drop_row)
        if action == "reset":
            if code not in o:
                raise CadError(f"{code} is already the default")
            if not base_known:
                return self._units_op("delete", body, op)
            changes = [("units", code, REMOVE)]
            if "_boards" in o:
                for b in layout:
                    if code in b["units"]:
                        b["units"].remove(code)
                home = next((b for b in self.base.unit_boards if code in b["units"]), None)
                if home:
                    tgt = next((b for b in layout if b["id"] == home["id"]), None)
                    if tgt is not None:
                        tgt["units"].insert(min(home["units"].index(code), len(tgt["units"])), code)
                changes.append(("units", "_boards", self._layout_change(layout)))
            return self._commit(changes, op, [("units", code, o[code], "default")])
        raise CadError("action: add, edit, move, retire, restore, delete, reset or reset_section")

    # -- dispatch -----------------------------------------------------------------------------------------

    def _tones(self, v) -> list[list[list[float]]]:
        """Tone groups (or one flat group), each [Hz, seconds]; saved as groups."""
        if not isinstance(v, list):
            raise CadError("tones: a list of [Hz, seconds], or of such groups")
        flat = all(isinstance(t, (list, tuple)) and len(t) == 2 and all(isinstance(x, (int, float)) for x in t) for t in v)
        groups = [v] if flat and v else v
        if len(groups) > MAX_GROUPS:
            raise CadError(f"tones: up to {MAX_GROUPS} groups (one per department paged)")
        out = []
        for gi, g in enumerate(groups):
            if not isinstance(g, list) or not 1 <= len(g) <= MAX_TONES:
                raise CadError(f"tones: group {gi + 1}: 1 to {MAX_TONES} tones")
            grp = []
            for i, t in enumerate(g):
                if not (isinstance(t, (list, tuple)) and len(t) == 2
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in t)):
                    raise CadError(f"tones: group {gi + 1}, tone {i + 1}: [Hz, seconds]")
                hz, sec = float(t[0]), float(t[1])
                if not (50 <= hz <= 4000 and 0 < sec <= 10):
                    raise CadError(f"tones: group {gi + 1}, tone {i + 1}: 50 to 4000 Hz, for up to 10 s")
                grp.append([hz, sec])
            if sum(x for _, x in grp) > 20:
                raise CadError(f"tones: group {gi + 1}: 20 s in all at most")
            out.append(grp)
        return out

    def _channels(self, v) -> list[str]:
        if not isinstance(v, list) or not 1 <= len(v) <= MAX_CHANNELS:
            raise CadError(f"channels: 1 to {MAX_CHANNELS} labels")
        out = [_text(c, "channel", 40) for c in v]
        if len(set(out)) != len(out):
            raise CadError("channels: each label once")
        return out

    def _dispatch_op(self, action: str, body: dict, op: str) -> dict:
        ov = self.overrides()
        L = self.cad.lists
        if action == "preview":
            return self.preview(body.get("wording") or {})
        if action == "voice_preview":
            return self.cad.voice_preview()
        if action == "reset_section":
            ch = [("dispatch_wording", k, REMOVE) for k in ov.get("dispatch_wording", {})]
            ch += [("dispatch_tones", k, REMOVE) for k in ov.get("dispatch_tones", {})]
            ch += [("dispatch", k, REMOVE) for k in ov.get("dispatch", {})]
            return self._commit(ch, op, [("dispatch", "dispatch", f"{len(ch)} edits", "reset to the defaults")])
        key = _text(body.get("key"), "key", 80)
        if key.startswith("lead_in_joint_only/") or key.startswith("tones/"):
            what, _, town = key.partition("/")
            if town not in L.joint_response:
                raise CadError(f"{town!r} is not a joint-response department")
            if what == "tones":
                cur = [list(x) for x in L.dispatch_tones[town]] if L.dispatch_tones.get(town) else None
                if action == "set":
                    v = body.get("value")
                    v = None if v is None else self._tones(v)
                    if v is not None and len(v) != 1:
                        raise CadError("tones: one group for a department")
                    v = v[0] if v else None
                    base = [list(x) for x in self.base.dispatch_tones[town]] if self.base.dispatch_tones.get(town) else None
                    return self._commit([("dispatch_tones", town, REMOVE if v == base else v)], op,
                                        [("dispatch", f"tones/{town}", cur, v)])
                if action == "reset":
                    return self._commit([("dispatch_tones", town, REMOVE)], op, [("dispatch", f"tones/{town}", cur, "default")])
            else:
                only = dict(L.lead_in_joint_only)
                if action == "set":
                    only[town] = _text(body.get("value"), "lead-in", 80)
                elif action == "reset":
                    only[town] = self.base.lead_in_joint_only.get(town, "")
                    only = {t: v for t, v in only.items() if v}
                else:
                    raise CadError("action: set or reset")
                same = only == dict(self.base.lead_in_joint_only)
                return self._commit([("dispatch_wording", "lead_in_joint_only", REMOVE if same else only)], op,
                                    [("dispatch", key, L.lead_in_joint_only.get(town), only.get(town))])
        elif key in ("voice", "speed"):
            dflt = self.config_voice[0 if key == "voice" else 1]
            cur = getattr(self.cad.s, key, dflt)
            if action == "set":
                v = body.get("value")
                if key == "speed":
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.5 <= v <= 2.0:
                        raise CadError("speed: 0.5 to 2.0")
                    v = round(float(v), 2)
                elif getattr(self.cad.s, "tts_engine", "say") in ("kokoro", "polly"):
                    if v not in self._voice_ids():
                        raise CadError(f"voice: one of {', '.join(self._voice_ids()[:4])}, ...")
                else:
                    v = _text(v, "voice", 60)
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, dflt)])
        elif key in dict(WORDING):
            if action == "set":
                v = _text(body.get("value"), key, 400)
                return self._commit([("dispatch_wording", key, REMOVE if v == self.base.wording[key] else v)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], v)])
            if action == "reset":
                return self._commit([("dispatch_wording", key, REMOVE)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], self.base.wording[key])])
        elif key in eta.KEYS:
            cur, dflt = self.cad.eta.as_dict()[key], eta.Params().as_dict()[key]
            if action == "set":
                try:
                    v = eta.check(key, body.get("value"), L.free)
                except ValueError as e:
                    raise CadError(str(e)) from None
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", f"eta/{key}", cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", f"eta/{key}", cur, dflt)])
        elif key == "weather_speak":                  # an NWS warning said once by the dispatch voice (#132)
            cur = bool((ov.get("dispatch") or {}).get("weather_speak", False))
            if action == "set":
                v = body.get("value")
                if not isinstance(v, bool):
                    raise CadError("weather_speak: true or false")
                return self._commit([("dispatch", key, v if v else REMOVE)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, False)])
        elif key in ("tones", "channels"):
            cur = tone_groups(self.cad.s.tones) if key == "tones" else list(self.cad.s.channels)
            dflt = self.config_tones if key == "tones" else list(self.config_channels)
            if action == "set":
                v = self._tones(body.get("value")) if key == "tones" else self._channels(body.get("value"))
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, dflt)])
        else:
            raise CadError(f"key: a template ({', '.join(k for k, _ in WORDING)}), tones, tones/<town>, "
                           f"lead_in_joint_only/<town>, channels, voice, speed, weather_speak or {', '.join(eta.KEYS)}")
        raise CadError("action: set, reset, reset_section, preview or voice_preview")

    def _voice_ids(self) -> list[str]:
        """The voices the engine set in the config offers (Polly's: men first)."""
        engine = getattr(self.cad.s, "tts_engine", "say")
        return list(polly.VOICES) if engine == "polly" else list(KOKORO_VOICES) if engine == "kokoro" else []

    def preview(self, wording: dict) -> dict:
        """The dispatch wording on a sample event, as it would be said (templates not saved)."""
        if not isinstance(wording, dict):
            raise CadError("wording: an object")
        L = self.cad.lists
        w, errors = dict(L.wording), {}
        dummy = dict.fromkeys(("department", "units", "location", "type", "type_bare", "already", "info", "change",
                               "respond", "time", "full", "Full", "status", "remarks", "joint"), "x")
        for k, v in wording.items():
            if k not in dict(WORDING) or not isinstance(v, str):
                continue
            try:
                v.format(**dummy)
            except (KeyError, IndexError, ValueError) as e:
                errors[k] = f"unknown or broken placeholder ({e})"
                continue
            if v.strip():
                w[k] = v
        inc = {"address": "209 Westchester Ave", "cross_street": "Pearl St"}
        at = self.store.clock()
        dep = self.cad.s.department
        alarm, stru = L.phrase("ALARM", "COMM"), L.phrase("STRU")
        only = dict(L.lead_in_joint_only)
        if isinstance(wording.get("lead_in_joint_only"), dict):
            only.update({k: v for k, v in wording["lead_in_joint_only"].items() if isinstance(v, str) and v.strip()})
        joint = next(iter(L.joint_response), "")

        def c(kind, lead=None, **kw):
            lead = lead or dispatcher.lead_in(dep, [], True, False, w, only)
            return dispatcher.compose(kind, department=lead, incident=inc, at=at, wording=w, **kw)
        try:
            out = {"new": c("new", units=["Engine 60", "Ladder 31", "Rescue 40", "23 98"], phrase=stru,
                            remarks="smoke showing from the second floor"),
                   "add_on": c("add_on", units=["Engine 61"], phrase=alarm,
                               already=[("responding", ["Engine 60"]), ("on location", ["Ladder 31", "23 98"])]),
                   "upgrade": c("upgrade", units=[], phrase=alarm, info="smoke showing from the second floor",
                                new_phrase=stru, full=True),
                   "full": c("new", units=["Engine 60"], phrase=stru, full=True)}
            if joint:
                out["joint"] = c("new", lead=dispatcher.lead_in(dep, [joint], True, False, w, only),
                                 units=["Engine 60", "Engine 14", "Ladder 30"], phrase=stru)
                out["joint_only"] = c("new", lead=dispatcher.lead_in(dep, [joint], False, False, w, only),
                                      units=["Engine 14"], phrase=alarm)
        except (KeyError, IndexError, ValueError) as e:
            out = {}
            errors["_"] = f"can't render: {e}"
        return {"texts": out, "errors": errors,
                "sample": "Sample: 209 Westchester Ave, cross of Pearl St; a structure fire (first dispatch with remarks, "
                          "full response, with a joint department) and a commercial fire alarm (add-on, upgrade, only the "
                          "joint department's units)."}

    # -- radios: channels (record / transcribe / engine) and speech-to-text (sizeup/channels.py) ---------------

    def channel_overrides(self) -> dict:
        return self.section(channels_mod.SECTION)

    def _channel_value(self, key: str, cur: dict, vals: dict):
        """The override to store for key: only what differs from the default (REMOVE: nothing does)."""
        dflt = channels_mod.defaults(key, self._aws_channels(), self._record_only(), self._context())
        new = {k: v for k, v in {**cur, **vals}.items() if k in channels_mod.FIELDS and v != dflt.get(k)}
        if channels_mod.is_tg(key):
            new.pop("record", None)
        return new or REMOVE

    def _aws_channels(self) -> tuple:
        ctx = self.cad.radio_ctx
        return tuple(ctx.aws_channels()) if ctx is not None else channels_mod.DEFAULT_AWS

    def _context(self):
        """context(key): a context or neighbour talkgroup, its default Auto · background (#896, App.context_tg)."""
        return getattr(self.cad.radio_ctx, "context_tg", None)

    def _record_only(self) -> tuple:
        """[stt] record_only (#201): recorded, not transcribed unless a choice here says otherwise."""
        ctx = self.cad.radio_ctx
        f = getattr(ctx, "record_only", None)
        return tuple(f()) if callable(f) else channels_mod.DEFAULT_RECORD_ONLY

    ENGINE_MIGRATION = "channels_engine_896"     # (cad meta) the one-time engine migration (#896) ran: when, what

    def migrate_engines(self, op: str = "system: engine selector (#896)", context_keys=()) -> dict | None:
        """The one-time migration of #896, on the CAD of record's start: every channel's engine is the only thing that
        decides where its audio goes. A stored "aws" (AWS, with a GPU worker first wherever the pool's class switch
        was on) becomes "pin:aws" (AWS; a GPU worker only when AWS can't take a clip); a channel on the default
        (the [stt] aws_channels: "aws" before, "pin:aws" now) is noted the same way. "local" is the channel's own
        station pinned and "both" Compare already: unchanged. Each channel audited (Settings > Radios, operator op),
        the counts kept in cad meta. Idempotent: once it ran (the meta says so), nothing. A context or neighbour talkgroup
        on its station's whisper (context_keys: the talkgroups known now; a stored "local", or the default) goes to
        Auto · background, its default now (channels.defaults). -> {migrated, default, background, background_default}
        the first time, else None."""
        store = self.cad.store
        if store.meta_get(self.ENGINE_MIGRATION):
            return None
        ov = self.channel_overrides()
        aws = self._aws_channels()
        changes, audit = [], []
        stored = sorted(k for k, v in ov.items() if isinstance(v, dict) and v.get("engine") == "aws")
        for key in stored:
            new = self._channel_value(key, {k: v for k, v in ov[key].items() if k != "engine"},
                                      {"engine": channels_mod.PIN_AWS})
            changes.append((channels_mod.SECTION, key, new))
            audit.append(("radios", f"channel/{key}", {"engine": "aws"}, {"engine": channels_mod.PIN_AWS}))
        default = sorted(k for k in aws if not (isinstance(ov.get(k), dict) and "engine" in ov[k]))
        for key in default:
            audit.append(("radios", f"channel/{key}", {"engine": "aws (default)"}, {"engine": "pin:aws (default)"}))
        ctx = sorted({k for k in context_keys if isinstance(k, str) and channels_mod.is_tg(k) and k not in aws})
        bg = [k for k in ctx if isinstance(ov.get(k), dict) and ov[k].get("engine") == "local"]
        for key in bg:                            # (Auto · background is its default now: the engine's override goes)
            new = self._channel_value(key, {k: v for k, v in ov[key].items() if k != "engine"}, {"engine": "auto_bg"})
            changes.append((channels_mod.SECTION, key, new))
            audit.append(("radios", f"channel/{key}", {"engine": "local"}, {"engine": "auto_bg"}))
        bg_default = [k for k in ctx if not (isinstance(ov.get(k), dict) and "engine" in ov[k])]
        for key in bg_default:
            audit.append(("radios", f"channel/{key}", {"engine": "local (default)"}, {"engine": "auto_bg (default)"}))
        out = {"migrated": stored, "default": default, "background": bg, "background_default": bg_default,
               "at": time.time()}
        mark = (lambda db: db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                      "value = excluded.value", (self.ENGINE_MIGRATION, json.dumps(out))))
        if audit:                                 # (the overrides, their audit and the mark: one transaction)
            self._commit(changes, op, audit, extra_sql=mark)
        else:
            store.meta_put(self.ENGINE_MIGRATION, json.dumps(out))
        return out

    def _channels_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, record?, transcribe?, engine?} | set_many {keys, transcribe?, engine?} | reset {key}.
        A talkgroup's record goes to its own Record switch (the station's TalkgroupPrefs)."""
        ctx = self.cad.radio_ctx
        locked = ctx.channels_locked() if ctx is not None else ""
        if locked:
            raise CadError(locked)
        ov = self.channel_overrides()
        aws, ro = self._aws_channels(), self._record_only()
        if action in ("set", "set_many"):
            keys = [body.get("key")] if action == "set" else body.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 2000:
                raise CadError("keys: a list of channels")
            if body.get("engine") == "aws":           # (#896: AWS is pinned AWS; "aws" is never stored again)
                body = {**body, "engine": channels_mod.PIN_AWS}
            try:
                vals = {k: channels_mod.clean(k, {f: body.get(f) for f in channels_mod.FIELDS}) for k in keys}
            except ValueError as e:
                raise CadError(str(e)) from None
            changes, audit, applied = [], [], []
            # A talkgroup's Record first, all of them (its own switch, on the station): one refused stops the rest,
            # and those already applied are on the record (audited) before the refusal is said
            for key, v in vals.items():
                if channels_mod.is_tg(key) and "record" in v:
                    rec = v.pop("record")
                    tsys, tgn = channels_mod.tg_of(key)       # (#409) another system's: with its system
                    err = "no talkgroups here" if ctx is None else (
                        ctx.set_talkgroup(tgn, rec, None, None, tsys) if tsys else ctx.set_talkgroup(tgn, rec))
                    if err:
                        if applied:
                            self._commit([], op, applied)
                        raise CadError(f"{key}: {err}" + (f" ({len(applied)} before it applied)" if applied else ""))
                    applied.append(("radios", f"channel/{key}", None, {"record": rec}))
            for key, v in vals.items():
                if not v:
                    continue
                cur = ov.get(key) or {}
                new = self._channel_value(key, cur, v)
                before = channels_mod.pref(key, ov, aws, ro, self._context())
                after = channels_mod.pref(key, {key: {} if new is REMOVE else new}, aws, ro, self._context())
                if before != after:
                    changes.append((channels_mod.SECTION, key, new))
                    audit.append(("radios", f"channel/{key}", {k: before[k] for k in v}, {k: after[k] for k in v}))
            if not changes and not applied:
                return {"ok": True}
            out = self._commit(changes, op, applied + audit)
        elif action == "reset":
            key = _text(body.get("key"), "key", 40)
            if key not in ov:
                return {"ok": True}
            out = self._commit([(channels_mod.SECTION, key, REMOVE)], op,
                               [("radios", f"channel/{key}", ov[key], "default")])
        else:
            raise CadError("action: set, set_many or reset")
        if ctx is not None:
            ctx.channels_changed()
        return out

    # -- an FM channel's squelch (Settings > Radios > Squelch; sizeup/channels.py, sizeup/radio/fm.py) --------------

    def squelch_overrides(self) -> dict:
        return self.section(channels_mod.SQUELCH_SECTION)

    def _squelch_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, open_db?, close_db?, min_open_s?} (null: back to the receiver's default; expect {open_db, close_db}:
        the squelch the suggestion was worked out for: refused unless that is what runs now and, on the hub, the
        channel's suggestion still offers Apply for it: a suggestion applied from Settings > Radios > Squelch, #606,
        #686) | reset {key}. Audited. A paging channel: Open / Close only, guarded (_paging_squelch_set)."""
        ctx = self.cad.radio_ctx
        locked = ctx.channels_locked() if ctx is not None else ""
        if locked:
            raise CadError(locked)
        ov = self.squelch_overrides()
        key = body.get("key")
        dflt = ctx.squelch_defaults(key) if ctx is not None and isinstance(key, str) else {}
        if action == "set":
            if self._squelch_editable(key) == "paging":
                out = self._paging_squelch_set(key, body, op, ov, dflt)
                if ctx is not None:
                    ctx.channels_changed()
                return out
            exp = body.get("expect")
            if exp is not None:                       # #606: applied from a suggestion worked out for what ran then
                rows = {c["key"]: c for c in ctx.squelch_state()["channels"]} if ctx is not None else {}
                run = (rows.get(key) or {}).get("running") or {}
                if not isinstance(exp, dict) or any(not isinstance(exp.get(f), (int, float)) or isinstance(exp.get(f), bool)
                                                    or run.get(f) is None or abs(float(run[f]) - float(exp[f])) > 0.01
                                                    for f in ("open_db", "close_db")):
                    raise CadError(f"{key} now runs Open {run.get('open_db')} / Close {run.get('close_db')}: "
                                   "not what the suggestion was for, look again")
                # ... and the suggestion itself still offers it, for that squelch (#686: the hub's own check)
                check = getattr(getattr(self.cad, "station_metrics_insight", None), "applicable", None)
                why = check(key, exp, {f: body.get(f) for f in ("open_db", "close_db")}) if callable(check) else ""
                if why:
                    raise CadError(why)
            try:
                vals = channels_mod.clean_squelch(key, {f: body.get(f) for f in channels_mod.SQUELCH})
            except ValueError as e:
                raise CadError(str(e)) from None
            cur = dict(ov.get(key) or {})
            for f in channels_mod.SQUELCH:
                if f in body and body[f] is None:
                    cur.pop(f, None)
            new = {k: v for k, v in {**cur, **vals}.items() if v != dflt.get(k)}
            eff = {**dflt, **new}
            if eff.get("open_db") is not None and eff.get("close_db") is not None and eff["close_db"] >= eff["open_db"]:
                raise CadError(f"close {eff['close_db']:g} dB: under open ({eff['open_db']:g} dB)")
            if new == (ov.get(key) or {}):
                return {"ok": True}
            before = {**dflt, **(ov.get(key) or {})}
            out = self._commit([(channels_mod.SQUELCH_SECTION, key, new or REMOVE)], op,
                               [("radios", f"squelch/{key}", before, eff)])
            ins = getattr(self.cad, "station_metrics_insight", None)   # its suggestion again (#606)
            if ins is not None:
                ins.forget(key)
        elif action == "reset":
            key = _text(key, "key", 40)
            if key not in ov:
                return {"ok": True}
            out = self._commit([(channels_mod.SQUELCH_SECTION, key, REMOVE)], op,
                               [("radios", f"squelch/{key}", {**dflt, **ov[key]}, "default")])
            ins = getattr(self.cad, "station_metrics_insight", None)   # its suggestion again (#606)
            if ins is not None:
                ins.forget(key)
        else:
            raise CadError("action: set or reset")
        if ctx is not None:
            ctx.channels_changed()
        return out

    def _squelch_editable(self, key) -> str:
        """A traffic channel's squelch is set here, or a paging channel's Open / Close (_paging_squelch_set: guarded),
        one the station has reported (with its own defaults). -> its kind."""
        ctx = self.cad.radio_ctx
        rows = {c["key"]: c for c in ctx.squelch_state()["channels"]} if ctx is not None else {}
        row = rows.get(key) if isinstance(key, str) else None
        if row is None:
            ed = [k for k, c in rows.items() if c.get("editable")]
            raise CadError(f"key: one of {', '.join(ed)}" if ed else "no FM channel's squelch reported yet")
        if not row.get("editable"):
            raise CadError(f"{key}: {row.get('kind') or 'not a traffic'} channel: its squelch is read-only")
        return str(row.get("kind") or "")

    def _paging_guard(self, key: str, open_db: float, dflt: dict) -> dict:
        """A paging channel's Open checked against what it hears (sizeup/cloud/metricsinsight.py paging_levels, the
        hub's signal metrics): at least PAGING_IDLE_GAP_DB over the recent idle max (under it the squelch opens on the
        idle noise), at most PAGE_GAP_DB under the recent pages' median (over it weak pages go unheard). Without idle
        data: never under the receiver's default; without pages: at most PAGING_BLIND_RAISE_DB over it. CadError,
        else {idle_max, page_db, pages}."""
        ins = getattr(self.cad, "station_metrics_insight", None)
        try:
            lv = (ins.paging_levels(key) if ins is not None else None) or {}
        except Exception:  # noqa: BLE001 - no numbers: the blind limits below
            lv = {}
        idle, page = lv.get("idle_max"), lv.get("page_db")
        base = float(dflt.get("open_db") or 10.0)
        if idle is not None and open_db < idle + PAGING_IDLE_GAP_DB:
            raise CadError(f"{key}: Open {open_db:g} dB is under the idle max {idle:g} + {PAGING_IDLE_GAP_DB:g} dB: "
                           "it would open on the noise")
        if idle is None and open_db < base:
            raise CadError(f"{key}: no idle numbers to check a lower Open against (default {base:g} dB)")
        if page is not None and open_db > page - metricsinsight.PAGE_GAP_DB:
            raise CadError(f"{key}: Open {open_db:g} dB is within {metricsinsight.PAGE_GAP_DB:g} dB of the pages "
                           f"({page:g} dB): pages could be missed")
        if page is None and open_db > base + PAGING_BLIND_RAISE_DB:
            raise CadError(f"{key}: no recent pages to check against: Open {base + PAGING_BLIND_RAISE_DB:g} dB at most")
        return {"idle_max": idle, "page_db": page, "pages": int(lv.get("pages") or 0)}

    def _paging_squelch_set(self, key: str, body: dict, op: str, ov: dict, dflt: dict) -> dict:
        """A paging channel's squelch (admins, audited as Radios): Open, and Close (not given: Open less the default
        hysteresis), inside _paging_guard; open_db null: back to the default. Min stays the receiver's."""
        if body.get("min_open_s") is not None or body.get("expect") is not None:
            raise CadError(f"{key}: paging channel: Open and Close only")
        before = {**{f: dflt.get(f) for f in channels_mod.PAGING_FIELDS}, **(ov.get(key) or {})}
        if "open_db" in body and body["open_db"] is None:
            if key not in ov:
                return {"ok": True}
            out = self._commit([(channels_mod.SQUELCH_SECTION, key, REMOVE)], op,
                               [("radios", f"squelch/{key}", before, "default")])
        else:
            o = body.get("open_db", (ov.get(key) or {}).get("open_db"))
            if o is None:
                raise CadError(f"{key}: open_db: required")
            raw = {"open_db": o, "close_db": body.get("close_db")}
            try:
                vals = channels_mod.clean_squelch(key, {f: v for f, v in raw.items() if v is not None})
            except ValueError as e:
                raise CadError(str(e)) from None
            if "close_db" not in vals:
                hyst = float(dflt.get("open_db") or 10.0) - float(dflt.get("close_db") or 6.0)
                lo = channels_mod.SQUELCH["close_db"][0]
                vals["close_db"] = max(lo, vals["open_db"] - (hyst if hyst > 0 else 4.0))
            if vals["close_db"] >= vals["open_db"]:
                raise CadError(f"close {vals['close_db']:g} dB: under open ({vals['open_db']:g} dB)")
            guard = self._paging_guard(key, vals["open_db"], dflt)
            if vals == (ov.get(key) or {}):
                return {"ok": True, "guard": guard}
            out = self._commit([(channels_mod.SQUELCH_SECTION, key, vals)], op,
                               [("radios", f"squelch/{key}", before, dict(vals))])
            out = {**out, "guard": guard} if isinstance(out, dict) else out
        ins = getattr(self.cad, "station_metrics_insight", None)
        if ins is not None:
            ins.forget(key)
        return out

    def _import_squelch(self, sq: dict) -> tuple[dict, list[str]]:
        """An imported squelch section: invalid entries and a channel the station reports as paging (read-only)
        skipped; one no station has reported (a fresh hub, a channel gone) kept, left out of the push until a traffic
        channel by that name is there (channels.fit_squelch). -> (kept, notes)."""
        ctx = self.cad.radio_ctx
        rows = {c["key"]: c for c in ctx.squelch_state()["channels"]} if ctx is not None else {}
        kept, notes = {}, []
        for k, v in sq.items():
            try:
                c = channels_mod.clean_squelch(k, v)
            except ValueError as e:
                notes.append(f"squelch {k}: skipped ({e})")
                continue
            if not c:
                continue
            row = rows.get(k)
            if row is not None and not row.get("editable"):
                notes.append(f"squelch {k}: skipped ({row.get('kind') or 'not a traffic'} channel: read-only)")
                continue
            if row is not None and row.get("kind") == "paging":
                # a paging channel's Open / Close only, and only inside its guard (as on the card)
                c = {f: x for f, x in c.items() if f in channels_mod.PAGING_FIELDS}
                try:
                    if not c or "open_db" not in c:
                        raise CadError("Open / Close only")
                    self._paging_guard(k, c["open_db"], ctx.squelch_defaults(k) if ctx is not None else {})
                except CadError as e:
                    notes.append(f"squelch {k}: skipped (paging: {e})")
                    continue
            if row is None:
                notes.append(f"squelch {k}: kept, not applied until a traffic channel {k} is reported")
            kept[k] = c
        return kept, notes

    def replace_squelch(self, m: dict, operator: str) -> bool:
        """The whole squelch map as the hub sent it. True when anything changed."""
        cur = self.squelch_overrides()
        sec = channels_mod.SQUELCH_SECTION
        changes = [(sec, k, REMOVE) for k in cur if k not in m]
        changes += [(sec, k, v) for k, v in m.items() if cur.get(k) != v]
        if not changes:
            return False
        self._commit(changes, operator, [("radios", f"squelch/{k}", cur.get(k), None if v is REMOVE else v)
                                         for _s, k, v in changes])
        return True

    def squelch_page(self) -> dict:
        """GET /api/cad/squelch: each FM channel's squelch as set and as running, the noise floor, the short opens."""
        ctx = self.cad.radio_ctx
        return ctx.squelch_state() if ctx is not None else {"channels": [], "sync": {"state": "local", "detail": ""}}

    def replace_channels(self, prefs: dict, operator: str) -> bool:
        """The whole channel map as the hub sent it (the station applying the CAD of record's choices).
        True when anything changed."""
        cur = self.channel_overrides()
        changes = [(channels_mod.SECTION, k, REMOVE) for k in cur if k not in prefs]
        changes += [(channels_mod.SECTION, k, v) for k, v in prefs.items() if cur.get(k) != v]
        if not changes:
            return False
        self._commit(changes, operator, [("radios", f"channel/{k}", cur.get(k), None if v is REMOVE else v)
                                         for _s, k, v in changes])
        return True

    # -- tone pairs named from the console (#80) --------------------------------------------------------
    # Kept as overrides (section "tones", key "A/B" in Hz) so they are audited, exported and imported with
    # the rest, and sent to the station with the channel choices (the hub's set_channel_prefs job).

    def tone_overrides(self) -> dict:
        return self.section(TONES_SECTION)

    def tone_rows(self) -> list[dict]:
        """GET /api/cad/tones: each pair named here (not the edits to list rows or entities), who named it and when."""
        rows = self.store.q("SELECT key, value, updated_at, operator FROM settings_overrides WHERE section = ? ORDER BY key",
                            (TONES_SECTION,))
        return [dict(json.loads(r["value"]), key=r["key"], at=r["updated_at"], operator=r["operator"] or "") for r in rows
                if not tonelist.is_list_key(r["key"]) and not r["key"].startswith(tonelist.ENTITY)]

    @staticmethod
    def _clean_tone(v, where: str = "") -> tuple[str, dict]:
        """A pair {a_hz, b_hz, places, service, role?, confirmed?, page?, list?} (or #84's / #115's {name, ...}), a list
        row taken out {list, a_hz, b_hz, deleted}, or an entity {places, service, ours?, notes?, aliases?, merged_into?}
        checked -> (key, the value stored). CadError. sizeup/cad/tonelist.py clean()."""
        return tonelist.clean(v, where)

    def use_tone_list(self, codes, ours, unknown_log: Path | None = None) -> None:
        self.base_tones, self.base_ours, self.unknown_log = list(codes), set(ours), unknown_log

    def _locked(self) -> set[str]:
        """The names that are ours whatever is set here: PCFD's and Rye Brook's (the config's ours)."""
        return set(self.base_ours) | set(self.cad.s.monitor_departments)

    def _heard(self) -> list:
        """Every pair heard (tonelist.heard), kept until a page row or unknown_tones.jsonl changes."""
        r = self.store.q("SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM radio_traffic WHERE tones != ''")[0]
        try:
            st = self.unknown_log.stat() if self.unknown_log else None
            log = (st.st_size, st.st_mtime) if st else None
        except OSError:
            log = None
        sig = (r["n"], r["u"], log)
        if self._heard_cache[0] != sig:
            rows = self.store.q("SELECT id, start, channel, tones, voice_at FROM radio_traffic WHERE tones != ''")
            self._heard_cache = (sig, tonelist.heard(rows, self.unknown_log))
        return self._heard_cache[1]

    def tone_book(self, ov: dict | None = None) -> tonelist.Book:
        """The tone book (#122): the list read as entities (places + service) with their pairs, the overrides on it."""
        return tonelist.Book(self.base_tones, self.tone_overrides() if ov is None else ov, self._locked())

    def tone_plan(self, ov: dict | None = None):
        from sizeup.radio.tones import TonePlan
        return TonePlan(self.tone_book(ov).codes())

    def tone_choices(self, book: tonelist.Book | None = None) -> dict:
        """What the dialog and the unnamed list pick from: the entities (and the county's agencies), the places, the
        services."""
        book = book or self.tone_book()
        return {"choices": tonelist.entity_choices(book), "places": tonelist.places(book),
                "services": [[s, tonelist.LABEL[s]] for s in tonelist.SERVICES]}

    def tones_page(self) -> dict:
        """Settings > Radios > Tones: every entity with its pairs, the pairs heard that nobody's code matches, what the
        list's reading did (report)."""
        ov = self.tone_overrides()
        meta = {r["key"]: {"operator": r["operator"] or "", "at": r["updated_at"]} for r in
                self.store.q("SELECT key, operator, updated_at FROM settings_overrides WHERE section = ?", (TONES_SECTION,))}
        events = self._heard()
        book = self.tone_book(ov)
        from sizeup.radio.tones import TonePlan
        plan = TonePlan(book.codes())
        has_clip = getattr(self.cad, "has_clip", None)
        return {"entities": book.table(events, meta),
                "unnamed": tonelist.unnamed(events, plan, has_clip if callable(has_clip) else None),
                **self.tone_choices(book), "locked": sorted(self._locked()), "report": book.report(), "max": MAX_NAMED_TONES}

    def _list_row(self, key: str):
        return next((c for c in self.base_tones if tonelist.list_key(c) == key), None)

    def _tone_commit(self, changes: list, op: str, cur: dict) -> dict | None:
        changes = [c for c in changes if (c[2] is REMOVE and c[1] in cur) or (c[2] is not REMOVE and cur.get(c[1]) != c[2])]
        if not changes:
            return None
        return self._commit(changes, op, [("radios", f"tones/{k}", cur.get(k), None if v is REMOVE else v)
                                          for _s, k, v in changes])

    def _pair_value(self, book: tonelist.Book, key: str, cur: dict, places, service: str, role: str | None = None,
                    confirmed: bool | None = None, page: str = "") -> tuple[str, object]:
        """The override for a pair as it should be now (REMOVE when a list row is back to what the list says)."""
        p = book.pair(key)
        if p is None:
            raise CadError(f"no tone pair {key!r}")
        body = {"a_hz": p.a_hz, "b_hz": p.b_hz, "places": list(places), "service": service,
                "role": p.role if role is None else role}
        if p.origin != "named":
            body["list"] = p.list_name
        old = cur.get(key) or {}
        conf = old.get("confirmed") if confirmed is None else confirmed
        if conf is not None:
            body["confirmed"] = conf
        if p.origin == "named" and (page or old.get("page")):
            body["page"] = page or old["page"]
        k, val = self._clean_tone(body)
        orig = p.parsed
        if p.origin != "named" and orig is not None and self._as_listed(key, val, orig):
            return k, REMOVE
        return k, val

    def _as_listed(self, key: str, val: dict, orig) -> bool:
        """A list row's edit that says what the list says (no edit left)."""
        return (tonelist.ekey(val["places"], val["service"]) == tonelist.ekey(orig.places, orig.service)
                and val.get("role", "") == orig.role
                and val.get("confirmed", self._list_confirmed(key)) == self._list_confirmed(key))

    def _list_confirmed(self, key: str) -> bool:
        row = self._list_row(key)
        return row is not None and not row.name.endswith(" (est.)")

    def _entity_value(self, book: tonelist.Book, ekey: str, cur: dict, **change) -> tuple[str, object]:
        """The entity's override with these fields changed (REMOVE when nothing is left to keep)."""
        e = book.entities.get(ekey)
        old = dict(cur.get(tonelist.ENTITY + ekey) or {})
        if e is None and not old:
            raise CadError(f"no entity {ekey!r}")
        body = {k: v for k, v in old.items() if k not in ("places", "service")}
        body.update({"places": list(e.places if e else old["places"]), "service": e.service if e else old["service"]})
        for k, v in change.items():
            if v is None:
                body.pop(k, None)
            else:
                body[k] = v
        if e is not None and e.ours_locked:
            body.pop("ours", None)                # PCFD's / Rye Brook's: ours whatever is set
        k, val = self._clean_tone(body)
        return k, (REMOVE if set(val) <= {"places", "service"} else val)

    def _tones_op(self, action: str, body: dict, op: str) -> dict:
        """The tone book (#115, #122). set {a_hz, b_hz, places, service, role?, confirmed?, ours?, page?, list?}: a pair
        named or edited (page: the page it was heard on, read again; ours: its entity is ours) | move {key, places,
        service}: a pair to another entity (or a new one: split) | entity {key, ours?, notes?, aliases?} | merge {key,
        into: {places, service}}: an entity's pairs under another | delete {key}: forget a pair named here, take a list
        row out | reset {key}: a list row as the list has it, an entity as read | reread {page}: that page's recording
        through the tone decoder again (App.reread_tones; changes no setting)."""
        cur = self.tone_overrides()
        book = self.tone_book(cur)
        page, key = "", ""
        if action in ("delete", "reset"):
            key = _text(body.get("key"), "key", 200)
            row = self._list_row(key) if tonelist.is_list_key(key) else None
            if action == "reset" or row is None:
                if key not in cur:
                    raise CadError(f"no {'edit to' if action == 'reset' else 'named tones'} {key!r}")
                out = self._tone_commit([(TONES_SECTION, key, REMOVE)], op, cur)
            else:
                k, val = self._clean_tone({"list": key.split("@", 1)[1], "a_hz": row.a_hz, "b_hz": row.b_hz, "deleted": True})
                out = self._tone_commit([(TONES_SECTION, key, val)], op, cur)
        elif action == "set":
            key, val = self._clean_tone(body)
            if key not in cur and len(cur) >= MAX_NAMED_TONES:
                raise CadError(f"at most {MAX_NAMED_TONES} edited or named tone pairs")
            changes = []
            if "places" in val:
                ours = val.pop("ours", None)
                if "page" not in val and cur.get(key, {}).get("page") and not val.get("list"):
                    val["page"] = cur[key]["page"]        # an edit keeps the page it was named from
                row = self._list_row(key) if val.get("list") else None
                if row is not None and self._as_listed(key, val, tonelist.parse_name(val["list"])):
                    val = REMOVE                          # back to what the list says: no edit left
                changes.append((TONES_SECTION, key, val))
                ek = book.resolve(tonelist.ekey(body_places(body), body["service"]))
                if ours and ek not in book.locked_keys and not (book.entities.get(ek) and book.entities[ek].ours):
                    if ek in book.entities or tonelist.ENTITY + ek in cur:
                        changes.append((TONES_SECTION, *self._entity_value(book, ek, cur, ours=True)))
                    else:
                        changes.append((TONES_SECTION, *self._clean_tone({"places": body_places(body),
                                                                          "service": body["service"], "ours": True})))
            else:                                         # #84 / #115: by name, as before
                if val.get("ours") is False and val["name"] in self._locked():
                    val["ours"] = True                    # PCFD's / Rye Brook's name: ours whatever the box said
                if "page" not in val and cur.get(key, {}).get("page") and not val.get("list"):
                    val["page"] = cur[key]["page"]
                changes.append((TONES_SECTION, key, val))
            out = self._tone_commit(changes, op, cur)
            page = body.get("page") or "" if out else ""
        elif action == "move":
            key = _text(body.get("key"), "key", 200)
            places = body_places(body)
            k, val = self._pair_value(book, key, cur, places, tonelist._service(body.get("service"), "service"))
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "entity":
            key = _text(body.get("key"), "key", 200)
            change = {k: body[k] for k in ("ours", "notes", "aliases") if k in body}
            if change.get("notes") == "":
                change["notes"] = None
            if change.get("aliases") == []:
                change["aliases"] = None
            k, val = self._entity_value(book, key, cur, **change)
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "merge":
            key = _text(body.get("key"), "key", 200)
            into = body.get("into") if isinstance(body.get("into"), dict) else {}
            target = book.resolve(tonelist.ekey(body_places(into), tonelist._service(into.get("service"), "into.service")))
            if target == book.resolve(key):
                raise CadError("merge: into another entity")
            k, val = self._entity_value(book, key, cur, merged_into={"places": body_places(into), "service": into["service"]})
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "reread":
            pid = _text(body.get("page"), "page", 80)
            ctx = self.cad.radio_ctx
            if ctx is None or not hasattr(ctx, "reread_tones"):
                raise CadError("no radios here to read the page's tones again")
            try:
                return {"ok": True, **ctx.reread_tones(pid)}
            except ValueError as e:
                raise CadError(str(e)) from None
        else:
            raise CadError("action: set, move, entity, merge, delete, reset or reread")
        if out is None:
            return {"ok": True, "key": key}
        ctx = self.cad.radio_ctx
        if ctx is not None:
            ctx.tones_changed(page)
        return {**out, "key": key}

    def replace_tones(self, tones: dict, operator: str) -> bool:
        """The whole named-pairs map as the hub sent it (the station applying the CAD of record's names).
        True when anything changed. A bad entry: nothing changes (ValueError)."""
        want = {}
        for k, v in (tones or {}).items():
            try:
                key, val = self._clean_tone(v, f"tones.{k}")
            except CadError as e:
                raise ValueError(str(e)) from None
            want[key] = val
        cur = self.tone_overrides()
        changes = [(TONES_SECTION, k, REMOVE) for k in cur if k not in want]
        changes += [(TONES_SECTION, k, v) for k, v in want.items() if cur.get(k) != v]
        if not changes:
            return False
        self._commit(changes, operator, [("radios", f"tones/{k}", cur.get(k), None if v is REMOVE else v)
                                         for _s, k, v in changes])
        return True

    def stt_values(self) -> dict:
        return self.section("stt")

    def _vocab_list(self, v) -> list[str]:
        if not isinstance(v, list) or len(v) > MAX_VOCAB:
            raise CadError(f"vocabulary: a list of at most {MAX_VOCAB} phrases")
        out = []
        for i, x in enumerate(v):
            x = _text(x, f"vocabulary[{i}]", MAX_PHRASE_CHARS, required=False)
            x = " ".join(x.split())
            if x and x.lower() not in {y.lower() for y in out}:
                out.append(x)
        if sum(len(x.encode()) + 1 for x in out) > MAX_VOCAB_BYTES:
            raise CadError(f"vocabulary: at most {MAX_VOCAB_BYTES // 1000} KB in all (AWS's limit)")
        return out

    @staticmethod
    def _limit(v, key: str = "aws_monthly_limit_usd") -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10000:
            raise CadError(f"{key}: 0 to 10000 (dollars)")
        return round(float(v), 2)

    @staticmethod
    def _catchup_value(key: str, v):
        """catchup: true | false; catchup_max_age_h: hours (sizeup/sttcatchup.py MAX_AGE_RANGE_H)."""
        from sizeup.sttcatchup import MAX_AGE_RANGE_H
        if key == "catchup":
            if not isinstance(v, bool):
                raise CadError("catchup: true or false")
            return v
        lo, hi = MAX_AGE_RANGE_H
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise CadError(f"catchup_max_age_h: {lo:g} to {hi:g} (hours)")
        return round(float(v), 1)

    def stt_catchup(self) -> tuple[bool, float]:
        """(on, max age in hours): shed clips caught up (sizeup/sttcatchup.py), on and 6 h unless set here."""
        from sizeup.sttcatchup import DEFAULT_MAX_AGE_H
        cur = self.stt_values()
        on, age = cur.get("catchup", True), cur.get("catchup_max_age_h", DEFAULT_MAX_AGE_H)
        try:
            return self._catchup_value("catchup", on), self._catchup_value("catchup_max_age_h", age)
        except CadError:
            return True, DEFAULT_MAX_AGE_H

    def _stt_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: vocabulary | aws_monthly_limit_usd | aws_context_budget_usd | catchup | catchup_max_age_h, value} |
        reset {key} | push (the vocabularies to AWS)."""
        ctx = self.cad.radio_ctx
        cur = self.stt_values()
        if action == "push":
            if ctx is None:
                raise CadError("no speech-to-text here")
            phrases = self.vocabulary()
            st = ctx.vocabulary_push(phrases)
            with self.store.tx() as db:
                self._audit(db, self.store.clock(), op, "radios", "vocabulary/push", None,
                            f"{len(phrases)} phrases: {st.get('state', '?')}")
            return {"ok": st.get("state") != "error", "vocabulary": st, **({"error": st["reason"]}
                                                                           if st.get("state") == "error" else {})}
        key = _text(body.get("key"), "key", 40)
        if key not in STT_KEYS:
            raise CadError("key: vocabulary, aws_monthly_limit_usd, aws_context_budget_usd, catchup or catchup_max_age_h")
        if action == "set":
            if key == "vocabulary":
                v = self._vocab_list(body.get("value"))
                dflt = ctx.vocabulary_suggestions() if ctx is not None else []
                new = REMOVE if v == dflt else v
                shown = (f"{len(cur[key])} phrases" if key in cur else "default"), f"{len(v)} phrases"
            elif key in ("catchup", "catchup_max_age_h"):
                from sizeup.sttcatchup import DEFAULT_MAX_AGE_H
                v = self._catchup_value(key, body.get("value"))
                dflt = True if key == "catchup" else DEFAULT_MAX_AGE_H
                new = REMOVE if v == dflt else v
                shown = cur.get(key, dflt), v
            else:
                v = self._limit(body.get("value"), key)
                dflt = self._limit_default(key)
                new = REMOVE if v == dflt else v
                shown = cur.get(key, dflt), v
            out = self._commit([("stt", key, new)], op, [("radios", f"stt/{key}", *shown)])
        elif action == "reset":
            out = self._commit([("stt", key, REMOVE)], op, [("radios", f"stt/{key}", "edited", "default")])
        else:
            raise CadError("action: set, reset or push")
        if ctx is not None:
            ctx.channels_changed()
        return out

    def vocabulary(self) -> list[str]:
        v = self.stt_values().get("vocabulary")
        if isinstance(v, list):
            return [str(x) for x in v]
        ctx = self.cad.radio_ctx
        return list(ctx.vocabulary_suggestions()) if ctx is not None else []

    def aws_limit(self) -> float:
        v = self.stt_values().get("aws_monthly_limit_usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return self._limit_default("aws_monthly_limit_usd")

    def aws_context_limit(self) -> float:
        """Another P25 system's (police) own monthly AWS limit: never ours, nor our reserve (radio-intel PR 3)."""
        v = self.stt_values().get("aws_context_budget_usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        return self._limit_default("aws_context_budget_usd")

    def _limit_default(self, key: str) -> float:
        ctx = self.cad.radio_ctx
        if key == "aws_context_budget_usd":
            fn = getattr(ctx, "aws_context_limit_default", None)
            return float(fn()) if fn is not None else 15.0
        return ctx.aws_limit_default() if ctx is not None else 25.0

    def stt_page(self) -> dict:
        """GET /api/cad/stt: the vocabulary (as edited, as AWS gets it, its state there), the limit, AWS's use."""
        from sizeup.transcribe import build_vocabulary
        ctx = self.cad.radio_ctx
        cur = self.stt_values()
        phrases = self.vocabulary()
        built = build_vocabulary(phrases)
        return {"vocabulary": {"phrases": phrases, "edited": "vocabulary" in cur,
                               "suggestions": ctx.vocabulary_suggestions() if ctx is not None else [],
                               "aws": built["phrases"], "skipped": built["skipped"],
                               "sig": channels_mod.phrases_sig(built["phrases"]),
                               "state": ctx.vocabulary_state() if ctx is not None else {"state": "none"}},
                "limit": {"value": self.aws_limit(), "default": ctx.aws_limit_default() if ctx is not None else 25.0,
                          "edited": "aws_monthly_limit_usd" in cur},
                "context_limit": {"value": self.aws_context_limit(), "default": self._limit_default("aws_context_budget_usd"),
                                  "edited": "aws_context_budget_usd" in cur},
                "catchup": dict(zip(("on", "max_age_h"), self.stt_catchup()),
                                edited="catchup" in cur or "catchup_max_age_h" in cur),
                "aws": ctx.aws_status() if ctx is not None else {"enabled": False}}

    # -- the camera wall's area (#125): the margin around our fire districts, cameras always / never on it ---------
    # Kept as overrides (section "camera_wall": margin_m -> metres, include / exclude -> [camera ids]) so they are
    # audited, exported and imported with the rest; sizeup/roads.py Roads.area reads them on every request.

    def wall_values(self) -> dict:
        return self.section(WALL_SECTION)

    @staticmethod
    def _wall_margin(v) -> int:
        lo, hi, _ = WALL_MARGIN
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise CadError(f"margin_m: {lo} to {hi} (metres)")
        return int(round(v))

    @staticmethod
    def _wall_ids(v, what: str) -> list[str]:
        if not isinstance(v, list) or len(v) > MAX_WALL_IDS or not all(isinstance(x, str) and WALL_ID.fullmatch(x) for x in v):
            raise CadError(f"{what}: a list of camera ids (at most {MAX_WALL_IDS})")
        return sorted(set(v))

    def _wall_check(self, d: dict) -> None:
        if set(d) - set(WALL_KEYS):
            raise CadError(f"overrides.{WALL_SECTION}: {', '.join(WALL_KEYS)} only")
        if "margin_m" in d:
            self._wall_margin(d["margin_m"])
        for k in ("include", "exclude"):
            if k in d:
                self._wall_ids(d[k], f"overrides.{WALL_SECTION}.{k}")
        if set(d.get("include") or []) & set(d.get("exclude") or []):
            raise CadError(f"overrides.{WALL_SECTION}: a camera both included and excluded")

    def wall_page(self) -> dict:
        """Settings > System > Camera wall: the margin, every camera with where it falls (the app's Roads), the edits."""
        cur = self.wall_values()
        out = {"margin_m": cur.get("margin_m", WALL_MARGIN[2]), "default_margin_m": WALL_MARGIN[2], "max_margin_m": WALL_MARGIN[1],
               "include": list(cur.get("include") or []), "exclude": list(cur.get("exclude") or []), "edited": bool(cur),
               "cameras": None}
        fn = getattr(self.cad, "camera_area", None)
        if fn is not None:
            try:
                out |= {k: v for k, v in fn().items() if k in ("area", "cameras", "n", "streams")}
            except Exception as e:  # the cameras can't be listed now: the settings still can be edited
                out["error"] = f"{type(e).__name__}: {e}"
        return out

    def _wall_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: margin_m, value} | camera {id, mode: auto | include | exclude} | reset (everything to the default)."""
        cur = self.wall_values()
        if action == "reset":
            if not cur:
                raise CadError("the camera wall is already the default")
            return self._commit([(WALL_SECTION, k, REMOVE) for k in cur], op, [("system", WALL_SECTION, cur, "default")])
        if action == "set":
            if body.get("key") != "margin_m":
                raise CadError("key: margin_m")
            v = self._wall_margin(body.get("value"))
            old = cur.get("margin_m", WALL_MARGIN[2])
            return self._commit([(WALL_SECTION, "margin_m", REMOVE if v == WALL_MARGIN[2] else v)], op,
                                [("system", f"{WALL_SECTION}/margin_m", old, v)])
        if action == "camera":
            cid = _text(body.get("id"), "id", 64)
            if not WALL_ID.fullmatch(cid):
                raise CadError("id: a camera id")
            mode = body.get("mode")
            if mode not in ("auto", "include", "exclude"):
                raise CadError("mode: auto, include or exclude")
            inc, exc = set(cur.get("include") or []), set(cur.get("exclude") or [])
            was = "include" if cid in inc else "exclude" if cid in exc else "auto"
            if was == mode:
                return {"ok": True}
            inc.discard(cid)
            exc.discard(cid)
            (inc if mode == "include" else exc if mode == "exclude" else set()).add(cid)
            if len(inc) > MAX_WALL_IDS or len(exc) > MAX_WALL_IDS:
                raise CadError(f"at most {MAX_WALL_IDS} cameras each way")
            return self._commit([(WALL_SECTION, "include", sorted(inc) or REMOVE), (WALL_SECTION, "exclude", sorted(exc) or REMOVE)], op,
                                [("system", f"{WALL_SECTION}/camera/{cid}", was, mode)])
        raise CadError("action: set, camera or reset")

    # -- data sources (#186): how often each is refreshed (sizeup/datasources.py; Settings > Data sources) ------------
    # Kept as overrides (section "datasources": source key -> a CHOICES key) so they survive a restart and are audited,
    # exported and imported with the rest; the registry reads them on every turn of its schedule.

    @staticmethod
    def _ds_check(d: dict) -> None:
        from sizeup.datasources import CHOICES, KEY_RE
        for k, v in d.items():
            if not isinstance(k, str) or not KEY_RE.fullmatch(k) or v not in CHOICES:
                raise CadError(f"overrides.{DS_SECTION}.{k}: one of {', '.join(CHOICES)}")

    def _ds_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, value: off | hourly | daily | weekly | monthly} | reset {key} (that one) | reset (all): admins."""
        from sizeup.datasources import CHOICES
        ds = getattr(self.cad, "datasources", None)
        cur = dict(self.overrides().get(DS_SECTION) or {})
        if action == "reset" and not body.get("key"):
            if not cur:
                raise CadError("every data source is on its default schedule already")
            return self._commit([(DS_SECTION, k, REMOVE) for k in cur], op, [("system", DS_SECTION, cur, "default")])
        key = _text(body.get("key"), "key", 64)
        src = ds.get(key) if ds is not None else None
        if ds is not None and src is None:
            raise CadError(f"no data source {key!r}")
        if src is not None and not src.managed:
            raise CadError(f"{src.name} keeps its own schedule ({src.builtin or 'refreshed by hand'})")
        default = src.every if src is not None else None
        old = cur.get(key, default or "default")
        if action == "reset":
            if key not in cur:
                raise CadError(f"{src.name if src else key} is on its default schedule already")
            return self._commit([(DS_SECTION, key, REMOVE)], op, [("system", f"{DS_SECTION}/{key}", old, default or "default")])
        if action != "set":
            raise CadError("action: set or reset")
        v = body.get("value")
        if v not in CHOICES:
            raise CadError(f"value: one of {', '.join(CHOICES)}")
        if v == old:
            return {"ok": True}
        return self._commit([(DS_SECTION, key, REMOVE if v == default else v)], op, [("system", f"{DS_SECTION}/{key}", old, v)])

    # -- push notifications: the ntfy topics by kind (sizeup/notify.py Pushes; Settings > System, admins) ----------
    # Kept as overrides (section "notify": "alerts" / "status" -> {"topic"} | {"off": true}, "server" -> URL) over the
    # env / SSM / config values; read at every push, so a change takes effect at once. The names are secret (ntfy
    # topics are public by name): never in the export, the audit log (a fingerprint there), the activity or the logs.

    def _notify_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: alerts | status | intel, topic} ("" = the default) | off {key} | reset {key} | server {value} ("" = the
        default) | test {key}: a marked test push to the topic in effect. Admins (sizeup/cad/api.py WRITES). An intel topic
        that is an alerts or status topic, or the reverse, is refused (notify.INTEL_SAME_WHY: intel's channel is its own)."""
        from sizeup import notify
        p = getattr(self.cad, "pushes", None)
        if p is None:
            raise CadError("no push notifications here")
        cur = dict(self.overrides().get(NOTIFY_SECTION) or {})
        key = body.get("key")
        if key == "server" or action == "server":
            v = _text(body.get("value"), "value", 200, required=False).rstrip("/")
            if v and notify.server_problem(v):
                raise CadError(f"ntfy server: {notify.server_problem(v)}")
            old = cur.get("server") or f"default {p.default_server[0]}"
            if v == (cur.get("server") or ""):
                return {"ok": True}
            out = self._commit([(NOTIFY_SECTION, "server", v or REMOVE)], op,
                               [("system", f"{NOTIFY_SECTION}/server", old, v or f"default {p.default_server[0]}")])
            self.cad.log(p.line() + " (Settings changed)")
            return out
        if key in (notify.PREALERT_RING, notify.PREALERT_STATUS, notify.PROTECTED):
            return self._notify_protected_op(action, body, op, key, p, cur)
        if key not in notify.KINDS:
            raise CadError("key: alerts, status or intel")
        def said(v) -> str:                          # never the name: the audit log is open to everyone signed in
            if not isinstance(v, dict):
                d, src = p.defaults[key]
                return f"default ({notify.SOURCES[src]}: {notify.fingerprint(d)})" if d else "default (not set)"
            return "off" if v.get("off") else f"Settings: {notify.fingerprint(str(v.get('topic') or ''))}"
        old = cur.get(key)
        if action == "test":
            try:
                r = p.test(key)
            except notify.NotifyError as e:
                raise CadError(str(e)) from None
            eff = p.effective(key)
            self.note(op, "system", f"{NOTIFY_SECTION}/{key}/test", notify.fingerprint(eff["topic"]),
                      "test push sent" if r["ok"] else f"test push not sent: {r['error'][:200]}")
            return {"ok": True, "test": r}
        if action == "set":
            t = _text(body.get("topic"), "topic", 64, required=False)
            if t:
                why = p.problem(t)
                if why:
                    raise CadError(f"{notify.LABELS[key]}: {why}")
                # intel goes to a channel of its own (admin, 2026-11-17): never the alerts' or status' topic, either way
                theirs = (notify.ALERTS, notify.STATUS) if key == notify.INTEL else (notify.INTEL,)
                if notify.same_topic(t, p.named(theirs)):
                    raise CadError(f"{notify.LABELS[key]}: {notify.INTEL_SAME_WHY}")
                new = {"topic": t}
            else:
                new = REMOVE
        elif action == "off":
            new = {"off": True}
        elif action == "reset":
            if old is None:
                raise CadError(f"{notify.LABELS[key]} is on its default already")
            new = REMOVE
        else:
            raise CadError("action: set, off, reset, server or test")
        if (old is None and new is REMOVE) or old == new:
            return {"ok": True}
        out = self._commit([(NOTIFY_SECTION, key, new)], op,
                           [("system", f"{NOTIFY_SECTION}/{key}", said(old), said(None if new is REMOVE else new))])
        self.cad.log(p.line() + " (Settings changed)")
        return out

    def _notify_protected_op(self, action: str, body: dict, op: str, key: str, p, cur: dict) -> dict:
        """The pre-alert topics sizeup subscribes to (prealert_ring / prealert_status: set {topic} | off | reset) and the
        extra topics never published to (protected: set {topics: [..] | "a, b"} | reset). Never a name in the audit log.
        A name that is one of our own push topics in effect is refused (it would silence them: protected topics are
        never published to), and a subscribed topic can't be both feeds."""
        from sizeup import notify
        import re as _re
        label = {notify.PREALERT_RING: "Pre-alert ring topic", notify.PREALERT_STATUS: "Pre-alert status topic",
                 notify.PROTECTED: "Protected topics"}[key]
        ours = {p.effective(k)["topic"].lower() for k in notify.KINDS if p.effective(k)["topic"]}

        def check(t: str) -> str:
            if not notify.TOPIC.fullmatch(t):
                return f"{t[:20]!r}: letters, digits, - and _ (at most 64)"
            if t.lower() in ours:
                return "that is one of our own push topics in effect: it can't also be protected"
            return ""
        old = cur.get(key)
        if key == notify.PROTECTED:
            if action == "reset" or (action == "set" and not body.get("topics")):
                new = REMOVE
            elif action == "set":
                raw = body["topics"]
                names = _re.split(r"[\s,;]+", raw) if isinstance(raw, str) else raw
                if not isinstance(names, list) or len(names) > 50:
                    raise CadError("topics: a list of up to 50 ntfy topic names")
                names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
                for n in names:
                    if check(n):
                        raise CadError(f"{label}: {check(n)}")
                new = {"topics": names}
            else:
                raise CadError("action: set or reset")
            said = lambda v: (f"{len(v['topics'])} listed" if isinstance(v, dict) else "none")      # noqa: E731
        else:
            if action == "set":
                t = _text(body.get("topic"), "topic", 64, required=False)
                if t:
                    if check(t):
                        raise CadError(f"{label}: {check(t)}")
                    other = cur.get(notify.PREALERT_STATUS if key == notify.PREALERT_RING else notify.PREALERT_RING)
                    if isinstance(other, dict) and str(other.get("topic") or "").lower() == t.lower():
                        raise CadError(f"{label}: the ring and status topics must differ")
                    new = {"topic": t}
                else:
                    new = REMOVE
            elif action == "off":
                new = {"off": True}
            elif action == "reset":
                if old is None:
                    raise CadError(f"{label} is on its default already")
                new = REMOVE
            else:
                raise CadError("action: set, off or reset")
            said = lambda v: ("default" if not isinstance(v, dict) else "off" if v.get("off")      # noqa: E731
                              else f"Settings: {notify.fingerprint(str(v.get('topic') or ''))}")
        if (old is None and new is REMOVE) or old == new:
            return {"ok": True}
        changes = [(NOTIFY_SECTION, key, new)]
        audit = [("system", f"{NOTIFY_SECTION}/{key}", said(old), said(None if new is REMOVE else new))]
        gone = str(old.get("topic") or "").strip() if isinstance(old, dict) and key != notify.PROTECTED else ""
        newt = str(new.get("topic") or "").strip() if isinstance(new, dict) else ""
        if gone and gone.lower() != newt.lower():
            # a topic read until now (it may page people) leaves the feed but stays protected: never a publishable topic
            listed = list(((cur.get(notify.PROTECTED) or {}).get("topics")) or [])
            if gone.lower() not in {x.lower() for x in listed}:
                listed.append(gone)
                changes.append((NOTIFY_SECTION, notify.PROTECTED, {"topics": listed}))
                audit.append(("system", f"{NOTIFY_SECTION}/{notify.PROTECTED}", f"{len(listed) - 1} listed",
                              f"{len(listed)} listed (a retired feed topic)"))
        out = self._commit(changes, op, audit)
        self.cad.log(f"notify: {label} changed in Settings")
        sync = getattr(getattr(self.cad, "prealert", None), "sync_topics", None)
        if sync is not None:
            try:
                sync()
            except Exception as e:  # noqa: BLE001 - the next tick follows it
                self.cad.log(f"notify: pre-alert topics: {type(e).__name__}")
        return out

    def note(self, operator: str, section: str, key: str, old, new) -> None:
        """An audit row for something done from Settings that isn't a setting (a data source's Refresh now, #186)."""
        with self.store.tx() as db:
            self._audit(db, self.store.clock(), operator, section, key, old, new)

    # -- run cards: reset ------------------------------------------------------------------------------

    def _cards_op(self, action: str, body: dict, op: str) -> dict:
        if action == "reset":
            k = _text(body.get("type"), "type", 50)
            old = runcards.card(self.store, k)
            c = runcards.reset(self.store, k)
            self.audit_card(op, old, c)
            return c
        if action == "reset_section":
            n = len(runcards.cards(self.store))
            runcards.reset_all(self.store)
            with self.store.tx() as db:
                self._audit(db, self.store.clock(), op, "run cards", "run cards", f"{n} cards", "reset to the defaults")
            return {"ok": True}
        raise CadError("action: reset or reset_section")

    # -- users ----------------------------------------------------------------------------------------------

    # last_seen / last_ip / last_ua (sizeup/cad/activity.py) are left out: where someone was is their
    # activity, an admin's to see (GET /api/cad/activity), not every reader of the Settings page's.
    OPERATOR_COLUMNS = "id, name, role, active, created_at, updated_at, email, callsign"

    def operators(self) -> list[dict]:
        rows = self.store.q(f"SELECT {self.OPERATOR_COLUMNS} FROM operators ORDER BY active DESC, name COLLATE NOCASE")
        for r in rows:
            r["active"] = bool(r["active"])
        return rows

    def operator_names(self) -> list[str]:
        return [r["name"] for r in self.store.q("SELECT name FROM operators WHERE active = 1 ORDER BY name COLLATE NOCASE")]

    # Signed in through Cloudflare Access (sizeup/cloud/access.py, 2026-10-07): the verified email is
    # who did it. What's recorded as the operator is "Name <email>" (just the email until someone
    # names it here), so the chronology reads "Casey" (cad.js shows the name, the email as its
    # tooltip) and the audit can't mix up two people with one name.

    @staticmethod
    def recorded(name: str, email: str) -> str:
        return email if not name or name.lower() == email else f"{name} <{email}>"

    def _unlinked(self, email: str) -> dict | None:
        """An operator entered by hand with this email as its name and no email yet: the leader's person() links it
        (sets its email); a fenced task (#501) reads it as it would be, its role and active as they are."""
        return self.store.one("SELECT name, callsign, role, active FROM operators WHERE name = ? AND email = ''", (email,))

    def person(self, email: str) -> dict:
        """{email, name, callsign, operator} for a verified email. An email not on the list is added
        (named by its email, a dispatcher) so an admin can give it a name in Settings > Users."""
        email = email.strip().lower()
        if not EMAIL.fullmatch(email):
            raise CadError("not an email address")
        r = self.store.one("SELECT name, callsign FROM operators WHERE email = ?", (email,))
        if r is None and not self.store.may_write():
            # a fenced hub task (not the leader, sizeup/fence.py, #501): signed in all the same, nothing written: the
            # row the leader would give them (one entered by hand under this email as its name), else a dispatcher
            # named by their email; the leader adds them on their next request there
            r = self._unlinked(email) or {"name": email, "callsign": ""}
        elif r is None:
            with self.store.tx() as db:
                row = db.execute("SELECT id, name, callsign, email FROM operators WHERE email = ? OR name = ?",
                                 (email, email)).fetchone()
                if row is None:
                    now = self.store.clock()
                    db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email) "
                               "VALUES (?, 'dispatcher', 1, ?, ?, ?)", (email, now, now, email))
                    self._audit(db, now, email, "users", email, None,
                                {"email": email, "seen": "signed in through Cloudflare Access (not named yet)"})
                    self.store.rev += 1
                elif not row["email"]:      # an operator named by the email itself, entered by hand
                    # (updated_at: settings_rev moves, so an open Users page shows the email at once)
                    db.execute("UPDATE operators SET email = ?, updated_at = ? WHERE id = ?", (email, self.store.clock(), row["id"]))
            r = self.store.one("SELECT name, callsign FROM operators WHERE email = ?", (email,)) or {"name": email, "callsign": ""}
        return {"email": email, "name": r["name"], "callsign": r["callsign"], "operator": self.recorded(r["name"], email)}

    # Roles (user, 2026-10-08: "admins should be able to set users, dispatchers should not, but first
    # login should be able to set whatever i guess"): enforced only for a verified sign-in (the hub
    # with Cloudflare Access; sizeup/cad/api.py checks every POST). An admin changes anything; a
    # dispatcher runs calls but can't change Settings; a viewer looks. Deactivated: a viewer. Until
    # there is an active admin with a sign-in email, everyone signed in may do what an admin does, so
    # the first person can set things up (and nobody is locked out); after that the last such admin
    # can't be demoted, deactivated or imported away (LastAdmin).

    def admins(self) -> list[dict]:
        """The active admins with a sign-in email: the ones who count."""
        return [o for o in self.operators() if _is_admin(o)]

    def standing(self, email: str) -> dict:
        """person() plus {role, active, may, bootstrap, admins}: may = what they may do now (admin |
        dispatcher | viewer), bootstrap = no admin yet (anyone signed in may do anything)."""
        p = self.person(email)
        r = (self.store.one("SELECT role, active FROM operators WHERE email = ?", (p["email"],))
             or (None if self.store.may_write() else self._unlinked(p["email"]))      # fenced: as the leader would
             or {"role": "dispatcher", "active": 1})
        admins = self.admins()
        active = bool(r["active"])
        may = "admin" if not admins else r["role"] if active and r["role"] in OPERATOR_ROLES else "viewer"
        return {**p, "role": r["role"], "active": active, "may": may, "bootstrap": not admins,
                "admins": [a["name"] for a in admins]}

    def standing_readonly(self, email: str) -> dict:
        """standing() for a reader that must not write (the mobile app's reads, #560): the same role, active, may and
        bootstrap, but never person()'s side effects. An email not on the list is NOT added (no operators row, no
        settings audit row, no store.rev bump): it is a viewer, never a dispatcher. A bad email raises CadError, as
        person() does. {email, role, active, may, bootstrap}."""
        email = email.strip().lower()
        if not EMAIL.fullmatch(email):
            raise CadError("not an email address")
        r = (self.store.one("SELECT role, active FROM operators WHERE email = ?", (email,))
             or self._unlinked(email)                       # entered by hand with this email as its name: as it is
             or {"role": "viewer", "active": 1})            # not known: the least
        admins = self.admins()
        active = bool(r["active"])
        may = "admin" if not admins else r["role"] if active and r["role"] in OPERATOR_ROLES else "viewer"
        return {"email": email, "role": r["role"], "active": active, "may": may, "bootstrap": not admins}

    def whoami(self, email: str) -> dict:
        """GET /api/cad/whoami: the signed-in person ("access", roles enforced), or nobody known
        ("none": the Operator field is typed, as before, and roles are recorded, not enforced)."""
        if not email:
            return {"email": "", "name": "", "callsign": "", "source": "none", "role": "", "enforced": False}
        st = self.standing(email)
        return {k: st[k] for k in ("email", "name", "callsign", "role", "active", "may", "bootstrap", "admins")} | {
            "source": "access", "enforced": True}

    def _keep_an_admin(self, after: list[dict], why: str) -> None:
        """guard: the operators as they would be after a change still have an active admin with a
        sign-in email, if they have one now."""
        if self.admins() and not any(_is_admin(o) for o in after):
            raise LastAdmin(why)

    def _email(self, v, oid: int | None = None) -> str:
        v = _text(v, "email", 254, required=False).lower()
        if v and not EMAIL.fullmatch(v):
            raise CadError("email: the address they sign in with (name@example.org)")
        if v and self.store.one("SELECT 1 AS x FROM operators WHERE email = ? AND id != ?", (v, oid or -1)):
            raise CadError(f"{v} is already another operator's email")
        return v

    def _users_op(self, action: str, body: dict, op: str, guard: bool = False) -> dict:
        now = self.store.clock()

        def role_of(v, dflt):
            v = v or dflt
            if v not in OPERATOR_ROLES:
                raise CadError(f"role: one of {', '.join(OPERATOR_ROLES)}")
            return v
        if action == "add":
            name = _text(body.get("name"), "name", 60)
            role = role_of(body.get("role"), "dispatcher")
            email, callsign = self._email(body.get("email")), _text(body.get("callsign"), "callsign", 20, required=False)
            if self.store.one("SELECT 1 AS x FROM operators WHERE name = ?", (name,)):
                raise CadError(f"{name} is already an operator")
            with self.store.tx() as db:
                oid = db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email, callsign) "
                                 "VALUES (?, ?, 1, ?, ?, ?, ?)", (name, role, now, now, email, callsign)).lastrowid
                self._audit(db, now, op, "users", name, None, {"role": role, "active": True,
                                                                 **({"email": email} if email else {}),
                                                                 **({"callsign": callsign} if callsign else {})})
            return self._operator(oid)
        oid = body.get("id")
        if not isinstance(oid, int) or isinstance(oid, bool):
            raise CadError("id: the operator's id")
        cur = self._operator(oid)
        new = dict(cur)
        if action == "edit":
            if "name" in body:
                new["name"] = _text(body.get("name"), "name", 60)
                if new["name"].lower() != cur["name"].lower() and self.store.one("SELECT 1 AS x FROM operators WHERE name = ?", (new["name"],)):
                    raise CadError(f"{new['name']} is already an operator")
            if "role" in body:
                new["role"] = role_of(body.get("role"), "")
            if "active" in body:
                if not isinstance(body["active"], bool):
                    raise CadError("active: true or false")
                new["active"] = body["active"]
            if "email" in body:
                new["email"] = self._email(body.get("email"), oid)
            if "callsign" in body:
                new["callsign"] = _text(body.get("callsign"), "callsign", 20, required=False)
        elif action in ("deactivate", "activate"):
            new["active"] = action == "activate"
        else:
            raise CadError("action: add, edit, deactivate or activate")
        diff = {k: (cur[k], new[k]) for k in ("name", "role", "active", "email", "callsign") if cur[k] != new[k]}
        if not diff:
            return cur
        if guard and _is_admin(cur) and not _is_admin(new):
            self._keep_an_admin([new if o["id"] == oid else o for o in self.operators()],
                                f"{cur['name']} is the last admin: make someone else an admin first "
                                "(nobody could change Settings after that)")
        with self.store.tx() as db:
            db.execute("UPDATE operators SET name = ?, role = ?, active = ?, email = ?, callsign = ?, updated_at = ? WHERE id = ?",
                       (new["name"], new["role"], int(new["active"]), new["email"], new["callsign"], now, oid))
            self._audit(db, now, op, "users", cur["name"], {k: a for k, (a, _) in diff.items()}, {k: b for k, (_, b) in diff.items()})
        return self._operator(oid)

    def _operator(self, oid: int) -> dict:
        r = self.store.one(f"SELECT {self.OPERATOR_COLUMNS} FROM operators WHERE id = ?", (oid,))
        if r is None:
            raise CadError(f"no operator {oid}")
        r["active"] = bool(r["active"])
        return r

    # -- export / import ------------------------------------------------------------------------------------

    def export(self) -> dict:
        return {"format": FORMAT, "version": FORMAT_VERSION,
                "exported_at": datetime.fromtimestamp(self.store.clock(), TZ).isoformat(timespec="seconds"),
                "data_file": lists_mod._shown_path(str(self.path)), "defaults_version": str(self.defaults.get("version") or ""),
                # names secret; the county's place names: their own import, never in this file
                # (radio_services always, {} with none: an import of this file sets them as it says; only a file from
                # before them, without the section, keeps the ones here)
                "overrides": {SVC_SECTION: {}, **_without_imported({k: v for k, v in self.overrides().items()
                                                                   if k not in KEPT_SECTIONS})},
                "run_cards": [{"type": c["type"], "spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"],
                               "remarks": c["remarks"]} for c in runcards.cards(self.store)],
                "operators": [{"name": o["name"], "role": o["role"], "active": o["active"], "email": o["email"],
                               "callsign": o["callsign"]} for o in self.operators()]}

    def _check_import(self, data) -> tuple[dict, list | None, list | None, object]:
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise CadError(f"not a settings file (format {FORMAT!r} expected)")
        if data.get("version") != FORMAT_VERSION:
            raise CadError(f"settings file version {data.get('version')!r}: this sizeup reads version {FORMAT_VERSION}")
        ov = data.get("overrides") or {}
        if not isinstance(ov, dict) or any(s not in PAGE_SECTION or not isinstance(v, dict) for s, v in ov.items()):
            raise CadError(f"overrides: sections from {', '.join(PAGE_SECTION)}")
        ov = {k: v for k, v in ov.items() if k not in KEPT_SECTIONS}    # the push topics, the mode: set here only, kept
        ov = _without_imported(ov)          # the county's place names: their own import (Common places), kept
        try:
            json.dumps(ov)
        except (TypeError, ValueError):
            raise CadError("overrides: plain JSON values") from None
        d = ov.get("dispatch") or {}
        if "tones" in d:
            self._tones(d["tones"])
        if "channels" in d:
            self._channels(d["channels"])
        if set(d) - {"tones", "channels", "voice", "speed", "weather_speak", *eta.KEYS}:
            raise CadError(f"overrides.dispatch: tones, channels, voice, speed, weather_speak and {', '.join(eta.KEYS)} only")
        if "weather_speak" in d and not isinstance(d["weather_speak"], bool):
            raise CadError("overrides.dispatch.weather_speak: true or false")
        for k, v in (ov.get(channels_mod.SECTION) or {}).items():
            try:
                channels_mod.clean(k, v)
            except ValueError as e:
                raise CadError(f"overrides.channels.{k}: {e}") from None
            if isinstance(v, dict) and v.get("engine") == "aws":     # (an export from before #896: pinned AWS)
                v["engine"] = channels_mod.PIN_AWS
        if channels_mod.SQUELCH_SECTION in ov:
            # never the whole backup refused over a squelch entry: what can't apply is skipped, said in the notes
            ov[channels_mod.SQUELCH_SECTION], self._import_notes = self._import_squelch(ov[channels_mod.SQUELCH_SECTION])
        for k, v in (ov.get(TONES_SECTION) or {}).items():
            key, val = self._clean_tone(v, f"overrides.tones.{k}")
            if key != k:
                raise CadError(f"overrides.tones.{k}: the key is the pair, {key}")
            ov[TONES_SECTION][k] = val
        self._wall_check(ov.get(WALL_SECTION) or {})
        self._ds_check(ov.get(DS_SECTION) or {})
        for k, v in (ov.get("alarms") or {}).items():         # sizeup/cad/alarms.py (#128)
            ov["alarms"][k] = alarms_mod.clean(k, v)
        from sizeup.intel.admin import clean_value
        intel = ov.get("intel") or {}
        for k, v in list(intel.items()):  # as Settings > System > Intel takes it: a bad value refuses the whole file
            try:                          # (policy.settings() would read it as the default without a word)
                intel[k] = clean_value(k, v, intel)
            except CadError as e:
                raise CadError(f"overrides.intel.{k}: {str(e).split(': ', 1)[-1]}") from None
        if ADDR_SECTION in ov:            # sizeup/geo/localaddr.py: a point that can't be placed is skipped, in the notes
            ov[ADDR_SECTION], more = self._import_addresses(ov[ADDR_SECTION])
            self._import_notes = list(getattr(self, "_import_notes", None) or []) + more
        a = ov.get(ATC_SECTION) or {}
        if set(a) - set(ATC_KEYS) or any(not isinstance(v, bool) for v in a.values()):
            raise CadError("overrides.atc: identify and map, true or false")
        a = ov.get(HYD_SECTION) or {}
        if set(a) - set(HYD_KEYS) or any(not isinstance(v, bool) for v in a.values()):
            raise CadError(f"overrides.{HYD_SECTION}: assume_c, true or false")
        if not isinstance(ov.get(REM_SECTION) or {}, dict):
            raise CadError(f"overrides.{REM_SECTION}: an object")
        for k, v in (ov.get(REM_SECTION) or {}).items():      # sizeup/cad/reminders.py
            try:
                reminders.clean(k, v)
            except ValueError as e:
                raise CadError(f"overrides.{REM_SECTION}.{e}") from None
        fx = ov.get(FIX_SECTION) or {}
        if set(fx) - {"keep"} or any(not isinstance(v, bool) for v in fx.values()):
            raise CadError(f"overrides.{FIX_SECTION}: keep, true or false")
        for k, v in (ov.get(ERR_SECTION) or {}).items():      # sizeup/errwatch.py (#285)
            if k not in errwatch.DEFAULTS:
                raise CadError(f"overrides.{ERR_SECTION}: {', '.join(errwatch.DEFAULTS)} only")
            try:
                self._errors_value(k, v)
            except CadError as e:
                raise CadError(f"overrides.{ERR_SECTION}.{e}") from None
        for k, v in (ov.get(MAPD_SECTION) or {}).items():     # sizeup/cad/mapview.py (#376)
            try:
                mapview.display_value(k, v)
            except ValueError as e:
                raise CadError(f"overrides.{MAPD_SECTION}: {e}") from None
        e = ov.get(EM_SECTION) or {}                          # Settings > Radios > Emergencies
        if set(e) - {"prominent"}:
            raise CadError(f"overrides.{EM_SECTION}: prominent only")
        if "prominent" in e:
            try:
                e["prominent"] = em_value(e["prominent"])
            except CadError as x:
                raise CadError(f"overrides.{EM_SECTION}.{x}") from None
        for k, v in (ov.get(SIGNAL_SECTION) or {}).items():   # sizeup/cloud/metricsinsight.py (#606)
            if k not in metricsinsight.DEFAULTS:
                raise CadError(f"overrides.{SIGNAL_SECTION}: {', '.join(metricsinsight.DEFAULTS)} only")
            try:
                self._signal_value(k, v)
            except CadError as x:
                raise CadError(f"overrides.{SIGNAL_SECTION}.{x}") from None
        f = ov.get(FOLDERS_SECTION) or {}                     # the Radio page's folders (#607)
        if set(f) - {radiofolders.KEY}:
            raise CadError(f"overrides.{FOLDERS_SECTION}: {radiofolders.KEY} only")
        if radiofolders.KEY in f:
            try:
                f[radiofolders.KEY] = radiofolders.clean(f[radiofolders.KEY])
            except CadError as x:
                raise CadError(f"overrides.{FOLDERS_SECTION}.{x}") from None
        for k, v in (ov.get(SHORT_SECTION) or {}).items():    # a source's short label (sizeup/radionames.py)
            try:
                ok = radiofolders.is_key(k) and radionames.qualified(k) == k and bool(radionames.clean_short(v))
            except ValueError:
                ok = False
            if not ok:
                raise CadError(f"overrides.{SHORT_SECTION}: {k!r} -> {v!r} (a qualified source key -> "
                               f"a label of 1 to {radionames.SHORT_MAX} characters)")
        for k, v in (ov.get(SVC_SECTION) or {}).items():      # a source's service (sizeup/sourcekeys.py)
            if not radiofolders.is_key(k) or sourcekeys.service_key(k) != k or v not in sourcekeys.SERVICES:
                raise CadError(f"overrides.{SVC_SECTION}: {k!r} -> {v!r} (a qualified source key -> "
                               f"{' | '.join(sourcekeys.SERVICES)})")
        for k, v in (ov.get(PREMISE_SECTION) or {}).items():  # sizeup/cad/premise.py (#403)
            if k not in _visibility_keys() or v not in PREMISE_LEVELS[1:]:
                raise CadError(f"overrides.{PREMISE_SECTION}: {k!r} -> {v!r} (a category -> dispatcher | admin)")
        for sec in (RET_SECTION, TRS_SECTION):                 # sizeup/audiostore.py (#414)
            for k, v in (ov.get(sec) or {}).items():
                try:
                    if self._ret_key(k, sec) != k:
                        raise CadError(f"{k}: write {self._ret_key(k, sec)}")
                    self._ret_value(k, v, sec)
                except CadError as e:
                    raise CadError(f"overrides.{sec}: {e}") from None
        for k, v in list((ov.get(MUTE_SECTION) or {}).items()):     # sizeup/cloud/health.py (#340)
            if k not in health_mod.MUTE_KINDS:
                raise CadError(f"overrides.{MUTE_SECTION}: {', '.join(health_mod.MUTE_KINDS)} only")
            try:
                m = self._mute_value(k, v, self.store.clock())
            except CadError as e:
                raise CadError(f"overrides.{MUTE_SECTION}.{k}: {e}") from None
            if m is None:
                del ov[MUTE_SECTION][k]                   # over already: not brought back
            else:
                ov[MUTE_SECTION][k] = m
        for k, v in (ov.get(FT_SECTION) or {}).items():       # the department's feed types (sizeup/cad/feedtypes.py)
            source, code = feedtypes.parse_key(k)
            if (not feedtypes.SOURCE.fullmatch(source) or not code or feedtypes.row_key(source, code) != k
                    or not isinstance(v, dict) or set(v) - {"type", "subtype"}
                    or not all(isinstance(v.get(x, ""), str) for x in ("type", "subtype")) or not v.get("type")):
                raise CadError(f"overrides.{FT_SECTION}: {str(k)[:60]!r} -> {{type, subtype}} (key <feed>|<code>)")
        for k, v in (ov.get(DEPT_SECTION) or {}).items():     # a feed's department entries (#370)
            try:
                self._dept_value(k, v)
            except CadError as e:
                raise CadError(f"overrides.{DEPT_SECTION}.{e}") from None
        st = ov.get("stt") or {}
        if set(st) - set(STT_KEYS):
            raise CadError("overrides.stt: vocabulary, aws_monthly_limit_usd, aws_context_budget_usd, catchup and "
                           "catchup_max_age_h only")
        if "vocabulary" in st:
            self._vocab_list(st["vocabulary"])
        for k in ("aws_monthly_limit_usd", "aws_context_budget_usd"):
            if k in st:
                self._limit(st[k], k)
        for k in ("catchup", "catchup_max_age_h"):
            if k in st:
                self._catchup_value(k, st[k])
        L = self.build(ov)
        for k in eta.KEYS:
            if k in d:
                try:
                    eta.check(k, d[k], L.free)
                except ValueError as e:
                    raise CadError(f"overrides.dispatch.{e}") from None
        # nothing in use may disappear
        for sec in LIST_SECTIONS:
            now = {x["code"] for x in getattr(self.cad.lists, sec)}
            then = {x["code"] for x in getattr(L, sec)}
            used = self.usage(sec)
            lost = sorted(c for c in now - then if used.get(c))
            if lost:
                raise CadError(f"{LIST_TITLES[sec]}: {', '.join(lost)} are used here and missing from the file (retire, don't delete)")
        on = {c for c, _, _ in L.board_units()}
        busy = sorted(u["name"] for u in self.store.units() if u["incident_id"] and u["name"] not in on)
        if busy:
            raise CadError(f"units on an event now would leave the boards: {', '.join(busy)}")
        cards = data.get("run_cards")
        if cards is not None:
            if not isinstance(cards, list) or len(cards) > 500:
                raise CadError("run_cards: a list")
            known = {c for c, _, _ in L.board_units()}
            seen = set()
            fire_kinds = set(L.card_kinds)
            for i, c in enumerate(cards):
                if not isinstance(c, dict):
                    raise CadError(f"run_cards[{i}]: an object")
                try:
                    t, s = L.check_key(str(c.get("type", "")))
                except ValueError as e:
                    raise CadError(f"run_cards[{i}]: {e}") from None
                k = lists_mod.key(t, s)
                if k in seen:
                    raise CadError(f"run_cards: {k} twice")
                seen.add(k)
                slots = c.get("slots")
                if not isinstance(slots, list) or not 1 <= len(slots) <= runcards.MAX_SLOTS:
                    raise CadError(f"run_cards[{i}].slots: 1 to {runcards.MAX_SLOTS}")
                if sum(1 for sl in slots if isinstance(sl, dict) and runcards.ALL in sl) > 1:
                    raise CadError(f"run_cards[{i}]: one full department per card")
                for sl in slots:
                    if not isinstance(sl, dict):
                        raise CadError(f"run_cards[{i}]: a slot is an object")
                    if runcards.ALL in sl:
                        if sl[runcards.ALL] not in L.departments:
                            raise CadError(f"run_cards[{i}]: all: {' or '.join(L.departments)}")
                    elif sl.get("unit"):
                        if sl["unit"] not in known or L.response(sl["unit"]) == "agency":
                            raise CadError(f"run_cards[{i}]: {sl['unit']!r} is not a fire unit on the boards")
                    elif not (sl.get("kind") == lists_mod.ANY_KIND or set(str(sl.get("kind", "")).split("|")) <= fire_kinds) or not (
                            isinstance(sl.get("count", 1), int) and 1 <= sl.get("count", 1) <= runcards.MAX_COUNT):
                        raise CadError(f"run_cards[{i}]: kind {sl.get('kind')!r} x {sl.get('count')!r}: a fire apparatus kind, 1 to {runcards.MAX_COUNT}")
                c["type"] = k
        ops = data.get("operators")
        if ops is not None:
            if not isinstance(ops, list) or len(ops) > 500:
                raise CadError("operators: a list")
            names, emails = set(), set()
            for i, o in enumerate(ops):
                if not isinstance(o, dict):
                    raise CadError(f"operators[{i}]: an object")
                o["name"] = _text(o.get("name"), f"operators[{i}].name", 60)
                if o.get("role", "dispatcher") not in OPERATOR_ROLES or not isinstance(o.get("active", True), bool):
                    raise CadError(f"operators[{i}]: role one of {', '.join(OPERATOR_ROLES)}, active true/false")
                if o["name"].lower() in names:
                    raise CadError(f"operators: {o['name']} twice")
                names.add(o["name"].lower())
                o["email"] = _text(o.get("email"), f"operators[{i}].email", 254, required=False).lower()
                o["callsign"] = _text(o.get("callsign"), f"operators[{i}].callsign", 20, required=False)
                if o["email"] and not EMAIL.fullmatch(o["email"]):
                    raise CadError(f"operators[{i}].email: an email address")
                if o["email"] and o["email"] in emails:
                    raise CadError(f"operators: {o['email']} twice")
                emails.add(o["email"])
        return ov, cards, ops, L

    def _import_op(self, action: str, body: dict, op: str, guard: bool = False) -> dict:
        if action not in ("dry_run", "apply"):
            raise CadError("action: dry_run or apply")
        self._import_notes = []
        ov, cards, ops, L = self._check_import(body.get("settings"))
        notes = list(self._import_notes)
        if guard and ops is not None:         # the operators as apply (below) would leave them
            after = {o["name"]: dict(o) for o in self.operators()}
            for o in ops:
                for r in after.values():
                    if o["email"] and r["email"] == o["email"] and r["name"] != o["name"]:
                        r["email"] = ""
            for o in ops:
                after[o["name"]] = {"name": o["name"], "role": o.get("role", "dispatcher"), "active": o.get("active", True),
                                    "email": o["email"]}
            self._keep_an_admin(list(after.values()), "the file leaves no active admin with a sign-in email "
                                f"({', '.join(a['name'] for a in self.admins())} now): nobody could change Settings after it")
        cur = _without_imported({k: v for k, v in self.overrides().items() if k not in KEPT_SECTIONS})
        if SVC_SECTION not in ov and cur.get(SVC_SECTION):
            # a file with no service choices (one from before them): the ones here are kept, not cleared (Auto, one
            # by one, clears them)
            ov = {**ov, SVC_SECTION: dict(cur[SVC_SECTION])}
        svc_moved = services_moved(cur, ov)
        changes = []
        for sec in sorted(set(cur) | set(ov)):
            a, b = cur.get(sec, {}), ov.get(sec, {})
            for k in sorted(set(a) | set(b)):
                if a.get(k, REMOVE) != b.get(k, REMOVE):
                    changes.append({"section": PAGE_SECTION[sec], "store": sec, "key": k,
                                    "change": "added" if k not in a else "removed" if k not in b else "changed",
                                    "old": a.get(k), "new": b.get(k)})
        if cards is not None:
            have = {c["type"]: {"spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"], "remarks": c["remarks"]}
                    for c in runcards.cards(self.store)}
            want = {c["type"]: {"spoken": str(c.get("spoken") or ""), "is_default": bool(c.get("is_default")),
                                "slots": [runcards.normal(s) for s in c["slots"]],
                                "remarks": " ".join(str(c.get("remarks") or "").split())[:runcards.MAX_REMARKS]} for c in cards}
            for k in sorted(set(have) | set(want)):
                if have.get(k) != want.get(k):
                    changes.append({"section": "run cards", "store": "run_cards", "key": k,
                                    "change": "added" if k not in have else "removed" if k not in want else "changed",
                                    "old": have.get(k), "new": want.get(k)})
        if ops is not None:
            have = {o["name"].lower(): o for o in self.operators()}
            for o in ops:
                h = have.get(o["name"].lower())
                new = {"role": o.get("role", "dispatcher"), "active": o.get("active", True), "email": o["email"],
                       "callsign": o["callsign"]}
                old = {k: h[k] for k in new} if h else None
                if old != new or h["name"] != o["name"]:
                    changes.append({"section": "users", "store": "operators", "key": o["name"],
                                    "change": "added" if h is None else "changed", "old": old, "new": new})
        if action == "dry_run":
            return {"ok": True, "dry_run": True, "changes": changes, "notes": notes,
                    "note": "Nothing changed yet. Operators not in the file are kept; run cards and edits are replaced."}
        now = self.store.clock()
        with self.store.tx() as db:
            db.execute("DELETE FROM settings_overrides WHERE section NOT IN (?, ?) AND NOT (section = 'places' AND "
                       "key LIKE ?)", (*KEPT_SECTIONS, commonplaces.KEY_PREFIX + "%"))
            for sec, kv in ov.items():
                for k, v in kv.items():
                    db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?)",
                               (sec, k, json.dumps(v), now, op))
            if cards is not None:
                db.execute("DELETE FROM run_cards")
                for i, c in enumerate(cards):
                    cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort, remarks) VALUES (?, ?, ?, ?, ?)",
                                     (c["type"], str(c.get("spoken") or "")[:120], int(bool(c.get("is_default"))), i,
                                      " ".join(str(c.get("remarks") or "").split())[:runcards.MAX_REMARKS])).lastrowid
                    runcards._write_slots(db, cid, c["slots"])
            if ops is not None:
                for o in ops:      # an email the file gives someone else moves to them (the file wins)
                    if o["email"]:
                        db.execute("UPDATE operators SET email = '' WHERE email = ? AND name != ?", (o["email"], o["name"]))
                for o in ops:
                    row = db.execute("SELECT id FROM operators WHERE name = ?", (o["name"],)).fetchone()
                    if row:
                        db.execute("UPDATE operators SET name = ?, role = ?, active = ?, email = ?, callsign = ?, updated_at = ? "
                                   "WHERE id = ?", (o["name"], o.get("role", "dispatcher"), int(o.get("active", True)),
                                                    o["email"], o["callsign"], now, row[0]))
                    else:
                        db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email, callsign) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (o["name"], o.get("role", "dispatcher"),
                                                                    int(o.get("active", True)), now, now, o["email"], o["callsign"]))
            for c in changes:
                self._audit(db, now, op, c["section"], f"{c['store']}/{c['key']}" if c["store"] not in ("run_cards", "operators")
                            else c["key"], c["old"], c["new"])
            self._audit(db, now, op, "system", "import", None, f"{len(changes)} changes")
            mark_retag(db, svc_moved, now, op)
        self._use(L, ov)
        self.store.sync_units()
        self.store.rev += 1
        if svc_moved:
            self.cad.retag_services()          # a source's service the file changed: its recent rows (off this thread)
        if self.cad.radio_ctx is not None and any(c["store"] in (channels_mod.SECTION, "stt", channels_mod.SQUELCH_SECTION)
                                                  for c in changes):
            self.cad.radio_ctx.channels_changed()
        if self.cad.radio_ctx is not None and any(c["store"] == TONES_SECTION for c in changes):
            self.cad.radio_ctx.tones_changed()
        if getattr(self.cad, "intel", None) is not None and any(c["store"] in INTEL_SECTIONS for c in changes):
            self.cad.intel.reload()          # the intel switches (push, push_pii, the caps) live now, as after their own op
        return {"ok": True, "dry_run": False, "changes": changes, "notes": notes}
