"""Who a station is, on the hub (multi-station design A1): each station's own token, the shared token kept for the
migration, and who may post as which station id. docs/cloud.md "How station authentication works" explains it end to
end; this is the code.

Tokens
    sz1.<id>.<secret>   a station's own token: the format version, the station id ([station] id, e.g. "pcfd-hq": lower
                        case letters, digits, - and _, at most 40) and 32 random bytes (secrets.token_urlsafe: 43
                        characters). Made here, shown to the admin ONCE (Settings > Station > Stations, or the CLI
                        below), never stored: hub.sqlite keeps sha256(token) only. The station keeps it in
                        /etc/sizeup/station.env as SIZEUP_STATION_TOKEN, as it keeps the shared one today.
    the shared token    SIZEUP_STATION_TOKEN on the hub (SSM /sizeup-hub/station-token): the one token every request
                        used before this. It still works, as ONE station: [cloud] legacy_station_id, or, unset, the
                        first id it posts as (kept in hub.sqlite meta "station_legacy_id"). Any other id with it is
                        refused, so a new station can't use it. An admin retires it once the station runs on its own.
    status tokens       read only, GET /api/station/status (the deploy check): HMAC-SHA256(token, "sizeup-status-token-v1")
                        of a station's token (health.status_token: the same derivation as the shared one's), or
                        [cloud] status_token / SIZEUP_STATUS_TOKEN. hub.sqlite keeps sha256(status token) only.

Why a plain SHA-256 and not a password hash or a pepper: the secret is 256 random bits, not something a person chose,
so there is nothing to guess. A slow hash protects low-entropy secrets from a stolen database; here a stolen hub.sqlite
(or its replica) gives hashes no one can turn back into a token, and verifying stays a microsecond. Every comparison is
hmac.compare_digest, and an unknown id is compared against a dummy hash, so the time taken says nothing about a token.

Every /api/station/ request goes through Hub._principal (sizeup/cloud/hub.py): the bearer -> a Principal (the station id
it proves). The payload's own id ("station" on events and logs, status "id" on state) must be that id, or be absent (a
station from before this sends none on some posts): another id is refused (401, reason "id_mismatch") and logged once.
Everything after uses the authenticated id (the log stream's name, sizeup/cloud/cwlogs.py; the status "id").

Refusals (401 JSON {error, reason}; the station's log says the reason, sizeup/cloud/client.py):
    no_token        no bearer
    token_unknown   not a token this hub knows (wrong, mistyped, or a rotated one past its grace)
    token_revoked   a station's own token, revoked by an admin (said only to the right token: a guess learns nothing)
    token_retired   the shared token, after an admin retired it
    read_only       a status token on anything but GET /api/station/status
    id_mismatch     the payload names another station than the token's

The registry: hub.sqlite table "stations", made by the first add (never at the hub's start: tests/test_startup_writes.py).
Reads go to the database on each request (an indexed row, cheap), so a revoke from the CLI in another process takes
effect at the next request too. Last used: in memory, written at most every TOUCH_S (bookkeeping: skipped when fenced).

    python -m sizeup.cloud.stations [--state DIR] list
    python -m sizeup.cloud.stations [--state DIR] add <id> [--name NAME]       prints the token and its status token once
    python -m sizeup.cloud.stations [--state DIR] rotate <id>                  ... a new pair; the old one ROTATE_GRACE_S more
    python -m sizeup.cloud.stations [--state DIR] revoke <id>                  refused from its next request
    python -m sizeup.cloud.stations [--state DIR] rename <id> <name>
    python -m sizeup.cloud.stations [--state DIR] retire-shared [--force] | restore-shared
--state: the hub's state dir (SIZEUP_STATE_DIR, /data in the task). Meant for an emergency inside the hub task (aws ecs
execute-command): it refuses on a task that isn't the leader (SIZEUP_LEASE_FILE), and writes the Settings audit too.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from sizeup.cloud.health import status_token

PREFIX = "sz1"
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TOKEN = re.compile(r"sz1\.([a-z0-9][a-z0-9_-]{0,39})\.([A-Za-z0-9_-]{43})")
NAME_MAX = 60
ROTATE_GRACE_S = 600.0      # a rotated token still works this long: time to put the new one in the station's env
TOUCH_S = 300.0             # last used: written to hub.sqlite at most this often per station
SHARED_BUSY_S = 120.0       # the shared token used this recently: Retire asks for force (a station still on it)
META_LEGACY = "station_legacy_id"
META_RETIRED = "station_shared_retired"
META_SHARED_USED = "station_shared_used"
_DUMMY = hashlib.sha256(b"sizeup: no such station").hexdigest()

SCHEMA = """CREATE TABLE IF NOT EXISTS stations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL, status_hash TEXT NOT NULL,
  prev_hash TEXT, prev_status_hash TEXT, prev_until REAL,
  created REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '', rotated REAL, revoked REAL, last_used REAL)"""
COLUMNS = ("id", "name", "hash", "status_hash", "prev_hash", "prev_status_hash", "prev_until", "created", "created_by",
           "rotated", "revoked", "last_used")

REASONS = {
    "no_token": "no station token (Authorization: Bearer)",
    "token_unknown": "unknown station token",
    "token_revoked": "this station's token was revoked (Settings > Station > Stations: Rotate issues a new one)",
    "token_retired": "the shared station token was retired: this station needs its own (Settings > Station > Stations)",
    "read_only": "a status token: it opens GET /api/station/status only",
    "id_mismatch": "the token belongs to another station id than the one this request names",
}


class Refused(Exception):
    """A request the hub won't take as any station. reason: a key of REASONS (fixed text: never the token)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(REASONS[reason] + (f": {detail}" if detail else ""))
        self.reason = reason


