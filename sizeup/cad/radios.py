"""Radio IDs -> rigs: which unit (the CAD's board code, "E63") a transmission came from.

    FG1 (analog)  MDC-1200 PTT-ID, 4 hex digits as decoded ("6301", "0611")    system "mdc"
    county P25    the radio's numeric ID ("41228") and the talker alias the     system "p25"
                  system sends with it ("77A4", "E60", "60 Ctrl OP 3")

The registry is the `radios` table in the CAD database, seeded once per version of
data/radio_ids.json: the IDs the department confirmed, the IDs heard on the air, and guesses
from the user's numbering pattern (new APX radios <unit number>01-10, old ones 0<unit number>1-9).
An ID two units would both get by the pattern (M40 / R40 -> 4001) is left unmapped and listed
as a conflict; a unit the pattern doesn't fit (TL2, U239, the chiefs) is listed too.

P25 needs no table to work: a talker alias that is a board code ("E60", "2398", "77A4") or starts
with one ("E60 P2") names that unit. Every (radio ID, alias) pair heard is kept, so a radio heard
once with its alias is known later without it. A person's edit in the Radios panel always wins
over what is heard (rows they touched are never overwritten) and every edit goes in settings_audit
(who, section 'radios', key 'mdc:0611/unit', old -> new).
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SEED = ROOT / "data" / "radio_ids.json"
SYSTEMS = ("mdc", "p25")
SOURCES = ("confirmed", "guessed", "heard")
PCFD_KINDS = ("chief", "engine", "ladder", "tower", "rescue", "utility", "marine")
DISPATCHER = re.compile(r"\bCtrl\b|\bControl\b|\bDispatch", re.I)
MDC_ID = re.compile(r"[0-9A-F]{4}")
P25_ID = re.compile(r"\d{1,9}")
HEARD_WRITE_S = 60.0        # last-heard times are written at most this often per radio


def audit(db, at: float, operator: str, section: str, key: str, old: str, new: str) -> None:
    """One row of settings_audit (shared by every Settings section): who changed what, old -> new."""
    db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
               (at, operator, section, key, old, new))


def audit_log(store, section: str = "", limit: int = 100) -> list[dict]:
    if section:
        return store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY at DESC, id DESC LIMIT ?", (section, limit))
    return store.q("SELECT * FROM settings_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,))


class RadioError(ValueError):
    """A registry edit that can't be done as asked (a 400 with the reason)."""


def is_pcfd(u: dict) -> bool:
    """PCFD's own apparatus and chiefs (not Rye Brook's, mutual aid, PD, EMS or agencies)."""
    return (u.get("board") == "main" and not u.get("department") and not u.get("mutual_aid")
            and u.get("kind") in PCFD_KINDS)


def is_dispatcher(alias: str) -> bool:
    return bool(DISPATCHER.search(alias or ""))


def load_seed(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or SEED).read_text())


def _ranges(ids: list[str]) -> str:
    """['4001', ..., '4010'] -> '4001–4010' (consecutive runs joined)."""
    out, run = [], []
    for i in sorted(ids):
        if run and i.isdigit() and run[-1].isdigit() and int(i) == int(run[-1]) + 1 and len(i) == len(run[-1]):
            run.append(i)
        else:
            if run:
                out.append(run[0] if len(run) == 1 else f"{run[0]}–{run[-1]}")
            run = [i]
    if run:
        out.append(run[0] if len(run) == 1 else f"{run[0]}–{run[-1]}")
    return ", ".join(out)


