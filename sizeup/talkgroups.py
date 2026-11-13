"""Per-talkgroup display/recording choices for P25 traffic, changed from the web app.

    record: keep this talkgroup's calls (transcribe + show). Off = calls are dropped on arrival.
    main:   show it in the main traffic list and attach it to our incident; otherwise it lives
            under "Other talkgroups" as context.

Defaults: main = the configured incident talkgroups (Fire/EMS 15, Airport Incident); everything
records except encrypted talkgroups (they can't be decoded anyway). Choices are saved to JSON.

More than one P25 system (GitHub #409, sizeup/sourcekeys.py): a talkgroup number is only unique inside its system, so
every method takes the system too (`system`: "" or the county's name = the county system, as before). A talkgroup of
another system (`others`: {system: {tg: info}}, from sizeup/radio/systems.py) has a tier instead of the switches
(sizeup/tiers.py: alert, text, record, off, activity): Record follows the tier and it is never Main (its traffic is
context, never our incident's). The county's choices stay in talkgroups.json under the bare number, as a release from
before this reads them; the other systems' are in talkgroups_systems.json under "p25:<system>:<tg>". A talkgroup
granted on another system that isn't in its file is counted as unknown (unknown()).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from sizeup import sourcekeys as sources, tiers

UNKNOWN_MAX = 500          # unknown talkgroups remembered per system (the least recently heard go first)


class TalkgroupPrefs:
    def __init__(self, info: dict[int, dict], main_default: set[int], path: str | Path | None = None,
                 others: dict[str, dict[int, dict]] | None = None, county: str = sources.COUNTY):
        self.county = county                  # the county system's name: `system` "" means this one
        # tg -> {"name", "group", "encrypted"} for the county's; (system, tg) -> the same + "tier", "service",
        # "department" for every other system's
        self.info: dict = dict(info)
        for name, tgs in (others or {}).items():
            if name != county:
                self.info.update({(name, int(tg)): dict(i, system=name) for tg, i in tgs.items()})
        self.main_default = set(main_default)
        self.path = Path(path) if path else None
        self.systems_path = self.path.with_name("talkgroups_systems.json") if self.path else None
        self.unknown_path = self.path.with_name("talkgroups_unknown.json") if self.path else None
        self._lock = threading.Lock()
        self._prefs: dict = {}
        self._seen: dict = {}                 # key -> {"count", "last", "dropped"}
        self._unknown: dict[str, dict[int, dict]] = {}    # system -> tg -> {"count", "first", "last"}: not in its file
        self._unknown_saved = 0.0
        if self.path and self.path.is_file():
            try:
                self._prefs = {int(k): v for k, v in json.loads(self.path.read_text()).items()}
            except (OSError, ValueError):
                self._prefs = {}
        try:
            if self.systems_path and self.systems_path.is_file():
                for k, v in json.loads(self.systems_path.read_text()).items():
                    got = sources.parse(k, county)
                    if got is not None and got[0] != county and isinstance(v, dict):
                        self._prefs[got] = v
        except (OSError, ValueError, AttributeError):
            pass
        try:
            raw = json.loads(self.unknown_path.read_text()) if self.unknown_path and self.unknown_path.is_file() else {}
            self._unknown = {str(x): {int(k): dict(v) for k, v in d.items()} for x, d in raw.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self._unknown = {}

    def _k(self, tg: int, system: str = ""):
        """The key a talkgroup is held under: its number for the county's, (system, number) for another system's."""
        return int(tg) if not system or system == self.county else (system, int(tg))

    @staticmethod
    def _parts(k) -> tuple[str, int]:
        return k if isinstance(k, tuple) else ("", k)

    def source(self, tg: int, system: str = "") -> str:
        return sources.p25(system or self.county, tg)

    def name(self, tg: int, system: str = "") -> str:
        return self.info.get(self._k(tg, system), {}).get("name") or f"TG {tg}"

    def known(self, tg: int, system: str = "") -> bool:
        return self._k(tg, system) in self.info

    def tier(self, tg: int, system: str = "") -> str | None:
        """Its tier (sizeup/tiers.py); None for a county talkgroup (its own switches)."""
        k = self._k(tg, system)
        if not isinstance(k, tuple):
            return None
        i = self.info.get(k, {})
        enc = bool(i.get("encrypted"))
        t = tiers.clean(self._prefs.get(k, {}).get("tier"))
        return t if t and tiers.allowed(t, enc) else tiers.default(enc, i.get("tier"))

    def record(self, tg: int, system: str = "") -> bool:
        k = self._k(tg, system)
        if isinstance(k, tuple):
            return self.tier(tg, system) in tiers.RECORDED
        p = self._prefs.get(k, {})
        return p.get("record", not self.info.get(k, {}).get("encrypted", False))

    def main(self, tg: int, system: str = "") -> bool:
        k = self._k(tg, system)
        if isinstance(k, tuple):
            return False                      # another system's traffic is context: never our incident's
        return self._prefs.get(k, {}).get("main", k in self.main_default)

    def set_tier(self, tg: int, tier: str, system: str = "") -> str | None:
        """A tier for a talkgroup of another system. None, or why not."""
        k = self._k(tg, system)
        t = tiers.clean(tier)
        if not isinstance(k, tuple):
            return f"talkgroup {tg}: the county system's talkgroups have Record / Main / Transcribe, not a tier"
        if k not in self.info:
            return f"no talkgroup {tg} on {system}"
        if t is None:
            return f"tier: one of {', '.join(tiers.TIERS)}"
        if not tiers.allowed(t, bool(self.info[k].get("encrypted"))):
            return f"{self.name(tg, system)} is encrypted: activity or off only"
        with self._lock:
            self._prefs.setdefault(k, {})["tier"] = t
        self.save()
        return None

    # -- talkgroups of another system that aren't in its file ---------------------------------------

    def unknown_seen(self, system: str, tg: int, at: float | None = None) -> bool:
        """A talkgroup heard on `system` that its file doesn't list: counted. True the first time."""
        at = time.time() if at is None else at
        with self._lock:
            d = self._unknown.setdefault(system, {})
            new = tg not in d
            u = d.setdefault(tg, {"count": 0, "first": at, "last": at})
            u["count"] += 1
            u["last"] = max(u["last"], at)
            if len(d) > UNKNOWN_MAX:
                for x in sorted(d, key=lambda x: d[x]["last"])[:len(d) - UNKNOWN_MAX]:
                    del d[x]
            due = new or at - self._unknown_saved > 60
        if due:
            self.save_unknown()
        return new

    def unknown(self, system: str, limit: int = 50) -> list[dict]:
        """[{tg, source, count, first, last}] for that system, most recently heard first."""
        with self._lock:
            rows = [{"tg": tg, "source": sources.p25(system, tg), **u} for tg, u in self._unknown.get(system, {}).items()
                    if self._k(tg, system) not in self.info]
        return sorted(rows, key=lambda r: -r["last"])[:limit]

    def save_unknown(self) -> None:
        if not self.unknown_path:
            return
        try:
            with self._lock:
                data = json.dumps({x: {str(k): v for k, v in d.items()} for x, d in self._unknown.items()})
                self._unknown_saved = time.time()
            self.unknown_path.write_text(data)
        except OSError:
            pass

    def seen(self, tg: int, dropped: bool = False, system: str = "") -> None:
        tg = self._k(tg, system)
        with self._lock:
            s = self._seen.setdefault(tg, {"count": 0, "last": 0.0, "dropped": 0})
            s["count"] += 1
            s["last"] = time.time()
            if dropped:
                s["dropped"] += 1

    def seed(self, tg: int, at: float, system: str = "") -> None:
        """Count a call already in the saved history (so a restart doesn't zero the counts)."""
        tg = self._k(tg, system)
        with self._lock:
            s = self._seen.setdefault(tg, {"count": 0, "last": 0.0, "dropped": 0})
            s["count"] += 1
            s["last"] = max(s["last"] or 0.0, at)

    def heard(self, extra=(), limit: int = 40) -> dict[str, dict]:
        """{"tg:307": {"at": its last call, "name": "Fire/EMS 15"}} for the main talkgroups (and `extra`) heard since
        the start: the station's health, for the hub's per-talkgroup silence hints (sizeup/cloud/health.py, #242, #258)."""
        with self._lock:
            seen = {tg: s.get("last") for tg, s in self._seen.items() if s.get("last") and not isinstance(tg, tuple)}
        tgs = sorted(tg for tg in seen if tg in set(extra) or self.main(tg))[:limit]
        return {f"tg:{tg}": {"at": seen[tg], "name": self.name(tg)} for tg in tgs}

    def set(self, tg: int, record: bool | None = None, main: bool | None = None, system: str = "") -> None:
        k = self._k(tg, system)
        if isinstance(k, tuple):              # another system's: Record is its tier, and it is never Main
            if record is not None and bool(record) != self.record(tg, system):
                enc = bool(self.info.get(k, {}).get("encrypted"))
                back = tiers.default(enc, self.info.get(k, {}).get("tier"))
                self.set_tier(tg, tiers.IGNORE if not record else tiers.ACTIVITY if enc
                              else back if back in tiers.RECORDED else tiers.RECORD, system)
            return
        with self._lock:
            p = self._prefs.setdefault(tg, {})
            if record is not None:
                p["record"] = bool(record)
            if main is not None:
                p["main"] = bool(main)
        self.save()

    def group_of(self, tg: int, system: str = "") -> str:
        return self.info.get(self._k(tg, system), {}).get("group") or "Unknown"

    def set_group(self, group: str, record: bool | None = None, main: bool | None = None) -> int:
        """A whole section (e.g. 'County Police') on or off. Turning recording on skips encrypted
        talkgroups (they can't be decoded). Saved per talkgroup, so it survives restarts."""
        tgs = {tg for tg in set(self.info) | set(self._seen) | set(self._prefs) if self.group_of(*self._parts(tg)[::-1]) == group}
        for sysname, tg in [tg for tg in tgs if isinstance(tg, tuple)]:
            self.set(tg, record=record, system=sysname)       # by its tier
        with self._lock:
            for tg in [tg for tg in tgs if not isinstance(tg, tuple)]:
                p = self._prefs.setdefault(tg, {})
                if record is not None and not (record and self.info.get(tg, {}).get("encrypted")):
                    p["record"] = bool(record)
                if main is not None:
                    p["main"] = bool(main)
        self.save()
        return len(tgs)

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data = json.dumps({str(k): v for k, v in self._prefs.items() if not isinstance(k, tuple)})
                more = {sources.p25(*k): v for k, v in self._prefs.items() if isinstance(k, tuple)}
            self.path.write_text(data)
            if more or self.systems_path.is_file():
                self.systems_path.write_text(json.dumps(more))
        except OSError:
            pass

    def state(self) -> list[dict]:
        """Every known or heard talkgroup with its switches: main first, then most recently heard."""
        with self._lock:
            tgs = set(self.info) | set(self._seen) | set(self._prefs)
            rows = []
            for k in tgs:
                sysname, tg = self._parts(k)
                i, s = self.info.get(k, {}), self._seen.get(k, {})
                main = self.main(tg, sysname)
                rows.append({"tg": tg, "name": self.name(tg, sysname), "group": i.get("group") or "Unknown",
                             "encrypted": bool(i.get("encrypted")), "record": self.record(tg, sysname),
                             "main": main, "count": s.get("count", 0), "last": s.get("last"),
                             "dropped": s.get("dropped", 0),
                             # #409 / #415: the source, qualified by its system; `key`: where its speech-to-text
                             # choices are (sizeup/channels.py); tier None: the county's own switches
                             "system": sysname or self.county, "source": sources.p25(sysname or self.county, tg),
                             "key": sources.channel_key(sysname, tg, self.county), "ours": main,
                             "service": sources.service(i.get("group") or "", i.get("service") or ""),
                             "department": i.get("department") or "", "tier": self.tier(tg, sysname)})
        rows.sort(key=lambda r: (not r["main"], -(r["last"] or 0), r["group"], r["tg"]))
        return rows
