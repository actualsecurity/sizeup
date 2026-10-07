"""The dispatch voice: a warm Kokoro-82M text-to-speech server on 127.0.0.1 (CPU, so it never
competes with whisper-server for the GPU).

Runs on the TTS environment's Python (tools/setup_tts.sh), not sizeup's, and imports nothing from
sizeup; sizeup's TtsService (sizeup/tts.py) starts it and keeps it running:

    ~/.local/share/sizeup/tts/venv/bin/python -I sizeup/tts_server.py --port 8093 --voice af_heart

    GET  /health   {"ok": true, "voice", "device", "voices": [...]}   (only once warmed up: the port
                   isn't opened before the model is loaded and has spoken once)
    GET  /voices   {"voices": ["af_heart", ...]}  the voices on disk
    POST /tts      {"text", "voice"?, "speed"?} -> audio/wav, 24 kHz mono 16-bit. The text is said as
                   given: sizeup has already written numbers out the dispatch way (sizeup/cad/speech.py).

The model and voices come from the environment's Hugging Face cache (<env>/hf, offline): nothing
is downloaded at run time and nothing leaves the machine.
"""

import argparse
import io
import json
import os
import re
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = "hexgrad/Kokoro-82M"
RATE = 24000
MAX_TEXT = 2000
VOICE = re.compile(r"[ab][fm]_[a-z]{2,20}")   # American / British English voices


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--voice", default="af_heart", help="loaded and warmed up at start")
    ap.add_argument("--threads", type=int, default=0, help="CPU threads (0 = torch's default)")
    args = ap.parse_args()
    os.environ.setdefault("HF_HOME", str(Path(sys.prefix).parent / "hf"))   # <env>/venv -> <env>/hf
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import warnings
    warnings.filterwarnings("ignore")

    t0 = time.time()
    import numpy as np
    import torch
    from kokoro import KModel, KPipeline
    if args.threads > 0:
        torch.set_num_threads(args.threads)
    cache = Path(os.environ["HF_HOME"], "hub", "models--" + REPO.replace("/", "--"))
    try:
        voices_dir = cache / "snapshots" / (cache / "refs" / "main").read_text().strip() / "voices"
    except OSError:
        print(f"tts_server: no Kokoro model in {cache}: run tools/setup_tts.sh", file=sys.stderr)
        return 2
    voices = sorted(p.stem for p in voices_dir.glob("*.pt") if VOICE.fullmatch(p.stem))
    if args.voice not in voices:
        print(f"tts_server: voice {args.voice!r} not installed (have: {', '.join(voices)})", file=sys.stderr)
        return 2
    model = KModel(repo_id=REPO).to("cpu").eval()
    pipes: dict[str, KPipeline] = {}
    lock = threading.Lock()                      # one synthesis at a time (dispatches are sequential)

    def synth(text: str, voice: str, speed: float) -> bytes:
        with lock, torch.inference_mode():
            lang = voice[0]
            if lang not in pipes:
                pipes[lang] = KPipeline(lang_code=lang, repo_id=REPO, model=model)
            parts = [a.cpu().numpy() if hasattr(a, "cpu") else np.asarray(a)
                     for _, _, a in pipes[lang](text, voice=voice, speed=speed) if a is not None]
        pcm = (np.clip(np.concatenate(parts) if parts else np.zeros(0), -1, 1) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()

    synth("Port Chester Fire, Engine sixty.", args.voice, 1.0)   # warm-up: the first call is the slow one

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code: int, msg: str) -> None:
            self._send(code, json.dumps({"error": msg}).encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, json.dumps({"ok": True, "engine": "kokoro", "voice": args.voice,
                                                   "device": "cpu", "voices": voices}).encode())
            if self.path == "/voices":
                return self._send(200, json.dumps({"voices": voices}).encode())
            self._error(404, "not found")

        def do_POST(self) -> None:
            if self.path != "/tts":
                return self._error(404, "not found")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                text = str(req.get("text") or "").strip()
                voice = str(req.get("voice") or args.voice)
                speed = float(req.get("speed", 1.0))
            except (ValueError, TypeError):
                return self._error(400, "a JSON body {text, voice?, speed?}")
            if not text or len(text) > MAX_TEXT:
                return self._error(400, f"text: 1 to {MAX_TEXT} characters")
            if voice not in voices:
                return self._error(400, f"voice: one of {', '.join(voices)}")
            if not 0.5 <= speed <= 2.0:
                return self._error(400, "speed: 0.5 to 2.0")
            t1 = time.time()
            try:
                wav = synth(text, voice, speed)
            except Exception as e:  # the caller falls back to `say`
                return self._error(500, f"synthesis failed: {e}")
            self.log_message("%s %.2fx: %.2f s for %d chars", voice, speed, time.time() - t1, len(text))
            self._send(200, wav, "audio/wav")

        def log_message(self, fmt: str, *a) -> None:
            if a and isinstance(a[0], str) and "/health" in a[0]:
                return                                   # polled every few seconds by sizeup
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} tts_server: {fmt % a}\n")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"tts_server: ready on 127.0.0.1:{args.port} in {time.time() - t0:.1f} s (cpu, {args.voice}, "
          f"{len(voices)} voices)", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
