"""Life-safety alarms (GitHub #128): the P25 emergency button, and a mayday heard on our fire channels.

    emergency   a P25 transmission trunk-recorder flagged as an emergency (radio/p25.py: the call JSON's
                "emergency", or its srcList entry's). trunk-recorder 5.2.0 knows it from the control
                channel's grant, but the unit script it runs at the grant isn't told (plugins/unit_script
                passes the radio ID and talkgroup only), so sizeup learns it when the call's JSON is
                written: the station raises it then, before speech-to-text (App._call). It always counts
                when the radio is on one of our rigs (Settings > Units, the radio registry) or the
                talkgroup is one of our fire channels; on anyone else's talkgroup (a police one) it is
                a notice (`other`: "notice", the default; "alarm"; or "off").
    keyword     words in a transcript on one of our fire channels (`channels`: FG1 and the other FM
                traffic channels, Fire/EMS 15 = tg:307, Airport Incident = tg:333 by default), whole
                words, any case (KEYWORDS below; `keywords` in Settings). "..." in a phrase is up to
                GAP_WORDS words in between that don't say no ("PAR ... missing": "PAR, two missing" but not
                "PAR complete, nobody missing"). The split spelling "may day" counts only said twice
                ("may day, may day"): once it is a date.

One alarm per transmission (its traffic id): a later transcript (Amazon Transcribe after the station's
early flag, a re-run) updates the same alarm, never raises another. A transmission that ended more than
MAX_AGE_S ago raises nothing (a re-run of old traffic, a replay after a long outage): logged.

Each alarm is kept (table alarms in the CAD SQLite, 90 days) with what (the button, the words), who (talker
alias, rig, radio ID), the channel, the time, the transcript and where the words are in it, whether it was a
drill (Settings: `drill` on), the open event it belongs to and who acknowledged it when. The event: the one
the transmission is on in the communications log (comms.py: the rig's, or the one call on FG1), else an open
event the rig is assigned to; its chronology gets "EMERGENCY: E64 P1 mayday on FG1 at 14:32:10" and,
acknowledged, "...; acknowledged by Corey at 14:32:40" (kind "alarm").

An alarm stays on every console and wall board (/cad, /monitor: static/alarm.js) until someone acknowledges
it (POST /api/cad/alarm/<id>/ack: dispatchers and admins); a notice can be acknowledged the same way and
otherwise drops off after NOTICE_S. With [alerts] ntfy_topic each alarm, and its acknowledgement, is also
pushed through ntfy (sizeup/notify.py; never Tapout's topics), set by the app as `notifier`. Who is in it
(user, 2026-10-14: "we want it to include info on who was called or mentioned"): the talker (unit code and
name, radio ID), every unit of ours the transcript names, the channel, and the event's number and address
when it is linked: title "MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St" (notify.Who),
body "heard: “mayday mayday, Engine 64 …” · units named: E64, L31". The screens show the same.

Runs on the CAD of record (all-in-one or the hub). A station forwarding to a hub raises nothing itself: it
sends the hub an "alarm" event for the button at the end of the call, and the transcripts as always.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from typing import Callable

from sizeup.cad.store import OPERATOR_MAX, TZ, CadError

SECTION = "alarms"                         # settings_overrides section (audited under "system")
GAP = "..."
GAP_WORDS = 6
NEGATORS = frozenset({"no", "not", "none", "nobody", "nothing", "negative", "zero"})
KEYWORDS = ("mayday", "may day may day", "emergency traffic", "firefighter down", "fire fighter down", "member down",
            "lost firefighter", "lost fire fighter", "par ... missing", "par ... unaccounted")
OTHER = ("notice", "alarm", "off")         # the emergency button on a talkgroup that isn't ours, by someone not ours
MAX_KEYWORDS, MAX_PHRASE_CHARS, MAX_PHRASE_WORDS, MAX_CHANNELS = 50, 60, 8, 20
MAX_AGE_S = 30 * 60                        # a transmission that ended longer ago than this raises nothing
NOTICE_S = 15 * 60                         # a notice nobody acknowledged drops off after this
RECENT_S = 10 * 60                         # acknowledged alarms listed this long (the screens say who and when)
KEEP_S = 90 * 86400
TOKEN = re.compile(r"(?:[A-Za-z]\.){2,}|[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")
CHANNEL = re.compile(r"tg:\d{1,9}|[A-Za-z0-9][A-Za-z0-9 ./_-]{0,39}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alarms (
  id INTEGER PRIMARY KEY,
  traffic_id TEXT NOT NULL UNIQUE,      -- one alarm per transmission
  at REAL NOT NULL,                     -- the transmission's start
  end REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  level TEXT NOT NULL,                  -- alarm | notice
  emergency INTEGER NOT NULL DEFAULT 0, -- the P25 emergency button
  keywords TEXT NOT NULL DEFAULT '[]',  -- the phrases heard (JSON list)
  spans TEXT NOT NULL DEFAULT '[]',     -- where in text: [[start, end], ...] (JSON)
  text TEXT NOT NULL DEFAULT '',
  named TEXT NOT NULL DEFAULT '[]',     -- our units the transcript names, in order (feed.py Matcher.units_named)
  channel TEXT NOT NULL DEFAULT '',     -- "FG1", "Fire/EMS 15"
  talkgroup INTEGER,
  unit TEXT NOT NULL DEFAULT '',        -- the talker as heard (P25 alias)
  rig TEXT NOT NULL DEFAULT '',
  radio_id TEXT NOT NULL DEFAULT '',
  drill INTEGER NOT NULL DEFAULT 0,
  incident_id INTEGER,
  ack_by TEXT NOT NULL DEFAULT '',
  ack_at REAL
);
CREATE INDEX IF NOT EXISTS alarms_ack ON alarms(ack_at);
"""


