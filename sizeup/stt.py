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
# What whisper says about a short burst of noise (a mic click, a squelch tail).
# What whisper says for dead air / a mic click (FG1 bench: "Thank you." or "Yes." on 6 of 6
# dead-air cuts). Rejected only when the audio has no voice energy or is very short.
HALLUCINATIONS = {"thank you", "thanks", "thank you very much", "yes", "you", "bye", "bye bye", "okay",
                  "ok", "thanks for watching", "thank you for watching", "so", "oh", "the end"}
SHORT_S = 1.5


def wav_bytes(audio: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    pcm = audio if audio.dtype == np.int16 else (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


DEEMPH_TAU = 750e-6     # land-mobile NFM de-emphasis (6 dB/octave, corner ~212 Hz)
TARGET_DBFS = -16.0     # loudness of the speech after processing
PEAK_LIMIT = 0.9


def radio_voice(audio: np.ndarray, rate: int = 16000, deemph: bool = True) -> np.ndarray:
    """FM discriminator output -> what a radio's speaker plays, at a steady loudness.

    De-emphasis (transmitters pre-emphasise voice), 300-3000 Hz band-pass (drops the CTCSS
    tone + DC drift below and the FM hiss above, which otherwise dominate the level), then
    set the *speech* loudness: RMS of the louder half of 20 ms blocks to TARGET_DBFS, with
    a peak limit. Used for both the clips people play and the audio speech-to-text gets.
    deemph=False for already-decoded digital voice (P25 calls from trunk-recorder)."""
    x = audio.astype(np.float64)
    if len(x) < 64:
        return x.astype(np.float32)
    X = np.fft.rfft(x - x.mean())
    f = np.fft.rfftfreq(len(x), 1 / rate)
    if deemph:
        X = X / (1 + 1j * 2 * np.pi * f * DEEMPH_TAU)
    # Gentle (raised-cosine) band edges: a brick wall rings audibly on short clips.
    gain = np.ones_like(f)
    lo, hi = (f < 300), (f > 3000)
    gain[f < 200] = 0.0
    m = (f >= 200) & lo
    gain[m] = 0.5 - 0.5 * np.cos(np.pi * (f[m] - 200) / 100)
    gain[f > 3400] = 0.0
    m = hi & (f <= 3400)
    gain[m] = 0.5 + 0.5 * np.cos(np.pi * (f[m] - 3000) / 400)
    y = np.fft.irfft(X * gain, len(x))
    blk = rate // 50
    n = len(y) // blk
    if n >= 3:
        # Loudness of the speech itself: blocks 8 dB over the clip's dead-air floor (radio clips are
        # mostly dead air between words; averaging that in would leave the voice too quiet).
        e = np.sqrt((y[: n * blk].reshape(n, blk) ** 2).mean(axis=1)) + 1e-12
        db = 20 * np.log10(e)
        active = e[db > np.percentile(db, 5) + 8]
        speech_rms = float(np.sqrt(np.mean((active if len(active) else e) ** 2)))
    else:
        speech_rms = float(np.sqrt(np.mean(y ** 2)))
    if speech_rms <= 1e-9:
        return np.zeros(len(y), np.float32)
    # Gain from the speech level; clicks (key-up, squelch crash) are soft-clipped instead of
    # being allowed to turn the whole clip down. Only real speech peaks limit the gain.
    g = 10 ** (TARGET_DBFS / 20) / speech_rms
    body = float(np.percentile(np.abs(y), 99.5)) * g
    if body > PEAK_LIMIT:
        g *= PEAK_LIMIT / body
    z = y * g
    knee = 0.6
    over = np.abs(z) > knee
    z[over] = np.sign(z[over]) * (knee + (1 - knee) * np.tanh((np.abs(z[over]) - knee) / (1 - knee)))
    return z.astype(np.float32)


voice_band = radio_voice  # older name


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
                    raw, conf = self.client.transcribe(radio_voice(audio, deemph=getattr(item, "fm", True)),
                                                      self.prompt(item))
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:160]
                    time.sleep(2 + 3 * attempt)
            else:
                item.status = "error"
                self.on_done(item)
                continue
            said = re.sub(r"[^a-z0-9 ]", "", raw.lower()).strip()
            if (not raw or NOISE_LABEL.match(raw) or not said  # "." = squelch tail
                    or (said in HALLUCINATIONS
                        and (len(audio) / 16000 < SHORT_S or getattr(item, "low_energy", False)))):
                raw, item.status = "", "no_speech"
            else:
                item.status = "done"
            item.raw, item.confidence = raw, conf
            self.on_done(item)
