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
    python -m sizeup.cloud.stations [--state DIR] reset-shared-id [--id ID]    the shared token's station: set, or cleared
--state: the hub's state dir (SIZEUP_STATE_DIR, /data in the task). Meant for an emergency inside the hub task (aws ecs
execute-command): every change refuses unless this task holds the leader's lease now (leader_problem: the supervisor's
<SIZEUP_RUN_DIR>/lease.json; --no-lease only on a hub without the supervisor), and writes the Settings audit too.

Until multi-station (per-station state, outbox cursors, jobs: design A2-A4) the hub takes ONE station, the primary
(legacy_id: the shared token's): another station's events, state, jobs, aircraft and vessels are refused (409, reason
"multi_station_off") and shown on Health. Its hello, status and logs (its own stream) still go through.

Renaming the shared token's station (an admin's shared_id, Settings > Station > Stations: e.g. to name it for where it
is) never locks it out. The hub is the source of truth for the id:
    aliases     the ids it had before (hub.sqlite meta "station_legacy_aliases", at most ALIASES_MAX) are still taken
                from the shared token, as the current id: a station whose [station] id still says the old one keeps
                working, and everything it sends is kept under the new one. They were that token's ids already, so
                this widens nothing; a never-seen id is still refused (id_mismatch). An alias is dropped once the
                station reports its config's id ("config_id" in its status) is the current one (it was reconfigured),
                and all of them when the id is cleared back to "learned"
    adoption    the hub answers each state post with the id it took it as ("station"); the station sends that id from
                then on (sizeup/cloud/station.py StationLink.adopt: its events, status, logs and metrics)
    history     on_rename(old, new): the hub moves what it keys by the id (its status, the station's signal metrics,
                sizeup/cloud/metricstore.py rename_station). The station's CloudWatch streams are <id>-<date>: new ones
                are named for the new id, the old ones stay in the same group (the Logs view reads the group)
    confirm     a rename while the old id was live (RENAME_LIVE_S) is refused unless the request says confirm (the
                card asks; the CLI's --force), and the audit says it was confirmed. A station on its OWN token under the
                old id is refused (multi_station_off) after the rename until it has a token for the new id: the
                confirm says so
A request whose payload names an id the token isn't (id_mismatch) is kept (mismatch()): Health raises "Station
rejected: id mismatch" while it goes on (sizeup/cloud/health.py), and the hub logs it at most once a minute.
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
OTHERS_SHOWN_S = 3600.0     # another station refused (multi-station off): said on Health and the card this long
RENAME_LIVE_S = 600.0       # the shared token's station seen this recently: changing its id asks for confirm
MISMATCH_HOLD_S = 120.0     # an id_mismatch refusal this recent: Health's "Station rejected" holds (the station retries
                            # every few seconds, so a gap this long means it stopped)
ALIASES_MAX = 8             # the shared token's former ids kept (the oldest dropped first)
META_LEGACY = "station_legacy_id"
META_ALIASES = "station_legacy_aliases"
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
    "multi_station_off": "multi-station isn't enabled yet: this hub takes one station",
}


