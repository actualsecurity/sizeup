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


def pdf(pages=1, extra=b"", page_extra=b""):
    """A small PDF written here: each page a framed rectangle (a "floor plan"); extra: more names in its catalog;
    page_extra: in each page's dictionary."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R " + extra + b">>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)).encode()
    objs.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>")
    for i in range(pages):
        stream = f"0.2 g 50 50 500 {300 + 50 * i} re S 0.8 g 60 60 480 {280 + 50 * i} re f".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 {400 + 50 * i}] /Contents {4 + 2 * i} 0 R ".encode()
                    + page_extra + b">>")
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
    assert d["bytes"] == len(data) and d["plan"] is False
    clean, w, h = docfiles.clean_jpeg(jpeg_with_gps(orientation=1))
    assert 0x0112 not in Image.open(io.BytesIO(clean)).getexif() and (w, h) == (64, 48)


def test_png_text_and_exif_chunks_are_dropped():
    text = png_chunk(b"tEXt", b"Comment\0secret") + png_chunk(b"eXIf", b"MM\0*\0\0\0\x08\0\0") + png_chunk(b"iTXt", b"x\0\0\0\0\0y")
    clean, w, h = docfiles.clean_png(png(extra=text, tail=b"<script>alert(1)</script>"))
    assert (w, h) == (40, 30)
    assert b"tEXt" not in clean and b"eXIf" not in clean and b"secret" not in clean and b"<script" not in clean
    assert clean == png()


ACTIVE_NAMES = (b"/URI", b"/JS", b"/JavaScript", b"/Launch", b"/AA", b"/OpenAction", b"/SubmitForm", b"/A ", b"/A<")


def inflated(data: bytes) -> bytes:
    """The PDF with every stream inflated (FlateDecode) beside it: what a name could hide in."""
    out = [data]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            pass
    return b"\n".join(out)


def stored_pdf(api, did):
    ref = api.cad.store.q("SELECT ref FROM preplan_docs WHERE id = ?", (did,))[0]["ref"]
    return (Path(api.cad.preplan_docs.store.root) / "preplan-docs" / ref / "original.pdf").read_bytes()


def test_attachments_xfa_and_encryption_are_refused(api):
    for name in (b"/Names << /EmbeddedFiles 9 0 R >>", b"/AcroForm << /XFA 9 0 R >>", b"/Encrypt 9 0 R"):
        code, out = upload(api, pdf(extra=name), kind="inspection", title="Annual")
        assert code == 400 and "PDF" in out["error"], name
    code, out = upload(api, pdf(), kind="inspection", title="Annual", floor="")
    assert code == 200, out


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_links_actions_and_scripts_are_stripped_from_the_stored_copy(api):
    """Vendor and county exports have links, a GoTo /OpenAction, page actions, form fields: they upload, and what is
    stored has no action at all (in its streams either)."""
    annots = (b"/Annots [<< /Type /Annot /Subtype /Link /Rect [10 10 90 30] /A << /S /URI /URI (https://example.com/) >> >> "
              b"<< /Type /Annot /Subtype /Square /Rect [100 100 200 200] /C [1 0 0] "
              b"/AA << /E << /S /Launch /F (x.exe) >> >> /A << /S /JavaScript /JS (app.alert(1)) >> >> "
              b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (Owner) /V (Example) /Rect [300 300 400 320] "
              b"/AA << /K << /S /JavaScript /JS (event.rc = true) >> >> >>] "
              b"/AA << /O << /S /JavaScript /JS (app.alert(2)) >> >> ")
    catalog = (b"/OpenAction << /S /GoTo /D [3 0 R /Fit] >> /AA << /WC << /S /JavaScript /JS (x) >> >> "
               b"/Names << /JavaScript << /Names [(a) << /S /JavaScript /JS (app.alert(3)) >>] >> >> "
               b"/AcroForm << /Fields [] /NeedAppearances true >> ")
    for extra, page_extra in ((catalog, annots), (b"/OpenAction << /S /J#61vaScript /JS (x) >>", b""),
                              (b"/OpenAction << /S /Launch /F (x.exe) >>", b""), (b"", b"/AA << /O 9 0 R >> ")):
        raw = pdf(extra=extra, page_extra=page_extra)
        code, out = upload(api, raw, kind="preplan", title="Vendor export", floor="")
        assert code == 200, (out, extra, page_extra)
        clean = inflated(stored_pdf(api, out["doc"]["id"]))
        for name in ACTIVE_NAMES:
            assert name not in clean, (name, extra, page_extra)
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(stored_pdf(api, get(api, "/api/cad/preplan/docs", f"incident={api.iid}",
                                                         VAL)[1]["docs"][0]["id"])))
    assert len(reader.pages) == 1
    code, out = upload(api, pdf(extra=catalog, page_extra=annots), title="Plan with links", floor="1")   # a plan too
    assert code == 200 and out["doc"]["plan"] is True
    page = PdfReader(io.BytesIO(stored_pdf(api, out["doc"]["id"]))).pages[0]
    kinds = [a.get_object()["/Subtype"] for a in page["/Annots"]]
    assert kinds == ["/Square", "/Widget"]                                  # the link gone, the markup and field kept


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
    assert out["doc"]["corners"] is None
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
    assert rows and not [r for r in rows if r["key"].split()[0] == str(did)]          # not there at all
    assert not any("Alarm" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    mine = [r for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"]
            if r["key"].split()[0] == str(did)]
    assert json.loads(mine[0]["new"]) == {"admin_only": True, "changed": sorted(ppd.AUDITED)}      # redacted once
    # a document made admin-only later: its earlier rows too
    lobby = next(d for d in get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] if d["title"] == "Lobby")
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, JULIAN)[0] == 400
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, COREY)[0] == 200
    rows = get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", JULIAN)[1]["audit"]
    assert not any("Lobby" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    assert not [r for r in rows if r["key"].split()[0] == str(lobby["id"])]
    assert not any('"changed":["admin_only","changed"]' in (r["new"] or "") for r in
                   get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"])
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
    (key, (body, ctype)), = [(k, v) for k, v in fake.objects.items() if "/original." in k]
    assert any(k.endswith("/plan.png") for k in fake.objects)
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
    corners = box_corners(0.5123456, 0.5432198)
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
    for s in ("blue door", "Knox", "Mezzanine", "Alarm panel", "evil-plan", "Secret riser", ".svg", "0.512", "0.543",
              str(round(lat_of(100), 4)), str(LON)):
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
    assert "`/api/cad/preplan/docs/${d.id}/file`" in src
    assert "`/api/cad/preplan/docs/${d.id}/plan?v=${d.page || 0}-${d.plan_w}x${d.plan_h}`" in src   # a new page: a new URL
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
// a footprint 40 m east-west, 10 m north-south, rotated 30 degrees (counter-clockwise), at 0.5 N 0.5 E (nowhere real)
const lat0 = 0.5, lon0 = 0.5, kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110574;
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
    kx, ky = 111320 * math.cos(math.radians(0.5)), 110574

    def m(c):
        return [((x - 0.5) * kx, (y - 0.5) * ky) for x, y in c]
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


# -- the security review of #464 (each a regression test) ----------------------------------------------------------------

def objstm_pdf(action: bytes):
    """A PDF 1.5 whose catalog and its /OpenAction live in a compressed object stream (FlateDecode), found through a
    cross-reference stream: nothing active is visible in the raw bytes."""
    inner = [(1, b"<< /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >>"), (4, action)]
    head, body = b"", b""
    for n, o in inner:
        head += f"{n} {len(body)} ".encode()
        body += o + b" "
    raw = head + body
    packed = zlib.compress(raw)
    objs = {2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
            5: b"<< /Type /ObjStm /N 2 /First " + str(len(head)).encode() + b" /Filter /FlateDecode /Length "
               + str(len(packed)).encode() + b" >>\nstream\n" + packed + b"\nendstream"}
    out, offs = b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n", {}
    for n in (2, 3, 5):
        offs[n] = len(out)
        out += f"{n} 0 obj\n".encode() + objs[n] + b"\nendobj\n"
    offs[6] = len(out)
    rows = [(0, 0, 65535), (2, 5, 0), (1, offs[2], 0), (1, offs[3], 0), (2, 5, 1), (1, offs[5], 0), (1, offs[6], 0)]
    xs = b"".join(struct.pack(">BIH", *r) for r in rows)
    out += (b"6 0 obj\n<< /Type /XRef /Size 7 /W [1 4 2] /Root 1 0 R /Length " + str(len(xs)).encode()
            + b" >>\nstream\n" + xs + b"\nendstream\nendobj\n")
    return out + f"startxref\n{offs[6]}\n%%EOF\n".encode()


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_actions_hidden_in_a_compressed_object_stream_never_reach_the_stored_copy(api):
    for action in (b"<< /S /JavaScript /JS (app.alert(1)) >>", b"<< /S /URI /URI (https://example.com/) >>",
                   b"<< /S /Launch /F (x) >>", b"<< /S /Named /N /Print >>", b"<< /D [3 0 R /Fit] /S /GoTo >>",
                   b"[3 0 R /Fit]"):
        data = objstm_pdf(action)
        docfiles.check_pdf(data)                                   # the raw bytes show nothing
        assert b"JavaScript" not in data and b"/URI" not in data
        code, out = upload(api, data, kind="inspection", title="Annual", floor="")
        assert code == 200, (out, action)
        clean = inflated(stored_pdf(api, out["doc"]["id"]))
        for name in ACTIVE_NAMES:
            assert name not in clean, (name, action)
    with pytest.raises(docfiles.Refused):                          # an attachment inside one: refused all the same
        docfiles.render(objstm_pdf(b"<< /Type /Filespec /F (x) /EF << /F 3 0 R >> >>"), 0)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_the_stored_pdf_is_saved_again_without_its_metadata(api):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(300, 200)
    w.add_metadata({"/Author": "Example Author", "/Title": "Example inner title"})
    b = io.BytesIO()
    w.write(b)
    raw = b.getvalue()
    assert b"Example Author" in raw
    code, out = upload(api, raw, kind="inspection", title="Annual", floor="")
    assert code == 200, out
    stored = next(Path(api.cad.preplan_docs.store.root).rglob("original.pdf")).read_bytes()
    assert stored.startswith(b"%PDF-") and b"Example Author" not in stored and b"Example inner title" not in stored
    assert api.cad.store.q("SELECT sha256 FROM preplan_docs")[0]["sha256"] == hashlib.sha256(stored).hexdigest()


def test_without_a_pdf_reader_pdfs_are_refused(api, monkeypatch):
    monkeypatch.setattr(docfiles, "can_render", lambda: False)
    code, out = upload(api, pdf(), kind="inspection", title="Annual", floor="")
    assert code == 400 and "PDF" in out["error"]


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_the_child_gets_none_of_the_hubs_environment(monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", "example-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/example")
    seen = []
    real = subprocess.run

    def run(*a, **kw):
        seen.append(kw.get("env"))
        return real(*a, **kw)
    monkeypatch.setattr(docfiles.subprocess, "run", run)
    assert docfiles.render(pdf(), 1)["png"].startswith(b"\x89PNG")
    env, = seen
    assert env is not None and not [k for k in env if k.startswith(("SIZEUP_", "AWS_"))]
    assert set(env) <= {"PATH", "LC_ALL"}
    # and in the child itself
    out = real([docfiles.sys.executable, "-I", "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
               env=docfiles.child_env(), capture_output=True, text=True, check=True)
    assert not [k for k in json.loads(out.stdout) if k.startswith(("SIZEUP_", "AWS_"))]


def test_the_hub_process_is_made_non_dumpable():
    code = ("import sys; from sizeup.docfiles import no_dump; r = no_dump(); "
            "d = None\nif sys.platform.startswith('linux'):\n import ctypes; d = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)\n"
            "print(r, d)")
    out = subprocess.run([docfiles.sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=str(Path(__file__).resolve().parent.parent)).stdout.split()
    if docfiles.sys.platform.startswith("linux"):
        assert out == ["True", "0"]                                 # PR_GET_DUMPABLE: 0
    else:
        assert out == ["False", "None"]                             # a no-op elsewhere


def big_image_pdf(side=6000):
    """A page drawing one large grey image (6000 x 6000 px, FlateDecode: small on disk, 36 MB decoded)."""
    img = zlib.compress(bytes(side * side), 9)
    content = b"q 600 0 0 600 0 0 cm /Im1 Do Q"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 600] /Resources << /XObject << /Im1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width " + str(side).encode() + b" /Height " + str(side).encode()
            + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(img)).encode()
            + b" >>\nstream\n" + img + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_a_large_image_pdf_renders_within_the_limits_or_is_refused(monkeypatch):
    data = big_image_pdf()
    try:
        r = docfiles.render(data, 1)
    except docfiles.Refused:
        r = None
    if r is not None:
        assert max(r["w"], r["h"]) <= docfiles.MAX_PX and r["w"] * r["h"] <= docfiles.RENDER_PIXELS
    assert docfiles.RLIMIT_AS_MB <= 800
    monkeypatch.setattr(docfiles, "RENDER_PIXELS", 1000)               # the pixels capped before rendering
    with pytest.raises(docfiles.Refused):
        docfiles.render(pdf(), 1)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_one_render_at_a_time(monkeypatch):
    monkeypatch.setattr(docfiles, "RENDER_WAIT_S", 0.05)
    assert docfiles._RENDER.acquire(timeout=1)
    try:
        with pytest.raises(docfiles.Refused, match="Busy"):
            docfiles.render(pdf(), 1)
    finally:
        docfiles._RENDER.release()
    assert docfiles.render(pdf(), 1)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_another_page_clears_the_alignment_and_says_so(api):
    c = api.cad
    did = upload(api, pdf(pages=2), page=1)[1]["doc"]["id"]
    assert call(api, f"/api/cad/preplan/docs/{did}", {"corners": box_corners(lat_of(100), LON)}, JULIAN)[0] == 200
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"page": 2}, JULIAN)
    assert code == 200 and out["doc"]["corners"] is None and out["changed"] == ["corners", "page"]
    last = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id DESC LIMIT 1", (ppd.SECTION,))[0]
    assert json.loads(last["new"])["corners"] is None and json.loads(last["old"])["corners"]
    assert f"Aligned site doc {did} and edited: page" in [r["summary"] for r in c.activity.rows(kind="action")][:1]
    # a kind that isn't a plan: no picture, no corners
    out = call(api, f"/api/cad/preplan/docs/{did}", {"kind": "inspection"}, JULIAN)[1]
    assert out["doc"]["plan"] is False and out["doc"]["corners"] is None
    assert not list(Path(c.preplan_docs.store.root).rglob("plan.png"))


def test_without_a_verified_person_the_reader_is_a_viewer(api):
    c = api.cad
    did = upload(api, png(), email=COREY, admin_only=True, title="Panel")[1]["doc"]["id"]
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", None)[1]["docs"] == []          # all-in-one: a viewer
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", None)[0] == 404
    with pytest.raises(ppd.CadError):
        c.preplan_docs.edit(did, {"title": "x"})                                                    # no _may: a viewer
    with pytest.raises(ppd.CadError):
        c.preplan_docs.upload({"kind": "photo", "title": "x", "incident_id": api.iid, "admin_only": True}, png())
    assert c.preplan_docs.edit(did, {"title": "y", "_may": "admin"})["ok"]                         # admin: explicit


def test_the_upload_slot_is_taken_before_the_body_is_read(api, monkeypatch):
    from sizeup.web.server import WebServer
    monkeypatch.setattr(ppd, "UPLOAD_WAIT_S", 0.1)
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    held = [ppd.UPLOADS.acquire(timeout=1) for _ in range(2)]
    try:
        assert all(held)
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/cad/preplan/docs")
        meta = quote(json.dumps({"kind": "photo", "title": "x", "incident_id": api.iid}))
        for k, v in {"X-Sizeup-Doc": meta, "Content-Length": str(20 * 1024 * 1024)}.items():
            conn.putheader(k, v)
        conn.endheaders()                                                    # 20 MB announced, never sent
        r = conn.getresponse()
        assert r.status == 503 and r.getheader("Retry-After")
    finally:
        for _ in held:
            ppd.UPLOADS.release()
        srv.httpd.shutdown()
    assert upload(api, png())[0] == 200


def test_a_delete_marks_the_row_first_and_the_sweep_takes_what_was_left(api, monkeypatch):
    import time as _time
    c = api.cad
    st = c.preplan_docs.store
    did = upload(api, png(), kind="photo", title="Rear", floor="")[1]["doc"]["id"]
    real = st.delete

    def broken(key):
        raise docstore.DocStoreError("S3 DELETE: 503")
    monkeypatch.setattr(st, "delete", broken)
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, JULIAN)
    assert code == 200 and out["doc"]["active"] is False                       # deleted for everyone at once
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", COREY)[0] == 404
    left = list(Path(st.root).rglob("original.*"))
    assert len(left) == 1                                                      # the bytes: still there
    monkeypatch.setattr(st, "delete", real)
    assert c.preplan_docs.sweep(now=_time.time()) == 0                         # not yet: inside the margin
    assert c.preplan_docs.sweep(now=_time.time() + ppd.SWEEP_MARGIN_S + 60) == 1
    assert not list(Path(st.root).rglob("original.*"))
    assert "site doc" in "\n".join(map(str, c.logged)) and "Rear" not in "\n".join(map(str, c.logged))


def test_the_sweep_keeps_what_documents_name_and_stops_when_too_much_would_go(api, tmp_path):
    import time as _time
    c = api.cad
    st = c.preplan_docs.store
    keep = upload(api, png(), kind="photo", title="Rear", floor="")[1]["doc"]
    later = _time.time() + ppd.SWEEP_MARGIN_S + 60
    outside = Path(st.root) / "other" / "0123456789abcdef" / "original.png"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    stray = Path(st.root) / "preplan-docs" / "0123456789abcdef" / "original.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    odd = Path(st.root) / "preplan-docs" / "notes.txt"
    odd.write_bytes(b"x")
    c.preplan_docs.may_write = lambda: False
    assert c.preplan_docs.sweep(now=later) == 0 and stray.exists()            # only the leader
    c.preplan_docs.may_write = lambda: True
    assert c.preplan_docs.sweep(now=later) == 1
    assert not stray.exists() and outside.exists() and odd.exists()           # its own shape, under its prefix only
    assert len(list(Path(st.root).rglob("original.png"))) == 2                # the document's own: kept (and outside)
    for i in range(ppd.SWEEP_MAX_N + 5):
        f = Path(st.root) / "preplan-docs" / f"{i:016x}" / "original.pdf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    assert c.preplan_docs.sweep(now=later) == 0                               # the circuit breaker
    assert "too many, nothing deleted" in c.preplan_docs.sweep_status
    assert len(list((Path(st.root) / "preplan-docs").rglob("original.pdf"))) == ppd.SWEEP_MAX_N + 5
    assert keep["id"]


def test_plan_pictures_are_upright_and_at_most_4096_a_side(api):
    from PIL import Image
    c = api.cad
    d = upload(api, png(4200, 30), title="Long", floor="1")[1]["doc"]
    assert (d["plan_w"], d["plan_h"]) == (4096, 29) or (d["plan_w"], d["plan_h"]) == (4096, 30)
    code, out = upload(api, jpeg_with_gps(orientation=6), title="Phone photo of a plan", floor="2")
    assert code == 200, out
    assert (out["doc"]["plan_w"], out["doc"]["plan_h"]) == (48, 64)          # turned upright
    ref = c.store.q("SELECT ref FROM preplan_docs WHERE id = ?", (out["doc"]["id"],))[0]["ref"]
    plan = (Path(c.preplan_docs.store.root) / "preplan-docs" / ref / "plan.png").read_bytes()
    im = Image.open(io.BytesIO(plan))
    assert im.size == (48, 64) and not im.getexif() and not im.info.get("exif")
    assert ppd.PLAN_MAX_SIDE == docfiles.MAX_PX == 4096


def test_jfxx_thumbnails_and_other_app_segments_are_dropped():
    raw = jpeg_with_gps(orientation=1)
    jfxx = b"\xff\xe0" + struct.pack(">H", 2 + 5 + 4) + b"JFXX\0" + b"\x10\x01\x01\x00"
    app2 = b"\xff\xe2" + struct.pack(">H", 2 + 8) + b"MPF\0\0\0\0\0"
    data = raw[:2] + jfxx + app2 + raw[2:]
    clean, w, h = docfiles.clean_jpeg(data)
    assert b"JFXX" not in clean and b"MPF\0" not in clean and (w, h) == (64, 48)


def test_local_files_are_never_readable_by_others(tmp_path):
    import os
    st = docstore.FsDocStore(tmp_path)
    k = st.key("0123456789abcdef", "original.png")
    st.put(k, b"x", "image/png")
    assert os.stat(tmp_path / k).st_mode & 0o777 == 0o600


def test_the_s3_store_can_serve_through_the_hub_instead_of_links(api):
    st, fake = s3_store()
    st.links = False
    api.cad.preplan_docs.store = st
    did = upload(api, png())[1]["doc"]["id"]
    code, raw, h = get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    assert code == 200 and raw.startswith(b"\x89PNG") and h["X-Content-Type-Options"] == "nosniff"
    assert "Location" not in h
