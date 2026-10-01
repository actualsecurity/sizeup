"""Speech-to-text through a persistent whisper.cpp server (model stays loaded on the GPU).

Same client as shortfinal. The model for fire radio is a general English one (large-v3-turbo
on the Mac); the ATC fine-tune is the wrong vocabulary here. An area prompt (streets, units,
incident types) can be passed per request.
"""

from __future__ import annotations

import io
import json
import queue
import re
import threading
import time
import urllib.request
import uuid
import wave
from typing import Callable

import numpy as np

NOISE_LABEL = re.compile(r"^[\[(*].*[\])*]$")  # "(static)", "[MUSIC]" — not speech


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    pcm = audio if audio.dtype == np.int16 else (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


def voice_band(audio: np.ndarray, rate: int = 16000) -> np.ndarray:
    """300-3400 Hz band-pass + normalise: drops FM hiss above the voice and CTCSS below it."""
    x = audio.astype(np.float64) - float(np.mean(audio))
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / rate)
    X[(f < 300) | (f > 3400)] = 0
    y = np.fft.irfft(X, len(x))
    peak = np.percentile(np.abs(y), 99.5) or 1.0
    return (y / peak * 0.7).astype(np.float32)


class WhisperClient:
    def __init__(self, url: str, timeout_s: float = 60.0):
        self.url, self.timeout_s = url, timeout_s

    def transcribe(self, audio16k: np.ndarray, prompt: str = "") -> tuple[str, float | None]:
        pad = np.zeros(8000, dtype=np.float32)  # whisper likes a little silence around speech
        body_wav = wav_bytes(np.concatenate([pad, audio16k.astype(np.float32), pad]))
        boundary = uuid.uuid4().hex
        parts = []
        fields = [("response_format", "verbose_json"), ("temperature", "0.0")]
        if prompt:
            fields.append(("prompt", prompt))
        for name, value in fields:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="tx.wav"\r\n'
                      "Content-Type: audio/wav\r\n\r\n").encode() + body_wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url, data=b"".join(parts), method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            out = json.load(r)
        text = " ".join(str(out.get("text", "")).split())
        probs = [w.get("probability") for s in out.get("segments", []) for w in s.get("words", [])
                 if isinstance(w.get("probability"), (int, float))]
        return text, (round(float(np.mean(probs)), 2) if probs else None)


class Transcriber:
    """One worker thread: items with an `audio_for_stt()`-style accessor are transcribed in
    order and handed to on_done. whisper-server restarting -> retry with backoff."""

    def __init__(self, client: WhisperClient, on_done: Callable[[object], None],
                 prompt: Callable[[object], str] = lambda item: ""):
        self.client, self.on_done, self.prompt = client, on_done, prompt
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self.last_error = ""

    def submit(self, item, audio: np.ndarray) -> bool:
        try:
            self._q.put_nowait((item, audio))
            return True
        except queue.Full:
            return False

    def start(self) -> Transcriber:
        threading.Thread(target=self._run, name="stt", daemon=True).start()
        return self

    def _run(self) -> None:
        while True:
            item, audio = self._q.get()
            for attempt in range(3):
                try:
                    raw, conf = self.client.transcribe(voice_band(audio), self.prompt(item))
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:160]
                    time.sleep(2 + 3 * attempt)
            else:
                item.status = "error"
                self.on_done(item)
                continue
            if not raw or NOISE_LABEL.match(raw):
                raw, item.status = "", "no_speech"
            else:
                item.status = "done"
            item.raw, item.confidence = raw, conf
            self.on_done(item)
