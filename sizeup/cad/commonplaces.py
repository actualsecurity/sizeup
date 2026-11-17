"""The county's "common place names" list (Settings > Lists > Place names > Import): a spreadsheet from the county's
CAD (Address | Common Place | Mun, one row per name: one address may have several) imported into the department's own
place names (settings_overrides section "places", sizeup/cad/settings.py Editor.local_places).

    read_table(data, filename) -> rows        .xlsx (its first sheet, read with zipfile + XML: the standard library) or
                                              .csv (UTF-8, else Windows-1252)
    find_columns(header, given) -> {address, name, town}: header words, any case ("Address", "Common Place", "Mun"),
                                              or the admin's own mapping (column numbers from 0)
    town_code(code) -> "port chester"         the county's town code ("PORT_CHESTER") as words, for the lists'
                                              town_of (geo_town_aliases): the CAD's own spelling of the town
    entry_key(name, address) -> "county/<name> @ <address>"   lower case, the place search's words (one entry per name
                                              and address: several names per address are fine)
    name_key(text) -> "sample marina"         how a name is compared everywhere (sizeup/geo/places.py norm_tokens)
    group_sites(entries)                      one name at neighbouring addresses -> one site, its primary
    Lookup(entries).find(text) -> entry       a location that is only a place's name ("@SAMPLE MARINA", "SAMPLE MARINA,
                                              PORT_CHESTER"): the county feed's location (sizeup/cad/sources.py)

Each imported entry: {name, aliases: [], label (the name as people write it), lat, lon (None: the address did not
resolve), muni (the CAD's town), note: "", address (the address as the file has it), at (the address point's label,
"12 Example Ave"; "" when not found), source: SOURCE, imported
(YYYY-MM-DD), located: exact | interpolated | "" (not found)}; one name at several addresses that are one place (a
site: group_sites) adds site (the key of the site's primary entry) to each, and site_addresses (all of them) to the
primary. The address is looked up in the gazetteer's address
points through the place search's own street words (sizeup/geo/places.py PlaceIndex: "1 EXAMPLE AV" = "1 Example
Avenue"), the row's town first (locate_address: another town's point only when it is the same place). muni is always
the row's own town when it gives one, never the town the gazetteer files the address under. A name whose address does
not resolve is kept, flagged (located ""): the place search and the radio skip it (no point), the county feed still
maps the name to its address text.

A re-import replaces every entry of SOURCE and never touches a name made by hand (keys without the "county/" prefix).
The list is the county's: it lives in cad.sqlite (replicated with the rest of the CAD), never in the repo.
"""

from __future__ import annotations

import csv
import io
import re
import weakref
import zipfile
import zlib
from xml.etree import ElementTree as ET

from sizeup.geo.places import norm_tokens

SOURCE = "county common places"
KEY_PREFIX = "county/"
MAX_BYTES = 3 * 1024 * 1024          # the file as uploaded (an .xlsx of ~1,000 rows is ~50 KB)
MAX_ROWS = 5000
MAX_XML = 4 * 1024 * 1024            # an .xlsx part, unpacked (a zip bomb stops here; 5,000 rows are ~1 MB)
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Header words for each column, best first (lower case, the header's words joined by one space).
HEADERS = {
    "address": ("address", "addr", "street address", "location"),
    "name": ("common place", "common place name", "common name", "place name", "place", "name", "common"),
    "town": ("mun", "municipality", "town", "city", "village", "muni", "jurisdiction"),
}


class TableError(ValueError):
    """The file can't be read as a table (said to the admin as it is)."""


# ----------------------------------------------------------------------------------- reading the file

def read_table(data: bytes, filename: str = "") -> list[list[str]]:
    """The rows of an .xlsx (first sheet) or .csv, each a list of cell texts (stripped). Empty rows dropped."""
    if not data:
        raise TableError("the file is empty")
    if len(data) > MAX_BYTES:
        raise TableError(f"at most {MAX_BYTES // (1024 * 1024)} MB")
    try:
        if data[:2] == b"PK":
            rows = _read_xlsx(data)
        elif filename.lower().endswith((".xlsx", ".xlsm")):
            raise TableError("not an .xlsx file")
        else:
            rows = _read_csv(data)
    except TableError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError, OSError,
            zlib.error, csv.Error, ValueError) as e:     # encrypted, another compression, a broken zip, a huge field
        raise TableError(f"the file can't be read ({type(e).__name__})") from None
    rows = [r for r in rows if any(c for c in r)]
    if len(rows) > MAX_ROWS + 1:
        raise TableError(f"at most {MAX_ROWS} rows")
    return rows


