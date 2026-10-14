"""The county GIS layers (sizeup/gis.py, #91): the ArcGIS REST query client against recorded fixtures
(tests/fixtures/wcgis/: a few real features of each starter layer, and each layer's description as the
service gives it), the catalog and layer endpoints (viewers, ETag, gzip, the bbox tiles), the config,
and the page's static checks. Never the county itself (conftest.py refuses any host but this one)."""

import gzip
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from sizeup import gis as gis_mod
from sizeup.config import ConfigError, load
from sizeup.gis import (BACKOFF_S, DEFAULT_LAYERS, PAGE, TILE_MAX_DEG, Gis, Layer, contains, fetch_features, geometry_bbox,
                        parse_bbox, query_url, simplify_line, tidy_feature, tidy_geometry)
from tests.test_access import AUD, TEAM, jwks, jwt, keys, req  # noqa: F401  (fixtures)
from tests.test_cloud import HubRun, hub_cfg
from tests.test_settings import post

FIX = Path(__file__).parent / "fixtures" / "wcgis"
STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
BY_KEY = {l.key: l for l in DEFAULT_LAYERS}


def fixture(name: str):
    return json.loads((FIX / name).read_text())


class Clock:
    def __init__(self, t=1_791_915_000.0):
        self.t = t

    def __call__(self):
        return self.t


def arcgis_answer(url: str, fixtures: dict, page_limit: int | None = None) -> tuple[int, dict, bytes]:
    """What the county's REST service answers for one of our URLs, from the fixtures: <layer>?f=json is the
    layer's description, <layer>/query?... one page of its features (resultOffset / resultRecordCount honoured,
    exceededTransferLimit set while there are more)."""
    u = urlsplit(url)
    q = parse_qs(u.query)
    path = u.path.rstrip("/")
    is_query = path.endswith("/query")
    layer_url = path[:-len("/query")] if is_query else path
    key = next((k for k, l in fixtures.items() if l["url"].endswith(layer_url) or layer_url.endswith(urlsplit(l["url"]).path)), None)
    if key is None:
        return 404, {}, b'{"error":{"code":404,"message":"no such layer"}}'
    info, feats = fixtures[key]["info"], fixtures[key]["features"]
    if not is_query:
        return 200, {"content-type": "application/json"}, json.dumps(info).encode()
    assert q.get("f") == ["geojson"] and q.get("outSR") == ["4326"] and q.get("returnGeometry") == ["true"]
    off, n = int(q.get("resultOffset", ["0"])[0]), int(q.get("resultRecordCount", [str(PAGE)])[0])
    n = min(n, page_limit or n, int(info.get("maxRecordCount") or PAGE))
    if q.get("outFields", ["*"])[0] != "*":
        want = q["outFields"][0].split(",")
        feats = [{**f, "properties": {k: v for k, v in f["properties"].items() if k in want}} for f in feats]
    if q.get("geometry"):
        w, s, e, nn = (float(x) for x in q["geometry"][0].split(","))
        feats = [f for f in feats if (b := geometry_bbox(f["geometry"])) and b[0] <= e and b[2] >= w and b[1] <= nn and b[3] >= s]
    page = feats[off:off + n]
    doc = {"type": "FeatureCollection", "features": page}
    if off + n < len(feats):
        doc["exceededTransferLimit"] = True
    return 200, {"content-type": "application/geo+json"}, json.dumps(doc).encode()


class FakeFetch:
    """The fetch Gis is given: the county's answers from the fixtures, every URL recorded."""

    def __init__(self, keys=None, page_limit=None):
        self.urls: list[str] = []
        self.fixtures = {}
        for k in (keys or [l.key for l in DEFAULT_LAYERS]):
            self.fixtures[k] = {"url": BY_KEY[k].url, "info": fixture(f"{k}.info.json"), "features": fixture(f"{k}.geojson")["features"]}
        self.fail: dict[str, Exception] = {}
        self.page_limit = page_limit

    def __call__(self, url, timeout, limit):
        self.urls.append(url)
        for k, exc in self.fail.items():
            if BY_KEY[k].url in url:
                raise exc
        return arcgis_answer(url, self.fixtures, self.page_limit)


