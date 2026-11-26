"""Is a transcript usable? One check for every engine's words (the station's whisper, a GPU worker's whisper, Amazon
Transcribe): a repetition loop, more words than the clip could hold, or one of whisper's stock phrases on noise is
UNCLEAR, never a confident transcript.

    assess(text, seconds=None) -> ""      the words can stand
                               -> reason  one of REASONS: why they can't

    repeated      whisper's decoder loop ("out of the out of the out of the ..."): the same 1-12 word phrase LOOP_MIN or
                  more times in a row (a single word: LOOP_MIN_ONE), or REPEAT_MIN or more times in a row (a single word:
                  REPEAT_MIN_ONE) faster than anyone repeats a call (over LOOP_WPS words a second), or one phrase of 2-12
                  words LOOP_MIN or more times making up more than REPEAT_COVER of the text. Radio repeats on purpose
                  ("emergency traffic" x4, "get out" x4, a unit called four times, a page read again): only a loop goes
                  past that. A run of numbers alone is never a loop ("2-2-2-2": a radio ID said digit by digit)
    too_long      more words than anyone says in the clip: over WPS_MAX words a second plus WPS_SLACK (the clip's
                  length is the transmission's, edges and rounding included)
    stock_phrase  what whisper writes on silence or noise and nobody says on the radio: a whole text in STOCK ("you",
                  "thanks for watching"), or one that says one of STOCK_IN anywhere ("please subscribe")
    no_words      only punctuation or symbols
    not_english   whisper wrote another language (the radio here is English): the engine said so (language, from its
                  verbose_json, when it is there), or over FOREIGN_LETTERS of the letters are ones no Spanish or
                  English name uses (NORDIC: þ ð æ ø å ö œ ß), or, in FOREIGN_MIN_WORDS words or more, over FOREIGN_SHARE
                  are other languages' function words (FOREIGN: none a local place name is made of: no "el", "los",
                  "del", "las") and under ENGLISH_SHARE are English ones. Accented place names and surnames ("José
                  Peña", "El Camino Del Rio"), unit IDs and numbers in English stay English

Safety words (SAFETY: "mayday", "evacuate", "firefighter down" ...) are said again and again on purpose, often fast: a
text with one is never judged by the fast-repeat rule (REPEAT_MIN in a row). A real loop (LOOP_MIN in a row, the
cover rule, too many words for the clip) is unclear whatever it says: the alarms read an unclear row's words anyway.

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
REASONS = ("repeated", "too_long", "stock_phrase", "no_words", "not_english")
LABELS = {"repeated": "Unclear (repeated)", "too_long": "Unclear (too long)", "stock_phrase": "Unclear (stock phrase)",
          "no_words": "Unclear (no words)", "not_english": "Unclear (not English)"}
LOOP_MIN = 8                             # a phrase this many times in a row: a loop, whatever the clip
LOOP_MIN_ONE = 12                        # ... a single word
REPEAT_MIN = 4                           # ... this many (a single word: REPEAT_MIN_ONE) only faster than LOOP_WPS
REPEAT_MIN_ONE = 6
LOOP_WPS = 6.0                           # (a call repeated on purpose, digits and all, is said slower than this; a loop
                                         # runs at 100+ words a second)
REPEAT_MAX_N = 12                        # phrases of up to this many words
REPEAT_COVER = 0.5                       # ... or one phrase, LOOP_MIN times anywhere, over this share of the words
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
SAFETY = re.compile(r"\b(mayday|emergency|evacuat\w*|help|get out|priority|urgent|firefighter down|man down|"
                    r"officer down|shots? fired|trapped|collapse\w*|abandon)\b")
FOREIGN_MIN_WORDS = 8                    # (a shorter text is never judged by its words)
NORDIC = frozenset("þðæøåöœß")            # letters no Spanish (or English) name uses
FOREIGN_LETTERS = 0.03                   # ... as a share of all letters (and two at least)
FOREIGN_SHARE = 0.25                     # other languages' function words, as a share of the words ...
ENGLISH_SHARE = 0.15                     # ... with English function words under this share
FOREIGN = frozenset({
    # Spanish
    "que", "por", "para", "una", "pero", "muy", "también", "tambien", "usted", "nosotros",
    "hay", "yo", "señor", "esta", "está", "estoy", "aquí", "aqui", "ahora", "bueno", "gracias", "porque", "cuando",
    # German
    "der", "das", "und", "ist", "nicht", "ich", "wir", "ein", "eine", "mit", "auf", "für", "fur", "auch", "sehr",
    "nein", "wie", "aber", "oder", "noch", "jetzt", "hier", "bitte", "danke",
    # French
    "les", "des", "et", "est", "je", "vous", "nous", "pas", "avec", "pour", "dans", "qui", "oui", "très", "mais",
    "c'est", "merci", "bien",
    # Portuguese
    "não", "você", "isso", "muito", "obrigado", "então", "entao",
    # Icelandic
    "og", "að", "ekki", "það", "ég", "við", "hún", "sem", "með", "þetta", "já", "nei", "eða",
    # ... as speech-to-text writes it without its letters
    "eg", "thad", "thetta", "hann", "hvar", "hvad", "mjog", "nuna", "fyrir", "eftir", "takk", "vid"})
ENGLISH = frozenset({"the", "and", "of", "to", "is", "a", "on", "at", "you", "we", "it", "that", "for", "with", "are",
                     "be", "this", "have", "i", "he", "she", "they", "out", "up", "go", "your", "from", "copy", "can",
                     "will", "there", "what", "just", "get", "in", "my", "our"})
NUMBER = re.compile(r"\d+|zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|niner")


def words(text: str) -> list[str]:
    """Lower-case words: letters (any alphabet), digits and apostrophes ("10-4" is two words, as it is said)."""
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", str(text or "").lower())


def not_english(text: str, ws: list[str], language: str | None = None) -> bool:
    """Another language: the engine's own detection (any but "en" / "english"), else the letters and words."""
    lang = str(language or "").strip().lower()
    if lang and lang not in ("en", "english") and not lang.startswith("en-"):
        return True
    letters = [c for c in str(text).lower() if c.isalpha()]
    nordic = sum(1 for c in letters if c in NORDIC)
    if nordic >= 2 and nordic / len(letters) > FOREIGN_LETTERS:
        return True
    if len(ws) < FOREIGN_MIN_WORDS:
        return False
    foreign = sum(1 for w in ws if w in FOREIGN) / len(ws)
    english = sum(1 for w in ws if w in ENGLISH) / len(ws)
    return foreign > FOREIGN_SHARE and english < ENGLISH_SHARE


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
    """The largest share of the words one 2..REPEAT_MAX_N word phrase said LOOP_MIN or more times (not overlapping,
    anywhere) makes up; numbers alone don't count."""
    best = 0.0
    for n in range(2, REPEAT_MAX_N + 1):
        seen: dict[tuple, list[int]] = {}
        for i in range(len(ws) - n + 1):
            seen.setdefault(tuple(ws[i:i + n]), []).append(i)
        for phrase, at in seen.items():
            if len(at) < LOOP_MIN or _numbers(phrase):
                continue
            k, last = 0, -n
            for i in at:
                if i >= last + n:
                    k, last = k + 1, i
            if k >= LOOP_MIN:
                best = max(best, k * n / len(ws))
    return best


