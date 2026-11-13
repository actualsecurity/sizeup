"""Pre-plan site documents (#459, sizeup/cad/preplan_docs.py, sizeup/docfiles.py, sizeup/docstore.py, static/preplan-docs.js):
a building's floor plans, overlaid on the map of every call there, and its other documents, opened in the app.

Physical-security data: everything here is invented. The buildings are tests/test_preplan.py's ("100 Example Ave" on an
invented street grid), the plans are rectangles drawn here, the PDFs are written here byte by byte, and nothing a
document is called, where its plan sits, or what its file was named ever reaches the server log."""
import hashlib
import http.client
import io
import json
import re
import struct
import subprocess
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from sizeup import aws, docfiles, docstore
from sizeup.cad import preplan_docs as ppd
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from tests.test_address_history import fake_geocode
from tests.test_cad import make_cad
from tests.test_preplan import LON, NODE, STATIC, at, fake_gis, lat_of
from tests.test_roles import COREY, DEE, JULIAN, VAL, call, people

HAVE_PDFIUM = docfiles.can_render()


# -- invented files ---------------------------------------------------------------------------------------------------------

def png(w=40, h=30, extra=b"", tail=b""):
    """A grey rectangle with a dark frame (a "floor plan"), as an RGB PNG; extra: chunks before IDAT; tail: after IEND."""
    rows = b""
    for y in range(h):
        line = bytes(40 if (y in (0, h - 1) or x in (0, w - 1)) else 220 for x in range(w) for _ in range(3))
        rows += b"\0" + line

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"") + tail)


def png_chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))


def jpeg_with_gps(orientation=6):
    """A photo with EXIF: GPS (an invented point), the camera, the orientation; a comment; data after the end."""
    PIL = pytest.importorskip("PIL.Image")
    from PIL import Image
    im = Image.new("RGB", (64, 48), (200, 200, 200))
    for x in range(10, 54):
        im.putpixel((x, 24), (20, 20, 20))
    exif = Image.Exif()
    exif[0x010F] = "ExampleCam"                            # Make
    exif[0x0112] = orientation
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (41.0, 0.0, 7.5), "W", (73.0, 39.0, 36.0)
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif, comment=b"Example comment")
    assert PIL
    return buf.getvalue() + b"<html><script>alert(1)</script></html>"