def make(tmp_path=None, layers=None, fetch=None, clock=None, **kw):
    logs = []
    g = Gis(layers if layers is not None else DEFAULT_LAYERS, cache_dir=(tmp_path / "gis") if tmp_path else None,
            fetch=fetch or FakeFetch(), log=logs.append, clock=clock or Clock(), **kw)
    g.logs = logs
    return g


# ---- the starter set and its fixtures -------------------------------------------------------------------------


def test_the_starter_set_is_complete_and_each_dataset_has_its_terms_and_a_fixture():
    assert len(DEFAULT_LAYERS) >= 6 and len({l.key for l in DEFAULT_LAYERS}) == len(DEFAULT_LAYERS)
    for l in DEFAULT_LAYERS:
        assert gis_mod.URL_RE.fullmatch(l.url), l.key
        assert l.licence and l.page.startswith("https://gis.westchestercountyny.gov/"), l.key
        assert l.fields, f"{l.key}: the fields shown on a click"
        assert (FIX / f"{l.key}.info.json").is_file() and (FIX / f"{l.key}.geojson").is_file(), l.key
        info, feats = fixture(f"{l.key}.info.json"), fixture(f"{l.key}.geojson")["features"]
        assert 1 <= len(feats) <= 12, f"{l.key}: a few real features, not a dump"
        names = {f["name"] for f in info["fields"]}
        for name, _ in l.fields:
            assert name in names, f"{l.key}: {name} isn't a field of the service ({sorted(names)})"
        if l.ours_field:
            assert l.ours_field in names
        assert gis_mod.GEOM_KIND[info["geometryType"]] == (l.kind or gis_mod.GEOM_KIND[info["geometryType"]]), l.key
    parcels = BY_KEY["parcels"]
    assert parcels.tiled and parcels.minzoom >= 15 and parcels.refresh == "weekly" and parcels.simplify_m > 0
    assert parcels.bbox and parcels.bbox[0] >= -73.78 and parcels.bbox[2] <= -73.62      # our four towns, not the county
    assert BY_KEY["fire_districts"].on and BY_KEY["fire_districts"].ours_field
    assert not any(l.on for l in DEFAULT_LAYERS if l.key not in ("fire_districts", "municipalities"))


# ---- the query, the pages, the geometry ------------------------------------------------------------------------------


def test_the_query_url_asks_for_geojson_in_our_box_with_only_the_fields_shown():
    u = query_url("https://gis.example/arcgis/rest/services/X/MapServer/3", (-73.7, 40.9, -73.6, 41.0), "MUNI='Rye'",
                  ("ADDR", "OWNER"), 2000, 500)
    q = parse_qs(urlsplit(u).query)
    assert urlsplit(u).path.endswith("/MapServer/3/query")
    assert q["f"] == ["geojson"] and q["outSR"] == ["4326"] and q["outFields"] == ["ADDR,OWNER"] and q["where"] == ["MUNI='Rye'"]
    assert q["geometry"] == ["-73.7,40.9,-73.6,41.0"] and q["geometryType"] == ["esriGeometryEnvelope"] and q["inSR"] == ["4326"]
    assert q["resultOffset"] == ["2000"] and q["resultRecordCount"] == ["500"] and q["geometryPrecision"] == ["6"]
    plain = parse_qs(urlsplit(query_url("https://gis.example/MapServer/3")).query)
    assert plain["where"] == ["1=1"] and plain["outFields"] == ["*"] and "geometry" not in plain


def test_pages_follow_result_offset_until_the_service_has_no_more():
    f = FakeFetch(["parcels"], page_limit=2)
    layer = BY_KEY["parcels"]
    feats = fetch_features(f, layer, None, 2)
    assert len(feats) == len(f.fixtures["parcels"]["features"])
    offsets = [int(parse_qs(urlsplit(u).query)["resultOffset"][0]) for u in f.urls]
    assert offsets == list(range(0, len(feats) + 1, 2))[:len(offsets)] and len(offsets) >= 2
    # the service's maxRecordCount caps a page, whatever we ask
    g = make(fetch=FakeFetch(["parcels"]))
    g.fetch.fixtures["parcels"]["info"]["maxRecordCount"] = 2
    assert g.refresh("parcels")
    assert all(int(parse_qs(urlsplit(u).query)["resultRecordCount"][0]) <= 2 for u in g.fetch.urls if "/query?" in u)


