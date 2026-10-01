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
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

import numpy as np

from sizeup.config import Config, ConfigError, load
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore, Traffic
from sizeup.radio.fm import RATE, Channel, NfmReceiver, Segment, speech_seconds
from sizeup.radio.mdc1200 import decode as mdc_decode
from sizeup.radio.p25 import Call, CallIngest
from sizeup.radio.recorder import TrunkRecorder
from sizeup.radio.page import Page, assemble, page_from_dict
from sizeup.radio.tones import TonePlan
from sizeup.stt import Transcriber, WhisperClient, radio_voice, wav_bytes
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
    """Keep whisper-server running (model stays loaded). Reuses one already on the port."""

    def __init__(self, binary: str, model: str, port: int, threads: int):
        self.argv = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-t", str(threads)]
        self.port, self.state, self.detail = port, "starting", ""
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()

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
        while not self._stop.is_set():
            if self.up():
                self.state, self.detail, backoff = "ok", "speech-to-text ready", 2.0
            elif self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = subprocess.Popen(self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.state, self.detail = "starting", "loading model"
                except OSError as e:
                    self.state, self.detail = "down", f"cannot start whisper-server: {e}"
                    backoff = min(backoff * 2, 30)
            self._stop.wait(backoff)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plan = TonePlan(list(cfg.tones))
        self.whisper = WhisperService(cfg.stt_server, cfg.stt_model, cfg.stt_port, cfg.stt_threads)
        self.stt = Transcriber(WhisperClient(f"http://127.0.0.1:{cfg.stt_port}/inference"), self._transcribed,
                               prompt=lambda item: cfg.stt_prompt)
        self.gaz = self._load_gazetteer()
        self.keep_s = cfg.keep_hours * 3600
        self.clips: OrderedDict[str, np.ndarray] = OrderedDict()
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
        self._lock = threading.Lock()
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
        self.p25 = (CallIngest(cfg.p25_dir, cfg.p25_talkgroups, self._call, cfg.p25_aliases, on_event=log,
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
        for f in list(self.clips_dir.glob("*.wav")) + list(self.clips_dir.glob("*.json")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
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

    def _save_page(self, page: Page) -> None:
        if not self.clips_dir:
            return
        try:
            d = dict(page.as_dict(), dispatch=getattr(page, "dispatch", None))
            (self.clips_dir / f"{page.id}.page.json").write_text(json.dumps(d))
        except OSError:
            pass

    # -- clips ----------------------------------------------------------------------------------

    def _keep_clip(self, cid: str, audio: np.ndarray, processed: bool = False) -> None:
        # Stored as heard through a radio speaker (de-emphasis, voice band, steady loudness),
        # not the raw discriminator output, which is quiet and dominated by CTCSS + hiss.
        pcm = (np.clip(audio if processed else radio_voice(audio), -1, 1) * 32767).astype(np.int16)
        with self._lock:
            self.clips[cid] = pcm
            while len(self.clips) > 400:
                self.clips.popitem(last=False)
        if self.clips_dir:
            try:
                (self.clips_dir / f"{cid}.wav").write_bytes(wav_bytes(pcm, RATE))
            except OSError:
                pass

    def clip(self, cid: str) -> bytes | None:
        if not cid.replace("-", "").replace("_", "").isalnum():
            return None
        with self._lock:
            pcm = self.clips.get(cid)
        if pcm is not None:
            return wav_bytes(pcm, RATE)
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
            self.stt.submit(page, page.voice)
        else:
            t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end,
                        "", status="pending", unit=", ".join(units))
            t.low_energy = low_energy  # type: ignore[attr-defined]
            t.raw_audio = seg.audio    # type: ignore[attr-defined]  (saved if it turns out to be a click)
            self._keep_clip(t.id, seg.audio)
            self.stt.submit(t, seg.audio)

    def _unknown_tones(self, page: Page) -> None:
        log("  unknown tone pair(s) kept for learning: "
            + ", ".join(f"{t.a_hz:.1f}/{(t.b_hz or 0):.1f} Hz" for t in page.tones))

    def _call(self, call: Call) -> None:
        if not self.tg.record(call.talkgroup):  # switched off in the web app: dropped on arrival
            self.tg.seen(call.talkgroup, dropped=True)
            return
        self.tg.seen(call.talkgroup)
        t = Traffic(call.id, call.label, call.start, call.end, "", status="pending",
                    unit=", ".join(call.units), talkgroup=call.talkgroup)
        if call.audio is not None:
            t.fm = False  # decoded P25 voice: no FM de-emphasis  # type: ignore[attr-defined]
            self._keep_clip(t.id, radio_voice(call.audio, deemph=False), processed=True)
            self.stt.submit(t, call.audio)

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
        page: Page = item
        d = parse(page.raw) if page.raw else None
        if d is not None:  # units dispatched name their department ("Engine 61" -> Port Chester FD)
            for n in d.numbers:  # "174, 175, Ladder 1": engines read without the word, if the roster has them
                u = f"Engine {n}"
                if u in self.cfg.unit_departments and u not in d.units:
                    d.units.append(u)
            for u in d.units:
                dep = self.cfg.unit_departments.get(u)
                if dep and dep not in d.departments:
                    d.departments.append(dep)
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
            pages = [dict(p.as_dict(), dispatch=getattr(p, "dispatch", None)) for p in list(self.pages)[:50]]
        st = {"time": time.time(), "radios": self.radios(), **self.store.state(), "pages": pages, "sdr": self.sdr(),
              "talkgroups": self.tg.state() if self.p25 else []}
        # Tell the display which entries have a recording (so ▶ isn't a dead button).
        for p in st["pages"]:
            p["has_audio"] = self.has_clip(p["id"])
        for t in st["loose_traffic"]:
            t["has_audio"] = self.has_clip(t["id"])
        for inc in st["incidents"]:
            for t in inc["traffic"]:
                t["has_audio"] = self.has_clip(t["id"])
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
                       app.set_talkgroup)
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
