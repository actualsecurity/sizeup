"""Pre-plan site documents (#459): a building's floor plans (per floor, overlaid on the map of every call there, aligned
to its county footprint) and its other documents (pre-plan and inspection sheets, hazmat / Tier II, contact sheets,
photos, other PDFs), opened in the app. Linked to the building the way pre-plan points are (#456,
sizeup/cad/preplan.py): its building key (sizeup/cad/addrkey.py) and the county footprint (building_ref).

    preplan_docs   one per upload: ref (its storage name: 16 hex digits), the building (bldg_key, addr_key, addr_label,
                   building_ref, link: as a pre-plan point's), the building's point (lat, lon, cell: a call with no
                   building key finds it within addrkey.GROUP_M, or by the footprint its pin is in), kind (KINDS), title,
                   floor ("1", "B1", "Roof"), the file as stored (mime, bytes, sha256: a PDF saved again with only its
                   pages, a picture without its metadata; a PDF's pages), the plan (OVERLAY kinds: plan.png, the picture
                   drawn on the map: a PDF's page rendered, or the picture turned upright, at most 4096 px a side; page,
                   plan_w, plan_h), corners (the plan's four corners on the map: [[lon, lat] x 4], top left, top right,
                   bottom right, bottom left; NULL: not aligned, not drawn; cleared when the picture changes),
                   admin_only, active (0: deleted, its bytes gone),
                   who uploaded it and when, who changed it last, who aligned it last.

The bytes: sizeup/docstore.py (a private bucket prefix, read only by the hub and streamed to the person; a folder in
dev). What may be uploaded: sizeup/docfiles.py (PDF, PNG, JPEG by their first bytes; at most MAX_BYTES; photos without
EXIF / GPS; PDFs saved again without their actions, scripts or metadata).

Who: anyone signed in sees the documents, an admin-only one only admins (to everyone else it doesn't exist: not in a
list, a count, the audit's text or a link). Dispatchers and admins upload, edit, align and delete (the API's WRITES);
only an admin makes a document admin-only or changes one. Every upload, edit, alignment and delete is in the Settings
audit log (section "preplan_docs"); an admin-only document's rows are its id and the names of what changed, and only
admins see them at all. The server log never gets a title, a file name or a corner: the activity line says "Aligned site
doc 12".

A delete marks the row deleted, then deletes its objects; sweep() (the leader, every SWEEP_EVERY_S) deletes objects under
the store's prefix that no active document names, SWEEP_MARGIN_S old, behind a circuit breaker.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from sizeup import docfiles
from sizeup.cad import addrkey
from sizeup.cad.premise import who
from sizeup.cad.preplan import cell_of, cells, footprint
from sizeup.cad.settings import Editor
from sizeup.cad.store import CadError

KINDS = {"floor_plan": "Floor plan", "site_plan": "Site plan", "preplan": "Pre-plan sheet", "inspection": "Inspection",
         "hazmat": "Hazmat / Tier II", "contacts": "Contacts", "photo": "Photo", "other": "Other"}
OVERLAY = ("floor_plan", "site_plan")       # the kinds drawn on the map
MAX_BYTES = 25 * 1024 * 1024
PLAN_MAX_SIDE = docfiles.MAX_PX             # a plan picture's side: bigger ones are scaled down (one iPad texture)
TITLE_MAX, FLOOR_MAX = 80, 20
CORNER_M = 1000.0                           # every corner this close to the building's point
SIDE_M = (1.0, 1000.0)                      # a plan's side, metres
SECTION = "preplan_docs"
UPLOADS = threading.BoundedSemaphore(2)     # read at once (each up to 25 MB): server.py takes one before the body
UPLOAD_WAIT_S = 5.0
DOWNLOADS = threading.BoundedSemaphore(4)   # documents being sent at once (each: a store connection, a server thread)
DOWNLOAD_WAIT_S = 2.0
DOWNLOAD_WRITE_S = 30.0                     # server.py: a client that takes no bytes this long is dropped
SWEEP_MARGIN_S = 6 * 3600.0                 # the sweep: an object no document names, this old
SWEEP_FIRST_S, SWEEP_EVERY_S = 900.0, 6 * 3600.0
SWEEP_MAX_N, SWEEP_SHARE, SWEEP_MIN_ALLOW = 50, 0.2, 10      # its circuit breaker
VIEW_S = 600.0                              # the same person opening the same document: in their activity once a while
AUDITED = ("kind", "title", "floor", "mime", "bytes", "sha256", "pages", "page", "bldg_key", "building_ref", "link",
           "corners", "admin_only", "active")

SCHEMA = """
CREATE TABLE IF NOT EXISTS preplan_docs (
  id INTEGER PRIMARY KEY,
  ref TEXT NOT NULL UNIQUE,             -- the storage name (16 hex digits): sizeup/docstore.py
  bldg_key TEXT NOT NULL DEFAULT '',
  addr_key TEXT,
  building_ref TEXT,                    -- the county footprint: "buildings/<id>"
  link TEXT NOT NULL DEFAULT 'call',
  addr_label TEXT NOT NULL DEFAULT '',
  lat REAL, lon REAL,                   -- the building's point (the call's pin, the address's point)
  cell TEXT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  floor TEXT,
  mime TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  pages INTEGER,                        -- a PDF's
  page INTEGER,                         -- a PDF plan: the page drawn
  plan TEXT NOT NULL DEFAULT '',        -- the plan's picture: 'original' | 'plan.png' | '' (none)
  plan_w INTEGER, plan_h INTEGER,
  corners TEXT,                         -- JSON [[lon, lat] x 4]: TL TR BR BL; NULL: not aligned
  admin_only INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL,
  aligned_by TEXT NOT NULL DEFAULT '',
  aligned_at REAL
);
CREATE INDEX IF NOT EXISTS preplan_docs_bldg ON preplan_docs(bldg_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS preplan_docs_cell ON preplan_docs(cell) WHERE active = 1;
"""

FLOOR_ORDER = re.compile(r"(?i)^(?:(b|basement|sub|cellar)\s*(\d*)|(\d+)|(g|ground|lobby)|(m|mezz\w*)|(roof|r|attic|penthouse|ph))$")


def floor_key(f: str | None) -> tuple:
    """Floors in building order: basements (B2 under B1), ground, 1, 2 ..., the roof last; anything else after."""
    t = (f or "").strip()
    m = FLOOR_ORDER.match(t)
    if not t:
        return (5, 0, "")
    if not m:
        return (4, 0, t.lower())
    if m.group(1):
        return (0, -(int(m.group(2)) if m.group(2) else 1), "")
    if m.group(3):
        return (1, int(m.group(3)), "")
    if m.group(4):
        return (1, 0, "")
    if m.group(5):
        return (1, 1.5, "")
    return (3, 0, "")


def local_m(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """(east, north) metres from (lat0, lon0), on a local flat plane."""
    return (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 110_574.0


def check_corners(v, near: tuple[float, float] | None) -> list[list[float]]:
    """Four [lon, lat] corners, TL TR BR BL: a convex quadrilateral, clockwise on the map (not mirrored), each side
    SIDE_M, every corner within CORNER_M of the building's point. CadError otherwise."""
    bad = CadError("corners: four [lon, lat], top left, top right, bottom right, bottom left")
    if not isinstance(v, list) or len(v) != 4:
        raise bad
    out = []
    for c in v:
        if not isinstance(c, list) or len(c) != 2:
            raise bad
        try:
            lon, lat = float(c[0]), float(c[1])
        except (TypeError, ValueError):
            raise bad from None
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -85 <= lat <= 85):
            raise bad
        out.append([round(lon, 8), round(lat, 8)])
    lat0 = sum(c[1] for c in out) / 4
    lon0 = sum(c[0] for c in out) / 4
    if near is not None and any(addrkey.meters(near[0], near[1], c[1], c[0]) > CORNER_M for c in out):
        raise CadError(f"corners: within {CORNER_M:.0f} m of the building")
    p = [local_m(lat0, lon0, c[1], c[0]) for c in out]
    for i in range(4):
        a, b = p[i], p[(i + 1) % 4]
        if not SIDE_M[0] <= math.hypot(b[0] - a[0], b[1] - a[1]) <= SIDE_M[1]:
            raise CadError(f"corners: each side {SIDE_M[0]:.0f} to {SIDE_M[1]:.0f} m")
    cross = []
    for i in range(4):
        a, b, c = p[i], p[(i + 1) % 4], p[(i + 2) % 4]
        cross.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if not all(x < 0 for x in cross):
        raise CadError("corners: a plan the right way round (not crossed or mirrored)")
    return out