def test_an_arcgis_error_in_a_200_is_an_error():
    def fetch(url, timeout, limit):
        if url.endswith("?f=json"):
            return 200, {}, json.dumps(fixture("parcels.info.json")).encode()
        return 200, {}, b'{"error":{"code":400,"message":"Invalid query","details":["where"]}}'
    g = make(fetch=fetch, layers=[BY_KEY["parcels"]])
    assert not g.refresh("parcels") and "Invalid query" in g.status["parcels"]["error"]
    nope = Layer(key="nope", label="x", url="https://gis.example/arcgis/rest/services/X/MapServer/9")
    g2 = make(fetch=lambda u, t, l: (200, {}, b'{"name":"x","capabilities":"Map","geometryType":"esriGeometryPoint"}'), layers=[nope])
    assert not g2.refresh("nope") and "doesn't allow queries" in g2.status["nope"]["error"]


def test_douglas_peucker_drops_the_in_between_points_and_keeps_rings_closed():
    # a straight line with a wobble of 0.2 m: gone at 0.5 m, kept at 0.1 m
    line = [[-73.66, 41.0], [-73.6595, 41.0000018], [-73.659, 41.0], [-73.6585, 41.0], [-73.658, 41.0]]
    assert simplify_line(line, 0.5) == [[-73.66, 41.0], [-73.658, 41.0]]
    assert len(simplify_line(line, 0.1)) >= 3
    assert simplify_line(line, 0) is line
    # a corner that matters (10 m) stays
    bend = [[-73.66, 41.0], [-73.659, 41.0], [-73.659, 41.0001], [-73.658, 41.0001]]
    assert simplify_line(bend, 0.5) == [[-73.66, 41.0], [-73.659, 41.0], [-73.659, 41.0001], [-73.658, 41.0001]]
    ring = [[0, 0], [0.00001, 0.000001], [0.00002, 0], [0.00001, -0.000001], [0, 0]]       # a sliver: never under 4 points
    assert len(simplify_line(ring, 5, ring=True)) >= 4
    g = tidy_geometry({"type": "Polygon", "coordinates": [[[-73.66, 41.0], [-73.6595, 41.0000018], [-73.659, 41.0],
                                                            [-73.659, 41.0001], [-73.66, 41.0001], [-73.66, 41.0]]]}, 0.5)
    assert g["coordinates"][0][0] == g["coordinates"][0][-1] and len(g["coordinates"][0]) == 5
    assert tidy_geometry({"type": "Point", "coordinates": [-73.66012345678, 41.00098765432]}) == {"type": "Point", "coordinates": [-73.660123, 41.000988]}
    assert tidy_geometry(None) is None and tidy_geometry({"type": "Polygon", "coordinates": []}) is None
    assert tidy_geometry({"type": "LineString", "coordinates": [[0, 0]]}) is None