def pdf(pages=1, extra=b""):
    """A small PDF written here: each page a framed rectangle (a "floor plan"); extra: more names in its catalog."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R " + extra + b">>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)).encode()
    objs.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>")
    for i in range(pages):
        stream = f"0.2 g 50 50 500 {300 + 50 * i} re S 0.8 g 60 60 480 {280 + 50 * i} re f".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 {400 + 50 * i}] /Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


# -- fixtures ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cad(tmp_path):
    c = make_cad(geocoder=fake_geocode)
    c.gis = fake_gis()
    c.logged = []
    c.log = c.logged.append
    c.preplan_docs.store = docstore.FsDocStore(tmp_path / "docs")
    return c


@pytest.fixture
def api(cad):
    a = CadApi(cad)
    a.ids = people(cad)
    a.iid = at(cad, 100)
    return a


def who(email):
    return Identity(email=email) if email else None


def upload(api, data, email=JULIAN, **meta):
    m = {"kind": "floor_plan", "title": "First floor", "floor": "1", "incident_id": api.iid, **meta}
    code, raw, _ = api.upload("/api/cad/preplan/docs", quote(json.dumps(m)), data, who(email))
    return code, json.loads(raw)


def get(api, path, query="", email=None):
    code, raw, ctype, *h = api.get(path, query, who(email))
    return code, (json.loads(raw) if "json" in ctype else raw), (h[0] if h else {})


def box_corners(lat, lon, dlat=0.00003, dlon=0.00004):
    """TL TR BR BL of a north-up box around the point (the 7 m footprints of tests/test_preplan.py)."""
    return [[lon - dlon, lat + dlat], [lon + dlon, lat + dlat], [lon + dlon, lat - dlat], [lon - dlon, lat - dlat]]


# -- what may be uploaded ------------------------------------------------------------------------------------------------

def test_the_type_is_the_content_never_the_name():
    assert docfiles.sniff(png()) == "image/png"
    assert docfiles.sniff(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert docfiles.sniff(pdf()) == "application/pdf"
    for bad in (SVG, b"<svg/>", b"  <!DOCTYPE html><html>", b"<html><script>", b"GIF89a....", b"PK\x03\x04zip",
                b"RIFF\0\0\0\0WEBPVP8 ", b"\0\0\0\x18ftypheic", b"just text", b""):
        with pytest.raises(docfiles.Refused):
            docfiles.sniff(bad)
    with pytest.raises(docfiles.Refused, match="SVG"):
        docfiles.sniff(SVG)


def test_svg_html_and_bad_magic_bytes_are_refused_whatever_they_are_called(api):
    for data in (SVG, b"<html><body>plan</body></html>", b"GIF89a" + b"\0" * 100, b"\x89PNX....", b"%PDX-1.4"):
        code, out = upload(api, data, title="plan.png")
        assert code == 400, data[:10]
    assert api.cad.store.q("SELECT COUNT(*) AS n FROM preplan_docs")[0]["n"] == 0
    assert not list((Path(api.cad.preplan_docs.store.root)).rglob("*.*"))          # nothing stored
    code, out = upload(api, png()[:-20])                                            # a PNG cut short
    assert code == 400 and "PNG" in out["error"]
    bad_crc = bytearray(png())
    bad_crc[40] ^= 0xFF
    assert upload(api, bytes(bad_crc))[0] == 400


def test_the_size_cap(api, monkeypatch):
    monkeypatch.setattr(ppd, "MAX_BYTES", 1000)
    code, out = upload(api, png(tail=bytes(2000)))
    assert code == 400 and "MB" in out["error"]
    assert upload(api, b"")[0] == 400


def test_exif_and_gps_are_stripped_from_photos_and_the_orientation_kept(api):
    from PIL import Image
    raw = jpeg_with_gps(orientation=6)
    assert b"ExampleCam" in raw and b"Example comment" in raw
    code, out = upload(api, raw, kind="photo", title="Rear", floor="")
    assert code == 200, out
    d = out["doc"]
    stored = (Path(api.cad.preplan_docs.store.root) / "preplan-docs").rglob("original.jpg")
    data = next(stored).read_bytes()
    assert b"ExampleCam" not in data and b"Example comment" not in data and b"<script" not in data
    assert data.endswith(b"\xff\xd9")
    im = Image.open(io.BytesIO(data))
    ex = im.getexif()
    assert dict(ex) == {0x0112: 6}                                    # only the orientation
    assert not ex.get_ifd(0x8825)                                     # no GPS
    assert im.size == (64, 48) and im.getpixel((30, 24))[0] < 80      # the picture itself unchanged
    assert d["bytes"] == len(data) and (d["plan_w"], d["plan_h"]) == (48, 64)   # as it stands (rotated)
    clean, w, h = docfiles.clean_jpeg(jpeg_with_gps(orientation=1))
    assert 0x0112 not in Image.open(io.BytesIO(clean)).getexif() and (w, h) == (64, 48)


def test_png_text_and_exif_chunks_are_dropped():
    text = png_chunk(b"tEXt", b"Comment\0secret") + png_chunk(b"eXIf", b"MM\0*\0\0\0\x08\0\0") + png_chunk(b"iTXt", b"x\0\0\0\0\0y")
    clean, w, h = docfiles.clean_png(png(extra=text, tail=b"<script>alert(1)</script>"))
    assert (w, h) == (40, 30)
    assert b"tEXt" not in clean and b"eXIf" not in clean and b"secret" not in clean and b"<script" not in clean
    assert clean == png()


def test_pdfs_that_can_run_something_are_refused(api):
    for name in (b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>", b"/Names << /EmbeddedFiles 9 0 R >>",
                 b"/OpenAction << /S /Launch /F (x.exe) >>", b"/AcroForm << /XFA 9 0 R >>", b"/OpenAction << /S /J#61vaScript >>"):
        code, out = upload(api, pdf(extra=name), kind="inspection", title="Annual")
        assert code == 400 and "PDF" in out["error"], name
    code, out = upload(api, pdf(), kind="inspection", title="Annual", floor="")
    assert code == 200, out


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdfium2 not installed")
def test_a_pdf_plan_is_rendered_from_the_chosen_page(api):
    code, out = upload(api, pdf(pages=3), page=2, title="Second floor", floor="2")
    assert code == 200, out
    d = out["doc"]
    assert (d["pages"], d["page"], d["plan"]) == (3, 2, True)
    assert max(d["plan_w"], d["plan_h"]) == min(docfiles.MAX_PX, 600 * 4)   # at most MAX_PX, at most 4x (288 dpi)
    assert abs(d["plan_w"] / d["plan_h"] - 600 / 450) < 0.01               # page 2: 600 x 450 pt
    plan = next((Path(api.cad.preplan_docs.store.root)).rglob("plan.png")).read_bytes()
    assert docfiles.png_size(plan) == (d["plan_w"], d["plan_h"])
    code, out = call(api, f"/api/cad/preplan/docs/{d['id']}", {"page": 3}, JULIAN)          # another page
    assert code == 200 and out["doc"]["page"] == 3 and out["changed"] == ["page"]
    assert abs(out["doc"]["plan_w"] / out["doc"]["plan_h"] - 600 / 500) < 0.01
    assert call(api, f"/api/cad/preplan/docs/{d['id']}", {"page": 9}, JULIAN)[0] == 400
    assert upload(api, pdf(pages=1), page=4)[0] == 400


def test_a_pdf_that_pdfium_cant_open_is_refused():
    if not HAVE_PDFIUM:
        pytest.skip("pypdfium2 not installed")
    with pytest.raises(docfiles.Refused):
        docfiles.render(b"%PDF-1.4\ngarbage\n%%EOF\n", 1)


# -- who --------------------------------------------------------------------------------------------------------------------

def test_viewers_look_and_never_write_and_a_deactivated_person_sees_nothing(api):
    code, out = upload(api, png(), email=VAL)
    assert code == 403
    code, out = upload(api, png())
    assert code == 200
    did = out["doc"]["id"]
    assert [d["id"] for d in get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["docs"]] == [did]
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)[0] == 200
    for body in ({"title": "x"}, {"corners": box_corners(lat_of(100), LON)}, {"action": "delete"}):
        assert call(api, f"/api/cad/preplan/docs/{did}", body, VAL)[0] == 403, body
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", DEE)[0] == 403        # deactivated
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", DEE)[0] == 403


def test_unauthenticated_requests_are_refused_by_the_server(api):
    """With Cloudflare Access on (the hub), nobody signed in gets a 401: no list, no file, no upload."""
    from sizeup.web.server import WebServer

    class Access:
        def identify(self, headers):
            e = headers.get("X-Test-Email")
            return Identity(email=e) if e else None
    did = upload(api, png())[1]["doc"]["id"]
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api, access=Access(), role="hub")
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        def req(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read(), r
        meta = quote(json.dumps({"kind": "photo", "title": "x", "incident_id": api.iid}))
        assert req("GET", f"/api/cad/preplan/docs?incident={api.iid}")[0] == 401
        assert req("GET", f"/api/cad/preplan/docs/{did}/file")[0] == 401
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta})[0] == 401
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta, "X-Test-Email": VAL})[0] == 403
        code, body, r = req("GET", f"/api/cad/preplan/docs/{did}/file", headers={"X-Test-Email": VAL})
        assert code == 200 and body.startswith(b"\x89PNG")
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        assert r.getheader("Content-Disposition") == f'inline; filename="site-doc-{did}.png"'
        assert "sandbox" in r.getheader("Content-Security-Policy") and "no-store" in r.getheader("Cache-Control")
        code, body, r = req("GET", f"/api/cad/preplan/docs/{did}/file?dl=1", headers={"X-Test-Email": VAL})
        assert r.getheader("Content-Disposition").startswith("attachment;")
        code, body, r = req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta, "X-Test-Email": JULIAN})
        assert code == 200, body
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Test-Email": JULIAN})[0] == 400        # no details
        big = {"X-Sizeup-Doc": meta, "X-Test-Email": JULIAN, "Content-Length": str(30 * 1024 * 1024)}
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/cad/preplan/docs")
        for k, v in big.items():
            conn.putheader(k, v)
        conn.endheaders()                                                     # never sent: refused on its length
        assert conn.getresponse().status == 413
    finally:
        srv.httpd.shutdown()


def test_admin_only_documents_are_hidden_from_everyone_else(api):
    c = api.cad
    assert upload(api, png(), admin_only=True)[0] == 400                      # a dispatcher can't make one
    code, out = upload(api, png(), email=COREY, admin_only=True, title="Alarm panel layout", kind="preplan")
    assert code == 200 and out["doc"]["admin_only"] is True
    did = out["doc"]["id"]
    upload(api, png(), title="Lobby")
    for email in (VAL, JULIAN):
        docs = get(api, "/api/cad/preplan/docs", f"incident={api.iid}", email)[1]["docs"]
        assert [d["title"] for d in docs] == ["Lobby"], email
        assert get(api, f"/api/cad/preplan/docs/{did}", "", email)[0] == 404
        assert get(api, f"/api/cad/preplan/docs/{did}/file", "", email)[0] == 404
        assert get(api, f"/api/cad/preplan/docs/{did}/plan", "", email)[0] == 404
    for body in ({"title": "x"}, {"action": "delete"}, {"admin_only": False}):
        code, out = call(api, f"/api/cad/preplan/docs/{did}", body, JULIAN)
        assert code == 400 and out["error"] == f"no document {did}", body              # as if there were none
    assert len(get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"]) == 2
    rows = [r for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", JULIAN)[1]["audit"]]
    mine = [r for r in rows if r["key"].split()[0] == str(did)]
    assert mine and not any("Alarm" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    assert json.loads(mine[0]["new"])["admin_only"] is True
    # a document made admin-only later: its earlier rows too
    lobby = next(d for d in get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] if d["title"] == "Lobby")
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, JULIAN)[0] == 400
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, COREY)[0] == 200
    rows = get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", JULIAN)[1]["audit"]
    assert not any("Lobby" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    assert any("Lobby" in (r["new"] or "") for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"])
    assert c.preplan_docs.for_incident(c.store.incident(api.iid), "viewer") == []


# -- storage: presigned links ------------------------------------------------------------------------------------------------

class FakeS3:
    def __init__(self):
        self.objects, self.calls = {}, []

    def __call__(self, method, url, headers, body, timeout):
        key = urlsplit(url).path.lstrip("/")
        self.calls.append((method, key, headers))
        if method == "PUT":
            self.objects[key] = (body, headers.get("Content-Type"))
            return 200, {}, b""
        if method == "GET":
            return (200, {}, self.objects[key][0]) if key in self.objects else (404, {}, b"<Code>NoSuchKey</Code>")
        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""
        return 400, {}, b""


def s3_store():
    fake = FakeS3()
    st = docstore.S3DocStore("example-bucket", "preplan-docs", region="us-east-1", send=fake,
                             creds=lambda: aws.Credentials("AKIDEXAMPLE", "secret", "session-token"))
    return st, fake


def test_s3_links_are_presigned_and_expire_in_two_minutes(api):
    st, fake = s3_store()
    api.cad.preplan_docs.store = st
    code, out = upload(api, png(), email=JULIAN)
    assert code == 200
    did = out["doc"]["id"]
    (key, (body, ctype)), = fake.objects.items()
    assert re.fullmatch(r"preplan-docs/[0-9a-f]{16}/original\.png", key) and ctype == "image/png"
    put = next(h for m, k, h in fake.calls if m == "PUT")
    assert put["x-amz-server-side-encryption"] == "AES256"
    code, raw, h = get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    assert code == 302 and raw == b""
    assert h["Cache-Control"] == "private, no-store" and h["Referrer-Policy"] == "no-referrer"
    u = urlsplit(h["Location"])
    q = parse_qs(u.query)
    assert u.netloc == "example-bucket.s3.us-east-1.amazonaws.com" and u.path == f"/{key}"
    assert q["X-Amz-Expires"] == ["120"] and q["X-Amz-SignedHeaders"] == ["host"] and q["X-Amz-Signature"]
    assert q["response-content-type"] == ["image/png"]
    assert q["response-content-disposition"] == [f'inline; filename="site-doc-{did}.png"']
    assert q["response-cache-control"] == ["private, no-store"]
    assert "First floor" not in h["Location"]                             # never the title
    assert int(q["X-Amz-Expires"][0]) <= 300
    assert st.link(key, "image/png", "inline", expires=3600).count("X-Amz-Expires=120") == 1       # never longer
    # the signature is S3's query authentication: UNSIGNED-PAYLOAD
    at_ = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    a = aws.presign("https://example-bucket.s3.us-east-1.amazonaws.com/k", aws.Credentials("A", "S"), "us-east-1", "s3",
                    120, at=at_, payload_hash="UNSIGNED-PAYLOAD")
    b = aws.presign("https://example-bucket.s3.us-east-1.amazonaws.com/k", aws.Credentials("A", "S"), "us-east-1", "s3",
                    120, at=at_)
    assert a != b
    # a delete removes the bytes
    assert call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, JULIAN)[0] == 200
    assert fake.objects == {}
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)[0] == 404


def test_without_storage_uploads_say_so(api):
    api.cad.preplan_docs.store = None
    code, out = upload(api, png())
    assert code == 400 and "not configured" in out["error"]
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["configured"] is False


def test_from_env(tmp_path, monkeypatch):
    for k in ("SIZEUP_PREPLAN_DOCS_BUCKET", "SIZEUP_PREPLAN_DOCS_DIR", "SIZEUP_PREPLAN_DOCS_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    assert docstore.from_env(log=lambda m: None) is None                  # a hub without the bucket
    assert isinstance(docstore.from_env(log=lambda m: None, default_dir=tmp_path), docstore.FsDocStore)
    monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_BUCKET", "example-bucket")
    st = docstore.from_env(log=lambda m: None)
    assert isinstance(st, docstore.S3DocStore) and st.prefix == "preplan-docs"
    monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_PREFIX", "../x")
    assert docstore.from_env(log=lambda m: None) is None
    with pytest.raises(ValueError):
        docstore.FsDocStore(tmp_path).key("../../etc", "original.png")


# -- the building, the call, the overlay -------------------------------------------------------------------------------------

def test_a_call_lists_its_buildings_documents_in_order_and_never_a_neighbours(api):
    c = api.cad
    upload(api, png(), title="Second floor", floor="2")
    upload(api, png(), title="Basement", floor="B1")
    upload(api, pdf(), kind="hazmat", title="Tier II", floor="")
    upload(api, png(), title="Ground", floor="1")
    other = at(c, 102)
    upload(api, png(), title="Next door", incident_id=other)
    docs = get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["docs"]
    assert [d["title"] for d in docs] == ["Basement", "Ground", "Second floor", "Tier II"]
    d = docs[0]
    assert (d["bldg_key"], d["building_ref"], d["by"], d["plan"], d["corners"]) == (
        "100|example ave|port chester", "buildings/7100", "Julian", True, None)
    assert [d["title"] for d in get(api, "/api/cad/preplan/docs", "where=100+Example+Ave", VAL)[1]["docs"]][:1] == ["Basement"]
    assert [d["title"] for d in get(api, "/api/cad/preplan/docs", f"incident={other}", VAL)[1]["docs"]] == ["Next door"]
    # a call with no address at all, its pin on the building: the footprint's documents, "nearby"
    pin = c.store.create_incident(type="STRU", lat=lat_of(100) + 0.00001, lon=LON)
    near = get(api, "/api/cad/preplan/docs", f"incident={pin}", VAL)[1]["docs"]
    assert len(near) == 4 and all(x["nearby"] for x in near)


def test_align_saves_four_corners_audited_and_validated(api):
    c = api.cad
    did = upload(api, png())[1]["doc"]["id"]
    out = get(api, f"/api/cad/preplan/docs/{did}", "", VAL)[1]
    assert out["footprint"]["type"] == "Polygon"                          # the snap's footprint
    corners = box_corners(lat_of(100), LON)
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"corners": corners}, JULIAN)
    assert code == 200 and out["changed"] == ["corners"] and out["doc"]["aligned_by"] == "Julian"
    assert out["doc"]["corners"] == [[round(x, 8) for x in p] for p in corners]
    mirrored = [corners[1], corners[0], corners[3], corners[2]]
    crossed = [corners[0], corners[2], corners[1], corners[3]]
    far = [[x + 0.02, y] for x, y in corners]
    tiny = box_corners(lat_of(100), LON, 0.000001, 0.000001)
    for bad in (mirrored, crossed, far, tiny, corners[:3], [["a", 1]] * 4, [[float("nan"), 41]] * 4):
        assert call(api, f"/api/cad/preplan/docs/{did}", {"corners": bad}, JULIAN)[0] == 400, bad
    doc = call(api, f"/api/cad/preplan/docs/{did}", {"corners": None}, JULIAN)[1]["doc"]
    assert doc["corners"] is None
    other = upload(api, pdf(), kind="contacts", title="Owner", floor="")[1]["doc"]["id"]
    assert call(api, f"/api/cad/preplan/docs/{other}", {"corners": corners}, JULIAN)[0] == 400   # not a plan
    audit = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id", (ppd.SECTION,))
    assert [a["key"] for a in audit][:3] == [f"{did} Floor plan"] * 3
    assert json.loads(audit[0]["new"])["title"] == "First floor"
    assert json.loads(audit[1]["new"])["corners"] and json.loads(audit[1]["old"]) == {"corners": None}
    acts = [r["summary"] for r in c.activity.rows(kind="action")]
    assert f"Aligned site doc {did}" in acts and any(s.startswith(f"Uploaded site doc {did} (Floor plan)") for s in acts)


def test_every_upload_edit_alignment_and_delete_is_audited(api):
    c = api.cad
    did = upload(api, png())[1]["doc"]["id"]
    call(api, f"/api/cad/preplan/docs/{did}", {"title": "Ground floor", "floor": "G"}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": box_corners(lat_of(100), LON)}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, COREY)
    audit = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id", (ppd.SECTION,))
    assert [sorted(json.loads(a["new"])) for a in audit] == [sorted(ppd.AUDITED), ["floor", "title"], ["corners"], ["active"]]
    assert [a["operator"].split(" <")[0] for a in audit] == ["Julian", "Julian", "Julian", "Corey"]
    acts = [r["summary"] for r in c.activity.rows(kind="action")][:4]
    assert acts == [f"Deleted site doc {did}", f"Aligned site doc {did}", f"Edited site doc {did}: floor, title",
                    f"Uploaded site doc {did} (Floor plan)" + (f" from {c.store.incident(api.iid)['number']}"
                                                              if c.store.incident(api.iid)["number"] else "")]
    assert not list((Path(c.preplan_docs.store.root)).rglob("*.png"))    # the bytes are gone
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] == []
    sha = c.store.q("SELECT sha256 FROM preplan_docs WHERE id = ?", (did,))[0]["sha256"]
    assert sha == hashlib.sha256(docfiles.clean_png(png())[0]).hexdigest()   # the record keeps what was there


def test_no_titles_filenames_notes_or_coordinates_in_the_logs(api):
    c = api.cad
    log = []
    c.activity.log = log.append
    c.log = log.append
    code, out = upload(api, png(), title="Knox box behind blue door", floor="Mezzanine")
    did = out["doc"]["id"]
    corners = box_corners(41.0123456, -73.6543219)
    call(api, f"/api/cad/preplan/docs/{did}", {"title": "Alarm panel code room"}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": [[x, y] for x, y in box_corners(lat_of(100), LON)]}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": corners}, JULIAN)          # refused: too far
    get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    upload(api, SVG, title="evil-plan.svg")
    c.preplan_docs.store = s3_store()[0]
    c.preplan_docs.store.s3.send = lambda *a: (500, {}, b"<Code>InternalError</Code>")
    upload(api, png(), title="Secret riser room.png")                                   # a store failure
    get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    text = "\n".join(map(str, log))
    assert log and f"site doc {did}" in text
    for s in ("blue door", "Knox", "Mezzanine", "Alarm panel", "evil-plan", "Secret riser", ".svg", "41.0", "73.6", "41.012",
              str(round(lat_of(100), 4))):
        assert s not in text, s


# -- the page -------------------------------------------------------------------------------------------------------------

def test_the_page_loads_the_script_and_hooks_it_in():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/preplan.js"') < html.index('src="/static/preplan-docs.js"')
    grid = html[html.index('<div id="d-grid"'):html.index('<form id="edit-form"')]
    assert grid.index('id="d-preplan"') < grid.index('id="d-docs"') < grid.index('id="d-cams"')
    assert 'id="sv-pp-docs"' in html
    cad = (STATIC / "cad.js").read_text()
    assert 'if (window.PreplanDocs) PreplanDocs.card(inc, { readonly: BOARD || !canDo("dispatch") });' in cad
    src = (STATIC / "preplan-docs.js").read_text()
    assert "MapLayers.addGroup(" in src
    for f in ("cadmap.js", "map-style.js"):
        assert "preplan" not in (STATIC / f).read_text().lower(), f


def test_the_script_draws_the_plan_under_the_points_and_uploads_with_its_header():
    src = (STATIC / "preplan-docs.js").read_text()
    # the plan: an image source and a raster layer, under the pre-plan points (and so the routes, rigs and pins)
    assert 'type: "image"' in src and 'type: "raster"' in src
    assert 'const ABOVE = ["pp-edit-fp", "pp-icons", "pp-labels", "cm-lines", "cm-trail", "cm-routes", "firehouse"];' in src
    assert '"raster-opacity"' in src and "setCoordinates(" in src
    # the upload: the file as the body, its details in X-Sizeup-Doc; opened through the hub's link
    assert '"X-Sizeup-Doc": encodeURIComponent(JSON.stringify(' in src
    assert "`/api/cad/preplan/docs/${d.id}/file`" in src and "`/api/cad/preplan/docs/${d.id}/plan`" in src
    assert 'accept = "application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg"' in src
    for line in src.splitlines():                                         # never a title or a corner in the console
        assert not re.search(r"console\.\w+\([^)]*\b(title|corners|lat|lon|name)\b", line), line
    assert "localStorage" in src and re.search(r"try \{[^}]*localStorage", src)


NODE_HARNESS = r"""
global.window = global;
require(process.argv[2]);
const D = window.PreplanDocs;
const out = {};
out.kinds = Object.keys(D.KINDS);
// a footprint 40 m east-west, 10 m north-south, rotated 30 degrees (counter-clockwise), at 41 N
const lat0 = 41.0, lon0 = -73.66, kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110574;
const rot = (x, y, a) => [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
const a = 30 * Math.PI / 180;
const ring = [[-20, -5], [20, -5], [20, 5], [-20, 5], [-20, -5]].map(([x, y]) => { const [u, v] = rot(x, y, a); return [lon0 + u / kx, lat0 + v / ky]; });
out.obb = D.obb(ring);
out.snapWide = D.snap(ring, 800, 200, false);
out.snapTall = D.snap(ring, 200, 800, false);
out.snapNorth = D.snap(ring, 800, 200, true);
out.valid = D.valid(out.snapWide);
const c = out.snapWide;
out.moved = D.move(c, [lon0 + 10 / kx, lat0], [lon0, lat0]);
out.rotated = D.rotate(c, Math.PI / 2);
out.scaled = D.scale(c, 2);
out.corner = D.setCorner(c, 0, [c[0][0] - 1 / kx, c[0][1]]);
out.mirrored = D.valid([c[1], c[0], c[3], c[2]]);
out.size = D.sizeM(c);
out.floors = D.floorOrder(["2", "Roof", "1", "B1", "", "Mezz", "B2", "G"]);
out.pick = D.pickPlan([{ id: 1, kind: "floor_plan", floor: "2", corners: c }, { id: 2, kind: "floor_plan", floor: "1", corners: c },
                       { id: 3, kind: "site_plan", floor: "", corners: c }, { id: 4, kind: "floor_plan", floor: "B1", corners: null }]);
out.pickNone = D.pickPlan([{ id: 4, kind: "floor_plan", floor: "B1", corners: null }]);
out.count = D.countText(3);
out.before = D.beforeId(["bg", "gis-buildings", "pp-edit-fp", "pp-icons", "cm-lines"]);
out.beforeNoPP = D.beforeId(["bg", "cm-trail", "firehouse"]);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_overlay_math_in_node(tmp_path):
    import math
    h = tmp_path / "h.js"
    h.write_text(NODE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "preplan-docs.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["kinds"] == list(ppd.KINDS)                                   # one list, both ends
    o = out["obb"]
    assert abs(o["w"] - 40) < 0.5 and abs(o["h"] - 10) < 0.5               # the oriented box: 40 x 10 m
    assert abs(math.degrees(o["angle"]) % 180 - 30) < 1
    kx, ky = 111320 * math.cos(math.radians(41.0)), 110574

    def m(c):
        return [((x + 73.66) * kx, (y - 41.0) * ky) for x, y in c]
    tl, tr, br, bl = m(out["snapWide"])
    assert abs(math.hypot(tr[0] - tl[0], tr[1] - tl[1]) - 40) < 0.5        # the image's width along the long side
    assert abs(math.hypot(bl[0] - tl[0], bl[1] - tl[1]) - 10) < 0.5
    assert tl[1] > bl[1]                                                    # its top is the north side
    assert abs(math.degrees(math.atan2(tr[1] - tl[1], tr[0] - tl[0])) - 30) < 1
    tl, tr, br, bl = m(out["snapTall"])                                       # a tall image: its height along the long side
    assert abs(math.hypot(bl[0] - tl[0], bl[1] - tl[1]) - 40) < 0.5
    tl, tr, br, bl = m(out["snapNorth"])                                      # north up: the footprint's lon/lat box
    assert abs(tl[1] - tr[1]) < 0.01 and abs(tl[0] - bl[0]) < 0.01 and tr[0] > tl[0] and tl[1] > bl[1]
    assert out["valid"] is True and out["mirrored"] is False
    assert all(abs((a[0] - b[0]) * kx - 10) < 0.01 for a, b in zip(out["moved"], out["snapWide"]))
    w, h = out["size"]
    assert abs(w - 40) < 0.5 and abs(h - 10) < 0.5
    rt = m(out["rotated"])
    assert abs(math.degrees(math.atan2(rt[1][1] - rt[0][1], rt[1][0] - rt[0][0])) - 120) < 1
    sc = m(out["scaled"])
    assert abs(math.hypot(sc[1][0] - sc[0][0], sc[1][1] - sc[0][1]) - 80) < 0.5
    assert out["corner"][1:] == out["snapWide"][1:] and out["corner"][0] != out["snapWide"][0]
    assert out["floors"] == ["B2", "B1", "G", "1", "Mezz", "2", "Roof", ""]
    assert out["pick"] == 2 and out["pickNone"] is None                     # floor 1 first; an unaligned plan: none
    assert out["count"] == "Site docs (3)"
    assert out["before"] == "pp-edit-fp" and out["beforeNoPP"] == "cm-trail"
    py = sorted(["2", "Roof", "1", "B1", "", "Mezz", "B2", "G"], key=ppd.floor_key)
    assert py == out["floors"]                                              # the hub's order and the page's agree
