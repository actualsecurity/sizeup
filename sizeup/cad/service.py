"""The CAD service: what the web API and the monitor bridge call. Validates input (CadError =
a 400 with the reason), keeps the store, run cards, dispatch audio and the transmitter together.

    monitor incident for Port Chester (60 Control page) -> from_monitor() -> pending CAD incident
    operator: accept -> recommend -> preview -> dispatch (assign, log, audio, NullTransmitter)
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from sizeup.cad import dispatcher, runcards
from sizeup.cad.dispatcher import AudioUnavailable
from sizeup.cad.store import FREE, KINDS, LABELS, STATUSES, TZ, CadError, Store
from sizeup.cad.transmit import Transmitter, make_transmitter

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIO_NAME = re.compile(r"[0-9a-f]{20}")
PREVIEW_KEEP_S = 24 * 3600
MAX_UNITS = 30


@dataclass
class Settings:
    db: str = ""                                   # "" = in memory (tests)
    audio_dir: str = ""                            # "" = next to the db (in memory: a temp dir)
    department: str = "Port Chester Fire"          # first words of every dispatch
    roster_department: str = "Port Chester"        # "department" column of the apparatus CSV
    roster_csv: str = str(ROOT / "data" / "westchester_apparatus.csv")
    callsigns: dict[str, str] = field(default_factory=dict)   # "2398" -> "2398 Duty Officer"
    monitor_departments: tuple[str, ...] = ("Port Chester FD",)   # monitor incidents that become pending ones
    tones: tuple[tuple[float, float], ...] = ((349.1, 1.0), (311.1, 3.0))
    voice: str = "Samantha"
    rate_wpm: int = 0
    repeat: int = 2
    transmit: bool = False
    channels: tuple[str, ...] = ("FG1", "PC repeater")


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


class Cad:
    def __init__(self, s: Settings, geocoder: Callable[[str], list[dict] | None] | None = None,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time):
        self.s, self.geocoder, self.log, self.clock = s, geocoder, log, clock
        self.store = Store(s.db or ":memory:", clock=clock)
        if s.audio_dir:
            self.audio_dir: Path | None = Path(s.audio_dir)
        elif s.db:
            self.audio_dir = Path(s.db).parent / "dispatch-audio"
        else:
            self.audio_dir = None                      # made on first render
        self.transmitter: Transmitter = make_transmitter(s.transmit, log)
        self._render_lock = threading.Lock()           # one `say` at a time
        self._state: tuple[int, dict] | None = None
        try:
            n = self.store.seed_units(s.roster_csv, s.roster_department, s.callsigns)
            if n:
                log(f"CAD: seeded {n} units from {Path(s.roster_csv).name} ({s.roster_department})")
        except OSError as e:
            log(f"CAD: no roster ({e}); add units before dispatching")
        if runcards.seed(self.store):
            log("CAD: seeded default run cards (a stand-in: edit them to PCFD policy)")

    @classmethod
    def from_config(cls, cfg, geocoder=None, log=print) -> Cad:
        return cls(Settings(db=cfg.cad_db, department=cfg.cad_department, roster_department=cfg.cad_roster_department,
                            roster_csv=cfg.apparatus_path or Settings.roster_csv, callsigns=dict(cfg.unit_callsigns),
                            monitor_departments=tuple(cfg.cad_monitor_departments), tones=tuple(cfg.dispatch_tones),
                            voice=cfg.dispatch_voice, rate_wpm=cfg.dispatch_rate, repeat=cfg.dispatch_repeat,
                            transmit=cfg.dispatch_transmit, channels=tuple(cfg.dispatch_channels)),
                   geocoder=geocoder, log=log)

    # -- state ----------------------------------------------------------------------------------

    def state(self) -> dict:
        """Pushed to /cad once a second; rebuilt only when something changed."""
        rev = self.store.rev
        if self._state is None or self._state[0] != rev:
            cards = runcards.cards(self.store)
            self._state = (rev, {
                "department": self.s.department,
                "units": self.store.units(),
                "incidents": self.store.incidents(),
                "unit_log": self.store.unit_log(),
                "run_cards": cards,
                "run_cards_default": any(c["is_default"] for c in cards),
                "kinds": list(KINDS),
                "statuses": [{"id": k, "label": LABELS[k]} for k in STATUSES],
                "transmit": {"enabled": self.transmitter.enabled, "transmitter": self.transmitter.name,
                             "channels": list(self.s.channels),
                             "note": "Transmit is off: logged and played here only"},
                "audio": {"tts": bool(shutil.which("say") and shutil.which("afconvert")), "voice": self.s.voice,
                          "tones": [list(t) for t in self.s.tones]},
                "geocoder": self.geocoder is not None,
            })
        return dict(self._state[1], time=self.clock(), rev=rev)

    # -- incidents --------------------------------------------------------------------------------

    def _fields(self, body: dict, partial: bool) -> dict:
        out: dict = {}
        if not partial or "type" in body:
            typ = _text(body, "type", 60, required=True)
            if runcards.card(self.store, typ) is None:
                raise CadError(f"type: no run card {typ!r}")
            out["type"] = typ
        for k, n in (("address", 200), ("cross_street", 200), ("narrative", 4000), ("geo_label", 200)):
            if not partial or k in body:
                out[k] = _text(body, k, n)
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
        """Address typed but no suggestion picked: place it if the gazetteer is sure; else no pin."""
        if f.get("lat") is not None or not f.get("address") or self.geocoder is None:
            return
        q = f["address"] + (f" cross of {f['cross_street']}" if f.get("cross_street") else "")
        m = next(iter(self.geocoder(q) or []), None)
        if m and m["confidence"] >= 0.6:
            f["lat"], f["lon"], f["geo_label"] = m["lat"], m["lon"], m["label"]

    def create(self, body: dict) -> dict:
        f = self._fields(body, partial=False)
        self._auto_geo(f)
        iid = self.store.create_incident(**f, source="manual", status="active",
                                         operator=_text(body, "operator", 60))
        return self.store.incident(iid)

    def update(self, iid: int, body: dict) -> dict:
        f = self._fields(body, partial=True)
        if "address" in f and "lat" not in f:      # moved without a picked point: re-place or unpin
            f["lat"] = f["lon"] = None
            f["geo_label"] = ""
            self._auto_geo(f)
        return self.store.update_incident(iid, f, _text(body, "operator", 60))

    def accept(self, iid: int, body: dict) -> dict:
        return self.store.accept(iid, _text(body, "operator", 60))

    def dismiss(self, iid: int, body: dict) -> dict:
        return self.store.dismiss(iid, _text(body, "operator", 60), _text(body, "reason", 200))

    def close(self, iid: int, body: dict) -> dict:
        return self.store.close(iid, _text(body, "operator", 60))

    def note(self, iid: int, body: dict) -> dict:
        self.store.note(iid, _text(body, "text", 2000, required=True), _text(body, "operator", 60))
        return {"ok": True}

    def recommend(self, iid: int, body: dict) -> dict:
        inc = self.store.incident(iid)
        typ = _text(body, "type", 60) or inc["type"]
        r = runcards.recommend(self.store, typ, iid)
        return dict(r, units=[{k: u[k] for k in ("id", "name", "kind", "spare", "status", "slot")} for u in r["units"]])

    # -- dispatch -------------------------------------------------------------------------------

    def _units_for(self, body: dict) -> list[dict]:
        ids = body.get("unit_ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_UNITS:
            raise CadError(f"unit_ids: a list of 1 to {MAX_UNITS} unit ids")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(set(ids)) != len(ids):
            raise CadError("unit_ids: distinct unit ids (numbers)")
        by_id = {u["id"]: u for u in self.store.units()}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise CadError(f"unit_ids: no unit {missing[0]}")
        oos = [by_id[i]["name"] for i in ids if by_id[i]["status"] == "out_of_service"]
        if oos:
            raise CadError(f"out of service: {', '.join(oos)}")
        return [by_id[i] for i in ids]

    def _channel(self, body: dict) -> str:
        ch = _text(body, "channel", 40) or (self.s.channels[0] if self.s.channels else "")
        if self.s.channels and ch not in self.s.channels:
            raise CadError(f"channel: one of {', '.join(self.s.channels)}")
        return ch

    def _render(self, text: str) -> tuple[Path | None, str]:
        if self.audio_dir is None:
            import tempfile
            self.audio_dir = Path(tempfile.mkdtemp(prefix="sizeup-cad-audio-"))
        try:
            with self._render_lock:
                return dispatcher.render_audio(text, self.s.tones, self.audio_dir, self.s.voice, self.s.rate_wpm,
                                               self.s.repeat), ""
        except AudioUnavailable as e:
            return None, str(e)
        except OSError as e:
            return None, f"could not write the dispatch audio: {e}"

    def _text_for(self, inc: dict, units: list[dict]) -> str:
        return dispatcher.build_text(self.s.department, inc, [u["name"] for u in units],
                                     runcards.card(self.store, inc["type"]), at=self.clock())

    def preview(self, iid: int, body: dict) -> dict:
        """The dispatch as it would go out now, to hear first. Nothing is assigned or logged."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("pending", "active"):
            raise CadError(f"incident is {inc['status']}")
        units = self._units_for(body)
        text = self._text_for(inc, units)
        path, note = self._render(text)
        self._prune_audio()
        return {"text": text, "audio_url": f"/api/cad/audio/{path.stem}.wav" if path else None, "audio_note": note}

    def dispatch(self, iid: int, body: dict) -> dict:
        inc = self.store.incident(iid)
        if inc["status"] == "pending":
            raise CadError("accept the incident first")
        if inc["status"] != "active":
            raise CadError(f"incident is {inc['status']}")
        operator = _text(body, "operator", 60, required=True)
        units = self._units_for(body)
        channel = self._channel(body)
        moved = self.store.assign(iid, [u["id"] for u in units], operator)
        text = self._text_for(self.store.incident(iid), units)
        path, note = self._render(text)
        tx = self.transmitter.transmit(path, channel, text)
        did = self.store.log_dispatch(iid, operator=operator, text=text, unit_ids=[u["id"] for u in units],
                                      audio_path=str(path) if path else "", audio_note=note,
                                      transmitted=tx.transmitted, channel=channel, tx_note=tx.detail)
        return {"dispatch_id": did, "text": text, "audio_url": f"/api/cad/dispatch/{did}.wav" if path else None,
                "audio_note": note, "transmitted": tx.transmitted, "detail": tx.detail, "channel": channel,
                "moved": moved}

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
        return runcards.save(self.store, _text(body, "type", 60, required=True), body.get("slots"),
                             _text(body, "spoken", 120), _text(body, "rename_from", 60))

    def delete_run_card(self, body: dict) -> dict:
        typ = _text(body, "type", 60, required=True)
        if self.store.one("SELECT 1 AS x FROM incidents WHERE type = ? AND status IN ('pending', 'active')", (typ,)):
            raise CadError(f"{typ!r} is the type of an open incident: change that first")
        runcards.delete(self.store, typ)
        return {"ok": True}

    def geocode(self, q: str) -> dict:
        q = (q or "").strip()
        if not q or len(q) > 200:
            raise CadError("q: 1 to 200 characters")
        if self.geocoder is None:
            return {"matches": [], "error": "geocoder unavailable"}
        return {"matches": [m for m in (self.geocoder(q) or []) if m["confidence"] >= 0.2][:6]}

    # -- the bridge from the monitor ----------------------------------------------------------------

    def from_monitor(self, inc) -> dict | None:
        """A monitor incident (60 Control page) for one of our departments -> a pending CAD
        incident, once per monitor incident. A repeat of the page fills in what the first
        reading missed while it is still pending; nothing changes once a person has accepted it."""
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
        narrative = "\n".join(lines + ["; ".join(facts)])[:4000]
        f = {"type": runcards.from_page(inc.incident_type, inc.category, " ".join(inc.page_text)),
             "address": (address or "")[:200], "cross_street": (inc.cross_street or "").title()[:200],
             "narrative": narrative, "lat": geo.get("lat"), "lon": geo.get("lon"),
             "geo_label": (geo.get("label") or "")[:200]}
        if runcards.card(self.store, f["type"]) is None:
            f["type"] = "Other" if runcards.card(self.store, "Other") else f["type"]
        old = self.store.by_source_ref(ref)
        if old is None:
            iid = self.store.create_incident(**f, source="60 Control page", source_ref=ref, status="pending")
            self.log(f"CAD: pending {f['type']} at {f['address'] or '(no address)'} from 60 Control page")
            return self.store.incident(iid)
        if old["status"] != "pending":
            return old
        fill = {k: v for k, v in f.items() if k == "narrative" or (v not in (None, "") and old[k] in (None, ""))}
        if old["type"] == "Other" and f["type"] != "Other":    # the repeat said what it is
            fill["type"] = f["type"]
        return self.store.update_incident(old["id"], fill, log=False)
