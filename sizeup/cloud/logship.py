"""The station's journal to the hub, then CloudWatch (/sizeup/station): its logs in one place, kept cheap.

The journal is read by sizeup-logread (sizeup/cloud/logread.py: a user of its own in systemd-journal, no network), which
hands over ONLY the station's own units' entries (`journalctl -u 'sizeup-*'`) as files in /var/lib/sizeup-logs. The
sizeup user (this process: the web UI, the incident-source adapter) never reads the journal itself. This shipper, in
sizeup-station where every credential is known, filters and scrubs them and sends what is left over the hub link it
already has (the station token and the Access service token, POST /api/station/logs): no AWS key on the station. The
hub writes it to CloudWatch Logs (sizeup/cloud/cwlogs.py).

    take      each entry, in order:
              - the incident-source adapter's own lines are NOT sent unless an admin turned "adapter" on in Settings
                (county CAD details and its credentials live there): a line that names an installed adapter (its entry
                point, module, distribution, source id or label), the runner's "incident sources:" lines, a
                third-party logger's "[log <name>]" line (install_log_tags) and a traceback frame in site-packages
                (with its source line); counted as dropped_adapter
              - the unit kept (the hub's include / exclude), its level (logscrub.level) at least the hub's threshold
                (default info: debug dropped)
              - scrubbed (logscrub.scrub: a line with a credential is DROPPED, fail closed; a scrub that raises drops
                the line too, counted, and the shipping goes on)
              - the same line at most BURST times per 10 min (logscrub.Repeats)
    spool     a batch every BATCH_S, or as soon as it reaches MAX_BATCH_LINES / MAX_BATCH_BYTES, as one gzipped JSON
              file in <state>/logspool; the hand-off files it came from are deleted only once it is written (at least
              once). The batch is kept in memory until the write succeeds. At most SPOOL_MAX_BYTES on disk, the oldest
              batch deleted first (counted)
    send      the oldest batch first, as spooled: {v, station, batch, lines: [[ms, unit, level, msg]]}. The hub answers
              {ok, accepted, config, retry_s}: deleted once accepted; "not configured" (the AWS step not run yet) keeps
              it and waits retry_s (10 min). A 400 / 413 / 415 / 422 (the hub can't take that batch, ever): moved to
              logspool/rejected (the last few kept), said once, never retried
    off       SIZEUP_LOG_SHIP=off in station.env, or Settings (Station > Logs: Off): the hand-off files are deleted
              unread

stats() goes up in the station's status ("logship"): Station > Logs on the hub shows it.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup import logscrub
from sizeup.cloud.client import HubError

PATH = "/api/station/logs"
HANDOFF = "/var/lib/sizeup-logs"       # sizeup-logread's directory (sizeup/cloud/logread.py DIR)
READ_S = 15.0
BATCH_S = 45.0                         # a batch every 30-60 s (the design)
MAX_BATCH_LINES = 5000
MAX_BATCH_BYTES = 512 * 1024           # uncompressed: well under the hub's body limit and CloudWatch's 1 MB per call
MAX_FILES_A_TURN = 50
SPOOL_MAX_BYTES = 50 * 1024 * 1024
REJECTED_KEEP = 5
RETRY_S = (5.0, 600.0)
REJECT = (400, 413, 415, 422)          # the hub will never take this batch
DEFAULT_CONFIG = {"on": True, "level": "info", "include": [], "exclude": [], "adapter": False}
ADAPTER_GROUP = "sizeup.incident_sources"
LOG_TAG = re.compile(r"^\s*(?:\d\d:\d\d:\d\d\s+)?\[log ([\w.-]+)\]")
FRAME = re.compile(r'^\s*File "[^"]*(?:site-packages|dist-packages)/')


def _env_off() -> bool:
    return (os.environ.get("SIZEUP_LOG_SHIP") or "").strip().lower() in ("0", "off", "no", "false")


def entry(raw: dict) -> tuple[float, str, int, str] | None:
    """A journalctl -o json entry -> (at, unit, priority, message), or None (nothing to send)."""
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
    return at, unit, prio, msg.rstrip()


def unit_ok(unit: str, cfg: dict) -> bool:
    inc, exc = cfg.get("include") or [], cfg.get("exclude") or []
    if unit in exc:
        return False
    return not inc or unit in inc


def adapter_markers(app=None, entry_points=None) -> set[str]:
    """What names the incident-source adapter in a line: its entry points' names, modules (top package) and
    distributions, and the runner's sources (id, label). Read at runtime: the repo names none of them."""
    out: set[str] = set()
    try:
        if entry_points is None:
            from importlib.metadata import entry_points
        for ep in entry_points(group=ADAPTER_GROUP):
            out.add(ep.name)
            mod = (getattr(ep, "module", "") or ep.value.split(":")[0]).split(".")[0]
            out.add(mod)
            dist = getattr(getattr(ep, "dist", None), "name", "") or ""
            out.add(dist)
            out.add(dist.replace("-", "_"))
    except Exception:  # noqa: BLE001 - broken metadata: the other markers still hold
        pass
    runner = getattr(app, "sources_runner", None) if app is not None else None
    for r in getattr(runner, "runs", None) or []:
        a = getattr(r, "a", None)
        out.update(str(getattr(a, k, "") or "") for k in ("source", "label"))
    return {m for m in out if len(m) >= 3 and m not in ("sizeup", "sources")}