def _read_csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise TableError("not a .csv or .xlsx file")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]


def _part(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML:
        raise TableError("the sheet is too large")
    return z.read(info)


def _col(ref: str) -> int:
    """'C12' -> 2."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _text_of(node) -> str:
    """A shared or inline string: its <t> runs joined (rich text too), phonetic runs (<rPh>) left out."""
    out = []
    for child in node:
        if child.tag == f"{NS}t":
            out.append(child.text or "")
        elif child.tag == f"{NS}r":
            out += [t.text or "" for t in child.findall(f"{NS}t")]
    return "".join(out)


def _first_sheet(z: zipfile.ZipFile) -> str:
    """The workbook's first sheet's part name (its relationship), else xl/worksheets/sheet1.xml."""
    try:
        wb = ET.fromstring(_part(z, "xl/workbook.xml") or b"")
        rels = ET.fromstring(_part(z, "xl/_rels/workbook.xml.rels") or b"")
        first = wb.find(f"{NS}sheets/{NS}sheet")
        rid = first.get(f"{REL_NS}id") if first is not None else None
        for r in rels.iter(f"{PKG_NS}Relationship"):
            if r.get("Id") == rid:
                target = r.get("Target") or ""
                return target.lstrip("/") if target.startswith("/") else "xl/" + target.removeprefix("./")
    except ET.ParseError:
        pass
    return "xl/worksheets/sheet1.xml"


def _read_xlsx(data: bytes) -> list[list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise TableError("not an .xlsx file") from None
    with z:
        try:
            shared = []
            raw = _part(z, "xl/sharedStrings.xml")
            if raw:
                shared = [_text_of(si) for si in ET.fromstring(raw).iter(f"{NS}si")]
            sheet = _part(z, _first_sheet(z)) or _part(z, "xl/worksheets/sheet1.xml")
            if sheet is None:
                raise TableError("no sheet in the file")
            root = ET.fromstring(sheet)
        except ET.ParseError:
            raise TableError("not an .xlsx file (its XML can't be read)") from None
        rows = []
        for row in root.iter(f"{NS}row"):
            cells: dict[int, str] = {}
            for k, c in enumerate(row.iter(f"{NS}c")):
                i = _col(c.get("r") or "") if c.get("r") else k
                t = c.get("t") or "n"
                if t == "inlineStr":
                    node = c.find(f"{NS}is")
                    val = _text_of(node) if node is not None else ""
                else:
                    v = c.find(f"{NS}v")
                    val = (v.text or "") if v is not None else ""
                    if t == "s":
                        try:
                            val = shared[int(val)]
                        except (ValueError, IndexError):
                            val = ""
                    elif t == "b":
                        val = "TRUE" if val == "1" else "FALSE"
                    elif t == "n" and re.fullmatch(r"-?\d+\.0+", val):
                        val = val.split(".")[0]          # a whole number stored as 12.0
                if 0 <= i < 200:
                    cells[i] = " ".join(val.split())
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
            if len(rows) > MAX_ROWS + 1:
                raise TableError(f"at most {MAX_ROWS} rows")
        return rows


# ----------------------------------------------------------------------------------- columns and keys

def _header_word(h: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (h or "").lower()))


def find_columns(header: list[str], given: dict | None = None) -> dict[str, int]:
    """{address, name, town: column index} (town -1: none). given: the admin's mapping (column numbers from 0, -1 = no
    town column) over what the header says. Raises TableError when the address or the name has no column."""
    words = [_header_word(h) for h in header]
    out: dict[str, int] = {}
    for what, names in HEADERS.items():
        hit = next((i for n in names for i, w in enumerate(words) if w == n and i not in out.values()), None)
        if hit is None:            # "Common Place Name (CAD)": a header that starts with one of the words
            hit = next((i for n in names for i, w in enumerate(words) if w.startswith(n + " ") and i not in out.values()),
                       None)
        out[what] = -1 if hit is None else hit
    for what, v in (given or {}).items():
        if what not in HEADERS:
            raise TableError(f"columns: {what}? (address, name, town)")
        if isinstance(v, bool) or not isinstance(v, int) or not -1 <= v < max(len(header), 1):
            raise TableError(f"columns.{what}: a column number from 0 (-1: none)")
        out[what] = v
    for what in ("address", "name"):
        if out[what] < 0:
            raise TableError(f"no {what} column: name it in the header ({', '.join(HEADERS[what][:2])}) or map it")
    if len({out["address"], out["name"]} | ({out["town"]} if out["town"] >= 0 else set())) < (3 if out["town"] >= 0 else 2):
        raise TableError("columns: address, name and town each their own column")
    return out


def town_code(code: str) -> str:
    """The county's town code as words: 'PORT_CHESTER' -> 'port chester' (then the lists' town_of)."""
    return " ".join(re.findall(r"[a-z0-9]+", (code or "").lower().replace("_", " ")))


def name_key(text: str) -> str:
    """How names are compared: the place search's words ('COMMUNITY HALL - SAMPLE' = 'community hall sample')."""
    return " ".join(norm_tokens(text or ""))


