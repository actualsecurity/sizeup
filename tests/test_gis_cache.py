"""The county GIS layers downloaded only when they changed, and the map caches kept across hub restarts (GitHub #229):
sizeup/gis.py's unchanged / incremental / full looks against a fake ArcGIS layer, sizeup/cachesync.py against a fake
S3 (never the county, never AWS: conftest.py refuses both), the "loading" state and Settings > Data sources' rows."""

import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from sizeup import aws
from sizeup import cachesync as cs
from sizeup import datasources as ds_mod
from sizeup.cachesync import CacheMirror
from sizeup.gis import EDIT_SLACK_MS, FULL_EVERY_S, Gis, Layer
from tests.test_gis import FakeFetch

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
URL = "https://gis.example.org/arcgis/rest/services/Test/FeatureServer/0"
DAY = 86400.0
T0 = 1_791_915_000.0                 # the clock's start (Nov 2026)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def pt(i, lon, lat, edit, **props):
    return {"type": "Feature", "id": i, "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"OBJECTID": i, "NAME": f"Hydrant {i}", "EditDate": edit, **props}}


class FakeArcGIS:
    """One ArcGIS feature layer: its description (?f=json), its features as GeoJSON (where: 1=1, or our incremental
    "... AND EditDate >= TIMESTAMP '...'"), returnIdsOnly; every URL recorded, a failure on demand."""

    def __init__(self, features, edit=1_700_000_000_000, tracking=True, max_records=2):
        self.features, self.edit, self.tracking, self.max = list(features), edit, tracking, max_records
        self.urls: list[str] = []
        self.fail_query: Exception | None = None
        self.refuse_incremental = False

    def info(self):
        fields = [{"name": "OBJECTID", "type": "esriFieldTypeOID"}, {"name": "NAME", "type": "esriFieldTypeString"},
                  {"name": "DEPT", "type": "esriFieldTypeString"},
                  {"name": "EditDate", "type": "esriFieldTypeDate" if self.tracking else "esriFieldTypeString"}]
        d = {"name": "Test", "geometryType": "esriGeometryPoint", "capabilities": "Map,Query,Data", "maxRecordCount": self.max,
             "objectIdField": "OBJECTID", "fields": fields}
        if self.edit is not None:
            d["editingInfo"] = {"lastEditDate": self.edit, "dataLastEditDate": self.edit}
        if self.tracking is True:                           # "name": no editFieldsInfo, a date field named like an edit date
            d["editFieldsInfo"] = {"editDateField": "EditDate", "editorField": "Editor"}
        return d

    def queries(self, kind=None):
        out = [u for u in self.urls if "/query?" in u]
        if kind == "ids":
            return [u for u in out if "returnIdsOnly=true" in u]
        if kind == "features":
            return [u for u in out if "returnIdsOnly" not in u]
        return out

    def __call__(self, url, timeout, limit):
        self.urls.append(url)
        u = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not u.path.endswith("/query"):
            return 200, {}, json.dumps(self.info()).encode()
        if self.fail_query is not None:
            raise self.fail_query
        feats = list(self.features)
        m = re.search(r"EditDate >= TIMESTAMP '([^']+)'", q.get("where", ""))
        if m:
            if self.refuse_incremental:
                return 200, {}, b'{"error":{"code":400,"message":"Unable to complete operation.","details":[]}}'
            import calendar
            import time
            since = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) * 1000
            feats = [f for f in feats if f["properties"]["EditDate"] >= since]
        if q.get("returnIdsOnly") == "true":
            return 200, {}, json.dumps({"objectIdFieldName": "OBJECTID", "objectIds": [f["id"] for f in self.features]}).encode()
        off, n = int(q.get("resultOffset", 0)), min(int(q.get("resultRecordCount", 1000)), self.max)
        want = q.get("outFields", "*")
        page = feats[off:off + n]
        if want != "*":
            page = [{**f, "properties": {k: v for k, v in f["properties"].items() if k in want.split(",")}} for f in page]
        doc = {"type": "FeatureCollection", "features": page}
        if off + n < len(feats):
            doc["exceededTransferLimit"] = True
        return 200, {}, json.dumps(doc).encode()


LAYER = Layer("hydrants", "Hydrants", URL, kind="point", refresh="monthly", fields=(("NAME", "Hydrant"), ("DEPT", "Dept")),
              ours_field="DEPT", bbox=(-74.0, 40.0, -73.0, 41.0))


