"""Dispatch text and audio.

    build_text(incident, units)  "Port Chester Fire, Engine 60, Ladder 31, Rescue 40, respond to
                                  209 Westchester Avenue, cross of Pearl Street, for a structure
                                  fire. Time out 14:32."
    render_audio(text, tones)    alert tones (two-tone sequential) + gap + the voice read twice,
                                 16 kHz mono 16-bit WAV, the voice from macOS `say`

The written text is what the timeline shows; speech() rewrites it the way a dispatcher says it
(house numbers digit by digit, "time out fourteen thirty-two", chiefs "twenty-three ninety-eight").
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from sizeup.cad.store import TZ

RATE = 16000
GAP_S = 0.6          # after the tones, before the voice
REPEAT_GAP_S = 1.0   # between the two readings
AMPLITUDE = 0.5      # tones; the voice is normalised to the same peak
RAMP_S = 0.005       # tone on/off ramp, no clicks
SAY_TIMEOUT_S = 60
HOME_MUNI = "Port Chester"   # left out of the spoken address; any other municipality is said

# Street words as written in the gazetteer ("209 Westchester Ave") -> as said.
SUFFIXES = {"ave": "Avenue", "av": "Avenue", "st": "Street", "rd": "Road", "dr": "Drive", "ln": "Lane",
            "pl": "Place", "ct": "Court", "blvd": "Boulevard", "ter": "Terrace", "terr": "Terrace",
            "pkwy": "Parkway", "hwy": "Highway", "tpke": "Turnpike", "sq": "Square", "cir": "Circle",
            "plz": "Plaza", "expy": "Expressway", "ext": "Extension", "hts": "Heights", "trl": "Trail"}
DIRECTIONS = {"n": "North", "s": "South", "e": "East", "w": "West"}


class AudioUnavailable(RuntimeError):
    """No text-to-speech here (`say`/`afconvert` are macOS): the dispatch is text only."""


def spoken_street(s: str) -> str:
    """'209 Westchester Ave, Port Chester' -> '209 Westchester Avenue'. Abbreviations are expanded
    only where a street type goes (after a name), so 'St Marys Pl' stays Saint-able for the voice."""
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) > 1 and parts[-1].lower() == HOME_MUNI.lower():
        parts = parts[:-1]
    out = []
    for p in parts:
        words = p.split()
        for i, w in enumerate(words):
            key = w.lower().rstrip(".")
            named_before = any(not x.isdigit() for x in words[:i])
            if key in SUFFIXES and named_before:
                words[i] = SUFFIXES[key]
            elif key in DIRECTIONS and (i == 0 or words[i - 1].isdigit()) and i + 1 < len(words):
                words[i] = DIRECTIONS[key]
        out.append(" ".join(words))
    return ", ".join(out)


def type_phrase(card: dict | None, typ: str) -> str:
    if card and card.get("spoken"):
        return card["spoken"]
    t = (typ or "a call for service").lower().replace(" / ", " or ").replace(" – ", ", ")
    return t if t.startswith(("a ", "an ")) else ("an " if t[:1] in "aeiou" else "a ") + t


def build_text(department: str, incident: dict, unit_names: list[str], card: dict | None = None,
               at: float | None = None) -> str:
    at = at if at is not None else datetime.now(TZ).timestamp()
    parts = [department, *unit_names]
    where = spoken_street(incident.get("address") or "")
    s = ", ".join(p for p in parts if p)
    if where:
        s += f", respond to {where}"
        if incident.get("cross_street"):
            s += f", cross of {spoken_street(incident['cross_street'])}"
        s += f", for {type_phrase(card, incident.get('type', ''))}."
    else:
        s += f", respond for {type_phrase(card, incident.get('type', ''))}."
    s += f" Time out {datetime.fromtimestamp(at, TZ):%H:%M}."
    return s


def speech(text: str) -> str:
    """Written dispatch -> words for the voice."""
    def digits(m: re.Match) -> str:
        return m.group(1) + " ".join(m.group(2))
    t = re.sub(r"(respond to )(\d+)", digits, text)                      # "two zero nine Westchester"
    t = re.sub(r"\bTime out (\d{2}):(\d{2})", lambda m: f"Time out {m.group(1)} {m.group(2)}"
               if m.group(2) != "00" else f"Time out {m.group(1)} hundred", t)
    t = re.sub(r"\b(\d{2})(\d{2})\b(?= [A-Z])", r"\1 \2", t)            # "2398 Duty Officer": "23 98"
    return t


def tone_audio(tones: list[tuple[float, float]], rate: int = RATE) -> np.ndarray:
    chunks = []
    for hz, secs in tones:
        n = int(round(secs * rate))
        x = AMPLITUDE * np.sin(2 * np.pi * hz * np.arange(n) / rate)
        r = min(int(RAMP_S * rate), n // 2)
        if r:
            ramp = np.linspace(0, 1, r)
            x[:r] *= ramp
            x[-r:] *= ramp[::-1]
        chunks.append(x)
    return np.concatenate(chunks) if chunks else np.zeros(0)


def tts(text: str, voice: str = "", rate_wpm: int = 0) -> np.ndarray:
    """Speech from macOS `say`, as float samples at RATE."""
    say, afconvert = shutil.which("say"), shutil.which("afconvert")
    if not say or not afconvert:
        raise AudioUnavailable("no text-to-speech on this machine (macOS `say` and `afconvert` needed): text only")
    with tempfile.TemporaryDirectory(prefix="sizeup-say-") as d:
        txt, aiff, wav = Path(d, "in.txt"), Path(d, "out.aiff"), Path(d, "out.wav")
        txt.write_text(text)            # from a file: no text is ever read as a command-line option
        cmd = [say, "-o", str(aiff), "-f", str(txt)]
        if voice:
            cmd[1:1] = ["-v", voice]
        if rate_wpm:
            cmd[1:1] = ["-r", str(int(rate_wpm))]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=SAY_TIMEOUT_S)
            subprocess.run([afconvert, "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(aiff), str(wav)],
                           check=True, capture_output=True, timeout=SAY_TIMEOUT_S)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="replace").strip()
            raise AudioUnavailable(f"text-to-speech failed: {err or e}") from e
        except subprocess.TimeoutExpired as e:
            raise AudioUnavailable("text-to-speech timed out") from e
        with wave.open(str(wav)) as w:
            if w.getframerate() != RATE or w.getsampwidth() != 2:
                raise AudioUnavailable(f"text-to-speech gave {w.getframerate()} Hz / {8 * w.getsampwidth()}-bit audio")
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    return x * (AMPLITUDE / peak) if peak > 0 else x


def render(text: str, tones: list[tuple[float, float]], voice: str = "", rate_wpm: int = 0,
           repeat: int = 2) -> np.ndarray:
    v = tts(speech(text), voice, rate_wpm)
    parts = [tone_audio(tones), np.zeros(int(GAP_S * RATE))] if tones else []
    for k in range(max(1, repeat)):
        if k:
            parts.append(np.zeros(int(REPEAT_GAP_S * RATE)))
        parts.append(v)
    return np.concatenate(parts).astype(np.float32)


def wav_bytes(x: np.ndarray, rate: int = RATE) -> bytes:
    import io
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def audio_key(text: str, tones, voice: str, rate_wpm: int, repeat: int) -> str:
    """Same text, tones and voice = same file: a preview and the dispatch right after share it."""
    return hashlib.sha1(repr((text, list(tones), voice, rate_wpm, repeat)).encode()).hexdigest()[:20]


def render_audio(text: str, tones, out_dir: str | Path, voice: str = "", rate_wpm: int = 0,
                 repeat: int = 2) -> Path:
    """The dispatch as a WAV under out_dir (cached by content). Raises AudioUnavailable."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{audio_key(text, tones, voice, rate_wpm, repeat)}.wav"
    if not path.is_file():
        data = wav_bytes(render(text, list(tones), voice, rate_wpm, repeat))
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path