def plan(seed: dict, units: list[dict]) -> dict:
    """What the seed file and the boards say: {guesses: {id: (unit, label, pattern)}, conflicts: [...],
    unfit: [...], confirmed: [...], heard: [...]} (MDC only: the pattern is about code-plug IDs)."""
    claims: dict[str, list[tuple[str, str, str]]] = {}
    unfit: list[dict] = []
    kinds = set(seed.get("pattern_kinds", []))
    skip = seed.get("not_guessed_kinds", {})
    for u in units:
        if not is_pcfd(u):
            continue
        code = u["name"]
        if u["kind"] not in kinds:
            unfit.append({"unit": code, "reason": skip.get(u["kind"], f"{u['kind']}: not in the pattern")})
            continue
        m = re.fullmatch(r"[A-Z]+(\d+)", code)
        if not m:
            unfit.append({"unit": code, "reason": "no unit number in the code"})
            continue
        n = m.group(1)
        for p in seed.get("patterns", []):
            ids = [(p["format"].format(n=n, k=k), p["label"].format(k=k)) for k in range(p["first"], p["last"] + 1)]
            if any(len(i) != 4 for i, _ in ids):
                unfit.append({"unit": code, "reason": f"the {p['name']} pattern gives {ids[0][0]}…{ids[-1][0]}, "
                                                      f"not 4-digit IDs"})
                continue
            for i, label in ids:
                claims.setdefault(i, []).append((code, label, p["name"]))
    guesses = {i: c[0] for i, c in claims.items() if len(c) == 1}
    clash: dict[tuple, list[str]] = {}
    for i, c in claims.items():
        if len(c) > 1:
            clash.setdefault((tuple(sorted({u for u, _, _ in c})), c[0][2]), []).append(i)
    conflicts = [{"units": list(us), "pattern": pat, "ids": _ranges(ids), "count": len(ids),
                  "reason": f"{' and '.join(us)} would both get these IDs: left unmapped"}
                 for (us, pat), ids in sorted(clash.items())]
    confirmed = [dict(c) for c in seed.get("confirmed", [])]
    for c in confirmed:          # a confirmed ID inside another unit's guessed range: say so
        g = guesses.get(c["id"]) if c.get("system") == "mdc" else None
        if g and g[0] != c.get("unit"):
            conflicts.append({"units": [g[0], c.get("unit") or "(no unit)"], "pattern": g[2], "ids": c["id"],
                              "count": 1, "reason": f"confirmed as {c.get('unit') or c.get('label')}, "
                                                    f"inside {g[0]}'s guessed range"})
    return {"guesses": guesses, "conflicts": conflicts, "unfit": unfit, "confirmed": confirmed,
            "heard": [dict(h) for h in seed.get("heard", [])], "version": int(seed.get("version", 1))}


