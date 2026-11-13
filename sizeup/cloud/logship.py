"""The station's journal to the hub, then CloudWatch (/sizeup/station): its logs in one place, kept cheap.

The station reads its own units' journal (`journalctl -u 'sizeup-*'`: sizeup-station with the incident-source adapter
in it, sizeup-whisper, sizeup-trunk-recorder, sizeup-update, sizeup-health, sizeup-adsb, sizeup-ais, sizeup-usbfs and any
unit added later) every READ_S, and sends what is left over the hub link it already has (the station token and the
Access service token, POST /api/station/logs): no AWS key on the station. The hub writes it to CloudWatch Logs
(sizeup/cloud/cwlogs.py).

    read      journalctl -o json --after-cursor <the last one spooled> (the first time: the last FIRST_S), at most
              MAX_READ entries a turn. Needs the journal: the sizeup user is in systemd-journal (sizeup-station.service
              SupplementaryGroups); without it: "no journal access", nothing sent
    keep      each line: the unit kept (the hub's include / exclude), its level (logscrub.level) at least the hub's
              threshold (default info: debug dropped), scrubbed (logscrub.scrub: a line with a credential is
              DROPPED, fail closed), the same line at most BURST times per 10 min (logscrub.Repeats)
    spool     a batch every BATCH_S (or MAX_BATCH_LINES sooner) as one gzipped JSON file in <state>/logspool, written
              before the journal cursor moves (at least once, never lost to a restart); at most SPOOL_MAX_BYTES on disk,
              the oldest batch deleted first (counted: "dropped")
    send      the oldest batch first, gzipped as spooled: {v, station, batch, lines: [[ms, unit, level, msg]]}. The
              hub answers {ok, accepted, config: {on, level, include, exclude}, retry_s}: the batch is deleted only once
              accepted; "not configured" (the AWS step not run yet) keeps it and waits retry_s (10 min)
    off       SIZEUP_LOG_SHIP=off in station.env, or the hub's Settings (Station > Logs: Off): nothing is read; the
              cursor is forgotten, so turning it on again starts from now (not a backlog)

stats() goes up in the station's status ("logship"): Station > Logs on the hub shows it.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup import logscrub
from sizeup.cloud.client import HubError

PATH = "/api/station/logs"
READ_S = 15.0                 # the journal is read this often ...
BATCH_S = 45.0                # ... and a batch closed this often (30-60 s: the design)
MAX_BATCH_LINES = 5000
MAX_BATCH_BYTES = 512 * 1024  # uncompressed: well under the hub's body limit and CloudWatch's 1 MB per call
MAX_READ = 20000              # journal entries per turn (a backlog drains over several)
FIRST_S = 600.0               # no cursor: start this far back
SPOOL_MAX_BYTES = 50 * 1024 * 1024
RETRY_S = (5.0, 600.0)        # a failed send: doubling, at most this
UNITS = "sizeup-*"
DEFAULT_CONFIG = {"on": True, "level": "info", "include": [], "exclude": []}


def _env_off() -> bool:
    return (os.environ.get("SIZEUP_LOG_SHIP") or "").strip().lower() in ("0", "off", "no", "false")


def entry(raw: dict) -> tuple[float, str, int, str, str] | None:
    """A journalctl -o json entry -> (at, unit, priority, message, cursor), or None (nothing to send)."""
    msg = raw.get("MESSAGE")
    if isinstance(msg, list):                       # not UTF-8: journald gives the bytes
        try:
            msg = bytes(int(b) & 0xFF for b in msg).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return None
    if not isinstance(msg, str) or not msg.strip():
        return None
    try:
        at = int(raw.get("__REALTIME_TIMESTAMP")) / 1e6
    except (TypeError, ValueError):
        at = time.time()
    unit = str(raw.get("_SYSTEMD_UNIT") or "")
    if not unit.startswith("sizeup-"):                 # systemd's own lines about a unit ("Started ...", "Failed ...")
        unit = str(raw.get("UNIT") or raw.get("USER_UNIT") or unit or raw.get("SYSLOG_IDENTIFIER") or "?")
    unit = unit.removesuffix(".service")[:64]
    try:
        prio = int(raw.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    return at, unit, prio, msg.rstrip(), str(raw.get("__CURSOR") or "")


def unit_ok(unit: str, cfg: dict) -> bool:
    inc, exc = cfg.get("include") or [], cfg.get("exclude") or []
    if unit in exc:
        return False
    return not inc or unit in inc


class LogShipper:
    """The station's thread: journal -> filter -> spool -> hub."""

    def __init__(self, cfg, client: Callable[[float], object], log: Callable[[str], None] = print,
                 spool: Path | str | None = None, clock: Callable[[], float] = time.time,
                 journal: Callable[[str, float, int], list[dict]] | None = None):
        self.cfg, self._client, self.log, self.clock = cfg, client, log, clock
        self.station = str(getattr(cfg, "station_id", "") or "station")
        if spool is None:
            ob = getattr(cfg, "cloud_outbox", "") or ""
            spool = Path(ob).with_name("logspool") if ob and ob != ":memory:" else None
        self.dir = Path(spool) if spool else None
        self._journal = journal or self._journalctl
        self.config = dict(DEFAULT_CONFIG)
        self.repeats = logscrub.Repeats()
        self.pending: list[list] = []          # [ms, unit, level, msg] not spooled yet
        self.pending_bytes = 0
        self.batch_started = 0.0
        self.cursor = ""
        self.seq = 0
        self.next_send = 0.0
        self.fails = 0
        self.state, self.reason = "starting", ""
        self.sent_lines = self.sent_batches = self.sent_bytes = 0
        self.dropped_secret = self.dropped_level = self.dropped_unit = self.dropped_spool = 0
        self.last_ok: float | None = None
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.c = None
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.cursor = self._cursor_seen = (self.dir / "cursor").read_text().strip()
            except OSError:
                pass
            try:
                cfgf = json.loads((self.dir / "config.json").read_text())
                if isinstance(cfgf, dict):
                    self._set_config(cfgf)
            except (OSError, ValueError):
                pass
            self.seq = max([self._seq_of(p) for p in self._batches()] + [0])

    # -- the journal -----------------------------------------------------------------------------------------

    def _journalctl(self, cursor: str, since: float, limit: int) -> list[dict]:
        """Entries after cursor (or since), oldest first, at most limit. RuntimeError: no journal to read."""
        exe = shutil.which("journalctl")
        if not exe:
            raise RuntimeError("no journalctl here")
        args = [exe, "-o", "json", "--no-pager", "-q", "-u", UNITS]
        args += [f"--after-cursor={cursor}"] if cursor else ["--since", f"@{int(since)}"]
        out: list[dict] = []
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        try:
            for line in p.stdout:                       # type: ignore[union-attr]
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
                if len(out) >= limit:
                    break
        finally:
            if p.poll() is None:
                p.terminate()
            try:
                _, err = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                err = ""
        if not out and p.returncode not in (0, None, -15) and err:
            raise RuntimeError(err.strip().splitlines()[-1][:200] if err.strip() else f"journalctl exit {p.returncode}")
        if not out and "insufficient permissions" in (err or "").lower():
            raise RuntimeError("no journal access (the sizeup user isn't in systemd-journal)")
        return out

    # -- filter and batch ----------------------------------------------------------------------------------

    def take(self, raw: dict) -> None:
        """One journal entry: kept (into the batch) or counted as dropped."""
        e = entry(raw)
        if e is None:
            return
        at, unit, prio, msg, _ = e
        if not unit_ok(unit, self.config):
            self.dropped_unit += 1
            return
        lv = logscrub.level(prio, msg)
        if not logscrub.at_least(lv, self.config.get("level") or "info"):
            self.dropped_level += 1
            return
        clean = logscrub.scrub(msg)
        if clean is None:
            self.dropped_secret += 1
            return
        for t, u, lvl, m in self.repeats.allow(unit, clean, lv, at):
            self._add(t, u, lvl, m)

    def _add(self, at: float, unit: str, lv: str, msg: str) -> None:
        if not self.pending:
            self.batch_started = self.clock()
        self.pending.append([int(at * 1000), unit, lv, msg])
        self.pending_bytes += len(msg) + len(unit) + 32

    def _batches(self) -> list[Path]:
        if self.dir is None:
            return []
        try:
            return sorted(p for p in self.dir.iterdir() if p.name.endswith(".json.gz") and not p.name.startswith("."))
        except OSError:
            return []

    @staticmethod
    def _seq_of(p: Path) -> int:
        try:
            return int(p.name.split("-")[0])
        except ValueError:
            return 0

    def spool(self, force: bool = False) -> Path | None:
        """The batch so far into the spool (then the cursor saved), when due or forced."""
        now = self.clock()
        for t, u, lvl, m in self.repeats.flush(now):
            self._add(t, u, lvl, m)
        if not self.pending:
            self._save_cursor()
            return None
        due = (force or now - self.batch_started >= BATCH_S or len(self.pending) >= MAX_BATCH_LINES
               or self.pending_bytes >= MAX_BATCH_BYTES)
        if not due:
            return None
        self.seq += 1
        body = {"v": 1, "station": self.station, "batch": f"{self.station}-{int(now)}-{self.seq}",
                "lines": sorted(self.pending, key=lambda r: r[0])}
        data = gzip.compress(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(), 6)
        self.pending, self.pending_bytes = [], 0
        if self.dir is None:
            self._mem = getattr(self, "_mem", [])
            self._mem.append(data)
            self._mem = self._mem[-20:]
            return None
        path = self.dir / f"{self.seq:012d}-{int(now)}.json.gz"
        tmp = self.dir / f".{path.name}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            self.last_error = f"spool: {type(e).__name__}"
            return None
        self._save_cursor()
        self._cap()
        return path

    def _save_cursor(self) -> None:
        if self.dir is None or not self._cursor_seen:
            return
        try:
            tmp = self.dir / ".cursor.tmp"
            tmp.write_text(self._cursor_seen)
            os.replace(tmp, self.dir / "cursor")
            self.cursor = self._cursor_seen
        except OSError:
            pass

    _cursor_seen = ""

    def _cap(self) -> None:
        files = self._batches()
        sizes = {p: (p.stat().st_size if p.exists() else 0) for p in files}
        total = sum(sizes.values())
        while files and total > SPOOL_MAX_BYTES:
            p = files.pop(0)
            try:
                p.unlink()
                self.dropped_spool += 1
            except OSError:
                pass
            total -= sizes[p]

    def spooled(self) -> tuple[int, int]:
        files = self._batches()
        return len(files), sum(p.stat().st_size for p in files if p.exists())

    # -- the hub ---------------------------------------------------------------------------------------------

    def _set_config(self, c: dict) -> None:
        out = dict(DEFAULT_CONFIG)
        if isinstance(c.get("on"), bool):
            out["on"] = c["on"]
        if c.get("level") in logscrub.LEVELS:
            out["level"] = c["level"]
        for k in ("include", "exclude"):
            v = c.get(k)
            if isinstance(v, list):
                out[k] = [str(u)[:64] for u in v if isinstance(u, str)][:50]
        if out != self.config:
            self.config = out
            if self.dir is not None:
                try:
                    (self.dir / "config.json").write_text(json.dumps(out))
                except OSError:
                    pass

    def send(self, now: float | None = None) -> str:
        """The oldest spooled batch to the hub: "sent" | "empty" | "waiting" | "failed" | "not configured"."""
        now = self.clock() if now is None else now
        if now < self.next_send:
            return "waiting"
        files = self._batches()
        if not files:
            return "empty"
        p = files[0]
        try:
            data = p.read_bytes()
        except OSError:
            return "empty"
        try:
            if self.c is None:
                self.c = self._client(20.0)
            r = self.c.request("POST", PATH, raw_gzip=data, timeout=20.0)
        except HubError as e:
            return self._failed(now, str(e))
        if not isinstance(r, dict):
            return self._failed(now, "bad answer")
        if isinstance(r.get("config"), dict):
            self._set_config(r["config"])
        if not r.get("accepted"):
            try:
                wait = min(RETRY_S[1], max(RETRY_S[0], float(r.get("retry_s") or RETRY_S[1])))
            except (TypeError, ValueError):
                wait = RETRY_S[1]
            self.next_send = now + wait
            self.state, self.reason = "waiting", str(r.get("reason") or "hub: not configured")[:200]
            return "not configured"
        try:
            n = len(json.loads(gzip.decompress(data)).get("lines") or [])
        except (OSError, ValueError, EOFError):
            n = 0
        try:
            p.unlink()
        except OSError:
            pass
        self.sent_batches += 1
        self.sent_lines += n
        self.sent_bytes += len(data)
        if self.fails:
            self.log(f"station logs: sending again after {self.fails} failed tries")
        self.fails, self.last_ok, self.last_error = 0, now, ""
        self.state, self.reason = "ok", ""
        self.next_send = 0.0
        return "sent"

    def _failed(self, now: float, why: str) -> str:
        self.fails += 1
        wait = min(RETRY_S[1], RETRY_S[0] * 2 ** min(self.fails - 1, 20))
        self.next_send = now + wait
        self.last_error = why[:200]
        self.state, self.reason = "offline", "hub not reachable: kept on disk"
        if self.fails in (1, 10) or self.fails % 100 == 0:
            self.log(f"station logs: not sent ({why[:120]}); kept on disk, next try in {wait:.0f} s")
        return "failed"

    # -- the loop --------------------------------------------------------------------------------------------

    def step(self, now: float | None = None) -> str:
        """One turn: read, batch, send. "off" | "ok" | the reason it can't."""
        now = self.clock() if now is None else now
        if not _env_off() and not self.config.get("on", True):
            self.send(now)              # what was spooled still goes (and the hub's answer may turn it back on)
        if _env_off() or not self.config.get("on", True):
            self.state, self.reason = "off", ("SIZEUP_LOG_SHIP=off" if _env_off() else "off in Settings")
            if self.cursor and self.dir is not None:
                try:
                    (self.dir / "cursor").unlink()
                except OSError:
                    pass
            self.cursor = self._cursor_seen = ""
            self.pending, self.pending_bytes = [], 0
            return "off"
        try:
            rows = self._journal(self._cursor_seen or self.cursor, now - FIRST_S, MAX_READ)
        except RuntimeError as e:
            self.state, self.reason = "no journal", str(e)[:200]
            self.send(now)
            return self.reason
        for raw in rows:
            self.take(raw)
            c = raw.get("__CURSOR")
            if isinstance(c, str) and c:
                self._cursor_seen = c
        if self._cursor_seen != self.cursor and not self.pending:
            self._save_cursor()
        self.spool(force=len(rows) >= MAX_READ)
        while self.send(now) == "sent":
            pass
        if self.state in ("starting", "no journal"):
            self.state, self.reason = "ok", ""
        return "ok"

    def start(self) -> LogShipper:
        if self._thread is None:
            from sizeup import procenv
            procenv.register_secrets()          # the token and the adapter's credentials: never sent (logscrub)
            self._thread = threading.Thread(target=self._run, name="hub-logs", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - the shipper must never die (nor take the station with it)
                self.state, self.reason = "error", f"{type(e).__name__}"
                self.log(f"station logs: {type(e).__name__}")
            self._stop.wait(READ_S)
        try:
            self.spool(force=True)
        except Exception:  # noqa: BLE001
            pass
        if self.c is not None:
            try:
                self.c.close()
            except Exception:  # noqa: BLE001
                pass

    def stats(self) -> dict:
        n, size = self.spooled()
        return {"state": self.state, "reason": self.reason, "level": self.config.get("level"),
                "on": bool(self.config.get("on", True)) and not _env_off(),
                "spool_batches": n, "spool_bytes": size, "spool_max": SPOOL_MAX_BYTES,
                "sent_lines": self.sent_lines, "sent_batches": self.sent_batches, "sent_bytes": self.sent_bytes,
                "dropped_secret": self.dropped_secret, "dropped_level": self.dropped_level,
                "dropped_unit": self.dropped_unit, "dropped_spool": self.dropped_spool,
                "repeats": self.repeats.suppressed, "last_ok": self.last_ok, "error": self.last_error}
