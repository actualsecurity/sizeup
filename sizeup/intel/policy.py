"""Whether an event change is pushed: the mode ladder, the kill switch, quiet hours and the rate caps. A verdict only
(model.Decision); the live sink sends.

Settings (section "intel", sizeup/intel/settings.py), with these defaults (user, 2026-11-12: every category to the
test pre-alert topic from day one, caps generous for testing):

    enabled            True     the whole pipeline (off: nothing is detected)
    push               True     the kill switch: off, detection and the map go on, nothing is pushed
    per_hour           30       pushes an hour, every pack together
    per_day            200      ... a day
    per_category_hour  10       ... an hour of one category
    repush_min         10       an escalation (higher level, a place found or confirmed) pushes again at most this often
    max_repush         2        ... and at most this many times per event
    merge_min          20       an open event of the same pack, category and source this recent takes a new mention
                                ("still smoke showing", a repeated "shots fired"): updated, not pushed again
    mute_min           15       after an event ends ("disregard") or is dismissed, a new report of it this soon (the
                                same place, or no place on the same source) re-opens it and pushes again
    move_min           5        a moving incident (a pursuit, "now at", "last seen at") pushes its new place at most
                                this often
    place_repush_min   5        the place first heard within this long after the first push pushes once more,
                                whatever repush_min says (the read-back follows the call-out by a minute or two)
    max_age_min        15       a transmission older than this (a startup backfill, a late re-record) is never a
                                pre-alert: a restart doesn't replay old pushes
    quiet_from / quiet_to  ""   "HH:MM" local: no pushes in between (log and map go on); "" = none
    listen             True     a "Listen" link on each push (sizeup/intel/listen.py), playable listen_expiry_min (20),
                                listen_before_s (45) before the first transmission to listen_after_s (60) after the
                                last, at listen_kbps (24)

Over a cap the event is kept (and shown) with suppressed "rate"; a digest of them goes out every digest_min (15) at
priority 2, counts only.
"""

from __future__ import annotations

from datetime import datetime

from sizeup.intel.model import Decision, Event, mode_rank

DEFAULTS = {"enabled": True, "push": True, "per_hour": 30, "per_day": 200, "per_category_hour": 10, "repush_min": 10,
            "max_repush": 2, "quiet_from": "", "quiet_to": "", "digest_min": 15, "max_age_min": 15,
            "merge_min": 20, "mute_min": 15, "place_repush_min": 5, "move_min": 5,
            # the push's "Listen" link (sizeup/intel/listen.py): on, minutes it plays, seconds around the alert, bitrate
            "listen": True, "listen_expiry_min": 20, "listen_before_s": 45, "listen_after_s": 60, "listen_kbps": 24}
BOOLS = ("enabled", "push", "listen")
LIMITS = {"per_hour": (1, 1000), "per_day": (1, 10000), "per_category_hour": (1, 1000), "repush_min": (0, 1440),
          "max_repush": (0, 20), "digest_min": (1, 1440), "max_age_min": (1, 1440),
          "merge_min": (1, 240), "mute_min": (0, 240), "place_repush_min": (0, 60), "move_min": (1, 120), "listen_expiry_min": (1, 720),
          "listen_before_s": (0, 600), "listen_after_s": (0, 600), "listen_kbps": (16, 64)}
LEVELS = ((0.8, "high"), (0.6, "medium"), (0.0, "low"))


def level(score: float) -> str:
    return next(name for lo, name in LEVELS if score >= lo)


def level_rank(lv: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(lv, 0)


def settings(ov: dict | None) -> dict:
    """The "intel" section over the defaults (bad values: the default)."""
    out = dict(DEFAULTS)
    for k, v in (ov or {}).items():
        if k in BOOLS and isinstance(v, bool):
            out[k] = v
        elif k in LIMITS and isinstance(v, (int, float)) and not isinstance(v, bool) and LIMITS[k][0] <= v <= LIMITS[k][1]:
            out[k] = int(v)
        elif k in ("quiet_from", "quiet_to") and isinstance(v, str) and (v == "" or _hm(v) is not None):
            out[k] = v
        elif k not in DEFAULTS:
            out[k] = v
    return out


def _hm(s: str) -> int | None:
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, AttributeError):
        return None


def quiet(cfg: dict, now: float, tz=None) -> bool:
    a, b = _hm(cfg.get("quiet_from") or ""), _hm(cfg.get("quiet_to") or "")
    if a is None or b is None or a == b:
        return False
    t = datetime.fromtimestamp(now, tz)
    m = t.hour * 60 + t.minute
    return a <= m < b if a < b else (m >= a or m < b)


