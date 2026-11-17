"""The devices of the app's sign-in (#558, docs/mobile-api.md section 3.3), in hub.sqlite beside the stations' tokens.

Three tables, made on first use by the first write (never at the hub's start: tests/test_startup_writes.py), additive,
named mobile_*: an older release never reads them and a newer one finds them as they are. cad.sqlite is untouched.

    mobile_devices   id, email, name, platform, app_version, client_id, created, last_seen, revoked_at (+ by, why)
    mobile_refresh   sha256 of each refresh token, its device, when it was issued, its idle and absolute limits, and
                     `used` once it has been exchanged (a used token presented again is theft: the device is revoked)
    mobile_codes     sha256 of each authorization code, bound to the PKCE challenge, client id, redirect uri, the Access
                     session and the device name; 60 s, single use (`used` is set the first time it is presented, whether
                     or not the rest checks out), and the device it made (a replay revokes that device)

Everything runs under the MonitorDb's lock in one transaction, so two requests never both spend a code or a refresh
token. A fenced hub task (not the leader, sizeup/fence.py) refuses every write (FencedWrite), which the server answers
as a 503 to try again; reads work anywhere. D6 (#556): everyone signed in and active may enroll; at most MAX_DEVICES
active devices per person (an admin's revoke frees a slot)."""
from __future__ import annotations

import secrets
import threading
from typing import Callable

from .tokens import digest

MAX_DEVICES = 3
CODE_TTL_S = 60
IDLE_S = 30 * 86400              # a refresh token unused this long is dead
ABSOLUTE_S = 180 * 86400         # the device signs in again through Access after this
TOUCH_S = 300                    # last_seen is written at most this often per device
KEEP_REVOKED_S = 30 * 86400      # a revoked device is shown (Settings) this long
NAME_MAX, VERSION_MAX = 60, 32

SCHEMA = """
CREATE TABLE IF NOT EXISTS mobile_devices (
  id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '',
  app_version TEXT NOT NULL DEFAULT '', client_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
  last_seen REAL NOT NULL, revoked_at REAL, revoked_by TEXT NOT NULL DEFAULT '', revoked_why TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS mobile_devices_email ON mobile_devices (email);
CREATE TABLE IF NOT EXISTS mobile_refresh (
  hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, issued REAL NOT NULL, idle_until REAL NOT NULL,
  absolute_until REAL NOT NULL, used REAL);
CREATE INDEX IF NOT EXISTS mobile_refresh_device ON mobile_refresh (device_id);
CREATE TABLE IF NOT EXISTS mobile_codes (
  hash TEXT PRIMARY KEY, email TEXT NOT NULL, session TEXT NOT NULL DEFAULT '', challenge TEXT NOT NULL,
  client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, device_name TEXT NOT NULL DEFAULT '', expires REAL NOT NULL,
  used REAL, device_id TEXT NOT NULL DEFAULT '');
"""
DEVICE_COLUMNS = ("id", "email", "name", "platform", "app_version", "client_id", "created", "last_seen", "revoked_at",
                  "revoked_by", "revoked_why")


class DeviceLimit(Exception):
    """The person already has MAX_DEVICES active devices."""