def repeated(ws: list[str], seconds: float | None = None, safety: bool = False) -> bool:
    """A loop. safety: the text says a safety word (never judged by the fast-repeat rule alone)."""
    times, n = longest_run(ws)
    if times >= (LOOP_MIN_ONE if n == 1 else LOOP_MIN):
        return True
    fast = not safety and seconds is not None and seconds > 0 and len(ws) > LOOP_WPS * float(seconds)
    if fast and times >= (REPEAT_MIN_ONE if n == 1 else REPEAT_MIN):
        return True
    return len(ws) >= REPEAT_COVER_WORDS and cover(ws) > REPEAT_COVER


def assess(text: str, seconds: float | None = None, language: str | None = None) -> str:
    """"" when the words can stand, else the reason (REASONS). seconds: the clip's length (None or 0: not known, the
    words-a-second check left out). language: the engine's detected language, when it gave one."""
    raw = str(text or "").strip()
    if not raw:
        return ""                        # nothing said: the engines' own no-speech, not this
    ws = words(raw)
    if not ws:
        return "no_words"
    said = " ".join(ws).replace("'", "")
    if said in STOCK or any(p in said for p in STOCK_IN):
        return "stock_phrase"
    if repeated(ws, seconds, bool(SAFETY.search(said))):
        return "repeated"
    if not_english(raw, ws, language):
        return "not_english"
    if seconds is not None and seconds > 0 and len(ws) > WPS_MAX * float(seconds) + WPS_SLACK:
        return "too_long"
    return ""


REQUEUE = ("repeated", "too_long", "not_english")    # worth another engine's try; a stock phrase on noise: no speech


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
