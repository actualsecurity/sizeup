"""Storage limits for everything sizeup (and trunk-recorder) writes under captures/.

    audio   (*.wav anywhere: clips, skipped mic clicks, trunk-recorder calls)   -> audio_hours (12 h)
    trunk-recorder call metadata (*.json next to its WAVs)                    -> with its audio
    page records (<clips>/<id>.page.json)                                     -> history_hours (24 h)
    trunk-recorder temp files (p25-tmp)                                       -> 1 h
    empty date folders under the P25 capture dir                              -> removed

Runs at startup and every few minutes from the app. Age is file modification time. Returns
what it found so the display can show disk use and the oldest audio kept.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TMP_KEEP_S = 3600


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
    if p25 and p25.is_dir():
        for f in p25.rglob("*.wav"):
            if _older(f, audio_cut):
                deleted += _rm(f)
                deleted += _rm(f.with_suffix(".json"))
        for f in p25.rglob("*.json"):                       # metadata whose audio is gone or never came
            if _older(f, audio_cut) and not f.with_suffix(".wav").exists():
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