class Registry:
    """The `radios` table, cached in memory (resolving happens on every transmission and every
    trunk-recorder log line). Writes go through the store's lock."""

    def __init__(self, store, seed_path: str | Path | None = None, clock=None):
        self.store = store
        self.clock = clock or store.clock
        self.seed_path = Path(seed_path) if seed_path else SEED
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], dict] = {}
        self._written: dict[tuple[str, str], float] = {}
        self.refresh_units()
        try:
            self.seed_data = load_seed(self.seed_path)
        except (OSError, ValueError):
            self.seed_data = {}
        self.plan = plan(self.seed_data, self.units)
        self._reload()

    # -- units ------------------------------------------------------------------------------------

    def refresh_units(self) -> None:
        units = self.store.q("SELECT name, kind, department, mutual_aid, board, spoken FROM units")
        self.units = [dict(u, mutual_aid=bool(u["mutual_aid"])) for u in units]
        self._by_code = {u["name"].upper(): u["name"] for u in self.units if u["board"]}
        self._spoken = sorted(((u["spoken"].lower(), u["name"]) for u in self.units
                               if u["board"] and u["spoken"] and " " in u["spoken"]), key=lambda x: -len(x[0]))
        self.pcfd = [u["name"] for u in self.units if is_pcfd(u)]

    def unit_for_alias(self, alias: str) -> tuple[str, str] | None:
        """(unit code, the rest: a position) when a talker alias names a unit on the boards:
        'E60' -> E60, 'E60 P2' -> (E60, 'P2'), 'Engine 61 Portable 1' -> (E61, 'Portable 1'),
        '2398' -> 2398. '2243 P1', 'BUS527', '60 Ctrl OP 3' -> None."""
        a = " ".join(str(alias or "").split())
        if not a or is_dispatcher(a):
            return None
        first, _, rest = a.partition(" ")
        code = self._by_code.get(first.upper())
        if code:
            return code, rest
        low = a.lower()
        for spoken, code in self._spoken:
            if low == spoken or low.startswith(spoken + " "):
                return code, a[len(spoken):].strip()
        return None

    # -- rows -------------------------------------------------------------------------------------

    def _reload(self) -> None:
        with self._lock:
            self._rows = {(r["system"], r["radio_id"]): r for r in self.store.q("SELECT * FROM radios")}

    def rows(self) -> list[dict]:
        with self._lock:
            return sorted((dict(r) for r in self._rows.values()),
                          key=lambda r: (r["system"], r["unit"] == "", r["unit"], r["radio_id"]))

    def get(self, system: str, radio_id: str) -> dict | None:
        with self._lock:
            r = self._rows.get((system, radio_id))
            return dict(r) if r else None

    def seed(self) -> int:
        """Add what the data file says, once per version. Never overwrites a row already there
        (an edit, or an ID heard first). Returns how many rows were added."""
        p = self.plan
        done = self.store.one("SELECT value FROM meta WHERE key = 'radio_seed_version'")
        if done and int(done["value"]) >= p["version"]:
            return 0
        now = self.clock()
        rows: list[tuple] = []
        for c in p["confirmed"]:
            rows.append((c.get("system", "mdc"), str(c["id"]).upper(), c.get("unit", ""), c.get("label", ""),
                         "confirmed", "", c.get("notes", "")))
        for h in p["heard"]:
            g = p["guesses"].get(str(h["id"]).upper()) if h.get("system", "mdc") == "mdc" else None
            rows.append((h.get("system", "mdc"), str(h["id"]).upper(), g[0] if g else h.get("unit", ""),
                         h.get("label") or (g[1] if g else ""), "guessed" if g else "heard", "", h.get("notes", "")))
        for i, (unit, label, pat) in sorted(p["guesses"].items()):
            rows.append(("mdc", i, unit, label, "guessed", "", f"{pat} pattern"))
        added = 0
        with self.store.tx() as db:
            for system, rid, unit, label, source, alias, notes in rows:
                cur = db.execute("INSERT OR IGNORE INTO radios (system, radio_id, unit, label, source, alias, notes, "
                                 "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (system, rid, unit, label, source, alias, notes, now))
                added += cur.rowcount
            db.execute("INSERT INTO meta (key, value) VALUES ('radio_seed_version', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(p["version"]),))
            if added:
                audit(db, now, "", "radios", f"seed:{self.seed_path.name}", "",
                      f"v{p['version']}: {added} radio IDs added ({len(p['conflicts'])} conflicts left unmapped)")
        self._reload()
        return added

    # -- resolving ------------------------------------------------------------------------------

    def resolve(self, system: str, radio_id: str = "", alias: str = "") -> dict | None:
        """{unit, label, source, radio_id} for one radio, or None when nothing names a unit.
        A row with a unit wins (a person's edit, a confirmed or guessed ID); then the alias."""
        rid = str(radio_id or "").strip()
        if system == "mdc":
            rid = rid.upper()
        row = self.get(system, rid) if rid else None
        if row and row["unit"]:
            return {"unit": row["unit"], "label": row["label"], "source": row["source"], "radio_id": rid}
        if row and row["edited"]:      # a person said: this radio is on no rig
            return None
        hit = self.unit_for_alias(alias or (row["alias"] if row else ""))
        if hit:
            return {"unit": hit[0], "label": hit[1] or alias, "source": "heard", "radio_id": rid}
        return None

    def rig_mdc(self, ids: list[str]) -> str:
        out: list[str] = []
        for i in ids:
            r = self.resolve("mdc", i)
            if r and r["unit"] not in out:
                out.append(r["unit"])
        return ", ".join(out)

    def rig_p25(self, pairs: list[tuple[str, str]]) -> str:
        """pairs: (radio ID, alias) per talker."""
        out: list[str] = []
        for rid, alias in pairs:
            r = self.resolve("p25", rid, alias)
            if r and r["unit"] not in out:
                out.append(r["unit"])
        return ", ".join(out)

    def rig_for_names(self, names: str, fm: bool, mdc_aliases: dict[str, str] | None = None) -> tuple[str, str]:
        """(rig, radio IDs) from a traffic row's talker text alone (rows saved before this existed):
        FG1 rows hold MDC IDs or their [mdc] aliases; P25 rows hold talker aliases."""
        back = {v: k for k, v in (mdc_aliases or {}).items()}
        rigs, ids = [], []
        for n in [x.strip() for x in (names or "").split(",") if x.strip()]:
            n = n.removesuffix(" EMERGENCY")
            if fm:
                rid = n.upper() if MDC_ID.fullmatch(n.upper()) else back.get(n, "")
                r = self.resolve("mdc", rid, "" if rid else n)
            else:
                rid = ""
                r = self.resolve("p25", "", n)
            if rid and rid not in ids:
                ids.append(rid)
            if r and r["unit"] not in rigs:
                rigs.append(r["unit"])
        return ", ".join(rigs), ", ".join(ids)

    # -- heard on the air -----------------------------------------------------------------------

    def heard(self, system: str, radio_id: str, alias: str = "", at: float | None = None) -> None:
        """A radio heard (a decoded MDC ID; a P25 radio with its alias). New radios get a row
        (unit from the alias when it names one); known ones get their alias and last-heard time,
        and a heard-only row its unit, unless a person edited it. Cheap when nothing changed."""
        rid = str(radio_id or "").strip()
        if system == "mdc":
            rid = rid.upper()
        if system not in SYSTEMS or not rid or rid in ("0", "-1") or len(rid) > 12:
            return
        alias = " ".join(str(alias or "").split())[:60]
        if alias == rid:
            alias = ""
        at = self.clock() if at is None else at
        key = (system, rid)
        row = self.get(system, rid)
        hit = self.unit_for_alias(alias) if alias else None
        if row is None:
            unit, label = (hit[0], alias) if hit else ("", alias)
            with self.store.lock:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO radios (system, radio_id, unit, label, source, alias, first_heard, last_heard, "
                    "heard, updated_at) VALUES (?, ?, ?, ?, 'heard', ?, ?, ?, 1, ?)", (system, rid, unit, label, alias, at, at, at))
            self._written[key] = at
            self._reload_one(key)
            return
        changes: dict = {}
        if alias and alias != row["alias"]:
            changes["alias"] = alias
        if hit and not row["edited"] and row["source"] == "heard" and row["unit"] != hit[0]:
            changes["unit"], changes["label"] = hit[0], alias
        if not changes and at - self._written.get(key, 0) < HEARD_WRITE_S:
            with self._lock:
                self._rows[key]["last_heard"] = max(row["last_heard"] or 0, at)
            return
        changes["last_heard"] = max(row["last_heard"] or 0, at)
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.store.lock:
            self.store.db.execute(f"UPDATE radios SET {sets}, heard = heard + 1, first_heard = COALESCE(first_heard, ?) "
                                  "WHERE system = ? AND radio_id = ?", (*changes.values(), at, system, rid))
        self._written[key] = at
        self._reload_one(key)

    def _reload_one(self, key: tuple[str, str]) -> None:
        r = self.store.one("SELECT * FROM radios WHERE system = ? AND radio_id = ?", key)
        with self._lock:
            if r:
                self._rows[key] = r
            else:
                self._rows.pop(key, None)

    # -- the Radios panel -------------------------------------------------------------------------

    def save(self, body: dict, operator: str = "") -> dict:
        """Add or edit one radio: {system, radio_id, unit?, label?, source?, notes?}. Logged."""
        system = str(body.get("system") or "")
        if system not in SYSTEMS:
            raise RadioError(f"system: one of {', '.join(SYSTEMS)}")
        rid = str(body.get("radio_id") or "").strip()
        rid = rid.upper() if system == "mdc" else rid
        if not (MDC_ID if system == "mdc" else P25_ID).fullmatch(rid):
            raise RadioError("radio_id: 4 hex digits (MDC, as decoded: 0611)" if system == "mdc"
                             else "radio_id: the P25 radio's number (digits)")
        f: dict = {}
        if "unit" in body:
            unit = str(body.get("unit") or "").strip()
            if unit and unit.upper() not in self._by_code:
                raise RadioError(f"unit: {unit!r} is not on the boards")
            f["unit"] = self._by_code.get(unit.upper(), "")
        for k, n in (("label", 60), ("notes", 300)):
            if k in body:
                v = body.get(k)
                if v is not None and not isinstance(v, str):
                    raise RadioError(f"{k}: text")
                v = " ".join((v or "").split())
                if len(v) > n:
                    raise RadioError(f"{k}: at most {n} characters")
                f[k] = v
        if "source" in body:
            if body["source"] not in SOURCES:
                raise RadioError(f"source: one of {', '.join(SOURCES)}")
            f["source"] = body["source"]
        now = self.clock()
        old = self.get(system, rid)
        with self.store.tx() as db:
            if old is None:
                f.setdefault("source", "confirmed" if f.get("unit") else "heard")
                db.execute("INSERT INTO radios (system, radio_id, unit, label, source, notes, edited, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, 1, ?)", (system, rid, f.get("unit", ""), f.get("label", ""),
                                                             f["source"], f.get("notes", ""), now))
                audit(db, now, operator, "radios", f"{system}:{rid}", "", self._describe(dict(f, unit=f.get("unit", ""))))
            else:
                ch = {k: v for k, v in f.items() if old[k] != v}
                if not ch:
                    return old
                db.execute(f"UPDATE radios SET {', '.join(f'{k} = ?' for k in ch)}, edited = 1, updated_at = ? "
                           "WHERE system = ? AND radio_id = ?", (*ch.values(), now, system, rid))
                for k, v in ch.items():
                    audit(db, now, operator, "radios", f"{system}:{rid}/{k}", str(old[k] or ""), str(v or ""))
        self._reload_one((system, rid))
        return self.get(system, rid)

    def delete(self, system: str, radio_id: str, operator: str = "") -> None:
        rid = str(radio_id or "").strip()
        rid = rid.upper() if system == "mdc" else rid
        old = self.get(system, rid)
        if old is None:
            raise RadioError(f"no {system} radio {rid!r}")
        now = self.clock()
        with self.store.tx() as db:
            db.execute("DELETE FROM radios WHERE system = ? AND radio_id = ?", (system, rid))
            audit(db, now, operator, "radios", f"{system}:{rid}", self._describe(old), "")
        self._reload_one((system, rid))

    @staticmethod
    def _describe(f: dict) -> str:
        return " · ".join(x for x in (f.get("unit") or "no unit", f.get("label", ""), f.get("source", "")) if x)

    def log(self, limit: int = 100) -> list[dict]:
        return audit_log(self.store, "radios", limit)

    def public(self) -> dict:
        return {"radios": self.rows(), "conflicts": self.plan["conflicts"], "unfit": self.plan["unfit"],
                "log": self.log(), "seed": {"file": self.seed_path.name, "version": self.plan["version"]},
                "units": [{"name": u["name"], "kind": u["kind"], "pcfd": is_pcfd(u), "department": u["department"]}
                          for u in self.units if u["board"]],
                "sources": list(SOURCES), "systems": list(SYSTEMS)}
