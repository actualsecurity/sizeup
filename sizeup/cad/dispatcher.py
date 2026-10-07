"""Dispatch text and audio.

    compose("new", ...)     "Port Chester Fire, Engine 60, Ladder 31, Rescue 40, respond to
                             209 Westchester Avenue, cross of Pearl Street, for a structure fire.
                             Time out 14:32."
    compose("add_on", ...)  "Port Chester Fire, Engine 61, added to the assignment. Engine 60
                             responding, Ladder 31 and 23 98 on location. 169 North Main Street,
                             commercial fire alarm. Time out 15:13."
    compose("upgrade", ...) "Port Chester Fire, upgrade on your last alarm, 169 North Main Street.
                             Smoke in the structure. Now a structure fire, full department
                             response. Time out 15:20."
    full=True               "full department response" instead of the units' names
    remarks="two vehicles"  "... for a motor vehicle accident. Reported: two vehicles. Time out ..."
    lead_in(...)            "Port Chester Fire" / "Port Chester and Rye Brook Fire" / "Rye Brook Fire"
    tone_plan(...)          a tone group per department paged (Port Chester's, then Rye Brook's); a
                            department without tones gets Port Chester's, with a warning
    render_dispatch(text, tones, out_dir, VoiceCfg(...))
                                 alert tones + gap + the voice read twice. tones: one group of
                                 [hz, s] (two-tone sequential) or several groups (one per
                                 department paged), played in order with GROUP_GAP_S between;
                                 16 kHz mono 16-bit WAV, and which voice said it. The voice: the
                                 Kokoro server (sizeup/tts.py) or, when it's down, errors or takes
                                 over 10 s, macOS `say` with the fallback voice
    render_audio(text, tones)    the same with `say` only (the WAV's path)

The written text is what the timeline shows; speech() rewrites it the way a dispatcher says it
(sizeup/cad/speech.py: "two oh nine Westchester Avenue", "time out fourteen thirty-two"), the same
text for every engine. Unit names come already as said (cad_lists.json units.spoken: "Engine 60",
"23 98", "77 Alpha 1"); a unit code still in the text is said by that list too.
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
from typing import Callable

import numpy as np

from sizeup.cad import speech as spoken_words
from sizeup.cad.store import TZ
from sizeup.tts import TtsError, VoiceCfg, synthesize

RATE = 16000
GAP_S = 0.6          # after the tones, before the voice
GROUP_GAP_S = 0.5    # between tone groups (one department's tones, then the next's)
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


# The wording, as templates (data/cad_lists.json "dispatch_wording" overrides any of these). Empty
# parts drop out with their punctuation (tidy()).
WORDING = {
    "new": "{department}, {units}, respond to {location}, for {type}. {remarks} Time out {time}.",
    "new_no_location": "{department}, {units}, respond for {type}. {remarks} Time out {time}.",
    "add_on": "{department}, {units}, added to the assignment. {already} {location}, {type_bare}. {remarks} Time out {time}.",
    "upgrade": "{department}, upgrade on your last alarm, {location}. {info} {change} {respond} {remarks} Time out {time}.",
    "already_group": "{units} {status}",
    "change_type": "Now {type}.",
    "change_type_full": "Now {type}, {full}.",
    "change_full": "{Full}.",
    "respond": "{units}, respond.",
    "full_response": "full department response",
    "remarks": "Reported: {remarks}",                  # the dispatcher's remarks, said after the type
    "lead_in": "{department}",                         # {department}: the config's ("Port Chester Fire")
    "lead_in_joint": "Port Chester and {joint} Fire",  # a joint-response department's unit goes too
}
REMARKS_MAX = 500
# Dispatch shorthand that can only mean one thing, said in full ("w/" -> "with").
REMARK_WORDS = [(re.compile(r"(?<!\w)w/o(?!\w)", re.I), "without"), (re.compile(r"(?<!\w)w/(?=\s)", re.I), "with"),
                (re.compile(r"(?<!\w)vs\.?(?=\s)", re.I), "versus"), (re.compile(r"\s&\s"), " and ")]


def clean_remarks(text: str) -> str:
    """The operator's remarks, ready to say: trimmed, one space, shorthand said in full, ending in a period.
    '  2 cars w/  fluid leaking ' -> '2 cars with fluid leaking.'"""
    t = re.sub(r"\s+", " ", text or "").strip()
    for pat, words in REMARK_WORDS:
        t = pat.sub(words, t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(",;:- ")
    return t if not t or t[-1] in ".!?" else t + "."


def lead_in(department: str, joint: list[str] = (), own: bool = True, full: bool = False,
            wording: dict | None = None, joint_only: dict | None = None) -> str:
    """Whose dispatch it is. joint: the joint-response towns with a unit on this transmission or already on
    the event ("Rye Brook"); own: one of the department's own units is too. None of theirs: "Port Chester
    Fire"; theirs and ours (or full department response): "Port Chester and Rye Brook Fire"; only theirs:
    joint_only[town] ("Rye Brook Fire")."""
    w = dict(WORDING, **(wording or {}))
    joint = [t for t in dict.fromkeys(joint) if t]
    if not joint:
        return w["lead_in"].format(department=department)
    if not own and not full and len(joint) == 1 and (joint_only or {}).get(joint[0]):
        return joint_only[joint[0]]
    return w["lead_in_joint"].format(department=department, joint=join_and(joint))


def join_and(names: list[str]) -> str:
    """['Engine 61', 'Rescue 40'] -> 'Engine 61 and Rescue 40'."""
    names = [n for n in names if n]
    return names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1] if names else "")


def where(incident: dict) -> str:
    w = spoken_street(incident.get("address") or "")
    if w and incident.get("cross_street"):
        w += f", cross of {spoken_street(incident['cross_street'])}"
    return w


def bare(phrase: str) -> str:
    """'a commercial fire alarm' -> 'commercial fire alarm'."""
    return re.sub(r"^(an?|the) ", "", phrase or "")


def sentence(text: str) -> str:
    """'smoke in the structure' -> 'Smoke in the structure.'"""
    t = (text or "").strip()
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    return t if t[-1] in ".!?" else t + "."


def tidy(s: str) -> str:
    """Drop what an empty part left behind: doubled spaces, ', .', ', ,', a sentence starting with ', '."""
    s = re.sub(r"\s+", " ", s)
    for _ in range(3):
        s = re.sub(r"\s+([.,])", r"\1", s)
        s = re.sub(r",(\s*[.,])", r"\1", s)
        s = re.sub(r"\.\s*\.", ".", s)
        s = re.sub(r"(^|\.\s)\s*,\s*", r"\1", s)
        s = re.sub(r"\.,\s*", ". ", s)
    s = re.sub(r"(\.\s)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)   # a sentence starts upper case
    return s.strip()


def tone_plan(departments: list[tuple[str, list | None]], home: str, home_tones) -> dict:
    """[("Port Chester", tones), ("Rye Brook", None)] -> {"groups": [[(hz, s), ...] per department, in
    turn], "label": "Port Chester + Rye Brook (not set → PC)", "warnings": [...]}. Tones not set (None)
    are never skipped: the home department's are played instead, with a warning."""
    groups, labels, warnings = [], [], []
    short = "".join(w[0] for w in home.split()).upper()
    for name, t in departments or [(home, home_tones)]:
        if not t:
            t = home_tones
            labels.append(f"{name} (not set → {short})")
            warnings.append(f"{name} tones not set — played {home}'s")
        else:
            labels.append(name)
        groups.append([tuple(x) for x in t or ()])
    return {"groups": groups, "label": " + ".join(labels), "warnings": warnings}