# -- the words -----------------------------------------------------------------------------------------


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(word, start, end) in text: lower case, "P.A.R." as "par", apostrophes kept ("don't")."""
    return [(m.group(0).replace(".", "").replace("’", "'").lower(), m.start(), m.end())
            for m in TOKEN.finditer(text or "")]


def phrase_items(phrase: str) -> list[str]:
    """A phrase as words and GAPs ("PAR ... missing" -> ["par", "...", "missing"])."""
    out: list[str] = []
    for part in str(phrase or "").replace("…", f" {GAP} ").split():
        if part == GAP:
            out.append(GAP)
        else:
            out += [w for w, _a, _b in tokens(part)]
    return out


def clean_phrase(phrase) -> str:
    """The phrase as kept ("PAR ... missing"); CadError with the reason."""
    if not isinstance(phrase, str):
        raise CadError("keywords: a list of phrases")
    p = " ".join(str(phrase).replace("…", f" {GAP} ").split())
    items = phrase_items(p)
    words = [w for w in items if w != GAP]
    if not words:
        raise CadError(f"keyword {phrase!r}: at least one word")
    if len(p) > MAX_PHRASE_CHARS or len(words) > MAX_PHRASE_WORDS:
        raise CadError(f"keyword {phrase[:30]!r}: at most {MAX_PHRASE_WORDS} words, {MAX_PHRASE_CHARS} characters")
    if items[0] == GAP or items[-1] == GAP or any(a == b == GAP for a, b in zip(items, items[1:])):
        raise CadError(f"keyword {phrase!r}: \"...\" goes between two words")
    return " ".join(items)


def _match_at(words: list[str], i: int, items: list[str]) -> list[int] | None:
    """The word positions items match starting at word i (GAP: up to GAP_WORDS words, none a "no")."""
    if not items:
        return []
    it = items[0]
    if it == GAP:
        for g in range(GAP_WORDS + 1):
            if i + g > len(words) or (g and words[i + g - 1] in NEGATORS):
                return None
            got = _match_at(words, i + g, items[1:])
            if got is not None:
                return got
        return None
    if i < len(words) and words[i] == it:
        rest = _match_at(words, i + 1, items[1:])
        return None if rest is None else [i, *rest]
    return None


def find(text: str, phrases) -> tuple[list[str], list[list[int]]]:
    """(the phrases heard, in the order given; the [start, end] of every matched word in text, merged)."""
    toks = tokens(text)
    words = [w for w, _a, _b in toks]
    heard: list[str] = []
    marks: set[int] = set()
    for phrase in phrases:
        items = phrase_items(phrase)
        if not items:
            continue
        hit = False
        for i in range(len(words)):
            got = _match_at(words, i, items)
            if got:
                hit = True
                marks.update(got)
        if hit:
            heard.append(" ".join(items))
    spans: list[list[int]] = []
    for k in sorted(marks):
        a, b = toks[k][1], toks[k][2]
        if spans and spans[-1][1] >= a - 1 and k - 1 in marks:     # neighbouring words: one mark
            spans[-1][1] = b
        else:
            spans.append([a, b])
    return heard, spans


def clean(key: str, value):
    """One setting as kept; CadError with the reason."""
    if key == "keywords":
        if not isinstance(value, list) or len(value) > MAX_KEYWORDS:
            raise CadError(f"keywords: a list of at most {MAX_KEYWORDS} phrases")
        out: list[str] = []
        for p in value:
            p = clean_phrase(p)
            if p not in out:
                out.append(p)
        return out
    if key == "channels":
        if not isinstance(value, list) or len(value) > MAX_CHANNELS:
            raise CadError(f"channels: a list of at most {MAX_CHANNELS} channels (\"FG1\", \"tg:307\")")
        out = []
        for c in value:
            c = " ".join(str(c).split()) if isinstance(c, (str, int)) else ""
            if c.isdigit():
                c = f"tg:{c}"
            if not CHANNEL.fullmatch(c):
                raise CadError(f"channels: {c!r} is not a channel label (\"FG1\") or talkgroup (\"tg:307\")")
            if c not in out:
                out.append(c)
        return out
    if key == "other":
        if value not in OTHER:
            raise CadError(f"other: {', '.join(OTHER)}")
        return value
    if key == "drill":
        if not isinstance(value, bool):
            raise CadError("drill: true or false")
        return value
    raise CadError("key: keywords, channels, other or drill")


def _hms(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%H:%M:%S") if at else ""


def who(a: dict) -> str:
    """The talker as the alarm names it: "E64 P1", "E64 (77A4)", "radio 41228", "an unidentified radio"."""
    unit, rig, rid = (a.get("unit") or "").strip(), (a.get("rig") or "").strip(), (a.get("radio_id") or "").strip()
    if unit and rig and not unit.upper().startswith(rig.split(",")[0].strip().upper()):
        return f"{rig} ({unit})"
    return unit or rig or (f"radio {rid}" if rid else "an unidentified radio")


def what(a: dict) -> str:
    """"emergency button", "mayday", "emergency button + mayday", "“firefighter down”"."""
    kws = a.get("keywords") or []
    words = []
    for k in kws:
        w = "mayday" if k.replace(" ", "").startswith("mayday") else k.replace(f" {GAP} ", " … ")
        if w not in words:
            words.append(w)
    said = ", ".join(words)
    return " + ".join(x for x in ("emergency button" if a.get("emergency") else "", said) if x)


def line(a: dict) -> str:
    """The chronology's line: "EMERGENCY: E64 P1 mayday on FG1 at 14:32:10"."""
    return (("DRILL — " if a.get("drill") else "") + f"EMERGENCY: {who(a)} {what(a)} on {a.get('channel') or '?'} "
            f"at {_hms(a.get('at'))}")