class DeviceDb:
    """mdb: the hub's MonitorDb (its connection and lock). clock: unix seconds."""

    def __init__(self, mdb, clock: Callable[[], float]):
        self.mdb, self.clock = mdb, clock
        self._ready = False
        self._touched: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- plumbing -------------------------------------------------------------------------------------------------

    def _q(self, sql: str, params=()) -> list[tuple]:
        """A read; [] while the tables don't exist yet (nobody has signed in)."""
        try:
            with self.mdb._lock:
                return self.mdb.db.execute(sql, params).fetchall()
        except Exception as e:                       # sqlite3.OperationalError (not imported here: see test_api_v1)
            if "no such table" in str(e):
                return []
            raise

    def _tx(self, work: Callable):
        """work(db) in one IMMEDIATE transaction under the lock; the tables made first, once."""
        with self.mdb._lock:
            db = self.mdb.db
            if not self._ready:
                db.executescript(SCHEMA)
                self._ready = True
            db.execute("BEGIN IMMEDIATE")
            try:
                out = work(db)
                db.execute("COMMIT")
                return out
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _device(r) -> dict:
        d = dict(zip(DEVICE_COLUMNS, r))
        d["active"] = d["revoked_at"] is None
        return d

    # -- reads ----------------------------------------------------------------------------------------------------

    def device(self, did: str) -> dict | None:
        r = self._q(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices WHERE id = ?", (did,))
        return self._device(r[0]) if r else None

    def devices(self, email: str = "") -> list[dict]:
        """Active devices and the ones revoked in the last KEEP_REVOKED_S, newest first; one person's or everyone's."""
        floor = self.clock() - KEEP_REVOKED_S
        rows = self._q(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices "
                       "WHERE (revoked_at IS NULL OR revoked_at >= ?)" + (" AND email = ?" if email else "")
                       + " ORDER BY created DESC, id", (floor, email) if email else (floor,))
        return [self._device(r) for r in rows]

    def active_count(self, email: str) -> int:
        r = self._q("SELECT COUNT(*) FROM mobile_devices WHERE email = ? AND revoked_at IS NULL", (email,))
        return r[0][0] if r else 0

    def emails_with_devices(self) -> set[str]:
        return {r[0] for r in self._q("SELECT DISTINCT email FROM mobile_devices WHERE revoked_at IS NULL")}

    # -- authorization codes --------------------------------------------------------------------------------------

    def put_code(self, code: str, email: str, session: str, challenge: str, client_id: str, redirect_uri: str,
                 device_name: str) -> None:
        now = self.clock()

        def work(db):
            db.execute("DELETE FROM mobile_codes WHERE expires < ?", (now - 3600,))       # old ones, used or not
            db.execute("INSERT INTO mobile_codes (hash, email, session, challenge, client_id, redirect_uri, "
                       "device_name, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (digest(code), email, session[:64], challenge, client_id, redirect_uri, device_name[:NAME_MAX],
                        now + CODE_TTL_S))
        self._tx(work)

    def take_code(self, code: str) -> tuple[str, dict | None]:
        """Spend a code: ("ok", row) the first time, while it lives; ("replay", row) when it was spent before (the
        device it made, if any, is revoked: its tokens are in the wrong hands or the app replayed them); ("expired",
        None); ("unknown", None). Spent on first presentation, whatever the caller does with the answer."""
        now, h = self.clock(), digest(code)

        def work(db):
            r = db.execute("SELECT email, session, challenge, client_id, redirect_uri, device_name, expires, used, "
                           "device_id FROM mobile_codes WHERE hash = ?", (h,)).fetchone()
            if r is None:
                return "unknown", None
            row = dict(zip(("email", "session", "challenge", "client_id", "redirect_uri", "device_name", "expires",
                            "used", "device_id"), r))
            if row["used"] is not None:
                if row["device_id"]:
                    self._revoke(db, row["device_id"], now, "", "authorization code replayed")
                return "replay", row
            db.execute("UPDATE mobile_codes SET used = ? WHERE hash = ?", (now, h))
            return ("expired", None) if row["expires"] < now else ("ok", row)
        return self._tx(work)

    # -- enrollment and refresh -----------------------------------------------------------------------------------

    def enroll(self, code: str, email: str, name: str, platform: str, app_version: str, client_id: str,
               reuse: str = "") -> tuple[dict, str]:
        """A device for a spent code: (device, refresh token). reuse: the id of this person's own active device the
        app already holds (signing in again at its absolute limit): it is kept, its old refresh tokens dropped, so a
        re-sign-in never costs a slot. DeviceLimit when the person has MAX_DEVICES others."""
        now, token = self.clock(), secrets.token_urlsafe(32)

        def work(db):
            did = ""
            if reuse:
                r = db.execute("SELECT 1 FROM mobile_devices WHERE id = ? AND email = ? AND revoked_at IS NULL",
                               (reuse, email)).fetchone()
                did = reuse if r else ""
            if did:
                db.execute("DELETE FROM mobile_refresh WHERE device_id = ?", (did,))
                db.execute("UPDATE mobile_devices SET name = ?, platform = ?, app_version = ?, client_id = ?, "
                           "last_seen = ? WHERE id = ?", (name[:NAME_MAX], platform, app_version[:VERSION_MAX],
                                                         client_id, now, did))
            else:
                n = db.execute("SELECT COUNT(*) FROM mobile_devices WHERE email = ? AND revoked_at IS NULL",
                               (email,)).fetchone()[0]
                if n >= MAX_DEVICES:
                    raise DeviceLimit(email)
                did = "dv" + secrets.token_hex(8)
                db.execute("INSERT INTO mobile_devices (id, email, name, platform, app_version, client_id, created, "
                           "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (did, email, name[:NAME_MAX], platform, app_version[:VERSION_MAX], client_id, now, now))
            db.execute("INSERT INTO mobile_refresh (hash, device_id, issued, idle_until, absolute_until) "
                       "VALUES (?, ?, ?, ?, ?)", (digest(token), did, now, now + IDLE_S, now + ABSOLUTE_S))
            db.execute("UPDATE mobile_codes SET device_id = ? WHERE hash = ?", (did, digest(code)))
            return self._device(db.execute(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices WHERE id = ?",
                                           (did,)).fetchone())
        return self._tx(work), token

    def rotate(self, token: str, app_version: str = "") -> tuple[str, dict | None, str]:
        """Exchange a refresh token for the next one. (status, device, new token): "ok"; "unknown" (never issued, or
        long gone); "expired" (idle 30 days or absolute 180: sign in again); "revoked" (the device was); "reuse" (it was
        already exchanged: the device is revoked now, the thief's copy and the app's both dead)."""
        now, h, new = self.clock(), digest(token), secrets.token_urlsafe(32)

        def work(db):
            r = db.execute("SELECT device_id, idle_until, absolute_until, used FROM mobile_refresh WHERE hash = ?",
                           (h,)).fetchone()
            if r is None:
                return "unknown", None, ""
            did, idle, absolute, used = r
            dev = db.execute(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices WHERE id = ?", (did,)).fetchone()
            if dev is None:
                return "unknown", None, ""
            d = self._device(dev)
            if d["revoked_at"] is not None:
                return "revoked", d, ""
            if used is not None:
                self._revoke(db, did, now, "", "refresh token reused")
                d = self._device(db.execute(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices WHERE id = ?",
                                            (did,)).fetchone())
                return "reuse", d, ""
            if now > idle or now > absolute:
                return "expired", d, ""
            db.execute("UPDATE mobile_refresh SET used = ? WHERE hash = ?", (now, h))
            db.execute("DELETE FROM mobile_refresh WHERE device_id = ? AND used IS NOT NULL AND issued < ?",
                       (did, now - IDLE_S))                         # the used ones of the idle window are all that count
            db.execute("INSERT INTO mobile_refresh (hash, device_id, issued, idle_until, absolute_until) "
                       "VALUES (?, ?, ?, ?, ?)", (digest(new), did, now, min(now + IDLE_S, absolute), absolute))
            db.execute("UPDATE mobile_devices SET last_seen = ?, app_version = CASE WHEN ? != '' THEN ? "
                       "ELSE app_version END WHERE id = ?", (now, app_version, app_version[:VERSION_MAX], did))
            d["last_seen"] = now
            if app_version:
                d["app_version"] = app_version[:VERSION_MAX]
            return "ok", d, new
        return self._tx(work)

    def device_of_refresh(self, token: str) -> str:
        """The device a refresh token belongs to ("" when unknown): for a logout, which proves the device by holding it."""
        r = self._q("SELECT device_id FROM mobile_refresh WHERE hash = ?", (digest(token),))
        return r[0][0] if r else ""

    # -- revoking -------------------------------------------------------------------------------------------------

    @staticmethod
    def _revoke(db, did: str, now: float, by: str, why: str) -> bool:
        n = db.execute("UPDATE mobile_devices SET revoked_at = ?, revoked_by = ?, revoked_why = ? "
                       "WHERE id = ? AND revoked_at IS NULL", (now, by[:254], why[:120], did)).rowcount
        db.execute("DELETE FROM mobile_refresh WHERE device_id = ?", (did,))
        return n > 0

    def revoke(self, did: str, by: str = "", why: str = "") -> dict | None:
        """The device now revoked (its refresh tokens deleted), or None when there was no such active device."""
        now = self.clock()

        def work(db):
            if not self._revoke(db, did, now, by, why):
                return None
            return self._device(db.execute(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM mobile_devices WHERE id = ?",
                                           (did,)).fetchone())
        if not self._q("SELECT 1 FROM mobile_devices WHERE id = ? AND revoked_at IS NULL", (did,)):
            return None                                    # no write at all for a device that isn't there
        return self._tx(work)

    def revoke_all(self, email: str, by: str = "", why: str = "") -> list[dict]:
        """Every active device of a person (a deactivated user's): the ones revoked."""
        out = []
        for d in self.devices(email):
            if d["active"] and (done := self.revoke(d["id"], by, why)):
                out.append(done)
        return out

    def touch(self, did: str) -> None:
        """last_seen, at most every TOUCH_S per device: bookkeeping (a fenced task skips it, the request goes on)."""
        now = self.clock()
        with self._lock:
            if now - self._touched.get(did, -1e18) < TOUCH_S:
                return
            if len(self._touched) > 4096:
                self._touched.clear()
            self._touched[did] = now
        from sizeup.fence import bookkeeping
        with bookkeeping(), self.mdb._lock:          # one autocommit write: the device is there, so its table is
            self.mdb.db.execute("UPDATE mobile_devices SET last_seen = ? WHERE id = ? AND revoked_at IS NULL",
                                (now, did))
