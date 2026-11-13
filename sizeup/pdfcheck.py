"""The hub's own check of a cleaned PDF before it is stored (#459, sizeup/docfiles.py recheck_pdf): a strict parser of
exactly what pypdf writes, run as its own small process so that no input can hold the hub's CPU or its GIL.

    python -I sizeup/pdfcheck.py <rlimit AS MB>    stdin: the PDF; stdout: "ok" or "bad <why>"; exit 0 either way

Standard library only, and nothing imported from sizeup: the hub runs this file by its path, with an environment of its
own, niced, under RLIMIT_CPU and RLIMIT_AS, and a wall timeout (docfiles.RECHECK_S); a timeout is a refusal.

The shape (pypdf's PdfWriter output, and nothing else):
    %PDF-x.y\\n  [%<binary bytes>\\n]
    N G obj\\n <value> \\nendobj\\n                           (heads exactly "N G obj", single spaces, no leading zeros)
    N G obj\\n <dictionary> \\nstream\\n <Length bytes> \\nendstream\\nendobj\\n
    xref\\n <first count\\n> <10-digit 5-digit n|f + EOL> ...
    trailer\\n <dictionary> \\nstartxref\\n <offset>\\n%%EOF[\\n]
No comments after the header lines, nothing between objects, nothing after %%EOF. Inside a value: a real tokenizer
(literal strings with nesting and escapes, hex strings, names with #xx, numbers, [ ] << >>, N G R, true / false / null),
so a word inside a string is a string. Checks on tokens: no name of NEVER anywhere; the trailer only /Size (the highest
object plus one), /Root, /Info, /ID; the xref entries exactly the objects' own heads and generations, from object 0;
startxref the table's offset; every N G R an object in use, the same generation. Bounds: depth, tokens, a word's length.
Every regex here is linear (no nested or overlapping repetition).
"""

from __future__ import annotations

import re
import sys

NEVER = {b"JS", b"JavaScript", b"OpenAction", b"AA", b"Launch", b"URI", b"SubmitForm", b"ImportData", b"GoToR", b"GoToE",
         b"Rendition", b"RichMedia", b"EmbeddedFile", b"EmbeddedFiles", b"XFA", b"Encrypt", b"ObjStm", b"XRef", b"Prev",
         b"XRefStm"}
TRAILER_KEYS = {b"Size", b"Root", b"Info", b"ID"}
MAX_DEPTH = 64
MAX_TOKENS = 4_000_000
MAX_WORD = 32                     # a bare word (a number, a keyword), at most: before any regex looks at it

WS = b" \t\n\r\f\0"
DELIM = b"()<>[]{}/%"
VALUE_WORDS = {b"R", b"true", b"false", b"null"}
NUMBER = re.compile(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")         # unambiguous: linear on any input
HEADER = re.compile(rb"%PDF-\d\.\d\n(?:%[^\r\n]{1,64}\n)?")
OBJ_HEAD = re.compile(rb"(0|[1-9]\d{0,9}) (0|[1-9]\d{0,4}) obj\n")
XREF_SUB = re.compile(rb"(0|[1-9]\d{0,9}) (0|[1-9]\d{0,9})\n")
XREF_ROW = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?: \n|\r\n)")
TAIL = re.compile(rb"\nstartxref\n(0|[1-9]\d{0,10})\n%%EOF\n?\Z")
HEX_BODY = re.compile(rb"[0-9A-Fa-f]*")
NAME_ESC = re.compile(rb"[0-9A-Fa-f]{2}")


class Name(bytes):
    pass


class Ref(tuple):
    pass


class Bad(Exception):
    pass


class Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.pos, self.n = data, pos, 0

    def skip(self) -> None:
        d, i = self.d, self.pos
        while i < len(d) and d[i] in WS:
            i += 1
        if i < len(d) and d[i] == 0x25:
            raise Bad("a comment")                            # none after the header lines
        self.pos = i

    def next(self) -> tuple:
        self.skip()
        self.n += 1
        if self.n > MAX_TOKENS:
            raise Bad("too many tokens")
        d, i = self.d, self.pos
        if i >= len(d):
            raise Bad("end of file")
        c = d[i]
        if c == 0x28:                                          # (
            return self._string()
        if c == 0x3C:                                          # <
            if d[i + 1:i + 2] == b"<":
                self.pos = i + 2
                return ("<<",)
            j = d.find(b">", i + 1)
            if j < 0:
                raise Bad("hex string")
            body = bytes(b for b in d[i + 1:j] if b not in WS)
            if not HEX_BODY.fullmatch(body):
                raise Bad("hex string")
            self.pos = j + 1
            return ("str", bytes.fromhex((body + b"0" * (len(body) % 2)).decode()))
        if c == 0x3E:                                          # >
            if d[i + 1:i + 2] != b">":
                raise Bad("stray >")
            self.pos = i + 2
            return (">>",)
        if c in (0x5B, 0x5D):                                  # [ ]
            self.pos = i + 1
            return (chr(c),)
        if c == 0x2F:                                          # /
            j = i + 1
            while j < len(d) and d[j] not in WS and d[j] not in DELIM:
                j += 1
            raw, out, k = d[i + 1:j], bytearray(), 0
            while k < len(raw):
                if raw[k] == 0x23:                             # #xx
                    h = raw[k + 1:k + 3]
                    if not NAME_ESC.fullmatch(h):
                        raise Bad("name escape")
                    out.append(int(h, 16))
                    k += 3
                else:
                    out.append(raw[k])
                    k += 1
            self.pos = j
            return ("name", Name(bytes(out)))
        if c in b"{})":
            raise Bad("delimiter")
        j = i
        while j < len(d) and d[j] not in WS and d[j] not in DELIM:
            j += 1
            if j - i > MAX_WORD:
                raise Bad("a word too long")                  # before any regex sees it
        word = d[i:j]
        self.pos = j
        if NUMBER.fullmatch(word):
            return ("num", float(word) if b"." in word else int(word))
        if word in VALUE_WORDS:
            return ("kw", word)
        raise Bad("an unexpected word")

    def _string(self) -> tuple:
        d, i, depth, out = self.d, self.pos + 1, 1, bytearray()
        while i < len(d):
            c = d[i]
            if c == 0x5C:
                if i + 1 >= len(d):
                    break
                out += d[i:i + 2]
                i += 2
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    self.pos = i + 1
                    return ("str", bytes(out))
            out.append(c)
            i += 1
        raise Bad("unterminated string")

    def peek(self, k: int) -> list:
        save, n = self.pos, self.n
        try:
            return [self.next() for _ in range(k)]
        except Bad:
            return []
        finally:
            self.pos, self.n = save, n


def value(lx: Lexer, tok: tuple, refs: list, depth: int = 0):
    if depth > MAX_DEPTH:
        raise Bad("too deep")
    kind = tok[0]
    if kind == "num":
        if isinstance(tok[1], int) and tok[1] >= 0:
            ahead = lx.peek(2)
            if len(ahead) == 2 and ahead[0][0] == "num" and isinstance(ahead[0][1], int) and ahead[0][1] >= 0 \
                    and ahead[1] == ("kw", b"R"):
                lx.next()
                lx.next()
                r = Ref((tok[1], ahead[0][1]))
                refs.append(r)
                return r
        return tok[1]
    if kind == "name":
        if tok[1] in NEVER:
            raise Bad("an active name")
        return tok[1]
    if kind == "str":
        return tok[1]
    if kind == "kw" and tok[1] in (b"true", b"false", b"null"):
        return tok[1]
    if kind == "[":
        out = []
        while (t := lx.next()) != ("]",):
            out.append(value(lx, t, refs, depth + 1))
        return out
    if kind == "<<":
        out = {}
        while (t := lx.next()) != (">>",):
            if t[0] != "name":
                raise Bad("a dictionary key")
            if t[1] in NEVER:
                raise Bad("an active name")
            if t[1] in out:
                raise Bad("a key twice")
            out[t[1]] = value(lx, lx.next(), refs, depth + 1)
        return out
    raise Bad("an unexpected token")


