"""Offline evaluation of the HPN ATC speech-to-text pieces (GitHub #257). Nothing here calls AWS: Amazon Transcribe
can't run locally, so what is measured is everything around it.

    python tools/atc_eval.py normalizer
        sizeup/atc_normalize.py on reference cases (CASES: transcripts heard live on HPN Tower and the shortfinal
        captures' decodes, each with its written form): word error rate of the raw text and of the normalised text
        against the written form, and how many callsigns the matcher (sizeup/atc.py attribute) finds on the
        aircraft named in the case.

    python tools/atc_eval.py preroll --captures DIR
        sizeup/radio/am.py's squelch on recorded speech: each capture's voice (DIR/**/<id>.wav, 16 kHz) put back on an
        AM carrier that comes up over RAMP_S (a weak or distant transmitter, the squelch opening late) with noise, fed
        frame by frame through CarrierSquelch with the old pre-roll (0.2 s) and the new (0.4 s): how much of the first
        0.3 s of speech each kept.

    python tools/atc_eval.py whisper --captures DIR --url http://127.0.0.1:8178/inference [--out decodes.json]
        each capture through a local whisper-server (the station's fallback model) in several ways: the audio as
        captured, as sizeup prepares it (radio_voice), with the vocabulary prompt and with the ADS-B callsigns of the
        aircraft saved with the capture (shortfinal's scope), ordered by distance (before) and by phase (after).
        Callsign accuracy: a callsign the matcher finds exactly on an aircraft of the capture's scope (confirmed), and
        prompt insertions (a scope callsign found only when prompted, its characters not heard without the prompt).
        There are no human references for these captures: callsign accuracy is measured against ADS-B, not WER.

DIR is shortfinal's captures/ (clips/ and archive/<day>/: <id>.wav + <id>.json, the JSON with "scope" and "decodes").
The captures are read, never written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sizeup import atc, atc_talkers  # noqa: E402
from sizeup.atc_normalize import fixes, normalize  # noqa: E402
from sizeup.atc_snap import snap  # noqa: E402

# (heard, written form, aircraft on ADS-B or None). Heard: speech-to-text's own words. Written form: how the same words
# are written (the words nobody can tell from the audio, "Insight", stay as heard: only spelling is scored).
CASES = [
    # HPN Tower, 2026-11-06 (Amazon Transcribe with sizeup-atc), the issue's examples
    ("Insight 34 Charlie.", "Insight 34 Charlie.", None),
    ("All right, Clearly 34 Moxie 283.", "All right, Clearly 34 Moxy 283.", "MXY283"),
    ("A White Plains tower exits at 891, cleared for the RNA 3-4 Yankee over Taish.",
     "Westchester Tower exits at 891, cleared for the RNAV Yankee 34 over TAYSH.", None),
    ("Chester Tower 560. Mike was over to do for the GPS or GPS 34.",
     "Westchester Tower 560. Mike was over TOEDO for the RNAV Yankee 34.", None),
    ("Westchester Tower citation 568 Lima X-ray 1 mile from Twarf inbound for the Rav Yankee Runway 34.",
     "Westchester Tower Citation 568LX 1 mile from TWORF inbound for the RNAV Yankee 34.", "N568LX"),
    ("Tower shield 71534.", "Tower shield 71534.", None),
    ("Westchester Towers 203 on the R9834.", "Westchester Tower 203 on the RNAV Yankee 34.", None),
    ("Fairlane 34 jet speed 168.", "Fairlane 34 Jet Speed 168.", "EJM168"),
    ("34 clear to landing such 583.", "34 cleared to land such 583.", None),
    ("Sun Country 8766", "Sun Country 8766", None),
    # shortfinal's captures (its ATC model's words), the kinds of error they show
    ("jetblue four two six eight is with you on the rn of yankee runway three four",
     "JetBlue 4268 is with you on the RNAV Yankee 34", "JBU4268"),
    ("three four climb to land flexjet five three eight", "34 cleared to land Flexjet 538", "LXJ538"),
    ("lufthansa tower jetblue four two six eight is with you on a yankee three four",
     "Westchester Tower JetBlue 4268 is with you on a Yankee 34", "JBU4268"),
    ("runway three four cleared to land wind zero eight zero", "Runway 34 cleared to land wind 080", None),
    ("good evening tower jet jet nine seventy eight is on the rn of yankee runway three four",
     "Good evening tower jet jet nine seventy eight is on the RNAV Yankee 34", None),
    ("waypoint tower cimberly five forty eight arnav three four with tango",
     "Waypoint tower cimberly five forty eight RNAV 34 with tango", None),
    # synthetic: the phrasing HPN uses, as a general model writes it
    ("november one two three alfa bravo runway one six cleared for take off",
     "N123AB runway 16 cleared for takeoff", "N123AB"),
    ("Moxie 1 42 cleared ILS runway 1-6 approach", "Moxy 142 cleared ILS 16 approach", "MXY142"),
    ("Execjet 521, cross TWARF at or above 2000, cleared R nav Yankee 34",
     "Execjet 521, cross TWORF at or above 2000, cleared RNAV Yankee 34", "EJA521"),
    ("Cessna 4 5 6 Papa Romeo, line up in wait runway 3-4", "Cessna 456PR, line up and wait runway 34", "N456PR"),
    ("Jet blue 12 34, contact New York departure, good day", "JetBlue 1234, contact New York departure, good day",
     "JBU1234"),
    ("Republic 4721 descend and maintain 3000 until established, cleared GPS 16",
     "Republic 4721 descend and maintain 3000 until established, cleared RNAV Yankee 16", "RPA4721"),
    ("taxi via alfa bravo hold short of runway one six", "Taxi via alfa bravo hold short of runway 16", None),
]


def words(t: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (t or "").lower().replace("x-ray", "xray"))


def wer(hyp: str, ref: str) -> tuple[int, int]:
    """(word edits, reference words)."""
    h, r = words(hyp), words(ref)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


def plane(cs: str) -> dict:
    return {"hex": "e" + cs.lower(), "flight": cs, "reg": cs if cs.startswith("N") else "", "dist": 3.0}


def run_normalizer() -> dict:
    idx = atc_talkers.load_index()
    e_raw = e_norm = n = 0
    cs_raw = cs_norm = cs_snap = cs_n = 0
    rows = []
    for heard, ref, cs in CASES:
        a, _ = wer(heard, ref)
        b, k = wer(normalize(heard), ref)
        e_raw, e_norm, n = e_raw + a, e_norm + b, n + k
        if cs:
            planes = [plane(cs), plane("DAL1"), plane("N9ZZ")]
            ok = lambda t: (lambda x: bool(x and x.hex == "e" + cs.lower() and x.confidence >= 0.7))(atc.attribute(t, idx, planes))
            cs_n += 1
            cs_raw += ok(heard)
            cs_norm += ok(normalize(heard))
            cs_snap += ok(snap(normalize(heard), planes)[0])
        rows.append({"heard": heard, "normalised": normalize(heard), "ref": ref, "wer_raw": a / k, "wer_norm": b / k})
    out = {"cases": len(CASES), "words": n, "wer_raw": round(e_raw / n, 3), "wer_normalised": round(e_norm / n, 3),
           "callsign_cases": cs_n, "callsign_raw": cs_raw, "callsign_normalised": cs_norm, "callsign_snapped": cs_snap,
           "rows": rows}
    print(f"normaliser: {len(CASES)} cases, {n} reference words: WER raw {e_raw / n:.1%} -> normalised {e_norm / n:.1%}")
    print(f"  callsigns found (conf >= 0.7) on ADS-B: raw {cs_raw}/{cs_n} -> normalised {cs_norm}/{cs_n} "
          f"-> normalised + snapped {cs_snap}/{cs_n}")
    for r in rows:
        if r["wer_norm"] > 0:
            print(f"  left: {r['normalised']!r} (ref {r['ref']!r})")
    return out


# -- captures ---------------------------------------------------------------------------------------------------------


def captures(root: Path) -> list[tuple[Path, dict]]:
    out = []
    for meta in sorted(root.glob("**/*.json")):
        wav = meta.with_suffix(".wav")
        if not wav.is_file():
            continue
        try:
            d = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("id"):
            out.append((wav, d))
    return out


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0


LATE_S = (0.0, 0.1, 0.2, 0.3, 0.4)    # how long after the voice starts the squelch opens (a carrier under the open level)


def run_preroll(root: Path) -> dict:
    """The first 0.3 s of speech kept, old pre-roll (0.2 s) and new, for a squelch that opens LATE_S after the voice
    starts: the carrier sits just under the open level (+3.5 dB) for that long (a weak or fading start), then comes up."""
    from sizeup.radio import am
    from sizeup.radio.fm import FRAME, RATE
    rng = np.random.default_rng(257)
    clips = captures(root)
    res: dict = {}
    for late in LATE_S:
        for pre in (4, am.PREROLL_FRAMES):
            kept = total = 0.0
            for wav, _d in clips:
                voice = read_wav(wav)
                voice = voice / (np.max(np.abs(voice)) or 1.0)
                lead = int(6 * RATE) + int(rng.integers(0, FRAME))   # the voice starts anywhere in a frame
                n = len(voice)
                env_len = lead + n + int(2 * RATE)
                carrier = np.zeros(env_len)
                carrier[lead:lead + n] = 0.012                       # well over the open level ...
                carrier[lead:lead + int(late * RATE)] = 0.0035       # ... after `late` s just under it
                mod = np.zeros(env_len)
                mod[lead:lead + n] = 0.6 * voice
                env = 0.01 + carrier * (1 + mod) + 0.0004 * rng.standard_normal(env_len)
                old = am.PREROLL_FRAMES
                am.PREROLL_FRAMES = pre
                now = [0.0]
                try:
                    sq = am.CarrierSquelch(3.5, clock=lambda: now[0])   # the stream's own time: the frame's start
                    got = None
                    for k in range(0, env_len - FRAME + 1, FRAME):
                        now[0] = k / RATE
                        s = sq.feed(env[k:k + FRAME])
                        if s is not None and got is None:
                            got = s
                finally:
                    am.PREROLL_FRAMES = old
                if got is None:
                    continue
                seg_start = int(round(got.start * RATE))             # where its audio starts in the stream
                e = mod[lead:lead + int(0.3 * RATE)] ** 2
                kept += e[max(0, seg_start - lead):].sum()
                total += e.sum()
            res[f"late {late:.1f} s, pre-roll {pre * FRAME / RATE:.1f} s"] = round(kept / total, 3) if total else None
    print(f"pre-roll on {len(clips)} recorded voices: share of the first 0.3 s of speech kept")
    for k, v in res.items():
        print(f"  {k}: {v:.1%}" if v is not None else f"  {k}: no segment")
    return res


def run_whisper(root: Path, url: str, out: Path | None) -> dict:
    from sizeup.stt import WhisperClient, radio_voice
    client = WhisperClient(url, timeout_s=120)
    idx = atc_talkers.load_index()
    tel = atc.telephony_words()
    v = atc.vocabulary()
    v1 = ("Westchester Tower, Westchester Ground, New York Approach. Runway one six, runway three four, runway one one, "
          "runway two niner. Taxiway alfa, bravo, charlie, delta, echo, foxtrot, golf, hotel, juliett, kilo. Cleared to "
          "land, cleared for takeoff, line up and wait, hold short, go around, souls on board, fuel remaining, declaring "
          "an emergency, roll the trucks.")

    def by_dist(base, scope):
        s = [dict(a, phase="") for a in scope]
        return atc.whisper_prompt(base, s, tel)

    clips = [(w, d) for w, d in captures(root) if d.get("scope")]
    variants = {
        "captured, no prompt": (lambda a: a, lambda d: ""),
        "radio_voice, no prompt": (lambda a: radio_voice(a, deemph=False, nr=False), lambda d: ""),
        "radio_voice, prompt v1 + callsigns by distance (before)": (lambda a: radio_voice(a, deemph=False, nr=False),
                                                                    lambda d: by_dist(v1, d["scope"])),
        "radio_voice, prompt v2 + callsigns by phase (after)": (lambda a: radio_voice(a, deemph=False, nr=False),
                                                                lambda d: atc.whisper_prompt(v["prompt"], d["scope"], tel)),
    }
    dec: dict = json.loads(out.read_text()) if out and out.is_file() else {}
    for wav, d in clips:
        audio = read_wav(wav)
        for name, (prep, prompt) in variants.items():
            key = f"{d['id']}|{name}"
            if key in dec:
                continue
            t0 = time.time()
            try:
                text, _c = client.transcribe(prep(audio), prompt(d))
            except Exception as e:  # noqa: BLE001
                text = f"[error {type(e).__name__}]"
            dec[key] = {"text": text, "s": round(time.time() - t0, 2)}
            if out:
                out.write_text(json.dumps(dec, indent=1))
    res = {}
    fixset = set(fixes())
    for name in variants:
        r = {"clips": len(clips), "callsign_on_adsb": 0, "callsign_on_adsb_snapped": 0, "fix_words": 0, "no_text": 0}
        for _w, d in clips:
            text = dec[f"{d['id']}|{name}"]["text"]
            if not words(text):
                r["no_text"] += 1
                continue
            r["fix_words"] += any(w.upper() in fixset for w in re.findall(r"[A-Za-z]+", text))   # prompt words on noise
            a = atc.attribute(normalize(text), idx, d["scope"])
            r["callsign_on_adsb"] += bool(a and a.hex and a.confidence >= 0.7)
            a = atc.attribute(snap(normalize(text), d["scope"])[0], idx, d["scope"])
            r["callsign_on_adsb_snapped"] += bool(a and a.hex and a.confidence >= 0.7)
        res[name] = r
        print(f"whisper, {name}: callsign on the capture's ADS-B {r['callsign_on_adsb']}/{len(clips)} "
              f"(snapped {r['callsign_on_adsb_snapped']}), fix names written {r['fix_words']}, no text {r['no_text']}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", choices=("normalizer", "preroll", "whisper"))
    ap.add_argument("--captures", type=Path)
    ap.add_argument("--url", default="http://127.0.0.1:8178/inference")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.what == "normalizer":
        run_normalizer()
    elif not a.captures:
        ap.error("--captures DIR")
    elif a.what == "preroll":
        run_preroll(a.captures)
    else:
        run_whisper(a.captures, a.url, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
