"""Who did what, per person, on the hub (docs/cloud.md "Activity log").

user, 2026-10-08: "lets add some per user logging too"

One history per person signed in through Cloudflare Access (sizeup/cloud/access.py), in the CAD
database (user_activity, so Litestream backs it up with everything else):

    signin    once per Access session (Identity.session: the token's jti / nonce / iat), with the
              address and browser it came from: not every request
    action    every write that went through (sizeup/cad/api.py CadApi.post, and the server's own
              POSTs that permit() covers), as a sentence: "Dispatched 26-000014: E60, L31"
    refused   every 403 from role enforcement (and the last-admin guard's 409)

and per person when they were last seen and from where (operators.last_seen / last_ip / last_ua),
written at most once a minute each. Every row is also one line in the server log (CloudWatch).
Kept 90 days: older rows are dropped on open and once a day. All-in-one (no Access, nobody
verified) writes nothing here: the chronology and the settings audit are as they were.
"""

from __future__ import annotations

import threading
from typing import Callable

from sizeup.cad.export import _csv, ts

KINDS = ("signin", "action", "refused")
KEEP_S = 90 * 86400            # rows older than this are dropped
PRUNE_EVERY_S = 86400
SEEN_EVERY_S = 60              # operators.last_seen: at most this often per person (no write per request)
UA_MAX = 120
SUMMARY_MAX = 300
ROUTE_MAX = 200
MAX_ROWS = 2000                # one GET /api/cad/activity
CSV_MAX_ROWS = 200_000         # the CSV: everything kept
CSV_COLUMNS = ["Time", "Name", "Email", "Kind", "Summary", "Incident #", "OK", "IP", "Browser", "Route"]


class Activity:
    """One per Cad (sizeup/cad/service.py). Writes go straight to the store under its lock, outside
    any transaction of its own (no store.rev bump: nothing on the boards changed)."""

    def __init__(self, store, log: Callable[[str], None] = print, clock: Callable[[], float] | None = None):
        self.store, self.log = store, log
        self.clock = clock or store.clock
        self._lock = threading.Lock()
        self._sessions: set[tuple[str, str]] = set()   # (email, Access session) already logged
        self._seen: dict[str, float] = {}        # email -> when last_seen was last written
        self._pruned = -1e18
        self.prune()

    # -- writing ------------------------------------------------------------------------------------------

    def record(self, email: str, kind: str, route: str = "", summary: str = "", number: str | None = None,
               ok: bool = True, ip: str = "", ua: str = "", session: str = "") -> None:
        if not email or kind not in KINDS:
            return
        now = self.clock()
        summary, route, ip, ua = summary[:SUMMARY_MAX], route[:ROUTE_MAX], ip[:64], ua[:UA_MAX]
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO user_activity (at, email, kind, route, summary, incident_number, ok, ip, ua, session) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, email, kind, route, summary, number or None, int(ok), ip, ua, session[:64]))
        self.log(f"CAD activity: {email}: {summary}{f' [{number}]' if number else ''} ({route}) ip={ip or '-'}"
                 + (f" ua={ua[:60]}" if kind == "signin" else ""))
        self._maybe_prune(now)

    def visit(self, email: str, session: str, ip: str = "", ua: str = "", route: str = "") -> None:
        """Every verified request (sizeup/web/server.py): a new Access session is one signin row; and
        operators.last_seen, at most once a minute."""
        if not email:
            return
        now = self.clock()
        with self._lock:              # a set, not one per person: two devices signed in don't take turns
            new = (email, session) not in self._sessions
            due = now - self._seen.get(email, -1e18) >= SEEN_EVERY_S
            if new:
                self._sessions.add((email, session))
        if new and not self.store.one("SELECT 1 AS x FROM user_activity WHERE email = ? AND kind = 'signin' AND session = ?",
                                      (email, session[:64])):      # a restart doesn't sign everyone in again
            self.record(email, "signin", route, "Signed in", ip=ip, ua=ua, session=session)
        if new or due:
            with self.store.lock:
                n = self.store.db.execute("UPDATE operators SET last_seen = ?, last_ip = ?, last_ua = ? WHERE email = ?",
                                          (now, ip[:64], ua[:UA_MAX], email)).rowcount
            if n:          # not on the list yet (named on their first whoami): try again next request
                with self._lock:
                    self._seen[email] = now

    def prune(self) -> int:
        now = self.clock()
        self._pruned = now
        with self.store.lock:
            n = self.store.db.execute("DELETE FROM user_activity WHERE at < ?", (now - KEEP_S,)).rowcount
        if n:
            self.log(f"CAD activity: dropped {n} rows older than {KEEP_S // 86400} days")
        return n

    def _maybe_prune(self, now: float) -> None:
        if now - self._pruned >= PRUNE_EVERY_S:
            self.prune()

    # -- reading ------------------------------------------------------------------------------------------

    def rows(self, email: str = "", kind: str = "", since: float | None = None, limit: int = 200) -> list[dict]:
        """Newest first, each with the person's name (Settings > Users) when they have one."""
        where, args = [], []
        if email:
            where.append("a.email = ?")
            args.append(email)
        if kind:
            where.append("a.kind = ?")
            args.append(kind)
        if since is not None:
            where.append("a.at >= ?")
            args.append(since)
        rows = self.store.q("SELECT a.id, a.at, a.email, COALESCE(o.name, '') AS name, a.kind, a.route, a.summary, "
                            "a.incident_number, a.ok, a.ip, a.ua FROM user_activity a "
                            "LEFT JOIN operators o ON o.email = a.email AND a.email != '' "
                            + (f"WHERE {' AND '.join(where)} " if where else "")
                            + "ORDER BY a.at DESC, a.id DESC LIMIT ?", (*args, max(0, min(limit, CSV_MAX_ROWS))))
        for r in rows:
            r["ok"] = bool(r["ok"])
        return rows

    def seen(self, email: str = "") -> list[dict]:
        """[{email, name, last_seen, last_ip, last_ua}]: everyone with a sign-in email, or one person."""
        return self.store.q("SELECT email, name, last_seen, last_ip, last_ua FROM operators WHERE email != ''"
                            + (" AND email = ?" if email else "") + " ORDER BY name COLLATE NOCASE",
                            (email,) if email else ())

    @staticmethod
    def csv(rows: list[dict]) -> bytes:
        return _csv(CSV_COLUMNS, [[ts(r["at"]), r["name"] if r["name"] != r["email"] else "", r["email"], r["kind"],
                                   r["summary"], r["incident_number"] or "", "yes" if r["ok"] else "no", r["ip"], r["ua"],
                                   r["route"]] for r in rows])
