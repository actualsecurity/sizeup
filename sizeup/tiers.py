"""Talkgroup tiers (GitHub #409): what sizeup does with a talkgroup of a P25 system beside the county's.

    alert        record + transcribe + may raise a pre-alert (the pre-alerts themselves: #411)
    text         record + transcribe, no alerts
    record       the audio is kept, no speech-to-text (no STT cost)
    off          ignored: dropped on arrival
    activity     encrypted: never decoded, never recorded; only that it was active (time, talkgroup, radio ID)

(The names are #415's, the Radio page's source registry. "transcribe" and "ignore" are read as text and off.)

The default is the talkgroups file's "Tier" column (deploy/trunk-recorder/systems/<name>/talkgroups.csv); without one,
an encrypted talkgroup is `activity` and any other `record` (a new source costs no speech-to-text until a tier is
chosen). Settings > Radios changes it per talkgroup (TalkgroupPrefs,
sizeup/talkgroups.py). An encrypted talkgroup is only ever `activity` or `off`.

Speech-to-text follows the tier (sizeup/stt.py's classes, #300 / #306): `text` is LOW (shed when whisper is
behind) and another P25 system's `alert` is CONTEXT (shed when it waits);
neither is ever ahead of the pages, Fire/EMS 15, the firegrounds or the airband.

Retention ([storage] tier_keep_days, docs/cloud.md): how long a tier's audio is kept past the usual
audio_keep_hours. A clip on a CAD event, with an alarm or an emergency is a record as before.

The county system's talkgroups have no tier of their own: their Record / Main / Transcribe switches are as before, and
`derived()` only names what those amount to (for the rows and the API).

In the radio log (radio_traffic.tier, sizeup/cad/comms.py record()) only another P25 system's rows carry a tier: the
one their talkgroup had when the call was heard (App._call_system). A county talkgroup's row and an analog or airband
row have "" (App._row_source), on purpose: the column's readers (the tiers' retention and expected storage, the STT
catch-up's order, the Radio page's tier filter, a pack's scope "tiers") are all about the other systems' rows, and a
derived tier written on ours would give them a tier's retention. So a hub that has never recorded another P25 system
has the column empty on every row; nothing is missing and nothing is backfilled. A row recorded again without its
tier keeps the one it had.
"""

from __future__ import annotations

ALERT, TRANSCRIBE, RECORD, IGNORE, ACTIVITY = "alert", "text", "record", "off", "activity"
SAME = {"transcribe": TRANSCRIBE, "ignore": IGNORE}
TIERS = (ALERT, TRANSCRIBE, RECORD, IGNORE, ACTIVITY)
LABELS = {ALERT: "Alert", TRANSCRIBE: "Transcribe", RECORD: "Record only", IGNORE: "Off", ACTIVITY: "Activity only"}
RECORDED = (ALERT, TRANSCRIBE, RECORD)             # the audio is kept
TRANSCRIBED = (ALERT, TRANSCRIBE)
KEEP_DAYS = {ALERT: 30.0, TRANSCRIBE: 0.0, RECORD: 7.0}     # 0: the usual audio retention ([storage] audio_keep_hours)
CLIP_BYTES_PER_S = 16000                 # a clip as the hub keeps it: 8 kHz, 16-bit mono (sizeup/cloud/station.py clip_8k)
USD_PER_GB_MONTH = 0.023                 # S3 Standard, us-east-1 (the Litestream replica of audio.sqlite)


def clean(value) -> str | None:
    """A tier as written in a file or sent by the UI, None when it isn't one."""
    v = str(value or "").strip().lower()
    v = SAME.get(v, v)
    return v if v in TIERS else None


def default(encrypted: bool, listed=None) -> str:
    """The tier a talkgroup starts with: the file's, held to what an encrypted talkgroup may be."""
    t = clean(listed)
    if encrypted:
        return t if t in (ACTIVITY, IGNORE) else ACTIVITY
    return t if t and t != ACTIVITY else RECORD


def allowed(tier: str, encrypted: bool) -> bool:
    return tier in ((ACTIVITY, IGNORE) if encrypted else (ALERT, TRANSCRIBE, RECORD, IGNORE))