def make(tmp_path, arc, clock=None, layer=LAYER, ours=("Port Chester FD",)):
    logs: list[str] = []
    g = Gis((layer,), cache_dir=tmp_path / "gis", fetch=arc, log=logs.append, clock=clock or Clock(), ours=ours)
    g.logs = logs
    return g


def names(g, key="hydrants"):
    return {f["id"]: f["properties"].get("NAME") for f in g.data[key]}


# ---- unchanged: nothing downloaded --------------------------------------------------------------------------------


def test_an_unchanged_layer_is_looked_at_but_not_downloaded(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000), pt(2, -73.6, 40.6, 1_600_000_000_000),
                      pt(3, -73.7, 40.7, 1_650_000_000_000)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    assert g.refresh("hydrants") and g.status["hydrants"]["how"] == "full"
    assert len(arc.queries("features")) == 2                  # two pages (maxRecordCount 2)
    meta = json.loads((tmp_path / "gis" / "hydrants.geojson").read_text())["sizeup"]
    assert meta["edit"] == arc.edit and meta["edit_field"] == "EditDate" and meta["edit_max"] == 1_650_000_000_000
    at = g.status["hydrants"]["at"]

    clock.t += 30 * DAY
    arc.urls.clear()
    assert g.refresh("hydrants")
    assert arc.queries() == [] and len(arc.urls) == 1          # only the layer's description
    st = g.status["hydrants"]
    assert st["how"] == "unchanged" and st["at"] == at and st["checked"] == clock.t and st["n"] == 3
    assert st["next"] == clock.t + LAYER.every_s
    assert any("unchanged" in m for m in g.logs)
    checks = json.loads((tmp_path / "gis" / "checks.json").read_text())["layers"]["hydrants"]
    assert checks == {"checked": clock.t, "how": "unchanged"}
    # a restart keeps the look (checks.json) and the data's own time
    g2 = make(tmp_path, arc, clock)
    assert g2.status["hydrants"]["at"] == at and g2.status["hydrants"]["checked"] == clock.t
    assert g2.status["hydrants"]["how"] == "unchanged" and len(g2.data["hydrants"]) == 3


def test_a_config_change_or_refresh_now_downloads_whole_even_when_unchanged(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    g = make(tmp_path, arc)
    g.refresh("hydrants")
    arc.urls.clear()
    assert g.refresh("hydrants", full=True) and arc.queries("features") and g.status["hydrants"]["how"] == "full"
    # another bbox: the copy isn't this layer's any more (its signature), so no "unchanged"
    other = Layer(**{**LAYER.__dict__, "bbox": (-74.0, 40.0, -73.1, 41.0)})
    g2 = make(tmp_path, arc, layer=other)
    assert g2.status["hydrants"]["at"] is None
    arc.urls.clear()
    assert g2.refresh("hydrants") and arc.queries("features") and g2.status["hydrants"]["how"] == "full"


def test_a_layer_without_a_last_edit_date_is_downloaded_whole_on_its_schedule(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)], edit=None, tracking=False)
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    clock.t += 30 * DAY
    arc.urls.clear()
    assert g.refresh("hydrants") and arc.queries("features") and not arc.queries("ids")
    assert g.status["hydrants"]["how"] == "full" and g.status["hydrants"]["at"] == clock.t
    # the registry runs it from its last download, on its interval (never sooner)
    reg = ds_mod.DataSources(log=lambda m: None, clock=clock, rng=lambda: 0.0)
    for src in ds_mod.gis_sources(g):
        reg.register(src)
    assert reg.next_due("gis:hydrants") >= clock.t + 30 * DAY


# ---- incremental: only the edits, the deletions found by id -------------------------------------------------------


def test_a_date_field_named_like_an_edit_date_is_read_incrementally_without_a_last_edit_date(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)], edit=None, tracking="name")
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    arc.features.append(pt(2, -73.6, 40.6, 1_700_000_000_000))
    clock.t += 30 * DAY
    assert g.refresh("hydrants") and g.status["hydrants"]["how"] == "incremental" and set(names(g)) == {1, 2}