# -- the alarms ------------------------------------------------------------------------------------------


class Alarms:
    """One per Cad (sizeup/cad/service.py: cad.alarms). The app feeds it transmissions (on_traffic) on the
    CAD of record and sets notifier ([alerts] ntfy_topic) and on_change (push the screens now)."""

    def __init__(self, cad, log: Callable[[str], None] = print):
        self.cad, self.store, self.log = cad, cad.store, log
        self.clock = cad.clock
        self._lock = threading.RLock()
        self.on_change: Callable[[], None] = lambda: None
        self.notifier = None                      # sizeup/notify.py Notifier, or None: no pushes
        self.public_url = ""                      # the CAD's address, for the push's link
        with self.store.lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    self.store.db.execute(stmt)
            self.store.db.execute("DELETE FROM alarms WHERE created_at < ?", (self.clock() - KEEP_S,))

    # -- settings (Settings > System > Alarms; POST /api/cad/settings/alarms) -------------------------------

    def default_channels(self) -> list[str]:
        s = self.cad.s
        tgs = sorted(set(s.incident_talkgroups) | set(getattr(s, "airport_talkgroups", ())))
        return [*s.tactical, *(f"tg:{t}" for t in tgs)]

    def defaults(self) -> dict:
        return {"keywords": list(KEYWORDS), "channels": self.default_channels(), "other": "notice", "drill": False}

    def config(self) -> dict:
        ov = self.cad.settings.overrides().get(SECTION) or {}
        d = self.defaults()
        return {k: ov.get(k, v) for k, v in d.items()}

    def settings_page(self) -> dict:
        ov = self.cad.settings.overrides().get(SECTION) or {}
        return {**self.config(), "defaults": self.defaults(), "edited": sorted(ov),
                "push": bool(self.notifier is not None and self.notifier.enabled), "gap_words": GAP_WORDS}

    def settings_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: keywords | channels | other | drill, value} | reset {key}. Audited ("system")."""
        from sizeup.cad.settings import REMOVE
        key = body.get("key")
        if key not in ("keywords", "channels", "other", "drill"):
            raise CadError("key: keywords, channels, other or drill")
        cur = self.config()
        dflt = self.defaults()[key]
        if action == "set":
            v = clean(key, body.get("value"))
            new = REMOVE if v == dflt else v
        elif action == "reset":
            v, new = dflt, REMOVE
        else:
            raise CadError("action: set or reset")

        def shown(x):
            return f"{len(x)}: {', '.join(x)}"[:300] if isinstance(x, list) else x
        out = self.cad.settings._commit([(SECTION, key, new)], op, [("system", f"alarms/{key}", shown(cur[key]), shown(v))])
        if key == "drill":
            self.log(f"alarms: drill mode {'on' if v else 'off'}" + (f" ({op})" if op else ""))
        self.on_change()
        return {**out, "alarms": self.settings_page()}

    # -- what a transmission raises ----------------------------------------------------------------------

    @staticmethod
    def channel_key(t: dict) -> str:
        tg = t.get("talkgroup")
        return f"tg:{int(tg)}" if tg is not None and str(tg).lstrip("-").isdigit() else str(t.get("source") or "")

    def _named(self, text: str) -> list[str]:
        """Our units the transcript names ("Engine 64", "E64", 60 Control's "64, 62 ..."): the feed's matcher."""
        if not text:
            return []
        try:
            return list(self.cad.feed.names_ours(text))
        except Exception as e:  # noqa: BLE001 - the alarm stands without them
            self.log(f"alarms: units named: {type(e).__name__}: {e}")
            return []

    def unit_name(self, code: str) -> str:
        """A unit's name as said ("Engine 64"), "" when the code isn't on the boards."""
        r = self.store.one("SELECT spoken, title FROM units WHERE name = ?", (code.strip(),)) if code else None
        return ((r or {}).get("spoken") or (r or {}).get("title") or "") if r else ""

    def who_of(self, a: dict) -> dict:
        """notify.Who's fields for an alarm: the talker as units [(alias, unit name)], the radio ID, the channel
        (and talkgroup), the event's number and address."""
        rig = (a.get("rig") or "").split(",")[0].strip()
        alias = (a.get("unit") or "").strip()
        label = alias or rig
        name = self.unit_name(rig) if rig else ""
        if alias and rig and not alias.upper().startswith(rig.upper()):
            label, name = rig, alias if not name else f"{name}, {alias}"
        units = [(label, name)] if label else []
        rid = (a.get("radio_id") or "").strip()
        channel = a.get("channel") or ""
        if a.get("talkgroup") is not None and f"TG {a['talkgroup']}" not in channel:
            channel = f"{channel} (TG {a['talkgroup']})" if channel else f"TG {a['talkgroup']}"
        return {"units": units, "talker": f"radio {rid}" if rid else ("" if units else "an unidentified radio"),
                "channel": channel, "incident": str(a.get("incident_number") or ""),
                "address": str(a.get("incident_address") or "")}

    def _ours(self, rig: str) -> bool:
        pcfd = set(getattr(self.cad.radios, "pcfd", ()) or ())
        return any(r.strip() in pcfd for r in (rig or "").split(",") if r.strip())

    def on_traffic(self, t: dict) -> dict | None:
        """A transmission (new, or with new text): the alarm it raises or updates, None for none.
        t: {id, start, end, source, talkgroup, unit, rig, radio_id, text, emergency?}."""
        tid = str(t.get("id") or "")
        if not tid:
            return None
        now = self.clock()
        cfg = self.config()
        fire = self.channel_key(t) in cfg["channels"]
        emergency = bool(t.get("emergency"))
        text = str(t.get("text") or "")
        heard, spans = find(text, cfg["keywords"]) if fire and text else ([], [])
        named = self._named(text)
        start = float(t.get("start") or now)
        end = float(t.get("end") or start)
        rig = str(t.get("rig") or "")
        pushed = acked_line = None
        with self._lock:
            cur = self.store.one("SELECT * FROM alarms WHERE traffic_id = ?", (tid,))
            if cur is None:
                if not emergency and not heard:
                    return None
                if now - end > MAX_AGE_S:
                    self.log(f"alarms: {tid} on {t.get('source')}: {'emergency button' if emergency else ', '.join(heard)} "
                             f"{(now - end) / 60:.0f} min after it ended: too old to raise now (logged only)")
                    return None
                level = "alarm" if heard or fire or self._ours(rig) else cfg["other"]
                if level == "off":
                    self.log(f"alarms: emergency button on {t.get('source')} (radio {t.get('radio_id') or '?'}): "
                             "not our channel or rig, notices off")
                    return None
                with self.store.tx() as db:
                    aid = db.execute(
                        "INSERT INTO alarms (traffic_id, at, end, created_at, updated_at, level, emergency, keywords, spans, "
                        "text, named, channel, talkgroup, unit, rig, radio_id, drill) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, start, end, now, now, level, int(emergency), json.dumps(heard), json.dumps(spans), text,
                         json.dumps(named), str(t.get("source") or ""), t.get("talkgroup"), str(t.get("unit") or ""), rig,
                         str(t.get("radio_id") or ""), int(bool(cfg["drill"])))).lastrowid
                a = self.get(aid)
                self.log(f"alarms: {'DRILL ' if a['drill'] else ''}{level.upper()} {line(a)}"
                         + (f": {text}" if text else ""))
                if level == "alarm":
                    pushed = a
            else:
                a = self._row(cur)
                kws = list(a["keywords"]) + [k for k in heard if k not in a["keywords"]]
                em = a["emergency"] or emergency
                level = a["level"]
                if level == "notice" and (heard or (em and (fire or self._ours(rig or a["rig"])))):
                    level = "alarm"
                new_text = text if text else a["text"]
                new_spans = spans if text else a["spans"]
                new_named = named if text else a["named"]
                if (kws, em, level, new_text, new_spans, new_named) == (a["keywords"], a["emergency"], a["level"], a["text"],
                                                                        a["spans"], a["named"]) \
                        and (a["rig"] or not rig) and a["incident_id"]:
                    return a
                with self.store.tx() as db:
                    db.execute("UPDATE alarms SET keywords = ?, spans = ?, text = ?, named = ?, emergency = ?, level = ?, "
                               "updated_at = ?, rig = CASE WHEN rig = '' THEN ? ELSE rig END, "
                               "unit = CASE WHEN unit = '' THEN ? ELSE unit END WHERE id = ?",
                               (json.dumps(kws), json.dumps(new_spans), new_text, json.dumps(new_named), int(em), level, now, rig,
                                str(t.get("unit") or ""), a["id"]))
                if level == "alarm" and a["level"] == "notice":
                    pushed = self.get(a["id"])
                    self.log(f"alarms: notice {a['id']} is now an ALARM: {line(pushed)}")
                elif kws != a["keywords"]:
                    self.log(f"alarms: {a['id']} also heard {', '.join(k for k in kws if k not in a['keywords'])}")
                a = self.get(a["id"])
                acked_line = a if a["ack_at"] else None
            if not a["incident_id"]:
                a = self._link(a, acked_line is not None)
        if pushed is not None:
            self._push(self.get(pushed["id"]))
        self.on_change()
        return a

    # -- the event it belongs to ------------------------------------------------------------------------

    OPEN = "i.status NOT IN ('closed', 'dismissed')"

    def _incident_for(self, a: dict) -> int | None:
        st = self.store
        r = st.one(f"SELECT l.incident_id AS id FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                   f"WHERE l.traffic_id = ? AND {self.OPEN} ORDER BY i.created_at DESC LIMIT 1", (a["traffic_id"],))
        if r:
            return r["id"]
        from sizeup.cad.comms import Comms
        at = a["at"]
        for rig in [x.strip() for x in (a["rig"] or "").split(",") if x.strip()]:
            r = st.one(f"SELECT i.id FROM incidents i WHERE {self.OPEN} AND i.id IN ({Comms.ON_EVENT_BY_UNIT}) "
                       "ORDER BY i.created_at DESC LIMIT 1", (rig, at, at))
            if r:
                return r["id"]
        if not a["rig"] and a["channel"] in set(self.cad.comms.tactical):
            rows = st.q(f"SELECT i.id FROM incidents i WHERE {self.OPEN} AND i.id IN ({Comms.ACTIVE_EVENTS})", (at, at))
            if len(rows) == 1:                  # FG1 with one call on it: that call's (comms.py's "channel" link)
                return rows[0]["id"]
        return None

    def _link(self, a: dict, acked: bool = False) -> dict:
        iid = self._incident_for(a)
        if iid is None:
            return a
        with self.store.tx() as db:
            db.execute("UPDATE alarms SET incident_id = ? WHERE id = ? AND incident_id IS NULL", (iid, a["id"]))
            self.store._event(db, iid, "alarm", line(a), at=self.clock())
            if acked:
                self.store._event(db, iid, "alarm", self._ack_line(a), a["ack_by"], at=self.clock())
        a = self.get(a["id"])
        self.log(f"alarms: {a['id']} on {a.get('incident_number') or f'event {iid}'}")
        return a

    @staticmethod
    def _ack_line(a: dict) -> str:
        name = re.sub(r"\s*<[^>]*>\s*$", "", a["ack_by"] or "") or a["ack_by"]
        return f"{line(a)}; acknowledged by {name} at {_hms(a['ack_at'])}"

    # -- acknowledge ---------------------------------------------------------------------------------------

    def acknowledge(self, aid: int, body: dict) -> dict:
        """POST /api/cad/alarm/<id>/ack {operator}: who saw it, when (dispatchers and admins)."""
        op = body.get("operator")
        if not isinstance(op, str) or not op.strip():
            raise CadError("operator: who is acknowledging (enter your name)")
        op = op.strip()[:OPERATOR_MAX]
        with self._lock:
            cur = self.store.one("SELECT * FROM alarms WHERE id = ?", (aid,))
            if cur is None:
                raise CadError(f"no alarm {aid}")
            if cur["ack_at"] is not None:
                raise CadError(f"alarm {aid} was already acknowledged by {cur['ack_by']} at {_hms(cur['ack_at'])}")
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE alarms SET ack_by = ?, ack_at = ?, updated_at = ? WHERE id = ?", (op, now, now, aid))
            a = self.get(aid)
            if a["incident_id"]:
                with self.store.tx() as db:
                    self.store._event(db, a["incident_id"], "alarm", self._ack_line(a), op, at=now)
        self.log(f"alarms: {a['level']} {aid} acknowledged by {op}: {line(a)}")
        if a["level"] == "alarm":
            self._push(a, ack=True)
        self.on_change()
        return a

    # -- the push (sizeup/notify.py) --------------------------------------------------------------------------

    def _push(self, a: dict, ack: bool = False) -> None:
        """Title: what, then who / where (notify.compose: "MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St");
        body: when, what was heard, the units it names, who acknowledged."""
        n = self.notifier
        if n is None or not n.enabled:
            return
        head = what(a).upper()
        title = ("DRILL: " if a["drill"] else "") + ("Acknowledged: " if ack else "") + head
        body = [f"{_hms(a['at'])}" + (f" · radio {a['radio_id']}" if a["radio_id"] else "")]
        if a["text"]:
            body.append(f"heard: \u201c{a['text'][:500]}\u201d")
        if a.get("named"):
            body.append(f"units named: {', '.join(a['named'])}")
        if ack:
            body.append(f"acknowledged by {a['ack_by']} at {_hms(a['ack_at'])}")
        if a["drill"]:
            body.append("DRILL (training): not a real emergency")
        prio = 3 if a["drill"] else (4 if ack else 5)
        tags = ["white_check_mark"] if ack else (["construction"] if a["drill"] else ["rotating_light"])
        click = f"{self.public_url.rstrip('/')}/cad" if self.public_url else ""
        try:
            n.send(title, " · ".join(body), prio, tags, click, who=self.who_of(a))
        except Exception as e:  # noqa: BLE001 - a push never stands in the way of the alarm
            self.log(f"alarms: push not sent: {type(e).__name__}: {e}")

    # -- reading ---------------------------------------------------------------------------------------------

    def _row(self, r: dict) -> dict:
        a = dict(r)
        a["keywords"] = json.loads(a.get("keywords") or "[]")
        a["spans"] = json.loads(a.get("spans") or "[]")
        a["named"] = json.loads(a.get("named") or "[]")
        a["emergency"], a["drill"] = bool(a["emergency"]), bool(a["drill"])
        return a

    def _rows(self, where: str, args=(), limit: int = 50) -> list[dict]:
        rows = self.store.q("SELECT a.*, i.number AS incident_number, i.address AS incident_address, "
                            "i.type AS incident_type FROM alarms a LEFT JOIN incidents i ON i.id = a.incident_id "
                            f"WHERE {where} ORDER BY a.at, a.id LIMIT ?", (*args, limit))
        out = []
        for r in rows:
            a = self._row(r)
            a["what"], a["who"] = what(a), who(a)
            a["talker"] = self.who_of(a)
            try:
                a["has_clip"] = bool(self.cad.has_clip(a["traffic_id"]))
            except Exception:  # noqa: BLE001
                a["has_clip"] = False
            out.append(a)
        return out

    def get(self, aid: int) -> dict:
        rows = self._rows("a.id = ?", (aid,), 1)
        if not rows:
            raise CadError(f"no alarm {aid}")
        return rows[0]

    def state(self, now: float | None = None) -> dict:
        """The screens' part of the CAD state: open alarms (until acknowledged), notices, the just acknowledged."""
        now = self.clock() if now is None else now
        return {"time": now, "drill": bool(self.config()["drill"]),
                "open": self._rows("a.ack_at IS NULL AND a.level = 'alarm'", (), 20),
                "notices": self._rows("a.ack_at IS NULL AND a.level = 'notice' AND a.created_at >= ?", (now - NOTICE_S,), 10),
                "recent": self._rows("a.ack_at >= ?", (now - RECENT_S,), 10)}

    def history(self, limit: int = 200) -> list[dict]:
        rows = self._rows("1 = 1", (), 100000)
        return list(reversed(rows))[:limit]