def entry_key(name: str, address: str) -> str:
    return f"{KEY_PREFIX}{name_key(name)} @ {name_key(address)}"[:200]


def is_imported(key: str, value: dict | None = None) -> bool:
    return key.startswith(KEY_PREFIX) or (value or {}).get("source") == SOURCE


def proper_name(text: str) -> str:
    """'SAMPLE MARINA' -> 'Sample Marina' (the CAD's display casing)."""
    from sizeup.cad.sources import proper_case
    return proper_case(text)


# ----------------------------------------------------------------------------------- a location that names one

class Lookup:
    """Exact names (the place search's words) -> the entry, for a location that is only a place's name. One name at two
    places is not a place (None): a chain's stores. A site (plan: one name at several addresses that are one place,
    each entry's site = its primary's key) is one place: its primary entry (site_addresses: all of them). An entry
    without a site (made by hand, or imported before sites) is its address's own place, as before."""

    def __init__(self, entries: list[dict]):
        groups: dict[str, dict[tuple, dict]] = {}        # name -> {its place: the entry that stands for it}
        for e in entries:
            if not (e.get("address") or e.get("lat") is not None):
                continue
            site = e.get("site") or ""
            place = ("site", site) if site else ("address", name_key(e.get("address") or ""))
            primary = bool(site) and (e.get("key") == site or bool(e.get("site_addresses")))
            for n in [e.get("name") or "", *(e.get("aliases") or [])]:
                k = name_key(n)
                if k:
                    g = groups.setdefault(k, {})
                    if place not in g or primary:
                        g[place] = e
        self.names: dict[str, dict | None] = {k: next(iter(g.values())) if len(g) == 1 else None
                                              for k, g in groups.items()}

    def find(self, text: str, strip_town=None, town: str = "") -> dict | None:
        """The entry a location names, only when the location is nothing but that name: "@SAMPLE MARINA", "SAMPLE
        MARINA", or with a known town after it ("SAMPLE MARINA, SAMPLE_VILLAGE", "... - SAMPLE VILLAGE", "SAMPLE
        MARINA SAMPLE_VILLAGE"; strip_town: words -> (words without a known town at the end, that town in lower case)).
        Never one with anything before its "@" (an address, an intersection), never another town's entry: a town said
        (or town, the feed's own town field, in lower case) must be the entry's."""
        if not self.names:
            return None
        s = " ".join((text or "").split())
        at = s.find("@")
        if at >= 0:
            if re.search(r"[A-Za-z0-9]", s[:at]):
                return None                            # "EXAMPLE AVE/SAMPLE ST @SAMPLEMART": the street stays
            s = s[at + 1:].strip()
        if not s or "@" in s:
            return None

        def words(t: str) -> list[str]:
            return re.findall(r"[a-z0-9]+", t.lower().replace("_", " "))
        tries = [(s, "")]
        if strip_town is not None:
            m = re.match(r"(.+?)(?:\s*,\s*|\s+-\s+)([^,]+)$", s)     # "<name>, <town>" / "<name> - <town>"
            if m:
                rest, t = strip_town(words(m.group(2)))
                if t and not rest:
                    tries.append((m.group(1), t))
            rest, t = strip_town(words(s))                         # "<name> <TOWN_CODE>"
            if t and rest:
                tries.append((" ".join(rest), t))
        for name, said in tries:
            hit = self.names.get(name_key(name))
            if hit is None:
                continue
            theirs = (hit.get("muni") or "").lower()
            for t in (said, (town or "").lower()):
                if t and theirs and t != theirs:
                    return None                        # another town's place of that name
            return hit
        return None