def test_edits_additions_and_deletions_are_merged_from_an_incremental_read(tmp_path):
    old = 1_600_000_000_000
    arc = FakeArcGIS([pt(1, -73.5, 40.5, old), pt(2, -73.6, 40.6, old), pt(3, -73.7, 40.7, old),
                      pt(5, -73.75, 40.75, old, DEPT="Port Chester FD")])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    assert set(names(g)) == {1, 2, 3, 5} and g.data["hydrants"][3]["properties"]["_ours"] is True
    new = old + 10 * 86_400_000
    arc.features = [pt(1, -73.5, 40.5, new, NAME="Hydrant 1 (moved)"), pt(3, -73.7, 40.7, old),
                    pt(4, -73.8, 40.8, new), pt(5, -73.75, 40.75, new, DEPT="Port Chester FD", NAME="Ours, edited")]
    arc.edit = new
    clock.t += 30 * DAY
    arc.urls.clear()
    assert g.refresh("hydrants")
    st = g.status["hydrants"]
    assert st["how"] == "incremental" and st["at"] == clock.t and st["n"] == 4
    assert names(g) == {1: "Hydrant 1 (moved)", 3: "Hydrant 3", 4: "Hydrant 4", 5: "Ours, edited"}
    assert [f["id"] for f in g.data["hydrants"]] == [1, 3, 5, 4]       # an edit stays where it was; new ones last
    assert g.data["hydrants"][2]["properties"]["_ours"] is True and "_ours" not in g.data["hydrants"][0]["properties"]
    assert "EditDate" not in g.data["hydrants"][0]["properties"]        # asked for, never kept
    feats = arc.queries("features")
    assert feats and all("EditDate+%3E%3D+TIMESTAMP" in u for u in feats)
    since = parse_qs(urlsplit(feats[0]).query)["where"][0]
    assert since.startswith("(1=1) AND EditDate >= TIMESTAMP ")
    assert len(arc.queries("ids")) == 1
    assert any("1 edited" in m and "1 new" in m and "1 deleted" in m for m in g.logs) or any("incremental" in m for m in g.logs)
    meta = json.loads((tmp_path / "gis" / "hydrants.geojson").read_text())["sizeup"]
    assert meta["edit_max"] == new and meta["how"] == "incremental" and meta["edit"] == new and meta["full_at"] == T0
    # the window asked for: our newest edit less the slack (time zones), never more
    import calendar
    import time
    lit = re.search(r"TIMESTAMP '([^']+)'", since).group(1)
    assert calendar.timegm(time.strptime(lit, "%Y-%m-%d %H:%M:%S")) * 1000 == old - EDIT_SLACK_MS


def test_drop_ours_and_unusable_features_are_remembered_so_an_incremental_read_isnt_drift(tmp_path):
    old = 1_600_000_000_000
    layer = Layer(**{**LAYER.__dict__, "drop_ours": True})
    nogeom = {"type": "Feature", "id": 9, "geometry": None, "properties": {"OBJECTID": 9, "NAME": "x", "EditDate": old}}
    arc = FakeArcGIS([pt(1, -73.5, 40.5, old), pt(5, -73.75, 40.75, old, DEPT="Port Chester FD"), nogeom])
    clock = Clock()
    g = make(tmp_path, arc, clock, layer=layer)
    g.refresh("hydrants")
    assert set(names(g)) == {1}
    arc.features.append(pt(6, -73.6, 40.6, old + 86_400_000))
    arc.edit += 1
    clock.t += 30 * DAY
    assert g.refresh("hydrants") and g.status["hydrants"]["how"] == "incremental"
    assert set(names(g)) == {1, 6}