def test_a_feature_keeps_only_the_fields_shown_and_ours_is_marked():
    layer = Layer(key="t", label="T", url="https://gis.example/arcgis/rest/services/X/MapServer/1",
                  fields=(("NAME", "Name"), ("OWNER", "Owner")), ours_field="NAME")
    f = {"type": "Feature", "id": 7, "properties": {"NAME": "PORT CHESTER FIRE DISTRICT", "OWNER": "", "SHAPE_Area": 12.5, "X": None},
         "geometry": {"type": "Point", "coordinates": [-73.66, 41.0]}}
    t = tidy_feature(f, layer)
    assert t == {"type": "Feature", "id": 7, "properties": {"NAME": "PORT CHESTER FIRE DISTRICT"}, "geometry": {"type": "Point", "coordinates": [-73.66, 41.0]}}
    g = make(layers=[layer], ours=("Port Chester FD", "Rye Brook FD"))
    assert g.is_ours(layer, {"NAME": "PORT CHESTER FIRE DISTRICT"}) and g.is_ours(layer, {"NAME": "Rye Brook"})
    assert g.is_ours(layer, {"NAME": "PORT CHESTER F.D."}) and g.is_ours(layer, {"NAME": "Village of Port Chester"})
    assert not g.is_ours(layer, {"NAME": "RYE FD"}) and not g.is_ours(layer, {"NAME": "Rye"})        # Rye City is not Rye Brook
    assert not g.is_ours(layer, {"NAME": "Harrison"}) and not g.is_ours(layer, {}) and not g.is_ours(layer, {"NAME": "PURCHASE FD"})
    assert tidy_feature({"type": "Feature", "properties": {"A": 1}, "geometry": None}, layer) is None


def test_bbox_parsing_and_point_in_polygon():
    assert parse_bbox("-73.7,40.9,-73.6,41.0") == (-73.7, 40.9, -73.6, 41.0)
    for bad in ("x", "1,2,3", "-73.6,40.9,-73.7,41.0", "-73.9,40.9,-73.5,41.0"):
        with pytest.raises(ValueError):
            parse_bbox(bad, TILE_MAX_DEG)
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]], [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5], [0.5, 0.5]]]}
    assert contains(sq, 0.2, 0.2) and not contains(sq, 1, 1) and not contains(sq, 3, 1)       # the hole is out
    assert contains({"type": "MultiPolygon", "coordinates": [sq["coordinates"][:1]]}, 1, 1)
    assert not contains({"type": "Point", "coordinates": [1, 1]}, 1, 1)
    assert geometry_bbox(sq) == (0, 0, 2, 2) and geometry_bbox({"type": "Point", "coordinates": []}) is None


# ---- the service: refresh, cache, schedule, backoff ----------------------------------------------------------------


def test_a_refresh_reads_the_layer_through_our_box_and_caches_it_on_disk(tmp_path):
    f = FakeFetch()
    g = make(tmp_path, fetch=f)
    assert g.configured and sorted(g.due()) == sorted(BY_KEY)
    for key in BY_KEY:
        assert g.refresh(key), (key, g.status[key]["error"])
        st = g.status[key]
        assert st["n"] == len(f.fixtures[key]["features"]) and st["error"] == "" and st["bytes"] > 0
        assert (tmp_path / "gis" / f"{key}.geojson").is_file()
        assert any(u.startswith(BY_KEY[key].url + "?f=json") for u in f.urls)
        q = parse_qs(urlsplit(next(u for u in f.urls if u.startswith(BY_KEY[key].url + "/query?"))).query)
        box = BY_KEY[key].bbox or g.bbox
        assert q["geometry"] == [",".join(str(x) for x in box)]
        assert q["outFields"] == [",".join(n for n, _ in BY_KEY[key].fields)]
    assert g.due() == []
    assert any("features" in m and "MB" in m for m in g.logs)
    # a restart reads the cache and fetches nothing until each layer is due
    f2 = FakeFetch()
    again = make(tmp_path, fetch=f2)
    assert again.due() == [] and f2.urls == []
    assert {k: s["n"] for k, s in again.status.items()} == {k: s["n"] for k, s in g.status.items()}
    pub = again.public()
    assert pub["configured"] and pub["attribution"] == "Westchester County GIS" and pub["source"] == "Westchester County GIS"
    assert [l["key"] for l in pub["layers"]] == [l.key for l in DEFAULT_LAYERS]
    for l in pub["layers"]:
        assert set(l) >= {"key", "label", "group", "kind", "style", "minzoom", "fields", "on", "refresh", "tiled", "licence",
                          "page", "asof", "at", "n", "bytes", "error", "updated", "rev"}
        assert l["kind"] in ("point", "line", "polygon")
    # a changed entry (another box, other fields) is fetched afresh, not read from the cache
    other = [Layer(**{**BY_KEY["parcels"].__dict__, "bbox": (-73.7, 40.95, -73.65, 41.0)})]
    assert make(tmp_path, layers=other).due() == ["parcels"]
    (tmp_path / "gis" / "parcels.geojson").write_text("{nope")
    assert "parcels" in make(tmp_path).due()


