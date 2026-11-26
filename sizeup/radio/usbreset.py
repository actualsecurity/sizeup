"""One USB reset of an RTL-SDR, by its serial (#703: a dongle that opens but streams no audio).

A dongle can go bad in a way USB doesn't see: rtl_fm opens it without an error, then not a byte of audio comes
("rtl_fm stalled"). Before the app counts it as failed (sizeup/app.py, NOAUDIO_*), it tries one reset here: the
kernel's USBDEVFS_RESET on the device node, as `usbreset` does. The receiver's rtl_fm is down between its retries
then, so nothing of ours has the dongle open.

Native Linux only. Under WSL2 the dongle comes through usbipd (USB/IP): a reset there is the virtual host
controller's, never reaches the dongle, and can make usbipd drop the device until its re-attach task attaches it
again. So no reset there: reset() says why, and the dongle is counted failed at once.

ident() is where the dongle is on USB now (bus, device number): a replug gives it a new device number, which ends a
"no audio" (the app's _noaudio). Both look in /sys only (stdlib, nothing opened but the one node)."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

SYS = Path("/sys/bus/usb/devices")
VENDORS = ("0bda",)                          # Realtek: every RTL2832U dongle (2838, 2832)
USBDEVFS_RESET = (ord("U") << 8) | 20        # _IO('U', 20)


def in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def _read(d: Path, name: str) -> str:
    try:
        return (d / name).read_text().strip()
    except (OSError, ValueError):            # (ValueError: UnicodeDecodeError, a serial that is no UTF-8)
        return ""


def ident(serial: str, root: Path = SYS) -> tuple[int, int] | None:
    """(bus, device number) of the one dongle with this serial; None when none, two, or no /sys (not Linux)."""
    found = []
    try:
        dirs = list(root.iterdir())
    except OSError:
        return None
    for d in dirs:
        if ":" in d.name or _read(d, "idVendor") not in VENDORS or _read(d, "serial") != serial:
            continue
        try:
            found.append((int(_read(d, "busnum")), int(_read(d, "devnum"))))
        except ValueError:
            continue
    return found[0] if len(found) == 1 else None


def ports(root: Path = SYS) -> dict[str, str]:
    """{serial: its USB port path ("1-1.2": bus 1, port 1, then 2)} of the RTL-SDR dongles in /sys now; {} without /sys
    (not Linux). A serial on two dongles is left out. (sizeup/radio/replace.py: a new dongle in the old one's port.)"""
    found: dict[str, list[str]] = {}
    try:
        dirs = list(root.iterdir())
    except OSError:
        return {}
    for d in dirs:
        if ":" in d.name or _read(d, "idVendor") not in VENDORS:
            continue
        s = _read(d, "serial")
        if s:
            found.setdefault(s, []).append(d.name)
    return {s: p[0] for s, p in found.items() if len(p) == 1}


def reset(serial: str, root: Path = SYS, dev: str = "/dev/bus/usb", wsl=in_wsl) -> str:
    """Reset the dongle with this serial: "" when done, else why not (nothing done)."""
    if wsl():
        return "WSL: no USB reset through usbipd"
    at = ident(serial, root)
    if at is None:
        return "not found once in /sys"
    node = f"{dev}/{at[0]:03d}/{at[1]:03d}"
    try:
        fd = os.open(node, os.O_WRONLY)
    except OSError as e:
        return f"can't open {node}: {e.strerror or e}"
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    except OSError as e:
        return f"reset failed: {e.strerror or e}"
    finally:
        os.close(fd)
    return ""
