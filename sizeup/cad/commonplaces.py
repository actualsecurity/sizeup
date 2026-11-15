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
    Lookup(entries).find(text) -> entry       a location that is only a place's name ("@SAMPLE MARINA", "SAMPLE MARINA,
                                              PORT_CHESTER"): the county feed's location (sizeup/cad/sources.py)

Each imported entry: {name, aliases: [], label (the name as people write it), lat, lon (None: the address did not
resolve), muni (the CAD's town), note: "", address (the address as the file has it), at (the address point's label,
"12 Example Ave"; "" when not found), source: SOURCE, imported
(YYYY-MM-DD), located: exact | interpolated | "" (not found)}. The address is looked up in the gazetteer's address
points through the place search's own street words (sizeup/geo/places.py PlaceIndex: "1 EXAMPLE AV" = "1 Example
Avenue"), the row's town first. A name whose address does not resolve is kept, flagged (located ""): the place search
and the radio skip it (no point), the county feed still maps the name to its address text.

A re-import replaces every entry of SOURCE and never touches a name made by hand (keys without the "county/" prefix).
The list is the county's: it lives in cad.sqlite (replicated with the rest of the CAD), never in the repo.
"""

from __future__ import annotations

import csv
import io
import re
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
    addresses is not a place (None)."""

    def __init__(self, entries: list[dict]):
        found: dict[str, dict | None] = {}
        for e in entries:
            if not (e.get("address") or e.get("lat") is not None):
                continue
            for n in [e.get("name") or "", *(e.get("aliases") or [])]:
                k = name_key(n)
                if not k:
                    continue
                if k in found and found[k] is not None and name_key(found[k].get("address") or "") != \
                        name_key(e.get("address") or ""):
                    found[k] = None                    # two places of that name
                elif k not in found:
                    found[k] = e
        self.names = found

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


def locate_address(ix, address: str, town: str = "", town_of=lambda m: m) -> dict | None:
    """An address in the gazetteer's address points through the place search (ix: sizeup/geo/places.py PlaceIndex): the
    house number on a street whose every word was given (no prefix: "12 EXAMPLE AVE" is never Example Ave Ext), exact
    or interpolated between two points of that street. The row's town first; another town only when the street has
    the number in one town alone. -> {label, lat, lon, muni (the CAD's town), precision} | None."""
    from sizeup.geo.places import NUMBER, _Ctx
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
        if len({h["town"] for h in hits}) != 1:
            return None                                 # in two other towns: can't tell
        same = hits
    best = max(same, key=lambda h: (h["precision"] == "exact", h["quality"]))
    return {"label": best["label"], "lat": best["lat"], "lon": best["lon"], "muni": best["town"],
            "precision": best["precision"]}


# ----------------------------------------------------------------------------------- the plan

def plan(rows: list[list[str]], cols: dict[str, int], locate, town_of, today: str, current: dict[str, dict]) -> dict:
    """What an import would do. rows: the table, header first. locate(address, town) -> {label, lat, lon, muni,
    precision} | None (the gazetteer). town_of(words) -> the CAD's town. current: the entries of SOURCE now {key: value}.
    -> {entries: {key: value}, counts: {rows, names, new, present, removed, unresolved, skipped, approximate},
        unresolved: [{name, address, town}]}"""
    entries: dict[str, dict] = {}
    unresolved: list[dict] = []
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
        if (address.lower(), town) not in cache:
            cache[(address.lower(), town)] = locate(address, town)
        hit = cache[(address.lower(), town)]
        entries[key] = {"name": name, "aliases": [], "label": proper_name(name), "lat": hit["lat"] if hit else None,
                        "lon": hit["lon"] if hit else None, "muni": (hit or {}).get("muni") or town, "note": "",
                        "address": address, "at": hit["label"] if hit else "", "source": SOURCE, "imported": today,
                        "located": hit["precision"] if hit else ""}
        if not hit:
            unresolved.append({"name": name, "address": address, "town": town})
    addresses = {name_key(v["address"]) for v in entries.values()}
    counts = {"rows": len(rows) - 1, "names": len(entries), "addresses": len(addresses),
              "new": sum(1 for k in entries if k not in current),
              "present": sum(1 for k in entries if k in current),
              "removed": sum(1 for k in current if k not in entries),
              "unresolved": len(unresolved),
              "unresolved_addresses": len({name_key(u["address"]) for u in unresolved}),
              "approximate": sum(1 for v in entries.values() if v["located"] == "interpolated"),
              "skipped": skipped}
    return {"entries": entries, "counts": counts, "unresolved": unresolved}
