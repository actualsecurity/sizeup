"""Find RTL-SDR dongles by their exact USB serial.

rtl_fm, readsb and gr-osmosdr read an all-digit device string as an INDEX: with two dongles
plugged in, "-d 00000001" opened dongle #1 (the ADS-B stick behind a 1090 MHz filter) instead of
the Blog V4 whose serial is 00000001 (bench, 2026-10-05). So sizeup never passes a serial: right
before each start it asks librtlsdr which index has exactly that serial, and passes the index.
Only serials listed in the config are ever used.

Two dongles with one serial (GitHub #242: a Nooelec NESDR SMArt plugged in with its factory serial 00000001, the same
as the station's "V3 #2") can't be told apart by it: lookup() then gives no index and says why, so nothing opens
either of them by that serial, and duplicates() names them for the station's health (a critical alert) until one is
renamed with `sizeup-station sdr-serial` (sizeup/radio/sdrserial.py).
"""

from __future__ import annotations

import ctypes
import threading
import time
from pathlib import Path

CACHE_S = 3.0

_lock = threading.Lock()
_lib: ctypes.CDLL | None = None
_lib_tried = False
_cache: tuple[float, list[tuple[int, str, str]]] = (0.0, [])


def _load(rtl_fm: str = "") -> ctypes.CDLL | None:
    global _lib, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    here = Path(rtl_fm).resolve().parent if rtl_fm else None
    # next to rtl_fm, or in its prefix's lib/ (the Linux station: /opt/sizeup/rtl-sdr/{bin,lib}, install-linux.sh)
    for cand in ([str(here / "librtlsdr.dylib"), str(here / "librtlsdr.so"), str(here.parent / "lib" / "librtlsdr.so.0")]
                 if here else []) + [
            "/opt/homebrew/lib/librtlsdr.dylib", "/usr/local/lib/librtlsdr.dylib", "librtlsdr.dylib", "librtlsdr.so.0"]:
        try:
            _lib = ctypes.CDLL(cand)
            return _lib
        except OSError:
            continue
    return None


def devices(rtl_fm: str = "", fresh: bool = False) -> list[tuple[int, str, str]]:
    """[(index, product, serial)] of the dongles plugged in now (cached a few seconds)."""
    global _cache
    with _lock:
        if not fresh and time.monotonic() - _cache[0] < CACHE_S:
            return list(_cache[1])
        lib = _load(rtl_fm)
        out: list[tuple[int, str, str]] = []
        if lib is not None:
            try:
                for i in range(int(lib.rtlsdr_get_device_count())):
                    m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
                    if lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0:
                        out.append((i, p.value.decode(errors="replace"), s.value.decode(errors="replace")))
            except Exception:
                out = []
        _cache = (time.monotonic(), out)
        return list(out)


def resolve(serial: str, rtl_fm: str = "") -> int | None:
    """Current index of the dongle whose serial is exactly `serial`, or None if it isn't plugged in (or is twice)."""
    return lookup(serial, rtl_fm)[0]


_WORDS = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}


def _and(xs: list) -> str:
    xs = [str(x) for x in xs]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def duplicates(devs: list[tuple[int, str, str]]) -> dict[str, list[tuple[int, str]]]:
    """{serial: [(index, product), ...]} for every serial two or more of these dongles share (plug order)."""
    by: dict[str, list[tuple[int, str]]] = {}
    for i, p, s in devs:
        by.setdefault(s, []).append((i, p))
    return {s: v for s, v in by.items() if len(v) > 1}


def rename_index(serial: str, entries: list[tuple[int, str]], known: dict[str, str] | None = None) -> int:
    """The index to suggest renaming: the one whose product isn't the one this serial had when it was the only one
    (known: {serial: product}, sizeup/app.py's sdr_products.json), i.e. the newcomer; else the last one."""
    was = (known or {}).get(serial)
    if was:
        new = [i for i, p in entries if p != was]
        if len(new) == 1:
            return new[0]
    return entries[-1][0]


def duplicate_text(serial: str, entries: list[tuple[int, str]], known: dict[str, str] | None = None) -> str:
    """The alert's text: "Two dongles share serial 00000001 (indexes 1 and 4): sizeup can't tell them apart. Fix:
    sizeup-station sdr-serial 4 <new-serial> ..." (the index of the one to rename, with its product)."""
    n = len(entries)
    i = rename_index(serial, entries, known)
    prod = dict(entries).get(i) or ""
    return (f"{_WORDS.get(n, str(n))} dongles share serial {serial or '(none)'} (indexes {_and([x for x, _p in entries])}): "
            f"sizeup can't tell them apart. Fix: sizeup-station sdr-serial {i} <new-serial>"
            + (f" (renames #{i}, {prod})" if prod else "")
            + f"; nothing opens {serial or 'it'} until then")


def ambiguity(serial: str, devs: list[tuple[int, str, str]], known: dict[str, str] | None = None) -> str:
    """Why `serial` can't be opened now (it is on two or more of these dongles), or ""."""
    hits = [(i, p) for i, p, s in devs if s == serial]
    if len(hits) < 2:
        return ""
    return (f"serial {serial} is on {len(hits)} dongles (indexes {_and([i for i, _p in hits])}): not opened until one is "
            f"renamed (sizeup-station sdr-serial {rename_index(serial, hits, known)} <new-serial>)")


def lookup(serial: str, rtl_fm: str = "") -> tuple[int | None, str]:
    """(current index of the one dongle whose serial is exactly `serial`, why not): (None, "...") when it isn't
    plugged in, or when two or more dongles have it (ambiguity(): never a guess between them)."""
    devs = devices(rtl_fm, fresh=True)
    hits = [i for i, _p, s in devs if s == serial]
    if len(hits) == 1:
        return hits[0], ""
    return None, ambiguity(serial, devs) if hits else ""
