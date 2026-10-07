"""The CAD service: what the web API and the monitor bridge call. Validates input (CadError =
a 400 with the reason), keeps the lists, store, run cards, dispatch audio and the transmitter together.

    monitor incident for Port Chester (60 Control page) -> from_monitor() -> queued CAD event
    operator: Event info -> Accept event (numbered; emergency -> Pending events, else Non-emergency)
              -> recommend -> preview -> dispatch (assign, log, audio, NullTransmitter) -> Active events
              -> Close event with a disposition
    radio traffic (monitor) -> record_traffic(): the rig from the radio registry (radios.py), linked
              to the events its unit (or its tactical channel) was on (comms.py) -> /comms, event "Radio traffic"
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from sizeup.cad import dispatcher, export, runcards
from sizeup.cad.comms import Comms
from sizeup.cad.feed import Feed
from sizeup.cad.radios import Registry, RadioError, is_pcfd
from sizeup.cad import settings as settings_mod
from sizeup.cad import lists as lists_mod
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.store import TZ, CadError, Store
from sizeup.cad.transmit import Transmitter, make_transmitter
from sizeup.tts import VoiceCfg
from sizeup.tts import voices as tts_voices

AUDIO_NAME = re.compile(r"[0-9a-f]{20}")
FIRE_KINDS = ("chief", "engine", "ladder", "tower", "rescue", "utility", "marine")
PREVIEW_KEEP_S = 24 * 3600
MAX_UNITS = 30
GEO_WINDOW = 0.25        # suggestions this close to the best one are ordered by town first


@dataclass
class Settings:
    db: str = ""                                   # "" = in memory (tests)
    audio_dir: str = ""                            # "" = next to the db (in memory: a temp dir)
    lists: str = ""                                # "" = data/cad_lists.json
    department: str = "Port Chester Fire"          # first words of every dispatch
    monitor_departments: tuple[str, ...] = ("Port Chester FD",)   # monitor incidents that become queued ones
    tones: tuple[tuple[float, float], ...] = ((349.1, 1.0), (311.1, 3.0))
    voice: str = "Samantha"                        # tts_engine "say": a `say -v` voice; "kokoro": a Kokoro voice
    rate_wpm: int = 0
    tts_engine: str = "say"                        # "kokoro" | "say"; the config's default is kokoro, "say" here
    speed: float = 1.0                             # keeps tests off any running TTS server (sizeup/tts.py)
    tts_port: int = 8093
    fallback_voice: str = "Samantha"
    repeat: int = 2
    transmit: bool = False
    channels: tuple[str, ...] = ("FG1", "PC repeater")
    tactical: tuple[str, ...] = ("FG1",)          # analog fireground channels: their traffic goes on the active events
    incident_talkgroups: tuple[int, ...] = (307, 333)   # P25: where 60 Control answers our units
    radio_seed: str = ""                           # "" = data/radio_ids.json


def _text(body: dict, key: str, max_len: int, required: bool = False) -> str:
    v = body.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{key}: text")
    v = v.strip()
    if required and not v:
        raise CadError(f"{key}: required")
    if len(v) > max_len:
        raise CadError(f"{key}: at most {max_len} characters")
    return v


def _num(body: dict, key: str, lo: float, hi: float) -> float | None:
    v = body.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise CadError(f"{key}: a number from {lo} to {hi}")
    return float(v)


def _priority(body: dict) -> int | None:
    v = body.get("priority")
    if v in (None, ""):
        return None
    if isinstance(v, bool) or v not in (1, 2, 3, "1", "2", "3"):
        raise CadError("priority: 1, 2 or 3")
    return int(v)


def _bool(body: dict, key: str) -> bool | None:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, bool):
        raise CadError(f"{key}: true or false")
    return v


class Cad:
    def __init__(self, s: Settings, geocoder: Callable[[str], list[dict] | None] | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.s, self.geocoder, self.log, self.clock = s, geocoder, log, clock
        self.lists = lists_mod.load(s.lists or None)          # ListsError: the file says what's wrong
        self.store = Store(s.db or ":memory:", clock=clock, lists=self.lists)
        for m in self.store.migrated:
            log(f"CAD: {m}")
        if s.audio_dir:
            self.audio_dir: Path | None = Path(s.audio_dir)
        elif s.db:
            self.audio_dir = Path(s.db).parent / "dispatch-audio"
        else:
            self.audio_dir = None                      # made on first render
        self.transmitter: Transmitter = make_transmitter(s.transmit, log)
        self._render_lock = threading.Lock()           # one `say` at a time
        self._state: tuple[int, dict] | None = None
        self.settings = settings_mod.Editor(self)           # the Settings page's edits over the data file
        self.settings.startup()
        n = self.store.sync_units()
        if n:
            log(f"CAD: added {n} units from {Path(self.lists.path).name}")
        if runcards.seed(self.store, fill=bool(self.store.migrated)):
            log("CAD: seeded default run cards (a stand-in: edit them to PCFD policy)")
        self.radios = Registry(self.store, s.radio_seed or None)
        n = self.radios.seed()
        if n:
            log(f"CAD: seeded {n} radio IDs ({len(self.radios.plan['conflicts'])} conflicts left unmapped)")
        self.comms = Comms(self.store, s.tactical, s.incident_talkgroups, clock=clock)
        self.comms.prune()
        self.feed = Feed(self.store, lambda: self.lists, has_clip=lambda cid: self.has_clip(cid))   # the 60 Control feed
        # Set by the app: who is transmitting now (sizeup/radio/live.py) and whether a clip's audio is still kept.
        self.live: Callable[[], list[dict]] = lambda: []
        self.has_clip: Callable[[str], bool] = lambda cid: False

    @classmethod
    def from_config(cls, cfg, geocoder=None, log=print) -> Cad:
        return cls(Settings(db=cfg.cad_db, lists=cfg.cad_lists, department=cfg.cad_department,
                            monitor_departments=tuple(cfg.cad_monitor_departments), tones=tuple(cfg.dispatch_tones),
                            voice=cfg.dispatch_voice, rate_wpm=cfg.dispatch_rate, repeat=cfg.dispatch_repeat,
                            tts_engine=cfg.dispatch_tts_engine, speed=cfg.dispatch_speed, tts_port=cfg.dispatch_tts_port,
                            fallback_voice=cfg.dispatch_fallback_voice,
                            transmit=cfg.dispatch_transmit, channels=tuple(cfg.dispatch_channels),
                            tactical=tuple(c.label for c in cfg.channels if c.kind == "traffic") or ("FG1",),
                            incident_talkgroups=tuple(sorted(cfg.p25_incident_talkgroups))),
                   geocoder=geocoder, log=log)

    # -- state ----------------------------------------------------------------------------------

    def state(self) -> dict:
        """Pushed to /cad once a second; rebuilt only when something changed."""
        rev = self.store.rev
        if self._state is None or self._state[0] != rev:
            cards = runcards.cards(self.store)
            incs = self._with_radio(self.store.incidents())
            self._state = (rev, {
                "department": self.s.department,
                "lists": self.lists.public(),
                "units": self.store.units(),
                "incidents": incs,
                "unit_log": self.store.unit_log(),
                "run_cards": cards,
                "run_cards_default": any(c["is_default"] for c in cards),
                "transmit": {"enabled": self.transmitter.enabled, "transmitter": self.transmitter.name,
                             "channels": list(self.s.channels),
                             "note": "Transmit is off: logged and played here only"},
                "audio": {"tts": self.s.tts_engine == "kokoro" or bool(shutil.which("say") and shutil.which("afconvert")),
                          "engine": self.s.tts_engine, "voice": self.s.voice, "speed": self.s.speed,
                          "fallback_voice": self.s.fallback_voice, "label": self._voice().label(),
                          "tones": [list(t) for t in self.s.tones]},
                "geocoder": self.geocoder is not None,
                "operators": self.settings.operator_names(),
                "feed": self.feed.state(),
            })
        return dict(self._state[1], time=self.clock(), rev=rev, talking=self.live())

    def _with_radio(self, incs: list[dict], limit: int = 200) -> list[dict]:
        """Each event's radio traffic (newest first), with whether its audio is still kept."""
        by = self.comms.for_incidents([i["id"] for i in incs], limit)
        units = self.store.units()
        for i in incs:
            i["tag_units"] = self.tag_order(i["id"], units)
            rows = by.get(i["id"], [])
            for r in rows:
                r["has_audio"] = self.has_clip(r["id"])
            i["radio"] = rows
        return incs

    # -- events -----------------------------------------------------------------------------------

    def _fields(self, body: dict, partial: bool) -> dict:
        out: dict = {}
        for k, n in (("type", 24), ("subtype", 24), ("received_via", 40)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
        for k, n in (("address", 200), ("cross_street", 200), ("caller_name", 120), ("caller_address", 200),
                     ("phone", 40), ("remarks", 4000), ("geo_label", 200)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
        if "emergency" in body:
            e = _bool(body, "emergency")
            if e is not None:
                out["emergency"] = e
        if not partial or "until_at" in body:
            out["until_at"] = _num(body, "until_at", 1e9, 1e10)
        if not partial or "priority" in body:
            out["priority"] = _priority(body)
        if not partial or "lat" in body or "lon" in body:
            lat, lon = _num(body, "lat", 40.0, 42.0), _num(body, "lon", -75.0, -72.0)
            if (lat is None) != (lon is None):
                raise CadError("lat and lon: both or neither")
            out["lat"], out["lon"] = lat, lon
            if lat is None:
                out["geo_label"] = ""
        return out

    def _auto_geo(self, f: dict) -> None:
        """Location typed but no suggestion picked: place it if the gazetteer is sure; else no pin."""
        if f.get("lat") is not None or not f.get("address") or self.geocoder is None:
            return
        q = f["address"] + (f" cross of {f['cross_street']}" if f.get("cross_street") else "")
        m = next(iter(self._ranked(q)), None)
        if m and m["confidence"] >= 0.6:
            f["lat"], f["lon"], f["geo_label"] = m["lat"], m["lon"], m["label"]

    def create(self, body: dict) -> dict:
        """Accept event (Event info): numbered at once; emergency -> Pending events, else Non-emergency."""
        f = self._fields(body, partial=False)
        self._auto_geo(f)
        iid = self.store.create_incident(**f, source="manual", status="pending", operator=_text(body, "operator", 60))
        return self.store.incident(iid)

    def update(self, iid: int, body: dict) -> dict:
        f = self._fields(body, partial=True)
        if "address" in f and "lat" not in f:      # moved without a picked point: re-place or unpin
            f["lat"] = f["lon"] = None
            f["geo_label"] = ""
            self._auto_geo(f)
        return self.store.update_incident(iid, f, _text(body, "operator", 60))

    def accept(self, iid: int, body: dict) -> dict:
        """A queued 60 Control page accepted (the type can be picked in the same call)."""
        op = _text(body, "operator", 60)
        f = {k: _text(body, k, 24) for k in ("type", "subtype") if k in body}
        if f:
            self.store.update_incident(iid, f, op)
        return self.store.accept(iid, op)

    def dismiss(self, iid: int, body: dict) -> dict:
        return self.store.dismiss(iid, _text(body, "operator", 60), _text(body, "reason", 200))

    def close(self, iid: int, body: dict) -> dict:
        return self.store.close(iid, _text(body, "disposition", 40), _text(body, "operator", 60))

    def note(self, iid: int, body: dict) -> dict:
        """A timeline comment. units: the unit tags picked with the autocomplete ("E63 stated that ..."):
        who said it, so the event can show that unit's notes with its radio traffic."""
        units = body.get("units") or []
        if not isinstance(units, list) or len(units) > 10 or not all(isinstance(u, str) for u in units):
            raise CadError("units: a list of up to 10 unit codes")
        codes = {u["name"].upper(): u["name"] for u in self.store.units() if u["board"] or u["incident_id"]}
        bad = [u for u in units if u.strip().upper() not in codes]
        if bad:
            raise CadError(f"units: {bad[0]!r} is not on the boards")
        tags = list(dict.fromkeys(codes[u.strip().upper()] for u in units))
        self.store.note(iid, _text(body, "text", 2000, required=True), _text(body, "operator", 60), units=tags)
        return {"ok": True, "units": tags}

    def tag_order(self, iid: int | None, units: list[dict] | None = None) -> list[str]:
        """The unit autocomplete's order: units on this event (as assigned) first, then PCFD's and
        Rye Brook's fire units (board order), then everyone else (police, EMS, agencies)."""
        units = units if units is not None else self.store.units()
        first = [r["name"] for r in self.store.q("SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                                                 "WHERE a.incident_id = ? ORDER BY a.cleared_at IS NOT NULL, a.id",
                                                 (iid,))] if iid else []
        fire = [u["name"] for u in units if u["board"] and u["kind"] in FIRE_KINDS and not u["mutual_aid"]]
        rest = [u["name"] for u in units if u["board"]]
        return list(dict.fromkeys(first + fire + rest))

    def suggest_units(self, iid: int | None, prefix: str, limit: int = 8) -> list[str]:
        """Codes starting with what was typed (any case), in tag_order: 'e6' -> E63 (on the event), E59, E60..."""
        p = (prefix or "").strip().lower()
        if not p:
            return []
        return [c for c in self.tag_order(iid) if c.lower().startswith(p)][:limit]

    def recommend(self, iid: int, body: dict) -> dict:
        inc = self.store.incident(iid)
        if not inc["emergency"]:
            on = [u["name"] for u in self.store.units() if u["incident_id"] == iid]
            return {"card": None, "card_label": "", "units": [], "unfilled": [], "already": on,
                    "note": "Non-emergency event: no run card and no dispatch. Attach units if needed."}
        typ = _text(body, "type", 24) or inc["type"]
        sub = _text(body, "subtype", 24) if "type" in body else inc["subtype"]
        r = runcards.recommend(self.store, typ, sub, iid)
        return dict(r, units=[{k: u[k] for k in ("id", "name", "kind", "spare", "status", "slot")} for u in r["units"]])

    def full(self, iid: int) -> dict:
        return self._with_radio([self.store.full(iid)], limit=1000)[0]

    def history(self) -> dict:
        return {"incidents": self.store.history()}

    def admin(self) -> dict:
        return export.admin(self.store)

    def export(self, name: str) -> bytes | None:
        return export.export(self.store, name)

    # -- dispatch -------------------------------------------------------------------------------

    def _units_for(self, body: dict, dispatching: bool = True) -> list[dict]:
        ids = body.get("unit_ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_UNITS:
            raise CadError(f"unit_ids: a list of 1 to {MAX_UNITS} unit ids")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(set(ids)) != len(ids):
            raise CadError("unit_ids: distinct unit ids (numbers)")
        by_id = {u["id"]: u for u in self.store.units()}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise CadError(f"unit_ids: no unit {missing[0]}")
        agency = [by_id[i]["name"] for i in ids if by_id[i]["response"] == "agency"]
        if dispatching and agency:
            raise CadError(f"never dispatched (another agency's unit: attach it to the event when it shows up): {', '.join(agency)}")
        cant = [by_id[i]["name"] for i in ids if self.lists.role(by_id[i]["status"]) == "oos"
                and not (by_id[i]["response"] == "agency" and not dispatching)]
        if cant:
            raise CadError(f"can't be dispatched ({', '.join(self.lists.name('unit_statuses', by_id[i]['status']).lower() for i in ids if by_id[i]['name'] in cant)}): {', '.join(cant)}")
        return [by_id[i] for i in ids]

    def _channel(self, body: dict) -> str:
        ch = _text(body, "channel", 40) or (self.s.channels[0] if self.s.channels else "")
        if self.s.channels and ch not in self.s.channels:
            raise CadError(f"channel: one of {', '.join(self.s.channels)}")
        return ch

    def _voice(self) -> VoiceCfg:
        return VoiceCfg(self.s.tts_engine, self.s.voice, self.s.speed, self.s.tts_port, self.s.fallback_voice,
                        self.s.rate_wpm)

    def tts_voices(self) -> dict:
        """GET /api/cad/tts/voices: the Kokoro voices to pick from and what's set now."""
        return {"engine": self.s.tts_engine, "voice": self.s.voice, "speed": self.s.speed,
                "fallback_voice": self.s.fallback_voice,
                "voices": tts_voices(self.s.tts_port)}

    def _render(self, text: str, location: str = "", tones=None) -> tuple[Path | None, str]:
        """(the WAV, which voice said it: "Voice: Kokoro af_heart") or (None, why there's no audio).
        `location` is no longer needed: every number in the text is said the dispatch way."""
        if self.audio_dir is None:
            import tempfile
            self.audio_dir = Path(tempfile.mkdtemp(prefix="sizeup-cad-audio-"))
        try:
            with self._render_lock:
                return dispatcher.render_dispatch(text, self.s.tones if tones is None else tones, self.audio_dir, self._voice(), self.s.repeat,
                                                  {c: u.get("spoken", "") for c, u in self.lists.units.items()},
                                                  log=self.log)
        except AudioUnavailable as e:
            return None, str(e)
        except OSError as e:
            return None, f"could not write the dispatch audio: {e}"

    def _phrase(self, typ: str, sub: str) -> str:
        """The run card's phrase for TYPE/SUBTYPE; else the data file's, a TYPE card's phrase standing
        for the type's (the subtype is always said: '{type} with multiple injuries')."""
        card = runcards.resolve(self.store, typ, sub) or {}
        if card.get("spoken") and card["type"] == lists_mod.key(typ, sub):
            return card["spoken"]
        return self.lists.phrase(typ, sub, base=card.get("spoken") or "")

    def _departments(self, units: list[dict]) -> tuple[list[str], bool]:
        """The joint-response towns among these units (in the data file's order) and whether one of
        PCFD's own (apparatus kinds, not mutual aid) is among them."""
        L = self.lists
        towns = {L.joint_town(u["name"]) for u in units} - {""}
        own = any(not L.joint_town(u["name"]) and not u.get("mutual_aid")
                  and (u["kind"] in L.full_response_kinds or not L.full_response_kinds) for u in units)
        return [t for t in L.joint_response if t in towns], own

    def _plan(self, inc: dict, units: list[dict], kind: str, full: bool) -> tuple[str, dict]:
        """Lead-in and alert tones for this transmission. Lead-in: by the units sent and those already on
        the event ('Port Chester and Rye Brook Fire', or 'Rye Brook Fire' if only theirs). Tones: the
        departments of the units sent (an upgrade or full response: also everyone on the event)."""
        L = self.lists
        sending = {u["id"] for u in units}
        on = [u for u in self.store.units() if u["incident_id"] == inc["id"] and u["id"] not in sending]
        joint, own = self._departments(units + on)
        lead = dispatcher.lead_in(self.s.department, joint, own, full, L.wording, L.lead_in_joint_only)
        t_joint, t_own = self._departments(units + on if kind == "upgrade" or full else units)
        home = dispatcher.HOME_MUNI
        groups = ([(home, list(self.s.tones))] if t_own or full or not t_joint else []) + \
                 [(t, L.dispatch_tones.get(t)) for t in t_joint]
        return lead, dispatcher.tone_plan(groups, home, list(self.s.tones))

    def _text_for(self, inc: dict, units: list[dict], kind: str, full: bool = False, info: str = "",
                  new_phrase: str = "", remarks: str = "") -> tuple[str, dict]:
        """The dispatch wording (data/cad_lists.json dispatch_wording) and its tone plan. An add-on says who
        is already on the event, by status (already_statuses), leaving out the units being sent."""
        L = self.lists
        lead, tones = self._plan(inc, units, kind, full)
        already = []
        if kind == "add_on":
            sending = {u["id"] for u in units}
            order = [r["unit_id"] for r in self.store.q("SELECT unit_id FROM assignments WHERE incident_id = ? AND "
                                                        "cleared_at IS NULL ORDER BY id", (inc["id"],))]
            on = sorted((u for u in self.store.units() if u["incident_id"] == inc["id"] and u["id"] not in sending
                         and u["response"] != "agency"),                  # EMS / PD are never said on the air
                        key=lambda u: order.index(u["id"]) if u["id"] in order else len(order))   # in the order sent
            for st in L.already_statuses:
                names = [u["spoken"] or u["name"] for u in on if u["status"] == st]
                if names:
                    already.append((L.status_phrase(st), names))
        return dispatcher.compose(kind, department=lead, incident=inc,
                                  units=[u["spoken"] or u["name"] for u in units], phrase=self._phrase(inc["type"], inc["subtype"]),
                                  at=self.clock(), full=full, already=already, info=info, new_phrase=new_phrase,
                                  wording=L.wording, remarks=remarks), tones

    def _dispatchable(self, inc: dict) -> None:
        if inc["status"] == "queued":
            raise CadError("accept the event first")
        if inc["status"] not in ("pending", "active"):
            raise CadError(f"event is {inc['status']}")
        if not inc["emergency"]:
            raise CadError("non-emergency event: no dispatch (attach units instead, or mark it an emergency)")

    def _audio_url(self, path: Path | None) -> str | None:
        return f"/api/cad/audio/{path.stem}.wav" if path else None

    @staticmethod
    def _remarks(body: dict) -> str:
        return dispatcher.clean_remarks(_text(body, "remarks", dispatcher.REMARKS_MAX))

    @staticmethod
    def _tones_text(plan: dict) -> str:
        """'Port Chester + Rye Brook (not set → PC). Rye Brook tones not set — played Port Chester's.'"""
        return ". ".join([plan["label"]] + plan["warnings"])

    def _shown(self, text: str, kind: str, path: Path | None, note: str, remarks: str, plan: dict) -> dict:
        """What a preview (and a dispatch) answers: the text and audio, the remarks and tones used."""
        return {"text": text, "kind": kind, "audio_url": self._audio_url(path), "audio_note": note, "remarks": remarks,
                "tones": plan["label"], "tone_warnings": plan["warnings"]}

    def _send(self, iid: int, text: str, units: list[dict], operator: str, channel: str, kind: str, moved=(),
              remarks: str = "", plan: dict | None = None) -> dict:
        plan = plan or dispatcher.tone_plan([], dispatcher.HOME_MUNI, list(self.s.tones))
        path, note = self._render(text, dispatcher.where(self.store.incident(iid)), plan["groups"])
        tx = self.transmitter.transmit(path, channel, text)
        did = self.store.log_dispatch(iid, operator=operator, text=text, unit_ids=[u["id"] for u in units],
                                      audio_path=str(path) if path else "", audio_note=note, transmitted=tx.transmitted,
                                      channel=channel, tx_note=tx.detail, kind=kind, remarks=remarks,
                                      tones=self._tones_text(plan))
        return dict(self._shown(text, kind, path, note, remarks, plan), dispatch_id=did,
                    audio_url=f"/api/cad/dispatch/{did}.wav" if path else None,
                    transmitted=tx.transmitted, detail=tx.detail, channel=channel, moved=list(moved))

    def preview(self, iid: int, body: dict) -> dict:
        """The dispatch as it would go out now, to hear first. Nothing is assigned or logged.
        An event that already had a dispatch gets the add-on wording."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("queued", "pending", "active"):
            raise CadError(f"event is {inc['status']}")
        if not inc["emergency"]:
            raise CadError("non-emergency event: no dispatch (attach units instead, or mark it an emergency)")
        units = self._units_for(body)
        remarks = self._remarks(body)
        kind = "add_on" if self.store.has_dispatch(iid) else "new"
        text, plan = self._text_for(inc, units, kind, full=bool(_bool(body, "full")), remarks=remarks)
        path, note = self._render(text, dispatcher.where(inc), plan["groups"])
        self._prune_audio()
        return self._shown(text, kind, path, note, remarks, plan)

    def dispatch(self, iid: int, body: dict) -> dict:
        """Dispatch the ticked units (full: 'full department response' instead of their names).
        An event that already had a dispatch: 'Engine 61, added to the assignment...'."""
        inc = self.store.incident(iid)
        self._dispatchable(inc)
        operator = _text(body, "operator", 60, required=True)
        units = self._units_for(body)
        remarks = self._remarks(body)
        channel = self._channel(body)
        kind = "add_on" if self.store.has_dispatch(iid) else "new"
        text, plan = self._text_for(inc, units, kind, full=bool(_bool(body, "full")), remarks=remarks)
        moved = self.store.assign(iid, [u["id"] for u in units], operator,
                                  verb="added to the assignment" if kind == "add_on" else "")
        return self._send(iid, text, units, operator, channel, "add-on" if kind == "add_on" else "dispatch", moved,
                          remarks, plan)

    def upgrade(self, iid: int, body: dict) -> dict:
        """Re-dispatch with new information: optional new type/subtype, a line of new information,
        optional extra units, full department response. {"preview": true} only renders it.
        'Port Chester Fire, upgrade on your last alarm, 169 North Main Street. Smoke in the
        structure. Now a structure fire, full department response. Time out 15:20.'"""
        inc = self.store.incident(iid)
        L = self.lists
        if inc["status"] not in ("pending", "active"):
            raise CadError("accept the event first" if inc["status"] == "queued" else f"event is {inc['status']}")
        if not self.store.has_dispatch(iid):
            raise CadError("nothing dispatched yet: dispatch it first")
        typ, sub = (_text(body, "type", 24), _text(body, "subtype", 24)) if body.get("type") else (inc["type"], inc["subtype"])
        try:
            L.check_type(typ, sub)
        except ValueError as e:
            raise CadError(str(e)) from None
        changed = (typ, sub) != (inc["type"], inc["subtype"])
        if not (inc["emergency"] or (changed and L.is_emergency(typ, sub))):
            raise CadError("non-emergency event: no dispatch (mark it an emergency first)")
        info = _text(body, "info", 300)
        remarks = self._remarks(body)
        full = bool(_bool(body, "full"))
        units = self._units_for(body) if body.get("unit_ids") else []
        if not (changed or info or full or units or remarks):
            raise CadError("nothing to upgrade: give a new type, new information, remarks, units or full department response")
        text, plan = self._text_for(inc, units, "upgrade", full=full, info=info,
                                    new_phrase=self._phrase(typ, sub) if changed else "", remarks=remarks)
        if body.get("preview"):
            path, note = self._render(text, dispatcher.where(inc), plan["groups"])
            return self._shown(text, "upgrade", path, note, remarks, plan)
        operator = _text(body, "operator", 60, required=True)
        channel = self._channel(body)
        what = []
        if changed:
            what.append(f"{L.type_label(inc['type'], inc['subtype'])} → {L.type_label(typ, sub)}")
            self.store.update_incident(iid, {"type": typ, "subtype": sub}, operator)
        if info:
            what.append(info)
        if full:
            what.append(L.wording["full_response"])
        self.store.note(iid, "Upgrade: " + "; ".join(what or ["more units"]), operator, kind="upgrade")
        moved = self.store.assign(iid, [u["id"] for u in units], operator, verb="dispatched (upgrade)") if units else []
        return self._send(iid, text, units, operator, channel, "upgrade", moved, remarks, plan)

    def attach(self, iid: int, body: dict) -> dict:
        """Units on an event with no dispatch (a non-emergency event: a hydrant, a closure)."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("pending", "active"):
            raise CadError("accept the event first" if inc["status"] == "queued" else f"event is {inc['status']}")
        units = self._units_for(body, dispatching=False)
        op = _text(body, "operator", 60)
        ours = [u["id"] for u in units if u["response"] != "agency"]
        agency = [u["id"] for u in units if u["response"] == "agency"]
        moved = self.store.assign(iid, ours, op, attach=True) if ours else []
        if agency:          # another agency's unit that showed up: "77A4 on location", never "dispatched"
            moved += self.store.assign(iid, agency, op, attach=True, quiet=True)
            onscene = self.lists.agency_statuses()[0]
            for i in agency:
                self.store.set_unit_status(i, onscene, op)
        return {"ok": True, "moved": moved}

    def _prune_audio(self) -> None:
        """Previews nobody dispatched are dropped after a day; dispatch audio is kept with the log."""
        if self.audio_dir is None or not self.audio_dir.is_dir():
            return
        keep = {Path(r["audio_path"]).name for r in self.store.q("SELECT audio_path FROM dispatches WHERE audio_path != ''")}
        cutoff = time.time() - PREVIEW_KEEP_S
        for f in self.audio_dir.glob("*.wav"):
            try:
                if f.name not in keep and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def dispatch_audio(self, did: int) -> bytes | None:
        row = self.store.dispatch_row(did)
        p = Path(row["audio_path"]) if row and row["audio_path"] else None
        return p.read_bytes() if p and p.is_file() else None

    def preview_audio(self, name: str) -> bytes | None:
        if not AUDIO_NAME.fullmatch(name) or self.audio_dir is None:
            return None
        p = self.audio_dir / f"{name}.wav"
        return p.read_bytes() if p.is_file() else None

    # -- units and run cards ----------------------------------------------------------------------

    def unit_status(self, uid: int, body: dict) -> dict:
        status = _text(body, "status", 40, required=True)
        return self.store.set_unit_status(uid, status, _text(body, "operator", 60), _text(body, "note", 200))

    def run_cards(self) -> list[dict]:
        return runcards.cards(self.store)

    def save_run_card(self, body: dict) -> dict:
        k, rename = _text(body, "type", 50, required=True), _text(body, "rename_from", 50)
        old = runcards.card(self.store, rename or k)
        c = runcards.save(self.store, k, body.get("slots"), _text(body, "spoken", 120), rename)
        self.settings.audit_card(_text(body, "operator", 60), old, c)
        return c

    def delete_run_card(self, body: dict) -> dict:
        k = _text(body, "type", 50, required=True)
        old = runcards.card(self.store, k)
        runcards.delete(self.store, k)
        self.settings.audit_card(_text(body, "operator", 60), old, None)
        return {"ok": True}

    # -- radios and communications ---------------------------------------------------------------

    def radio_registry(self) -> dict:
        return self.radios.public()

    def save_radio(self, body: dict) -> dict:
        try:
            return self.radios.save(body, _text(body, "operator", 60))
        except RadioError as e:
            raise CadError(str(e)) from None

    def delete_radio(self, body: dict) -> dict:
        try:
            self.radios.delete(_text(body, "system", 8, required=True), _text(body, "radio_id", 12, required=True),
                               _text(body, "operator", 60))
        except RadioError as e:
            raise CadError(str(e)) from None
        return {"ok": True}

    def record_traffic(self, t: dict, replace: bool = True) -> list[int]:
        """A transcribed transmission from the monitor: logged, and linked to the events its rig
        (or its tactical channel) was on. Returns the event ids."""
        iids = self.comms.record(t, replace=replace)
        if iids and replace:
            nums = [r["number"] or str(r["id"]) for r in self.store.q(
                f"SELECT id, number FROM incidents WHERE id IN ({','.join('?' * len(iids))})", iids)]
            self.log(f"CAD: {t.get('source')} {t.get('rig') or t.get('unit') or 'transmission'} -> {', '.join(nums)}")
        return iids

    def comms_query(self, q: dict) -> dict:
        """GET /api/cad/comms: {rows, rev, channels, rigs, pcfd, talking}. ?rev=N: {unchanged: true}
        when nothing was logged since N (the page polls)."""
        def one(k: str) -> str:
            v = (q.get(k) or [""])[0].strip()
            if len(v) > 60:
                raise CadError(f"{k}: at most 60 characters")
            return v

        def when(k: str) -> float | None:
            v = one(k)
            if not v:
                return None
            try:
                return float(v)
            except ValueError:
                raise CadError(f"{k}: unix seconds") from None
        rev = one("rev")
        talking = self.live()
        if rev and rev == f"{self.comms.rev}.{self.store.rev}":
            return {"unchanged": True, "rev": rev, "talking": talking}
        pcfd = [u["name"] for u in self.radios.units if is_pcfd(u)]
        try:
            limit = int(one("limit") or 300)
        except ValueError:
            raise CadError("limit: a number") from None
        rows = self.comms.query(unit=one("unit"), channel=one("channel"), event=one("event"),
                                pcfd=pcfd if one("pcfd") in ("1", "true", "on") else None,
                                since=when("since"), until=when("until"), limit=limit,
                                kind={"radio": "radio", "notes": "notes"}.get(one("type"), ""))
        for r in rows:
            r["has_audio"] = r["kind"] == "radio" and self.has_clip(r["id"])
        return {"rows": rows, "rev": f"{self.comms.rev}.{self.store.rev}", "pcfd": pcfd, "talking": talking, **self.comms.facets(),
                "events": [{"id": r["id"], "number": r["number"], "type": r["type"], "address": r["address"]}
                           for r in self.store.q("SELECT DISTINCT i.id, i.number, i.type, i.address FROM radio_links l "
                                                 "JOIN incidents i ON i.id = l.incident_id ORDER BY i.id DESC LIMIT 100")]}

    def feed_query(self, q: dict) -> dict:
        """GET /api/cad/feed?limit=&tg=307,333: the 60 Control feed (default: the data file's talkgroups)."""
        try:
            limit = int((q.get("limit") or ["0"])[0] or 0) or None
            tgs = tuple(int(x) for x in (q.get("tg") or [""])[0].split(",") if x.strip()) or None
        except ValueError:
            raise CadError("limit, tg: numbers (tg: comma-separated talkgroups)") from None
        return dict(self.feed.state(), rows=self.feed.rows(limit, tgs))

    def link_traffic(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/link {traffic_id, operator}: "+ event" on the 60 Control feed."""
        inc = self.store.incident(iid)
        if inc["status"] == "dismissed":
            raise CadError("event is dismissed")
        r = self.comms.link(_text(body, "traffic_id", 200, required=True), iid, _text(body, "operator", 60))
        if r.get("error"):
            raise CadError(r["error"])
        return dict(r, incident_id=iid, number=inc["number"])

    def rigs(self) -> dict:
        """The board units for the alert settings (PCFD's own flagged)."""
        return {"units": [{"name": u["name"], "kind": u["kind"], "pcfd": is_pcfd(u), "department": u["department"]}
                          for u in self.store.units() if u["board"]]}
    # -- settings (sizeup/cad/settings.py) ---------------------------------------------------------

    def settings_get(self) -> dict:
        return self.settings.page()

    def settings_post(self, section: str, body: dict) -> dict:
        return self.settings.post(section, body)

    def settings_export(self) -> dict:
        return self.settings.export()

    def settings_audit_log(self, q: dict) -> dict:
        return {"audit": self.settings.audit_log((q.get("section") or [""])[0][:40])}

    # -- location suggestions -----------------------------------------------------------------------

    def _ranked(self, q: str) -> list[dict]:
        """The gazetteer's readings, ours first: a town typed in the box wins; then, among the
        readings about as good as the best one, the towns in geo_town_order (Port Chester, Rye
        Brook, the towns around); then the rest. Each says its town."""
        L = self.lists
        ms = [dict(m) for m in (self.geocoder(q) or [])] if self.geocoder else []
        if not ms:
            return ms
        typed = L.towns_typed(q)
        best = max(m["confidence"] for m in ms)
        for m in ms:
            m["town"] = L.town_of(m.get("municipality") or "")
            if m["town"] and not m["label"].lower().endswith(m["town"].lower()):
                m["label"] = f"{m['label']}, {m['town']}"
        return sorted(ms, key=lambda m: (m["town"] not in typed, m["confidence"] < best - GEO_WINDOW,
                                         L.town_rank(m["town"]), -m["confidence"]))

    def geocode(self, q: str) -> dict:
        q = (q or "").strip()
        if not q or len(q) > 200:
            raise CadError("q: 1 to 200 characters")
        if self.geocoder is None:
            return {"matches": [], "error": "geocoder unavailable"}
        return {"matches": [m for m in self._ranked(q) if m["confidence"] >= 0.2][:6]}

    # -- the bridge from the monitor ----------------------------------------------------------------

    def from_monitor(self, inc) -> dict | None:
        """A monitor incident (60 Control page) for one of our departments -> a queued CAD event,
        once per monitor incident. A repeat of the page fills in what the first reading missed
        while it is still queued; nothing changes once a person has accepted it."""
        if not any(d in self.s.monitor_departments for d in inc.departments):
            return None
        ref = f"monitor:{inc.id}"
        geo = inc.geo or {}
        heard = (inc.location or "").title()
        address = geo.get("label") if geo.get("kind") in ("address", "intersection", "landmark") else heard
        lines = []
        for pid_text, start in zip(inc.page_text, inc.page_starts or [inc.opened] * len(inc.page_text)):
            lines.append(f"60 Control page {datetime.fromtimestamp(start, TZ):%H:%M}: {pid_text}")
        facts = [f"type heard: {inc.incident_type}"]
        if inc.code:
            facts.append(f"code {inc.code}")
        if inc.units:
            facts.append(f"units named on the page: {', '.join(inc.units)}")
        if heard:
            facts.append(f"location as heard: {heard}" + (f" (map: {geo['label']}, {geo.get('confidence', 0):.2f})"
                                                         if geo.get("label") else " (not found on the map)"))
        if inc.time_out:
            facts.append(f"county time out {inc.time_out}")
        remarks = "\n".join(lines + ["; ".join(facts)])[:4000]
        typ, sub, note = runcards.from_page(inc.incident_type, inc.category, " ".join(inc.page_text))
        try:
            self.lists.check_type(typ, sub, required=False)
        except ValueError:                             # a data file without that code: leave it to the operator
            typ, sub, note = "", "", note or f"type heard: {inc.incident_type}"
        f = {"type": typ, "subtype": sub, "type_note": note,
             "address": (address or "")[:200], "cross_street": (inc.cross_street or "").title()[:200],
             "remarks": remarks, "lat": geo.get("lat"), "lon": geo.get("lon"), "geo_label": (geo.get("label") or "")[:200]}
        old = self.store.by_source_ref(ref)
        if old is None:
            via = "60 CONTROL" if "60 CONTROL" in self.lists.received_codes else ""
            iid = self.store.create_incident(**f, received_via=via, source="60 Control page", source_ref=ref,
                                             status="queued")
            self.log(f"CAD: from 60 Control: {self.lists.type_label(typ, sub) or 'type not set'} at "
                     f"{f['address'] or '(no location)'}")
            return self.store.incident(iid)
        if old["status"] != "queued":
            return old
        fill = {k: v for k, v in f.items() if k == "remarks" or
                (k not in ("type", "subtype", "type_note") and v not in (None, "") and old[k] in (None, ""))}
        if not old["type"] and typ:                    # the repeat said what it is
            fill.update(type=typ, subtype=sub, type_note=note)
        return self.store.update_incident(old["id"], fill, log=False)
