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
from sizeup.radio.fm import RATE, Channel, NfmReceiver, Segment
from sizeup.radio.p25 import Call, CallIngest
from sizeup.radio.page import Page, assemble
from sizeup.radio.tones import TonePlan
from sizeup.stt import Transcriber, WhisperClient, wav_bytes
from sizeup.web.server import WebServer

ROOT = Path(__file__).resolve().parent.parent
SPLIT_PAGE_S = 30.0      # voice this soon after a tones-only page for us = that page's voice
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
        self.store = IncidentStore(set(cfg.ours), geocode=self._geocode)
        self.pages: deque[Page] = deque(maxlen=100)
        self.clips: OrderedDict[str, np.ndarray] = OrderedDict()
        self.clips_dir = Path(cfg.clips_dir) if cfg.clips_dir else None
        if self.clips_dir:
            self.clips_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_tones_only: Page | None = None
        self.receivers = [NfmReceiver(cfg.rtl_fm, c.device, c.gain_db, Channel(c.mhz, c.label),
                                      self._segment, log, open_db=c.squelch_db) for c in cfg.channels]
        self._kind = {c.label: c.kind for c in cfg.channels}
        self.p25 = (CallIngest(cfg.p25_dir, cfg.p25_talkgroups, self._call, cfg.p25_aliases, on_event=log)
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

    # -- clips ----------------------------------------------------------------------------------

    def _keep_clip(self, cid: str, audio: np.ndarray) -> None:
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
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

    def _segment(self, ch: Channel, seg: Segment) -> None:
        if self._kind.get(ch.label) == "paging":
            page = assemble(ch.label, ch.mhz, seg, self.plan)
            self._keep_clip(page.id, seg.audio)
            with self._lock:
                self.pages.appendleft(page)
            names = page.departments
            log(f"{ch.label}: {page.kind} " + (", ".join(t.describe() for t in page.tones) or "voice"))
            if page.status == "no_voice":
                if any(n in self.cfg.ours for n in names):
                    self._last_tones_only = page
                if page.tones and not names:
                    self._unknown_tones(page)
                return
            self.stt.submit(page, page.voice)
        else:
            t = Traffic(f"{ch.label.replace(' ', '')}-{int(seg.start * 1000)}", ch.label, seg.start, seg.end,
                        "", status="pending")
            self._keep_clip(t.id, seg.audio)
            self.stt.submit(t, seg.audio)

    def _unknown_tones(self, page: Page) -> None:
        log("  unknown tone pair(s) kept for learning: "
            + ", ".join(f"{t.a_hz:.1f}/{(t.b_hz or 0):.1f} Hz" for t in page.tones))

    def _call(self, call: Call) -> None:
        t = Traffic(call.id, call.label, call.start, call.end, "", status="pending",
                    unit=", ".join(call.units), talkgroup=call.talkgroup)
        if call.audio is not None:
            self._keep_clip(t.id, call.audio)
            self.stt.submit(t, call.audio)

    def _transcribed(self, item) -> None:
        if isinstance(item, Traffic):
            item.text = getattr(item, "raw", "")
            self.store.on_traffic(item)
            log(f"{item.source}: {item.unit + ': ' if item.unit else ''}{item.text or '(' + item.status + ')'}")
            return
        page: Page = item
        d = parse(page.raw) if page.raw else None
        page.dispatch = d.as_dict() if d else None  # type: ignore[attr-defined]
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

    def radios(self) -> list[dict]:
        out = [r.status() for r in self.receivers]
        if self.p25:
            out.append(self.p25.status())
        out.append({"label": "STT", "mhz": None, "state": self.whisper.state, "detail": self.whisper.detail,
                    "open": False, "quieting_db": None, "restarts": 0})
        return out

    def state(self) -> dict:
        with self._lock:
            pages = [dict(p.as_dict(), dispatch=getattr(p, "dispatch", None)) for p in list(self.pages)[:50]]
        return {"time": time.time(), "radios": self.radios(), **self.store.state(), "pages": pages}

    def scene(self) -> dict:
        fh = json.loads((ROOT / "data" / "firehouses.json").read_text())["firehouses"]
        lon, lat, zoom = self.cfg.home
        return {"station": {"id": self.cfg.station_id, "name": self.cfg.station_name},
                "home": {"center": [lon, lat], "zoom": zoom}, "incident_zoom": 17.2,
                "tiles": "https://tiles.openfreemap.org/planet",
                "glyphs": "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
                "firehouses": fh, "categories": CATEGORIES}

    def start(self) -> App:
        self.whisper.start()
        self.stt.start()
        for r in self.receivers:
            r.start()
        if self.p25:
            self.p25.start()
        return self

    def stop(self) -> None:
        for r in self.receivers:
            r.stop()
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
    server = WebServer(cfg.host, cfg.port, app.scene(), app.state, app.clip, app.store.close)
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