def test_the_schedule_is_slow_and_a_failure_backs_off(tmp_path):
    clock, f = Clock(), FakeFetch()
    g = make(tmp_path, fetch=f, clock=clock)
    for key in BY_KEY:
        g.refresh(key)
    assert g.due() == []
    clock.t += 86400 + 1
    assert set(g.due()) == {k for k, l in BY_KEY.items() if l.refresh == "daily"}
    clock.t += 6 * 86400
    assert set(g.due()) >= {k for k, l in BY_KEY.items() if l.refresh in ("daily", "weekly")}
    f.fail["parcels"] = HTTPError(BY_KEY["parcels"].url, 503, "Service Unavailable", None, None)
    assert not g.refresh("parcels")
    st = g.status["parcels"]
    assert st["error"] == "HTTP 503" and st["next"] == clock.t + BACKOFF_S[0] and st["n"] > 0     # the old data stays
    assert not g.refresh("parcels") and st["next"] == clock.t + 2 * BACKOFF_S[0]
    for _ in range(12):
        g.refresh("parcels")
    assert st["next"] == clock.t + BACKOFF_S[1]
    f.fail["parcels"] = OSError("Name or service not known")
    assert not g.refresh("parcels") and "OSError" in st["error"]
    del f.fail["parcels"]
    assert g.refresh("parcels") and st["error"] == "" and st["fails"] == 0
    assert any("next try in" in m for m in g.logs)


def test_nothing_configured_or_disabled_fetches_nothing_and_says_so(tmp_path):
    f = FakeFetch()
    g = make(tmp_path, layers=[], fetch=f)
    assert not g.configured and g.due() == [] and f.urls == []
    g.start()
    assert g._thread is None and "no county GIS layers" in g.logs[-1]
    assert g.public()["configured"] is False and g.public()["layers"] == []
    off = make(tmp_path, fetch=f, enabled=False)
    assert not off.configured and not off.refresh("parcels") and f.urls == []
    assert not (tmp_path / "gis").exists()


def test_district_at_finds_the_fire_district_a_point_is_in():
    g = make(layers=[BY_KEY["fire_districts"]], ours=("Port Chester FD", "Rye Brook FD"))
    assert g.refresh("fire_districts")
    feats = g.data["fire_districts"]
    assert any(f["properties"].get("_ours") for f in feats)
    f = next(f for f in feats if f["properties"].get("_ours"))
    b = geometry_bbox(f["geometry"])
    inside = next((p for p in _points_in(f["geometry"], b)), None)
    assert inside is not None
    d = g.district_at(inside[1], inside[0])
    assert d is not None and d.get("_ours") is True
    assert g.district_at(42.7, -73.8) is None and g.district_at(41.0, -73.66) in (None, *[x["properties"] for x in feats])
    assert g.feature_at("nope", 41.0, -73.66) is None


def _points_in(geometry, box):
    """A few points inside the polygon, by sampling its box."""
    for i in range(1, 20):
        for j in range(1, 20):
            lon = box[0] + (box[2] - box[0]) * i / 20
            lat = box[1] + (box[3] - box[1]) * j / 20
            if contains(geometry, lat, lon):
                yield (lon, lat)


# ---- HTTP: the catalog, a layer, the bbox tiles, ETag and gzip --------------------------------------------------------


