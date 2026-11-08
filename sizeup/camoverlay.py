"""A camera's on-image overlay (#320 follow-up): the black bar across the top of a NYSDOT still that names the camera
("CAM-<road><dir>-<place>-mp<mile>" on the left, the time on the right), as a signature to compare two records by.

Why: the CARS-Hub lists some cameras twice under two ids with two stream names, and with names and mileposts that
disagree (one camera listed "at" an exit and "north of" it, 0.4 mile apart). Nothing in the inventory, the stream or the
still's address ties them together; the overlay burned into the picture does. Read 2026-11-08 on every still in our
area: a pair of one camera's two records gave the same bar to the pixel (stills taken seconds apart, the time left
out); any two different cameras' bars at one size differed in 7 % of their pixels or more.

overlay(body): the bar's left 60 % (the name, not the time), white text on black, as bits: (width, height, bits) of a
PNG still whose top rows look like such a bar (dark mostly, some text), else None (a JPEG, no bar, a placeholder).
same(a, b): two of them at the same image size differing in under OVERLAY_DIFF of their bits.

Only the top rows are decoded (zlib in pieces, the PNG row filters undone here): no imaging library.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

BAR = 0.06              # the bar: this share of the image's height from the top (at least BAR_MIN rows)
BAR_MIN = 12
NAME = 0.6              # its left part: the camera's name (the time is on the right)
INK = 0.05              # a bar: at least this share of text (light) ...
DARK = 0.5              # ... on at least this share of dark
OVERLAY_DIFF = 0.01     # two bars differing in fewer of their bits than this: the same overlay
MAX_SIDE = 4096


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


def overlay(body: bytes) -> tuple[int, int, bytes] | None:
    """The still's overlay bar, its name part as bits: (image width, bar height, packed bits), None when there's none."""
    grey = top_rows(body)
    if grey is None:
        return None
    rows, w = grey.shape
    name = grey[:, :int(w * NAME)]
    light = name > 128
    if light.mean() < INK or (name < 64).mean() < DARK:
        return None
    return w, rows, np.packbits(light).tobytes()


def same(a: tuple[int, int, bytes] | None, b: tuple[int, int, bytes] | None) -> bool:
    """One overlay: the same image size and bar, under OVERLAY_DIFF of the bits apart."""
    if a is None or b is None or a[:2] != b[:2] or len(a[2]) != len(b[2]):
        return False
    x = np.unpackbits(np.frombuffer(a[2], np.uint8) ^ np.frombuffer(b[2], np.uint8))
    return x.mean() < OVERLAY_DIFF