def test_a_feature_the_incremental_read_cant_explain_means_a_full_download(tmp_path):
    old = 1_600_000_000_000
    arc = FakeArcGIS([pt(1, -73.5, 40.5, old), pt(2, -73.6, 40.6, old)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    arc.features.append(pt(7, -73.65, 40.65, old - 400 * 86_400_000))   # new to us, but with an old edit date (a reload)
    arc.edit += 1
    clock.t += 30 * DAY
    assert g.refresh("hydrants")
    assert g.status["hydrants"]["how"] == "full" and set(names(g)) == {1, 2, 7}
    assert any("downloading it whole" in m for m in g.logs)


def test_the_service_refusing_the_incremental_query_falls_back_to_full(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    arc.refuse_incremental = True
    arc.features.append(pt(2, -73.6, 40.6, 1_700_000_000_000))
    arc.edit += 1
    clock.t += 30 * DAY
    assert g.refresh("hydrants") and g.status["hydrants"]["how"] == "full" and set(names(g)) == {1, 2}


def test_a_failed_incremental_read_keeps_the_old_copy(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000), pt(2, -73.6, 40.6, 1_600_000_000_000)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    before = json.loads((tmp_path / "gis" / "hydrants.geojson").read_text())
    arc.edit += 1
    arc.fail_query = URLError("timed out")
    clock.t += 30 * DAY
    assert not g.refresh("hydrants")
    st = g.status["hydrants"]
    assert "timed out" in st["error"] and st["at"] == T0 and set(names(g)) == {1, 2}
    assert json.loads((tmp_path / "gis" / "hydrants.geojson").read_text()) == before
    assert not g.public()["layers"][0]["loading"]              # it has data: never "loading"


def test_a_layer_read_incrementally_is_downloaded_whole_every_so_often(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    g.refresh("hydrants")
    clock.t += FULL_EVERY_S - DAY
    arc.edit += 1
    g.refresh("hydrants")
    assert g.status["hydrants"]["how"] == "incremental"
    clock.t += 2 * DAY
    g.refresh("hydrants")                                     # unchanged, but 90 days since the last whole one
    assert g.status["hydrants"]["how"] == "full" and g.status["hydrants"]["full_at"] == clock.t


# ---- Settings > Data sources ----------------------------------------------------------------------------------------


def test_data_sources_show_checked_and_downloaded_and_schedule_from_the_look(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    clock = Clock()
    g = make(tmp_path, arc, clock)
    reg = ds_mod.DataSources(log=lambda m: None, clock=clock, rng=lambda: 0.0, spawn=lambda fn, name: fn())
    for src in ds_mod.gis_sources(g):
        reg.register(src)
    reg.tick()
    clock.t += 31 * DAY
    clock.t = reg.next_due("gis:hydrants")                   # (in the quiet hours)
    reg.tick()                                               # due: the look finds it unchanged
    row = reg.row("gis:hydrants")
    assert row["how"] == "unchanged" and row["checked"] == clock.t and row["last_fetched"] == T0
    assert reg.next_due("gis:hydrants") >= clock.t + 30 * DAY     # from the look, not the download
    arc.urls.clear()
    reg.refresh("gis:hydrants", by="admin@example.org")      # Refresh now: whole
    assert arc.queries("features") and reg.row("gis:hydrants")["how"] == "full"
    js = (STATIC / "datasources.js").read_text()
    assert "s.checked" in js and "howLine" in js and "changes only" in js


# ---- "loading" ----------------------------------------------------------------------------------------------------------


def test_a_layer_with_no_data_yet_is_loading_and_the_layers_box_says_so(tmp_path):
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    g = make(tmp_path, arc)
    assert g.public()["layers"][0]["loading"] is True
    arc.fail_query = URLError("down")
    g.refresh("hydrants")
    assert g.public()["layers"][0]["loading"] is False and g.public()["layers"][0]["error"]   # "not loaded", not loading
    arc.fail_query = None
    g.refresh("hydrants")
    lay = g.public()["layers"][0]
    assert lay["loading"] is False and lay["checked"] and lay["how"] == "full"
    js = (STATIC / "gislayers.js").read_text()
    assert re.search(r'else if \(!l\.at\) lab\.append\(h\("span", "dim small", "loading"\)\)', js)
    assert "LOADING_MS" in js and "l.loading" in js


# ---- the S3 copies (sizeup/cachesync.py) --------------------------------------------------------------------------------


class FakeS3:
    """A bucket: ListObjectsV2, GetObject, PutObject; each request's signature headers checked; failures on demand."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_put: set[str] = set()                     # PUTs of keys ending so fail (500)

    def __call__(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        self.calls.append((method, u.path))
        h = {k.lower(): v for k, v in headers.items()}
        assert h["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "/s3/aws4_request" in h["authorization"]
        assert "x-amz-date" in h and "x-amz-content-sha256" in h and "SECRET" not in json.dumps(headers)
        key = u.path.lstrip("/")
        if method == "PUT":
            data = body.read() if hasattr(body, "read") else body
            assert hashlib.sha256(data).hexdigest() == h["x-amz-content-sha256"] and int(h["content-length"]) == len(data)
            if any(key.endswith(x) for x in self.fail_put):
                return 500, b"<Error><Code>InternalError</Code></Error>"
            self.objects[key] = data
            return 200, b""
        if method == "GET" and not key:
            prefix = parse_qs(u.query)["prefix"][0]
            items = "".join(f"<Contents><Key>{k}</Key><Size>{len(v)}</Size>"
                            f"<ETag>&quot;{hashlib.md5(v).hexdigest()}&quot;</ETag></Contents>"   # (S3's: the MD5, quoted)
                            for k, v in sorted(self.objects.items()) if k.startswith(prefix))
            return 200, (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                         f"{items}<IsTruncated>false</IsTruncated></ListBucketResult>").encode()
        if method == "GET":
            return (200, self.objects[key]) if key in self.objects else (404, b"<Error><Code>NoSuchKey</Code></Error>")
        raise AssertionError(method)


def creds():
    return aws.Credentials("AKIDTEST", "SECRETSECRET", "TOKEN")


def mirror(root, s3, clock=None, logs=None):
    return CacheMirror(root, "test-bucket", "sizeup-hub", "us-east-1", send=s3, creds=creds,
                       log=(logs.append if logs is not None else (lambda m: None)), clock=clock or Clock())


def test_changed_caches_are_uploaded_gzipped_once_and_only_ours(tmp_path):
    s3, clock = FakeS3(), Clock()
    root = tmp_path / "state"
    (root / "gis").mkdir(parents=True)
    (root / "gis" / "hydrants.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    (root / "roads511.json").write_text("{}")
    (root / "cad.sqlite").write_bytes(b"litestream's")              # not ours: Litestream keeps it
    (root / "gis" / "hydrants.tmp").write_text("half")               # a provider's temp file
    m = mirror(root, s3, clock)
    assert m.scan() == 2
    assert sorted(s3.objects) == ["sizeup-hub/cache/gis/hydrants.geojson.gz", "sizeup-hub/cache/roads511.json.gz"]
    key = next(k for k in s3.objects if k.endswith("hydrants.geojson.gz"))
    assert gzip.decompress(s3.objects[key]) == (root / "gis" / "hydrants.geojson").read_bytes()
    clock.t += 120
    assert m.scan() == 0                                            # nothing changed
    (root / "gis" / "hydrants.geojson").write_text('{"type":"FeatureCollection","features":[1]}')
    (root / "roads511.json").write_text('{"a":1}')
    assert m.scan() == 1                                            # the GIS file at once; roads511.json once an hour
    clock.t += 3600
    assert m.scan() == 1


def test_an_upload_failure_is_logged_retried_later_and_never_fatal(tmp_path):
    s3, clock, logs = FakeS3(), Clock(), []
    s3.fail_put = {"hydrants.geojson.gz"}
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000)])
    g = Gis((LAYER,), cache_dir=tmp_path / "state" / "gis", fetch=arc, log=lambda m: None, clock=clock)
    m = mirror(tmp_path / "state", s3, clock, logs)
    assert g.refresh("hydrants")
    assert m.scan() == 1                                            # checks.json went; the layer's copy failed
    assert any("hydrants.geojson not uploaded" in x and "InternalError" in x for x in logs)
    assert "hydrants.geojson" in m.status["error"] and g.status["hydrants"]["error"] == ""
    s3.fail_put = set()
    assert m.scan() == 0                                            # backing off
    clock.t += cs.BACKOFF_S[0]
    assert m.scan() == 1 and any(k.endswith("hydrants.geojson.gz") for k in s3.objects)
    # no credentials at all: still nothing raised
    m2 = CacheMirror(tmp_path / "state", "b", send=s3, creds=lambda: (_ for _ in ()).throw(aws.AwsError("none", "NoCredentials")),
                     log=logs.append, clock=clock)
    (tmp_path / "state" / "gis" / "hydrants.geojson").write_text("{}")
    assert m2.scan() == 0 and m2.restore() == 0


def test_a_new_task_restores_the_layers_before_the_first_refresh(tmp_path):
    s3, clock = FakeS3(), Clock()
    arc = FakeArcGIS([pt(1, -73.5, 40.5, 1_600_000_000_000), pt(2, -73.6, 40.6, 1_600_000_000_000)])
    old = tmp_path / "old"
    g = Gis((LAYER,), cache_dir=old / "gis", fetch=arc, log=lambda m: None, clock=clock)
    g.refresh("hydrants")
    clock.t += 31 * DAY
    g.refresh("hydrants")                                           # unchanged: checks.json says so
    mirror(old, s3, clock).scan()
    # the new task: an empty disk, the copies put back before Gis reads its cache
    new = tmp_path / "new"
    logs: list[str] = []
    assert mirror(new, s3, clock, logs).restore() == 2
    arc.urls.clear()
    g2 = Gis((LAYER,), cache_dir=new / "gis", fetch=arc, log=lambda m: None, clock=clock)
    assert arc.urls == []                                           # served from the copy, nothing asked of the county
    st = g2.status["hydrants"]
    assert st["at"] == T0 and st["checked"] == clock.t and st["how"] == "unchanged" and len(g2.data["hydrants"]) == 2
    assert not g2.public()["layers"][0]["loading"]
    assert st["next"] == clock.t + LAYER.every_s
    assert g2.refresh("hydrants") and arc.queries() == []           # its next look: unchanged, nothing downloaded
    # a file already here is never overwritten; a restored one isn't sent straight back up
    puts = sum(1 for c in s3.calls if c[0] == "PUT")
    m3 = mirror(new, s3, clock)
    assert m3.restore() == 0
    m4 = mirror(tmp_path / "newer", s3, clock)
    assert m4.restore() == 2 and m4.scan() == 0 and sum(1 for c in s3.calls if c[0] == "PUT") == puts


def test_the_hub_restores_the_copies_before_its_providers_read_their_caches(tmp_path, monkeypatch):
    from sizeup.app import make_app
    from tests.test_cloud import hub_cfg
    from tests.test_gis import BY_KEY
    s3 = FakeS3()
    layers = (BY_KEY["hydrants"], BY_KEY["parcels"])
    old_cfg = hub_cfg(tmp_path / "old", gis_layers=layers)
    old = Gis.from_config(old_cfg, log=lambda m: None)
    old.fetch = FakeFetch(["hydrants"])
    assert old.refresh("hydrants")
    mirror(Path(old_cfg.cad_db).parent, s3).scan()
    monkeypatch.setenv("LITESTREAM_BUCKET", "test-bucket")
    monkeypatch.setenv("LITESTREAM_PATH", "sizeup-hub")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRETSECRET")
    monkeypatch.setattr(cs, "_send", s3)
    app = make_app(hub_cfg(tmp_path / "new", gis_layers=layers))
    try:
        assert app.cache_mirror is not None
        # #251: nothing restored or read while the app is made (the CAD serves first): every layer "loading"
        assert all(l["loading"] for l in app.gis.public()["layers"]) and not app.layers_ready.is_set()
        app._load_layers()                                          # start_layers()'s background loader, here
        assert app.layers_ready.is_set()
        lay = {l["key"]: l for l in app.gis.public()["layers"]}
        assert lay["hydrants"]["at"] and lay["hydrants"]["n"] == old.status["hydrants"]["n"] and not lay["hydrants"]["loading"]
        assert lay["parcels"]["loading"]                            # never fetched: loading until the hub has it
    finally:
        app.stop()


def test_the_mirror_is_off_without_a_bucket_and_signs_s3_with_the_given_payload_hash(tmp_path, monkeypatch):
    assert CacheMirror.from_env(tmp_path) is None
    monkeypatch.setenv("LITESTREAM_BUCKET", "b")
    monkeypatch.setenv("SIZEUP_CACHE_S3", "off")
    assert CacheMirror.from_env(tmp_path) is None
    monkeypatch.delenv("SIZEUP_CACHE_S3")
    m = CacheMirror.from_env(tmp_path)
    assert m is not None and m.prefix == "sizeup-hub/cache" and m.endpoint == "https://b.s3.us-east-1.amazonaws.com"
    from datetime import datetime, timezone
    c, body, at = creds(), b"hello", datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    sha = hashlib.sha256(body).hexdigest()
    a = aws.sign("PUT", "https://b.s3.us-east-1.amazonaws.com/k", {"x-amz-content-sha256": sha}, body, c, "us-east-1", "s3", at=at)
    b = aws.sign("PUT", "https://b.s3.us-east-1.amazonaws.com/k", {"x-amz-content-sha256": sha}, b"", c, "us-east-1", "s3", at=at,
                 payload_sha=sha)
    assert a["Authorization"] == b["Authorization"]             # the hash given signs as the body's own would
    assert cs.gap_for("gis/hydrants.geojson") == 0.0 and cs.gap_for("gis/../cad.sqlite") is None and cs.gap_for("cad.sqlite") is None