class _Tag(logging.Formatter):
    def format(self, rec: logging.LogRecord) -> str:
        return f"[log {rec.name}] {rec.levelname.lower()}: " + super().format(rec)


def install_log_tags() -> None:
    """Python logging (what the adapter or a library may use: sizeup itself prints) says its logger's name, so the
    shipper can leave a third party's lines on the station: a stderr handler on the root logger, WARNING and up (as
    logging's last resort prints them), when nothing else is set up."""
    root = logging.getLogger()
    if root.handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(_Tag("%(message)s"))
    h.setLevel(logging.WARNING)
    root.addHandler(h)


class LogShipper:
    """sizeup-station's thread: hand-off files -> filter -> scrub -> spool -> hub."""

    def __init__(self, cfg, client: Callable[[float], object], log: Callable[[str], None] = print,
                 spool: Path | str | None = None, clock: Callable[[], float] = time.time,
                 handoff: Path | str | None = None, app=None, markers: Callable[[], set[str]] | None = None):
        self.cfg, self._client, self.log, self.clock, self.app = cfg, client, log, clock, app
        self.station = str(getattr(cfg, "station_id", "") or "station")
        if spool is None:
            ob = getattr(cfg, "cloud_outbox", "") or ""
            spool = Path(ob).with_name("logspool") if ob and ob != ":memory:" else None
        self.dir = Path(spool) if spool else None
        self.handoff = Path(handoff or os.environ.get("SIZEUP_LOG_HANDOFF") or HANDOFF)
        self._markers_fn = markers or (lambda: adapter_markers(self.app))
        self._markers: set[str] = set()
        self._markers_at = 0.0
        self.config = dict(DEFAULT_CONFIG)
        self.repeats = logscrub.Repeats()
        self.pending: list[list] = []          # [ms, unit, level, msg] not spooled yet
        self.pending_bytes = 0
        self.batch_started = 0.0
        self.consumed: list[Path] = []         # hand-off files whose lines are in pending (deleted once spooled)
        self._skip_frame: dict[str, bool] = {}
        self.seq = 0
        self.next_send = 0.0
        self.fails = 0
        self.state, self.reason = "starting", ""
        self.sent_lines = self.sent_batches = self.sent_bytes = 0
        self.dropped_secret = self.dropped_level = self.dropped_unit = self.dropped_spool = 0
        self.dropped_adapter = self.dropped_error = self.rejected = self.dropped_off = 0
        self.last_ok: float | None = None
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.c = None
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                cfgf = json.loads((self.dir / "config.json").read_text())
                if isinstance(cfgf, dict):
                    self._set_config(cfgf)
            except (OSError, ValueError):
                pass
            self.seq = max([self._seq_of(p) for p in self._batches()] + [0])

    # -- the hand-off ------------------------------------------------------------------------------------------

    def _handoff_files(self) -> list[Path]:
        try:
            return sorted(p for p in self.handoff.glob("*.jsonl"))
        except OSError:
            return []

    def reader_status(self) -> dict:
        try:
            d = json.loads((self.handoff / "status.json").read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def markers(self) -> set[str]:
        now = self.clock()
        if now - self._markers_at > 300:
            self._markers_at = now
            try:
                self._markers = set(self._markers_fn() or ())
            except Exception:  # noqa: BLE001
                pass
        return self._markers

    # -- filter and batch ----------------------------------------------------------------------------------

    def is_adapter(self, unit: str, msg: str) -> bool:
        """The incident-source adapter's (or a third-party library's) own line, or the runner's about it."""
        if unit == "sizeup-station":
            m = LOG_TAG.match(msg)
            if m and not m.group(1).startswith("sizeup"):
                return True
            if "incident sources:" in msg or "incident source " in msg:
                return True
            if FRAME.match(msg):
                self._skip_frame[unit] = True          # ... and the frame's source line after it
                return True
            if self._skip_frame.pop(unit, False) and msg.startswith("    "):
                return True
        return any(k in msg for k in self.markers())

    def take(self, raw: dict) -> None:
        """One journal entry: kept (into the batch, which is spooled when full) or counted as dropped."""
        e = entry(raw)
        if e is None or self.dir is None:             # (no spool: nothing is kept)
            return
        at, unit, prio, msg = e
        if not self.config.get("adapter") and self.is_adapter(unit, msg):
            self.dropped_adapter += 1
            return
        if not unit_ok(unit, self.config):
            self.dropped_unit += 1
            return
        lv = logscrub.level(prio, msg)
        if not logscrub.at_least(lv, self.config.get("level") or "info"):
            self.dropped_level += 1
            return
        try:
            clean = logscrub.scrub(msg)
        except Exception:  # noqa: BLE001 - never stall the shipping on one line: dropped, counted
            self.dropped_error += 1
            return
        if clean is None:
            self.dropped_secret += 1
            return
        for t, u, lvl, m in self.repeats.allow(unit, clean, lv, at):
            self._add(t, u, lvl, m)

    def _add(self, at: float, unit: str, lv: str, msg: str) -> None:
        if not self.pending:
            self.batch_started = self.clock()
        self.pending.append([int(at * 1000), unit, lv, msg])
        self.pending_bytes += len(msg.encode()) + len(unit) + 32
        if len(self.pending) >= MAX_BATCH_LINES or self.pending_bytes >= MAX_BATCH_BYTES:
            self.spool(force=True)

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

    def _consumed_done(self) -> None:
        for p in self.consumed:
            try:
                p.unlink()
            except OSError:
                pass
        self.consumed = []

    def spool(self, force: bool = False) -> Path | None:
        """The batch so far into the spool, when due or forced; then the hand-off files it came from go."""
        now = self.clock()
        for t, u, lvl, m in self.repeats.flush(now):
            self.pending.append([int(t * 1000), u, lvl, m])
        if not self.pending:
            self._consumed_done()                       # everything read was dropped: nothing to keep
            return None
        due = (force or now - self.batch_started >= BATCH_S or len(self.pending) >= MAX_BATCH_LINES
               or self.pending_bytes >= MAX_BATCH_BYTES)
        if not due or self.dir is None:
            return None
        seq = self.seq + 1
        body = {"v": 1, "station": self.station, "batch": f"{self.station}-{int(now)}-{seq}",
                "lines": sorted(self.pending, key=lambda r: r[0])}
        data = gzip.compress(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(), 6)
        path = self.dir / f"{seq:012d}-{int(now)}.json.gz"
        tmp = self.dir / f".{path.name}.tmp"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:                            # kept in memory: tried again next turn
            self.last_error = f"spool: {type(e).__name__}"
            return None
        self.seq = seq
        self.pending, self.pending_bytes = [], 0
        self._consumed_done()
        self._cap()
        return path

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
        for k in ("on", "adapter"):
            if isinstance(c.get(k), bool):
                out[k] = c[k]
        if c.get("level") in logscrub.LEVELS:
            out["level"] = c["level"]
        for k in ("include", "exclude"):
            v = c.get(k)
            if isinstance(v, list):
                out[k] = [str(u)[:64] for u in v if isinstance(u, str)][:50]
        if out != self.config:
            if out["adapter"] and not self.config.get("adapter"):
                self.log("station logs: the incident-source adapter's lines are shipped now (Settings), scrubbed")
            self.config = out
            if self.dir is not None:
                try:
                    (self.dir / "config.json").write_text(json.dumps(out))
                except OSError:
                    pass

    def _reject(self, p: Path, why: str) -> None:
        """A batch the hub will never take: kept aside (the last few), said once, never sent again."""
        self.rejected += 1
        bad = p.parent / "rejected"
        try:
            bad.mkdir(exist_ok=True)
            os.replace(p, bad / p.name)
            for old in sorted(bad.iterdir())[:-REJECTED_KEEP]:
                old.unlink()
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
        self.log(f"station logs: the hub refused batch {p.name} ({why[:120]}): set aside, not sent again")

    def send(self, now: float | None = None) -> str:
        """The oldest spooled batch to the hub: "sent" | "empty" | "waiting" | "failed" | "rejected" | "not configured"."""
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
            if getattr(e, "status", 0) in REJECT:
                self._reject(p, str(e))
                return "rejected"
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
        """One turn: read the hand-off, batch, send. "off" | "ok" | the reason it can't."""
        now = self.clock() if now is None else now
        if not _env_off() and not self.config.get("on", True):
            self.send(now)              # what was spooled still goes (and the hub's answer may turn it back on)
        if _env_off() or not self.config.get("on", True):
            self.state, self.reason = "off", ("SIZEUP_LOG_SHIP=off" if _env_off() else "off in Settings")
            for p in self._handoff_files():
                try:
                    p.unlink()
                    self.dropped_off += 1
                except OSError:
                    pass
            self.pending, self.pending_bytes, self.consumed = [], 0, []
            return "off"
        files = [p for p in self._handoff_files() if p not in self.consumed][:MAX_FILES_A_TURN]
        if not files and not self.consumed:
            rs = self.reader_status()
            if not self.handoff.is_dir():
                self.state, self.reason = "no journal", "no journal reader here (sizeup-logread)"
            elif rs.get("state") in ("no journal", "full"):
                self.state, self.reason = rs["state"], str(rs.get("reason") or "")[:200]
        for p in files:
            try:
                lines = p.read_text(errors="replace").splitlines()
            except OSError:
                continue
            self.consumed.append(p)
            for line in lines:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if isinstance(raw, dict):
                    self.take(raw)
        self.spool()
        while self.send(now) in ("sent", "rejected"):
            pass
        if self.state in ("starting", "no journal") and files:
            self.state, self.reason = "ok", ""
        return "ok" if self.state not in ("no journal", "full") else self.reason

    def start(self) -> LogShipper:
        if self._thread is None:
            from sizeup import procenv
            procenv.register_secrets()          # the token and the adapter's credentials: never sent (logscrub)
            logscrub.secret(*(str(getattr(self.cfg, k, "") or "") for k in
                              ("cloud_token", "cloud_access_id", "cloud_access_secret", "cloud_status_token")))
            install_log_tags()
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
                "on": bool(self.config.get("on", True)) and not _env_off(), "adapter": bool(self.config.get("adapter")),
                "spool_batches": n, "spool_bytes": size, "spool_max": SPOOL_MAX_BYTES,
                "sent_lines": self.sent_lines, "sent_batches": self.sent_batches, "sent_bytes": self.sent_bytes,
                "dropped_secret": self.dropped_secret, "dropped_level": self.dropped_level,
                "dropped_unit": self.dropped_unit, "dropped_spool": self.dropped_spool,
                "dropped_adapter": self.dropped_adapter, "dropped_error": self.dropped_error,
                "dropped_off": self.dropped_off, "rejected": self.rejected,
                "repeats": self.repeats.suppressed, "last_ok": self.last_ok, "error": self.last_error}