def test_the_catalog_and_a_whole_layer_with_etag_and_gzip():
    g = make()
    for key in BY_KEY:
        g.refresh(key)
    code, body, ctype, _ = g.get("/api/cad/gis", "")
    cat = json.loads(body)
    assert code == 200 and ctype == "application/json" and cat["configured"] and len(cat["layers"]) == len(BY_KEY)
    assert cat["terms"].startswith("https://gis.westchestercountyny.gov/")
    whole = next(k for k, l in BY_KEY.items() if not l.tiled)
    code, body, ctype, h = g.get(f"/api/cad/gis/{whole}", "")
    doc = json.loads(body)
    assert code == 200 and ctype == "application/geo+json" and doc["type"] == "FeatureCollection" and doc["key"] == whole
    assert len(doc["features"]) == g.status[whole]["n"] and h["ETag"].startswith('W/"') and "max-age=300" in h["Cache-Control"]
    assert h["Vary"] == "Accept-Encoding" and "Content-Encoding" not in h
    code, body304, _, h2 = g.get(f"/api/cad/gis/{whole}", "", {"If-None-Match": h["ETag"]})
    assert code == 304 and body304 == b"" and h2["ETag"] == h["ETag"]
    code, gz, _, h3 = g.get(f"/api/cad/gis/{whole}", "", {"Accept-Encoding": "gzip, deflate, br"})
    assert code == 200 and h3["Content-Encoding"] == "gzip" and json.loads(gzip.decompress(gz)) == doc and len(gz) < len(body)
    # a new fetch: a new ETag
    g.refresh(whole)
    assert g.get(f"/api/cad/gis/{whole}", "")[3]["ETag"] != h["ETag"]
    assert g.get(f"/api/cad/gis/{whole}", "", {"If-None-Match": h["ETag"]})[0] == 200
    assert g.get("/api/cad/gis/nope", "")[0] == 404 and g.get("/api/cad/gis/../x", "")[0] == 404
    assert g.handles("/api/cad/gis") and g.handles("/api/cad/gis/parcels") and not g.handles("/api/cad/gisx")


def test_a_tiled_layer_is_served_by_viewport_only():
    g = make(layers=[BY_KEY["parcels"]])
    assert g.refresh("parcels")
    code, body, _, _ = g.get("/api/cad/gis/parcels", "")
    assert code == 400 and b"bbox" in body
    assert g.get("/api/cad/gis/parcels", "bbox=-74,40,-73,42")[0] == 400                # the whole county: no
    assert g.get("/api/cad/gis/parcels", "bbox=nope")[0] == 400
    feats = g.data["parcels"]
    b = geometry_bbox(feats[0]["geometry"])
    box = f"{b[0] - 0.0005},{b[1] - 0.0005},{b[2] + 0.0005},{b[3] + 0.0005}"
    code, body, ctype, h = g.get("/api/cad/gis/parcels", f"bbox={box}")
    doc = json.loads(body)
    assert code == 200 and ctype == "application/geo+json" and 1 <= len(doc["features"]) <= len(feats)
    assert any(f["properties"] == feats[0]["properties"] for f in doc["features"]) and doc["bbox"] == [float(x) for x in box.split(",")]
    assert "max-age=60" in h["Cache-Control"] and h["ETag"].startswith('W/"parcels-')
    assert g.get("/api/cad/gis/parcels", f"bbox={box}", {"If-None-Match": h["ETag"]})[0] == 304
    assert json.loads(g.get("/api/cad/gis/parcels", "bbox=-73.9,41.09,-73.85,41.1")[1])["features"] == []   # nothing there
    other = g.get("/api/cad/gis/parcels", "bbox=-73.9,41.09,-73.85,41.1")[3]["ETag"]
    assert other != h["ETag"]


# ---- through the hub: Cloudflare Access, a viewer -------------------------------------------------------------------