# ----------------------------------------------------------------------------------- the address points

EXACT_STREET = 0.99         # the street's every word as typed (sizeup/geo/places.py score_alias: 1.0), never a prefix
POINT = {"exact": 1.0, "interpolated": 0.9}     # PlaceIndex._addresses' factor for each precision taken


NEAR_TOWN_KM = 0.4          # another town's hit is the row's own place only this close to the row's town's address
                            # points: a village sharing the town's ZIP files some of its addresses (the point is right,
                            # at or just over the line); two small villages side by side are often under 1.5 km
                            # across, so a same-named street a block or two into the neighbour is not taken
# "23.5", "189-1/2", "55 1/2": a fractional house number, found by its whole number (approximate)
FRACTION = re.compile(r"^\s*(\d+)(\.\d+|\s*-?\s*\d+\s*/\s*\d+)(?=\s|-)")
_TOWN_POINTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _town_points(gaz) -> dict[str, "np.ndarray"]:
    """{gazetteer town: its address points as an array of (lat, lon)}: each town's known extent (built once)."""
    got = _TOWN_POINTS.get(gaz)
    if got is None:
        import numpy as np
        pts: dict[str, list] = {}
        for st in gaz.streets.values():
            for muni, lst in st.addrs.items():
                pts.setdefault(muni, []).extend((r[2], r[3]) for r in lst)
        got = {m: np.asarray(v, dtype=float) for m, v in pts.items()}
        _TOWN_POINTS[gaz] = got
    return got


def _km_to_town(ix, town: str, town_of, lat: float, lon: float) -> float:
    """How far a point is from the nearest of a town's address points (inf: the gazetteer has none of that town)."""
    import numpy as np
    from sizeup.geo.places import M_PER_DEG_LAT, M_PER_DEG_LON
    best = float("inf")
    for muni, arr in _town_points(ix.gaz).items():
        if (town_of(muni) or muni).lower() != town.lower() or not len(arr):
            continue
        d = np.hypot((arr[:, 0] - lat) * M_PER_DEG_LAT, (arr[:, 1] - lon) * M_PER_DEG_LON).min() / 1000.0
        best = min(best, float(d))
    return best