class Busy(CadError):
    """Every download slot is taken (the API answers 503)."""


class PreplanDocs:
    """One per Cad (sizeup/cad/service.py). store: sizeup/docstore.py's (the app sets it), None: not configured."""

    def __init__(self, cad):
        self.cad, self.db = cad, cad.store
        self.store = None
        self.gen = int(time.time() * 1000)
        self._lock = threading.Lock()
        self._viewed: dict[tuple, float] = {}
        self._said_limits = False
        self.may_write = lambda: True              # the app: the hub's fence (only the leader sweeps)
        self.sweep_status = ""
        self._thread, self._stop = None, threading.Event()
        with self.db.lock:
            if self.db.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone():
                self.db.db.executescript(SCHEMA)

    # -- reading ----------------------------------------------------------------------------------------------------------

    @staticmethod
    def may_see(d: dict, may: str) -> bool:
        return not d["admin_only"] or may == "admin"

    def state(self) -> dict:
        return {"gen": self.gen, "configured": self.store is not None}

    def shown(self, d: dict) -> dict:
        out = {k: d[k] for k in ("id", "kind", "title", "floor", "mime", "bytes", "pages", "page", "plan_w", "plan_h",
                                 "bldg_key", "addr_label", "building_ref", "lat", "lon", "created_at", "updated_at",
                                 "aligned_at")}
        out["corners"] = json.loads(d["corners"]) if d["corners"] else None
        out["plan"] = bool(d["plan"])
        out["admin_only"] = bool(d["admin_only"])
        out["active"] = bool(d["active"])
        out["by"] = who(d["created_by"])
        out["aligned_by"] = who(d["aligned_by"]) if d["aligned_by"] else ""
        return out

    @staticmethod
    def order(docs: list[dict]) -> list[dict]:
        kinds = list(KINDS)
        return sorted(docs, key=lambda d: (kinds.index(d["kind"]) if d["kind"] in kinds else 99, floor_key(d["floor"]),
                                           d["title"].lower(), d["id"]))

    def _at(self, bldg_key: str, lat, lon) -> list[dict]:
        """A building's active documents; with no building key on the call or the document, those whose building is the
        footprint the call's pin is in (both with a footprint), else within addrkey.GROUP_M of it ("nearby"). Two keyed
        buildings never share."""
        rows = self.db.q("SELECT * FROM preplan_docs WHERE bldg_key = ? AND active = 1", (bldg_key,)) if bldg_key else []
        for r in rows:
            r["nearby"] = False
        if lat is not None and lon is not None:
            fp = footprint(getattr(self.cad, "gis", None), lat, lon)
            ref = fp["ref"] if fp else None
            a, b, c, d = addrkey.bbox(lat, lon)
            cs = cells(c, a, d, b)
            seen = {r["id"] for r in rows}
            for r in self.db.q(f"SELECT * FROM preplan_docs WHERE active = 1 AND cell IN ({','.join('?' * len(cs))})", cs):
                if r["id"] in seen or (bldg_key and r["bldg_key"]):
                    continue
                # the footprint the pin is in (or nearest) when both have one; else by distance
                if (r["building_ref"] == ref) if (ref and r["building_ref"]) else (
                        r["lat"] is not None and addrkey.meters(lat, lon, r["lat"], r["lon"]) <= addrkey.GROUP_M):
                    r["nearby"] = True
                    rows.append(r)
        return rows

    def for_incident(self, inc: dict, may: str) -> list[dict]:
        out = []
        for d in self.order([d for d in self._at(inc.get("bldg_key") or "", inc.get("lat"), inc.get("lon"))
                             if self.may_see(d, may)]):
            s = self.shown(d)
            s["nearby"] = d.get("nearby", False)
            out.append(s)
        return out

    def get(self, q: dict, may: str) -> dict:
        """GET /api/cad/preplan/docs?incident=<id> | where=<address> (one building)."""
        def one(k: str, n: int = 200) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:n]
        if one("incident"):
            try:
                inc = self.db.incident(int(one("incident")))
            except (TypeError, ValueError):
                raise CadError("incident: a number") from None
            return {"docs": self.for_incident(inc, may), "gen": self.gen, "configured": self.store is not None}
        if one("where"):
            k = self.cad.premise._key_text(one("where"))
            if not k.bldg_key:
                raise CadError("Not an address the map knows")
            pt = self.cad.preplan._point_of(one("where"))
            docs = self._at(k.bldg_key, *(pt or (None, None)))
            return {"docs": [self.shown(d) | {"nearby": d.get("nearby", False)} for d in self.order(docs)
                             if self.may_see(d, may)], "gen": self.gen, "configured": self.store is not None}
        raise CadError("incident or where: the building")

    def _row(self, did: int, may: str, active: bool = True) -> dict:
        d = self.db.one("SELECT * FROM preplan_docs WHERE id = ?", (did,))
        if d is None or not self.may_see(d, may) or (active and not d["active"]):
            raise CadError(f"no document {did}")             # an admin-only one, to anyone else: none
        return d

    def one(self, did: int, may: str) -> dict:
        """GET /api/cad/preplan/docs/<id>: the document and its building's footprint (the align tool's snap)."""
        d = self._row(did, may)
        fp = footprint(getattr(self.cad, "gis", None), d["lat"], d["lon"]) if d["lat"] is not None else None
        return {"doc": self.shown(d), "footprint": fp["geometry"] if fp else None, "gen": self.gen}

    def file(self, did: int, part: str, may: str, email: str = "", dl: bool = False) -> dict:
        """GET /api/cad/preplan/docs/<id>/file | plan: {stream (sizeup/attachments.py Stream: the store's pieces as they
        are read), ctype, disposition}. The file name is the document's number, never its title. At most DOWNLOADS at
        once (each holds a store connection and a server thread while it is sent): Busy after DOWNLOAD_WAIT_S."""
        d = self._row(did, may)
        if self.store is None:
            raise CadError("Site documents not configured")
        ext = docfiles.MIMES[d["mime"]]
        if part == "plan":
            if not d["plan"]:
                raise CadError(f"no plan for document {did}")
            name, ctype = ("plan.png", "image/png") if d["plan"] == "plan.png" else (f"original.{ext}", d["mime"])
        else:
            name, ctype = f"original.{ext}", d["mime"]
        fname = f"site-doc-{did}{'-plan' if part == 'plan' else ''}.{name.rsplit('.', 1)[1]}"
        disposition = f'{"attachment" if dl else "inline"}; filename="{fname}"'
        key = self.store.key(d["ref"], name)
        self._seen(email, did, part)
        from sizeup.attachments import Stream
        from sizeup.docstore import DocStoreError
        if not DOWNLOADS.acquire(timeout=DOWNLOAD_WAIT_S):
            raise Busy("Busy: try again in a moment")
        try:
            opened = self.store.open(key)
        except DocStoreError as e:
            DOWNLOADS.release()
            self.cad.log(f"CAD site doc {did}: {e}")
            raise CadError("Not available now: try again") from None

        def done() -> None:
            try:
                opened.close()
            finally:
                DOWNLOADS.release()
        return {"stream": Stream(opened.length, opened.pieces, done), "ctype": ctype, "disposition": disposition}

    def _seen(self, email: str, did: int, part: str) -> None:
        """Opening a document is in the person's activity (kind "search": what they read), at most once per VIEW_S."""
        if not email:
            return
        now = time.monotonic()
        k = (email, did, part)
        with self._lock:
            if now - self._viewed.get(k, -VIEW_S) < VIEW_S:
                return
            if len(self._viewed) > 2000:
                self._viewed.clear()
            self._viewed[k] = now
        try:
            self.cad.activity.record(email, "search", f"GET /api/cad/preplan/docs/{did}/{part}",
                                     f"Opened site doc {did}{' (plan)' if part == 'plan' else ''}")
        except Exception as e:  # noqa: BLE001 - never in the way of the read
            self.cad.log(f"CAD site doc {did}: activity {type(e).__name__}")

    # -- writing ----------------------------------------------------------------------------------------------------------

    def _bump(self) -> None:
        with self._lock:
            self.gen += 1

    def _fields(self, body: dict, partial: bool) -> dict:
        f: dict = {}
        if not partial or "kind" in body:
            k = str(body.get("kind") or "")
            if k not in KINDS:
                raise CadError(f"kind: one of {', '.join(KINDS)}")
            f["kind"] = k
        if not partial or "title" in body:
            t = " ".join(str(body.get("title") or "").split())
            if not t or len(t) > TITLE_MAX:
                raise CadError(f"title: 1 to {TITLE_MAX} characters")
            f["title"] = t
        if not partial or "floor" in body:
            t = " ".join(str(body.get("floor") or "").split())
            if len(t) > FLOOR_MAX:
                raise CadError(f"floor: at most {FLOOR_MAX} characters")
            f["floor"] = t or None
        if "admin_only" in body:
            if not isinstance(body["admin_only"], bool):
                raise CadError("admin_only: true or false")
            f["admin_only"] = int(body["admin_only"])
        return f

    @staticmethod
    def _page(v) -> int:
        try:
            p = int(v)
        except (TypeError, ValueError):
            p = 0
        if not 1 <= p <= 9999:
            raise CadError("page: a page number")
        return p

    def _audit(self, db, op: str, d: dict, old: dict | None, new: dict | None, admin_only: bool) -> None:
        """An admin-only document's rows: its id and the names of what changed (never its title or corners)."""
        if admin_only:
            shape = lambda v: None if v is None else {"admin_only": True, "changed": sorted(v)}   # noqa: E731
            Editor._audit(db, self.cad.clock(), op, SECTION, str(d["id"]), shape(old), shape(new))
            return
        Editor._audit(db, self.cad.clock(), op, SECTION, f"{d['id']} {KINDS.get(d['kind'], d['kind'])}", old, new)

    def redact_audit(self, rows: list[dict], may: str) -> list[dict]:
        """The Settings audit log as this reader may see it: an admin-only document's rows (any written before it was
        made admin-only too) are not there at all for anyone but an admin."""
        if may == "admin":
            return rows
        ids = {int(m.group(1)) for r in rows if r.get("section") == SECTION and (m := re.match(r"(\d+)", r.get("key") or ""))}
        if not ids:
            return rows
        hidden = {r["id"] for r in self.db.q(f"SELECT id FROM preplan_docs WHERE admin_only = 1 AND id IN "
                                             f"({','.join('?' * len(ids))})", list(ids))}
        out = []
        for r in rows:
            m = re.match(r"(\d+)", r.get("key") or "") if r.get("section") == SECTION else None
            if m and int(m.group(1)) in hidden:
                continue
            out.append(r)
        return out

    def _need_store(self):
        if self.store is None:
            raise CadError("Site documents not configured (no storage: SIZEUP_PREPLAN_DOCS_BUCKET)")
        return self.store

    def _limits(self, why: str) -> None:
        if why and not self._said_limits:
            self._said_limits = True
            self.cad.log(f"CAD site doc: the renderer's limits not set ({why})")

    def _prepare(self, kind: str, mime: str, data: bytes, page: int | None) -> dict:
        """What is stored and drawn for this kind and file: {stored (a PDF saved again without anything active or its
        metadata; a picture without its metadata), plan ('plan.png' | ''), png, plan_w, plan_h, page, pages}.
        Refused (docfiles) when the file isn't acceptable."""
        out = {"plan": "", "png": None, "plan_w": None, "plan_h": None, "page": None, "pages": None}
        if mime == "application/pdf":
            docfiles.check_pdf(data)
            r = docfiles.render(data, (page or 1) if kind in OVERLAY else 0)
            if r is None:
                raise docfiles.Refused("PDFs can't be checked here (no PDF reader on this machine): upload a PNG or JPEG")
            self._limits(r["limits"])
            out.update(stored=r["pdf"], pages=r["pages"])
            if kind in OVERLAY:
                out.update(plan="plan.png", png=r["png"], plan_w=r["w"], plan_h=r["h"], page=page or 1)
            return out
        out["stored"] = docfiles.clean_image(data, mime)[0]
        if kind in OVERLAY:
            r = docfiles.plan_image(out["stored"])
            if r is None:
                raise docfiles.Refused("plans can't be drawn here (no image library on this machine)")
            self._limits(r["limits"])
            out.update(plan="plan.png", png=r["png"], plan_w=r["w"], plan_h=r["h"])
        return out

    def upload(self, meta: dict, data: bytes) -> dict:
        """POST /api/cad/preplan/docs (the file as the body; meta from the X-Sizeup-Doc header): {kind, title, floor?,
        page? (a PDF plan's page, 1), admin_only? (admins), incident_id | where}. The server holds an UPLOADS slot
        while it reads the body and this runs."""
        store = self._need_store()
        may = str(meta.get("_may") or "viewer")
        f = self._fields(meta, partial=False)
        if f.get("admin_only") and may != "admin":
            raise CadError("admin_only: admins")
        page = self._page(meta["page"]) if meta.get("page") not in (None, "") else None
        if not 0 < len(data) <= MAX_BYTES:
            raise CadError(f"a file of at most {MAX_BYTES // (1024 * 1024)} MB")
        ctx = self.cad.preplan._ctx({"incident_id": meta.get("incident_id"), "where": meta.get("where")})
        if ctx is None or not (ctx["bldg_key"] or ctx["lat"] is not None):
            raise CadError("incident_id or where: a building the map knows")
        if ctx["lat"] is not None:
            a = self.cad.preplan.associate(ctx["lat"], ctx["lon"], ctx, "call" if ctx["bldg_key"] else "auto")
        else:
            a = {"bldg_key": ctx["bldg_key"], "addr_key": ctx["addr_key"], "addr_label": ctx["label"], "link": "call",
                 "building_ref": None}
        try:
            mime = docfiles.sniff(data)
            if mime != "application/pdf":
                docfiles.clean_image(data, mime)                 # refused here when it isn't a whole picture
            p = self._prepare(f["kind"], mime, data, page)
        except docfiles.Refused as e:
            raise CadError(str(e)) from None
        clean = p["stored"]
        ref = secrets.token_hex(8)
        keys = [store.key(ref, f"original.{docfiles.MIMES[mime]}")]
        from sizeup.docstore import DocStoreError
        try:
            store.put(keys[0], clean, mime)
            if p["png"] is not None:
                keys.append(store.key(ref, "plan.png"))
                store.put(keys[1], p["png"], "image/png")
        except DocStoreError as e:
            self.cad.log(f"CAD site doc upload: {e}")
            self._drop(keys)                                     # what isn't dropped now, the sweep takes
            raise CadError("Not stored: try again") from None
        op = str(meta.get("operator") or "")[:320]
        now = self.cad.clock()
        lat, lon = ctx["lat"], ctx["lon"]
        try:
            with self.db.tx() as db:
                cur = db.execute(
                    "INSERT INTO preplan_docs (ref, bldg_key, addr_key, building_ref, link, addr_label, lat, lon, cell, kind, "
                    "title, floor, mime, bytes, sha256, pages, page, plan, plan_w, plan_h, admin_only, active, created_by, "
                    "created_at, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, 1, ?, ?, ?, ?)",
                    (ref, a["bldg_key"], a["addr_key"], a["building_ref"], a["link"], (a["addr_label"] or "")[:200], lat, lon,
                     cell_of(lat, lon) if lat is not None else None, f["kind"], f["title"], f["floor"], mime, len(clean),
                     hashlib.sha256(clean).hexdigest(), p["pages"], p["page"], p["plan"], p["plan_w"], p["plan_h"],
                     f.get("admin_only", 0), op, now, op, now))
                d = dict(db.execute("SELECT * FROM preplan_docs WHERE id = ?", (cur.lastrowid,)).fetchone())
                self._audit(db, op, d, None, {k: d[k] for k in AUDITED}, bool(d["admin_only"]))
        except Exception:
            self._drop(keys)
            raise
        self._bump()
        return {"ok": True, "doc": self.shown(d)}

    def _keys(self, d: dict) -> list[str]:
        keys = [self.store.key(d["ref"], f"original.{docfiles.MIMES[d['mime']]}")]
        if d["plan"] == "plan.png":
            keys.append(self.store.key(d["ref"], "plan.png"))
        return keys

    def _drop(self, keys: list[str]) -> bool:
        """Delete these objects; False when one couldn't be (it stays for the sweep: no row names it)."""
        from sizeup.docstore import DocStoreError
        ok = True
        for k in keys:
            try:
                self.store.delete(k)
            except DocStoreError as e:
                ok = False
                self.cad.log(f"CAD site doc: not deleted now, left for the sweep ({e})")
        return ok

    def edit(self, did: int, body: dict) -> dict:
        """POST /api/cad/preplan/docs/<id> {title?, kind?, floor?, page? (a PDF plan: draw another page), admin_only?
        (admins), corners? ([[lon, lat] x 4] | null: not aligned), action?: delete}. A new plan picture (another page,
        another kind) is aligned again: its corners are cleared. A delete marks the row deleted first, then deletes its
        bytes (what fails is the sweep's)."""
        may = str(body.get("_may") or "viewer")
        d = self._row(did, may)
        if d["admin_only"] and may != "admin":
            raise CadError(f"no document {did}")
        f = self._fields(body, partial=True)
        if "admin_only" in f and may != "admin":
            raise CadError("admin_only: admins")
        op = str(body.get("operator") or "")[:320]
        act = str(body.get("action") or "")
        if act and act != "delete":
            raise CadError("action: delete")
        if act == "delete":
            self._need_store()
            f = {"active": 0}
        kind = f.get("kind", d["kind"])
        png = None
        if not act and ("page" in body or ("kind" in f and (kind in OVERLAY) != bool(d["plan"]))):
            if kind in OVERLAY:
                page = self._page(body["page"]) if "page" in body else None
                if page is not None and d["mime"] != "application/pdf":
                    raise CadError("page: a PDF floor or site plan's")
                if page is not None and d["pages"] and page > d["pages"]:
                    raise CadError(f"page: 1 to {d['pages']}")
                try:
                    p = self._prepare(kind, d["mime"], self._original(d), page or d["page"])
                except docfiles.Refused as e:
                    raise CadError(str(e)) from None
                png = p["png"]
                f.update(plan="plan.png", plan_w=p["plan_w"], plan_h=p["plan_h"], page=p["page"],
                         pages=p["pages"] or d["pages"])
            elif "page" in body:
                raise CadError("page: a PDF floor or site plan's")
            else:
                f.update(plan="", plan_w=None, plan_h=None, page=None)
            f["corners"] = None                                  # a new picture (or none): aligned again
        if not act and "corners" in body:
            near = (d["lat"], d["lon"]) if d["lat"] is not None else None
            if body["corners"] is None:
                f["corners"] = None
            else:
                if kind not in OVERLAY:
                    raise CadError("corners: a floor or site plan's")
                if png is not None:
                    raise CadError("corners: align the new page once it is drawn")
                f["corners"] = json.dumps(check_corners(body["corners"], near), separators=(",", ":"))
        changes = {k: v for k, v in f.items() if d[k] != v}
        now = self.cad.clock()
        if not changes:
            return {"ok": True, "doc": self.shown(d), "changed": []}
        if png is not None:
            from sizeup.docstore import DocStoreError
            try:
                self.store.put(self.store.key(d["ref"], "plan.png"), png, "image/png")
            except DocStoreError as e:
                self.cad.log(f"CAD site doc {did}: {e}")
                raise CadError("Not stored: try again") from None
        sets = dict(changes, updated_at=now, updated_by=op)
        if "corners" in changes:
            sets.update(aligned_at=now if changes["corners"] else None, aligned_by=op if changes["corners"] else "")
        before = dict(d)
        with self.db.tx() as db:
            db.execute(f"UPDATE preplan_docs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), did))
            aud = {k: v for k, v in changes.items() if k in AUDITED}
            if aud:
                old = {k: d[k] for k in aud}
                self._audit(db, op, d, old, aud, bool(d["admin_only"]) or bool(changes.get("admin_only")))
            d = dict(db.execute("SELECT * FROM preplan_docs WHERE id = ?", (did,)).fetchone())
        if act == "delete":
            self._drop(self._keys(before))                       # the row says deleted already; a failure: the sweep
        elif before["plan"] == "plan.png" and d["plan"] != "plan.png":
            self._drop([self.store.key(d["ref"], "plan.png")])   # no longer a plan: its picture goes
        self._bump()
        return {"ok": True, "doc": self.shown(d), "changed": sorted(k for k in changes if k in AUDITED)}

    def _original(self, d: dict) -> bytes:
        from sizeup.docstore import DocStoreError
        store = self._need_store()
        try:
            return store.get(store.key(d["ref"], f"original.{docfiles.MIMES[d['mime']]}"))
        except DocStoreError as e:
            self.cad.log(f"CAD site doc {d['id']}: {e}")
            raise CadError("Not available now: try again") from None

    # -- the sweep: objects no live document names --------------------------------------------------------------------

    def sweep(self, now: float | None = None) -> int:
        """Objects under the store's prefix that no active document names (an upload whose row was never written, a
        delete whose bytes couldn't be deleted then, an old plan picture), older than SWEEP_MARGIN_S: deleted. Only
        the leader (may_write), only keys of the documents' own shape under the prefix, and a circuit breaker: more
        than SWEEP_MAX_N, or more than SWEEP_SHARE of what was looked at (SWEEP_MIN_ALLOW whatever the share), and
        nothing is deleted (a wrong or empty database would otherwise empty the prefix); sweep_status says why. The
        number deleted."""
        from sizeup.docstore import SAFE_KEY, DocStoreError
        self.sweep_status = ""
        st = self.store
        if st is None or not hasattr(st, "pages") or not self.may_write():
            return 0
        now = time.time() if now is None else now
        top = st.prefix + "/"
        looked, orphans = 0, []

        def named(keys: list[str]) -> set[str]:
            refs = sorted({k.split("/")[1] for k in keys})
            rows = self.db.q(f"SELECT ref, mime, plan FROM preplan_docs WHERE active = 1 AND ref IN "
                             f"({','.join('?' * len(refs))})", refs) if refs else []
            out = set()
            for r in rows:
                out.add(f"{top}{r['ref']}/original.{docfiles.MIMES.get(r['mime'], '?')}")
                if r["plan"] == "plan.png":
                    out.add(f"{top}{r['ref']}/plan.png")
            return out
        try:
            for page in st.pages(top):
                old = [k for k, t in page if t < now - SWEEP_MARGIN_S and k.startswith(top) and SAFE_KEY.fullmatch(k)]
                if not old:
                    continue
                looked += len(old)
                keep = named(old)
                orphans += [k for k in old if k not in keep]
                if len(orphans) > SWEEP_MAX_N:
                    break
        except DocStoreError as e:
            self.sweep_status = f"listing failed ({e})"
            return 0
        if len(orphans) > SWEEP_MAX_N or len(orphans) > max(SWEEP_MIN_ALLOW, SWEEP_SHARE * looked):
            self.sweep_status = (f"{len(orphans)}{'+' if len(orphans) > SWEEP_MAX_N else ''} of {looked} objects named "
                                 "by no document: too many, nothing deleted")
            self.cad.log(f"CAD site docs sweep stopped: {self.sweep_status}")
            return 0
        n = 0
        for k in orphans:
            if not self.may_write() or k in named([k]):          # looked at again: a row written meanwhile
                continue
            try:
                st.delete(k)
                n += 1
            except DocStoreError as e:
                self.cad.log(f"CAD site docs sweep: {e}")
        if n:
            self.cad.log(f"CAD site docs sweep: {n} object(s) no document names, deleted")
        return n

    def start(self):
        """The sweep, every SWEEP_EVERY_S (the first a while after start), in a thread of its own."""
        if self._thread is not None:
            return self
        self._stop.clear()

        def run():
            wait = SWEEP_FIRST_S
            while not self._stop.wait(wait):
                wait = SWEEP_EVERY_S
                try:
                    self.sweep()
                except Exception as e:  # noqa: BLE001 - the next round
                    self.cad.log(f"CAD site docs sweep: {type(e).__name__}")
        self._thread = threading.Thread(target=run, name="preplan-docs-sweep", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def meta_of(header: str) -> dict:
    """The X-Sizeup-Doc header: a URI-encoded JSON object (the page: encodeURIComponent(JSON.stringify(...)))."""
    from urllib.parse import unquote
    try:
        v = json.loads(unquote(header or ""))
    except ValueError:
        raise CadError("X-Sizeup-Doc: the document's title, kind and building (JSON)") from None
    if not isinstance(v, dict):
        raise CadError("X-Sizeup-Doc: a JSON object")
    v.pop("_may", None)
    return v
