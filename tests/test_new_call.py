"""A new call as the dispatcher takes it (#204): Location > Caller name > Caller address > Caller # > Event type > Event
subtype > Comments, Tab between them; a named place shown "<street address> @ <place>"; the cross streets from the
location's block; the big map beside the form (newcall-map.js) and what it asks the server (GET /api/cad/geocode/here).

The block and place checks run on the recorded street extract around 135 S Regent St (tests/fixtures/gazetteer/
regent_176.json, the #176 page tests' own), so a gazetteer rebuild can't move them. No network."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from sizeup.app import App
from sizeup.cad.api import CadApi, needs
from sizeup.cad.dispatcher import where
from sizeup.cloud.access import Identity
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.geo.pagegeo import block_of, location_detail, place_address, what_is_here
from sizeup.geo.places import PlaceIndex
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
EXTRACT = Path(__file__).parent / "fixtures" / "gazetteer" / "regent_176.json"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
REGENT_135 = (41.000791, -73.675162)          # the NYS address point of 135 S Regent St
ORDER = ["f-address", "f-name", "f-caddr", "f-phone", "f-type", "f-subtype", "f-remarks"]


@pytest.fixture(scope="module")
def regent(tmp_path_factory):
    d = json.loads(EXTRACT.read_text())
    path = tmp_path_factory.mktemp("gaz") / "regent.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", d["streets"])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,1)", d["addresses"])
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(s, m, json.dumps(c)) for s, m, c in d["lines"]])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", d["intersections"])
    db.executemany("INSERT INTO landmarks VALUES (?,?,?,?,?,?,?,?,?)", d["landmarks"])
    db.commit()
    db.close()
    return Gazetteer(path)


def wired(tmp_path, gaz):
    """A CAD wired to the gazetteer as the app wires it (sizeup/app.py)."""
    def geocoder(q):
        return [{k: getattr(m, k) for k in ("label", "lat", "lon", "kind", "municipality", "precision")}
                | {"confidence": round(float(m.confidence), 2)} | App._geocode_parts(m) for m in gaz.candidates(q)[:20]]
    cad = make_cad(tmp_path, geocoder=geocoder)
    cad.place_source = lambda: PlaceIndex(gaz)
    cad.detail_source = lambda m: location_detail(gaz, m)
    cad.here_source = lambda lat, lon, places: what_is_here(gaz, lat, lon, places)
    return cad


# --------------------------------------------------------------------------- cross streets from the block


def test_135_s_regent_st_is_between_clinton_and_william(regent):
    b = block_of(regent, *REGENT_135, "S Regent St")
    assert b["street"] == "S Regent St" and len(b["crosses"]) == 2
    assert "Clinton St" in b["crosses"] and any(c.endswith("William St") for c in b["crosses"])
    assert b["cross"] == " and ".join(b["crosses"])                 # as the page cross streets are written
    assert [e["name"] for e in b["ends"]] == b["crosses"]            # each end labelled on the map
    assert len(b["path"]) >= 2 and all(len(p) == 2 for p in b["path"])
    # the block's ends are where those streets meet S Regent St in the data (TIGER intersections)
    d = json.loads(EXTRACT.read_text())
    meets = {(round(r[2], 4), round(r[3], 4)) for r in d["intersections"] if 2165 in r[:2]}
    assert all((round(e["lat"], 4), round(e["lon"], 4)) in meets for e in b["ends"])


def test_no_street_near_or_no_ends_means_no_cross_streets(regent):
    assert block_of(regent, 41.05, -73.60, "S Regent St") is None              # far off its street: can't tell
    assert block_of(regent, *REGENT_135, "No Such St") is None
    assert block_of(regent, 41.05, -73.60) is None                             # a point near no street


def test_an_address_suggestion_carries_its_street_address_and_cross_streets(regent):
    m = {"kind": "address", "label": "135 S Regent St, Port Chester", "lat": REGENT_135[0], "lon": REGENT_135[1],
         "street": "S Regent St", "house_number": "135", "municipality": "Port Chester"}
    d = location_detail(regent, m)
    assert d["street_address"] == "135 S Regent St" and d["place"] == "" and d["address_checked"]
    assert "Clinton St" in d["cross"] and d["block"]["path"]
    for kind in ("intersection", "street", "mile marker", "exit"):            # nothing made up for these
        assert location_detail(regent, dict(m, kind=kind))["cross"] == ""


# --------------------------------------------------------------------------- named places: "address @ place"


def test_a_place_s_address_comes_from_the_places_data_checked_against_the_address_points(regent):
    # Corpus Christian Church: the county's facilities say "136 South Regent Street"; 136 S Regent St is an address
    # point: the gazetteer's spelling, and the block from it
    lm = next(x for x in regent.landmarks if x["name"] == "Corpus Christian Church")
    assert place_address(regent, lm["address"], lm["muni"], lm["lat"], lm["lon"])["address"] == "136 S Regent St"
    d = location_detail(regent, {"kind": "landmark", "place": lm["name"], "place_address": lm["address"],
                                 "lat": lm["lat"], "lon": lm["lon"], "municipality": lm["muni"]})
    assert (d["place"], d["street_address"], d["address_checked"]) == ("Corpus Christian Church", "136 S Regent St", True)
    assert d["cross"]
    # an address the address points don't have: as the data says it, never another one
    assert place_address(regent, "9999 South Regent Street", "Port Chester", lm["lat"], lm["lon"]) is None
    d = location_detail(regent, {"kind": "place", "place": "X", "place_address": "9999 South Regent Street",
                                 "lat": lm["lat"], "lon": lm["lon"], "municipality": "Port Chester"})
    assert (d["street_address"], d["address_checked"]) == ("9999 South Regent Street", False)


def test_a_place_with_no_address_in_the_data_gets_none(regent):
    lm = next(x for x in regent.landmarks if x["address"] == "")
    d = location_detail(regent, {"kind": "landmark", "place": lm["name"], "place_address": "", "lat": lm["lat"],
                                 "lon": lm["lon"], "municipality": lm["muni"]})
    assert d["place"] == lm["name"] and d["street_address"] == "" and not d["address_checked"]


def test_the_location_box_shows_a_place_with_its_address(tmp_path, regent):
    cad = wired(tmp_path, regent)
    ms = cad.geocode("corpus christian church")["matches"]
    m = next(x for x in ms if x.get("place") == "Corpus Christian Church")
    assert m["street_address"] == "136 S Regent St" and m["cross"] and m["block"]["ends"]
    a = cad.geocode("135 s regent st")["matches"][0]
    assert a["street_address"] == "135 S Regent St" and "Clinton St" in a["cross"] and a["place"] == ""
    # no gazetteer wiring: the suggestions as before, the new keys empty
    plain = make_cad(tmp_path / "p", geocoder=lambda q: [{"label": "1 Main St", "lat": 41.0, "lon": -73.67, "kind": "address",
                                                          "municipality": "Port Chester", "confidence": 0.9,
                                                          "street": "Main St", "house_number": "1"}])
    m = plain.geocode("1 main")["matches"][0]
    assert (m["street_address"], m["cross"], m["block"], m["place"]) == ("1 Main St", "", None, "")


def event(cad, **kw):
    return cad.create(dict({"type": "SERVICE", "subtype": "LIFT", "operator": "JD"}, **kw))


def test_an_incident_keeps_the_place_and_its_address_apart(tmp_path):
    cad = make_cad(tmp_path)
    inc = event(cad, address="40 Example Rd @ Sample Market", place="Sample Market", lat=40.99242, lon=-73.675505)
    assert (inc["address"], inc["place"], inc["place_address"]) == \
        ("40 Example Rd @ Sample Market", "Sample Market", "40 Example Rd")
    # no street address in the data: "@ <place>" at its own point; the dispatcher types the address in front later
    inc = event(cad, address="", place="Lyon Park", lat=41.001, lon=-73.664)
    assert (inc["address"], inc["place"], inc["place_address"], inc["lat"]) == ("@ Lyon Park", "Lyon Park", "", 41.001)
    inc = cad.update(inc["id"], {"address": "12 Lyon Pl @ Lyon Park", "lat": 41.001, "lon": -73.664, "operator": "JD"})
    assert (inc["place"], inc["place_address"]) == ("Lyon Park", "12 Lyon Pl")
    # the address no longer names the place: it goes
    inc = cad.update(inc["id"], {"address": "209 Westchester Ave", "operator": "JD"})
    assert (inc["address"], inc["place"], inc["place_address"]) == ("209 Westchester Ave", "", "")
    plain = event(cad, address="209 Westchester Ave")
    assert (plain["place"], plain["place_address"]) == ("", "")
    log = [e["text"] for e in cad.store.q("SELECT text FROM events WHERE incident_id = ?", (inc["id"],))]
    assert any("at @ Lyon Park" in t for t in log) and any("location: 12 Lyon Pl @ Lyon Park" in t for t in log)
    assert not any("place_address" in t or "place address" in t for t in log)


def test_said_on_the_air_without_the_at_sign():
    w = where({"address": "40 Example Rd @ Sample Market", "cross_street": "S Regent St and High St"})
    assert "@" not in w and "Sample Market" in w and "40" in w and "cross of" in w
    assert where({"address": "@ Lyon Park"}) == "Lyon Park"


def test_an_old_cad_file_gets_the_place_columns(tmp_path):
    db = tmp_path / "cad.sqlite"
    make_cad(tmp_path)
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE incidents DROP COLUMN place")
    con.execute("ALTER TABLE incidents DROP COLUMN place_address")
    con.commit()
    con.close()
    cad = make_cad(tmp_path)
    assert event(cad, address="x @ Y", place="Y")["place_address"] == "x"


# --------------------------------------------------------------------------- the caller


def test_the_caller_is_stored_logged_and_shown(tmp_path):
    cad = make_cad(tmp_path)
    inc = event(cad, address="135 S Regent St", caller_name="JANE DOE", caller_address="135 S Regent St",
                phone="914-555-0100", remarks="smoke from the loading dock")
    full = cad.full(inc["id"])
    assert (full["caller_name"], full["caller_address"], full["phone"], full["remarks"]) == \
        ("JANE DOE", "135 S Regent St", "914-555-0100", "smoke from the loading dock")
    texts = [e["text"] for e in cad.store.q("SELECT text FROM events WHERE incident_id = ?", (inc["id"],))]
    assert "Caller info: JANE DOE · 135 S Regent St · 914-555-0100" in texts
    cad.update(inc["id"], {"phone": "914-555-0199", "operator": "JD"})
    texts = [e["text"] for e in cad.store.q("SELECT text FROM events WHERE incident_id = ?", (inc["id"],))]
    assert any("phone: 914-555-0199" in t for t in texts)
    js = (STATIC / "cad.js").read_text()
    assert 'setText($("#d-caller"), caller ? `Caller: ${caller}` : "")' in js            # shown in the incident


def test_caller_details_never_go_out_in_a_push():
    # the pushes say who, the incident number and its address (sizeup/notify.py Who), nothing of the caller
    notify = (STATIC.parent.parent / "notify.py").read_text()
    alarms = (STATIC.parent.parent / "cad" / "alarms.py").read_text()
    for src in (notify, alarms, (STATIC / "newcall-map.js").read_text()):
        assert not re.search(r'caller_name|caller_address|\["phone"\]|\.phone\b', src)


# --------------------------------------------------------------------------- the map's "what is here"


def test_what_is_at_a_map_click(tmp_path, regent):
    cad = wired(tmp_path, regent)
    code, raw, _ = CadApi(cad).get("/api/cad/geocode/here", f"lat={REGENT_135[0]}&lon={REGENT_135[1]}", None)
    r = json.loads(raw)
    assert code == 200 and r["address"]["label"] == "135 S Regent St, Port Chester"
    assert r["address"]["street_address"] == "135 S Regent St" and "Clinton St" in r["address"]["cross"]
    assert r["street"]["name"] == "S Regent St"                    # the street under it: offered as a cross street
    lm = next(x for x in regent.landmarks if x["name"] == "Corpus Christian Church")
    r = json.loads(CadApi(cad).get("/api/cad/geocode/here", f"lat={lm['lat']}&lon={lm['lon']}", None)[1])
    assert r["place"]["place"] == "Corpus Christian Church" and r["place"]["street_address"] == "136 S Regent St"
    for bad in ("lat=x&lon=1", "lat=41&lon=-80", "lat=41"):
        assert CadApi(cad).get("/api/cad/geocode/here", bad, None)[0] == 400, bad
    alone = make_cad(tmp_path / "a")
    assert json.loads(CadApi(alone).get("/api/cad/geocode/here", "lat=41&lon=-73.67", None)[1])["error"]


# --------------------------------------------------------------------------- permissions: unchanged


def test_permissions_are_unchanged(tmp_path, regent):
    assert needs("/api/cad/incident") == "dispatch" and needs("/api/cad/incident/5") == "dispatch"
    api = CadApi(wired(tmp_path, regent))
    people(api.cad)
    assert call(api, "/api/cad/incident", {"type": "STRU", "address": "x @ Y", "place": "Y"}, VAL)[0] == 403
    code, inc = call(api, "/api/cad/incident", {"type": "STRU", "address": "x @ Y", "place": "Y"}, JULIAN)
    assert code == 200 and inc["place"] == "Y"
    assert call(api, f"/api/cad/incident/{inc['id']}", {"address": "z @ Y"}, VAL)[0] == 403
    assert call(api, f"/api/cad/incident/{inc['id']}", {"address": "z @ Y"}, COREY)[0] == 200
    # the map's lookups only read: anyone signed in, a viewer too
    for path, q in (("/api/cad/geocode/here", f"lat={REGENT_135[0]}&lon={REGENT_135[1]}"), ("/api/cad/geocode", "q=135+s+regent")):
        assert api.get(path, q, Identity(email=VAL))[0] == 200
    # a viewer's console locks the whole form (cad.js RO_DISPATCH), the new fields included
    js = (STATIC / "cad.js").read_text()
    assert '#new-form :is(input, select, textarea, #new-create)' in js
    posts = set(re.findall(r'api\("POST", `?"?(/api/cad/[^`"$?]+)', (STATIC / "newcall-map.js").read_text()))
    assert not posts                                                # the map changes the form, never the record


# --------------------------------------------------------------------------- the form: order and keys


class _Tabbable(HTMLParser):
    """The new-incident form's elements in document order: id, tag, tabindex, whether hidden by an ancestor."""

    FOCUSABLE = {"input", "select", "textarea", "button", "a", "summary"}
    VOID = {"input", "br", "img", "hr", "meta", "link", "use"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.inside = False
        self.hidden_at: list[int] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == "new-form":
            self.inside, self.depth = True, 0
        if not self.inside:
            return
        if tag not in self.VOID:
            self.depth += 1
            if "hidden" in a and a.get("id") != "new-form":      # the form itself: shown when it opens
                self.hidden_at.append(self.depth)
        if tag in self.FOCUSABLE or "tabindex" in a:
            self.items.append({"id": a.get("id", ""), "tag": tag, "tabindex": a.get("tabindex"),
                               "hidden": bool(self.hidden_at), "type": a.get("type", "")})

    def handle_endtag(self, tag):
        if not self.inside or tag in self.VOID:
            return
        if self.hidden_at and self.hidden_at[-1] == self.depth:
            self.hidden_at.pop()
        self.depth -= 1
        if self.depth == 0:
            self.inside = False


def tab_sequence():
    p = _Tabbable()
    p.feed((STATIC / "cad.html").read_text())
    return [x for x in p.items if x["tabindex"] != "-1" and not x["hidden"]], p.items


def test_the_seven_fields_come_first_in_the_tab_order_with_nothing_between():
    seq, every = tab_sequence()
    assert [x["id"] for x in seq[:7]] == ORDER                     # Location ... Comments, in that order
    assert not any(x["tabindex"] and x["tabindex"] not in ("-1", "0") for x in every)   # no positive tabindex
    first, last = [x["id"] for x in every].index("f-address"), [x["id"] for x in every].index("f-remarks")
    between = [x["id"] for x in every[first:last + 1] if x["id"] not in ORDER]
    assert between == ["f-cross", "f-caddr-same"]                  # the cross streets and the shortcut: tabindex -1
    assert all(x["tabindex"] == "-1" for x in every if x["id"] in between)
    cancel = next(x for x in every if x["id"] == "new-cancel")
    assert cancel["tabindex"] == "-1"                              # before Location, out of the way
    # the rest after Comments; the map last (after Create)
    rest = [x["id"] for x in seq[7:]]
    assert rest.index("f-via") < rest.index("f-emergency") < rest.index("new-create")
    html = (STATIC / "cad.html").read_text()
    form = html[html.index('<form id="new-form"'):html.index("</form>", html.index('<form id="new-form"'))]
    assert form.index('id="new-create"') < form.index('id="nc-split"') < form.index('id="nc-map-pane"')
    labels = re.findall(r">\s*(Location|Caller name|Caller address|Caller #|Event type|Event subtype|Comments)(?=[\s<])", form)
    assert labels == ["Location", "Caller name", "Caller address", "Caller #", "Event type", "Event subtype", "Comments"]


def test_the_form_s_keys():
    js = (STATIC / "cad.js").read_text()
    assert 'const NEW_CALL_ORDER = ["#f-address", "#f-name", "#f-caddr", "#f-phone", "#f-type", "#f-subtype", "#f-remarks"];' in js
    open_new = js[js.index("function openNew()"):js.index("// The new call's fields")]
    assert '$("#f-address").focus();' in open_new and '$("#f-type").focus()' not in open_new
    box = js[js.index("function addressBox("):js.index("// ---- new incident / edit")]
    # Tab takes the highlighted suggestion (and the browser goes on); Esc closes the list and stays; Enter untouched
    assert 'e.key === "Tab"' in box and "if (open && !e.shiftKey) box.take(box.items[box.idx]);" in box
    assert 'e.key === "Escape" && !list.hidden' in box and "e.preventDefault();" in box
    assert '"Enter"' not in box
    assert 'e.key === "Enter" && (e.ctrlKey || e.metaKey)' in js and '$("#new-form").requestSubmit()' in js
    assert "locationText(m)" in box and "`${m.street_address} @ ${m.place}` : `@ ${m.place}`" in js
    # the cross streets follow the location unless written by hand
    assert "if (!c.value.trim() || c.value.trim() === box.autoCross)" in box
    sheet = (STATIC / "ui.js").read_text()
    assert '[["Ctrl/⌘ Enter"]' in sheet


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_tab_order_in_a_dom():
    """With jsdom (when node can find it): the form's sequential focus order, as a browser computes it."""
    probe = subprocess.run([NODE, "-e", "require.resolve('jsdom')"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip("jsdom not installed")
    script = r"""
const { JSDOM } = require("jsdom");
const html = require("fs").readFileSync(process.argv[1], "utf8");
const doc = new JSDOM(html).window.document;
const form = doc.getElementById("new-form");
form.hidden = false;
const hiddenUp = (n) => { for (; n && n !== form.parentNode; n = n.parentNode) if (n.hidden) return true; return false; };
const out = [...form.querySelectorAll("input, select, textarea, button, a[href], [tabindex]")]
  .filter((n) => n.tabIndex >= 0 && !n.disabled && !hiddenUp(n)).map((n) => n.id);
console.log(JSON.stringify(out));
"""
    r = subprocess.run([NODE, "-e", script, str(STATIC / "cad.html")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)[:7] == ORDER


# --------------------------------------------------------------------------- the big map


def test_the_map_is_made_once_and_never_takes_the_form_s_tab():
    src = (STATIC / "newcall-map.js").read_text()
    assert "if (!st.rm) make();" in src and src.count("new CadMap.RigMap(") == 1
    assert 'layers: "incident"' in src and "manual: true" in src               # its usual Layers box, its own camera
    assert "hd.tabIndex = -1" in src                                            # the splitter: not in the Tab order
    assert "draggable: true" in src and 'st.pin.on("dragend"' in src
    # a drag keeps the address as typed and asks; only "Use this address" changes it
    drag = src[src.index("async function dragged("):src.index("// ---- open / follow")]
    assert "newAddr.move(lat, lon);" in drag and '"Use this address"' in drag and "use(a)" in drag
    assert "newAddr.take(" not in drag
    assert '"Use this location"' in src and '"Add as cross street"' in src
    for m in re.finditer(r"localStorage\.\w+Item", src):                       # storage may refuse: never throws
        before = src[:m.start()]
        assert before.rfind("try {") > before.rfind("} catch"), m.group(0)
    cadmap = (STATIC / "cadmap.js").read_text()
    assert "if (!this.userMoved && !this.opts.manual) this.fit(!first);" in cadmap
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/cadmap.js"') < html.index('src="/static/newcall-map.js"') < html.index('src="/static/panels.js"')
    css = (STATIC / "cad.css").read_text()
    assert ".nc-body.nc-stacked" in css and ".nc-map-pane" in css