def locate_address(ix, address: str, town: str = "", town_of=lambda m: m) -> dict | None:
    """An address in the gazetteer's address points through the place search (ix: sizeup/geo/places.py PlaceIndex): the
    house number on a street whose every word was given (no prefix: "12 EXAMPLE AVE" is never Example Ave Ext), exact
    or interpolated between two points of that street; a fractional number ("23.5", "189-1/2") by its whole number,
    approximate. The row's town first; another town's only when the street has the number in that one town and,
    with the row's town given, the point is within NEAR_TOWN_KM of the row's town (the same place filed under a
    neighbour), never a same-named street farther off. -> {label, lat, lon, muni (the row's town, when given; else the
    hit's), precision} | None."""
    from sizeup.geo.places import NUMBER, _Ctx
    own = ix.local_address(address, town, town_of,                  # the department's own point first (Settings)
                           near=lambda la, lo: _km_to_town(ix, town, town_of, la, lo) <= NEAR_TOWN_KM) \
        if hasattr(ix, "local_address") else None
    if own is not None:
        return own
    fraction = FRACTION.match(address or "")
    if fraction:                                        # "0189-1/2" -> "189"; "12 1/2-14" -> "12-14"
        address = str(int(fraction.group(1))) + address[fraction.end():]
    q = norm_tokens(address)
    if len(q) < 2 or not NUMBER.match(q[0]):
        return None
    tries = [q[1:]]
    if len(q) > 2 and q[1].isdigit():
        tries.append(q[2:])                             # "12-14 EXAMPLE AVE": the first number
    hits: list[dict] = []
    for rest in tries:
        hits = [h for h in ix._addresses(q[0], rest, _Ctx(None, ix.priority))     # quality: the street's x the number's
                if h.get("precision") in POINT and h["quality"] / POINT[h["precision"]] >= EXACT_STREET]
        if hits:
            break
    if not hits:
        return None
    for h in hits:
        h["town"] = town_of(h["sub"]) or h["sub"]
    same = [h for h in hits if town and h["town"].lower() == town.lower()]
    if not same:
        if town:                                        # only the row's town's own place, filed under a neighbour
            hits = [h for h in hits if _km_to_town(ix, town, town_of, h["lat"], h["lon"]) <= NEAR_TOWN_KM]
        if not hits or len({h["town"] for h in hits}) != 1:
            return None                                 # in no town near it, or in two other towns: can't tell
        same = hits
    best = max(same, key=lambda h: (h["precision"] == "exact", h["quality"]))
    label, precision = best["label"], best["precision"]
    if fraction:
        label = " ".join(fraction.group(0).split()) + label[len(str(int(fraction.group(1)))):]  # "189-1/2 Sample St"
        precision = "interpolated"                      # the whole number's point: next door, not the door itself
    return {"label": label, "lat": best["lat"], "lon": best["lon"], "muni": town or best["town"],
            "precision": precision}


# ----------------------------------------------------------------------------------- sites

SAME_PLACE_KM = 0.15        # one name at located addresses this close: one site (a building listed under "145", "149"
                            # and "149-151" of a street, a school's buildings across the street); a chain's stores are
                            # farther apart than that
SITE_SPAN_KM = 0.3          # a site's addresses all this close to one another: two buildings next door each way of the
                            # primary, a campus across a street; the pin (the primary) is never farther off than that
SAME_PLACE_NUMBERS = 20     # an address of that name with no point joins a site on its street this many numbers off
                            # (likely the next building)


def house(address: str) -> tuple[int | None, str]:
    """'149-151 EXAMPLE AVE' -> (149, 'example ave'); '55-1/2 SAMPLE ST' -> (55, 'sample st'); no number: (None, ...)."""
    m = FRACTION.match(address or "")
    q = norm_tokens(str(int(m.group(1))) + address[m.end():] if m else address)
    num = None
    while q and (q[0].isdigit() or (num is None and re.fullmatch(r"\d+[a-z]?", q[0]))):
        if num is None:
            num = int(re.match(r"\d+", q[0]).group())
        q = q[1:]
    return num, " ".join(q)