class Refused(Exception):
    """A request the hub won't take as any station. reason: a key of REASONS (fixed text: never the token)."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(REASONS[reason] + (f": {detail}" if detail else ""))
        self.reason = reason


class StationError(ValueError):
    """An admin's request that can't be done (fixed text, safe to show)."""


class ConfirmNeeded(StationError):
    """An admin's change that would affect a live station: done only when the request says confirm (the text is the
    question the card asks)."""


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
        self._others: dict[str, float] = {}       # other stations refused (multi-station off): id -> when
        self._mismatch: dict | None = None        # the last id_mismatch refusal: {said, token, at, since, n}
        self.on_rename: Callable[[str, str], None] | None = None   # the hub's: move what it keys by the id
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
        """The station the shared token is, and until multi-station the one station this hub takes (primary): an id an
        admin set (set_shared_id) or the shared token learned, both in hub.sqlite meta; else [cloud]
        legacy_station_id; "" until then. Read from hub.sqlite each time, so a change from the CLI counts at once."""
        return self.mdb.get(META_LEGACY, "") or self.configured_legacy or self._pinned or ""

    def aliases(self) -> list[str]:
        """The shared token's former ids, still taken from it as the current one (oldest first)."""
        v = self.mdb.get(META_ALIASES, "")
        return [a for a in v.split("\n") if a] if v else []

    def _set_aliases(self, ids: list[str]) -> None:
        ids = list(dict.fromkeys(a for a in ids if a))[-ALIASES_MAX:]
        with self.mdb._lock:
            if ids:
                self.mdb.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                    "value = excluded.value", (META_ALIASES, "\n".join(ids)))
            else:
                self.mdb.db.execute("DELETE FROM meta WHERE key = ?", (META_ALIASES,))

    def live(self, sid: str) -> tuple[str, float] | None:
        """(how, seconds ago) when a station was seen as `sid` within RENAME_LIVE_S: "shared" (the shared token, whose
        station it is) or "own" (its own token); None when not. From this process's memory (the hub), else hub.sqlite,
        written at most every TOUCH_S (the CLI: another process), so from there "recent" is that much longer."""
        if not sid:
            return None
        now = self.clock()
        if sid == self.legacy_id() and self.shared and self.retired() is None:
            used, window = self.shared_last, RENAME_LIVE_S
            if used is None:
                used, window = self.shared_used(), RENAME_LIVE_S + TOUCH_S
            if used is not None and now - used < window:
                return "shared", now - used
        row = self.row(sid) if ID.fullmatch(sid) else None
        if row is not None and row["revoked"] is None:
            mem = self._used.get(sid)
            used, window = (mem, RENAME_LIVE_S) if mem is not None else (row["last_used"], RENAME_LIVE_S + TOUCH_S)
            if used is not None and now - used < window:
                return "own", now - used
        return None

    def rename_question(self, old: str, new: str) -> str | None:
        """The confirm the card asks before the shared token's station changes from old to new (None: nothing live)."""
        if not old or old == new:
            return None
        how = self.live(old)
        if how is None:
            return None
        to = new or "the id it posts next"
        if how[0] == "own":
            return (f"{old} is connected with its own token right now and will be rejected until it's reconfigured to "
                    f"{to} (a token for {to}). Change anyway?")
        return (f"{old} is connected with this token right now. It becomes {to}: the hub keeps taking {old} from it "
                f"and moves its history. Change anyway?" if new else
                f"{old} is connected with this token right now. Its id is learned again from its next post. "
                "Change anyway?")

    def set_shared_id(self, sid: str = "", by: str = "", confirm: bool = False) -> tuple[str, str]:
        """An admin's change of the shared token's station: sid sets it (outranking [cloud] legacy_station_id); "" clears
        a set or learned one, back to the config's or, with none there, learned again from the next post. (old, new).
        The old id stays an alias (still taken from the shared token: never a lockout) and on_rename moves the history.
        ConfirmNeeded while the old id is live (rename_question) unless confirm."""
        sid = str(sid or "").strip()
        if sid and (len(sid) > 64 or not sid.isprintable()):
            raise StationError("id: the station's [station] id, at most 64 characters")
        old = self.legacy_id()
        if sid == old:
            return old, old
        ask = self.rename_question(old, sid or self.configured_legacy)     # ("": back to the config's, or learned)
        if ask and not confirm:
            raise ConfirmNeeded(ask)
        with self._lock:
            self._pinned = None
            with self.mdb._lock:
                if sid:
                    self.mdb.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                        "value = excluded.value", (META_LEGACY, sid))
                else:
                    self.mdb.db.execute("DELETE FROM meta WHERE key = ?", (META_LEGACY,))
            new = self.legacy_id()
            # the old id stays the shared token's (an alias); cleared to "learned", the next post's id is it: none kept
            self._set_aliases([a for a in self.aliases() + [old] if a != new] if new else [])
        self._others.pop(new, None)
        self.log(f"stations: the shared token's station is {new or '(learned from its next post)'} now "
                 f"(was {old or 'none'}), by {by or 'someone'}" + (" (confirmed: it was live)" if ask else ""))
        if old and new and old != new and self.on_rename is not None:
            try:
                self.on_rename(old, new)
            except Exception as e:  # noqa: BLE001 - the id is changed; what couldn't move is said, never a lockout
                self.log(f"stations: {old} -> {new}: history not moved: {type(e).__name__}: {e}")
        return old, new

    def reconfigured(self, config_id) -> None:
        """The shared token's station reports its config's [station] id: once that is the current id, the aliases
        aren't needed (it was reconfigured), so they are dropped."""
        cur = self.legacy_id()
        if isinstance(config_id, str) and cur and config_id == cur and self.aliases():
            dropped = self.aliases()
            self._set_aliases([])
            self.log(f"stations: {cur} is reconfigured: {', '.join(dropped)} no longer taken from the shared token")

    def primary(self, p: Principal) -> str:
        """The one station this hub takes until multi-station (per-station state, cursors, jobs) lands: the shared
        token's. A station on its own token with none known yet becomes it (a hub that never had the shared token)."""
        cur = self.legacy_id()
        if not cur and p.via == "own" and p.id:
            self._pin(p.id)
            cur = self.legacy_id()
        return cur

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
            if c != self.legacy_id():             # another request pinned another id in between: not this one
                raise Refused("id_mismatch")
            return c
        if c != p.id:
            if p.via == "shared" and c in self.aliases():
                return p.id                       # a former id of the shared token's station (renamed): as the current
            raise Refused("id_mismatch")
        return p.id

    def mismatched(self, said: str, token: str) -> dict:
        """An id_mismatch refusal: kept for Health ("Station rejected") and the card. The episode's start is kept while
        they keep coming (each within MISMATCH_HOLD_S of the last)."""
        now = self.clock()
        m = self._mismatch
        since = m["since"] if m and now - m["at"] < MISMATCH_HOLD_S else now
        self._mismatch = {"said": said, "token": token, "at": now, "since": since,
                          "n": (m["n"] + 1) if m and since == m["since"] else 1}
        return self._mismatch

    def mismatch(self, within: float = MISMATCH_HOLD_S) -> dict | None:
        """The id_mismatch refusals going on now (the last one within `within` s): {said, token, at, since, n}."""
        m = self._mismatch
        return dict(m) if m and self.clock() - m["at"] < within else None

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
        # last used: this process's memory (the hub), else hub.sqlite, written at most every TOUCH_S (the CLI: another
        # process), so from there "recent" is that much longer
        used, window = self.shared_last, SHARED_BUSY_S
        if used is None:
            used, window = self.shared_used(), SHARED_BUSY_S + TOUCH_S
        if used is not None and self.clock() - used < window and not force:
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
        meta = self.mdb.get(META_LEGACY, "")
        own = next((r for r in out if r["id"] == legacy and r["revoked"] is None), None) if legacy else None
        return {"at": now, "stations": out,
                "shared": {"configured": bool(self.shared), "station": legacy,
                           "pinned_by": "hub.sqlite" if meta else ("config" if self.configured_legacy else
                                                                   ("first use" if legacy else "")),
                           "config": self.configured_legacy,
                           "retired": self.retired(), "last_used": self.shared_used(),
                           "own_token": own is not None, "aliases": self.aliases()},
                "others": self.others(),
                "mismatch": self.mismatch(),
                "grace_s": ROTATE_GRACE_S}

    def refused_other(self, sid: str) -> None:
        """A station other than the primary was refused (multi-station isn't on yet): for Health and the card."""
        self._others[sid] = self.clock()

    def others(self, within: float = OTHERS_SHOWN_S) -> dict[str, float]:
        """Other stations refused in the last `within` seconds: id -> when (the last time)."""
        now = self.clock()
        return {k: v for k, v in self._others.items() if now - v < within}

    def act(self, body: dict, by: str = "", note: Callable[[str, object, object], None] | None = None) -> dict:
        """POST /api/cad/stations {action: add {id, name?} | rename {id, name} | rotate {id} | revoke {id} |
        retire_shared {force?} | restore_shared | shared_id {id, confirm?}}. The answer of add / rotate carries the token and its status
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
            # grace_s: the old token and its status token keep working this long (none after a revoke)
            return {"ok": True, "id": sid, "token": token, "status_token": status_token(token),
                    "grace_s": 0 if was and was["revoked"] is not None else ROTATE_GRACE_S}
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
        if a == "shared_id":
            sid = str(body.get("id") or "").strip()
            ask = self.rename_question(self.legacy_id(), sid or self.configured_legacy)
            old, new = self.set_shared_id(sid, by, confirm=body.get("confirm") is True)
            if old != new:
                note("stations/shared token id", old or None, (new or "(learned from its next post)")
                     + (f" (confirmed: {old} was connected)" if ask else ""))
            return {"ok": True, "station": new, "aliases": self.aliases()}
        raise StationError("action: add, rename, rotate, revoke, retire_shared, restore_shared or shared_id")


# -- the CLI (an emergency, inside the hub task) ----------------------------------------------------------------------

def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def leader_problem(no_lease: bool = False, env=None) -> str | None:
    """None when this process may write the hub's databases, else why not. The hot/cold supervisor (sizeup/handoff.py)
    writes the leader's lease to <SIZEUP_RUN_DIR>/lease.json (default /tmp/sizeup-run) and hands SIZEUP_LEASE_FILE only
    to its app child, so a shell from `aws ecs execute-command` has to find the file itself. A lease file: it must be
    ours now (fence.Fence.ok, the container's monotonic clock). None at all: refused, unless no_lease (--no-lease: a hub
    without the supervisor, one process, e.g. compose or a Mac) and nothing says this is a supervised task."""
    from pathlib import Path
    from sizeup.fence import Fence
    env = os.environ if env is None else env
    run = Path(env.get("SIZEUP_RUN_DIR", "").strip() or "/tmp/sizeup-run")
    for p in [Path(x) for x in (env.get("SIZEUP_LEASE_FILE", "").strip(),) if x] + [run / "lease.json"]:
        if p.is_file():
            return None if Fence(p, log=lambda s: None).ok() else \
                f"this task isn't the leader (its lease {p} isn't current): run this in the task that is"
    if env.get("LITESTREAM_BUCKET", "").strip() or run.is_dir():
        return (f"no lease at {run / 'lease.json'}: this task can't show it is the leader (a standby, or the supervisor "
                "isn't up): run this in the leader task")
    if not no_lease:
        return "no lease file found: on a hub without the hot/cold supervisor (one process) add --no-lease"
    return None


def _config_legacy(path: str) -> str:
    """[cloud] legacy_station_id from the hub's config (the task: /app/deploy/hub/config.hub.toml), "" if none."""
    try:
        import tomllib
        with open(path, "rb") as f:
            return str((tomllib.load(f).get("cloud") or {}).get("legacy_station_id") or "").strip()
    except (OSError, ValueError):
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sizeup.cloud.stations",
                                 description="The hub's station tokens (docs/cloud.md: How station authentication works)")
    ap.add_argument("--state", default=os.environ.get("SIZEUP_STATE_DIR", ""),
                    help="the hub's state dir, with hub.sqlite (default: SIZEUP_STATE_DIR)")
    ap.add_argument("--by", default=os.environ.get("USER", "") or "cli", help="who is doing this (the audit)")
    ap.add_argument("--config", default=os.environ.get("SIZEUP_CONFIG", "") or "/app/deploy/hub/config.hub.toml",
                    help="the hub's config, for [cloud] legacy_station_id (default: the task's)")
    ap.add_argument("--no-lease", action="store_true",
                    help="a hub without the hot/cold supervisor (one process): no lease file to check")
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
    p = sub.add_parser("reset-shared-id")
    p.add_argument("--id", default="", help="set it to this id (else: back to the config's, or learned)")
    p.add_argument("--force", action="store_true", help="even while its station is connected (it keeps working: the "
                                                         "old id stays taken from the shared token)")
    a = ap.parse_args(argv)
    if not a.state:
        print("stations: --state DIR (or SIZEUP_STATE_DIR): the hub's state dir", file=sys.stderr)
        return 2
    from pathlib import Path
    path = Path(a.state) / "hub.sqlite"
    if not path.is_file():
        print(f"stations: no hub.sqlite in {a.state}", file=sys.stderr)
        return 2
    if a.cmd != "list" and (why := leader_problem(a.no_lease)) is not None:
        print(f"stations: {why}", file=sys.stderr)
        return 3
    from sizeup.cloud.hubstore import MonitorDb
    shared = os.environ.get("SIZEUP_STATION_TOKEN", "").strip()
    reg = StationRegistry(MonitorDb(path), shared=shared, legacy_id=_config_legacy(a.config), log=lambda s: None)
    if a.cmd == "reset-shared-id" and (Path(a.state) / "metrics.sqlite").is_file():
        def moved(old: str, new: str) -> None:      # the station's signal metrics, under the new id (as the hub does)
            from sizeup.cloud.metricstore import MetricStore
            ms = MetricStore(Path(a.state) / "metrics.sqlite", log=lambda s: None)
            try:
                print(f"station metrics: {ms.rename_station(old, new)} row(s) moved from {old} to {new}")
            finally:
                ms.close()
        reg.on_rename = moved

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
                  f"{', retired ' + _when(sh['retired']) if sh['retired'] else ''}; station {sh['station'] or '(not yet)'}"
                  f"{' (also takes ' + ', '.join(sh['aliases']) + ')' if sh['aliases'] else ''}; "
                  f"last used {_when(sh['last_used'])}")
            return 0
        body = {"action": {"retire-shared": "retire_shared", "restore-shared": "restore_shared",
                           "reset-shared-id": "shared_id"}.get(a.cmd, a.cmd),
                "id": getattr(a, "id", None), "name": getattr(a, "name", None), "force": getattr(a, "force", False),
                "confirm": getattr(a, "force", False)}
        out = reg.act(body, a.by, note)
    except ConfirmNeeded as e:
        print(f"stations: {e} (--force)", file=sys.stderr)
        return 1
    except (StationError, sqlite3.Error) as e:
        print(f"stations: {e}", file=sys.stderr)
        return 1
    if out.get("token"):
        print(f"SIZEUP_STATION_TOKEN={out['token']}")
        print(f"status token (read only, the deploy check): {out['status_token']}")
        print("Shown once: put the token in the station's /etc/sizeup/station.env (sizeup-station secrets).", file=sys.stderr)
    elif a.cmd == "reset-shared-id":
        print(f"shared token's station: {out['station'] or '(learned from its next post)'}"
              + (f" (also takes {', '.join(out['aliases'])})" if out.get("aliases") else ""))
    else:
        print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