class Policy:
    """The verdicts, with the push bucket (when each push went, by category) kept here: state() / restore() carry it
    across a handover (the live sink keeps it in the store's meta)."""

    def __init__(self, cfg: dict | None = None, tz=None):
        self.cfg = settings(cfg)
        self.tz = tz
        self.sent: list[tuple[float, str]] = []          # (at, category) of each push, the last day
        self.held: list[int] = []                        # event ids over a cap since the last digest
        self.digest_at = 0.0

    def configure(self, cfg: dict | None) -> None:
        self.cfg = settings(cfg)

    def state(self) -> dict:
        return {"sent": self.sent[-2000:], "held": self.held[-200:], "digest_at": self.digest_at}

    def restore(self, d: dict | None) -> None:
        if isinstance(d, dict):
            self.sent = [(float(a), str(c)) for a, c in d.get("sent") or [] if isinstance(a, (int, float))]
            self.held = [int(x) for x in d.get("held") or [] if isinstance(x, int)]
            self.digest_at = float(d.get("digest_at") or 0.0)

    def _over(self, cat: str, now: float) -> bool:
        self.sent = [(a, c) for a, c in self.sent if a > now - 86400]
        hour = [(a, c) for a, c in self.sent if a > now - 3600]
        return (len(self.sent) >= self.cfg["per_day"] or len(hour) >= self.cfg["per_hour"]
                or sum(1 for _a, c in hour if c == cat) >= self.cfg["per_category_hour"])

    def decide(self, ev: Event, change: str, now: float, before: Event | None = None, moved: bool = False) -> Decision:
        """The verdict for this change of ev (before: the event as it was, for an update). moved: a moving incident's
        new place (at most one push per move_min). "reopened": a new report right after a disregard: pushed again."""
        prio = int(ev.priority)
        if mode_rank(ev.mode) < mode_rank("push"):
            return Decision(ev.mode, False, "shadow" if ev.mode == "shadow" else "", prio)
        if change not in ("raised", "updated", "reopened") or ev.status != "open":
            return Decision(ev.mode, False, "", prio)
        repush = False
        if moved and ev.pushed_at is not None:
            if now - ev.pushed_at < self.cfg["move_min"] * 60 or ev.push_count > self.cfg["max_repush"]:
                return Decision(ev.mode, False, "merged", prio)        # a move counts toward max_repush too
            repush = True
        elif change == "reopened":
            if ev.pushed_at is not None and (now - ev.pushed_at < self.cfg["repush_min"] * 60
                                             or ev.push_count > self.cfg["max_repush"]):
                return Decision(ev.mode, False, "merged", prio)        # re-opens obey repush_min and max_repush
            repush = ev.pushed_at is not None
        elif change == "updated" or ev.pushed_at is not None:
            if ev.pushed_at is None:
                pass                              # never pushed (held, quiet): an update may push it now
            elif before is None or not escalated(before, ev):
                return Decision(ev.mode, False, "", prio)
            elif ev.push_count > self.cfg["max_repush"]:
                return Decision(ev.mode, False, "merged", prio)
            elif now - ev.pushed_at < self.cfg["repush_min"] * 60 and not self.place_found(before, ev, now):
                return Decision(ev.mode, False, "merged", prio)
            else:
                repush = True
        if not self.cfg["push"]:
            return Decision(ev.mode, False, "off", prio)
        if quiet(self.cfg, now, self.tz):
            return Decision(ev.mode, False, "quiet", prio)
        if self._over(ev.category, now):
            if ev.id is not None and ev.id not in self.held:
                self.held.append(ev.id)
            return Decision(ev.mode, False, "rate", prio)
        return Decision(ev.mode, True, "", prio, repush)

    def place_found(self, before: Event, ev: Event, now: float) -> bool:
        """The place first heard soon after the first push (the read-back): one more push, whatever repush_min says.
        Chosen over holding the first push for a read-back: the call-out's push is never late, and it costs at most
        one more push per event (#492 review, M1)."""
        return (before.precision == "none" and ev.precision != "none" and ev.push_count == 1
                and ev.pushed_at is not None and now - ev.pushed_at <= self.cfg["place_repush_min"] * 60)

    def may_digest(self, now: float) -> bool:
        """The digest obeys what a push obeys: enabled, the kill switch, quiet hours."""
        return bool(self.cfg["enabled"]) and bool(self.cfg["push"]) and not quiet(self.cfg, now, self.tz)

    def sent_now(self, ev: Event, now: float) -> None:
        self.sent.append((now, ev.category))

    def digest_due(self, now: float) -> list[int]:
        """The held event ids, when a digest is due (and forgets them); else []."""
        if self.held and not (self.cfg["enabled"] and self.cfg["push"]):
            self.held = []                        # off: nothing held is ever sent later
            return []
        if not self.held or now - self.digest_at < self.cfg["digest_min"] * 60 or not self.may_digest(now):
            return []
        out, self.held, self.digest_at = self.held, [], now
        return out


def escalated(before: Event, ev: Event) -> bool:
    """Worth another push: a higher level, a higher priority, a place first found, or first confirmed."""
    return (level_rank(ev.level) > level_rank(before.level) or ev.priority > before.priority
            or (before.precision == "none" and ev.precision != "none")
            or (not before.confirmed and ev.confirmed and ev.precision != "none"))