def derived(record: bool, transcribe: bool, encrypted: bool) -> str:
    """What a county talkgroup's own switches amount to (never `alert`: its alerts are the CAD's own rules)."""
    if not record:
        return ACTIVITY if encrypted else IGNORE
    return TRANSCRIBE if transcribe else RECORD


# Settings > Radios shows every talkgroup with the same switches (Record, Transcribe, Alerts, Main): another system's
# tier is presented as them and a switch is written back as the tier (the stored model is unchanged). The chain:
# Alerts needs Transcribe, Transcribe needs Record. The county's talkgroups keep their own Record / Main / Transcribe;
# they have no Alerts switch: their pre-alerts are the intel packs' (scope by service and source, Settings > System >
# Intel), never a per-talkgroup choice, so their `alerts` is None.
SWITCHES = ("record", "transcribe", "alerts")
NEVER_MAIN = "another system: never Main"
COUNTY_ALERTS = "county: pre-alerts by the intel packs (Settings > System > Intel)"
ENCRYPTED = "encrypted: activity only"
BOTH = "send tier or switches, not both"


def switches(tier) -> dict:
    """Another system's tier as the switches: {record, transcribe, alerts, activity} (activity: encrypted, only that
    it was active)."""
    t = clean(tier)
    return {"record": t in RECORDED, "transcribe": t in TRANSCRIBED, "alerts": t == ALERT, "activity": t == ACTIVITY}


def from_switches(current, encrypted: bool, record=None, transcribe=None, alerts=None) -> tuple[str | None, str | None]:
    """The tier the switches given (None: as they are) make of `current`: (tier, None), or (None, why). The chain: a
    switch turned on turns on what it needs (Alerts: Transcribe and Record; Transcribe: Record), one turned off turns
    off what needs it (Record: both; Transcribe: Alerts). Both at once (Record off, Alerts on) is refused. An
    encrypted talkgroup is activity only: Record off is `off`, any switch on is refused."""
    if encrypted:
        if record or transcribe or alerts:
            return None, ENCRYPTED
        return (IGNORE if record is False else current), None
    if (record is False and (transcribe or alerts)) or (transcribe is False and alerts):
        return None, "Alerts needs Transcribe, Transcribe needs Record"
    s = switches(current)
    r = s["record"] if record is None else bool(record)
    t = s["transcribe"] if transcribe is None else bool(transcribe)
    a = s["alerts"] if alerts is None else bool(alerts)
    if alerts:
        t = r = True
    if transcribe:
        r = True
    if record is False:
        t = a = False
    if transcribe is False:
        a = False
    return (ALERT if a else TRANSCRIBE if t else RECORD if r else IGNORE), None


def keep_days(raw=None) -> dict[str, float]:
    """[storage] tier_keep_days over the defaults. ValueError with the reason."""
    out = dict(KEEP_DAYS)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ValueError("a table, e.g. { alert = 30, record = 7 }")
    for k, v in raw.items():
        if k not in KEEP_DAYS:
            raise ValueError(f"{k!r}: one of {', '.join(KEEP_DAYS)}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 3650:
            raise ValueError(f"{k}: days from 0 to 3650")
        out[k] = float(v)
    return out


def storage(seconds_per_day: dict[str, float], days: dict[str, float], base_hours: float = 12.0) -> dict:
    """The expected audio storage per tier: {tiers: [{tier, label, keep_days, s_per_day, mb}], mb, usd_month}.
    seconds_per_day: the audio heard per tier in a day (measured). A tier with keep_days 0 is kept base_hours."""
    rows, total = [], 0.0
    for tier in RECORDED:
        d = float(days.get(tier) or 0.0)
        kept_days = d if d > 0 else base_hours / 24.0
        s = max(0.0, float(seconds_per_day.get(tier) or 0.0))
        b = s * kept_days * CLIP_BYTES_PER_S
        total += b
        rows.append({"tier": tier, "label": LABELS[tier], "keep_days": d, "s_per_day": round(s, 1),
                     "mb": round(b / 1e6, 1)})
    return {"tiers": rows, "mb": round(total / 1e6, 1), "usd_month": round(total / 1e9 * USD_PER_GB_MONTH, 4)}