def group_sites(entries: dict[str, dict]) -> int:
    """One name at several addresses that are one place -> one site, in place: each of its entries gets site (the
    primary's key), the primary site_addresses (all of them, by street and number). Located entries of a name within
    SAME_PLACE_KM of one another are one site, as long as the site spans SITE_SPAN_KM at most (closest first: a name
    every ~120 m along a street is several places, never one long site); its primary: the lowest number located
    exactly (a single number before a range), else the first. An entry with no point joins the one site of its name
    with a located address on its street within SAME_PLACE_NUMBERS, in its town; otherwise it stays on its own (the
    same whatever the file's order). -> how many sites."""
    from sizeup.geo.places import dist_km
    order = {k: i for i, k in enumerate(entries)}
    by_name: dict[str, list[str]] = {}
    for k, e in entries.items():
        e.pop("site", None)
        e.pop("site_addresses", None)
        by_name.setdefault(name_key(e["name"]), []).append(k)
    n_sites = 0
    for keys in by_name.values():
        if len(keys) < 2:
            continue
        located = [k for k in keys if entries[k].get("lat") is not None]
        km = {(a, b): dist_km(entries[a]["lat"], entries[a]["lon"], entries[b]["lat"], entries[b]["lon"])
              for a in located for b in located}
        clusters = [[k] for k in located]
        while True:                  # the closest two whose link is SAME_PLACE_KM at most and span SITE_SPAN_KM at most
            best = None
            for i, a in enumerate(clusters):
                for j in range(i + 1, len(clusters)):
                    b = clusters[j]
                    link = min(km[x, y] for x in a for y in b)
                    if link <= SAME_PLACE_KM and max(km[x, y] for x in a + b for y in a + b) <= SITE_SPAN_KM \
                            and (best is None or link < best[0]):
                        best = (link, i, j)
            if best is None:
                break
            _link, i, j = best
            clusters[i] += clusters.pop(j)
        bare = [list(c) for c in clusters]               # an entry with no point joins by the located ones alone
        for k in (k for k in keys if entries[k].get("lat") is None):
            num, street = house(entries[k]["address"])
            town = (entries[k].get("muni") or "").lower()
            homes = [i for i, c in enumerate(bare) if num is not None and any(
                h[1] == street and h[0] is not None and abs(h[0] - num) <= SAME_PLACE_NUMBERS
                and (entries[o].get("muni") or "").lower() == town
                for o in c for h in (house(entries[o]["address"]),))]
            if len(homes) == 1:
                clusters[homes[0]].append(k)
        for c in clusters:
            if len(c) < 2:
                continue
            n_sites += 1
            c.sort(key=order.get)
            exact = [k for k in c if entries[k].get("located") == "exact"]
            def rank(k: str) -> tuple:                  # by number, "149" before "149-151", then the file's order
                a = entries[k]["address"]
                return house(a)[0] or 0, bool(re.match(r"\s*\d+[a-z]?\s*-\s*\d+(?!\s*/)", a, re.I)), order[k]
            primary = min(exact, key=rank) if exact else next(k for k in c if entries[k].get("lat") is not None)
            for k in c:
                entries[k]["site"] = primary
            by_street = sorted(c, key=lambda k: (house(entries[k]["address"])[1], rank(k)))
            entries[primary]["site_addresses"] = [entries[k]["address"] for k in by_street]
    return n_sites


# ----------------------------------------------------------------------------------- the plan