def compose(kind: str, *, department: str, incident: dict, units: list[str], phrase: str = "",
            at: float | None = None, full: bool = False, already: list[tuple[str, list[str]]] = (),
            info: str = "", new_phrase: str = "", wording: dict | None = None, remarks: str = "") -> str:
    """The written dispatch (what the chronology shows and, through speech(), what is said).

    kind      new | add_on (units added to an event that already had a dispatch) | upgrade
    units     the units sent, as spoken ("Engine 60", "23 98"); with full=True they are not
              spoken: "full department response" instead
    already   add_on: the units already on the event by status, [("responding", ["Engine 60"]), ...]
    info      upgrade: the new information ("smoke in the structure")
    new_phrase upgrade: the new type as spoken, when the type changed ("a structure fire")
    remarks   the dispatcher's remarks ("two vehicles, one on its side"): "Reported: ...", before the time out
    department the lead-in (lead_in(): "Port Chester and Rye Brook Fire" when theirs go too)
    """
    w = dict(WORDING, **(wording or {}))
    at = at if at is not None else datetime.now(TZ).timestamp()
    phrase = phrase or "a call for service"
    loc = where(incident)
    fr = w["full_response"]
    if kind == "new":
        tpl = w["new"] if loc else w["new_no_location"]
        units_text = fr if full else ", ".join(u for u in units if u)
    elif kind == "add_on":
        tpl, units_text = w["add_on"], fr if full else join_and(units)
    elif kind == "upgrade":
        tpl, units_text = w["upgrade"], ""
    else:
        raise ValueError(f"dispatch kind {kind!r}")
    groups = [w["already_group"].format(units=join_and(names), status=st) for st, names in already if names]
    if new_phrase and full:
        change = w["change_type_full"].format(type=new_phrase, full=fr, Full=fr[:1].upper() + fr[1:])
    elif new_phrase:
        change = w["change_type"].format(type=new_phrase, full=fr, Full=fr[:1].upper() + fr[1:])
    elif full:
        change = w["change_full"].format(full=fr, Full=fr[:1].upper() + fr[1:])
    else:
        change = ""
    respond = w["respond"].format(units=", ".join(units)) if kind == "upgrade" and units and not full else ""
    remarks = clean_remarks(remarks)
    if remarks and "{remarks}" not in tpl:              # a template from before remarks: before the time out
        tpl = tpl.replace("Time out {time}", "{remarks} Time out {time}") if "Time out {time}" in tpl else tpl + " {remarks}"
    s = tpl.format(department=department, units=units_text, location=loc, type=phrase, type_bare=bare(phrase),
                   already=(", ".join(groups) + ".") if groups else "", info=sentence(info), change=change,
                   respond=respond, time=f"{datetime.fromtimestamp(at, TZ):%H:%M}", full=fr,
                   remarks=w["remarks"].format(remarks=remarks) if remarks else "")
    return tidy(s)


