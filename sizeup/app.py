"""sizeup service: receivers -> transcription -> incidents -> web display.

    python -m sizeup --config config.toml        # open http://localhost:8090/

    paging channel (South 2) --squelch--> transmission --tones?--> Page
        page for one of our departments --STT--> dispatch parse --> incident (geocoded)
        voice-only transmission --STT--> dispatcher traffic (attached to the active incident),
            or the voice half of a tones-only page sent as a separate transmission
    traffic channels (FG1)    --squelch--> transmission --STT--> traffic on the active incident
    P25 (trunk-recorder dir)  --call--> STT --> traffic on the active incident (unit IDs kept)
    incident for Port Chester --> an event in the CAD (/cad, sizeup/cad/) waiting for a person to accept it
    every transmission: its radio ID (MDC / P25) -> the rig it's on (sizeup/cad/radios.py) -> the CAD's
        communications log, linked to the events that rig is assigned to (/comms, sizeup/cad/comms.py)
    who is keying up now (squelch + live MDC decode on FG1; trunk-recorder's log for P25):
        sizeup/radio/live.py -> "talking" in both pages' state
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from sizeup.config import Config, ConfigError, load
from sizeup import janitor
from sizeup.cad import Cad
from sizeup.cad.api import CadApi
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic
from sizeup.radio.fm import FRAME, RATE, Channel, NfmReceiver, Segment, data_frames, speech_seconds
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio.p25 import Call, CallIngest
from sizeup.radio import claims
from sizeup.radio.live import LiveTalk
from sizeup.radio import sdr as sdr_lookup
from sizeup.radio.recorder import TrunkRecorder
from sizeup.radio.page import Page, assemble, page_from_dict
from sizeup.radio.tones import TonePlan
from sizeup.talkers import talker_prompt
from sizeup.stt import LIVE, RERUN, Transcriber, WhisperClient, radio_voice, wav_bytes
from sizeup.talkgroups import TalkgroupPrefs
from sizeup.web.server import WebServer

ROOT = Path(__file__).resolve().parent.parent
SPLIT_PAGE_S = 30.0      # voice this soon after a tones-only page for us = that page's voice
P25_MODE = "Fire/EMS 15"   # the shared-dongle mode that runs trunk-recorder
OFF_MODE = "Off"           # the shared dongle released for other programs (sizeup keeps running)
SWITCH_PAUSE_S = 1.0       # between stopping one program on the shared dongle and starting the next
MIN_SPEECH_S = 0.15      # less sound than this above the dead-air floor (and no tones) = a mic click
LIVE_WATCH_S = 0.2       # FG1 talking watcher: how often the squelch is looked at (and the MDC ID tried)
LIVE_ID_WINDOW_S = 4.0   # ... the live MDC decode looks at the first seconds only (pre-IDs; APX 0.3-0.8 s in)
CATEGORIES = {"fire": "#ff4d3d", "ems": "#4da3ff", "rescue": "#ffb020", "hazard": "#c86bff",
              "service": "#3ddc97", "unknown": "#9aa0b4"}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class WhisperService:
    """Keep whisper-server running (model stays loaded). A server sizeup started earlier (recorded
    in state_path) is reused when its model/flags still match the config and replaced when they
    don't; a server on the port that sizeup didn't start is used as-is, never killed."""

    def __init__(self, binary: str, model: str, port: int, threads: int, beam_size: int = 0,
                 state_path: str | Path | None = None):
        self.argv = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-t", str(threads)]
        if beam_size > 1:
            self.argv += ["-bs", str(beam_size)]
        self.port, self.state, self.detail = port, "starting", ""
        self.state_path = Path(state_path) if state_path else None
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

    def _ours(self) -> tuple[int, list[str]] | None:
        """(pid, argv) of the whisper-server sizeup last started, if that process is still it."""
        try:
            d = json.loads(self.state_path.read_text()) if self.state_path else None
            pid, argv = int(d["pid"]), list(d["argv"])
            args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
        return (pid, argv) if "whisper-server" in args and f"--port {self.port}" in args else None

    def _replace_stale(self) -> None:
        ours = self._ours()
        if not ours or ours[1] == self.argv:
            return
        log(f"whisper-server: config changed ({' '.join(ours[1][2:3])} -> {' '.join(self.argv[2:3])}), restarting it")
        try:
            os.kill(ours[0], signal.SIGTERM)
        except OSError:
            return
        for _ in range(40):
            if not self.up():
                return
            time.sleep(0.5)

    def up(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                return True
        except OSError:
            return False

    def start(self) -> WhisperService:
        threading.Thread(target=self._run, name="whisper", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self) -> None:
        backoff = 2.0
        self._replace_stale()
        while not self._stop.is_set():
            if self.up():
                self.state, self.detail, backoff = "ok", "speech-to-text ready", 2.0
            elif self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.state, self.detail = "starting", "loading model"
                    if self.state_path:
                        try:
                            self.state_path.write_text(json.dumps({"pid": self._proc.pid, "argv": self.argv}))
                        except OSError:
                            pass
                except OSError as e:
                    self.state, self.detail = "down", f"cannot start whisper-server: {e}"
                    backoff = min(backoff * 2, 30)
            self._stop.wait(backoff)



CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
MIN_TURN_S = 0.35   # a key-up shorter than this is a blip: folded into its neighbour, not its own row


def split_turns(call: Call) -> list[tuple[str, float, float, str, np.ndarray]]:
    """(id, start, end, talker, audio) per talker turn; one entry (the call's own id) when one
    talker or no positions."""
    audio, n = call.audio, len(call.audio)
    cuts = [(min(n, int(pos * RATE)), name) for pos, name in call.turns]
    merged: list[list] = []
    for i, (a, name) in enumerate(cuts):
        b = cuts[i + 1][0] if i + 1 < len(cuts) else n
        if merged and (b - a < MIN_TURN_S * RATE or merged[-1][2] == name):
            merged[-1][1] = b                                    # blip or same talker: extend previous
        elif merged and merged[-1][1] - merged[-1][0] < MIN_TURN_S * RATE:
            merged[-1] = [merged[-1][0], b, name]                # previous was the blip: this talker owns it
        else:
            merged.append([a, b, name])
    if len(merged) < 2:
        return [(call.id, call.start, call.end, ", ".join(call.units), audio)]
    merged[0][0] = 0
    return [(f"{call.id}-t{k}", call.start + a / RATE, call.start + b / RATE, name, audio[a:b])
            for k, (a, b, name) in enumerate(merged)]


@dataclass
class Rerun:
    """A speech-to-text re-run of a recording already on screen (a transmission or a page)."""
    id: str
    kind: str                 # "traffic" | "page"
    fm: bool                  # FM audio still needs de-emphasis (False for P25 and for saved listening clips)
    low_energy: bool = False
    unit: str = ""            # talkers, for the speech-to-text hint (same as the live pass)
    start: float | None = None
    status: str = "pending"
    raw: str = ""
    confidence: float | None = None

class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plan = TonePlan(list(cfg.tones))
        self.whisper = WhisperService(cfg.stt_server, cfg.stt_model, cfg.stt_port, cfg.stt_threads, cfg.stt_beam_size,
                                      (Path(cfg.clips_dir).parent / "whisper-server.json") if cfg.clips_dir else None)
        self.stt = Transcriber(WhisperClient(f"http://127.0.0.1:{cfg.stt_port}/inference"), self._transcribed,
                               prompt=self._prompt)
        self.gaz = self._load_gazetteer()
        self.keep_s = cfg.keep_hours * 3600
        self._lock = threading.Lock()
        self.storage: dict | None = None
        self._rerun_ids: set[str] = set()
        self.rerun = {"total": 0, "done": 0, "changed": 0, "failed": 0}
        self.clips: OrderedDict[str, tuple[np.ndarray, float]] = OrderedDict()   # id -> (pcm, saved at)
        self.clips_dir = Path(cfg.clips_dir) if cfg.clips_dir else None
        if self.clips_dir:
            self.clips_dir.mkdir(parents=True, exist_ok=True)
        # Pages, incidents and traffic survive restarts (storage.keep_hours); old clips are pruned.
        self.store = IncidentStore(set(cfg.ours), geocode=self._geocode, keep_s=self.keep_s,
                                   path=(self.clips_dir.parent / "incidents.json") if self.clips_dir else None)
        self.pages: deque[Page] = deque(maxlen=1000)
        self._load_pages()
        self.tg = TalkgroupPrefs(cfg.p25_tg_info or {k: {"name": v} for k, v in cfg.p25_talkgroups.items()},
                                 set(cfg.p25_incident_talkgroups),
                                 (self.clips_dir.parent / "talkgroups.json") if self.clips_dir else None)
        for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]:
            if t.talkgroup is not None:
                self.tg.seed(t.talkgroup, t.end)
        self._last_tones_only: Page | None = None
        self.receivers = [NfmReceiver(cfg.rtl_fm, c.device, c.gain_db, Channel(c.mhz, c.label),
                                      self._segment, log, open_db=c.squelch_db) for c in cfg.channels]
        self.recorder = (TrunkRecorder(cfg.p25_recorder, cfg.p25_recorder_dir or ".", log)
                         if cfg.p25_recorder else None)
        # Switchable dongles ([radio] sdrs, by exact serial): each runs one mode at a time, picked in
        # the web app; a channel / Fire/EMS 15 runs on at most one of them.
        # sdrs = "auto": every RTL-SDR plugged in gets a row (new ones start Off: sizeup never grabs a
        # dongle by itself). A listed serial is always shown. sdr_ignore hides a dongle for good.
        self._listed: list[str] = [sd for sd in (list(cfg.sdrs) or ([cfg.shared_device] if cfg.shared_device else []))
                                   if sd not in cfg.sdr_ignore]
        self.shared = bool(self._listed) or cfg.sdr_auto
        self.modes = [c.label for c in cfg.channels] + ([P25_MODE] if self.recorder else [])
        if self.shared:
            self.modes.append(OFF_MODE)
        first = cfg.start_mode if cfg.start_mode in self.modes else (self.modes[0] if self.modes else OFF_MODE)
        self.assign: dict[str, str] = {sd: (first if k == 0 else OFF_MODE) for k, sd in enumerate(self._listed)}
        # What was picked in the web app wins over start_mode, so a restart doesn't move a dongle.
        self._mode_file = (Path(cfg.clips_dir).parent / "sdr_modes.json") if cfg.clips_dir else None
        self._load_modes()
        self._mode_lock = threading.Lock()
        self.switching = False
        self._kind = {c.label: c.kind for c in cfg.channels}
        self.p25 = (CallIngest(cfg.p25_dir, cfg.p25_talkgroups, self._call, {**cfg.unit_callsigns, **cfg.p25_aliases}, on_event=log,
                               name=P25_MODE)
                    if cfg.p25_dir else None)
        # PCFD's own dispatch console: incidents, units, run cards (state/cad.sqlite).
        self.cad = Cad.from_config(cfg, geocoder=self._geocode_text if self.gaz is not None else None, log=log)
        # Radio ID -> rig, and who is transmitting right now (both pages' "talking").
        self.radios = self.cad.radios
        self.live = LiveTalk(resolve=self.radios.resolve, tg_name=self.tg.name, main=self.tg.main,
                             learn=lambda src, alias, at: self.radios.heard("p25", src, alias, at))
        self.cad.live = self.live.state
        self.cad.has_clip = self.has_clip
        if self.recorder is not None:
            self.recorder.on_line = self.live.feed_line
        self._fm_live: dict[str, dict] = {}
        self._stopping = threading.Event()
        self._backfill()

    # -- geocoding ------------------------------------------------------------------------------

    def _load_gazetteer(self):
        try:
            from sizeup.geo.gazetteer import Gazetteer
            return Gazetteer()
        except Exception as e:  # missing data / module: incidents still open, just without a map point
            log(f"geocoder unavailable ({type(e).__name__}: {e}); incidents won't be placed on the map")
            return None

    def _geocode(self, d) -> dict | None:
        if self.gaz is None or not d.location:
            return None
        q = d.location + (f" cross of {d.cross_street}" if d.cross_street else "")
        try:
            m = self.gaz.geocode(q) or self.gaz.find_in_text(d.text)
        except Exception as e:
            log(f"geocode error for {q!r}: {type(e).__name__}: {e}")
            return None
        if m is None:
            return None
        return {"lat": m.lat, "lon": m.lon, "label": m.label, "kind": m.kind,
                "confidence": round(float(m.confidence), 2)}

    def _geocode_text(self, q: str) -> list[dict]:
        """The CAD location box: every reading of what was typed, best first (the CAD orders them by town)."""
        try:
            return [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                    | {"confidence": round(float(m.confidence), 2)} for m in self.gaz.candidates(q)[:20]]
        except Exception as e:
            log(f"geocode error for {q!r}: {type(e).__name__}: {e}")
            return []

    # -- persistence ----------------------------------------------------------------------------

    def _load_pages(self) -> None:
        if not self.clips_dir:
            return
        cutoff = time.time() - self.keep_s
        self._clean()
        loaded = []
        for f in self.clips_dir.glob("*.page.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if d.get("end", 0) >= cutoff:
                loaded.append(page_from_dict(d))
        for p in sorted(loaded, key=lambda p: p.start, reverse=True):
            self.pages.append(p)
        if loaded:
            log(f"reloaded {len(loaded)} page(s) from the last {self.cfg.keep_hours:g} h")

    def _clean(self) -> None:
        """Enforce the storage limits (sizeup/janitor.py) and drop expired history from memory."""
        if not self.clips_dir:
            return
        audio_s, history_s = self.cfg.audio_keep_hours * 3600, self.keep_s
        self.storage = janitor.clean(self.clips_dir.parent, self.clips_dir,
                                     Path(self.cfg.p25_dir) if self.cfg.p25_dir else None, audio_s, history_s)
        now = time.time()
        with self._lock:
            for cid in [c for c, (_, t) in self.clips.items() if now - t > audio_s]:
                del self.clips[cid]
            while self.pages and self.pages[-1].end < now - history_s:
                self.pages.pop()
        if hasattr(self, "store"):
            self.store.prune(now - history_s)
        if self.storage.get("deleted"):
            log(f"storage: removed {self.storage['deleted']} expired file(s); "
                f"{self.storage['bytes'] / 1e6:.1f} MB in captures/")

    def _janitor(self) -> None:
        while True:
            time.sleep(600)
            try:
                self._clean()
            except Exception as e:  # cleanup must never take the display down
                log(f"storage cleanup error: {type(e).__name__}: {e}")

    def _save_page(self, page: Page) -> None:
        if not self.clips_dir:
            return
        try:
            d = dict(page.as_dict(), dispatch=getattr(page, "dispatch", None),
                     history=getattr(page, "history", None) or [], rerun_at=getattr(page, "rerun_at", None),
                     model=getattr(page, "model", ""))
            (self.clips_dir / f"{page.id}.page.json").write_text(json.dumps(d))
        except OSError:
            pass

    # -- clips ----------------------------------------------------------------------------------

    def _keep_clip(self, cid: str, audio: np.ndarray, processed: bool = False) -> None:
        # Stored as heard through a radio speaker (de-emphasis, voice band, steady loudness),
        # not the raw discriminator output, which is quiet and dominated by CTCSS + hiss.
        pcm = (np.clip(audio if processed else radio_voice(audio), -1, 1) * 32767).astype(np.int16)
        with self._lock:
            self.clips[cid] = (pcm, time.time())
            while len(self.clips) > 400:
                self.clips.popitem(last=False)
        if self.clips_dir:
            try:
                (self.clips_dir / f"{cid}.wav").write_bytes(wav_bytes(pcm, RATE))
            except OSError:
                pass

    def _prompt(self, item) -> str:
        """whisper's prompt for one recording: the configured text, plus (stt.talker_prompt) the
        talkers on this call as they're said on the air."""
        parts = [self.cfg.stt_prompt]
        if self.cfg.stt_talker_prompt != "off":
            parts.append(talker_prompt(getattr(item, "unit", "") or "", getattr(item, "start", None),
                                       with_time=self.cfg.stt_talker_prompt == "ids+time"))
        return " ".join(p for p in parts if p)

    def _keep_source(self, cid: str, audio: np.ndarray | None) -> None:
        """The exact audio speech-to-text was given (before the listening filter), kept with the
        clip for the same time so a re-run sees what the live pass saw."""
        if not self.clips_dir or audio is None or not len(audio):
            return
        try:
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            (self.clips_dir / f"{cid}.src.wav").write_bytes(wav_bytes(pcm, RATE))
        except OSError:
            pass

    def clip(self, cid: str) -> bytes | None:
        # P25 ids carry trunk-recorder's timestamp ("307-1790954731.970_476112500.0-call_11-t2"):
        # dots are allowed, but nothing that could leave the clips folder.
        if not CLIP_ID.fullmatch(cid) or ".." in cid:
            return None
        with self._lock:
            hit = self.clips.get(cid)
        if hit is not None:
            return wav_bytes(hit[0], RATE)
        p = self.clips_dir / f"{cid}.wav" if self.clips_dir else None
        return p.read_bytes() if p and p.is_file() else None

    # -- radio events -----------------------------------------------------------------------------

    def _mdc(self, *audios) -> list[tuple[str, bool]]:
        """(MDC-1200 unit ID, emergency) for each radio ID found in these pieces of audio, in order."""
        out: list[tuple[str, bool]] = []
        for b in audios:
            try:
                pkts = mdc_decode(b, RATE)
            except Exception:
                continue
            for p in pkts:
                if p.kind in ("pre-id", "post-id", "emergency") and (p.unit_hex, p.kind == "emergency") not in out:
                    out.append((p.unit_hex, p.kind == "emergency"))
        return out

    def _units(self, seg: Segment, found: list[tuple[str, bool]] | None = None) -> list[str]:
        """Who keyed up: MDC-1200 PTT-IDs in the bursts cut from the transmission's ends, and anywhere
        in the transmission itself (a burst the edge cut missed still names the radio; packets are
        CRC-checked, so this can't invent an ID)."""
        out: list[str] = []
        for hexid, emergency in (self._mdc(*seg.bursts, seg.audio) if found is None else found):
            name = self.cfg.mdc_aliases.get(hexid, hexid)
            name = f"{name} EMERGENCY" if emergency else name
            if name not in out:
                out.append(name)
        return out

    def _id_note(self, label: str, seg: Segment, units: list[str]) -> str:
        """Why no radio ID, when there isn't one: a burst we couldn't read vs. no burst at all."""
        if units:
            return ""
        F = RATE // 20
        looks_like_data = any(data_frames([seg.audio[i:i + F] for i in range(0, len(seg.audio) - F + 1, F)]))
        if not seg.bursts and not looks_like_data:
            return "no ID burst"
        bad = 0
        for b in [*seg.bursts, seg.audio]:
            try:
                bad += sum(not p.crc_ok for p in mdc_decode(b, RATE, only_valid=False))
            except Exception:
                pass
        log(f"{label}: MDC ID burst heard but not decoded ({len(seg.bursts)} cut burst(s), "
            + (f"{bad} sync hit(s) failed the CRC" if bad else "no sync found") + ")")
        return "ID burst unreadable"

    def _keep_bursts(self, cid: str, seg: Segment) -> None:
        """The cut-off MDC bursts, next to the clip (same 12 h limit), so the ID can be decoded again."""
        if not self.clips_dir or not seg.bursts:
            return
        gap = np.zeros(RATE // 20, dtype=np.float32)
        audio = np.concatenate([x for b in seg.bursts for x in (np.asarray(b, np.float32), gap)])
        try:
            (self.clips_dir / f"{cid}.mdc.wav").write_bytes(wav_bytes((np.clip(audio, -1, 1) * 32767).astype(np.int16), RATE))
        except OSError:
            pass

    def _segment(self, ch: Channel, seg: Segment) -> None:
        paging = self._kind.get(ch.label) == "paging"
        page = assemble(ch.label, ch.mhz, seg, self.plan) if paging else None
        found = self._mdc(*seg.bursts, seg.audio)
        units = self._units(seg, found)
        id_note = "" if paging else self._id_note(ch.label, seg, units)
        # Weak signals: speech can sit only 2-6 dB over the dead air (FG1 bench, indoor antenna),
        # so nothing is skipped on energy. Everything is transcribed; a result that is one of
        # whisper's dead-air phrases ("Thank you.", "Yes.") on audio with no voice energy is a
        # mic click (stt.Transcriber), dropped in _transcribed.
        low_energy = speech_seconds(seg.audio) < MIN_SPEECH_S
        if paging:
            self._keep_clip(page.id, seg.audio)
            with self._lock:
                self.pages.appendleft(page)
            names = page.departments
            log(f"{ch.label}: {page.kind} " + (", ".join(t.describe() for t in page.tones) or "voice"))
            if page.status == "no_voice":
                self._save_page(page)
                if any(n in self.cfg.ours for n in names):
                    self._last_tones_only = page
                if page.tones and not names:
                    self._unknown_tones(page)
                return
            self._keep_source(page.id, page.voice)
            self.stt.submit(page, page.voice)
        else:
            ids = list(dict.fromkeys(h for h, _e in found))
            for h in ids:
                self.radios.heard("mdc", h, at=seg.end)
            self.live.fm_segment(ch.label, ids, seg.start, seg.end)
            t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end,
                        "", status="pending", unit=", ".join(units), rig=self.radios.rig_mdc(ids), radio_id=", ".join(ids))
            t.low_energy = low_energy  # type: ignore[attr-defined]
            if id_note:
                t.id_note = id_note    # type: ignore[attr-defined]
            self._keep_bursts(t.id, seg)
            t.raw_audio = seg.audio    # type: ignore[attr-defined]  (saved if it turns out to be a click)
            self._keep_clip(t.id, seg.audio)
            self._keep_source(t.id, seg.audio)
            self.stt.submit(t, seg.audio)

    def _unknown_tones(self, page: Page) -> None:
        log("  unknown tone pair(s) kept for learning: "
            + ", ".join(f"{t.a_hz:.1f}/{(t.b_hz or 0):.1f} Hz" for t in page.tones))

    def _call(self, call: Call) -> None:
        if not self.tg.record(call.talkgroup):  # switched off in the web app: dropped on arrival
            self.tg.seen(call.talkgroup, dropped=True)
            self._drop_recorder_files(call)
            return
        self.tg.seen(call.talkgroup)
        for _name, (src, tag) in call.ids.items():      # radio ID <-> alias, kept for lookups without the alias
            self.radios.heard("p25", src, tag, call.start)
        if call.audio is not None:
            # One row per talker: 60 Control and the unit it's talking to are separate key-ups in the
            # same call; each piece is transcribed with only its own talker as the hint.
            for cid, start, end, unit, audio in split_turns(call):
                names = [n for n in unit.split(", ") if n]
                pairs = [(call.ids.get(n, ("", ""))[0], call.ids.get(n, ("", ""))[1] or n) for n in names]
                t = Traffic(cid, call.label, start, end, "", status="pending", unit=unit, talkgroup=call.talkgroup,
                            rig=self.radios.rig_p25(pairs), radio_id=", ".join(src for src, _t in pairs if src))
                t.fm = False  # decoded P25 voice: no FM de-emphasis  # type: ignore[attr-defined]
                self._keep_clip(t.id, radio_voice(audio, deemph=False), processed=True)
                self._keep_source(t.id, audio)
                self.stt.submit(t, audio)
        self._drop_recorder_files(call)  # sizeup's clip is the one copy kept

    def _drop_recorder_files(self, call: Call) -> None:
        if not self.clips_dir or not call.wav_path:  # without our own clip store, keep the originals
            return
        wav = Path(call.wav_path)
        for f in (wav, wav.with_suffix(".json")):
            try:
                f.unlink()
            except OSError:
                pass

    def _transcribed(self, item) -> None:
        if isinstance(item, Traffic):
            item.text = getattr(item, "raw", "")
            attach = item.talkgroup is None or self.tg.main(item.talkgroup)
            if item.status == "no_speech":
                # A key-up with nothing said (mic click): still shown, as who keyed and when, with
                # its clip; just no transcript.
                item.text = ""
                log(f"{item.source}: key-up ({item.end - item.start:.1f} s, no speech)"
                    + (f" · unit {item.unit}" if item.unit else ""))
            if hasattr(item, "raw_audio"):
                del item.raw_audio
            item.model = Path(self.cfg.stt_model).name  # type: ignore[attr-defined]  (which model wrote this text)
            self.store.on_traffic(item, attach=attach)
            self._to_cad(item)
            if item.status != "no_speech":
                log(f"{item.source}: {'[' + item.unit + '] ' if item.unit else ''}{item.text or '(' + item.status + ')'}")
            return
        if isinstance(item, Rerun):
            return self._rerun_done(item)
        page: Page = item
        page.model = Path(self.cfg.stt_model).name  # type: ignore[attr-defined]
        d = self._dispatch(page.raw)
        page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
        self._save_page(page)
        log(f"{page.label} {page.kind}: {page.raw or '(' + page.status + ')'}")
        if d is None:
            return
        depts = page.departments
        if page.kind == "voice":
            prev = self._last_tones_only
            if prev and page.start - prev.end < SPLIT_PAGE_S:
                depts = prev.departments  # the voice half of a tones-only page
                self._last_tones_only = None
            elif not any(x in self.cfg.ours for x in d.departments):
                t = Traffic(page.id, page.label, page.start, page.end, page.raw)
                self.store.on_traffic(t)
                self._to_cad(t)
                return
        inc = self.store.on_page(page.id, depts, d, at=page.start)
        if inc:
            where = inc.geo["label"] if inc.geo else (inc.location or "location not found")
            log(f"INCIDENT {inc.incident_type} · {where} · {', '.join(inc.departments)}")
            try:
                self.cad.from_monitor(inc)
            except Exception as e:  # the CAD must never stop the monitor
                log(f"CAD: could not open a pending incident: {type(e).__name__}: {e}")

    def _to_cad(self, t: Traffic, replace: bool = True) -> None:
        """A transmission into the CAD's communications log (linked to its rig's events)."""
        try:
            self.cad.record_traffic({"id": t.id, "start": t.start, "end": t.end, "source": t.source,
                                     "talkgroup": t.talkgroup, "unit": t.unit, "rig": t.rig, "radio_id": t.radio_id,
                                     "text": t.text, "status": t.status, "model": getattr(t, "model", "")},
                                    replace=replace)
        except Exception as e:  # the CAD must never stop the monitor
            log(f"CAD: could not log a transmission: {type(e).__name__}: {e}")

    def _backfill(self) -> None:
        """Traffic saved before radio IDs were tied to rigs: the rig from its talker text, and every
        transmission still in the history into the communications log (ones already there are left)."""
        rows = list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
        n = 0
        for t in rows:
            if not t.rig and t.unit:
                rig, ids = self.radios.rig_for_names(t.unit, t.talkgroup is None, self.cfg.mdc_aliases)
                t.rig = rig
                t.radio_id = t.radio_id or ids
            if t.status != "pending":
                before = self.cad.comms.rev
                self._to_cad(t, replace=False)
                n += self.cad.comms.rev != before
        if n:
            log(f"CAD: {n} earlier transmission(s) added to the communications log")

    # -- who is talking now (FG1) ------------------------------------------------------------------

    def _watch_fm(self) -> None:
        """While a traffic channel's squelch is open it is "talking"; the MDC ID is tried on the
        audio so far (an APX sends it 0.3-0.8 s in) so the rig shows before the transmission ends."""
        while not self._stopping.wait(LIVE_WATCH_S):
            try:
                self._watch_fm_once()
            except Exception as e:  # never let the indicator take anything down
                log(f"talking watcher error: {type(e).__name__}: {e}")

    def _watch_fm_once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for r in list(self.receivers):
            label = r.channel.label
            if self._kind.get(label) != "traffic":
                continue
            sq = r.squelch
            on = sq.open and (not self.shared or label in self.assign.values())
            st = self._fm_live.get(label)
            if not on:
                if st is not None:
                    del self._fm_live[label]
                    self.live.fm_close(label)
                continue
            since = sq._start
            if st is None or st["since"] != since:
                st = self._fm_live[label] = {"since": since, "ids": [], "n": 0}
                self.live.fm_open(label, since)
            frames = list(sq._frames)[: int(LIVE_ID_WINDOW_S * RATE / FRAME)]
            if not st["ids"] and len(frames) >= 4 and len(frames) != st["n"] and now - since < LIVE_ID_WINDOW_S + 1:
                st["n"] = len(frames)
                ids = list(dict.fromkeys(h for h, _e in self._mdc(np.concatenate(frames).astype(np.float32))))
                if ids:
                    st["ids"] = ids
                    self.live.fm_ids(label, ids)

    def _dispatch(self, raw: str):
        d = parse(raw) if raw else None
        if d is not None:  # units dispatched name their department ("Engine 61" -> Port Chester FD)
            for n in d.numbers:  # "174, 175, Ladder 1": engines read without the word, if the roster has them
                u = f"Engine {n}"
                if u in self.cfg.unit_departments and u not in d.units:
                    d.units.append(u)
            for cs, name in self.cfg.unit_callsigns.items():  # chiefs by callsign: "2398 respond to ..."
                if re.search(rf"\b{cs}\b", raw) and name not in d.units:
                    d.units.append(name)
            for u in d.units:
                dep = self.cfg.unit_departments.get(u)
                if dep and dep not in d.departments:
                    d.departments.append(dep)
        return d

    # -- speech-to-text re-runs ----------------------------------------------------------------------

    def _read_clip(self, name: str) -> np.ndarray | None:
        p = self.clips_dir / name if self.clips_dir else None
        if not p or not p.is_file():
            return None
        try:
            with wave.open(str(p)) as w:
                if w.getframerate() != RATE or w.getsampwidth() != 2:
                    return None
                return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
        except (OSError, EOFError, wave.Error):
            return None

    def _rerun_audio(self, cid: str, fm: bool, voice_offset_s: float = 0.0) -> tuple[np.ndarray, bool] | None:
        """(audio, needs FM de-emphasis). The saved STT input when there is one; otherwise the saved
        listening clip (already de-emphasised and filtered; pages: tones cut off by voice_offset_s)."""
        src = self._read_clip(f"{cid}.src.wav")
        if src is not None:
            return src, fm
        with self._lock:
            hit = self.clips.get(cid)
        audio = hit[0].astype(np.float32) / 32768.0 if hit is not None else self._read_clip(f"{cid}.wav")
        if audio is None:
            return None
        return audio[int(voice_offset_s * RATE):], False

    def retranscribe(self, ids: list[str] | None = None) -> int:
        """Run speech-to-text again on recordings still on disk (ids=None: all of them). Queued
        behind live traffic; results replace the text and keep the old one in history."""
        want = set(ids) if ids is not None else None
        jobs: list[tuple[float, Rerun, np.ndarray]] = []
        seen: set[str] = set()
        with self._lock:
            pages = [p for p in self.pages if p.status != "no_voice"]
            busy = set(self._rerun_ids)
        for p in pages:
            if (want is None or p.id in want) and p.id not in busy and p.id not in seen:
                got = self._rerun_audio(p.id, True, p.voice_offset_s)
                if got:
                    jobs.append((p.start, Rerun(p.id, "page", got[1], start=p.start), got[0]))
                    seen.add(p.id)
        traffic = list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
        for t in traffic:
            if (want is None or t.id in want) and t.id not in busy and t.id not in seen:
                got = self._rerun_audio(t.id, getattr(t, "fm", True))
                if got:
                    low = speech_seconds(got[0]) < MIN_SPEECH_S  # same mic-click rule as the live pass
                    jobs.append((t.start, Rerun(t.id, "traffic", got[1], low, t.unit, t.start), got[0]))
                    seen.add(t.id)
        jobs.sort(key=lambda j: -j[0])  # newest first: what's on screen updates first
        with self._lock:
            if not self._rerun_ids:  # a new batch: restart the progress count
                self.rerun = {"total": 0, "done": 0, "changed": 0, "failed": 0}
            n = 0
            for _start, job, audio in jobs:
                if self.stt.submit(job, audio, priority=RERUN):
                    self._rerun_ids.add(job.id)
                    n += 1
            self.rerun["total"] += n
        if n:
            log(f"speech-to-text re-run: {n} recording(s) queued")
        return n

    def _rerun_done(self, job: Rerun) -> None:
        model = Path(self.cfg.stt_model).name
        text = job.raw if job.status == "done" else ""
        changed = False
        if job.status != "error":
            with self._lock:
                page = next((p for p in self.pages if p.id == job.id), None)
            if page is not None:
                if page.raw != text:
                    hist = list(getattr(page, "history", None) or [])
                    hist.append({"text": page.raw, "at": time.time(), "model": getattr(page, "model", "") or ""})
                    page.history = hist[-5:]  # type: ignore[attr-defined]
                    changed = True
                page.raw, page.confidence = text, job.confidence
                page.rerun_at, page.model = time.time(), model  # type: ignore[attr-defined]
                page.status = "done" if text else "no_speech"
                d = self._dispatch(text)
                page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
                self._save_page(page)
            old = next((t.text for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
                        if t.id == job.id), None)
            changed = changed or (old is not None and old != text)
            self.store.retext(job.id, text, job.status, job.confidence, model)
            try:
                self.cad.comms.retext(job.id, text, job.status, model)
            except Exception as e:  # the CAD must never stop the monitor
                log(f"CAD: could not update the communications log: {type(e).__name__}: {e}")
            if changed:
                log(f"re-run {job.id}: {text or '(no speech)'}")
        with self._lock:
            self._rerun_ids.discard(job.id)
            self.rerun["done"] += 1
            self.rerun["changed"] += changed
            self.rerun["failed"] += job.status == "error"

    # -- state --------------------------------------------------------------------------------------

    # -- switchable dongles ------------------------------------------------------------------------

    @property
    def sdrs(self) -> list[str]:
        """The dongles shown now: listed ones, every one plugged in (auto), and any unplugged one
        still assigned a mode (so you can see what stopped). Plug order; listed first."""
        out = list(self._listed)
        here = [s for _i, _p, s in sdr_lookup.devices(self.cfg.rtl_fm)] if self.cfg.sdr_auto else []
        for sd in here + [sd for sd, m in self.assign.items() if m != OFF_MODE]:
            if sd and sd not in out and sd not in self.cfg.sdr_ignore:
                out.append(sd)
        return out

    @property
    def mode(self) -> str:
        """The first dongle's mode (one-dongle setups)."""
        rows = self.sdrs
        return self.assign.get(rows[0], OFF_MODE) if rows else OFF_MODE

    def _load_modes(self) -> None:
        saved: dict = {}
        try:
            if self._mode_file and self._mode_file.is_file():
                saved = json.loads(self._mode_file.read_text())
            elif self._mode_file and self._listed and self._mode_file.with_name("sdr_mode.txt").is_file():
                saved = {self._listed[0]: self._mode_file.with_name("sdr_mode.txt").read_text().strip()}  # one-dongle era
        except (OSError, ValueError):
            saved = {}
        taken: set[str] = set()
        for sd in list(dict.fromkeys([*self._listed, *saved])):
            if sd in self.cfg.sdr_ignore:
                continue
            m = saved.get(sd, self.assign.get(sd, OFF_MODE))
            m = m if m in self.modes and (m == OFF_MODE or m not in taken) else OFF_MODE
            self.assign[sd] = m
            taken.add(m)

    def sdr_name(self, serial: str) -> str:
        return self.cfg.sdr_names.get(serial) or serial

    def _save_modes(self) -> None:
        if self._mode_file:
            try:
                self._mode_file.write_text(json.dumps(self.assign))
            except OSError:
                pass

    def _dongle(self, serial: str):
        def find() -> str | None:
            i = sdr_lookup.resolve(serial, self.cfg.rtl_fm)
            return None if i is None else str(i)
        find.serial = serial  # type: ignore[attr-defined]
        find.label = self.sdr_name(serial)  # type: ignore[attr-defined]
        return find

    def _receiver(self, label: str) -> NfmReceiver | None:
        return next((r for r in self.receivers if r.channel.label == label), None)

    def _fresh_receiver(self, old: NfmReceiver, serial: str) -> NfmReceiver:
        c = next(c for c in self.cfg.channels if c.label == old.channel.label)
        r = NfmReceiver(self.cfg.rtl_fm, self._dongle(serial), c.gain_db, Channel(c.mhz, c.label),
                        self._segment, log, open_db=c.squelch_db)
        self.receivers[self.receivers.index(old)] = r
        return r

    def _start_mode(self, mode: str, serial: str) -> bool:
        if mode == OFF_MODE:
            log(f"SDR {self.sdr_name(serial)} released: nothing of ours on it (switch it back on in the web app)")
            return True
        if not claims.claim(serial, mode):   # another app (shortfinal) holds it: leave it alone
            c = claims.other(serial) or {}
            log(f"SDR {self.sdr_name(serial)}: in use by {c.get('app', 'another app')} ({c.get('mode', '?')}), not starting {mode}")
            return False
        if mode == P25_MODE:
            self.recorder.device_fn, self.recorder.device_name = self._dongle(serial), self.sdr_name(serial)
            self.recorder.start()
        else:
            r = self._receiver(mode)
            if r is not None:
                self._fresh_receiver(r, serial).start()
        return True

    def _stop_mode(self, mode: str, why: str, serial: str) -> None:
        if mode == P25_MODE:
            self.recorder.stop(why)
        elif mode != OFF_MODE:
            r = self._receiver(mode)
            if r is not None:
                r.stop()
        claims.release(serial)

    def set_mode(self, mode: str, sdr: str | None = None) -> str | None:
        """Give dongle `sdr` (default: the first) a channel, Fire/EMS 15 (trunk-recorder) or Off.
        A mode already running on another dongle moves here (that dongle goes Off)."""
        if not self.shared:
            return "each channel has its own dongle: nothing to switch"
        rows = self.sdrs
        sdr = sdr or (rows[0] if rows else "")
        if sdr not in rows:
            return f"no SDR {sdr!r} plugged in" + (f"; one of {', '.join(rows)}" if rows else "")
        if mode not in self.modes:
            return f"unknown mode {mode!r}; one of {', '.join(self.modes)}"
        held = claims.other(sdr) if mode != OFF_MODE else None
        if held:
            return f"SDR {self.sdr_name(sdr)} is in use by {held.get('app', 'another app')} ({held.get('mode', '?')})"
        with self._mode_lock:
            if self.assign.get(sdr, OFF_MODE) == mode:
                return None
            self.switching = True
            try:
                other = next((o for o, m in self.assign.items() if o != sdr and m == mode != OFF_MODE), None)
                if other:
                    log(f"SDR {self.sdr_name(other)}: {mode} -> {OFF_MODE} (moving to {self.sdr_name(sdr)})")
                    self._stop_mode(mode, f"moved to SDR {self.sdr_name(sdr)}", other)
                    self.assign[other] = OFF_MODE
                log(f"SDR {self.sdr_name(sdr)}: {self.assign.get(sdr, OFF_MODE)} -> {mode}")
                self._stop_mode(self.assign.get(sdr, OFF_MODE), f"SDR {self.sdr_name(sdr)} on {mode}", sdr)
                time.sleep(SWITCH_PAUSE_S)  # let the dongle go before the next program opens it
                ok = self._start_mode(mode, sdr)
                self.assign[sdr] = mode if ok else OFF_MODE
                self._save_modes()
                if not ok:
                    return f"SDR {self.sdr_name(sdr)} was just taken by another app"
            finally:
                self.switching = False
        return None

    def set_talkgroup(self, tg: int, record: bool | None = None, main: bool | None = None) -> str | None:
        """POST /api/talkgroup: record = keep its calls; main = main list + attach to our incident."""
        if not self.p25:
            return "no P25 configured"
        self.tg.set(int(tg), record, main)
        log(f"talkgroup {tg} ({self.tg.name(int(tg))}): record={self.tg.record(int(tg))} main={self.tg.main(int(tg))}")
        return None

    def _off_detail(self) -> str:
        many = len(self.sdrs) > 1
        on = [f"SDR {self.sdr_name(sd)} on {m}" if many else f"SDR on {m}" for sd, m in self.assign.items() if m != OFF_MODE]
        return "; ".join(on) if on else ("SDRs released (Off)" if many else "SDR released (Off)")

    def set_talkgroup_group(self, group: str, record: bool | None = None, main: bool | None = None) -> str | None:
        """POST /api/talkgroup-group: a whole section on/off in one go."""
        if not self.p25:
            return "no P25 configured"
        n = self.tg.set_group(str(group), record, main)
        if not n:
            return f"no talkgroups in group {group!r}"
        log(f"talkgroup group {group} ({n}): record={record} main={main}")
        return None

    def sdr(self) -> dict | None:
        if not self.shared:
            return None
        plugged = {s: p for _i, p, s in sdr_lookup.devices(self.cfg.rtl_fm)}
        rows = self.sdrs
        return {"shared": True, "mode": self.assign.get(rows[0], OFF_MODE) if rows else OFF_MODE, "modes": self.modes,
                "switching": self.switching,
                "sdrs": [{"serial": sd, "name": self.sdr_name(sd), "product": plugged.get(sd, ""),
                          "mode": self.assign.get(sd, OFF_MODE), "present": sd in plugged,
                          "held_by": ({"app": c.get("app"), "mode": c.get("mode")} if (c := claims.other(sd)) else None)}
                         for sd in rows]}

    def radio_status(self) -> list[dict]:
        out = []
        for r in self.receivers:
            st = r.status()
            if self.shared and r.channel.label not in self.assign.values():
                st.update(state="off", detail=self._off_detail(), open=False, quieting_db=None)
            out.append(st)
        if self.p25:
            st = self.p25.status()
            if self.recorder is not None:
                rs = self.recorder.status()
                if self.shared and P25_MODE not in self.assign.values():
                    st.update(state="off", detail=self._off_detail())
                elif rs["state"] != "ok":
                    st.update(state=rs["state"], detail=rs["detail"])
                else:
                    st["detail"] = rs["detail"] + (" · " + st["detail"] if st.get("detail") else "")
            out.append(st)
        out.append({"label": "STT", "mhz": None, "state": self.whisper.state, "detail": self.whisper.detail,
                    "open": False, "quieting_db": None, "restarts": 0, "last_heard": None})
        return out

    def has_clip(self, cid: str) -> bool:
        with self._lock:
            if cid in self.clips:
                return True
        return bool(self.clips_dir and (self.clips_dir / f"{cid}.wav").is_file())

    def state(self) -> dict:
        with self._lock:
            pages = [dict(p.as_dict(), dispatch=getattr(p, "dispatch", None), history=getattr(p, "history", None) or [],
                          rerun_at=getattr(p, "rerun_at", None))
                     for p in list(self.pages)[:50]]
        st = {"time": time.time(), "radios": self.radio_status(), **self.store.state(), "pages": pages, "sdr": self.sdr(),
              "storage": getattr(self, "storage", None),
              "rerun": dict(self.rerun, pending=len(self._rerun_ids)),
              "talkgroups": self.tg.state() if self.p25 else [],
              "talking": self.live.state()}
        # Tell the display which entries have a recording (so ▶ isn't a dead button).
        with self._lock:
            busy = set(self._rerun_ids)
        for p in st["pages"]:
            p["has_audio"] = self.has_clip(p["id"])
            p["rerun_pending"] = p["id"] in busy
        for t in st["loose_traffic"]:
            t["has_audio"] = self.has_clip(t["id"])
            t["rerun_pending"] = t["id"] in busy
        for inc in st["incidents"]:
            for t in inc["traffic"]:
                t["has_audio"] = self.has_clip(t["id"])
                t["rerun_pending"] = t["id"] in busy
            for e in inc["page_entries"]:
                e["has_audio"] = self.has_clip(e["id"])
        return st

    def scene(self) -> dict:
        fh = json.loads((ROOT / "data" / "firehouses.json").read_text())["firehouses"]
        lon, lat, zoom = self.cfg.home
        return {"station": {"id": self.cfg.station_id, "name": self.cfg.station_name},
                "timezone": "America/New_York",
                "home": {"center": [lon, lat], "zoom": zoom}, "incident_zoom": 17.2,
                "tiles": "https://tiles.openfreemap.org/planet",
                "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
                "firehouses": fh, "categories": CATEGORIES}

    def start(self) -> App:
        self.whisper.start()
        self.stt.start()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()
        threading.Thread(target=self._watch_fm, name="talking", daemon=True).start()
        if self.shared:
            for sd, m in list(self.assign.items()):  # each dongle runs what was picked for it
                if not self._start_mode(m, sd):         # held by shortfinal now: don't fight for it
                    self.assign[sd] = OFF_MODE
            self._save_modes()
        else:
            for r in self.receivers:
                r.start()
            if self.recorder:
                self.recorder.start()
        if self.p25:
            self.p25.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        for r in self.receivers:
            r.stop()
        if self.recorder:
            self.recorder.stop()
        if self.p25:
            self.p25.stop()
        for sd in list(self.assign):
            claims.release(sd)
        self.whisper.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sizeup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except (ConfigError, FileNotFoundError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    app = App(cfg).start()
    server = WebServer(cfg.host, cfg.port, app.scene(), app.state, app.clip, app.store.close, app.set_mode,
                       app.set_talkgroup, app.set_talkgroup_group, app.retranscribe, cad=CadApi(app.cad))
    app.live.on_change = server.poke          # a new talker is pushed at once, not on the next second
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    log(f"sizeup: {cfg.station_name} · {len(cfg.channels)} channel(s)"
        + (f" · P25 {cfg.p25_dir}" if cfg.p25_dir else "") + f" · {server.url} · CAD {server.url}cad"
        + f" ({cfg.cad_db or 'in memory'}; transmit off)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0