class StationError(ValueError):
    """An admin's request that can't be done (fixed text, safe to show)."""


@dataclass(frozen=True)
class Principal:
    """Who a request is. id: the station id it proves ("" = the shared token before it has posted as anyone).
    via: "own" (its sz1 token) | "shared" (the shared token) | "status" (a read-only status token)."""
    id: str
    via: str
    read_only: bool = False


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def make_token(sid: str) -> str:
    return f"{PREFIX}.{sid}.{secrets.token_urlsafe(32)}"


def _same(a: str, b: str | None) -> bool:
    return hmac.compare_digest(a.encode(), (b or _DUMMY).encode())


class StationRegistry:
    """The hub's stations (hub.sqlite "stations") and the shared token's rules. mdb: the hub's MonitorDb."""

    def __init__(self, mdb, shared: str = "", configured_status: str = "", legacy_id: str = "",
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        """shared: the shared token (SIZEUP_STATION_TOKEN); configured_status: [cloud] status_token /
        SIZEUP_STATUS_TOKEN (its own secret: it outlives the shared token's retirement), else the status token is
        derived from the shared token (and retired with it)."""
        self.mdb, self.clock, self.log = mdb, clock, log
        self.shared = shared or ""
        self.configured_status = configured_status or ""
        self.shared_status = status_token(self.shared, self.configured_status)
        self.configured_legacy = legacy_id or ""
        self._pinned: str | None = None           # the learned legacy id, once read or pinned
        self._used: dict[str, float] = {}         # id -> last used (memory)
        self._written: dict[str, float] = {}      # id -> last used as written to hub.sqlite
        self.shared_last: float | None = None
        self._lock = threading.Lock()

    # -- the database -------------------------------------------------------------------------------------------

    def _q(self, sql: str, params=()) -> list[tuple]:
        """A read; [] while the table doesn't exist (nobody has added a station yet)."""
        try:
            with self.mdb._lock:
                return self.mdb.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise

    def _w(self, sql: str, params=()) -> int:
        with self.mdb._lock:
            self.mdb.db.execute(SCHEMA)          # on first use, never at the hub's start
            return self.mdb.db.execute(sql, params).rowcount

    def row(self, sid: str) -> dict | None:
        r = self._q(f"SELECT {', '.join(COLUMNS)} FROM stations WHERE id = ?", (sid,))
        return dict(zip(COLUMNS, r[0])) if r else None

    def rows(self) -> list[dict]:
        return [dict(zip(COLUMNS, r)) for r in self._q(f"SELECT {', '.join(COLUMNS)} FROM stations ORDER BY created, id")]

    def any(self) -> bool:
        return bool(self._q("SELECT 1 FROM stations WHERE revoked IS NULL LIMIT 1"))

    def ingest_on(self) -> bool:
        """Can any station connect: the shared token is set (and not retired), or a station has its own."""
        return (bool(self.shared) and not self.retired()) or self.any()

    # -- the shared token -------------------------------------------------------------------------------------

    def legacy_id(self) -> str:
        """The station the shared token is: [cloud] legacy_station_id, else the id it first posted as ("" until then)."""
        if self.configured_legacy:
            return self.configured_legacy
        if self._pinned is None:
            v = self.mdb.get(META_LEGACY, "")
            if not v:
                return ""
            self._pinned = v
        return self._pinned

    def _pin(self, sid: str) -> None:
        with self._lock:
            if self.legacy_id():
                return
            self._pinned = sid
        self.log(f"stations: the shared token is station {sid!r} from now on (the first id it posted as)")
        try:
            self.mdb.put(META_LEGACY, sid)
        except Exception as e:  # noqa: BLE001 - kept in memory; the next start learns it again
            self.log(f"stations: shared token's station not saved: {type(e).__name__}")

    def retired(self) -> float | None:
        v = self.mdb.get(META_RETIRED, "")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    def shared_used(self) -> float | None:
        if self.shared_last is not None:
            return self.shared_last
        v = self.mdb.get(META_SHARED_USED, "")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    # -- who is this ----------------------------------------------------------------------------------------------

    def resolve(self, bearer: str, read_only: bool = False) -> Principal:
        """The bearer -> a Principal, or Refused. read_only: GET /api/station/status, where status tokens count too."""
        now = self.clock()
        if not bearer:
            raise Refused("no_token")
        if bearer.startswith(PREFIX + "."):
            m = TOKEN.fullmatch(bearer)
            row = self.row(m.group(1)) if m else None
            h = digest(bearer)
            cur = _same(h, row["hash"] if row else None)
            prev = _same(h, row["prev_hash"] if row and (row["prev_until"] or 0) > now else None)
            if not row or not (cur or prev):
                raise Refused("token_unknown")
            if row["revoked"] is not None:
                raise Refused("token_revoked")
            return Principal(row["id"], "own")
        if self.shared and hmac.compare_digest(bearer.encode(), self.shared.encode()):
            if self.retired() is not None:
                raise Refused("token_retired")
            return Principal(self.legacy_id(), "shared")
        # a status token (64 hex digits, or [cloud] status_token): read only
        st = None
        if self.shared_status and hmac.compare_digest(bearer.encode(), self.shared_status.encode()):
            if not self.configured_status and self.retired() is not None:
                raise Refused("token_retired")
            st = Principal(self.legacy_id(), "status", True)
        else:
            h = digest(bearer)
            for sid, sh, ph, until, revoked in self._q(
                    "SELECT id, status_hash, prev_status_hash, prev_until, revoked FROM stations"):
                if _same(h, sh) or ((until or 0) > now and _same(h, ph)):
                    if revoked is not None:
                        raise Refused("token_revoked")
                    st = Principal(sid, "status", True)
                    break
        if st is None:
            raise Refused("token_unknown")
        if not read_only:
            raise Refused("read_only")
        return st

    def claim(self, p: Principal, claimed) -> str:
        """The station id a request may act as: the principal's. claimed: the id the payload names (None / "": an
        older station that sends none). Another id: Refused("id_mismatch"). The shared token's first id pins it."""
        c = str(claimed).strip() if isinstance(claimed, str) else ""
        if not c:
            return p.id
        if p.via == "shared" and not p.id:
            if len(c) > 64 or not c.isprintable():
                raise Refused("id_mismatch", "not a station id")
            self._pin(c)                          # (any id the station has today: own tokens need ID's shape)
            return self.legacy_id()
        if c != p.id:
            raise Refused("id_mismatch")
        return p.id

    def touch(self, p: Principal) -> None:
        """A request went through: last used (memory now, hub.sqlite at most every TOUCH_S, never on a fenced task)."""
        from sizeup import fence as fence_mod
        now = self.clock()
        if p.via == "shared":
            self.shared_last = now
            key = "\0shared"
        elif p.via == "own":
            key = p.id
        else:
            return
        self._used[key] = now
        if now - self._written.get(key, 0.0) < TOUCH_S:
            return
        self._written[key] = now
        try:
            with fence_mod.bookkeeping():
                if key == "\0shared":
                    self.mdb.put(META_SHARED_USED, f"{now:.0f}")
                else:
                    with self.mdb._lock:
                        self.mdb.db.execute("UPDATE stations SET last_used = ? WHERE id = ?", (now, key))
        except Exception as e:  # noqa: BLE001 - bookkeeping: never in the way of the station's request
            self.log(f"stations: last used not saved: {type(e).__name__}")

    def last_used(self, sid: str, row: dict | None = None) -> float | None:
        mem = self._used.get(sid)
        db = (row or self.row(sid) or {}).get("last_used")
        return max(x for x in (mem, db) if x is not None) if (mem or db) else None

    # -- an admin's ----------------------------------------------------------------------------------------------

    @staticmethod
    def _id(sid) -> str:
        sid = str(sid or "").strip()
        if not ID.fullmatch(sid):
            raise StationError("id: lower case letters, digits, - and _ (at most 40), e.g. \"st-north\"")
        return sid

    @staticmethod
    def _name(name) -> str:
        name = " ".join(str(name or "").split())
        if len(name) > NAME_MAX:
            raise StationError(f"name: at most {NAME_MAX} characters")
        return name

    def add(self, sid, name="", by: str = "") -> str:
        """A new station: its token (shown once; only its hash kept)."""
        sid, name = self._id(sid), self._name(name)
        if self.row(sid) is not None:
            raise StationError(f"{sid} exists: Rotate issues it a new token")
        token = make_token(sid)
        try:
            self._w("INSERT INTO stations (id, name, hash, status_hash, created, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, name, digest(token), digest(status_token(token)), self.clock(), by or ""))
        except sqlite3.IntegrityError:
            raise StationError(f"{sid} exists: Rotate issues it a new token") from None
        self.log(f"stations: {sid} added by {by or 'someone'}")
        return token

    def rotate(self, sid, by: str = "") -> str:
        """A new token for sid. The old one keeps working ROTATE_GRACE_S (not after a revoke: then never again);
        a revoked station is active again with the new one."""
        sid = self._id(sid)
        row = self.row(sid)
        if row is None:
            raise StationError(f"no station {sid}")
        token = make_token(sid)
        now = self.clock()
        if row["revoked"] is None:
            prev = (row["hash"], row["status_hash"], now + ROTATE_GRACE_S)
        else:
            prev = (None, None, None)
        self._w("UPDATE stations SET hash = ?, status_hash = ?, prev_hash = ?, prev_status_hash = ?, prev_until = ?, "
                "rotated = ?, revoked = NULL WHERE id = ?",
                (digest(token), digest(status_token(token)), *prev, now, sid))
        self.log(f"stations: {sid} token rotated by {by or 'someone'}")
        return token

    def revoke(self, sid, by: str = "") -> None:
        """Refused from its next request, its previous token too."""
        sid = self._id(sid)
        row = self.row(sid)
        if row is None:
            raise StationError(f"no station {sid}")
        if row["revoked"] is not None:
            raise StationError(f"{sid} is revoked already")
        self._w("UPDATE stations SET revoked = ?, prev_hash = NULL, prev_status_hash = NULL, prev_until = NULL "
                "WHERE id = ?", (self.clock(), sid))
        self.log(f"stations: {sid} revoked by {by or 'someone'}")

    def rename(self, sid, name, by: str = "") -> tuple[str, str]:
        sid, name = self._id(sid), self._name(name)
        row = self.row(sid)
        if row is None:
            raise StationError(f"no station {sid}")
        self._w("UPDATE stations SET name = ? WHERE id = ?", (name, sid))
        return row["name"], name

    def retire_shared(self, by: str = "", force: bool = False) -> None:
        """The shared token refused from now on (the hub keeps it configured: restore_shared undoes this)."""
        if not self.shared:
            raise StationError("no shared token is configured on this hub")
        if self.retired() is not None:
            raise StationError("the shared token is retired already")
        used = self.shared_last
        if used is not None and self.clock() - used < SHARED_BUSY_S and not force:
            raise StationError(f"the shared token was used {int(self.clock() - used)} s ago: a station still runs on it")
        self.mdb.put(META_RETIRED, f"{self.clock():.0f}")
        self.log(f"stations: the shared token retired by {by or 'someone'}")

    def restore_shared(self, by: str = "") -> None:
        if self.retired() is None:
            raise StationError("the shared token isn't retired")
        with self.mdb._lock:
            self.mdb.db.execute("DELETE FROM meta WHERE key = ?", (META_RETIRED,))
        self.log(f"stations: the shared token restored by {by or 'someone'}")

    # -- Settings > Station > Stations -----------------------------------------------------------------------------

    def panel(self, live: Callable[[str], dict] | None = None) -> dict:
        """GET /api/cad/stations: each station (never a hash) and the shared token's state. live(id): {connected}."""
        now = self.clock()
        out = []
        for r in self.rows():
            out.append({"id": r["id"], "name": r["name"], "created": r["created"], "created_by": r["created_by"],
                        "rotated": r["rotated"], "revoked": r["revoked"], "last_used": self.last_used(r["id"], r),
                        "grace_until": r["prev_until"] if (r["prev_until"] or 0) > now else None,
                        **((live or (lambda _sid: {}))(r["id"]))})
        legacy = self.legacy_id()
        own = next((r for r in out if r["id"] == legacy and r["revoked"] is None), None) if legacy else None
        return {"at": now, "stations": out,
                "shared": {"configured": bool(self.shared), "station": legacy,
                           "pinned_by": "config" if self.configured_legacy else ("first use" if legacy else ""),
                           "retired": self.retired(), "last_used": self.shared_used(),
                           "own_token": own is not None},
                "grace_s": ROTATE_GRACE_S}

    def act(self, body: dict, by: str = "", note: Callable[[str, object, object], None] | None = None) -> dict:
        """POST /api/cad/stations {action: add {id, name?} | rename {id, name} | rotate {id} | revoke {id} |
        retire_shared {force?} | restore_shared}. The answer of add / rotate carries the token and its status
        token: the only time they exist outside the station. note(key, old, new): the Settings audit (never a token)."""
        a = str(body.get("action") or "")
        note = note or (lambda k, o, n: None)
        if a == "add":
            token = self.add(body.get("id"), body.get("name"), by)
            sid = self._id(body.get("id"))
            note(f"stations/{sid}", None, f"added ({self._name(body.get('name')) or 'no name'})")
            return {"ok": True, "id": sid, "token": token, "status_token": status_token(token)}
        if a == "rotate":
            sid = self._id(body.get("id"))
            was = self.row(sid)
            token = self.rotate(sid, by)
            note(f"stations/{sid}", "revoked" if was and was["revoked"] is not None else "token in use",
                 "new token" + ("" if was and was["revoked"] is not None else f" (old one {int(ROTATE_GRACE_S // 60)} min more)"))
            return {"ok": True, "id": sid, "token": token, "status_token": status_token(token),
                    "grace_s": ROTATE_GRACE_S}
        if a == "revoke":
            sid = self._id(body.get("id"))
            self.revoke(sid, by)
            note(f"stations/{sid}", "active", "revoked")
            return {"ok": True, "id": sid}
        if a == "rename":
            sid = self._id(body.get("id"))
            old, new = self.rename(sid, body.get("name"), by)
            note(f"stations/{sid}/name", old, new)
            return {"ok": True, "id": sid}
        if a == "retire_shared":
            self.retire_shared(by, force=body.get("force") is True)
            note("stations/shared token", "in use", "retired")
            return {"ok": True}
        if a == "restore_shared":
            self.restore_shared(by)
            note("stations/shared token", "retired", "restored")
            return {"ok": True}
        raise StationError("action: add, rename, rotate, revoke, retire_shared or restore_shared")


# -- the CLI (an emergency, inside the hub task) ----------------------------------------------------------------------

def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sizeup.cloud.stations",
                                 description="The hub's station tokens (docs/cloud.md: How station authentication works)")
    ap.add_argument("--state", default=os.environ.get("SIZEUP_STATE_DIR", ""),
                    help="the hub's state dir, with hub.sqlite (default: SIZEUP_STATE_DIR)")
    ap.add_argument("--by", default=os.environ.get("USER", "") or "cli", help="who is doing this (the audit)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("id")
    p.add_argument("--name", default="")
    sub.add_parser("rotate").add_argument("id")
    sub.add_parser("revoke").add_argument("id")
    p = sub.add_parser("rename")
    p.add_argument("id")
    p.add_argument("name")
    sub.add_parser("retire-shared").add_argument("--force", action="store_true")
    sub.add_parser("restore-shared")
    a = ap.parse_args(argv)
    if not a.state:
        print("stations: --state DIR (or SIZEUP_STATE_DIR): the hub's state dir", file=sys.stderr)
        return 2
    from pathlib import Path
    path = Path(a.state) / "hub.sqlite"
    if not path.is_file():
        print(f"stations: no hub.sqlite in {a.state}", file=sys.stderr)
        return 2
    if a.cmd != "list":
        from sizeup.fence import Fence
        f = Fence.from_env()
        if f is not None and not f.ok():
            print("stations: this task isn't the leader (SIZEUP_LEASE_FILE): run it in the task that is", file=sys.stderr)
            return 3
    from sizeup.cloud.hubstore import MonitorDb
    shared = os.environ.get("SIZEUP_STATION_TOKEN", "").strip()
    reg = StationRegistry(MonitorDb(path), shared=shared, log=lambda s: None)

    def note(key, old, new) -> None:
        cad = Path(a.state) / "cad.sqlite"
        if not cad.is_file():
            return
        db = sqlite3.connect(cad, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout = 5000")
            db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                       (time.time(), f"{a.by} (CLI)", "system", key, "" if old is None else str(old),
                        "" if new is None else str(new)))
        except sqlite3.Error as e:
            print(f"stations: not in the Settings audit ({type(e).__name__})", file=sys.stderr)
        finally:
            db.close()
    try:
        if a.cmd == "list":
            pan = reg.panel()
            for s in pan["stations"]:
                state = f"revoked {_when(s['revoked'])}" if s["revoked"] else "active"
                print(f"{s['id']:<20} {s['name'] or '-':<24} {state:<28} last used {_when(s['last_used'])}")
            sh = pan["shared"]
            print(f"shared token: {'configured' if sh['configured'] else 'not configured'}"
                  f"{', retired ' + _when(sh['retired']) if sh['retired'] else ''}; station {sh['station'] or '(not yet)'}; "
                  f"last used {_when(sh['last_used'])}")
            return 0
        body = {"action": {"retire-shared": "retire_shared", "restore-shared": "restore_shared"}.get(a.cmd, a.cmd),
                "id": getattr(a, "id", None), "name": getattr(a, "name", None), "force": getattr(a, "force", False)}
        out = reg.act(body, a.by, note)
    except (StationError, sqlite3.Error) as e:
        print(f"stations: {e}", file=sys.stderr)
        return 1
    if out.get("token"):
        print(f"SIZEUP_STATION_TOKEN={out['token']}")
        print(f"status token (read only, the deploy check): {out['status_token']}")
        print("Shown once: put the token in the station's /etc/sizeup/station.env (sizeup-station secrets).", file=sys.stderr)
    else:
        print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
