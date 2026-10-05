"""Find RTL-SDR dongles by their exact USB serial.

rtl_fm, readsb and gr-osmosdr read an all-digit device string as an INDEX: with two dongles
plugged in, "-d 00000001" opened dongle #1 (the ADS-B stick behind a 1090 MHz filter) instead of
the Blog V4 whose serial is 00000001 (bench, 2026-10-05). So sizeup never passes a serial: right
before each start it asks librtlsdr which index has exactly that serial, and passes the index.
Only serials listed in the config are ever used.
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
    for cand in ([str(here / "librtlsdr.dylib"), str(here / "librtlsdr.so")] if here else []) + [
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
    """Current index of the dongle whose serial is exactly `serial`, or None if it isn't plugged in."""
    hits = [i for i, _p, s in devices(rtl_fm, fresh=True) if s == serial]
    return hits[0] if len(hits) == 1 else None
