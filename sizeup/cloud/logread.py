"""The station's journal reader (sizeup-logread.service): the only part of the station that reads the system journal.

It runs as a user of its own (sizeup-logs, in systemd-journal) with the system python3 (stdlib only, like aisd.py),
without network, and hands sizeup-station ONLY the entries of the station's own units (`journalctl -u 'sizeup-*'`): the
web UI and the incident-source adapter, which run as the sizeup user, never get to read sshd, sudo or anything else
in the journal.

    every READ_S   journalctl -o json --after-cursor <its cursor> -u 'sizeup-*' (the first time: the last FIRST_S), at most
                   MAX_READ entries; each entry cut to the fields the shipper uses (FIELDS) and written as one file
                   <dir>/<seq>-<time>.jsonl (0640, group sizeup), then the cursor saved (at least once)
    cap            at most MAX_DIR_BYTES waiting in <dir>: past it, nothing is written (counted in status.json) and the
                   cursor stays where it is, so nothing is lost while sizeup-station is down for a while; after
                   STALE_S of that the oldest files go (a station that never comes back must not fill the disk)
    sizeup-station (sizeup/cloud/logship.py) reads the files, filters, scrubs and spools them, then deletes them

    python3 logread.py run [--dir D]      the service
    python3 logread.py once [--dir D]     one turn (tests, by hand)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DIR = "/var/lib/sizeup-logs"
READ_S = 10.0
FIRST_S = 600.0
MAX_READ = 20000
MAX_DIR_BYTES = 20 * 1024 * 1024
STALE_S = 6 * 3600.0
UNITS = "sizeup-*"
FIELDS = ("__CURSOR", "__REALTIME_TIMESTAMP", "PRIORITY", "_SYSTEMD_UNIT", "UNIT", "SYSLOG_IDENTIFIER", "MESSAGE",
          "CODE_FILE", "CODE_FUNC", "PYTHON_LOGGER", "LOGGER")


def journal(cursor: str, since: float, limit: int) -> list[dict]:
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
                out.append({k: d[k] for k in FIELDS if k in d})
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
    if not out and "insufficient permissions" in (err or "").lower():
        raise RuntimeError("no journal access (sizeup-logs isn't in systemd-journal)")
    if not out and p.returncode not in (0, None, -15) and (err or "").strip():
        raise RuntimeError(err.strip().splitlines()[-1][:200])
    return out


class Reader:
    def __init__(self, d: str | Path = DIR, read=journal, clock=time.time):
        self.dir, self.read, self.clock = Path(d), read, clock
        self.st = {"state": "starting", "reason": "", "written": 0, "held": 0, "dropped_files": 0, "at": None}

    def _files(self) -> list[Path]:
        try:
            return sorted(p for p in self.dir.glob("*.jsonl"))
        except OSError:
            return []

    def _status(self) -> None:
        try:
            tmp = self.dir / ".status.tmp"
            tmp.write_text(json.dumps(self.st))
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.dir / "status.json")
        except OSError:
            pass

    def once(self) -> str:
        now = self.clock()
        self.st["at"] = now
        files = self._files()
        size = sum(p.stat().st_size for p in files if p.exists())
        if size >= MAX_DIR_BYTES:
            old = [p for p in files if now - p.stat().st_mtime > STALE_S]
            for p in old[: max(1, len(old) // 2)]:
                try:
                    p.unlink()
                    self.st["dropped_files"] += 1
                except OSError:
                    pass
            self.st["held"] += 1
            self.st["state"], self.st["reason"] = "full", "sizeup-station isn't taking the logs"
            self._status()
            return "full"
        try:
            cursor = (self.dir / "cursor").read_text().strip()
        except OSError:
            cursor = ""
        try:
            rows = self.read(cursor, now - FIRST_S, MAX_READ)
        except RuntimeError as e:
            self.st["state"], self.st["reason"] = "no journal", str(e)[:200]
            self._status()
            return "no journal"
        self.st["state"], self.st["reason"] = "ok", ""
        if rows:
            last = next((r.get("__CURSOR") for r in reversed(rows) if r.get("__CURSOR")), "")
            seq = int(now * 1000)
            path = self.dir / f"{seq:015d}.jsonl"
            tmp = self.dir / f".{path.name}.tmp"
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            os.chmod(tmp, 0o640)
            os.replace(tmp, path)
            if last:
                c = self.dir / ".cursor.tmp"
                c.write_text(last)
                os.replace(c, self.dir / "cursor")
            self.st["written"] += len(rows)
        self._status()
        return "ok"

    def run(self) -> None:
        while True:
            try:
                self.once()
            except Exception as e:  # noqa: BLE001 - the reader must never die
                print(f"logread: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(READ_S)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="logread.py")
    ap.add_argument("cmd", choices=("run", "once"))
    ap.add_argument("--dir", default=DIR)
    a = ap.parse_args(argv)
    os.umask(0o027)
    r = Reader(a.dir)
    if a.cmd == "once":
        print(r.once())
        return 0
    r.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
