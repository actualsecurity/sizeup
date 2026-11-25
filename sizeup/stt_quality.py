"""Is a transcript usable? One check for every engine's words (the station's whisper, a GPU worker's whisper, Amazon
Transcribe): a repetition loop, more words than the clip could hold, or one of whisper's stock phrases on noise is
UNCLEAR, never a confident transcript.

    assess(text, seconds=None) -> ""      the words can stand
                               -> reason  one of REASONS: why they can't

    repeated      the same 1-6 word phrase said REPEAT_MIN or more times in a row (a single word: REPEAT_MIN_ONE), or
                  one phrase of 2-6 words REPEAT_MIN or more times making up more than REPEAT_COVER of the text
                  (whisper's decoder loop: "a bit of a bit of a bit of ..."). A run of numbers alone is never a loop
                  ("2-2-2-2": a radio ID or a unit said digit by digit)
    too_long      more words than anyone says in the clip: over WPS_MAX words a second plus WPS_SLACK (the clip's
                  length is the transmission's, edges and rounding included)
    stock_phrase  what whisper writes on silence or noise and nobody says on the radio: a whole text in STOCK ("you",
                  "thanks for watching"), or one that says one of STOCK_IN anywhere ("please subscribe")
    no_words      only punctuation or symbols

What happens to an unclear transcript (sizeup/app.py App._screen, on the CAD of record: the hub, or all-in-one): its
status is UNCLEAR and its text "" (so nothing that reads the text acts on it: intel and its model, pre-alerts, page
parsing, incident matching, search), the engine's words kept apart (unclear_text, quality: the radio log shows
LABELS[reason] dimmed, the words on hover), its run recorded with the engine that wrote it (stt_runs status
"unclear"), and (the hub) the clip asked once of another engine when one can take it.

Pure and standard library only: the per-channel model work (#867-#871) and the STT experiment log reuse it.
"""

from __future__ import annotations

import re

UNCLEAR = "unclear"                      # the transcript's status
REASONS = ("repeated", "too_long", "stock_phrase", "no_words")
LABELS = {"repeated": "Unclear (repeated)", "too_long": "Unclear (too long)", "stock_phrase": "Unclear (stock phrase)",
          "no_words": "Unclear (no words)"}
REPEAT_MIN = 4                           # a phrase this many times in a row (no dispatch repeats a unit 4 times)
REPEAT_MIN_ONE = 6                       # ... a single word ("no no no no" is said; six in a row is a loop)
REPEAT_MAX_N = 6                         # phrases of up to this many words
REPEAT_COVER = 0.5                       # ... or one phrase, REPEAT_MIN times anywhere, over this share of the words
REPEAT_COVER_WORDS = 16                  # (only in a text this long: a short one is judged by the runs alone)
WPS_MAX = 6.0                            # words a second nobody keeps up over a radio
WPS_SLACK = 4                            # words over that the clip's edges and rounding allow
STOCK = frozenset({"you", "thanks for watching", "thank you for watching", "thanks for watching bye",
                   "thank you for watching bye", "please subscribe", "subscribe", "like and subscribe",
                   "see you in the next video", "see you next time", "well see you next time",
                   "and well see you next time", "well be right back", "music", "applause", "silence", "laughter",
                   "subtitles by the amara org community", "transcription by castingwords"})
STOCK_IN = ("thanks for watching", "thank you for watching", "please subscribe", "like and subscribe",
            "subscribe to my channel", "subtitles by", "amara org", "castingwords", "see you in the next video")
NUMBER = re.compile(r"\d+|zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|niner")


def words(text: str) -> list[str]:
    """Lower-case words: letters, digits and apostrophes ("10-2" is two words, as it is said)."""
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _numbers(phrase) -> bool:
    return all(NUMBER.fullmatch(w) for w in phrase)


def longest_run(ws: list[str]) -> tuple[int, int]:
    """(times, n): the most times one phrase of n (1..REPEAT_MAX_N) words is said in a row; numbers alone don't count."""
    best = (1, 1)
    for n in range(1, REPEAT_MAX_N + 1):
        i = 0
        while i + 2 * n <= len(ws):
            k, j = 1, i + n
            while ws[j:j + n] == ws[i:i + n]:
                k, j = k + 1, j + n
            if k > 1 and not _numbers(ws[i:i + n]) and k * n > best[0] * best[1]:
                best = (k, n)
            i = j - n + 1 if k > 1 else i + 1
    return best


def cover(ws: list[str]) -> float:
    """The largest share of the words one 2..REPEAT_MAX_N word phrase said REPEAT_MIN or more times (not overlapping,
    anywhere) makes up; numbers alone don't count."""
    best = 0.0
    for n in range(2, REPEAT_MAX_N + 1):
        seen: dict[tuple, list[int]] = {}
        for i in range(len(ws) - n + 1):
            seen.setdefault(tuple(ws[i:i + n]), []).append(i)
        for phrase, at in seen.items():
            if len(at) < REPEAT_MIN or _numbers(phrase):
                continue
            k, last = 0, -n
            for i in at:
                if i >= last + n:
                    k, last = k + 1, i
            if k >= REPEAT_MIN:
                best = max(best, k * n / len(ws))
    return best


def repeated(ws: list[str]) -> bool:
    times, n = longest_run(ws)
    if times >= (REPEAT_MIN_ONE if n == 1 else REPEAT_MIN):
        return True
    return len(ws) >= REPEAT_COVER_WORDS and cover(ws) > REPEAT_COVER


def assess(text: str, seconds: float | None = None) -> str:
    """"" when the words can stand, else the reason (REASONS). seconds: the clip's length (None or 0: not known, the
    words-a-second check left out)."""
    raw = str(text or "").strip()
    if not raw:
        return ""                        # nothing said: the engines' own no-speech, not this
    ws = words(raw)
    if not ws:
        return "no_words"
    said = " ".join(ws).replace("'", "")
    if said in STOCK or any(p in said for p in STOCK_IN):
        return "stock_phrase"
    if repeated(ws):
        return "repeated"
    if seconds is not None and seconds > 0 and len(ws) > WPS_MAX * float(seconds) + WPS_SLACK:
        return "too_long"
    return ""


def label(reason: str) -> str:
    return LABELS.get(reason, "Unclear")


def engine_of(model: str) -> str:
    """The engine behind a row's model label: "aws", "gpu-worker" or "station-whisper" (sttruns.ENGINES' names)."""
    m = str(model or "")
    if m.startswith("aws"):
        return "aws"
    if "(GPU" in m:
        return "gpu-worker"
    return "station-whisper"