def build_text(department: str, incident: dict, unit_names: list[str], phrase: str = "",
               at: float | None = None, full: bool = False, wording: dict | None = None, remarks: str = "") -> str:
    """A first dispatch. unit_names as spoken ("Engine 60", "23 98"); phrase as spoken ("a
    commercial fire alarm", from the run card or the data file); remarks: "Reported: ..."."""
    return compose("new", department=department, incident=incident, units=unit_names, phrase=phrase, at=at,
                   full=full, wording=wording, remarks=remarks)


def speech(text: str, location: str = "", spoken: dict[str, str] | None = None) -> str:
    """Written dispatch -> words for the voice (sizeup/cad/speech.py): "two oh nine Westchester
    Avenue", "Ladder thirty-one and twenty-three ninety-eight", "Time out fifteen thirteen".
    `spoken`: unit code -> as said (cad_lists.json), for codes still in the text ("77A4").
    `location` is accepted for older callers: every number is said the same way now."""
    return spoken_words.words(text, spoken)


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
    return _peak(x)


def _peak(x: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    return (x * (AMPLITUDE / peak) if peak > 0 else x).astype(np.float32)


def tone_groups(tones) -> tuple[tuple[tuple[float, float], ...], ...]:
    """[[hz, s], ...] (one group) or [[[hz, s], ...], [[hz, s], ...]] (groups, in order) -> groups.
    Empty groups are dropped."""
    tones = list(tones or [])
    flat = bool(tones) and all(isinstance(t, (list, tuple)) and len(t) == 2
                               and all(isinstance(v, (int, float)) for v in t) for t in tones)
    groups = [tones] if flat else tones
    return tuple(g for g in (tuple((float(hz), float(sec)) for hz, sec in grp) for grp in groups) if g)


def tone_sequence(tones, rate: int = RATE) -> np.ndarray:
    """Each tone group in order, GROUP_GAP_S of silence between groups."""
    parts = []
    for k, g in enumerate(tone_groups(tones)):
        if k:
            parts.append(np.zeros(int(GROUP_GAP_S * rate)))
        parts.append(tone_audio(list(g), rate))
    return np.concatenate(parts) if parts else np.zeros(0)


TAIL_GAP_S = 0.25
TIME_OUT = re.compile(r"\s*(Time out\b[^.]*\.?)\s*$")


def split_time_out(said: str) -> tuple[str, str]:
    """'... Reported: x. Time out fifteen thirteen.' -> (body, 'Time out fifteen thirteen.'). The
    message is read twice but the time out only once, after the second reading (user, 2026-10-07)."""
    m = TIME_OUT.search(said)
    return (said[:m.start()].rstrip(), m.group(1)) if m else (said, "")


def assemble(v: np.ndarray, tones, repeat: int = 2, tail: np.ndarray | None = None) -> np.ndarray:
    """Tone groups, a gap, then the voice `repeat` times with a pause between; `tail` (the time out)
    once, after the last reading."""
    t = tone_sequence(tones)
    parts = [t, np.zeros(int(GAP_S * RATE))] if len(t) else []
    for k in range(max(1, repeat)):
        if k:
            parts.append(np.zeros(int(REPEAT_GAP_S * RATE)))
        parts.append(v)
    if tail is not None and len(tail):
        parts += [np.zeros(int(TAIL_GAP_S * RATE)), tail]
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


def audio_key(said: str, tones, engine: str, voice: str, pace: float, repeat: int) -> str:
    """Same words, tones, engine, voice and speed = same file: a preview and the dispatch right
    after share it."""
    return hashlib.sha1(repr((said, tone_groups(tones), engine, voice, pace, repeat)).encode()).hexdigest()[:20]


def _write(path: Path, x: np.ndarray) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(wav_bytes(x))
    os.replace(tmp, path)


def render_dispatch(text: str, tones, out_dir: str | Path, voice: VoiceCfg, repeat: int = 2,
                    spoken: dict[str, str] | None = None,
                    log: Callable[[str], None] | None = None) -> tuple[Path, str]:
    """The dispatch as a WAV under out_dir (cached by content) and which voice said it ("Voice:
    Kokoro af_heart", "Voice: Samantha (fallback)"). Engine kokoro falls back to `say` with
    voice.fallback_voice when the server is down, errors or is slower than voice.timeout_s.
    Raises AudioUnavailable (no voice at all)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    said = speech(text, spoken=spoken)
    body, tail = split_time_out(said)
    key_said = said + "\x00time-out-once"          # (older files said the time out every reading)
    tones = tone_groups(tones)
    fallback = False
    if voice.engine == "kokoro":
        path = out_dir / f"{audio_key(key_said, tones, 'kokoro', voice.voice, voice.speed, repeat)}.wav"
        if path.is_file():
            return path, voice.label()
        try:
            say = lambda x: _peak(synthesize(x, voice.voice, voice.speed, voice.port, voice.timeout_s))
            _write(path, assemble(say(body), tones, repeat, say(tail) if tail else None))
            return path, voice.label()
        except TtsError as e:
            fallback = True
            if log:
                log(f"CAD: dispatch voice: {e}; using {voice.fallback_voice or 'say'} instead")
        say_voice = voice.fallback_voice
    else:
        say_voice = voice.voice
    path = out_dir / f"{audio_key(key_said, tones, 'say', say_voice, voice.rate_wpm, repeat)}.wav"
    if not path.is_file():
        _write(path, assemble(tts(body, say_voice, voice.rate_wpm), tones, repeat,
                              tts(tail, say_voice, voice.rate_wpm) if tail else None))
    return path, voice.label(fallback)


def render_audio(text: str, tones, out_dir: str | Path, voice: str = "", rate_wpm: int = 0,
                 repeat: int = 2) -> Path:
    """The dispatch said by macOS `say` (render_dispatch for the engine choice). Raises AudioUnavailable."""
    return render_dispatch(text, tones, out_dir, VoiceCfg("say", voice, rate_wpm=rate_wpm), repeat)[0]
