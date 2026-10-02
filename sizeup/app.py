"""sizeup service: receivers -> transcription -> incidents -> web display.

    python -m sizeup --config config.toml        # open http://localhost:8090/

    paging channel (South 2) --squelch--> transmission --tones?--> Page
        page for one of our departments --STT--> dispatch parse --> incident (geocoded)
        voice-only transmission --STT--> dispatcher traffic (attached to the active incident),
            or the voice half of a tones-only page sent as a separate transmission
    traffic channels (FG1)    --squelch--> transmission --STT--> traffic on the active incident
    P25 (trunk-recorder dir)  --call--> STT --> traffic on the active incident (unit IDs kept)
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
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic
from sizeup.radio.fm import RATE, Channel, NfmReceiver, Segment, speech_seconds
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio.p25 import Call, CallIngest
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
        # One dongle for everything (bench): only the selected mode runs; switch in the web app.
        self.shared = bool(cfg.shared_device)
        self.modes = [c.label for c in cfg.channels] + ([P25_MODE] if self.recorder else [])
        if self.shared:
            self.modes.append(OFF_MODE)
        self.mode = (cfg.start_mode if cfg.start_mode in self.modes else (self.modes[0] if self.modes else ""))
        # The last mode picked in the web app wins over start_mode, so a restart doesn't move the dongle.
        self._mode_file = (Path(cfg.clips_dir).parent / "sdr_mode.txt") if cfg.clips_dir else None
        if self.shared and self._mode_file and self._mode_file.is_file():
            saved = self._mode_file.read_text().strip()
            if saved in self.modes:
                self.mode = saved
        self._mode_lock = threading.Lock()
        self.switching = False
        self._kind = {c.label: c.kind for c in cfg.channels}
        self.p25 = (CallIngest(cfg.p25_dir, cfg.p25_talkgroups, self._call, {**cfg.unit_callsigns, **cfg.p25_aliases}, on_event=log,
                               name=P25_MODE)
                    if cfg.p25_dir else None)

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
                     history=getattr(page, "history", None) or [])
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

    def _units(self, seg: Segment) -> list[str]:
        """Who keyed up: MDC-1200 PTT-IDs in the bursts cut from the transmission's ends."""
        out: list[str] = []
        for b in seg.bursts:
            try:
                pkts = mdc_decode(b, RATE)
            except Exception:
                continue
            for p in pkts:
                if p.kind in ("pre-id", "post-id", "emergency"):
                    name = self.cfg.mdc_aliases.get(p.unit_hex, p.unit_hex)
                    name = f"{name} EMERGENCY" if p.kind == "emergency" else name
                    if name not in out:
                        out.append(name)
        return out

    def _segment(self, ch: Channel, seg: Segment) -> None:
        paging = self._kind.get(ch.label) == "paging"
        page = assemble(ch.label, ch.mhz, seg, self.plan) if paging else None
        units = self._units(seg)
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
            t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end,
                        "", status="pending", unit=", ".join(units))
            t.low_energy = low_energy  # type: ignore[attr-defined]
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
        if call.audio is not None:
            # One row per talker: 60 Control and the unit it's talking to are separate key-ups in the
            # same call; each piece is transcribed with only its own talker as the hint.
            for cid, start, end, unit, audio in split_turns(call):
                t = Traffic(cid, call.label, start, end, "", status="pending", unit=unit, talkgroup=call.talkgroup)
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

    def _save_skipped(self, item) -> None:
        audio = getattr(item, "raw_audio", None)
        if self.clips_dir is None or audio is None:
            return
        try:  # kept (not shown) so the click/speech decision can be checked
            d = self.clips_dir / "skipped"
            d.mkdir(exist_ok=True)
            (d / f"{item.id}.wav").write_bytes(wav_bytes((np.clip(audio, -1, 1) * 32767).astype(np.int16), RATE))
        except OSError:
            pass

    def _transcribed(self, item) -> None:
        if isinstance(item, Traffic):
            item.text = getattr(item, "raw", "")
            attach = item.talkgroup is None or self.tg.main(item.talkgroup)
            if item.status == "no_speech":
                log(f"{item.source}: mic click ({item.end - item.start:.1f} s, no speech) skipped"
                    + (f" · unit {item.unit}" if item.unit else ""))
                self._save_skipped(item)
                with self._lock:
                    self.clips.pop(item.id, None)
                return
            if hasattr(item, "raw_audio"):
                del item.raw_audio
            self.store.on_traffic(item, attach=attach)
            log(f"{item.source}: {'[' + item.unit + '] ' if item.unit else ''}{item.text or '(' + item.status + ')'}")
            return
        if isinstance(item, Rerun):
            return self._rerun_done(item)
        page: Page = item
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
                self.store.on_traffic(Traffic(page.id, page.label, page.start, page.end, page.raw))
                return
        inc = self.store.on_page(page.id, depts, d, at=page.start)
        if inc:
            where = inc.geo["label"] if inc.geo else (inc.location or "location not found")
            log(f"INCIDENT {inc.incident_type} · {where} · {', '.join(inc.departments)}")

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
                    hist.append({"text": page.raw, "at": time.time(), "model": model})
                    page.history = hist[-5:]  # type: ignore[attr-defined]
                    changed = True
                page.raw, page.confidence = text, job.confidence
                page.status = "done" if text else "no_speech"
                d = self._dispatch(text)
                page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
                self._save_page(page)
            old = next((t.text for t in list(self.store.loose) + [t for i in self.store.incidents for t in i.traffic]
                        if t.id == job.id), None)
            changed = changed or (old is not None and old != text)
            self.store.retext(job.id, text, job.status, job.confidence, model)
            if changed:
                log(f"re-run {job.id}: {text or '(no speech)'}")
        with self._lock:
            self._rerun_ids.discard(job.id)
            self.rerun["done"] += 1
            self.rerun["changed"] += changed
            self.rerun["failed"] += job.status == "error"

    # -- state --------------------------------------------------------------------------------------

    # -- one shared dongle ---------------------------------------------------------------------

    def _receiver(self, label: str) -> NfmReceiver | None:
        return next((r for r in self.receivers if r.channel.label == label), None)

    def _fresh_receiver(self, old: NfmReceiver) -> NfmReceiver:
        c = next(c for c in self.cfg.channels if c.label == old.channel.label)
        r = NfmReceiver(self.cfg.rtl_fm, self.cfg.shared_device, c.gain_db, Channel(c.mhz, c.label),
                        self._segment, log, open_db=c.squelch_db)
        self.receivers[self.receivers.index(old)] = r
        return r

    def _start_mode(self, mode: str) -> None:
        if mode == OFF_MODE:
            log("SDR released: no receiver running (switch it back on in the web app)")
            return
        if mode == P25_MODE:
            self.recorder.start()
        else:
            r = self._receiver(mode)
            if r is not None:
                self._fresh_receiver(r).start()

    def set_mode(self, mode: str) -> str | None:
        """Hand the shared dongle to another channel (or to trunk-recorder for Fire/EMS 15)."""
        if not self.shared:
            return "each channel has its own dongle: nothing to switch"
        if mode not in self.modes:
            return f"unknown mode {mode!r}; one of {', '.join(self.modes)}"
        with self._mode_lock:
            if mode == self.mode:
                return None
            self.switching = True
            try:
                log(f"SDR: {self.mode} -> {mode}")
                if self.mode == OFF_MODE:
                    pass
                elif self.mode == P25_MODE:
                    self.recorder.stop(f"SDR on {mode}")
                else:
                    r = self._receiver(self.mode)
                    if r is not None:
                        r.stop()
                time.sleep(SWITCH_PAUSE_S)  # let the dongle go before the next program opens it
                self.mode = mode
                self._start_mode(mode)
                if self._mode_file:
                    try:
                        self._mode_file.write_text(mode)
                    except OSError:
                        pass
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
        return "SDR released (Off)" if self.mode == OFF_MODE else f"SDR on {self.mode}"

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
        return ({"shared": True, "mode": self.mode, "modes": self.modes, "switching": self.switching}
                if self.shared else None)

    def radios(self) -> list[dict]:
        out = []
        for r in self.receivers:
            st = r.status()
            if self.shared and r.channel.label != self.mode:
                st.update(state="off", detail=self._off_detail(), open=False, quieting_db=None)
            out.append(st)
        if self.p25:
            st = self.p25.status()
            if self.recorder is not None:
                rs = self.recorder.status()
                if self.shared and self.mode != P25_MODE:
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
            pages = [dict(p.as_dict(), dispatch=getattr(p, "dispatch", None), history=getattr(p, "history", None) or [])
                     for p in list(self.pages)[:50]]
        st = {"time": time.time(), "radios": self.radios(), **self.store.state(), "pages": pages, "sdr": self.sdr(),
              "storage": getattr(self, "storage", None),
              "rerun": dict(self.rerun, pending=len(self._rerun_ids)),
              "talkgroups": self.tg.state() if self.p25 else []}
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
        if self.shared:
            for r in self.receivers:  # everything on the one dongle, only the selected mode running
                r.device = self.cfg.shared_device
            if self.mode:
                self._start_mode(self.mode)
        else:
            for r in self.receivers:
                r.start()
            if self.recorder:
                self.recorder.start()
        if self.p25:
            self.p25.start()
        return self

    def stop(self) -> None:
        for r in self.receivers:
            r.stop()
        if self.recorder:
            self.recorder.stop()
        if self.p25:
            self.p25.stop()
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
                       app.set_talkgroup, app.set_talkgroup_group, app.retranscribe)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    log(f"sizeup: {cfg.station_name} · {len(cfg.channels)} channel(s)"
        + (f" · P25 {cfg.p25_dir}" if cfg.p25_dir else "") + f" · {server.url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return 0
