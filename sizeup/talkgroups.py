"""Per-talkgroup display/recording choices for P25 traffic, changed from the web app.

    record: keep this talkgroup's calls (transcribe + show). Off = calls are dropped on arrival.
    main:   show it in the main traffic list and attach it to our incident; otherwise it lives
            under "Other talkgroups" as context.

Defaults: main = the configured incident talkgroups (Fire/EMS 15, Airport Incident); everything
records except encrypted talkgroups (they can't be decoded anyway). Choices are saved to JSON.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class TalkgroupPrefs:
    def __init__(self, info: dict[int, dict], main_default: set[int], path: str | Path | None = None):
        self.info = info                      # tg -> {"name", "group", "encrypted"}
        self.main_default = set(main_default)
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._prefs: dict[int, dict] = {}
        self._seen: dict[int, dict] = {}      # tg -> {"count", "last", "dropped"}
        if self.path and self.path.is_file():
            try:
                self._prefs = {int(k): v for k, v in json.loads(self.path.read_text()).items()}
            except (OSError, ValueError):
                self._prefs = {}

    def name(self, tg: int) -> str:
        return self.info.get(tg, {}).get("name") or f"TG {tg}"

    def record(self, tg: int) -> bool:
        p = self._prefs.get(tg, {})
        return p.get("record", not self.info.get(tg, {}).get("encrypted", False))

    def main(self, tg: int) -> bool:
        return self._prefs.get(tg, {}).get("main", tg in self.main_default)

    def seen(self, tg: int, dropped: bool = False) -> None:
        with self._lock:
            s = self._seen.setdefault(tg, {"count": 0, "last": 0.0, "dropped": 0})
            s["count"] += 1
            s["last"] = time.time()
            if dropped:
                s["dropped"] += 1

    def seed(self, tg: int, at: float) -> None:
        """Count a call already in the saved history (so a restart doesn't zero the counts)."""
        with self._lock:
            s = self._seen.setdefault(tg, {"count": 0, "last": 0.0, "dropped": 0})
            s["count"] += 1
            s["last"] = max(s["last"] or 0.0, at)

    def set(self, tg: int, record: bool | None = None, main: bool | None = None) -> None:
        with self._lock:
            p = self._prefs.setdefault(tg, {})
            if record is not None:
                p["record"] = bool(record)
            if main is not None:
                p["main"] = bool(main)
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data = json.dumps({str(k): v for k, v in self._prefs.items()})
            self.path.write_text(data)
        except OSError:
            pass

    def state(self) -> list[dict]:
        """Every known or heard talkgroup with its switches: main first, then most recently heard."""
        with self._lock:
            tgs = set(self.info) | set(self._seen) | set(self._prefs)
            rows = []
            for tg in tgs:
                i, s = self.info.get(tg, {}), self._seen.get(tg, {})
                rows.append({"tg": tg, "name": self.name(tg), "group": i.get("group", "Unknown"),
                             "encrypted": bool(i.get("encrypted")), "record": self.record(tg),
                             "main": self.main(tg), "count": s.get("count", 0), "last": s.get("last"),
                             "dropped": s.get("dropped", 0)})
        rows.sort(key=lambda r: (not r["main"], -(r["last"] or 0), r["group"], r["tg"]))
        return rows