class FakeCounty:
    """The county's REST service on this machine: the fixtures' answers for the starter layers."""

    def __init__(self):
        self.requests: list[str] = []
        fake = self
        f = FakeFetch()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                code, headers, body = arcgis_answer("https://x" + self.path, f.fixtures)
                self.send_response(code)
                self.send_header("Content-Type", headers.get("content-type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def wait_for(cond, timeout=10.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def local_layers(port: int) -> tuple:
    """The starter layers pointed at the fake county (the same paths, this machine's host)."""
    out = []
    for l in DEFAULT_LAYERS:
        path = urlsplit(l.url).path
        out.append(Layer(**{**l.__dict__, "url": f"http://127.0.0.1:{port}{path}"}))
    return tuple(out)


def test_the_hub_serves_the_layers_to_a_signed_in_viewer_and_the_browser_never_sees_the_county(tmp_path, keys, jwks, monkeypatch):  # noqa: F811
    county = FakeCounty()
    monkeypatch.setattr(gis_mod, "URL_RE", re.compile(r"https?://[^/\s?#]+/\S*/(FeatureServer|MapServer)/\d+"))
    h = HubRun(hub_cfg(tmp_path, cloud_access_team=TEAM, cloud_access_aud=AUD, gis_layers=local_layers(county.port)))
    try:
        h.server.access.certs_url = jwks.url
        h.server.access.log = lambda m: None
        post(h.app.cad, "users", action="add", name="Corey", email="corey@example.org", role="admin")
        post(h.app.cad, "users", action="add", name="Val", email="val@example.org", role="viewer")
        assert wait_for(lambda: all(h.app.gis.status[k]["at"] for k in BY_KEY))
        assert req(h.url + "/api/cad/gis")[0] == 401                                      # Access: a person only
        val = {"Cf-Access-Jwt-Assertion": jwt(keys[0], email="val@example.org")}            # a viewer may look
        code, cat = req(h.url + "/api/cad/gis", headers=val)
        assert code == 200 and cat["configured"] and len(cat["layers"]) == len(BY_KEY)
        assert "127.0.0.1" not in json.dumps(cat) and "westchestergov" not in json.dumps(cat)     # the service URLs stay on the hub
        whole = next(l["key"] for l in cat["layers"] if not l["tiled"])
        code, doc = req(h.url + f"/api/cad/gis/{whole}", headers=val)
        assert code == 200 and len(doc["features"]) == h.app.gis.status[whole]["n"]
        code, tile = req(h.url + "/api/cad/gis/parcels?bbox=-73.9,40.9,-73.4,41.3", headers=val)
        assert code == 400                                                                 # the county: bigger than a viewport
        b = geometry_bbox(h.app.gis.data["parcels"][0]["geometry"])
        code, tile = req(h.url + f"/api/cad/gis/parcels?bbox={b[0] - 0.001},{b[1] - 0.001},{b[2] + 0.001},{b[3] + 0.001}", headers=val)
        assert code == 200 and tile["features"] and "PARCEL_ADDR" in tile["features"][0]["properties"]
        assert (tmp_path / "hub" / "gis" / "parcels.geojson").is_file()
        assert h.app.gis.district_at(41.0017, -73.6657) is None or isinstance(h.app.gis.district_at(41.0017, -73.6657), dict)
    finally:
        h.close()
        county.close()


def test_a_hub_with_gis_off_still_answers(tmp_path):
    h = HubRun(hub_cfg(tmp_path, gis_enabled=False))
    try:
        doc = h.get("/api/cad/gis")
        assert doc["configured"] is False and doc["layers"] == []
        assert h.app.gis._thread is None
    finally:
        h.close()


# ---- config -------------------------------------------------------------------------------------------------------


def load_cfg(tmp_path, text: str):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return load(p)


def test_gis_config_ships_the_starter_set_and_one_entry_changes_one_thing(tmp_path):
    c = load_cfg(tmp_path, "")
    assert c.gis_enabled and [l.key for l in c.gis_layers] == [l.key for l in DEFAULT_LAYERS]
    c = load_cfg(tmp_path, '[[gis.layers]]\nkey = "parcels"\non = true\nrefresh = "daily"\n')
    p = {l.key: l for l in c.gis_layers}["parcels"]
    assert p.on and p.refresh == "daily" and p.url == BY_KEY["parcels"].url and p.fields == BY_KEY["parcels"].fields
    c = load_cfg(tmp_path, '[[gis.layers]]\nkey = "parcels"\nenabled = false\n'
                           '[[gis.layers]]\nkey = "dams"\nlabel = "Dams"\ngroup = "Hazards"\n'
                           'url = "https://giswww.westchestergov.com/wcgis/rest/services/OpenData/MapServer/40/"\n'
                           'fields = ["NAME", ["OWNER", "Owner"]]\nstyle = { color = "--err", radius = 6 }\nminzoom = 12\n'
                           'refresh = "monthly"\nbbox = [-73.8, 40.9, -73.5, 41.1]\nwhere = "HAZARD = \'HIGH\'"\n')
    keys_ = [l.key for l in c.gis_layers]
    assert "parcels" not in keys_ and keys_[-1] == "dams"
    d = c.gis_layers[-1]
    assert (d.label, d.group, d.url, d.kind, d.minzoom, d.refresh) == (
        "Dams", "Hazards", "https://giswww.westchestergov.com/wcgis/rest/services/OpenData/MapServer/40", "", 12, "monthly")
    assert d.fields == (("NAME", "Name"), ("OWNER", "Owner")) and d.style == {"color": "--err", "radius": 6}
    assert d.bbox == (-73.8, 40.9, -73.5, 41.1) and d.where == "HAZARD = 'HIGH'" and not d.on and not d.tiled
    assert load_cfg(tmp_path, "[gis]\nenabled = false\n").gis_enabled is False
    assert load_cfg(tmp_path, "[gis]\nbbox = [-73.9, 40.9, -73.5, 41.2]\n").gis_bbox == (-73.9, 40.9, -73.5, 41.2)
    for bad, why in (("[gis]\nnope = 1\n", "unknown key"),
                     ("[gis]\nbbox = [-73, 41, -74, 42]\n", "west < east"),
                     ('[[gis.layers]]\nkey = "Bad Key"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\n', "short name"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "http://x.example/a/MapServer/1"\n', "https://"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/"\n', "MapServer"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = ""\nurl = "https://x.example/a/MapServer/1"\n', "label"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nrefresh = "never"\n', "hourly, daily"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nkind = "blob"\n', "point, line or polygon"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nfields = [1]\n', "field names"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nminzoom = 30\n', "0 to 22"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\non = "yes"\n', "true or false"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nstyle = { color = true }\n', "style"),
                     ('[[gis.layers]]\nkey = "x"\nlabel = "x"\nurl = "https://x.example/a/MapServer/1"\nnope = 1\n', "unknown key")):
        with pytest.raises(ConfigError, match=why):
            load_cfg(tmp_path, bad)


# ---- the page (static: tests/test_design.py its classes; tools/ui_consistency.py the render) --------------------------


def test_the_county_gis_group_is_in_every_layers_box_and_only_ever_asks_the_hub():
    html = (STATIC / "cad.html").read_text()
    order = [html.index(f'src="/static/{n}"') for n in ("maplayers.js", "gislayers.js", "cad.js", "cadmap.js")]
    assert order == sorted(order)                                     # registered before the maps are made
    ml = (STATIC / "maplayers.js").read_text()
    assert "function addGroup(g)" in ml and "g.attach(this)" in ml and "g.panel(folds)" in ml and "addGroup," in ml
    js = (STATIC / "gislayers.js").read_text()
    assert "MapLayers.addGroup({ attach: (control) => new GisMap(control) })" in js
    assert '"/api/cad/gis"' in js and "/api/cad/gis/${encodeURIComponent(l.key)}" in js
    assert not re.search(r"westchestergov|westchestercountyny\.gov/(arcgis|wcgis)|/rest/services|arcgis\.com", js)   # never the county
    assert "Westchester County GIS" in js and 'h("b", "", "County GIS")' in js
    # each viewer's choice through the box's control (its remembered sets, an event's own; #111): nothing kept here
    assert "localStorage" not in js and "this.control.on(gisKey(l)" in js and "this.control.setLayer(gisKey(l), cb.checked)" in js
    assert "?bbox=" in js and "getBounds()" in js and "l.tiled" in js        # tiled layers by viewport
    assert "zoom in" in js and "control.showInfo(" in js and "fill-extrusion" in js
    for cls in ("ml-group", "ml-ghead", "ml-sub", "ml-note"):
        assert re.search(rf"\.{cls}\b", (STATIC / "cadmap.css").read_text()), cls
