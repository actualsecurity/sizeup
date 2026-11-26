"""Double Metaphone (Lawrence Philips, 2000): a word's two sound keys, primary and alternate, standard library only.

    double_metaphone("Smith")      -> ("SM0", "XMT")
    double_metaphone("Schmidt")    -> ("XMT", "SMT")

A port of the reference algorithm's rules (the C++ original), letter by letter. The keys use A (a vowel at the start),
0 (th), X (sh / ch), J, K, S, T and the other consonants. max_len: the keys' length at most (the original's 4; None:
the whole word, what sizeup/stt_correct.py uses to tell long names apart). Non-letters are skipped."""

from __future__ import annotations

import re
import unicodedata

VOWELS = "AEIOUY"


def _clean(word: str) -> str:
    w = unicodedata.normalize("NFKD", str(word or "")).upper()
    w = w.replace("Ç", "C").replace("Ñ", "N")
    return re.sub(r"[^A-Z ]", "", "".join(c for c in w if not unicodedata.combining(c)))


def double_metaphone(word: str, max_len: int | None = 4) -> tuple[str, str]:
    """(primary, alternate) sound keys of a word ("" for none)."""
    w = _clean(word).strip()
    length = len(w)
    if not length:
        return "", ""
    s = w + "     "                                  # (look-ahead past the end reads spaces)
    last = length - 1
    pri: list[str] = []
    alt: list[str] = []
    slavo = any(x in w for x in ("W", "K", "CZ", "WITZ"))

    def at(i: int) -> str:
        return s[i] if 0 <= i < len(s) else ""

    def sat(i: int, n: int, *opts: str) -> bool:
        return i >= 0 and s[i:i + n] in opts

    def vowel(i: int) -> bool:
        return 0 <= i < length and s[i] in VOWELS

    def add(p: str, a: str | None = None) -> None:
        pri.append(p)
        alt.append(p if a is None else a)

    cur = 0
    if sat(0, 2, "GN", "KN", "PN", "WR", "PS"):
        cur += 1
    if at(0) == "X":                                 # Xavier
        add("S")
        cur += 1
    while cur < length and (max_len is None or len("".join(pri)) < max_len or len("".join(alt)) < max_len):
        c = s[cur]
        if c in VOWELS:
            if cur == 0:
                add("A")
            cur += 1
        elif c == "B":
            add("P")
            cur += 2 if at(cur + 1) == "B" else 1
        elif c == "C":
            if (cur > 1 and not vowel(cur - 2) and sat(cur - 1, 3, "ACH") and at(cur + 2) != "I"
                    and (at(cur + 2) != "E" or sat(cur - 2, 6, "BACHER", "MACHER"))):
                add("K")
                cur += 2
            elif cur == 0 and sat(cur, 6, "CAESAR"):
                add("S")
                cur += 2
            elif sat(cur, 4, "CHIA"):
                add("K")
                cur += 2
            elif sat(cur, 2, "CH"):
                if cur > 0 and sat(cur, 4, "CHAE"):
                    add("K", "X")
                elif cur == 0 and (sat(cur + 1, 5, "HARAC", "HARIS") or sat(cur + 1, 3, "HOR", "HYM", "HIA", "HEM")) \
                        and not sat(0, 5, "CHORE"):
                    add("K")
                elif (sat(0, 4, "VAN ", "VON ") or sat(0, 3, "SCH") or sat(cur - 2, 6, "ORCHES", "ARCHIT", "ORCHID")
                      or sat(cur + 2, 1, "T", "S")
                      or ((sat(cur - 1, 1, "A", "O", "U", "E") or cur == 0)
                          and sat(cur + 2, 1, "L", "R", "N", "M", "B", "H", "F", "V", "W", " "))):
                    add("K")
                elif cur > 0:
                    add("K") if sat(0, 2, "MC") else add("X", "K")
                else:
                    add("X")
                cur += 2
            elif sat(cur, 2, "CZ") and not sat(cur - 2, 4, "WICZ"):
                add("S", "X")
                cur += 2
            elif sat(cur + 1, 3, "CIA"):
                add("X")
                cur += 3
            elif sat(cur, 2, "CC") and not (cur == 1 and at(0) == "M"):
                if sat(cur + 2, 1, "I", "E", "H") and not sat(cur + 2, 2, "HU"):
                    if (cur == 1 and at(cur - 1) == "A") or sat(cur - 1, 5, "UCCEE", "UCCES"):
                        add("KS")
                    else:
                        add("X")
                    cur += 3
                else:
                    add("K")
                    cur += 2
            elif sat(cur, 2, "CK", "CG", "CQ"):
                add("K")
                cur += 2
            elif sat(cur, 2, "CI", "CE", "CY"):
                add("S", "X") if sat(cur, 3, "CIO", "CIE", "CIA") else add("S")
                cur += 2
            else:
                add("K")
                if sat(cur + 1, 2, " C", " Q", " G"):
                    cur += 3
                elif sat(cur + 1, 1, "C", "K", "Q") and not sat(cur + 1, 2, "CE", "CI"):
                    cur += 2
                else:
                    cur += 1
        elif c == "D":
            if sat(cur, 2, "DG"):
                if sat(cur + 2, 1, "I", "E", "Y"):
                    add("J")
                    cur += 3
                else:
                    add("TK")
                    cur += 2
            elif sat(cur, 2, "DT", "DD"):
                add("T")
                cur += 2
            else:
                add("T")
                cur += 1
        elif c == "F":
            add("F")
            cur += 2 if at(cur + 1) == "F" else 1
        elif c == "G":
            if at(cur + 1) == "H":
                if cur > 0 and not vowel(cur - 1):
                    add("K")
                    cur += 2
                elif cur == 0:
                    add("J") if at(cur + 2) == "I" else add("K")
                    cur += 2
                elif ((cur > 1 and sat(cur - 2, 1, "B", "H", "D")) or (cur > 2 and sat(cur - 3, 1, "B", "H", "D"))
                      or (cur > 3 and sat(cur - 4, 1, "B", "H"))):
                    cur += 2
                else:
                    if cur > 2 and at(cur - 1) == "U" and sat(cur - 3, 1, "C", "G", "L", "R", "T"):
                        add("F")
                    elif cur > 0 and at(cur - 1) != "I":
                        add("K")
                    cur += 2
            elif at(cur + 1) == "N":
                if cur == 1 and vowel(0) and not slavo:
                    add("KN", "N")
                elif not sat(cur + 2, 2, "EY") and at(cur + 1) != "Y" and not slavo:
                    add("N", "KN")
                else:
                    add("KN")
                cur += 2
            elif sat(cur + 1, 2, "LI") and not slavo:
                add("KL", "L")
                cur += 2
            elif cur == 0 and (at(cur + 1) == "Y" or sat(cur + 1, 2, "ES", "EP", "EB", "EL", "EY", "IB", "IL", "IN",
                                                          "IE", "EI", "ER")):
                add("K", "J")
                cur += 2
            elif ((sat(cur + 1, 2, "ER") or at(cur + 1) == "Y") and not sat(0, 6, "DANGER", "RANGER", "MANGER")
                  and not sat(cur - 1, 1, "E", "I") and not sat(cur - 1, 3, "RGY", "OGY")):
                add("K", "J")
                cur += 2
            elif sat(cur + 1, 1, "E", "I", "Y") or sat(cur - 1, 4, "AGGI", "OGGI"):
                if sat(0, 4, "VAN ", "VON ") or sat(0, 3, "SCH") or sat(cur + 1, 2, "ET"):
                    add("K")
                elif sat(cur + 1, 4, "IER "):
                    add("J")
                else:
                    add("J", "K")
                cur += 2
            else:
                add("K")
                cur += 2 if at(cur + 1) == "G" else 1
        elif c == "H":
            if (cur == 0 or vowel(cur - 1)) and vowel(cur + 1):
                add("H")
                cur += 2
            else:
                cur += 1
        elif c == "J":
            if sat(cur, 4, "JOSE") or sat(0, 4, "SAN "):
                if (cur == 0 and at(cur + 4) == " ") or sat(0, 4, "SAN "):
                    add("H")
                else:
                    add("J", "H")
                cur += 1
                continue
            if cur == 0 and not sat(cur, 4, "JOSE"):
                add("J", "A")
            elif vowel(cur - 1) and not slavo and at(cur + 1) in ("A", "O"):
                add("J", "H")
            elif cur == last:
                add("J", "")
            elif not sat(cur + 1, 1, "L", "T", "K", "S", "N", "M", "B", "Z") and not sat(cur - 1, 1, "S", "K", "L"):
                add("J")
            cur += 2 if at(cur + 1) == "J" else 1
        elif c == "K":
            add("K")
            cur += 2 if at(cur + 1) == "K" else 1
        elif c == "L":
            if at(cur + 1) == "L":
                if ((cur == length - 3 and sat(cur - 1, 4, "ILLO", "ILLA", "ALLE"))
                        or ((sat(last - 1, 2, "AS", "OS") or sat(last, 1, "A", "O")) and sat(cur - 1, 4, "ALLE"))):
                    add("L", "")
                    cur += 2
                    continue
                cur += 2
            else:
                cur += 1
            add("L")
        elif c == "M":
            if (sat(cur - 1, 3, "UMB") and (cur + 1 == last or sat(cur + 2, 2, "ER"))) or at(cur + 1) == "M":
                cur += 2
            else:
                cur += 1
            add("M")
        elif c == "N":
            add("N")
            cur += 2 if at(cur + 1) == "N" else 1
        elif c == "P":
            if at(cur + 1) == "H":
                add("F")
                cur += 2
            else:
                add("P")
                cur += 2 if sat(cur + 1, 1, "P", "B") else 1
        elif c == "Q":
            add("K")
            cur += 2 if at(cur + 1) == "Q" else 1
        elif c == "R":
            if cur == last and not slavo and sat(cur - 2, 2, "IE") and not sat(cur - 4, 2, "ME", "MA"):
                add("", "R")
            else:
                add("R")
            cur += 2 if at(cur + 1) == "R" else 1
        elif c == "S":
            if sat(cur - 1, 3, "ISL", "YSL"):
                cur += 1
            elif cur == 0 and sat(cur, 5, "SUGAR"):
                add("X", "S")
                cur += 1
            elif sat(cur, 2, "SH"):
                add("S") if sat(cur + 1, 4, "HEIM", "HOEK", "HOLM", "HOLZ") else add("X")
                cur += 2
            elif sat(cur, 3, "SIO", "SIA") or sat(cur, 4, "SIAN"):
                add("S") if slavo else add("S", "X")
                cur += 3
            elif (cur == 0 and sat(cur + 1, 1, "M", "N", "L", "W")) or sat(cur + 1, 1, "Z"):
                add("S", "X")
                cur += 2 if sat(cur + 1, 1, "Z") else 1
            elif sat(cur, 2, "SC"):
                if at(cur + 2) == "H":
                    if sat(cur + 3, 2, "OO", "ER", "EN", "UY", "ED", "EM"):
                        add("X", "SK") if sat(cur + 3, 2, "ER", "EN") else add("SK")
                    elif cur == 0 and not vowel(3) and at(3) != "W":
                        add("X", "S")
                    else:
                        add("X")
                elif sat(cur + 2, 1, "I", "E", "Y"):
                    add("S")
                else:
                    add("SK")
                cur += 3
            else:
                if cur == last and sat(cur - 2, 2, "AI", "OI"):
                    add("", "S")
                else:
                    add("S")
                cur += 2 if sat(cur + 1, 1, "S", "Z") else 1
        elif c == "T":
            if sat(cur, 4, "TION"):
                add("X")
                cur += 3
            elif sat(cur, 3, "TIA", "TCH"):
                add("X")
                cur += 3
            elif sat(cur, 2, "TH") or sat(cur, 3, "TTH"):
                if sat(cur + 2, 2, "OM", "AM") or sat(0, 4, "VAN ", "VON ") or sat(0, 3, "SCH"):
                    add("T")
                else:
                    add("0", "T")
                cur += 2
            else:
                add("T")
                cur += 2 if sat(cur + 1, 1, "T", "D") else 1
        elif c == "V":
            add("F")
            cur += 2 if at(cur + 1) == "V" else 1
        elif c == "W":
            if sat(cur, 2, "WR"):
                add("R")
                cur += 2
                continue
            if cur == 0 and (vowel(cur + 1) or sat(cur, 2, "WH")):
                add("A", "F") if vowel(cur + 1) else add("A")
            if ((cur == last and vowel(cur - 1)) or sat(cur - 1, 5, "EWSKI", "EWSKY", "OWSKI", "OWSKY")
                    or sat(0, 3, "SCH")):
                add("", "F")
                cur += 1
            elif sat(cur, 4, "WICZ", "WITZ"):
                add("TS", "FX")
                cur += 4
            else:
                cur += 1
        elif c == "X":
            if not (cur == last and (sat(cur - 3, 3, "IAU", "EAU") or sat(cur - 2, 2, "AU", "OU"))):
                add("KS")
            cur += 2 if sat(cur + 1, 1, "C", "X") else 1
        elif c == "Z":
            if at(cur + 1) == "H":
                add("J")
                cur += 2
            else:
                if sat(cur + 1, 2, "ZO", "ZI", "ZA") or (slavo and cur > 0 and at(cur - 1) != "T"):
                    add("S", "TS")
                else:
                    add("S")
                cur += 2 if at(cur + 1) == "Z" else 1
        else:
            cur += 1
    p, a = "".join(pri), "".join(alt)
    if max_len is not None:
        p, a = p[:max_len], a[:max_len]
    return p, a
