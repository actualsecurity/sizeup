"""A camera's on-image overlay (#320 follow-up): the black bar across the top of a NYSDOT still that names the camera
("CAM-<road><dir>-<place>-mp<mile>" on the left, the time on the right), as a signature to compare two records by.

Why: the CARS-Hub lists some cameras twice under two ids with two stream names, and with names and mileposts that
disagree (one camera listed "at" an exit and "north of" it, 0.4 mile apart). Nothing in the inventory, the stream or the
still's address ties them together; the overlay burned into the picture does. Read 2026-11-08 on every still in our
area: a pair of one camera's two records gave the same name to the pixel, at two fetches half an hour apart; two
names one character apart differ in a few pixels only, so nothing short of the same pixels is the same name.

overlay(body): the bar's name, white text on black, as bits: (width, bar height, bits) of a PNG still whose top rows
look like such a bar (dark mostly, some text), else None (a JPEG, no bar, a placeholder, too big to read cheaply). The
name is the bar's first run of text: from its first lit column to the first gap wider than NAME_GAP bar heights
(the date and the time come after such a gap; a name's own spaces are a character wide). The rest of the bar is
blanked, so a name of any length is kept whole and the time never counts. A bar whose name runs into the time without
such a gap gives a different pattern at each still: it never matches (the safe way to fail).
same(a, b): the same image size and bar, and not one bit apart, with at least MIN_INK lit pixels in the name.

Only the top rows are decoded (zlib in pieces, the PNG row filters undone here): no imaging library.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

BAR = 0.06              # the bar: this share of the image's height from the top (at least BAR_MIN rows)
BAR_MIN = 12
NAME_GAP = 1.5         # the name ends at the first gap this many bar heights wide (the date / time follow it)
NAME_MIN = 0.15         # a name at least this share of the width ...
MIN_INK = 150           # ... with at least this many lit pixels
INK = 0.03              # a bar: at least this share of text (light) ...
DARK = 0.5              # ... on at least this share of dark
MAX_SIDE = 1920         # bigger stills aren't read (the row filters are undone in Python: the cost grows with the size)
MAX_BYTES = 200_000     # nor a bar of more than this many bytes (rows x width x channels)


def _chunks(body: bytes):
    i = 8
    while i + 8 <= len(body):
        n, = struct.unpack(">I", body[i:i + 4])
        yield body[i + 4:i + 8], body[i + 8:i + 8 + n]
        i += 12 + n


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def top_rows(body: bytes, share: float = BAR, least: int = BAR_MIN) -> np.ndarray | None:
    """The top rows of an 8-bit RGB / RGBA / grey PNG as grey levels (rows x width), None when it isn't one."""
    if body[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        hdr = None
        dec = zlib.decompressobj()
        raw = b""
        for kind, data in _chunks(body):
            if kind == b"IHDR":
                hdr = struct.unpack(">IIBBBBB", data[:13])
                w, h, depth, ctype, _, _, interlace = hdr
                if depth != 8 or ctype not in (0, 2, 6) or interlace or not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
                    return None
                ch = {0: 1, 2: 3, 6: 4}[ctype]
                rows = min(h, max(least, int(h * share)))
                need = rows * (w * ch + 1)
                if rows * w * ch > MAX_BYTES:
                    return None
            elif kind == b"IDAT" and hdr is not None:
                raw += dec.decompress(data, need - len(raw))
                if len(raw) >= need:
                    break
            elif kind == b"IEND":
                break
        if hdr is None or len(raw) < need:
            return None
    except (zlib.error, struct.error, ValueError):
        return None
    stride = w * ch
    out = np.zeros((rows, stride), np.uint8)
    prev = bytearray(stride)
    for y in range(rows):
        f = raw[y * (stride + 1)]
        line = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        cur = bytearray(line)
        if f == 1:
            for x in range(ch, stride):
                cur[x] = (cur[x] + cur[x - ch]) & 255
        elif f == 2:
            cur = bytearray(((np.frombuffer(line, np.uint8).astype(np.uint16) + np.frombuffer(bytes(prev), np.uint8)) & 255)
                            .astype(np.uint8).tobytes())
        elif f == 3:
            for x in range(stride):
                cur[x] = (cur[x] + ((cur[x - ch] if x >= ch else 0) + prev[x]) // 2) & 255
        elif f == 4:
            for x in range(stride):
                cur[x] = (cur[x] + _paeth(cur[x - ch] if x >= ch else 0, prev[x], prev[x - ch] if x >= ch else 0)) & 255
        elif f != 0:
            return None
        out[y] = np.frombuffer(bytes(cur), np.uint8)
        prev = cur
    return out.reshape(rows, w, ch)[:, :, :3].mean(axis=2)


def name_span(light: np.ndarray) -> tuple[int, int] | None:
    """The bar's name: (first column, column after its last) of the first run of lit columns ended by a gap wider than
    NAME_GAP bar heights (or the bar's edge)."""
    xs = np.flatnonzero(light.any(axis=0))
    if not len(xs):
        return None
    gap = NAME_GAP * light.shape[0]
    wide = np.flatnonzero(np.diff(xs) - 1 >= gap)
    end = xs[wide[0]] if len(wide) else xs[-1]
    return int(xs[0]), int(end) + 1


def overlay(body: bytes) -> tuple[int, int, bytes] | None:
    """The still's overlay bar, its name as bits (the rest blanked): (image width, bar height, packed bits), None when
    there's none."""
    grey = top_rows(body)
    if grey is None:
        return None
    rows, w = grey.shape
    light = grey > 128
    if light.mean() < INK or (grey < 64).mean() < DARK:
        return None
    span = name_span(light)
    if span is None or span[1] - span[0] < NAME_MIN * w:
        return None
    name = np.zeros_like(light)
    name[:, span[0]:span[1]] = light[:, span[0]:span[1]]
    if name.sum() < MIN_INK:
        return None
    return w, rows, np.packbits(name).tobytes()


def same(a: tuple[int, int, bytes] | None, b: tuple[int, int, bytes] | None) -> bool:
    """One overlay: the same image size and bar, and the same name to the pixel (with MIN_INK of it lit)."""
    if a is None or b is None or a[:2] != b[:2] or len(a[2]) != len(b[2]) or a[2] != b[2]:
        return False
    return int(np.unpackbits(np.frombuffer(a[2], np.uint8)).sum()) >= MIN_INK
