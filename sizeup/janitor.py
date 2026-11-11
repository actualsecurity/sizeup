"""Storage limits for everything sizeup (and trunk-recorder) writes under captures/.

    audio   (*.wav anywhere: clips, skipped mic clicks, trunk-recorder calls)   -> audio_hours (12 h)
    trunk-recorder call metadata (*.json next to its WAVs)                    -> with its audio
    page records (<clips>/<id>.page.json)                                     -> history_hours (24 h)
    trunk-recorder temp files (p25-tmp)                                       -> 1 h
    an emergency call's trunk-recorder JSON (emergency/*.json, App._drop_recorder_files) -> EMERGENCY_KEEP_S (90 days)
    empty date folders under the P25 capture dir                              -> removed
    another P25 system's captures (captures/p25-<name>, p25-<name>-tmp: #409)  -> the same ages, and at most
                                                                                 SYSTEM_MAX_BYTES (clean_system)

Runs at startup and every few minutes from the app. Age is file modification time. Returns
what it found so the display can show disk use and the oldest audio kept.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TMP_KEEP_S = 3600
EMERGENCY_DIR = "emergency"            # under captures/: emergency calls' records, kept for audit (#128)
EMERGENCY_KEEP_S = 90 * 86400          # as long as their alarms (sizeup/cad/alarms.py KEEP_S)


def _older(p: Path, cutoff: float) -> bool:
    try:
        return p.stat().st_mtime < cutoff
    except OSError:
        return False


def _rm(p: Path) -> bool:
    try:
        p.unlink()
        return True
    except OSError:
        return False


SYSTEM_MAX_BYTES = 200 * 1024 * 1024   # another P25 system's capture directory at most (the oldest calls go first)


def clean_system(capture: Path, audio_s: float, max_bytes: int = SYSTEM_MAX_BYTES, now: float | None = None) -> dict:
    """Another P25 system's own capture directory and its temp directory beside it (<capture>-tmp), which nothing
    else cleans (#409): calls older than audio_s (made while sizeup was down, or never ingested: an unreadable WAV, a
    talkgroup that isn't in its file), metadata whose audio is gone, temp files older than TMP_KEEP_S, empty date
    folders; then, over max_bytes, the oldest calls. {"deleted", "over", "bytes"}."""
    now = time.time() if now is None else now
    deleted = over = 0
    if capture.is_dir():
        for f in capture.rglob("*.wav"):
            if _older(f, now - audio_s):
                deleted += _rm(f)
                deleted += _rm(f.with_suffix(".json"))
        for f in capture.rglob("*.json"):
            if _older(f, now - audio_s) and not f.with_suffix(".wav").exists():
                deleted += _rm(f)
        files = []
        for f in capture.rglob("*"):
            try:
                st = f.stat()
            except OSError:
                continue
            if f.is_file():
                files.append((st.st_mtime, st.st_size, f))
        total = sum(size for _m, size, _f in files)
        for _m, size, f in sorted(files, key=lambda x: x[0]):
            if total <= max_bytes:
                break
            if _rm(f):
                total -= size
                over += f.suffix == ".wav"
                deleted += 1
        for d in sorted((d for d in capture.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    else:
        total = 0
    tmp = capture.with_name(capture.name + "-tmp")
    if tmp.is_dir():
        for f in tmp.rglob("*"):
            if f.is_file() and _older(f, now - TMP_KEEP_S):
                deleted += _rm(f)
    return {"deleted": deleted, "over": over, "bytes": total}


def clean(captures: Path, clips: Path | None, p25: Path | None, audio_s: float, history_s: float,
          now: float | None = None) -> dict:
    now = time.time() if now is None else now
    audio_cut, history_cut = now - audio_s, now - history_s
    deleted = 0
    if clips and clips.is_dir():
        for f in clips.rglob("*.wav"):                      # clips + skipped/
            if _older(f, audio_cut):
                deleted += _rm(f)
        for f in clips.glob("*.page.json"):
            if _older(f, history_cut):
                deleted += _rm(f)
        for f in clips.glob("*.json"):                      # older clip metadata (non-page)
            if not f.name.endswith(".page.json") and _older(f, history_cut):
                deleted += _rm(f)
    kept = captures / EMERGENCY_DIR
    if kept.is_dir():
        for f in kept.glob("*.json"):
            if _older(f, now - EMERGENCY_KEEP_S):
                deleted += _rm(f)
    if p25 and p25.is_dir():
        for f in p25.rglob("*.wav"):
            if _older(f, audio_cut):
                deleted += _rm(f)
                deleted += _rm(f.with_suffix(".json"))
        for f in p25.rglob("*.json"):                       # metadata whose audio is gone or never came
            if _older(f, audio_cut) and not f.with_suffix(".wav").exists() and kept not in f.parents:
                deleted += _rm(f)
        for d in sorted((d for d in p25.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()                                    # only succeeds when empty
            except OSError:
                pass
    tmp = captures / "p25-tmp"
    if tmp.is_dir():
        for f in tmp.iterdir():
            if f.is_file() and _older(f, now - TMP_KEEP_S):
                deleted += _rm(f)
    total, oldest_audio, n_audio = 0, None, 0
    for root, _dirs, files in os.walk(captures):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_size
            if name.endswith(".wav"):
                n_audio += 1
                oldest_audio = st.st_mtime if oldest_audio is None else min(oldest_audio, st.st_mtime)
    return {"bytes": total, "audio_files": n_audio, "oldest_audio": oldest_audio, "deleted": deleted,
            "audio_hours": audio_s / 3600, "history_hours": history_s / 3600, "checked": now}