def parse(pdf: bytes) -> dict:
    h = HEADER.match(pdf)
    if h is None:
        raise Bad("the header")
    i = h.end()
    objects: dict[int, tuple] = {}
    refs: list = []
    while not pdf.startswith(b"xref\n", i):
        m = OBJ_HEAD.match(pdf, i)
        if m is None:
            raise Bad("an object head")
        num, gen = int(m.group(1)), int(m.group(2))
        if num < 1 or num in objects:
            raise Bad("an object number")
        lx = Lexer(pdf, m.end())
        v = value(lx, lx.next(), refs)
        j = lx.pos
        if pdf.startswith(b"\nstream\n", j):
            if not isinstance(v, dict) or type(v.get(Name(b"Length"))) is not int or v[Name(b"Length")] < 0:
                raise Bad("a stream's /Length")
            end = j + len(b"\nstream\n") + v[Name(b"Length")]
            if end > len(pdf) or not pdf.startswith(b"\nendstream\nendobj\n", end):
                raise Bad("a stream's end")
            i = end + len(b"\nendstream\nendobj\n")
        elif pdf.startswith(b"\nendobj\n", j):
            i = j + len(b"\nendobj\n")
        else:
            raise Bad("endobj")
        objects[num] = (gen, m.start())
    xref_at = i
    j = i + len(b"xref\n")
    named: dict[int, tuple[int, int]] = {}
    first_sub = None
    while (sub := XREF_SUB.match(pdf, j)) is not None:
        first, count = int(sub.group(1)), int(sub.group(2))
        first_sub = first if first_sub is None else first_sub
        j = sub.end()
        for k in range(count):
            row = XREF_ROW.match(pdf, j)
            if row is None:
                raise Bad("an xref row")
            num = first + k
            if row.group(3) == b"n":
                if num in named or num == 0:
                    raise Bad("an xref entry")
                named[num] = (int(row.group(2)), int(row.group(1)))
            j = row.end()
    if first_sub != 0 or not pdf.startswith(b"trailer\n", j):
        raise Bad("the xref table")
    lx = Lexer(pdf, j + len(b"trailer\n"))
    if lx.next() != ("<<",):
        raise Bad("the trailer")
    trailer = value(lx, ("<<",), refs)
    tail = TAIL.match(pdf, lx.pos)
    if tail is None or int(tail.group(1)) != xref_at:
        raise Bad("startxref / %%EOF")
    if not objects or named != objects:
        raise Bad("the xref offsets")
    if not set(trailer) <= TRAILER_KEYS or not isinstance(trailer.get(Name(b"Root")), Ref):
        raise Bad("the trailer's keys")
    if trailer.get(Name(b"Size")) != max(objects) + 1:
        raise Bad("/Size")
    live = {(n, g) for n, (g, _) in objects.items()}
    for r in refs:
        if tuple(r) not in live:
            raise Bad("a dangling reference")
    return {"objects": objects, "trailer": trailer}


def check(pdf: bytes) -> str:
    try:
        parse(pdf)
    except (Bad, ValueError, IndexError, RecursionError, MemoryError) as e:
        return f"bad {type(e).__name__}: {e}"[:200]
    return "ok"


def main() -> None:
    import os
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        mb = int(sys.argv[1]) if len(sys.argv) > 1 else 512
        resource.setrlimit(resource.RLIMIT_AS, (mb << 20, mb << 20))
    except (ImportError, ValueError, OSError):
        pass
    sys.stdout.write(check(sys.stdin.buffer.read()))


if __name__ == "__main__":
    main()