def plan(rows: list[list[str]], cols: dict[str, int], locate, town_of, today: str, current: dict[str, dict],
         known: set[str] | None = None) -> dict:
    """What an import would do. rows: the table, header first. locate(address, town) -> {label, lat, lon, muni,
    precision} | None (the gazetteer). town_of(words) -> the CAD's town. current: the entries of SOURCE now {key: value}.
    known: the CAD's towns (lower case; None: every town is known). A town code that is none of them: looked up as a
    row with no town, its muni the point's town (else the code's words), the code listed (unknown_towns).
    -> {entries: {key: value}, counts: {rows, names, new, present, removed, unresolved, skipped, approximate,
        unknown_towns (rows)}, unresolved: [{name, address, town}], unknown_towns: [the codes]}"""
    entries: dict[str, dict] = {}
    unresolved: list[dict] = []
    unknown: dict[str, int] = {}
    skipped = 0
    cache: dict[tuple[str, str], dict | None] = {}
    for r in rows[1:]:
        def cell(what: str) -> str:
            i = cols.get(what, -1)
            return " ".join(r[i].split()) if 0 <= i < len(r) else ""
        name, address, code = cell("name")[:60], cell("address")[:120], cell("town")
        if not name_key(name) or not name_key(address):
            skipped += 1
            continue
        town = town_of(town_code(code)) if code else ""
        key = entry_key(name, address)
        if key in entries:
            continue                                   # the same name at the same address twice
        mine = town                                    # the town the row is held to (none: an unknown code)
        if town and known is not None and town.lower() not in known:
            unknown[code] = unknown.get(code, 0) + 1
            mine = ""
        if (address.lower(), mine) not in cache:
            cache[(address.lower(), mine)] = locate(address, mine)
        hit = cache[(address.lower(), mine)]
        entries[key] = {"name": name, "aliases": [], "label": proper_name(name), "lat": hit["lat"] if hit else None,
                        "lon": hit["lon"] if hit else None, "muni": mine or (hit or {}).get("muni") or town, "note": "",
                        "address": address, "at": hit["label"] if hit else "", "source": SOURCE, "imported": today,
                        "located": hit["precision"] if hit else ""}
        if not hit:
            unresolved.append({"name": name, "address": address, "town": town})
    group_sites(entries)
    addresses = {name_key(v["address"]) for v in entries.values()}
    counts = {"rows": len(rows) - 1, "names": len(entries), "addresses": len(addresses),
              "new": sum(1 for k in entries if k not in current),
              "present": sum(1 for k in entries if k in current),
              "removed": sum(1 for k in current if k not in entries),
              "unresolved": len(unresolved),
              "unresolved_addresses": len({name_key(u["address"]) for u in unresolved}),
              "approximate": sum(1 for v in entries.values() if v["located"] == "interpolated"),
              "skipped": skipped, "unknown_towns": sum(unknown.values())}
    return {"entries": entries, "counts": counts, "unresolved": unresolved, "unknown_towns": sorted(unknown)}


# ----------------------------------------------------------------------------------- the maps' layer

def map_points(entries: list[dict]) -> dict:
    """The department's located names for the maps' "Common places" layer (static/common-places.js; GET
    /api/cad/places/map) as GeoJSON: one point per spot. A site once, at its primary (every address of it listed); an
    entry with no point never; names at one point (two businesses at one address) one feature together. Each feature:
    id (its number), properties {label: the first name, "+N" for more, places: [{name, addresses, town, hand}]} (hand:
    made by hand in Settings, its addresses: where it was picked when that is not its name). Alphabetical by name."""
    from sizeup.cad.sources import proper_case
    spots: dict[tuple[float, float], list[dict]] = {}
    for e in entries:
        if e.get("lat") is None or e.get("lon") is None:
            continue
        if e.get("site") and e.get("site") != e.get("key") and not e.get("site_addresses"):
            continue                                   # one of a site's other addresses: the primary stands for it
        hand = not is_imported(e.get("key") or "", e)
        if hand:
            name = e.get("name") or ""
            at = e.get("label") or ""
            addresses = [at] if at and name_key(at) != name_key(name) else []
        else:
            name = e.get("label") or proper_name(e.get("name") or "")
            addresses = [proper_case(a) for a in (e.get("site_addresses") or [e.get("address") or ""]) if a]
        if not name_key(name):
            continue
        spot = (round(float(e["lat"]), 5), round(float(e["lon"]), 5))
        spots.setdefault(spot, []).append({"name": name, "addresses": addresses, "town": e.get("muni") or "", "hand": hand})
    features = []
    for (lat, lon), places in sorted(spots.items(), key=lambda kv: min(p["name"].lower() for p in kv[1])):
        places.sort(key=lambda p: (p["name"].lower(), p["hand"]))
        seen, one = set(), []
        for p in places:                               # one name twice at one spot (made by hand and the county's)
            if name_key(p["name"]) not in seen:
                seen.add(name_key(p["name"]))
                one.append(p)
        label = one[0]["name"] + (f" +{len(one) - 1}" if len(one) > 1 else "")
        features.append({"type": "Feature", "id": len(features),
                         "geometry": {"type": "Point", "coordinates": [lon, lat]},
                         "properties": {"label": label, "places": one}})
    return {"type": "FeatureCollection", "features": features}
